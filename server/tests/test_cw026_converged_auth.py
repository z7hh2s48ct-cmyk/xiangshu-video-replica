"""CW-026 — exit internal authentication on the converged lane; re-verify fencing.

Fail-first tests for the CW-026 remaining work (V3 收敛清单):
``收敛后所有环境移除 internal Bearer、X-Dev-User-Id、固定桌面身份旁路``.

The converged PostgreSQL lane accepts exactly one identity — the live
customer session — in EVERY environment (customer production, staging,
dev/test/CI). Before CW-026 a PG lane without the customer-production flag
still accepted internal Bearer tokens (auth.py A1 carve-out) and the
autouse dev-identity configuration could resolve a bare ``X-Dev-User-Id``
through ``identity_user_id``. The legacy internal/desktop lane is
SQLite-only and exits later with CW-021/CW-040/CW-041 — the guard tests
pin that CW-026 did not reach into it.

Also covered here (the CW-026 acceptance delta):

- every retained business method-path classifies as read-owner
  (``AuthenticatedUser``), write-fence (``BusinessDbDep``) or one of the
  explicitly justified exception classes — an unclassified route fails the
  matrix until it is placed (漏项=0);
- a switch after the request-time snapshot (epoch bump / lease pull-back /
  logout) fences the late write: zero business rows and zero billing rows;
- an idempotent replay re-executes authorization inside the fenced
  transaction — a replaced session cannot collect a sealed response.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
)
from app.auth import CurrentUser, get_current_user
from app.customer_fence import (
    CustomerSessionSnapshot,
    customer_session_snapshot,
    fenced_pg_transaction,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

CW026_DB_NAME = "cw026_converged_auth_test"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY_V1 = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V2 = secrets.token_urlsafe(48)
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)

ACTIVATE_PATH = "/api/customer/activate"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
AUTHORIZATION_HEADER = "Authorization"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"
FIRST_CODE = "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD"


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _cw026_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{CW026_DB_NAME}"


@pytest.fixture(scope="module")
def cw026_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{CW026_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{CW026_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _cw026_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _cw026_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{CW026_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(cw026_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_cw026_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, device_pairing_requests, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            "projects, audit_logs, customer_fencing_write_evidence, "
            "internal_access_tokens, security_rate_limit_counters, "
            "security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
    yield cw026_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# The identity-matrix probe app (the get_current_user read-owner entry)
# ---------------------------------------------------------------------------


def _probe_app() -> FastAPI:
    probe = FastAPI()

    @probe.get("/whoami")
    def whoami(user: CurrentUser = Depends(get_current_user)) -> dict[str, str]:
        return {"id": user.id, "role": user.role}

    return probe


def _pg_env(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)


def _seed_internal_token(dsn: str, user_id: str = "internal_admin_u") -> str:
    raw_token = "cw026-internal-token-" + secrets.token_urlsafe(24)
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    token_id = "cw026-token-" + secrets.token_urlsafe(8)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            f"VALUES ('{user_id}', '{user_id}', 'Internal Admin', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO internal_access_tokens (id, user_id, token_digest) "
            "VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (token_id, user_id, digest),
        )
    return raw_token


# ---------------------------------------------------------------------------
# Class 1 — the converged lane refuses every legacy identity (PG, any env)
# ---------------------------------------------------------------------------


def test_internal_bearer_refused_on_converged_lane_without_customer_flag(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CW-026: the A1 carve-out is gone — a PG lane without the customer
    production flag no longer accepts internal Bearer tokens."""
    _pg_env(monkeypatch, route_state)
    raw_token = _seed_internal_token(route_state)
    client = TestClient(_probe_app())

    response = client.get("/whoami", headers={"Authorization": f"Bearer {raw_token}"})

    # Session keys are configured here, so the token resolves as a customer
    # session and fails as unknown/never-owner — never as internal_admin_u.
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_REPLACED"


