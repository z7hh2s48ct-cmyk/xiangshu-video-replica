"""T09 / DB-08 — per-operator admin sessions: exchange, CSRF, RBAC.

Implements the application layer on top of the ``admin_sessions`` table
published by revision 026 (dev doc §15): every operator exchanges a short
lived, single-use HMAC-signed credential for an HttpOnly admin cookie plus a
per-session CSRF token. Only digests ever reach the database; the nonce digest
becomes the session primary key, which makes replaying a consumed credential
fail on the unique constraint instead of issuing a second session.

The legacy proxy-token control identity (``control_auth.get_control_user``)
stays the internal P0 path; customer production rejects it at startup
(``bootstrap.assert_customer_production_security``) and again at runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import string
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api_errors import http_error as _http
from app.db_pg import pg_transaction
from app.ops_metrics import (
    get_or_create_request_id,
    set_current_result_code,
    set_current_trace_fields,
)
from app.security_rate_limit import (
    DIMENSION_ADMIN_EXCHANGE_IP,
    DIMENSION_LOGIN_ACCOUNT,
    DIMENSION_LOGIN_IP,
    RateLimitDecision,
    admin_exchange_ip_limit,
    client_ip_from_request,
    consume_rate_limit,
    login_account_limit,
    login_ip_limit,
    rate_limit_window_seconds,
    record_auth_failure,
)

logger = logging.getLogger(__name__)

ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_CSRF_HEADER = "X-Admin-CSRF"
ADMIN_SESSION_HMAC_KEY_ENV = "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY"
ADMIN_SESSION_TTL_ENV = "VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS"
ADMIN_SESSION_IDLE_TIMEOUT_ENV = "VIDEO_REPLICA_ADMIN_SESSION_IDLE_TIMEOUT_SECONDS"
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"

DEFAULT_ADMIN_SESSION_TTL_SECONDS = 8 * 3600
DEFAULT_ADMIN_SESSION_IDLE_TIMEOUT_SECONDS = 30 * 60
MAX_ADMIN_RECOVERY_TTL_SECONDS = 10 * 60
MIN_ADMIN_SESSION_TTL_SECONDS = 60
MAX_ADMIN_SESSION_TTL_SECONDS = 24 * 3600
MIN_HMAC_KEY_BYTES = 32
EXCHANGE_CREDENTIAL_PREFIX = "ASX1"
MAX_EXCHANGE_CREDENTIAL_LENGTH = 512
NONCE_HEX_LENGTH = 32
ADMIN_COOKIE_PATH = "/api/control"
MIN_ADMIN_PASSWORD_LENGTH = 12
MAX_ADMIN_PASSWORD_LENGTH = 128
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16
ADMIN_CSRF_CONTEXT = b"video-replica:admin-csrf:v1"

_TRUTHY = {"1", "true", "yes", "on"}
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ADMIN_ROLES = frozenset({"admin", "auditor"})
_RECOVERY_OPERATIONS = frozenset(
    {
        ("GET", "/api/control/admin/session"),
        ("DELETE", "/api/control/admin/session"),
        ("PUT", "/api/control/admin/password"),
    }
)


class ExchangeCredentialError(ValueError):
    """Raised when an admin exchange credential fails verification."""


def is_customer_production() -> bool:
    return os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Exchange credentials (HMAC-signed, single-use via the nonce digest PK)
# ---------------------------------------------------------------------------


def admin_hmac_key(key_version: int, *, environ: Mapping[str, str] | None = None) -> bytes:
    """Resolve the versioned admin-session HMAC key from the environment.

    Version N reads ``VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY_VN``; version 1 also
    accepts the un-suffixed variable so single-version deployments stay simple.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    candidates = [f"{ADMIN_SESSION_HMAC_KEY_ENV}_V{key_version}"]
    if key_version == 1:
        candidates.append(ADMIN_SESSION_HMAC_KEY_ENV)
    for name in candidates:
        value = source.get(name, "").strip()
        if value:
            raw = value.encode("utf-8")
            if len(raw) < MIN_HMAC_KEY_BYTES:
                # ExchangeCredentialError (a ValueError subclass) so the route's
                # exchange handler answers 401 instead of a raw 500 (M1 review LOW).
                raise ExchangeCredentialError(
                    f"{name} must be at least {MIN_HMAC_KEY_BYTES} bytes, got {len(raw)}"
                )
            return raw
    raise ExchangeCredentialError(
        f"admin session HMAC key for key version {key_version} is not configured "
        f"(expected {candidates[0]} or a later key version)"
    )


