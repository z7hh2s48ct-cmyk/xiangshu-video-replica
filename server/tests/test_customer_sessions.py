"""T19 / SES-01 — login, heartbeat, logout and the 30/90-second DB lease.

Fail-first tests for the frozen files ``server/app/customer_session_service.py``
and ``server/app/customer_session_routes.py`` (code checklist §3.2 / §3.3):
the single-online session half of the customer runtime, building on the T13
activation transaction (which grants the first epoch-1 lease) and the T16
device credential layer (which authenticates the login caller).

Contract under test (task list §4 T19; dev doc §3.3 / §6.1 / §12.3 / §13.2;
acceptance spec §2.3 / §3.4):

- ``POST /api/customer/sessions/login`` authenticates the *device* credential
  (Bearer) and drives the dev-doc §12.3 state machine under a row lock on
  ``customer_session_state``:

  * no session row / expired lease -> establish the caller's session
    (epoch + 1, fresh session token, lease = now + 90 s, ``LOGIN`` event; a
    lapsed prior lease additionally records the system ``TIMEOUT`` event);
  * same device + currently valid session token presented in the body ->
    renew only: same token, same epoch, lease pushed to now + 90 s;
  * same device without a usable session token -> epoch + 1 and a fresh
    token (the recovery path — the old token can never heartbeat again);
  * another device online with a live lease -> 409 ``OTHER_DEVICE_ONLINE``
    with the masked device name and the remaining lease (never a silent
    kick, dev doc §3.3);

- ``POST /api/customer/sessions/heartbeat`` authenticates the *session* token
  (Bearer): a matching token under a live lease renews the lease (epoch
  untouched — §12.3 same-device lease extension) and appends a ``HEARTBEAT``
  event; a token that no longer matches the row answers 401
  ``SESSION_REPLACED``; a matching token under a lapsed lease answers 401
  ``SESSION_EXPIRED`` and must never resurrect the session (acceptance §3.4);

- ``POST /api/customer/sessions/logout`` authenticates the session token,
  pulls the lease into the past (the T16 unbind precedent) and appends a
  ``LOGOUT`` event: the released slot is immediately re-loggable by the other
  device, the old token's heartbeat fails with 401 ``SESSION_EXPIRED``, and a
  *late* logout after another device took over answers 401
  ``SESSION_REPLACED`` without touching the new session;

- login and logout carry a mandatory ``Idempotency-Key`` (dev doc §6.3): a
  lost response replays through the sealed envelope (same token, same epoch,
  no second ``LOGIN`` event, ``X-Idempotent-Replay: true``), while the same
  key against a different request body answers 409 ``IDEMPOTENCY_CONFLICT``;

- login is rate limited per device credential through the T45 account bucket,
  with the shared IP bucket retained as an auxiliary signal, and answers 429
  ``RATE_LIMITED`` once the credential budget is spent — while a fully validated
  idempotent replay short-circuits *before* the limiter and spends no budget
  (the activation-route T15 review rule);

- A/B concurrent logins from an expired-lease state leave exactly one
  current device (acceptance §3.4) — the row lock serializes the writers.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T19_DB_NAME = "t19_customer_sessions_test"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY_V1 = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V2 = secrets.token_urlsafe(48)
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)

ACTIVATE_PATH = "/api/customer/activate"
LOGIN_PATH = "/api/customer/sessions/login"
SWITCH_PATH = "/api/customer/sessions/switch"
HEARTBEAT_PATH = "/api/customer/sessions/heartbeat"
LOGOUT_PATH = "/api/customer/sessions/logout"
SUSPEND_PATH = "/api/control/activation-codes/{code_id}/suspend"
RESUME_PATH = "/api/control/activation-codes/{code_id}/resume"
REVOKE_PATH = "/api/control/activation-codes/{code_id}/revoke"
ADMIN_EXCHANGE_PATH = "/api/control/admin/session/exchange"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"
RETRY_AFTER_HEADER = "Retry-After"
AUTHORIZATION_HEADER = "Authorization"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

# Canonical Crockford-shaped codes (prefix + 4 groups x 7 characters).
FIRST_CODE = "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD"
SECOND_CODE = "XS04-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF"

COUNTERS_TABLE = "security_rate_limit_counters"
FAILURES_TABLE = "security_auth_failures"

# T22 recharge tests: the write path decrypts runtime settings with a Fernet
# key, and conftest disables the local keystore — so a module-stable key must
# encrypt the ZPay provider row seeded in route_state, and the app fixture
# must expose the same key (the test_customer_recharge precedent).
TEST_SETTINGS_FERNET_KEY = Fernet.generate_key()
TEST_ZPAY_ENCRYPTED_CONFIG = (
    Fernet(TEST_SETTINGS_FERNET_KEY)
    .encrypt(b'{"pid":"merchant-123","key":"merchant-secret","enabled_channels":"alipay,wxpay"}')
    .decode("ascii")
)


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t19_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T19_DB_NAME}"


# ---------------------------------------------------------------------------
# Module-level units (no database) — always run
# ---------------------------------------------------------------------------


def test_lease_seconds_matches_the_frozen_30_90_contract() -> None:
    from app.customer_session_service import SESSION_LEASE_SECONDS

    assert SESSION_LEASE_SECONDS == 90


def test_session_routes_keep_their_response_models_in_the_openapi_contract() -> None:
    """T28 (FE-01): login / switch / heartbeat keep their response models in
    the OpenAPI contract. The client's generated types are cut from this
    contract; dropping a response_model would silently shrink it and let the
    hand-written shapes drift back in."""
    from app.customer_session_routes import router as customer_session_router

    contract_app = FastAPI()
    contract_app.include_router(customer_session_router)
    paths = contract_app.openapi()["paths"]

    login_responses = paths["/api/customer/sessions/login"]["post"]["responses"]
    switch_responses = paths["/api/customer/sessions/switch"]["post"]["responses"]
    heartbeat_responses = paths["/api/customer/sessions/heartbeat"]["post"]["responses"]
    assert (
        login_responses["201"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/LoginResponse"
    )
    assert (
        switch_responses["201"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/LoginResponse"
    )
    assert (
        heartbeat_responses["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/HeartbeatResponse"
    )
    # PR #55 review: the same device renewing its live session answers 200
    # with the identical LoginResponse body (the outcome-sealed envelope),
    # so both status codes must carry the model — a 201-only contract leaves
    # the generated client unable to type a valid production response.
    assert (
        login_responses["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/LoginResponse"
    )
    assert (
        switch_responses["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/LoginResponse"
    )


def test_mask_device_name_keeps_a_short_hint_only() -> None:
    from app.customer_session_service import mask_device_name

    masked = mask_device_name("Zhang-san MacBook Pro")
    assert "Zhang" not in masked
    assert "MacBook" not in masked
    assert masked and set(masked.replace("*", "")) <= set("Zhang-san MacBook Pro")


def test_mask_device_name_handles_short_names() -> None:
    from app.customer_session_service import mask_device_name

    assert mask_device_name("") == ""
    assert mask_device_name("A") == "*"
    assert mask_device_name("AB") == "*"


def test_login_ip_limit_env_falls_back_to_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodeReview P3-5: the T15 _positive_int_env semantics — an env typo or
    a non-positive value can never disable or strangle the login limiter."""
    from app.security_rate_limit import DEFAULT_LOGIN_IP_LIMIT, login_ip_limit

    monkeypatch.delenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", raising=False)
    assert login_ip_limit() == DEFAULT_LOGIN_IP_LIMIT == 10
    for bad in ("0", "-5", "abc", ""):
        monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", bad)
        assert login_ip_limit() == DEFAULT_LOGIN_IP_LIMIT, bad
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "7")
    assert login_ip_limit() == 7


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sessions_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T19_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T19_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t19_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t19_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T19_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(sessions_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, device_pairing_requests, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            f"{COUNTERS_TABLE}, {FAILURES_TABLE} CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
        conn.execute(
            "INSERT INTO provider_settings "
            "(provider, encrypted_config, updated_by_user_id, created_at, updated_at) "
            "VALUES ('zpay', %s, 'admin_u', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (TEST_ZPAY_ENCRYPTED_CONFIG,),
        )
    yield sessions_dsn
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    # T20: the revocation-propagation cases drive the admin suspend/revoke
    # routes, so the admin auth + activation routers ride the same app.
    from app.activation_code_routes import router as activation_code_router
    from app.admin_activation_routes import router as admin_activation_router
    from app.admin_auth_routes import router as admin_auth_router
    from app.customer_session_routes import router as customer_session_router
    from app.recharge_routes import router as recharge_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(admin_auth_router)
    app.include_router(admin_activation_router)
    app.include_router(customer_session_router)
    app.include_router(recharge_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY", TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    # conftest disables the local keystore; the recharge write path (T22)
    # decrypts runtime settings, so the module-stable Fernet key that also
    # encrypted the seeded ZPay provider row must ride this app too.
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", TEST_SETTINGS_FERNET_KEY.decode("ascii"))
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://callback.example.com")
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    # Roomy budgets: these tests exercise the session routes, not the limiter
    # (one dedicated test narrows the budget on purpose).
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_ACCOUNT", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")
    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Seed helpers (the T16 test precedents)
# ---------------------------------------------------------------------------


def _seed_issuable_code(code: str, *, code_id: str, batch_id: str) -> None:
    """Insert one OPEN batch + ISSUED code pair (T12 shapes)."""
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            f"VALUES ('{batch_id}', 'batch-{batch_id}', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
        )
        digest = compute_code_digest(code, key=TEST_KEY.encode("utf-8"))
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) "
            f"VALUES ('{code_id}', '{batch_id}', %s, 1, 'XS04-****', "
            "'ISSUED', '2026-01-01T00:00:00+00:00')",
            (digest,),
        )