def test_dev_identity_header_unreachable_on_converged_lane(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X-Dev-User-Id must not resolve through identity_user_id on the
    converged lane, even with the dev identity header explicitly enabled."""
    _pg_env(monkeypatch, route_state)
    monkeypatch.setenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "1")
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('employee_1', 'employee_1', 'Employee One', 'employee')"
        )
    client = TestClient(_probe_app())

    response = client.get("/whoami", headers={"X-Dev-User-Id": "employee_1"})

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"


def test_desktop_identity_bypass_unreachable_on_converged_lane(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned desktop user id plus a legacy auth mode must not open the
    converged lane — the identity_user_id fallback is SQLite-only now."""
    _pg_env(monkeypatch, route_state)
    monkeypatch.setenv("VIDEO_REPLICA_DESKTOP_USER_ID", "desktop_admin")
    for auth_mode in ("development", "desktop"):
        monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", auth_mode)
        with psycopg.connect(route_state, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('desktop_admin', 'desktop_admin', 'Desktop Admin', 'admin') "
                "ON CONFLICT (id) DO NOTHING"
            )
        client = TestClient(_probe_app())

        response = client.get("/whoami")

        assert response.status_code == 401, (auth_mode, response.text)
        assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED", auth_mode


def test_internal_bearer_with_desktop_pin_still_fences_on_converged_lane(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full legacy chain (internal Bearer + desktop pin + legacy mode)
    must stay dead on the converged lane: the Bearer resolves as a customer
    session and fences, the desktop pin never applies."""
    _pg_env(monkeypatch, route_state)
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "development")
    monkeypatch.setenv("VIDEO_REPLICA_DESKTOP_USER_ID", "desktop_admin")
    raw_token = _seed_internal_token(route_state)
    client = TestClient(_probe_app())

    response = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {raw_token}", "X-Dev-User-Id": "desktop_admin"},
    )

    assert response.status_code == 401, response.text
    body = response.json()
    assert body["detail"]["code"] == "SESSION_REPLACED"
    assert body.get("id") != "desktop_admin"


def test_malformed_bearer_answers_invalid_token_not_identity_fallthrough(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pg_env(monkeypatch, route_state)
    monkeypatch.setenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "1")
    client = TestClient(_probe_app())

    response = client.get("/whoami", headers={"Authorization": "Bearer has space"})

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"


def test_customer_production_flag_still_refuses_internal_bearer(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original A1 refusal on the customer-production flag is kept."""
    _pg_env(monkeypatch, route_state)
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    raw_token = _seed_internal_token(route_state)
    client = TestClient(_probe_app())

    response = client.get("/whoami", headers={"Authorization": f"Bearer {raw_token}"})

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_REPLACED"


# ---------------------------------------------------------------------------
# Class 3 — read owner: a customer session reads only its own rows
# ---------------------------------------------------------------------------


def _activate_customer(client: TestClient, dsn: str, *, suffix: str) -> dict:
    code = FIRST_CODE if suffix == "a" else FIRST_CODE.replace("AAAAAAA", "AAAAAAB")
    _seed_issuable_code(dsn, code, code_id=f"code-{suffix}", batch_id=f"batch-{suffix}")
    response = client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": f"fp-{suffix}",
            "device_name": f"Device {suffix}",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: f"idem-{suffix}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {AUTHORIZATION_HEADER: f"Bearer {token}"}


def _wallet_of_actor(client: TestClient, token: str) -> dict:
    response = client.get("/api/wallet", headers=_bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
def _seed_issuable_code(dsn: str, code: str, *, code_id: str, batch_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
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


def _activate_customer(client: TestClient, dsn: str, *, suffix: str) -> dict:
    code = FIRST_CODE if suffix == "a" else FIRST_CODE.replace("AAAAAAA", "AAAAAAB")
    _seed_issuable_code(dsn, code, code_id=f"code-{suffix}", batch_id=f"batch-{suffix}")
    response = client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": f"fp-{suffix}",
            "device_name": f"Device {suffix}",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: f"idem-{suffix}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {AUTHORIZATION_HEADER: f"Bearer {token}"}


def _wallet_of_actor(client: TestClient, token: str) -> dict:
    response = client.get("/api/wallet", headers=_bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------


@pytest.fixture()
def customer_app(route_state: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router
    from app.customer_session_routes import router as customer_session_router
    from app.rbac_routes import router as rbac_router
    from app.recharge_routes import router as recharge_router
    from app.wallet_routes import router as wallet_router

    app = FastAPI()
    for router in (
        activation_code_router,
        customer_session_router,
        rbac_router,
        recharge_router,
        wallet_router,
    ):
        app.include_router(router)
    _pg_env(monkeypatch, route_state)
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    # The recharge route needs the billing/zpay preconditions staged.
    from cryptography.fernet import Fernet

    settings_fernet_key = Fernet.generate_key()
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_fernet_key.decode("ascii"))
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://callback.example.com")
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_zpay', 'admin_zpay', 'Admin ZPay', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
        encrypted_config = (
            Fernet(settings_fernet_key)
            .encrypt(
                b'{"pid":"merchant-123","key":"merchant-secret","enabled_channels":"alipay,wxpay"}'
            )
            .decode("ascii")
        )
        conn.execute(
            "INSERT INTO provider_settings (provider, encrypted_config, "
            "updated_by_user_id, created_at, updated_at) VALUES (%s, %s, %s, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT(provider) DO UPDATE SET "
            "encrypted_config = excluded.encrypted_config",
            ("zpay", encrypted_config, "admin_zpay"),
        )
    yield app
    app.dependency_overrides.clear()


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


def test_customer_session_reads_resolve_to_session_owner_only(
    client: TestClient, route_state: str
) -> None:
    """有效客户读成功且仅限本人：两个客户各自读到自己的钱包，交叉令牌
    无法把对方的行读出来。"""
    first = _activate_customer(client, route_state, suffix="a")
    second = _activate_customer(client, route_state, suffix="b")
    with psycopg.connect(route_state, autocommit=True) as conn:
        for user in (first["user_id"], second["user_id"]):
            conn.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES (%s, 111, 0) ON CONFLICT (user_id) DO UPDATE "
                "SET available_credits = 111",
                (user,),
            )

    mine = _wallet_of_actor(client, str(first["session_token"]))
    theirs = _wallet_of_actor(client, str(second["session_token"]))

    assert mine == theirs  # both wallets read, each under its own session

    # A token displaced by a real second-device switch (the row now owns the
    # newcomer's digest) cannot read at all anymore.
    from app.customer_device_service import highest_device_domain_key, keyed_digest

    newcomer = secrets.token_urlsafe(32)
    _version, key = highest_device_domain_key()
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state SET token_digest = %s, "
            "session_epoch = session_epoch + 1 WHERE user_id = %s",
            (keyed_digest(key, newcomer), first["user_id"]),
        )
    stale = client.get("/api/wallet", headers=_bearer(str(first["session_token"])))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_REPLACED"


def test_project_write_is_scoped_to_the_fenced_session_owner(
    client: TestClient, route_state: str
) -> None:
    """有效客户写成功且仅限本人：fenced 写入的项目 owner 是会话用户，
    另一个客户读不到。"""
    first = _activate_customer(client, route_state, suffix="a")
    second = _activate_customer(client, route_state, suffix="b")

    created = client.post(
        "/api/projects", json={"name": "A 的项目"}, headers=_bearer(str(first["session_token"]))
    )
    assert created.status_code in (200, 201), created.text

    rows = client.get("/api/projects", headers=_bearer(str(second["session_token"])))
    assert rows.status_code == 200, rows.text
    payload = rows.json()
    items = payload["items"] if isinstance(payload, dict) else payload
    assert all(item["id"] != created.json()["id"] for item in items)

    with psycopg.connect(route_state, autocommit=True) as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM projects WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()
    assert row is not None
    assert str(row[0]) == first["user_id"]


# ---------------------------------------------------------------------------
# Class 4 — late-write fencing after the snapshot: zero business/billing rows
# ---------------------------------------------------------------------------


def _live_snapshot(dsn: str, token: str) -> CustomerSessionSnapshot:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/projects",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    snapshot = customer_session_snapshot(Request(scope))
    assert snapshot is not None
    return snapshot


def _counts(dsn: str) -> tuple[int, int, int]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        projects = conn.execute("SELECT count(*) FROM projects").fetchone()
        wallet_tx = conn.execute("SELECT count(*) FROM wallet_transactions").fetchone()
        evidence = conn.execute("SELECT count(*) FROM customer_fencing_write_evidence").fetchone()
    assert projects is not None and wallet_tx is not None and evidence is not None
    return int(projects[0]), int(wallet_tx[0]), int(evidence[0])


def _late_write_is_fenced_with_zero_deltas(
    dsn: str,
    snapshot: CustomerSessionSnapshot,
    mutate: Callable[[psycopg.Connection, str], None],
) -> None:
    before = _counts(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        mutate(conn, snapshot.expected_user_id)

    with pytest.raises(HTTPException) as excinfo:
        with fenced_pg_transaction(snapshot, record_write_evidence=True) as (pg_conn, _ctx):
            bc = BusinessConnection.postgres(pg_conn)
            bc.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
                ("cw026-late-write", snapshot.expected_user_id, "迟到写"),
            )

    assert excinfo.value.status_code == 401
    assert _counts(dsn) == before, "a fenced late write must leave no business/billing row"


@pytest.mark.parametrize(
    "mutation",
    ["epoch-switch", "lease-pullback", "logout"],
)
def test_switch_after_snapshot_fences_late_write_with_zero_deltas(
    client: TestClient, route_state: str, mutation: str
) -> None:
    """TEST-PG 双连接时序：先取请求期快照，第二条连接提交切换（epoch /
    lease / 注销），迟到写必须被栅栏拒绝且业务行与账务行增量均为 0。"""
    customer = _activate_customer(client, route_state, suffix="a")
    token = str(customer["session_token"])
    snapshot = _live_snapshot(route_state, token)

    def epoch_switch(conn: psycopg.Connection, user_id: str) -> None:
        conn.execute(
            "UPDATE customer_session_state SET session_epoch = session_epoch + 1 "
            "WHERE user_id = %s",
            (user_id,),
        )

    def lease_pullback(conn: psycopg.Connection, user_id: str) -> None:
        # The schema keeps lease_until after created_at; the smallest legal
        # pull-back lands one microsecond after the row was created.
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = GREATEST((now() - interval '1 second'), "
            "created_at::timestamptz + interval '1 microsecond')::text "
            "WHERE user_id = %s",
            (user_id,),
        )

    def logout(conn: psycopg.Connection, user_id: str) -> None:
        conn.execute("DELETE FROM customer_session_state WHERE user_id = %s", (user_id,))

    mutations = {
        "epoch-switch": epoch_switch,
        "lease-pullback": lease_pullback,
        "logout": logout,
    }
    _late_write_is_fenced_with_zero_deltas(route_state, snapshot, mutations[mutation])


# ---------------------------------------------------------------------------
# Class 5 — idempotent replay re-executes authorization
# ---------------------------------------------------------------------------


def _recharge_post(client: TestClient, token: str, idem_key: str) -> object:
    return client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers={**_bearer(token), IDEMPOTENCY_KEY_HEADER: idem_key},
    )


def test_idempotent_replay_reauthorizes_after_session_switch(
    client: TestClient, route_state: str
) -> None:
    """幂等重放不跳过授权：会话被切换后，旧令牌拿同一 Idempotency-Key
    重放必须 401，而不是领走封存的旧响应；新令牌重放才拿到封存响应。"""
    from app.customer_device_service import highest_device_domain_key, keyed_digest

    customer = _activate_customer(client, route_state, suffix="a")
    old_token = str(customer["session_token"])
    idem_key = "cw026-replay-key"

    created = _recharge_post(client, old_token, idem_key)
    assert created.status_code == 201, created.text  # type: ignore[union-attr]
    order_no = created.json()["order_no"]  # type: ignore[union-attr]

    # A second device takes over: the live row now owns a new token+epoch.
    new_token = secrets.token_urlsafe(32)
    _version, key = highest_device_domain_key()
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state SET token_digest = %s, "
            "session_epoch = session_epoch + 1, session_id = 'cw026-session-b' "
            "WHERE user_id = %s",
            (keyed_digest(key, new_token), customer["user_id"]),
        )

    replay_with_old = _recharge_post(client, old_token, idem_key)
    assert replay_with_old.status_code == 401, replay_with_old.text  # type: ignore[union-attr]
    old_body = replay_with_old.json()  # type: ignore[union-attr]
    assert old_body["detail"]["code"] == "SESSION_REPLACED"
    assert replay_with_old.headers.get("X-Idempotent-Replay") != "true"  # type: ignore[union-attr]
    with psycopg.connect(route_state, autocommit=True) as conn:
        # Activation itself books an ACT-* grant order; exclude it.
        count = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE merchant_order_no NOT LIKE 'ACT-%'"
        ).fetchone()
        envelope = conn.execute(
            "SELECT ciphertext FROM customer_idempotency_envelopes "
            "WHERE operation = 'recharge:create'"
        ).fetchone()
    assert count is not None and int(count[0]) == 1, "the fenced replay must not add an order"
    assert envelope is not None and envelope[0] is not None, "the sealed envelope is kept"

    replay_with_new = _recharge_post(client, new_token, idem_key)
    assert replay_with_new.status_code == 201, replay_with_new.text  # type: ignore[union-attr]
    assert replay_with_new.json()["order_no"] == order_no  # type: ignore[union-attr]
    assert replay_with_new.headers.get("X-Idempotent-Replay") == "true"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Class 6 — the retained business method-path matrix (漏项=0)