@dataclass(frozen=True)
class ExchangeCredentialPayload:
    actor_user_id: str
    expires_at: datetime
    nonce: str
    key_version: int


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _validate_admin_password(password: str) -> None:
    if not MIN_ADMIN_PASSWORD_LENGTH <= len(password) <= MAX_ADMIN_PASSWORD_LENGTH:
        raise ValueError(
            f"administrator password must contain {MIN_ADMIN_PASSWORD_LENGTH} to "
            f"{MAX_ADMIN_PASSWORD_LENGTH} characters"
        )
    if not password.strip():
        raise ValueError("administrator password must not contain only whitespace")


def _scrypt_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def hash_admin_password(password: str) -> str:
    """Return a salted memory-hard password hash; plaintext is never retained."""
    _validate_admin_password(password)
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = _scrypt_digest(password, salt)
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_admin_password(password: str, encoded: str) -> bool:
    """Verify a stored password hash and fail closed on malformed encodings."""
    try:
        algorithm, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
        if len(salt) != SCRYPT_SALT_BYTES or len(expected) != SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def _dummy_admin_password_hash() -> str:
    """Stable-cost fallback so unknown usernames share the password path."""
    salt = hashlib.sha256(b"video-replica:admin-login:dummy-salt").digest()[:SCRYPT_SALT_BYTES]
    digest = _scrypt_digest("not-a-real-administrator-password", salt)
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def _admin_csrf_token(session_token: str) -> str:
    return _b64encode(
        hmac.new(session_token.encode("utf-8"), ADMIN_CSRF_CONTEXT, hashlib.sha256).digest()
    )


def _credential_signature(key: bytes, prefix: str, body: str) -> bytes:
    message = f"{prefix}.{body}".encode("ascii")
    return hmac.new(key, message, hashlib.sha256).digest()


