from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import logging
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet

from app import content_store
from app.db_pg import (
    CUSTOMER_PRODUCTION_ENV,
    DatabaseMode,
    PgReadyInfo,
    check_pg_ready,
    close_pg_pool,
    pg_transaction,
    resolve_database_config,
    validate_customer_production,
)
from app.db_portable import BusinessConnection
from app.settings import (
    DEFAULT_BILLING_SETTINGS,
    DEFAULT_RUNTIME_SETTINGS,
    SETTINGS_KEY_ENV,
    SettingsRepository,
    normalize_config,
    validate_provider_config,
)
from app.storage import (
    StorageAdapter,
    cloud_storage_config_from_settings,
    create_storage_adapter,
)

logger = logging.getLogger(__name__)

# T09 / DB-08 — customer-production security gate (dev doc §17).
_TRUTHY = {"1", "true", "yes", "on"}
_ADMIN_SESSION_HMAC_KEY_ENV = "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY"
_ADMIN_KEY_VERSION_PREFIX = f"{_ADMIN_SESSION_HMAC_KEY_ENV}_V"
CUSTOMER_PUBLIC_ORIGIN_ENV = "VIDEO_REPLICA_PUBLIC_ORIGIN"
PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
TRUSTED_PROXY_CIDRS_ENV = "VIDEO_REPLICA_TRUSTED_PROXY_CIDRS"
_ACTIVATION_CODE_HMAC_KEY_ENV = "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY"
_ACTIVATION_EXPORT_AEAD_KEY_ENV = "VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY"
_DEVICE_FINGERPRINT_HMAC_KEY_ENV = "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY"
_CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV = "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY"
# Keep in sync with admin_auth_routes.MIN_HMAC_KEY_BYTES; importing the route
# module here would drag the FastAPI dependency chain into bootstrap.
_MIN_ADMIN_KEY_BYTES = 32
_MIN_HMAC_KEY_BYTES = 32
_AEAD_KEY_BYTES = 32
_MAX_KEY_VERSION = 64
_LEGACY_CONTROL_ENVS = ("CONTROL_PROXY_TOKEN_DIGEST", "CONTROL_ADMIN_USER_ID")
_DEV_AUTH_MODES = {"desktop", "development"}
_EMPTY_CUSTOMER_BOOTSTRAP_LOCK_ID = 836_036
_FIRST_ADMIN_CREDENTIAL_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class EmptyCustomerProvisionResult:
    admin_user_id: str
    exchange_credential: str


def issue_exchange_credential(
    actor_user_id: str,
    *,
    ttl_seconds: int,
    key_version: int,
) -> str:
    """Mint without importing the FastAPI route layer during normal bootstrap."""
    from app.admin_auth_routes import issue_exchange_credential as mint_credential

    return mint_credential(
        actor_user_id,
        ttl_seconds=ttl_seconds,
        key_version=key_version,
    )


def is_customer_production(*, environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


def customer_public_origin(*, environ: Mapping[str, str] | None = None) -> str:
    """Return the canonical HTTPS browser origin for the customer deployment."""
    source = os.environ if environ is None else environ
    raw = source.get(CUSTOMER_PUBLIC_ORIGIN_ENV, "").strip()
    if not raw:
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} is required")
    if any(character.isspace() for character in raw):
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} must not contain whitespace")
    try:
        parsed = urlsplit(raw)
        parsed_hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} is not a valid origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed_hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{CUSTOMER_PUBLIC_ORIGIN_ENV} must be an HTTPS origin without credentials, "
            "path, query or fragment"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} has an invalid port") from exc
    if port not in (None, 443):
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} must use the standard HTTPS port 443")
    try:
        hostname = parsed_hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} has an invalid hostname") from exc
    authority = f"[{hostname}]" if ":" in hostname else hostname
    return f"https://{authority}"