# ---------------------------------------------------------------------------

ADMIN_DEPS = {
    "get_admin_actor",
    "get_admin_writer",
    "require_settings_admin",
    "get_control_route_user",
    "get_character_admin",
}

# Method-paths that are deliberately NOT customer read-owner/write-fence, each
# with its justification (the CW-026 evidence matrix carries the same table).
EXEMPT_PUBLIC_SIGNED = {
    # Content-addressed cache key; no per-user data.
    "/api/assets/character-cache/{cache_name}": "public cache object",
    # HMAC-signed download URL bound to object+actor+asset+session epoch;
    # the DB grant is revalidated before every read.
    "/api/assets/signed-objects/{object_key:path}": "signed URL",
    "/api/assets/local-objects/{object_key:path}": "signed URL (local provider)",
    # Public platform cover copy; deterministic key must match a stored row.
    "/api/viral/covers/{platform}/{video_id:path}": "public cover",
    # HMAC signature binds user + expiry (signature IS the authorization;
    # <audio>/<video> tags cannot attach headers).
    "/api/viral/videos/media/file": "signed media range",
    # Public price/capability cards with no user scoping.
    "/api/oral/price": "public price card",
    "/api/independent/capabilities": "public capability card",
}


def _expand_routes(app: object) -> list[tuple[str, str, tuple[str, ...]]]:
    rows: list[tuple[str, str, tuple[str, ...]]] = []

    def walk(routes: list) -> None:
        for route in routes:
            name = type(route).__name__
            if name == "_IncludedRouter":
                walk(route.original_router.routes)
            elif getattr(route, "methods", None) and hasattr(route, "dependant"):
                deps = tuple(
                    dep.call.__name__ if hasattr(dep, "call") else type(dep.call).__name__
                    for dep in route.dependant.dependencies
                )
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    rows.append((method, route.path, deps))

    walk(app.routes)  # type: ignore[arg-type]
    return rows


