"""CW-027 — reuse the admin console and close permission gaps (复验).

V3 收敛清单 CW-027 is a *verification* task: the admin console already rides
per-operator admin sessions (cookie + CSRF) with the shared admin write
contract (``Idempotency-Key`` + ``confirm`` + ``reason``), auditors are
strictly read-only, and the legacy single-admin ``X-Control-Proxy-Token``
identity is refused on the customer-production lane. CW-027 must NOT rewrite
that — it must prove it, in one place, with zero misses:

- **Static matrix (漏项=0)**: every admin/control/character-admin route
  carries an admin authority on both its read and write methods. The only
  exemptions are the self-scoped logout and the two session-bootstrap
  routes; a new unclassified admin route fails the matrix.
- **Dynamic bottom-line grid (TEST-PG)**: admin write OK with contract +
  audit + idempotent replay; missing/wrong CSRF refused; auditor read-only
  (but auditor self-logout stays allowed); customer credentials can neither
  reach session-gated admin routes nor pass the role gates; expired and
  revoked admin sessions refused; the legacy proxy token keeps working only
  outside customer production and never regains shared admin rights on the
  customer-production lane.

No production code changes are expected here — the spec says "仅在发现
漏接路由时修复".
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Set the HMAC key before importing app modules (the T34 fixture precedent).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-cw027-admin-matrix-tests-minimum-48-bytes-1234",
)

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
    issue_exchange_credential,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

CW027_DB_NAME = "cw027_admin_matrix_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)
TEST_KEY = secrets.token_urlsafe(48)  # activation-code HMAC key (customer chain)
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

ADJUST_PATH = "/api/control/customers/customer_u/adjustments"
ADJUST_BODY = {
    "confirm": True,
    "reason": "CW-027 复验：客服补偿",
    "credits": 5,
    "source_document_type": "CS_TICKET",
    "source_document_ref": "TICKET-CW027",
}


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _cw027_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{CW027_DB_NAME}"


@pytest.fixture(scope="module")
def cw027_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{CW027_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{CW027_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _cw027_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _cw027_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{CW027_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(cw027_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_cw027_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, device_pairing_requests, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "admin_adjustments, wallet_transactions, recharge_orders, wallets, "
            "audit_logs, users, projects, customer_fencing_write_evidence, "
            "security_rate_limit_counters, security_auth_failures, "
            "runtime_settings, provider_settings CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor User', 'auditor'), "
            "('customer_u', 'customer_u', 'Customer User', 'customer')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('customer_u', 100, 0)"
        )
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen) "
            "VALUES (1, 4, 2, 1000, 10000, 1000)"
        )
    yield cw027_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(route_state: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    import base64

    from app.activation_code_routes import router as activation_code_router
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_routes import router as admin_customer_router
    from app.admin_session_routes import router as admin_session_router
    from app.control_routes import router as control_router
    from app.customer_session_routes import router as customer_session_router
    from app.settings_routes import router as settings_router

    app = FastAPI()
    for router in (
        admin_auth_router,
        admin_customer_router,
        admin_session_router,
        control_router,
        settings_router,
        customer_session_router,
        activation_code_router,
    ):
        app.include_router(router)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.setenv("CONTROL_PROXY_TOKEN_DIGEST", "0" * 64)
    monkeypatch.setenv("CONTROL_ADMIN_USER_ID", "admin_u")
    # Admin lanes run on real admin sessions — never a dev identity shortcut.
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    # Customer session chain keys (the CW-026 module precedent).
    from app.activation_code_service import ACTIVATION_CODE_HMAC_KEY_ENV

    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv(
        "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY",
        "cw027-admin-matrix-device-key-0123456789abcdef",
    )
    yield app
    app.dependency_overrides.clear()


@pytest.fixture()
def client(admin_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(admin_app) as test_client:
        yield test_client


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    """Exchange a real admin session cookie + CSRF header (the T12 pattern)."""
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    cookie = response.cookies.get("admin_session") or ""
    return {"admin_session": cookie, ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _customer_session(client: TestClient, route_state: str) -> str:
    """Seed + activate one customer; return the live session Bearer token."""
    from app.activation_code_service import compute_code_digest

    code = "XS04-C27AAAA-BBBBBBB-CCCCCCC-DDDDDDD"
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            "VALUES ('cw027-batch', 'cw027-batch', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u') ON CONFLICT (id) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) VALUES ('code-cw027', 'cw027-batch', %s, "
            "1, 'XS04-****', 'ISSUED', '2026-01-01T00:00:00+00:00') "
            "ON CONFLICT (id) DO NOTHING",
            (compute_code_digest(code, key=TEST_KEY.encode("utf-8")),),
        )
    response = client.post(
        "/api/customer/activate",
        json={
            "activation_code": code,
            "device_fingerprint": "cw027-fp",
            "device_name": "CW-027 device",
            "device_platform": "windows",
        },
        headers={"Idempotency-Key": "cw027-activate"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_token"])


# ---------------------------------------------------------------------------
# Part 1 — the static admin authority matrix (漏项=0)
# ---------------------------------------------------------------------------

ADMIN_WRITE_AUTHORITIES = {
    "get_admin_writer",
    "get_control_route_user",
    # B3 (2026-09-22 review): bulk exports are GET routes that carry a
    # write-level dependency — the gate follows the route's intent, not its
    # HTTP method, so the CSV dumps stay on this axis.
    "get_control_writer",
    "require_settings_admin",
    "get_character_admin",
    # 方案 P2-4 团队与权限：超管专属写门槛——是 get_admin_writer 的加强版，
    # 矩阵按「携带管理级权限」核销；role+is_super_admin 的具体判定在
    # test_admin_team_routes.py 有专测。
    "get_super_admin_writer",
}
ADMIN_READ_AUTHORITIES = ADMIN_WRITE_AUTHORITIES | {
    "get_admin_actor",
    # 方案 P2-4：团队信息只对超管可见，读侧门槛是 get_admin_actor 的加强版。
    "get_super_admin_actor",
}

# Self-scoped recovery changes only the caller's password and revokes their
# sessions. The recovery restriction is enforced inside get_admin_actor and
# the password route; admin/auditor business writes retain writer authority.
SELF_SCOPED_EXEMPT = {
    ("DELETE", "/api/control/admin/session"),
    ("PUT", "/api/control/admin/password"),
}
# Session establishment (credential exchange / password login) runs before
# any session exists — they are the only dep-less admin routes.
BOOTSTRAP_EXEMPT = {
    ("POST", "/api/control/admin/session/exchange"),
    ("POST", "/api/control/admin/session/password"),
}


def _admin_routes() -> list[tuple[str, str, tuple[str, ...]]]:
    import warnings

    warnings.filterwarnings("ignore")
    from app.main import app

    def walk(routes: list) -> list:
        out = []
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                out.extend(walk(route.original_router.routes))
            elif getattr(route, "methods", None) and hasattr(route, "dependant"):
                out.append(route)
        return out

    rows = []
    for route in walk(app.routes):
        deps = tuple(d.call.__name__ for d in route.dependant.dependencies)
        is_admin = bool(
            {
                "get_admin_actor",
                "get_admin_writer",
                "get_control_route_user",
                "require_settings_admin",
                "get_character_admin",
            }
            & set(deps)
        ) or route.path.startswith(("/api/control/", "/api/admin/"))
        if not is_admin:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            rows.append((method, route.path, deps))
    return rows


def test_every_admin_method_path_carries_an_admin_authority() -> None:
    """补一张保留管理操作→route→AdminReader/AdminWriter 的静态核销表：
    每条管理路由的读、写方法都必须携带管理级权限，漏项=0；新增未分类
    管理路由即失败逼出评审。"""
    rows = _admin_routes()
    assert len(rows) >= 80, f"admin surface unexpectedly small: {len(rows)}"

    writes: list[tuple[str, str, tuple[str, ...]]] = []
    reads: list[tuple[str, str, tuple[str, ...]]] = []
    for method, path, deps in rows:
        kind = "WRITE" if method in {"POST", "PATCH", "PUT", "DELETE"} else "READ"
        if kind == "WRITE":
            writes.append((method, path, deps))
        else:
            reads.append((method, path, deps))
        if (method, path) in SELF_SCOPED_EXEMPT:
            assert "get_admin_actor" in deps
            continue
        if (method, path) in BOOTSTRAP_EXEMPT:
            continue
        authority = ADMIN_WRITE_AUTHORITIES if kind == "WRITE" else ADMIN_READ_AUTHORITIES
        assert authority & set(deps), (
            f"{kind} admin route without an admin authority: {method} {path} deps={deps}"
        )

    assert len(writes) >= 30, "admin write surface unexpectedly small"
    assert len(reads) >= 20, "admin read surface unexpectedly small"
    # The bottom-line specifics from the CW-027 spec:
    assert ("DELETE", "/api/control/admin/session") in SELF_SCOPED_EXEMPT
    # The legacy proxy token must not be a route dependency anywhere — it may
    # only be consulted inside get_control_route_user's lane switch.
    for method, path, deps in rows:
        assert "get_control_user" not in deps, f"{method} {path} rides the raw proxy identity"


# ---------------------------------------------------------------------------
# Part 2 — the dynamic bottom-line grid (TEST-PG)
# ---------------------------------------------------------------------------


def test_admin_write_happy_path_records_contract_replay_and_audit(
    client: TestClient, route_state: str
) -> None:
    headers = {**_admin_session(client), "Idempotency-Key": "cw027-adjust-1"}
    created = client.post(ADJUST_PATH, headers=headers, json=ADJUST_BODY)
    assert created.status_code == 201, created.text

    # The write contract recorded the real actor (not a proxy identity),
    # and the append-only adjustment row names the admin + reason.
    with psycopg.connect(route_state) as conn:
        contract = conn.execute(
            "SELECT actor_user_id, route FROM admin_write_idempotency "
            "WHERE route LIKE '%adjustments%'"
        ).fetchone()
        audit = conn.execute(
            "SELECT admin_user_id, reason FROM admin_adjustments LIMIT 1"
        ).fetchone()
    assert contract is not None and str(contract[0]) == "admin_u"
    assert audit is not None
    assert str(audit[0]) == "admin_u"
    assert "客服补偿" in str(audit[1])

    replay = client.post(ADJUST_PATH, headers=headers, json=ADJUST_BODY)
    assert replay.status_code == 201, replay.text
    assert replay.headers.get("X-Idempotent-Replay") == "true"
    with psycopg.connect(route_state) as conn:
        orders = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE merchant_order_no LIKE 'ADJ-%'"
        ).fetchone()
        ledger = conn.execute(
            "SELECT count(*) FROM wallet_transactions WHERE type = 'CHARGE'"
        ).fetchone()
    assert orders is not None and int(orders[0]) == 1, "replay must not book twice"
    assert ledger is not None and int(ledger[0]) == 1


def test_admin_write_contract_fields_are_enforced(client: TestClient) -> None:
    headers = {**_admin_session(client), "Idempotency-Key": "cw027-contract"}
    # The TestClient still carries the exchanged cookie, so the session gate
    # passes and the write-method CSRF gate refuses first (the earlier
    # grid test covers the no-cookie 401).
    missing_key = client.post(ADJUST_PATH, json=ADJUST_BODY)
    assert missing_key.status_code == 403
    assert missing_key.json()["detail"]["code"] == "ADMIN_CSRF_REQUIRED"

    no_confirm = client.post(ADJUST_PATH, headers=headers, json={**ADJUST_BODY, "confirm": False})
    assert no_confirm.status_code == 400
    assert no_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    blank_reason = client.post(ADJUST_PATH, headers=headers, json={**ADJUST_BODY, "reason": "  "})
    assert blank_reason.status_code == 400
    assert blank_reason.json()["detail"]["code"] == "REASON_REQUIRED"


def test_missing_or_wrong_csrf_is_refused(client: TestClient) -> None:
    session = _admin_session(client)
    no_csrf = {"admin_session": session["admin_session"]}
    refused = client.post(ADJUST_PATH, headers=no_csrf, json=ADJUST_BODY)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "ADMIN_CSRF_REQUIRED"

    wrong = {"admin_session": session["admin_session"], ADMIN_CSRF_HEADER: "not-the-token"}
    invalid = client.post(ADJUST_PATH, headers=wrong, json=ADJUST_BODY)
    assert invalid.status_code == 403
    assert invalid.json()["detail"]["code"] == "ADMIN_CSRF_INVALID"


def test_auditor_is_read_only_but_may_log_out(client: TestClient) -> None:
    session = _admin_session(client, actor="auditor_u")

    read = client.get(
        "/api/control/customers/customer_u/adjustments",
        headers={"admin_session": session["admin_session"]},
    )
    assert read.status_code == 200, read.text

    denied = client.post(
        ADJUST_PATH,
        headers={**session, "Idempotency-Key": "cw027-auditor-write"},
        json=ADJUST_BODY,
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUDITOR_READ_ONLY"

    # Self-scoped logout is exempt from the writer gate (the static matrix
    # pins this exemption): an auditor may end their own read session.
    logout = client.delete(
        "/api/control/admin/session",
        headers={
            "admin_session": session["admin_session"],
            ADMIN_CSRF_HEADER: session[ADMIN_CSRF_HEADER],
        },
    )
    assert logout.status_code in (200, 204), logout.text
    after = client.get(
        "/api/control/customers/customer_u/adjustments",
        headers={"admin_session": session["admin_session"]},
    )
    assert after.status_code == 401, "the revoked session must not keep reading"


def test_customer_credential_cannot_reach_admin_surfaces(
    client: TestClient, route_state: str
) -> None:
    token = _customer_session(client, route_state)
    bearer = {"Authorization": f"Bearer {token}"}

    # Session-gated admin surface: a Bearer customer credential is not an
    # admin cookie — the route must answer 401 before any business logic.
    session_gated = client.post(
        ADJUST_PATH, headers={**bearer, "Idempotency-Key": "cw027-cust-write"}, json=ADJUST_BODY
    )
    assert session_gated.status_code == 401, session_gated.text

    # Role-gated admin surface (settings admin rides the business identity):
    # the customer session resolves, but the admin role gate refuses.
    role_gated = client.patch(
        "/api/admin/settings/billing",
        headers=bearer,
        json={"free_seconds_per_generation": 3},
    )
    assert role_gated.status_code == 403, role_gated.text


def test_expired_admin_credential_and_revoked_session_are_refused(
    client: TestClient,
) -> None:
    expired_credential = issue_exchange_credential(
        "admin_u",
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(seconds=120),
    )
    expired = client.post(
        "/api/control/admin/session/exchange", json={"credential": expired_credential}
    )
    assert expired.status_code == 401
    assert expired.json()["detail"]["code"] == "EXCHANGE_CREDENTIAL_INVALID"

    session = _admin_session(client)
    logout = client.delete(
        "/api/control/admin/session",
        headers={
            "admin_session": session["admin_session"],
            ADMIN_CSRF_HEADER: session[ADMIN_CSRF_HEADER],
        },
    )
    assert logout.status_code in (200, 204)
    reused = client.get(
        "/api/control/customers/customer_u/adjustments",
        headers={"admin_session": session["admin_session"]},
    )
    assert reused.status_code == 401, "a logged-out session must be dead, not replayable"


def test_legacy_proxy_token_only_works_outside_customer_production(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = {"X-Control-Proxy-Token": "cw027-proxy-token"}

    # Outside customer production the internal P0 compatibility lane keeps
    # its proxy-token boundary (CW-041 will exit it; the digest here is the
    # all-zero test digest, so the lookup fails closed as CONTROL_AUTH_INVALID
    # rather than authenticating — what matters is that the *lane* is active).
    outside = client.get("/api/control/wallet-transactions", headers=proxy)
    assert outside.status_code == 401
    assert outside.json()["detail"]["code"] == "CONTROL_AUTH_INVALID"

    # On the customer-production lane the shared proxy identity is dead —
    # get_control_route_user switches to the per-operator admin session and
    # never even consults the proxy header: without a cookie the request is
    # a plain 401, whatever the proxy token says.
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    forbidden = client.get("/api/control/wallet-transactions", headers=proxy)
    assert forbidden.status_code == 401
    assert forbidden.json()["detail"]["code"] == "ADMIN_SESSION_INVALID"

    # The per-operator admin session remains the only production key.
    session = _admin_session(client)
    # In customer production the exchange sets a Secure cookie, which the
    # http(s)-less TestClient jar will not replay — pass it explicitly.
    ok = client.get(
        "/api/control/wallet-transactions",
        headers={"Cookie": f"admin_session={session['admin_session']}"},
    )
    assert ok.status_code == 200, ok.text