def trusted_proxy_networks(
    *, environ: Mapping[str, str] | None = None
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the exact proxy/LB networks allowed to assert forwarding headers."""
    source = os.environ if environ is None else environ
    raw = source.get(TRUSTED_PROXY_CIDRS_ENV, "").strip()
    if not raw:
        raise ValueError(f"{TRUSTED_PROXY_CIDRS_ENV} is required")
    values = [item.strip() for item in raw.split(",")]
    if not values or any(not item for item in values):
        raise ValueError(f"{TRUSTED_PROXY_CIDRS_ENV} contains an empty entry")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(
                f"{TRUSTED_PROXY_CIDRS_ENV} contains an invalid canonical CIDR: {value}"
            ) from exc
        if network.prefixlen == 0:
            raise ValueError(f"{TRUSTED_PROXY_CIDRS_ENV} must not trust the entire internet")
        networks.append(network)
    return tuple(networks)


def _configured_versioned_values(
    environ: Mapping[str, str], base_env: str
) -> list[tuple[str, str]]:
    """Resolve configured rotation values with explicit ``_V1`` precedence."""
    values: dict[int, tuple[str, str]] = {}
    base_value = environ.get(base_env, "").strip()
    if base_value:
        values[1] = (base_env, base_value)
    prefix = f"{base_env}_V"
    for name in sorted(environ):
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        value = environ[name].strip()
        if (
            value
            and suffix.isdigit()
            and 1 <= int(suffix) <= _MAX_KEY_VERSION
            and not suffix.startswith("0")
        ):
            values[int(suffix)] = (name, value)
    return [values[version] for version in sorted(values)]


def _append_raw_hmac_key_violations(
    violations: list[str], environ: Mapping[str, str], base_env: str
) -> None:
    configured = _configured_versioned_values(environ, base_env)
    if not configured:
        violations.append(f"{base_env} is missing: configure at least one key version")
        return
    for name, value in configured:
        if len(value.encode("utf-8")) < _MIN_HMAC_KEY_BYTES:
            violations.append(f"{name} must be at least {_MIN_HMAC_KEY_BYTES} bytes")


def _append_aead_key_violations(
    violations: list[str], environ: Mapping[str, str], base_env: str
) -> None:
    configured = _configured_versioned_values(environ, base_env)
    if not configured:
        violations.append(f"{base_env} is missing: configure at least one key version")
        return
    for name, value in configured:
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, binascii.Error):
            violations.append(f"{name} must be valid urlsafe base64")
            continue
        if len(decoded) != _AEAD_KEY_BYTES:
            violations.append(f"{name} must decode to exactly {_AEAD_KEY_BYTES} bytes")


def _configured_admin_session_keys(
    environ: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Every configured admin-session HMAC key variable (un-suffixed or ``_VN``).

    Key rotation retires old versions once outstanding credentials expire, so
    any configured version (e.g. only ``_V2`` after retiring ``_V1``) must keep
    customer production booting instead of tripping a V1-only check.
    """
    found: list[tuple[str, str]] = []
    for name in sorted(environ):
        value = environ[name].strip()
        if not value:
            continue
        if name == _ADMIN_SESSION_HMAC_KEY_ENV:
            found.append((name, value))
        elif name.startswith(_ADMIN_KEY_VERSION_PREFIX):
            suffix = name[len(_ADMIN_KEY_VERSION_PREFIX) :]
            # Reject zero-padded suffixes like _V01: they pass int() >= 1 but
            # admin_hmac_key(1) only reads _V1, so accepting them would boot
            # the door open while every login 401s (M1 review LOW).
            if suffix.isdigit() and int(suffix) >= 1 and not suffix.startswith("0"):
                found.append((name, value))
    return found


def customer_production_security_violations() -> list[str]:
    """List every customer-production boundary violation in the environment.

    No-op outside customer production so internal P0 deployments keep their
    dev identity, local assets and legacy control proxy token.
    """
    if not is_customer_production():
        return []
    violations: list[str] = []
    legacy = [name for name in _LEGACY_CONTROL_ENVS if os.environ.get(name, "").strip()]
    if legacy:
        violations.append(
            "legacy single-admin control identity must not represent operators in "
            f"customer production: unset {', '.join(legacy)}"
        )
    auth_mode = os.environ.get("VIDEO_REPLICA_AUTH_MODE", "").strip().lower()
    if auth_mode in _DEV_AUTH_MODES:
        violations.append(
            f"development identity mode is forbidden in customer production: "
            f"VIDEO_REPLICA_AUTH_MODE={auth_mode}"
        )
    if os.environ.get("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "").strip() == "1":
        violations.append(
            "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER=1 is forbidden in customer production"
        )
    if os.environ.get("VIDEO_REPLICA_DESKTOP_USER_ID", "").strip():
        violations.append(
            "VIDEO_REPLICA_DESKTOP_USER_ID (dev identity) is forbidden in customer production"
        )
    if os.environ.get("VIDEO_REPLICA_STORAGE_ROOT", "").strip():
        violations.append(
            "persistent local assets (VIDEO_REPLICA_STORAGE_ROOT) are forbidden in "
            "customer production: configure the private COS storage provider instead"
        )
    public_origin: str | None = None
    try:
        public_origin = customer_public_origin()
    except ValueError as exc:
        violations.append(str(exc))
    public_base_url = os.environ.get(PUBLIC_BASE_URL_ENV, "").strip()
    if not public_base_url:
        violations.append(
            f"{PUBLIC_BASE_URL_ENV} is required in customer production and must equal "
            f"{CUSTOMER_PUBLIC_ORIGIN_ENV}"
        )
    elif public_origin is not None and public_base_url != public_origin:
        violations.append(
            f"{PUBLIC_BASE_URL_ENV} must exactly equal {CUSTOMER_PUBLIC_ORIGIN_ENV} "
            "so browser ingress, signed asset URLs and payment callbacks share one origin"
        )
    try:
        trusted_proxy_networks()
    except ValueError as exc:
        violations.append(str(exc))

    _append_raw_hmac_key_violations(violations, os.environ, _ACTIVATION_CODE_HMAC_KEY_ENV)
    _append_aead_key_violations(violations, os.environ, _ACTIVATION_EXPORT_AEAD_KEY_ENV)
    _append_raw_hmac_key_violations(violations, os.environ, _DEVICE_FINGERPRINT_HMAC_KEY_ENV)
    _append_aead_key_violations(violations, os.environ, _CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV)
    settings_key = os.environ.get(SETTINGS_KEY_ENV, "").strip()
    if not settings_key:
        violations.append(
            f"{SETTINGS_KEY_ENV} is required in customer production; an app-local OS "
            "keystore is not a shared multi-instance secret source"
        )
    else:
        try:
            Fernet(settings_key.encode("ascii"))
        except (UnicodeEncodeError, ValueError):
            violations.append(f"{SETTINGS_KEY_ENV} must be a valid Fernet key")

    configured_keys = _configured_admin_session_keys(os.environ)
    if not configured_keys:
        violations.append(
            "admin session HMAC key is missing: set "
            f"{_ADMIN_SESSION_HMAC_KEY_ENV}_V1, the un-suffixed "
            f"{_ADMIN_SESSION_HMAC_KEY_ENV}, or any later key version kept "
            "after a rotation"
        )
    else:
        # A configured-but-weak key must fail the boot itself instead of
        # surfacing later as a runtime error from admin_hmac_key().
        for name, value in configured_keys:
            if len(value.encode("utf-8")) < _MIN_ADMIN_KEY_BYTES:
                violations.append(
                    f"{name} must be at least {_MIN_ADMIN_KEY_BYTES} bytes "
                    "for the customer-production admin session HMAC key"
                )
    # M1 review M2: an explicitly configured but out-of-range admin-session
    # TTL must fail the boot itself instead of surfacing later as a 500 on
    # the first exchange — the same fail-later shape PR #40 P2-2 fixed for
    # keys. Delayed import: bootstrap must not pull the FastAPI layer.
    from app.admin_auth_routes import (
        ADMIN_SESSION_IDLE_TIMEOUT_ENV,
        ADMIN_SESSION_TTL_ENV,
        MAX_ADMIN_SESSION_TTL_SECONDS,
        MIN_ADMIN_SESSION_TTL_SECONDS,
        resolve_admin_session_idle_timeout_seconds,
        resolve_admin_session_ttl_seconds,
    )

    if os.environ.get(ADMIN_SESSION_TTL_ENV, "").strip():
        try:
            resolve_admin_session_ttl_seconds()
        except ValueError:
            violations.append(
                f"{ADMIN_SESSION_TTL_ENV} is invalid: must be an integer between "
                f"{MIN_ADMIN_SESSION_TTL_SECONDS} and {MAX_ADMIN_SESSION_TTL_SECONDS} "
                "seconds"
            )
    if os.environ.get(ADMIN_SESSION_IDLE_TIMEOUT_ENV, "").strip():
        try:
            resolve_admin_session_idle_timeout_seconds()
        except ValueError:
            violations.append(
                f"{ADMIN_SESSION_IDLE_TIMEOUT_ENV} is invalid: must be an integer between "
                f"{MIN_ADMIN_SESSION_TTL_SECONDS} and {MAX_ADMIN_SESSION_TTL_SECONDS} seconds"
            )
    return violations


def assert_customer_production_security() -> None:
    """Fail closed (RuntimeError) when a customer-production boot carries any
    forbidden legacy/dev/local configuration (T09 exit gate)."""
    violations = customer_production_security_violations()
    if violations:
        raise RuntimeError(
            "customer production security gate failed:\n- " + "\n- ".join(violations)
        )


def _latest_admin_key_version() -> int:
    versions: list[int] = []
    for name, _value in _configured_admin_session_keys(os.environ):
        if name == _ADMIN_SESSION_HMAC_KEY_ENV:
            versions.append(1)
        else:
            versions.append(int(name.removeprefix(_ADMIN_KEY_VERSION_PREFIX)))
    if not versions:
        raise RuntimeError("customer production admin session HMAC key is not configured")
    return max(versions)


def provision_empty_customer(
    *,
    admin_username: str,
    admin_display_name: str,
    cos_config: dict[str, object],
    confirm_empty_database: bool,
) -> EmptyCustomerProvisionResult:
    """Atomically seed the first operator and encrypted COS settings.

    This is the one-shot path for a freshly migrated customer database. It
    deliberately accepts only the pristine state left by a complete migration
    so it cannot become a general-purpose privilege escalation or settings
    override tool.
    The returned exchange credential is shown once and is never stored.
    """
    if not confirm_empty_database:
        raise RuntimeError("empty customer bootstrap requires explicit confirmation")
    if not is_customer_production():
        raise RuntimeError("empty customer bootstrap is only available in customer production")

    config = resolve_database_config()
    validate_customer_production(config)
    assert_customer_production_security()

    username = admin_username.strip()
    display_name = admin_display_name.strip()
    if not username or not display_name:
        raise ValueError("first-admin username and display name are required")
    normalized_cos = normalize_config(cos_config)
    validate_provider_config("cos", normalized_cos)

    admin_user_id = str(uuid4())

    with pg_transaction(isolation="SERIALIZABLE") as raw_conn:
        conn = BusinessConnection.postgres(raw_conn)
        # Serialize the pristine-state check across independent migration
        # hosts. The database—not operator timing—owns the one-shot guarantee.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_EMPTY_CUSTOMER_BOOTSTRAP_LOCK_ID,))
        bootstrap_state = conn.execute(
            """
            SELECT
                EXISTS (SELECT 1 FROM users) AS has_users,
                EXISTS (SELECT 1 FROM provider_settings) AS has_provider_settings,
                EXISTS (SELECT 1 FROM audit_logs) AS has_audit_logs,
                (
                    SELECT COUNT(*) = 1
                    FROM runtime_settings
                    WHERE id = 1
                      AND max_generation_count_per_batch = %s
                      AND max_concurrent_h3_tasks = %s
                      AND active_storage_provider = %s
                      AND internal_base_unit_price_fen = %s
                      AND min_recharge_fen = %s
                      AND recharge_step_fen = %s
                      AND fair_queue_enabled = FALSE
                      AND updated_by_user_id IS NULL
                ) AS runtime_is_pristine
            """,
            (
                DEFAULT_RUNTIME_SETTINGS["max_generation_count_per_batch"],
                DEFAULT_RUNTIME_SETTINGS["max_concurrent_h3_tasks"],
                DEFAULT_RUNTIME_SETTINGS["active_storage_provider"],
                DEFAULT_BILLING_SETTINGS["internal_base_unit_price_fen"],
                DEFAULT_BILLING_SETTINGS["min_recharge_fen"],
                DEFAULT_BILLING_SETTINGS["recharge_step_fen"],
            ),
        ).fetchone()
        state_values = tuple(bootstrap_state) if bootstrap_state is not None else ()
        if state_values != (False, False, False, True):
            raise RuntimeError(
                "first-admin provisioning requires a pristine, fully migrated PostgreSQL "
                "database; use the existing administrator or restore a clean target"
            )

        # Mint before writing so any key/configuration failure rolls back the
        # transaction, but only after the serialized emptiness check so a
        # rejected invocation never creates a stray operator credential.
        exchange_credential = issue_exchange_credential(
            admin_user_id,
            ttl_seconds=_FIRST_ADMIN_CREDENTIAL_TTL_SECONDS,
            key_version=_latest_admin_key_version(),
        )

        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES (%s, %s, %s, 'admin')
            """,
            (admin_user_id, username, display_name),
        )
        conn.execute("INSERT INTO wallets (user_id) VALUES (%s)", (admin_user_id,))
        SettingsRepository(conn).save_provider_config(
            "cos",
            normalized_cos,
            actor_user_id=admin_user_id,
        )
        updated = conn.execute(
            """
            UPDATE runtime_settings
            SET active_storage_provider = 'cos',
                updated_by_user_id = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (admin_user_id,),
        )
        if updated.rowcount != 1:
            raise RuntimeError(
                "runtime_settings is missing; run the complete Alembic migration before bootstrap"
            )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, %s, 'customer_bootstrap.provision', 'user', %s, %s)
            """,
            (
                str(uuid4()),
                admin_user_id,
                admin_user_id,
                json.dumps({"cos_configured": True, "source": "empty_customer_bootstrap"}),
            ),
        )

    return EmptyCustomerProvisionResult(
        admin_user_id=admin_user_id,
        exchange_credential=exchange_credential,
    )


def _load_cos_bootstrap_config(path: Path) -> dict[str, object]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "COS bootstrap config file is missing, unreadable, or invalid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("COS bootstrap config file must contain one JSON object")
    return {str(key): value for key, value in decoded.items()}


def _probe_formal_service_write_path(storage: StorageAdapter) -> None:
    """CW-031: prove the formal-service write path, not just bucket readability.

    ``check_readiness`` only proves the bucket can be HEADed; a write-path
    outage would otherwise surface on the first request, when the storage
    selector could be tempted into a local fallback.  The probe writes a
    one-byte object under a reserved ``.cw031-readiness/`` prefix (outside the
    ``projects/`` and ``generation-results/`` business namespaces) and deletes
    it immediately.  Audit events stay on this throwaway adapter instance and
    are never consumed.  Any failure propagates to the caller's existing
    ``except Exception`` branch, which fail-closes the bootstrap.
    """
    probe_key = f".cw031-readiness/{uuid4().hex}"
    storage.put_object(probe_key, b"0", content_type="application/octet-stream")
    content_store.delete_object_outside_content_namespace(storage, probe_key)


def check_customer_production_runtime_dependencies() -> PgReadyInfo | None:
    """Prove the shared database and private object store are usable.

    Customer API and Worker processes call this gate before advertising
    readiness or entering their work loops.  The internal SQLite lane stays
    unchanged and therefore returns ``None``.
    """
    # CW-031: the internal lane intentionally performs no storage readiness —
    # keeping the local adapter there is the documented desktop architecture.
    # The formal-service storage fence lives at request time in
    # media_routes.get_media_storage.
    if not is_customer_production():
        return None

    if shutil.which("ffprobe") is None:
        logger.error("Customer video precheck runtime is unavailable")
        raise RuntimeError("customer production requires ffprobe from the ffmpeg runtime")

    ready = check_pg_ready()
    try:
        with pg_transaction() as raw_conn:
            repo = SettingsRepository(BusinessConnection.postgres(raw_conn))
            runtime = repo.read_runtime_settings()
            if runtime.get("active_storage_provider") != "cos":
                raise ValueError("active storage provider must be cos")
            cos_config = repo.load_provider_config("cos")
            validate_provider_config("cos", cos_config)
        # Release the pooled PG connection before starting an external network
        # call. A slow COS probe must not consume the database pool and turn a
        # storage incident into a database outage.
        storage = create_storage_adapter(cloud_storage_config_from_settings("cos", cos_config))
        # The cloud adapter performs a bucket-level HEAD. Object-level 404s
        # are intentionally not used here because COS also returns 404 when
        # the configured bucket itself does not exist.
        storage.check_readiness()
        # CW-031: readiness must also prove the formal-service write path so a
        # COS outage fails here, at startup, instead of silently falling back
        # to local persistence on the first request.
        _probe_formal_service_write_path(storage)
    except Exception as exc:
        logger.error("Customer private COS readiness check failed: %s", type(exc).__name__)
        raise RuntimeError(
            "customer production requires a configured private COS storage provider"
        ) from exc
    return ready


def _run_runtime_bootstrap() -> None:
    """CW-025: 全环境 PG-only bootstrap。

    T05: resolve the database mode first so all environments fail closed
    before any SQLite file is touched. T09: the security gate then rejects
    legacy single-admin mappings, dev identities, local assets and missing
    admin-session keys before the ready check or any pool warm-up.

    CW-025 后 resolve_database_config() 只返回 POSTGRESQL 模式（否则抛 RuntimeError），
    所以 SQLite 分支已删除。历史 SQLite 工具（backup/sqlite_to_postgres/gate1_*）
    走 CW-060 独立白名单，不经过本函数。
    """
    config = resolve_database_config()
    validate_customer_production(config)
    assert_customer_production_security()

    # CW-025: config.mode 一定是 POSTGRESQL（resolve_database_config 保证）
    if config.mode is not DatabaseMode.POSTGRESQL:  # pragma: no cover - defensive
        raise RuntimeError(
            f"bootstrap requires PostgreSQL, got {config.mode}; "
            "this indicates a bug in resolve_database_config()"
        )

    # PG runtime: warm the pool and verify the server round-trip. Alembic
    # migrations against PG are executed by deploy/postgres/migrate.sh;
    # the ready check itself is the API bootstrap contract for the PG lane.
    ready = (
        check_customer_production_runtime_dependencies()
        if is_customer_production()
        else check_pg_ready()
    )
    if ready is None:  # pragma: no cover - guarded by the branch above
        raise RuntimeError("PostgreSQL readiness check returned no result")
    logging.getLogger(__name__).info(
        "PostgreSQL runtime ready (pool_max=%d, server_now=%s)",
        ready.pool_size,
        ready.server_now.isoformat(),
    )
    # bootstrap is a short-lived process: release the pooled connections
    # before exit (M0 review M2).
    close_pg_pool()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate or provision the Video Replica runtime")
    commands = parser.add_subparsers(dest="command")
    provision = commands.add_parser(
        "provision-empty-customer",
        help="one-shot first-admin and COS provisioning for a migrated empty customer database",
    )
    provision.add_argument("--admin-username", required=True)
    provision.add_argument("--admin-display-name", required=True)
    provision.add_argument("--cos-config-file", required=True, type=Path)
    provision.add_argument("--confirm-empty-database", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.command == "provision-empty-customer":
        try:
            result = provision_empty_customer(
                admin_username=args.admin_username,
                admin_display_name=args.admin_display_name,
                cos_config=_load_cos_bootstrap_config(args.cos_config_file),
                confirm_empty_database=bool(args.confirm_empty_database),
            )
            # The exchange credential is intentionally returned once and is
            # never logged or stored. The runbook requires a root-only output
            # file rather than terminal scrollback.
            print(
                json.dumps(
                    {
                        "admin_user_id": result.admin_user_id,
                        "exchange_credential": result.exchange_credential,
                    },
                    sort_keys=True,
                )
            )
        finally:
            close_pg_pool()
        return
    _run_runtime_bootstrap()


if __name__ == "__main__":
    main()