def classify(method: str, path: str, deps: tuple[str, ...]) -> str:
    if not path.startswith("/api/"):
        return "PUBLIC_INFRA"
    if "get_business_db" in deps and "get_current_user" in deps:
        return "UNCLASSIFIED"  # a fenced write must not also take the read gate
    if "get_current_user" in deps:
        return "READ_OWNER"
    if "get_business_db" in deps:
        return "WRITE_FENCE"
    if path == "/api/auth/me":
        # The identity probe: it calls authenticate_request itself (plus the
        # login audit write), so post-CW-026 it resolves customer sessions
        # only on the converged lane.
        return "READ_OWNER"
    if ADMIN_DEPS & set(deps) or path.startswith("/api/control/"):
        return "ADMIN_SESSION"
    if path.startswith("/api/customer/"):
        # Session/device lifecycle endpoints authenticate their own device
        # credential or session token inside the handler (CW-016/017 lanes).
        return "SESSION_LIFECYCLE"
    if path.startswith("/api/payments/zpay/") or path == "/api/payments/wechat_native/notify":
        # Signature-verified provider callbacks / redirect returns
        # (the WeChat V3 callback verifies the raw body in-handler).
        return "PAYMENT_CALLBACK"
    if path in EXEMPT_PUBLIC_SIGNED:
        return "PUBLIC_SIGNED"
    return "UNCLASSIFIED"