def _activate(client: TestClient, code: str, fingerprint: str, key_suffix: str) -> object:
    return client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": fingerprint,
            "device_name": f"Device {key_suffix}",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: f"idem-{key_suffix}"},
    )


def _activated_customer(client: TestClient, *, code: str, fingerprint: str, suffix: str) -> dict:
    """Seed + activate one customer; assert success and return the payload."""
    _seed_issuable_code(code, code_id=f"code-{suffix}", batch_id=f"batch-{suffix}")
    response = _activate(client, code, fingerprint, suffix)
    assert response.status_code == 201, response.text
    payload = response.json()
    for field in ("user_id", "device_id", "device_token", "session_token"):
        assert isinstance(payload.get(field), str) and payload[field], payload
    return payload


def _second_device_row(
    *,
    user_id: str,
    activation_code_id: str,
    device_id: str,
    slot_no: int,
    display_name: str = "Second Device",
) -> str:
    """Insert a second BOUND device directly (the T17 enroll path is out of
    scope here). Returns the plaintext token the caller then presents."""
    from app.customer_device_service import highest_device_domain_key, keyed_digest

    token = secrets.token_urlsafe(32)
    version, key = highest_device_domain_key()
    digest = keyed_digest(key, token)
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES (%s, %s, %s, %s, %s, 'macos', %s, %s, %s, %s)",
            (
                device_id,
                activation_code_id,
                user_id,
                slot_no,
                display_name,
                keyed_digest(key, f"fp-second-{device_id}"),
                version,
                digest,
                version,
            ),
        )
    return token


def _bearer(token: str) -> dict[str, str]:
    return {AUTHORIZATION_HEADER: f"Bearer {token}"}


def _second_device_login(
    client: TestClient,
    user_id: str,
    device_id: str,
    *,
    suffix: str,
) -> dict:
    """Create a session on slot 2 (takeover scenario) for fencing tests.

    Mirrors the T20 switch pattern: a second BOUND device on the same
    (already activated) code, then the explicit switch endpoint displaces
    the first device's lease in one transaction (epoch bump). Returns the
    new session payload with epoch 2.
    """
    # Insert second device directly (bypass T17 enroll) on the already ACTIVE
    # code-a — a fresh code would stay ISSUED and fail the session gate.
    second_token = _second_device_row(
        user_id=user_id,
        activation_code_id="code-a",
        device_id=device_id,
        slot_no=2,
    )

    # The explicit switch (T20/SES-02): the user confirmed the takeover, so
    # the server displaces the other device's lease atomically.
    response = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: f"idem-second-switch-{suffix}",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    for field in ("user_id", "device_id", "session_token", "session_epoch"):
        assert isinstance(payload.get(field), str if field != "session_epoch" else int), payload
    return payload