def issue_exchange_credential(
    actor_user_id: str,
    *,
    ttl_seconds: int,
    key_version: int = 1,
    key: bytes | None = None,
    now: datetime | None = None,
) -> str:
    """Mint a short-lived, single-use exchange credential for an operator.

    The credential is self-contained (actor, expiry, nonce, key version) and
    HMAC-signed; it is handed to the operator out of band and never stored.
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    key_bytes = key if key is not None else admin_hmac_key(key_version)
    issued_at = now if now is not None else datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    nonce = secrets.token_hex(NONCE_HEX_LENGTH // 2)
    payload = json.dumps(
        {
            "actor": actor_user_id,
            "exp": int(expires_at.timestamp()),
            "nonce": nonce,
            "v": key_version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    body = _b64encode(payload)
    signature = _b64encode(_credential_signature(key_bytes, EXCHANGE_CREDENTIAL_PREFIX, body))
    return f"{EXCHANGE_CREDENTIAL_PREFIX}.{body}.{signature}"


def parse_and_verify_exchange_credential(
    credential: str,
    *,
    now: datetime | None = None,
    key: bytes | None = None,
) -> ExchangeCredentialPayload:
    """Verify signature, expiry and shape of an exchange credential."""
    if not credential or len(credential) > MAX_EXCHANGE_CREDENTIAL_LENGTH:
        raise ExchangeCredentialError("exchange credential is malformed")
    parts = credential.split(".")
    if len(parts) != 3 or parts[0] != EXCHANGE_CREDENTIAL_PREFIX:
        raise ExchangeCredentialError("exchange credential is malformed")
    prefix, body, signature_text = parts
    try:
        loaded = json.loads(_b64decode(body))
        signature = _b64decode(signature_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExchangeCredentialError("exchange credential is malformed") from exc
    # Valid non-object JSON (null/number/string/array) must reject as malformed
    # instead of raising TypeError from a dict() conversion.
    if not isinstance(loaded, dict):
        raise ExchangeCredentialError("exchange credential is malformed")
    decoded: dict[str, object] = loaded

    actor = decoded.get("actor")
    exp = decoded.get("exp")
    nonce = decoded.get("nonce")
    key_version = decoded.get("v")
    if (
        not isinstance(actor, str)
        or not actor
        or not isinstance(nonce, str)
        or len(nonce) != NONCE_HEX_LENGTH
        or any(character not in string.hexdigits for character in nonce)
        or not isinstance(key_version, int)
        or key_version < 1
        or not isinstance(exp, int)
    ):
        raise ExchangeCredentialError("exchange credential is malformed")

    key_bytes = key if key is not None else admin_hmac_key(key_version)
    expected = _credential_signature(key_bytes, prefix, body)
    if not hmac.compare_digest(signature, expected):
        raise ExchangeCredentialError("exchange credential signature mismatch")

    expires_at = datetime.fromtimestamp(exp, tz=UTC)
    current = now if now is not None else datetime.now(UTC)
    if current >= expires_at:
        raise ExchangeCredentialError("exchange credential has expired")
    return ExchangeCredentialPayload(
        actor_user_id=actor,
        expires_at=expires_at,
        nonce=nonce,
        key_version=key_version,
    )


def resolve_admin_session_ttl_seconds() -> int:
    raw = os.environ.get(ADMIN_SESSION_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_ADMIN_SESSION_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{ADMIN_SESSION_TTL_ENV} must be an integer number of seconds") from exc
    if not MIN_ADMIN_SESSION_TTL_SECONDS <= value <= MAX_ADMIN_SESSION_TTL_SECONDS:
        raise ValueError(
            f"{ADMIN_SESSION_TTL_ENV} must be between {MIN_ADMIN_SESSION_TTL_SECONDS} "
            f"and {MAX_ADMIN_SESSION_TTL_SECONDS} seconds, got {value}"
        )
    return value


def resolve_admin_session_idle_timeout_seconds() -> int:
    raw = os.environ.get(ADMIN_SESSION_IDLE_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_ADMIN_SESSION_IDLE_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ADMIN_SESSION_IDLE_TIMEOUT_ENV} must be an integer number of seconds"
        ) from exc
    if not MIN_ADMIN_SESSION_TTL_SECONDS <= value <= MAX_ADMIN_SESSION_TTL_SECONDS:
        raise ValueError(
            f"{ADMIN_SESSION_IDLE_TIMEOUT_ENV} must be between "
            f"{MIN_ADMIN_SESSION_TTL_SECONDS} and {MAX_ADMIN_SESSION_TTL_SECONDS} seconds"
        )
    return value


# ---------------------------------------------------------------------------
# Session storage / verification (PostgreSQL only — customer data plane)
# ---------------------------------------------------------------------------


def _sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


@dataclass(frozen=True)
class AdminActor:
    user_id: str
    username: str
    display_name: str
    role: str
    auth_method: str
    session_id: str
    session_expires_at: str
    last_activity_at: str


def _admin_exchange_identifier(request: Request) -> str:
    """Stable per-client digest; the raw address never enters audit storage."""
    return _sha256_hex(client_ip_from_request(request))


def _spend_admin_exchange_budget(
    request: Request,
    *,
    request_id: str,
) -> RateLimitDecision:
    with pg_transaction() as conn:
        decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_ADMIN_EXCHANGE_IP,
            identifier=_admin_exchange_identifier(request),
            limit=admin_exchange_ip_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
        if not decision.allowed:
            record_auth_failure(
                conn,
                dimension=DIMENSION_ADMIN_EXCHANGE_IP,
                identifier=_admin_exchange_identifier(request),
                request_id=request_id,
            )
    return decision


def _record_admin_exchange_failure(request: Request, *, request_id: str) -> None:
    try:
        with pg_transaction() as conn:
            record_auth_failure(
                conn,
                dimension=DIMENSION_ADMIN_EXCHANGE_IP,
                identifier=_admin_exchange_identifier(request),
                request_id=request_id,
            )
    except Exception as audit_error:
        # Authentication semantics must not depend on the diagnostic sink and
        # driver messages may contain connection details.
        logger.warning(
            "admin exchange failure audit unavailable (%s)",
            type(audit_error).__name__,
        )


def _admin_login_identifiers(request: Request, username: str) -> tuple[str, str]:
    return (
        _sha256_hex(client_ip_from_request(request)),
        _sha256_hex(username.strip()),
    )


def _spend_admin_password_budget(
    request: Request,
    *,
    username: str,
) -> tuple[RateLimitDecision, RateLimitDecision]:
    ip_identifier, account_identifier = _admin_login_identifiers(request, username)
    with pg_transaction() as conn:
        ip_decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_LOGIN_IP,
            identifier=ip_identifier,
            limit=login_ip_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
        account_decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_LOGIN_ACCOUNT,
            identifier=account_identifier,
            limit=login_account_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
    return ip_decision, account_decision


def _record_admin_password_failure(
    request: Request,
    *,
    username: str,
    request_id: str,
) -> None:
    ip_identifier, account_identifier = _admin_login_identifiers(request, username)
    try:
        with pg_transaction() as conn:
            for dimension, identifier in (
                (DIMENSION_LOGIN_IP, ip_identifier),
                (DIMENSION_LOGIN_ACCOUNT, account_identifier),
            ):
                record_auth_failure(
                    conn,
                    dimension=dimension,
                    identifier=identifier,
                    request_id=request_id,
                )
    except Exception as audit_error:
        logger.warning(
            "admin password failure audit unavailable (%s)",
            type(audit_error).__name__,
        )


def _create_admin_session_for_actor(
    actor_user_id: str,
    request: Request,
    *,
    session_id: str,
    auth_method: str,
    audit_action: str,
    reject_duplicate_as_reused: bool = False,
) -> tuple[AdminActor, str, str, int]:
    ttl_seconds = resolve_admin_session_ttl_seconds()
    if auth_method == "exchange":
        ttl_seconds = min(ttl_seconds, MAX_ADMIN_RECOVERY_TTL_SECONDS)
    session_token = secrets.token_urlsafe(32)
    csrf_token = _admin_csrf_token(session_token)
    client_ip = client_ip_from_request(request)
    user_agent = request.headers.get("user-agent", "")

    with pg_transaction() as conn:
        user_row = conn.execute(
            "SELECT id, username, display_name, role FROM users WHERE id = %s AND is_active = 1",
            (actor_user_id,),
        ).fetchone()
        if user_row is None:
            raise _http(401, "ADMIN_ACTOR_INVALID", "Operator is missing or inactive.")
        role = str(user_row[3])
        if role not in _ADMIN_ROLES:
            raise _http(403, "ADMIN_ROLE_REQUIRED", "Only admin/auditor operators may sign in.")
        now_row = conn.execute("SELECT now()").fetchone()
        if now_row is None:  # pragma: no cover - SELECT now() always returns a row
            raise RuntimeError("database clock unavailable")
        db_now = _as_datetime(now_row[0])
        expires_at = db_now + timedelta(seconds=ttl_seconds)
        try:
            conn.execute(
                "INSERT INTO admin_sessions "
                "(id, actor_user_id, session_digest, csrf_digest, created_at, "
                " last_activity_at, expires_at, created_ip_digest, created_ua_digest, "
                " auth_method) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    session_id,
                    str(user_row[0]),
                    _sha256_hex(session_token),
                    _sha256_hex(csrf_token),
                    db_now.isoformat(),
                    db_now.isoformat(),
                    expires_at.isoformat(),
                    _sha256_hex(client_ip),
                    _sha256_hex(user_agent),
                    auth_method,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            if reject_duplicate_as_reused:
                raise _http(
                    401,
                    "EXCHANGE_CREDENTIAL_REUSED",
                    "This exchange credential has already been used.",
                ) from exc
            raise RuntimeError("admin session id collision") from exc
        actor = AdminActor(
            user_id=str(user_row[0]),
            username=str(user_row[1]),
            display_name=str(user_row[2]),
            role=role,
            auth_method=auth_method,
            session_id=session_id,
            session_expires_at=expires_at.isoformat(),
            last_activity_at=db_now.isoformat(),
        )
        # M1 review M1: credential consumption (login) is a security-critical
        # lifecycle event — same transaction as the session row so the audit
        # trail can never drift from what actually happened.
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, %s, 'admin_session', %s, %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                audit_action,
                actor.session_id,
                json.dumps(
                    {"ttl_seconds": ttl_seconds, "auth_method": auth_method},
                    separators=(",", ":"),
                ),
            ),
        )
    return actor, session_token, csrf_token, ttl_seconds


def create_admin_session(
    payload: ExchangeCredentialPayload, request: Request
) -> tuple[AdminActor, str, str, int]:
    """Exchange a verified one-time credential for a recovery session."""
    return _create_admin_session_for_actor(
        payload.actor_user_id,
        request,
        session_id=_sha256_hex(payload.nonce),
        auth_method="exchange",
        audit_action="admin_session.exchange",
        reject_duplicate_as_reused=True,
    )


def create_password_admin_session(
    actor_user_id: str, request: Request
) -> tuple[AdminActor, str, str, int]:
    """Create a routine administrator session after password verification."""
    return _create_admin_session_for_actor(
        actor_user_id,
        request,
        session_id=_sha256_hex(secrets.token_bytes(32)),
        auth_method="password",
        audit_action="admin_session.password_login",
    )


def load_admin_session(
    session_token: str,
    *,
    user_agent: str | None = None,
) -> tuple[AdminActor, str]:
    """Verify an admin session cookie and refresh its activity timestamp.

    PostgreSQL time is the only clock: the stored ISO expiry is compared against
    ``now()`` fetched in the same transaction. Actor, revocation, expiry and
    role are re-checked on every request, so disabling a user or revoking a
    session invalidates it immediately.

    ADMIN-SESSION-BINDING-20260912 (user-approved option ②): the session binds
    to the browser environment (User-Agent) only. A changed network egress IP
    must no longer revoke an operator mid-session — office NAT rotation used to
    surface as repeated forced logouts. ``created_ip_digest`` is still recorded
    for audit, and the IP dimension of the *login* rate limiter
    (``_spend_admin_password_budget``) is unchanged.
    """
    rejection: tuple[str, str] | None = None
    actor: AdminActor | None = None
    csrf_digest = ""
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT s.id, s.csrf_digest, "
            "CASE WHEN s.auth_method = 'exchange' THEN "
            "LEAST(s.expires_at::timestamptz, s.created_at::timestamptz + interval '10 minutes') "
            "ELSE s.expires_at::timestamptz END, s.last_activity_at, "
            "       s.auth_method, s.actor_user_id, u.username, u.display_name, u.role, "
            "       s.created_ip_digest, s.created_ua_digest, now() AS db_now "
            "FROM admin_sessions s JOIN users u ON u.id = s.actor_user_id "
            "WHERE s.session_digest = %s AND s.revoked_at IS NULL AND u.is_active = 1",
            (_sha256_hex(session_token),),
        ).fetchone()
        if row is None:
            raise _http(
                401, "ADMIN_SESSION_INVALID", "Admin session is missing, revoked or invalid."
            )
        db_now = _as_datetime(row[11])
        expires_at = _as_datetime(row[2])
        if db_now >= expires_at:
            raise _http(401, "ADMIN_SESSION_EXPIRED", "Admin session has expired.")
        role = str(row[8])
        if role not in _ADMIN_ROLES:
            raise _http(
                401, "ADMIN_SESSION_INVALID", "Operator role no longer permits admin access."
            )
        last_activity_at = _as_datetime(row[3])
        idle_timeout = timedelta(seconds=resolve_admin_session_idle_timeout_seconds())
        # Browser environment (User-Agent) only — see the docstring: a rotated
        # network egress IP (row[9]/created_ip_digest stays audit-only) must not
        # revoke the operator's session.
        context_changed = user_agent is not None and not hmac.compare_digest(
            str(row[10]), _sha256_hex(user_agent)
        )
        if db_now >= last_activity_at + idle_timeout:
            rejection = ("ADMIN_SESSION_IDLE_EXPIRED", "Admin session was idle for too long.")
        elif context_changed:
            rejection = (
                "ADMIN_SESSION_CONTEXT_CHANGED",
                "Admin session browser context changed; sign in again.",
            )
        if rejection is not None:
            conn.execute(
                "UPDATE admin_sessions SET revoked_at = %s WHERE id = %s",
                (db_now.isoformat(), str(row[0])),
            )
            conn.execute(
                "INSERT INTO audit_logs "
                "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
                "VALUES (%s, %s, 'admin_session.security_rejected', "
                "'admin_session', %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(row[5]),
                    str(row[0]),
                    json.dumps({"code": rejection[0]}, separators=(",", ":")),
                ),
            )
        else:
            conn.execute(
                "UPDATE admin_sessions SET last_activity_at = %s WHERE id = %s",
                (db_now.isoformat(), str(row[0])),
            )
            actor = AdminActor(
                user_id=str(row[5]),
                username=str(row[6]),
                display_name=str(row[7]),
                role=role,
                auth_method=str(row[4]),
                session_id=str(row[0]),
                session_expires_at=expires_at.isoformat(),
                last_activity_at=db_now.isoformat(),
            )
            csrf_digest = str(row[1])
    if rejection is not None:
        raise _http(401, rejection[0], rejection[1])
    assert actor is not None
    return actor, csrf_digest


def revoke_admin_session(session_id: str, actor_user_id: str = "") -> None:
    with pg_transaction() as conn:
        now_row = conn.execute("SELECT now()").fetchone()
        if now_row is None:  # pragma: no cover - SELECT now() always returns a row
            raise RuntimeError("database clock unavailable")
        db_now = _as_datetime(now_row[0])
        revoked = conn.execute(
            "UPDATE admin_sessions SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL",
            (db_now.isoformat(), session_id),
        )
        # M1 review M1: revocation is audited only when it actually changed
        # state — a repeated revoke stays the silent idempotent no-op it
        # already was, without a second audit row.
        if revoked.rowcount and actor_user_id:
            conn.execute(
                "INSERT INTO audit_logs "
                "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
                "VALUES (%s, %s, 'admin_session.revoke', 'admin_session', %s, '{}')",
                (str(uuid.uuid4()), actor_user_id, session_id),
            )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_admin_actor(request: Request) -> AdminActor:
    """Authenticate an operator via the admin cookie; enforce CSRF on writes."""
    token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    if not token:
        raise _http(401, "ADMIN_SESSION_INVALID", "An admin session cookie is required.")
    try:
        actor, csrf_digest = load_admin_session(
            token,
            user_agent=request.headers.get("user-agent", ""),
        )
    except RuntimeError as exc:
        # The PG runtime is unavailable (internal SQLite deployments): fail
        # closed with 503 instead of falling back to any legacy identity.
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    set_current_trace_fields(
        actor_id=actor.user_id,
        user_id=actor.user_id,
        session_id=actor.session_id,
    )
    refresh_csrf_token = _admin_csrf_token(token)
    request.state.admin_csrf_token = (
        refresh_csrf_token
        if hmac.compare_digest(_sha256_hex(refresh_csrf_token), csrf_digest)
        else None
    )
    if request.method.upper() in _WRITE_METHODS:
        supplied = request.headers.get(ADMIN_CSRF_HEADER, "")
        if not supplied:
            raise _http(403, "ADMIN_CSRF_REQUIRED", "The CSRF header is required.")
        if not hmac.compare_digest(_sha256_hex(supplied), csrf_digest):
            raise _http(403, "ADMIN_CSRF_INVALID", "The CSRF token does not match.")
    route = getattr(request.scope.get("route"), "path", request.url.path)
    if (
        actor.auth_method == "exchange"
        and (request.method.upper(), route) not in _RECOVERY_OPERATIONS
    ):
        raise _http(
            403,
            "ADMIN_PASSWORD_RECOVERY_ONLY",
            "This recovery session only permits password setup, session status and logout.",
        )
    return actor


def get_admin_writer(actor: Annotated[AdminActor, Depends(get_admin_actor)]) -> AdminActor:
    """RBAC: auditors are strictly read-only."""
    if actor.role != "admin":
        raise _http(403, "AUDITOR_READ_ONLY", "Auditors may read but not modify control data.")
    return actor


AdminReader = Annotated[AdminActor, Depends(get_admin_actor)]
AdminWriter = Annotated[AdminActor, Depends(get_admin_writer)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class ExchangeRequest(BaseModel):
    credential: str


class PasswordLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(max_length=MAX_ADMIN_PASSWORD_LENGTH)


class PasswordRecoveryRequest(BaseModel):
    password: str = Field(max_length=MAX_ADMIN_PASSWORD_LENGTH)


class AdminActorInfo(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: str


class ExchangeResponse(BaseModel):
    session_id: str
    expires_at: str
    csrf_token: str
    actor: AdminActorInfo
    auth_method: str


class AdminSessionInfo(BaseModel):
    session_id: str
    expires_at: str
    last_activity_at: str
    csrf_token: str | None = None
    actor: AdminActorInfo
    auth_method: str


router = APIRouter(prefix="/api/control/admin", tags=["admin-auth"])


def _set_admin_session_cookie(response: Response, session_token: str, ttl_seconds: int) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        session_token,
        max_age=ttl_seconds,
        httponly=True,
        samesite="strict",
        path=ADMIN_COOKIE_PATH,
        secure=is_customer_production(),
    )


def _exchange_response(actor: AdminActor, csrf_token: str) -> ExchangeResponse:
    return ExchangeResponse(
        session_id=actor.session_id,
        expires_at=actor.session_expires_at,
        csrf_token=csrf_token,
        auth_method=actor.auth_method,
        actor=AdminActorInfo(
            user_id=actor.user_id,
            username=actor.username,
            display_name=actor.display_name,
            role=actor.role,
        ),
    )


def _authenticate_admin_password(username: str, password: str) -> str | None:
    """Return an active admin actor id without exposing account existence."""
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT u.id, u.role, u.is_active, c.password_hash "
            "FROM users u LEFT JOIN admin_password_credentials c ON c.user_id = u.id "
            "WHERE u.username = %s",
            (username,),
        ).fetchone()
    if row is not None and row[3] is not None:
        encoded = str(row[3])
    else:
        encoded = _dummy_admin_password_hash()
    password_matches = verify_admin_password(password, encoded)
    if (
        row is None
        or row[3] is None
        or int(row[2]) != 1
        or str(row[1]) not in _ADMIN_ROLES
        or not password_matches
    ):
        return None
    return str(row[0])


@router.post("/session/exchange", response_model=ExchangeResponse, status_code=201)
def exchange_admin_session(
    body: ExchangeRequest, request: Request, response: Response
) -> ExchangeResponse:
    request_id = get_or_create_request_id(request)
    try:
        rate_decision = _spend_admin_exchange_budget(request, request_id=request_id)
    except (RuntimeError, ValueError) as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    if not rate_decision.allowed:
        set_current_result_code("RATE_LIMITED")
        raise HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMITED",
                "message": "Too many administrator sign-in attempts. Try again later.",
            },
            headers={"Retry-After": str(rate_decision.retry_after_seconds)},
        )
    try:
        payload = parse_and_verify_exchange_credential(body.credential)
    except ExchangeCredentialError as exc:
        _record_admin_exchange_failure(request, request_id=request_id)
        logger.info("admin exchange credential rejected: %s", type(exc).__name__)
        raise _http(
            401, "EXCHANGE_CREDENTIAL_INVALID", "Exchange credential is invalid or expired."
        ) from exc
    try:
        actor, session_token, csrf_token, ttl_seconds = create_admin_session(payload, request)
    except HTTPException:
        # Parsing succeeded, but the authoritative actor/session checks can
        # still reject inactive/unknown actors, non-admin roles, or a reused
        # one-shot nonce. Every rejected exchange belongs in the same durable
        # security failure stream as malformed and rate-limited attempts.
        _record_admin_exchange_failure(request, request_id=request_id)
        raise
    except RuntimeError as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    set_current_trace_fields(
        actor_id=actor.user_id,
        user_id=actor.user_id,
        session_id=actor.session_id,
    )
    _set_admin_session_cookie(response, session_token, ttl_seconds)
    logger.info("admin session exchanged: actor=%s session=%s", actor.user_id, actor.session_id)
    return _exchange_response(actor, csrf_token)


@router.post("/session/password", response_model=ExchangeResponse, status_code=201)
def login_admin_with_password(
    body: PasswordLoginRequest, request: Request, response: Response
) -> ExchangeResponse:
    request_id = get_or_create_request_id(request)
    username = body.username.strip()
    try:
        ip_decision, account_decision = _spend_admin_password_budget(
            request,
            username=username,
        )
    except (RuntimeError, ValueError) as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    if not ip_decision.allowed or not account_decision.allowed:
        retry_after = max(
            ip_decision.retry_after_seconds,
            account_decision.retry_after_seconds,
        )
        set_current_result_code("RATE_LIMITED")
        raise HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMITED",
                "message": "Too many administrator sign-in attempts. Try again later.",
            },
            headers={"Retry-After": str(retry_after)},
        )
    try:
        actor_user_id = _authenticate_admin_password(username, body.password)
    except RuntimeError as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    if actor_user_id is None:
        _record_admin_password_failure(
            request,
            username=username,
            request_id=request_id,
        )
        raise _http(
            401,
            "ADMIN_LOGIN_INVALID",
            "Administrator account or password is incorrect.",
        )
    try:
        actor, session_token, csrf_token, ttl_seconds = create_password_admin_session(
            actor_user_id,
            request,
        )
    except RuntimeError as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    set_current_trace_fields(
        actor_id=actor.user_id,
        user_id=actor.user_id,
        session_id=actor.session_id,
    )
    _set_admin_session_cookie(response, session_token, ttl_seconds)
    logger.info(
        "admin password session created: actor=%s session=%s",
        actor.user_id,
        actor.session_id,
    )
    return _exchange_response(actor, csrf_token)


@router.put("/password", status_code=204)
def recover_admin_password(
    body: PasswordRecoveryRequest,
    actor: AdminReader,
    response: Response,
) -> None:
    if actor.auth_method != "exchange":
        raise _http(
            403,
            "ADMIN_PASSWORD_RECOVERY_REQUIRED",
            "A one-time recovery session is required to change the administrator password.",
        )
    try:
        password_hash = hash_admin_password(body.password)
    except ValueError as exc:
        raise _http(
            422,
            "ADMIN_PASSWORD_INVALID",
            f"Password must contain {MIN_ADMIN_PASSWORD_LENGTH} to "
            f"{MAX_ADMIN_PASSWORD_LENGTH} characters.",
        ) from exc
    try:
        with pg_transaction() as conn:
            # Serialize recovery for an account, then re-check the one-time
            # session inside the password-write transaction (hashing can take
            # long enough for another request to revoke it in the meantime).
            user_row = conn.execute(
                "SELECT role FROM users WHERE id = %s AND is_active = 1 FOR UPDATE",
                (actor.user_id,),
            ).fetchone()
            session_row = conn.execute(
                "SELECT expires_at, created_at FROM admin_sessions "
                "WHERE id = %s AND actor_user_id = %s "
                "AND auth_method = 'exchange' AND revoked_at IS NULL "
                "FOR UPDATE",
                (actor.session_id, actor.user_id),
            ).fetchone()
            if user_row is None or str(user_row[0]) not in _ADMIN_ROLES or session_row is None:
                raise _http(401, "ADMIN_SESSION_INVALID", "Recovery session is no longer valid.")
            # now() is frozen at transaction start; a lock wait can cross expiry.
            now_row = conn.execute("SELECT clock_timestamp()").fetchone()
            if now_row is None:  # pragma: no cover - database clock always returns a row
                raise RuntimeError("database clock unavailable")
            db_now = _as_datetime(now_row[0])
            recovery_expires_at = min(
                _as_datetime(session_row[0]),
                _as_datetime(session_row[1]) + timedelta(seconds=MAX_ADMIN_RECOVERY_TTL_SECONDS),
            )
            if recovery_expires_at <= db_now:
                raise _http(401, "ADMIN_SESSION_INVALID", "Recovery session is no longer valid.")
            conn.execute(
                "INSERT INTO admin_password_credentials "
                "(user_id, password_hash, credential_version, password_changed_at) "
                "VALUES (%s, %s, 1, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "password_hash = EXCLUDED.password_hash, "
                "credential_version = admin_password_credentials.credential_version + 1, "
                "password_changed_at = EXCLUDED.password_changed_at",
                (actor.user_id, password_hash, db_now.isoformat()),
            )
            conn.execute(
                "UPDATE admin_sessions SET revoked_at = %s "
                "WHERE actor_user_id = %s AND revoked_at IS NULL",
                (db_now.isoformat(), actor.user_id),
            )
            conn.execute(
                "INSERT INTO audit_logs "
                "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
                "VALUES (%s, %s, 'admin_password.recover', 'user', %s, '{}')",
                (str(uuid.uuid4()), actor.user_id, actor.user_id),
            )
    except RuntimeError as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    response.delete_cookie(ADMIN_SESSION_COOKIE, path=ADMIN_COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    logger.info("admin password recovered: actor=%s", actor.user_id)


@router.get("/session", response_model=AdminSessionInfo)
def get_current_admin_session(
    request: Request,
    response: Response,
    actor: AdminReader,
) -> AdminSessionInfo:
    response.headers["Cache-Control"] = "no-store"
    return AdminSessionInfo(
        session_id=actor.session_id,
        expires_at=actor.session_expires_at,
        last_activity_at=actor.last_activity_at,
        auth_method=actor.auth_method,
        csrf_token=getattr(request.state, "admin_csrf_token", None),
        actor=AdminActorInfo(
            user_id=actor.user_id,
            username=actor.username,
            display_name=actor.display_name,
            role=actor.role,
        ),
    )


@router.delete("/session", status_code=204)
def logout_admin_session(actor: AdminReader, response: Response) -> None:
    try:
        revoke_admin_session(actor.session_id, actor_user_id=actor.user_id)
    except RuntimeError as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    response.delete_cookie(ADMIN_SESSION_COOKIE, path=ADMIN_COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    logger.info("admin session revoked: actor=%s session=%s", actor.user_id, actor.session_id)