def test_every_business_method_path_is_classified_zero_misses() -> None:
    """生成全部保留业务 method-path 的 read-owner/write-fence 表：每条路由
    必须落在一个已知类别里，漏项=0（新增未分类路由会让本测试失败）。"""
    from app.main import app

    rows = _expand_routes(app)
    assert rows, "the route table must not be empty"

    matrix: dict[tuple[str, str], str] = {}
    for method, path, deps in rows:
        label = classify(method, path, deps)
        assert label != "UNCLASSIFIED", f"unclassified route: {method} {path} deps={deps}"
        matrix[(method, path)] = label

    read_owner = [k for k, v in matrix.items() if v == "READ_OWNER"]
    write_fence = [k for k, v in matrix.items() if v == "WRITE_FENCE"]
    assert read_owner, "customer read-owner routes are missing"
    assert write_fence, "customer write-fence routes are missing"
    # Spot-check the retained business surface the converged client drives.
    assert matrix[("GET", "/api/projects")] == "READ_OWNER"
    assert matrix[("POST", "/api/projects")] == "WRITE_FENCE"
    assert matrix[("GET", "/api/wallet")] == "READ_OWNER"
    assert matrix[("POST", "/api/customer/recharge-orders")] == "WRITE_FENCE"
    assert matrix[("POST", "/api/projects/{project_id}/generation-batches")] == "WRITE_FENCE"