# ---------------------------------------------------------------------------
# T22 / SES-06 — recharge fencing invariants (customer session top-up gates)
# ---------------------------------------------------------------------------


def test_recharge_requires_a_live_customer_session(client: TestClient) -> None:
    """T22 core fence: without a live customer session, top-up is refused.

    The recharge endpoint lives behind BusinessDbDep which takes a
    CustomerSessionSnapshot via customer_session_snapshot() — when the
    PostgreSQL runtime is configured, missing/invalid tokens answer 401
    SESSION_TOKEN_REQUIRED/SESSION_REPLACED rather than leaking internal
    identity paths.
    """
    # No Bearer header at all → SESSION_TOKEN_REQUIRED.
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers={IDEMPOTENCY_KEY_HEADER: "idem-no-session-1"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"

    # Create a valid customer but never establish a session.
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")

    # Wrong token type (device instead of session) → SESSION_REPLACED.
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-device-token-1",
        },
    )
    assert response.status_code == 401, response.text
    # Device tokens are not session tokens; the snapshot phase rejects them.
    assert response.json()["detail"]["code"] == "SESSION_REPLACED"


def test_recharge_fails_when_admin_revoked_the_session(client: TestClient) -> None:
    """T22 core fence: admin suspend/revoke logs out the session and the
    old session token can never revive for write operations including top-up.

    This is the fencing boundary where SES-05 propagates to BILL-01:
    the recharge order insert must fail with SESSION_EXPIRED once the
    activation code is suspended or revoked by an admin actor.
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    session_token = customer["session_token"]
    session_epoch_before = customer["session_epoch"]

    # Recharge succeeds while session is live.
    fresh = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers={
            **_bearer(session_token),
            IDEMPOTENCY_KEY_HEADER: "idem-fresh-recharge",
        },
    )
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["amount_fen"] == 10000

    # Admin suspends the code: this triggers LOGOUT with code_suspended reason.
    suspended = _admin_code_action(client, "code-a", "suspend", reason="风控暂停")
    assert suspended.status_code == 200, suspended.text

    # Verify session event chain: ACTIVATED + LOGIN + HEARTBEAT + LOGOUT(code_suspended).
    events = _session_events()
    logout_rows = [e for e in events if e[0] == "LOGOUT"]
    assert len(logout_rows) == 1
    assert logout_rows[0][2] == "code_suspended"

    # Recharge now fails with SESSION_EXPIRED: the lease was pulled into the past.
    stale = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers={
            **_bearer(session_token),
            IDEMPOTENCY_KEY_HEADER: "idem-stale-recharge",
        },
    )
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_EXPIRED"

    # The revocation propagation bumps the epoch (T20/SES-03): suspend pulls
    # the lease into the past and records LOGOUT at the bumped epoch.
    row_device, _, row_epoch, row_lease, _ = _session_row()
    assert row_epoch == session_epoch_before + 1
    assert datetime.fromisoformat(row_lease) <= datetime.now(UTC) + timedelta(seconds=1)


def _session_row() -> tuple[str, str, int, str, str]:
    """The live session row: (device_id, session_id, epoch, lease_until, token_digest)."""
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT device_id, session_id, session_epoch, lease_until, token_digest "
            "FROM customer_session_state"
        ).fetchone()
    assert row is not None, "expected exactly one session row"
    return str(row[0]), str(row[1]), int(row[2]), str(row[3]), str(row[4])


def _session_events() -> list[tuple[str, int, str | None]]:
    """All session events: (event, epoch, reason) ordered by creation."""
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT event, session_epoch, reason FROM customer_session_events "
            "ORDER BY created_at, id"
        ).fetchall()
    return [(str(r[0]), int(r[1]), r[2] if r[2] is None else str(r[2])) for r in rows]


def _expire_lease(device_id: str) -> None:
    """Pull the live lease into the past (the crash-recovery simulation).

    The GREATEST backstop mirrors the production logout pattern: the
    lease_after_created CHECK holds even for a lapsed lease (a just-created
    session's now()-10s lands before its created_at).
    """
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = GREATEST("
            "(now() - interval '10 seconds'), "
            "created_at::timestamptz + interval '1 microsecond')::text "
            "WHERE device_id = %s",
            (device_id,),
        )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/login — authentication and contract gates
# ---------------------------------------------------------------------------


def test_login_requires_bearer_device_credential(client: TestClient) -> None:
    response = client.post(LOGIN_PATH, json={}, headers={IDEMPOTENCY_KEY_HEADER: "idem-login-1"})
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"


def test_login_rejects_unknown_device_credential(client: TestClient) -> None:
    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(secrets.token_urlsafe(32)),
            IDEMPOTENCY_KEY_HEADER: "idem-login-2",
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_INVALID"


def test_login_rejects_released_device_credential(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    # Release the caller's own device (unbind revokes the credential; the
    # status shape requires the unbind timestamp).
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_devices SET status = 'UNBOUND', unbound_at = %s WHERE id = %s",
            ("2026-01-01T00:00:00+00:00", customer["device_id"]),
        )
    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-login-3",
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_REVOKED"


def test_login_requires_idempotency_key(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(LOGIN_PATH, json={}, headers=_bearer(customer["device_token"]))
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_login_is_rate_limited_per_device(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    from app.activation_code_routes import router as activation_code_router
    from app.customer_session_routes import router as customer_session_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_session_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_ACCOUNT", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")

    with TestClient(app) as tight_client:
        customer = _activated_customer(
            tight_client, code=FIRST_CODE, fingerprint="fp-a", suffix="a"
        )
        headers = {
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-budget",
        }
        # Two requests fit the budget of 2; the third is refused.
        first = tight_client.post(LOGIN_PATH, json={}, headers=headers)
        second = tight_client.post(
            LOGIN_PATH, json={}, headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-budget-2"}
        )
        third = tight_client.post(
            LOGIN_PATH, json={}, headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-budget-3"}
        )
        assert first.status_code in (200, 201), first.text
        assert second.status_code in (200, 201), second.text
        assert third.status_code == 429, third.text
        assert third.json()["detail"]["code"] == "RATE_LIMITED"
        assert third.headers.get(RETRY_AFTER_HEADER) is not None


def test_login_idempotent_replay_spends_no_rate_limit_budget(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """CodeReview P2-2: a fully validated replay short-circuits *before* the
    limiter — network retries may never lock a legal user out of their own
    cached response (the activation-route T15 review rule)."""
    from app.activation_code_routes import router as activation_code_router
    from app.customer_session_routes import router as customer_session_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_session_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_ACCOUNT", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")

    with TestClient(app) as tight_client:
        customer = _activated_customer(
            tight_client, code=FIRST_CODE, fingerprint="fp-a", suffix="a"
        )
        headers = {
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-replay-budget",
        }
        # The first fresh login spends one hit.  Its immediate replay is still
        # current and must not spend a second hit.
        first = tight_client.post(LOGIN_PATH, json={}, headers=headers)
        replay = tight_client.post(LOGIN_PATH, json={}, headers=headers)
        assert first.status_code in (200, 201), first.text
        assert replay.status_code == first.status_code, replay.text
        assert replay.json() == first.json()
        assert replay.headers.get(REPLAY_HEADER) == "true"

        # A second fresh key spends the remaining hit and replaces the first
        # sealed session.  The third fresh submission is then refused.
        second = tight_client.post(
            LOGIN_PATH,
            json={},
            headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-replay-budget-2"},
        )
        assert second.status_code in (200, 201), second.text

        # The budget is spent; a fresh key is now refused.
        blocked = tight_client.post(
            LOGIN_PATH,
            json={},
            headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-replay-budget-3"},
        )
        assert blocked.status_code == 429, blocked.text

        # T45 S-1: once another login replaces the sealed session, the old
        # envelope is no longer replayable even though it was valid above.
        stale = tight_client.post(LOGIN_PATH, json={}, headers=headers)
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "SESSION_REPLAY_STALE"


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/login — the §12.3 state machine
# ---------------------------------------------------------------------------


def test_login_same_device_with_valid_session_token_renews_only(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    device_id, old_session_id, old_epoch, old_lease, _ = _session_row()

    response = client.post(
        LOGIN_PATH,
        json={"session_token": customer["session_token"]},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-renew-1",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    # Renewal: same token, same epoch, same session id — lease pushed out.
    assert payload["session_token"] == customer["session_token"]
    assert payload["session_epoch"] == old_epoch == 1
    assert payload["session_id"] == old_session_id
    assert payload["device_id"] == device_id
    assert payload["session_lease_expires_at"] >= old_lease

    row_device, _, row_epoch, _, _ = _session_row()
    assert row_device == device_id and row_epoch == 1
    events = _session_events()
    assert [e[0] for e in events] == ["ACTIVATED", "LOGIN"]


def test_login_same_device_without_session_token_recovers_with_epoch_bump(
    client: TestClient,
) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")

    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-recover-1",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["session_epoch"] == 2
    assert payload["session_token"] != customer["session_token"]

    # The replaced token can never heartbeat again (§3.4: old token fails).
    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_REPLACED"


def test_login_missing_session_row_establishes_epoch_one(client: TestClient) -> None:
    """Defensive branch: the activation row was lost (never happens in the
    happy chain) — login still establishes a sound epoch-1 session."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute("DELETE FROM customer_session_state")
        conn.execute("SET session_replication_role = DEFAULT")

    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-orphan-1",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["session_epoch"] == 1


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/login — idempotency envelope
# ---------------------------------------------------------------------------


def test_login_lost_response_replays_same_token_and_epoch(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    headers = {
        **_bearer(customer["device_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-replay-1",
    }
    first = client.post(LOGIN_PATH, json={}, headers=headers)
    assert first.status_code == 201, first.text

    # The client lost the 201 and retries with the same key + same body.
    replay = client.post(LOGIN_PATH, json={}, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json() == first.json()

    # Exactly one LOGIN event — the replay added nothing (acceptance §3.4).
    assert [e[0] for e in _session_events()] == ["ACTIVATED", "LOGIN"]


def test_replay_recovery_window_uses_the_postgresql_clock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #51 review P2: the envelope recovery window is judged on the
    PostgreSQL clock, never on the application process clock — an API node
    whose clock runs ahead of PostgreSQL must not reject a still-valid
    lost-response replay (nor may a lagging node accept an expired one).
    The deadline was minted from ``SELECT now()`` and the verdict compares
    against the same server-side clock sampled in the envelope-read
    transaction (SES-01; the activation-route ``_server_now`` precedent)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    headers = {
        **_bearer(customer["device_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-clock-skew-1",
    }
    first = client.post(LOGIN_PATH, json={}, headers=headers)
    assert first.status_code == 201, first.text

    class _SkewedDatetime(datetime):
        """An API node whose process clock is a decade ahead of PostgreSQL."""

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return datetime.now(UTC) + timedelta(days=3650)

    monkeypatch.setattr("app.customer_session_routes.datetime", _SkewedDatetime)
    replay = client.post(LOGIN_PATH, json={}, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json() == first.json()


def test_login_same_key_different_body_answers_idempotency_conflict(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    headers = {
        **_bearer(customer["device_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-conflicting-1",
    }
    first = client.post(LOGIN_PATH, json={}, headers=headers)
    assert first.status_code == 201, first.text

    conflict = client.post(
        LOGIN_PATH,
        json={"session_token": "a-different-body-token"},
        headers=headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_requires_bearer_session_token(client: TestClient) -> None:
    response = client.post(HEARTBEAT_PATH)
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"


def test_heartbeat_renews_the_lease_without_touching_epoch(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    _, old_session_id, _, old_lease, _ = _session_row()

    response = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_epoch"] == 1
    assert payload["session_id"] == old_session_id
    assert payload["lease_expires_at"] >= old_lease

    _, _, row_epoch, _, _ = _session_row()
    assert row_epoch == 1
    assert [e[0] for e in _session_events()] == ["ACTIVATED", "HEARTBEAT"]


def test_heartbeat_rejects_forged_session_token(client: TestClient) -> None:
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(HEARTBEAT_PATH, headers=_bearer(secrets.token_urlsafe(32)))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_REPLACED"


def test_heartbeat_on_lapsed_lease_never_resurrects_the_session(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    _expire_lease(customer["device_id"])

    response = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_EXPIRED"

    # And the refused heartbeat wrote no HEARTBEAT event.
    assert [e[0] for e in _session_events()] == ["ACTIVATED"]


def test_heartbeat_after_logout_fails_with_session_expired(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    out = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-logout-hb",
        },
    )
    assert out.status_code == 204, out.text

    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_EXPIRED"


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/logout
# ---------------------------------------------------------------------------


def test_logout_requires_idempotency_key(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(LOGOUT_PATH, headers=_bearer(customer["session_token"]))
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_logout_releases_the_lease_and_records_the_event(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-logout-1",
        },
    )
    assert response.status_code == 204, response.text

    # The lease is in the past and the LOGOUT event is on the audit trail.
    _, _, row_epoch, row_lease, _ = _session_row()
    assert row_epoch == 1
    assert datetime.fromisoformat(row_lease) <= datetime.now(UTC) + timedelta(seconds=1)
    events = [e[0] for e in _session_events()]
    assert events == ["ACTIVATED", "LOGOUT"]


def test_logout_lets_the_other_device_log_in_immediately(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-a",
        device_id="device-b",
        slot_no=2,
    )
    out = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-logout-2",
        },
    )
    assert out.status_code == 204, out.text

    login = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-after-logout",
        },
    )
    assert login.status_code == 201, login.text
    assert login.json()["device_id"] == "device-b"


def test_logout_lost_response_replays_the_204(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    headers = {
        **_bearer(customer["session_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-logout-replay",
    }
    first = client.post(LOGOUT_PATH, headers=headers)
    assert first.status_code == 204, first.text

    # The client lost the 204; the same key replays it even though the
    # session token is now expired (the unbind-envelope precedent).
    replay = client.post(LOGOUT_PATH, headers=headers)
    assert replay.status_code == 204, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"

    # No second LOGOUT event.
    assert [e[0] for e in _session_events()] == ["ACTIVATED", "LOGOUT"]


def test_logout_on_lapsed_lease_answers_session_expired(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    _expire_lease(customer["device_id"])
    response = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-logout-late",
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_EXPIRED"


def test_heartbeat_without_device_keys_fails_closed_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CodeReview P2-1: a missing device-domain key is a server-side outage —
    503 fail-closed, never a 500 and never a client-credential 401 (which
    would make the client wipe perfectly valid credentials)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", raising=False)
    response = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "SESSION_SERVICE_UNAVAILABLE"


def test_user_driven_events_record_the_acting_user(client: TestClient) -> None:
    """CodeReview P3-1: LOGIN/HEARTBEAT/LOGOUT are user-driven audit events —
    their actor_user_id names the user, so the trail distinguishes them from
    system events (only TIMEOUT runs actor-less, 029's comment)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    client.post(
        LOGIN_PATH,
        json={"session_token": customer["session_token"]},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-actor-1",
        },
    )
    client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-actor-2",
        },
    )
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT event, actor_user_id FROM customer_session_events ORDER BY created_at, id"
        ).fetchall()
    user_driven = {str(event): actor for event, actor in rows if str(event) != "ACTIVATED"}
    assert set(user_driven) == {"LOGIN", "HEARTBEAT", "LOGOUT"}
    for event, actor in user_driven.items():
        assert actor == customer["user_id"], (event, actor)


# ---------------------------------------------------------------------------
# Concurrency — acceptance §3.4: one current device from a lapsed state
# ---------------------------------------------------------------------------


def test_concurrent_first_logins_on_a_missing_row_never_500(client: TestClient) -> None:
    """CodeReview P3-4: two first-writers racing the defensive insert (the
    session row is missing) settle on the winner's row — the loser re-drives
    the state machine instead of dying on UniqueViolation."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    # No production path deletes session rows; simulate the lost row directly
    # (customer_session_state has no append-only trigger — only events do).
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute("DELETE FROM customer_session_state")

    outcomes: dict[str, int | None] = {"first": None, "second": None}
    barrier = threading.Barrier(2)

    def _login(tag: str) -> None:
        with TestClient(client.app) as concurrent:
            barrier.wait()
            response = concurrent.post(
                LOGIN_PATH,
                json={},
                headers={
                    **_bearer(customer["device_token"]),
                    IDEMPOTENCY_KEY_HEADER: f"idem-lost-row-{tag}",
                },
            )
            outcomes[tag] = response.status_code

    threads = [
        threading.Thread(target=_login, args=("first",)),
        threading.Thread(target=_login, args=("second",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Both requests answered — no UniqueViolation 500 — and each established
    # (then recovered) against the single row: the epoch advanced exactly
    # twice with two LOGIN events.
    assert outcomes["first"] == 201, outcomes
    assert outcomes["second"] == 201, outcomes
    _, _, row_epoch, _, _ = _session_row()
    assert row_epoch == 2
    assert [e[0] for e in _session_events()].count("LOGIN") == 2


# ---------------------------------------------------------------------------
# M3 exit gate 2 — one hundred second-device logins all answer 409
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# No-Go red lines — no credential material in events or envelopes
# ---------------------------------------------------------------------------


def test_no_plaintext_credentials_in_session_events(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-redline-1",
        },
    )
    client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))

    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT event, reason, request_id FROM customer_session_events"
        ).fetchall()
    for row in rows:
        blob = " ".join(str(value) for value in row if value is not None)
        assert customer["session_token"] not in blob
        assert customer["device_token"] not in blob


# ---------------------------------------------------------------------------
# T20 / SES-02 — POST /api/customer/sessions/switch (the explicit atomic switch)
# ---------------------------------------------------------------------------


def _admin_csrf(client: TestClient) -> dict[str, str]:
    """Exchange one admin session for its CSRF header (the T12 precedent)."""
    from app.admin_auth_routes import ADMIN_CSRF_HEADER

    response = password_admin_session(client, "admin_u")
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _admin_code_action(
    client: TestClient,
    code_id: str,
    action: str,
    *,
    reason: str = "运营风控",
    key: str | None = None,
) -> object:
    return client.post(
        f"/api/control/activation-codes/{code_id}/{action}",
        json={"confirm": True, "reason": reason},
        headers={
            **_admin_csrf(client),
            IDEMPOTENCY_KEY_HEADER: key or f"admin-{action}-{uuid.uuid4()}",
        },
    )


def _session_event_rows() -> list[dict[str, object]]:
    """Full session events: event, epoch, device, actor, reason (creation order)."""
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT event, session_epoch, device_id, actor_user_id, reason "
            "FROM customer_session_events ORDER BY created_at, id"
        ).fetchall()
    return [
        {
            "event": str(row[0]),
            "epoch": int(row[1]),
            "device_id": str(row[2]),
            "actor": row[3],
            "reason": row[4],
        }
        for row in rows
    ]


def test_switch_same_device_with_valid_session_token_renews_only(client: TestClient) -> None:
    """A switch from the currently online device is a renewal, not a takeover
    (same token, same epoch — §12.3 same-device lease extension)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")

    response = client.post(
        SWITCH_PATH,
        json={"session_token": customer["session_token"]},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-switch-renew",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_token"] == customer["session_token"]
    assert payload["session_epoch"] == 1
    assert [e["event"] for e in _session_event_rows()] == ["ACTIVATED", "LOGIN"]


def test_switch_same_device_without_token_recovers_with_epoch_bump(
    client: TestClient,
) -> None:
    """Same-device recovery on the switch route: a lost session token on the
    online device recovers via epoch + 1 and a fresh token."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")

    response = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-switch-recover",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["session_epoch"] == 2
    assert payload["session_token"] != customer["session_token"]

    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_REPLACED"


def test_switch_on_missing_session_row_establishes_epoch_one(client: TestClient) -> None:
    """Defensive branch: a lost session row still yields a sound epoch-1
    session on the switch route (the login precedent)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute("DELETE FROM customer_session_state")
        conn.execute("SET session_replication_role = DEFAULT")

    response = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-switch-orphan",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["session_epoch"] == 1


def test_switch_requires_bearer_device_credential(client: TestClient) -> None:
    response = client.post(
        SWITCH_PATH, json={}, headers={IDEMPOTENCY_KEY_HEADER: "idem-switch-auth"}
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"


def test_switch_rejects_unknown_device_credential(client: TestClient) -> None:
    response = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(secrets.token_urlsafe(32)),
            IDEMPOTENCY_KEY_HEADER: "idem-switch-unknown",
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_INVALID"


def test_switch_requires_idempotency_key(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(SWITCH_PATH, json={}, headers=_bearer(customer["device_token"]))
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_login_replay_rechecks_suspended_code(client: TestClient) -> None:
    """T45 S-1: a sealed login cannot bypass a later code suspension."""
    customer = _activated_customer(
        client,
        code=FIRST_CODE,
        fingerprint="fp-login-replay-suspended",
        suffix="login-replay-suspended",
    )
    headers = {
        **_bearer(customer["device_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-login-replay-suspended",
    }
    first = client.post(LOGIN_PATH, json={}, headers=headers)
    assert first.status_code == 201, first.text
    suspended = _admin_code_action(client, "code-login-replay-suspended", "suspend")
    assert suspended.status_code == 200, suspended.text

    replay = client.post(LOGIN_PATH, json={}, headers=headers)

    assert replay.status_code == 403
    assert replay.json()["detail"]["code"] == "CODE_SUSPENDED"


def test_switch_is_rate_limited_through_the_login_device_budget(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """The switch route draws the same per-device login budget as login — an
    attacker must not bypass the login limiter by switching instead."""
    from app.activation_code_routes import router as activation_code_router
    from app.customer_session_routes import router as customer_session_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_session_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_ACCOUNT", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")

    with TestClient(app) as tight_client:
        customer = _activated_customer(
            tight_client, code=FIRST_CODE, fingerprint="fp-a", suffix="a"
        )
        headers = {
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-switch-budget",
        }
        # Budget of 2: one login, one switch — the third login-shaped request
        # is refused whichever route it takes.
        login = tight_client.post(LOGIN_PATH, json={}, headers=headers)
        switch = tight_client.post(
            SWITCH_PATH,
            json={},
            headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-switch-budget-2"},
        )
        third = tight_client.post(
            SWITCH_PATH,
            json={},
            headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-switch-budget-3"},
        )
        assert login.status_code in (200, 201), login.text
        assert switch.status_code in (200, 201), switch.text
        assert third.status_code == 429, third.text
        assert third.json()["detail"]["code"] == "RATE_LIMITED"
        assert third.headers.get(RETRY_AFTER_HEADER) is not None


def test_switch_writes_no_wallet_charge(client: TestClient) -> None:
    """Code checklist §9.1: no session path — switch included — may ever write
    a CHARGE transaction (the activation first charge is the only one)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-a",
        device_id="device-b",
        slot_no=2,
    )
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        before = conn.execute(
            "SELECT count(*) FROM wallet_transactions WHERE type = 'CHARGE'"
        ).fetchone()

    response = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-switch-no-charge",
        },
    )
    assert response.status_code == 201, response.text

    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        after = conn.execute(
            "SELECT count(*) FROM wallet_transactions WHERE type = 'CHARGE'"
        ).fetchone()
    assert int(after[0]) == int(before[0])


# ---------------------------------------------------------------------------
# T20 / SES-03 — code-status gates on session establishment
# ---------------------------------------------------------------------------


def test_login_rejects_suspended_code(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    suspended = _admin_code_action(client, "code-a", "suspend", reason="风控暂停")
    assert suspended.status_code == 200, suspended.text

    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-login-suspended",
        },
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "CODE_SUSPENDED"


def test_switch_rejects_suspended_code(client: TestClient) -> None:
    """A suspended code must not be resurrected through the switch route
    either — the establishment gate applies to every session path."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    suspended = _admin_code_action(client, "code-a", "suspend", reason="风控暂停")
    assert suspended.status_code == 200, suspended.text

    response = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-switch-suspended",
        },
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "CODE_SUSPENDED"


def test_login_rejects_revoked_code(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    revoked = _admin_code_action(client, "code-a", "revoke", reason="作废")
    assert revoked.status_code == 200, revoked.text

    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-login-revoked",
        },
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "CODE_REVOKED"


# ---------------------------------------------------------------------------
# T20 / SES-03 — revocation propagation: suspend/revoke kill the live session
# ---------------------------------------------------------------------------


def test_suspending_the_code_revokes_the_live_session(client: TestClient) -> None:
    """SES-03: suspending the code atomically terminates the customer's live
    session — epoch bump, lease in the past, a LOGOUT event naming the admin
    actor and the code_suspended reason; the heartbeat then answers
    SESSION_EXPIRED and never resurrects."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")

    suspended = _admin_code_action(client, "code-a", "suspend", reason="风控暂停")
    assert suspended.status_code == 200, suspended.text

    row_device, _, row_epoch, row_lease, _ = _session_row()
    assert row_device == customer["device_id"]
    assert row_epoch == 2
    assert datetime.fromisoformat(row_lease) <= datetime.now(UTC) + timedelta(seconds=1)

    logout_rows = [e for e in _session_event_rows() if e["event"] == "LOGOUT"]
    assert len(logout_rows) == 1
    assert logout_rows[0]["epoch"] == 2
    assert logout_rows[0]["actor"] == "admin_u"
    assert logout_rows[0]["reason"] == "code_suspended"

    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_EXPIRED"


def test_revoking_the_code_revokes_the_live_session(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")

    revoked = _admin_code_action(client, "code-a", "revoke", reason="违规作废")
    assert revoked.status_code == 200, revoked.text

    row_device, _, row_epoch, row_lease, _ = _session_row()
    assert row_device == customer["device_id"]
    assert row_epoch == 2
    assert datetime.fromisoformat(row_lease) <= datetime.now(UTC) + timedelta(seconds=1)

    logout_rows = [e for e in _session_event_rows() if e["event"] == "LOGOUT"]
    assert len(logout_rows) == 1
    assert logout_rows[0]["reason"] == "code_revoked"
    assert logout_rows[0]["actor"] == "admin_u"

    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_EXPIRED"


def test_resumed_code_requires_a_fresh_login(client: TestClient) -> None:
    """Resume un-suspends the code but never resurrects the old session: the
    customer must log in again (a fresh epoch follows)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    suspended = _admin_code_action(client, "code-a", "suspend", reason="风控暂停")
    assert suspended.status_code == 200, suspended.text

    resumed = _admin_code_action(client, "code-a", "resume", reason="恢复")
    assert resumed.status_code == 200, resumed.text

    # The pre-suspension token stays dead.
    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_EXPIRED"

    # A fresh login re-establishes the session (the code is ACTIVE again).
    fresh = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-login-after-resume",
        },
    )
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["session_epoch"] == 3


def test_suspending_an_unactivated_code_writes_no_session_logout(client: TestClient) -> None:
    """An ISSUED code has no user and no session: the suspension path must
    stay a no-op on the session side (never a 500)."""
    _seed_issuable_code(code=SECOND_CODE, code_id="code-b", batch_id="batch-b")

    suspended = _admin_code_action(client, "code-b", "suspend", reason="渠道退回")
    assert suspended.status_code == 200, suspended.text

    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        events = conn.execute("SELECT count(*) FROM customer_session_events").fetchone()
        state = conn.execute("SELECT count(*) FROM customer_session_state").fetchone()
    assert int(events[0]) == 0
    assert int(state[0]) == 0


def test_session_fixation_injection_is_never_adopted(client: TestClient) -> None:
    """CW-009/S6: 攻击者自造的 session token 无从"固定"为有效会话。

    Customer sessions are only ever server-issued (login/activate responses);
    an attacker-crafted Bearer value is rejected outright and leaves no
    session state or events behind, so the classic fixation precondition —
    a victim adopting an attacker-known token — cannot exist.
    """
    customer = _activated_customer(
        client,
        code="XS04-CW09AAA-BBBBBBB-CCCCCCC-DDDDDDD",
        fingerprint="fp-cw09-fix",
        suffix="cw09fix",
    )
    attacker_token = "attacker-crafted-session-token"

    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        state_before = conn.execute("SELECT COUNT(*) FROM customer_session_state").fetchone()[0]
        events_before = conn.execute("SELECT COUNT(*) FROM customer_session_events").fetchone()[0]

    for path in (HEARTBEAT_PATH, LOGOUT_PATH):
        rejected = client.post(
            path,
            json={},
            headers={
                **_bearer(attacker_token),
                IDEMPOTENCY_KEY_HEADER: f"cw09-fixation-{path.rsplit('/', 1)[-1]}",
            },
        )
        assert rejected.status_code == 401, (path, rejected.text)

    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        state_after = conn.execute("SELECT COUNT(*) FROM customer_session_state").fetchone()[0]
        events_after = conn.execute("SELECT COUNT(*) FROM customer_session_events").fetchone()[0]
    assert state_after == state_before
    assert events_after == events_before

    # Presenting the crafted token through the only client-facing intake
    # (LoginRequest.session_token) must never mint it into a live session:
    # either the server rejects it outright or it establishes a freshly
    # issued token that differs from the attacker-chosen value.
    presented = client.post(
        LOGIN_PATH,
        json={"session_token": attacker_token},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "cw09-fixation-presented",
        },
    )
    if presented.status_code in (200, 201):
        assert presented.json()["session_token"] != attacker_token
    else:
        assert presented.status_code == 401, presented.text
    # A real login mints a server-side token and never adopts the crafted one.
    login = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "cw09-fixation-real-login",
        },
    )
    assert login.status_code in (200, 201), login.text
    assert login.json().get("session_token") not in (None, attacker_token)


# UC batch 01 supersedes cross-device eviction with independent device sessions.
@pytest.mark.parametrize("path", [LOGIN_PATH, SWITCH_PATH])
def test_another_device_login_preserves_both_live_sessions(client: TestClient, path: str) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    token = _second_device_row(
        user_id=customer["user_id"], activation_code_id="code-a", device_id="device-b", slot_no=2
    )
    headers = {**_bearer(token), IDEMPOTENCY_KEY_HEADER: "independent-device-b"}
    created = client.post(path, json={}, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["session_epoch"] == 1
    assert created.json()["device_id"] == "device-b"
    replay = client.post(path, json={}, headers=headers)
    assert replay.json() == created.json()
    assert replay.headers["X-Idempotent-Replay"] == "true"
    for session_token in (customer["session_token"], created.json()["session_token"]):
        assert client.post(HEARTBEAT_PATH, headers=_bearer(session_token)).status_code == 200
    assert not any(event[0] == "SWITCH" for event in _session_events())
    with psycopg.connect(_t19_dsn()) as conn:
        rows = conn.execute(
            "SELECT device_id, session_epoch FROM customer_session_state"
        ).fetchall()
    assert dict(rows) == {customer["device_id"]: 1, "device-b": 1}


@pytest.mark.parametrize("path", [LOGIN_PATH, SWITCH_PATH])
def test_expired_session_recovery_is_limited_to_its_device(client: TestClient, path: str) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second = _second_device_login(client, customer["user_id"], "device-b", suffix="b")
    _expire_lease(customer["device_id"])
    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401
    assert stale.json()["detail"]["code"] == "SESSION_EXPIRED"
    recovered = client.post(
        path,
        json={},
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "recover-device-a"},
    )
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["session_epoch"] == 2
    assert client.post(HEARTBEAT_PATH, headers=_bearer(second["session_token"])).status_code == 200
    with psycopg.connect(_t19_dsn()) as conn:
        timeout = conn.execute(
            "SELECT device_id, session_epoch, actor_user_id "
            "FROM customer_session_events WHERE event = 'TIMEOUT'"
        ).fetchall()
    assert timeout == [(customer["device_id"], 1, None)]


@pytest.mark.parametrize("expired", [False, True])
def test_logout_never_changes_another_devices_session(client: TestClient, expired: bool) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second = _second_device_login(client, customer["user_id"], "device-b", suffix="b")
    if expired:
        _expire_lease(customer["device_id"])
    with psycopg.connect(_t19_dsn()) as conn:
        before = conn.execute(
            "SELECT session_id, session_epoch, lease_until "
            "FROM customer_session_state WHERE device_id = 'device-b'"
        ).fetchone()
    logout = client.post(
        LOGOUT_PATH,
        headers={**_bearer(customer["session_token"]), IDEMPOTENCY_KEY_HEADER: "logout-device-a"},
    )
    assert logout.status_code == (401 if expired else 204), logout.text
    if expired:
        assert logout.json()["detail"]["code"] == "SESSION_EXPIRED"
    with psycopg.connect(_t19_dsn()) as conn:
        after = conn.execute(
            "SELECT session_id, session_epoch, lease_until "
            "FROM customer_session_state WHERE device_id = 'device-b'"
        ).fetchone()
    assert after == before
    assert client.post(HEARTBEAT_PATH, headers=_bearer(second["session_token"])).status_code == 200


@pytest.mark.parametrize("path", [LOGIN_PATH, SWITCH_PATH])
def test_idempotent_response_survives_other_device_login_but_not_own_recovery(
    client: TestClient,
    path: str,
) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    token = _second_device_row(
        user_id=customer["user_id"], activation_code_id="code-a", device_id="device-b", slot_no=2
    )
    headers = {**_bearer(token), IDEMPOTENCY_KEY_HEADER: "replay-device-b"}
    first = client.post(path, json={}, headers=headers)
    assert first.status_code == 201
    recovered_a = client.post(
        LOGIN_PATH,
        json={},
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "recover-a"},
    )
    assert recovered_a.status_code == 201
    assert client.post(path, json={}, headers=headers).json() == first.json()
    recovered_b = client.post(
        path, json={}, headers={**_bearer(token), IDEMPOTENCY_KEY_HEADER: "recover-b"}
    )
    assert recovered_b.status_code == 201
    stale = client.post(path, json={}, headers=headers)
    assert stale.status_code == 409, stale.text
    assert (
        client.post(HEARTBEAT_PATH, headers=_bearer(first.json()["session_token"])).status_code
        == 401
    )
    assert (
        client.post(
            HEARTBEAT_PATH, headers=_bearer(recovered_a.json()["session_token"])
        ).status_code
        == 200
    )


def test_both_online_devices_can_create_account_recharge_orders(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second = _second_device_login(client, customer["user_id"], "device-b", suffix="b")
    for index, token in enumerate((customer["session_token"], second["session_token"])):
        response = client.post(
            "/api/customer/recharge-orders",
            json={"amount_fen": 10000},
            headers={**_bearer(token), IDEMPOTENCY_KEY_HEADER: f"recharge-device-{index}"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["amount_fen"] == 10000


@pytest.mark.parametrize("path", [LOGIN_PATH, SWITCH_PATH])
@pytest.mark.parametrize("both_devices", [False, True])
def test_hundred_concurrent_logins_keep_device_epochs_independent(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    both_devices: bool,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "10000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_ACCOUNT", "10000")
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"], activation_code_id="code-a", device_id="device-b", slot_no=2
    )
    if both_devices:
        _expire_lease(customer["device_id"])
    start = threading.Event()

    def login(index: int) -> tuple[int, str]:
        token = customer["device_token"] if both_devices and index % 2 == 0 else second_token
        start.wait(timeout=30)
        response = client.post(
            path, json={}, headers={**_bearer(token), IDEMPOTENCY_KEY_HEADER: f"parallel-{index}"}
        )
        return response.status_code, response.text

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(login, index) for index in range(100)]
        start.set()
        results = [future.result(timeout=120) for future in futures]
    assert all(status == 201 for status, _ in results), [r for r in results if r[0] != 201]
    with psycopg.connect(_t19_dsn()) as conn:
        epochs = dict(
            conn.execute("SELECT device_id, session_epoch FROM customer_session_state").fetchall()
        )
    assert epochs == {
        customer["device_id"]: 51 if both_devices else 1,
        "device-b": 50 if both_devices else 100,
    }
    assert not any(event[0] == "SWITCH" for event in _session_events())
    if not both_devices:
        assert (
            client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"])).status_code
            == 200
        )
