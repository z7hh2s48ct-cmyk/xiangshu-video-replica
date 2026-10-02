"""CW-076 — customer self-service registration.

Fail-first tests for the frozen files ``server/app/password_hashing.py`` and
``server/app/customer_auth_routes.py`` plus the registration revision. Three lanes:

- password-hashing units and the fail-closed 503 guard run with NO database, so
  they execute in every environment (including a laptop without PostgreSQL);
- the ``POST /api/customer/register`` contract runs against a dedicated migrated
  PostgreSQL fixture database: the account (``role='customer'`` + a salted
  scrypt hash + ``registration_source='self_register'``) and its wallet are
  created atomically, the secret never leaves the boundary, and the stable
  error codes (409 ``USERNAME_TAKEN`` / 400 ``WEAK_PASSWORD`` / 400
  ``INVALID_USERNAME``) own their cases instead of a 422 or a 500;
- the registration revision's schema guards run against their OWN dedicated database so the
  downgrade rehearsal can never disturb the route fixture: the three CHECK
  constraints reject a password-less ``self_register``, a blank hash and an
  unknown source, and the downgrade refuses (then symmetrically succeeds) per
  the 026/044/083 data-loss-guard precedent.

The route fixture database is self-managed with raw admin DROP/CREATE (the
``test_customer_devices`` precedent), so no name is added to
``pg_test_kit.RECORDED_TEST_DATABASES`` — that allowlist only guards the kit's
own ``create_test_database`` helper.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip
from psycopg.errors import CheckViolation

from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.password_hashing import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    hash_password,
    validate_password_policy,
    verify_password,
)

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

# Two dedicated databases, each created/dropped by its own module fixture so the
# downgrade rehearsal never shares a database with the route contract tests.
CW076_DB_NAME = "cw076_registration_test"
CW076_MIGRATION_DB_NAME = "cw076_migration_test"

REGISTER_PATH = "/api/customer/register"
# 重挂后链尾：#188 recharge_packages 之后是 Phase 3a sub_account_quotas、
# Phase 3b sub_account_permissions，再叠加 BILLING-OBS-20260922 三个 analysis 迁移
# 及其后的 REFUND 调账、1800 部署垫片、20260924T0000 交易号唯一索引、
# 20260924T0100 口播提交时刻列、20260924T0200 口播隐藏偏好表、20260926T0000
# 爆款首页策展排行与 20260925T1400 计费触发器追加修复（api_metadata 的 PENDING 期写入）。
HEAD_REVISION = "20261002T1000_oral_video_compatibility"
PRIOR_REVISION = "20260912T1353_customer_discounts"

# A policy-valid password (>= MIN_PASSWORD_LENGTH, not blank). Never a secret.
VALID_PASSWORD = "correct-horse-battery"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _dedicated_dsn(database: str) -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{database}"


def _alembic_config(dsn: str) -> Any:
    """In-process Alembic config (mirrors test_customer_devices / cw056)."""
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    return config


# ---------------------------------------------------------------------------
# Lane 1 — password hashing units (no database) — always run
# ---------------------------------------------------------------------------


def test_customer_password_accepts_six_characters() -> None:
    assert MIN_PASSWORD_LENGTH == 6
    assert verify_password("test-6", hash_password("test-6"))
    with pytest.raises(PasswordPolicyError):
        hash_password("short")


def test_hash_password_is_scrypt_encoded_and_hides_plaintext() -> None:
    encoded = hash_password(VALID_PASSWORD)
    assert encoded.startswith("scrypt$")
    assert len(encoded.split("$")) == 6
    assert VALID_PASSWORD not in encoded


def test_hash_password_is_salted_so_repeats_diverge() -> None:
    first = hash_password(VALID_PASSWORD)
    second = hash_password(VALID_PASSWORD)
    assert first != second, "a fresh salt must make two hashes of one password differ"
    assert verify_password(VALID_PASSWORD, first)
    assert verify_password(VALID_PASSWORD, second)


def test_verify_password_rejects_a_wrong_password() -> None:
    encoded = hash_password(VALID_PASSWORD)
    assert verify_password(VALID_PASSWORD + "!", encoded) is False


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "scrypt",
        "scrypt$16384$8$1$AAAA",  # too few fields
        "pbkdf2$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA$BBBB",  # wrong algorithm
        "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA$BBBB",  # wrong cost params
        "scrypt$16384$8$1$@@@@notbase64@@@@$BBBB",  # undecodable salt
        "scrypt$16384$8$1$AAAA$BBBB",  # salt/digest wrong length
    ],
)
def test_verify_password_fails_closed_on_malformed(malformed: str) -> None:
    assert verify_password(VALID_PASSWORD, malformed) is False


def test_validate_password_policy_enforces_length_and_blank() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password_policy("x" * (MIN_PASSWORD_LENGTH - 1))
    with pytest.raises(PasswordPolicyError):
        validate_password_policy("x" * (MAX_PASSWORD_LENGTH + 1))
    with pytest.raises(PasswordPolicyError):
        validate_password_policy(" " * MIN_PASSWORD_LENGTH)
    # The floor and ceiling themselves are accepted.
    validate_password_policy("x" * MIN_PASSWORD_LENGTH)
    validate_password_policy("x" * MAX_PASSWORD_LENGTH)


def test_hash_password_propagates_policy_error() -> None:
    with pytest.raises(PasswordPolicyError):
        hash_password("short")


# ---------------------------------------------------------------------------
# Lane 2 — fail-closed without PostgreSQL (no database) — always runs
# ---------------------------------------------------------------------------


def test_register_fails_closed_on_the_sqlite_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A SQLite target must answer 503, never self-register a customer."""
    from app.customer_auth_routes import router as customer_auth_router

    app = FastAPI()
    app.include_router(customer_auth_router)
    monkeypatch.setenv(DATABASE_URL_ENV, f"sqlite:///{tmp_path / 'cw076.sqlite'}")
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    close_pg_pool()
    try:
        with TestClient(app) as client:
            response = client.post(
                REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD}
            )
    finally:
        close_pg_pool()
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "REGISTRATION_SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Lane 3 — registration contract (dedicated migrated PostgreSQL fixture)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registration_dsn() -> Iterator[str]:
    from alembic import command

    require_pg_or_explicit_skip(_pg_dsn())
    dsn = _dedicated_dsn(CW076_DB_NAME)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{CW076_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{CW076_DB_NAME}"')
    command.upgrade(_alembic_config(dsn), "head")
    try:
        yield dsn
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{CW076_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(registration_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(registration_dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute("TRUNCATE wallets, users, security_rate_limit_counters CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        # CASCADE 会连坐带 users 外键的单行配置表（alert_settings 测试的已知坑），
        # registration_bonus_settings 的种子行同样被清掉——重建回默认 0，让每个
        # 用例都从「关闭赠送」的出厂态出发；runtime_settings（updated_by_user_id
        # 同样引用 users）是赠送定价快照的来源，一并重建。
        conn.execute(
            "INSERT INTO registration_bonus_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen) "
            "VALUES (1, 4, 2, 1000, 10000, 1000)"
        )
    yield registration_dsn
    close_pg_pool()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[TestClient]:
    from app.customer_auth_routes import CustomerBrowserTransport
    from app.customer_auth_routes import router as customer_auth_router
    from app.customer_session_routes import router as session_router
    from app.recharge_routes import router as recharge_router
    from app.viral_import_routes import router as viral_import_router

    app = FastAPI()
    app.add_middleware(CustomerBrowserTransport)
    app.include_router(customer_auth_router)
    app.include_router(session_router)
    app.include_router(recharge_router)
    app.include_router(viral_import_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    import base64
    import secrets

    for name in (
        "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY",
        "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
        "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY",
    ):
        monkeypatch.setenv(name, secrets.token_urlsafe(48))
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_ACCOUNT", "1000")
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def _password_login(client: TestClient, **overrides: Any) -> Any:
    return client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "username": "alice",
            "password": VALID_PASSWORD,
            "device_fingerprint": "cw077-web-device-0001",
            **overrides,
        },
    )


def test_browser_cookie_refresh_csrf_logout_and_bearer_isolation(client: TestClient) -> None:
    client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD})
    web = {"X-Customer-Web": "1", "Origin": "http://127.0.0.1"}
    login = client.post(
        "/api/customer/login",
        headers={**web, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "username": "alice",
            "password": VALID_PASSWORD,
            "device_fingerprint": "cookie-reload-device-0001",
            "device_platform": "browser",
        },
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["session_token"].startswith("web-session:")
    assert body["device_token"].startswith("web-device:")
    assert client.cookies.get("customer_web_session") not in login.text
    assert client.cookies.get("customer_web_device") not in login.text
    cookies = login.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("HttpOnly" in c and "SameSite=strict" in c and "Path=/api" in c for c in cookies)
    session = {**web, "Authorization": "Bearer " + body["session_token"]}
    assert client.get("/api/customer/profile", headers=session).status_code == 200
    # Cookie possession without the CSRF marker must not authenticate writes.
    assert client.post("/api/customer/sessions/heartbeat", headers=web).status_code == 401
    assert (
        client.post(
            "/api/customer/sessions/heartbeat",
            headers={
                **session,
                "Origin": "https://attacker.example",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/customer/sessions/heartbeat",
            headers={
                **web,
                "Authorization": "Bearer web-session:incorrect",
            },
        ).status_code
        == 403
    )
    # A reload gets non-secret handles; the normal device verifier renews it.
    boot = client.get("/api/customer/browser-session", headers=web)
    assert boot.status_code == 200
    assert boot.headers["cache-control"] == "no-store"
    assert boot.json()["device_token"] == body["device_token"]
    malformed = client.post(
        "/api/customer/sessions/login",
        headers={**web, "Authorization": "Bearer " + boot.json()["device_token"]},
        json={"session_token": "web-session:无效"},
    )
    assert malformed.status_code == 403
    assert malformed.json()["detail"]["code"] == "BROWSER_CSRF_INVALID"
    restored = client.post(
        "/api/customer/sessions/login",
        headers={
            **web,
            "Authorization": "Bearer " + boot.json()["device_token"],
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={"session_token": boot.json()["session_token"]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["session_id"] == body["session_id"]
    assert restored.json()["session_token"] == body["session_token"]
    assert (
        client.post(
            "/api/customer/sessions/logout",
            headers={
                **session,
                "Idempotency-Key": str(uuid.uuid4()),
            },
        ).status_code
        == 204
    )
    assert client.get("/api/customer/profile", headers=session).status_code in (401, 403)
    cleared = client.delete(
        "/api/customer/browser-session",
        headers={
            **web,
            "Authorization": "Bearer " + body["device_token"],
        },
    )
    assert cleared.status_code == 204
    assert client.get("/api/customer/browser-session", headers=web).json() == {
        "device_token": None,
        "session_token": None,
    }
    # The handle alone is never a credential on a fresh browser/desktop lane.
    assert client.get("/api/customer/profile", headers=session).status_code == 403
    assert (
        client.get(
            "/api/customer/profile",
            headers={
                "Authorization": "Bearer " + body["session_token"],
            },
        ).status_code
        == 401
    )


def test_existing_browser_session_upgrades_without_password_or_new_device(
    client: TestClient,
) -> None:
    client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD})
    original = _password_login(client).json()
    upgrade = client.post(
        "/api/customer/sessions/login",
        headers={
            "X-Customer-Web": "1",
            "Origin": "http://127.0.0.1",
            "Authorization": "Bearer " + original["device_token"],
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={"session_token": original["session_token"]},
    )
    assert upgrade.status_code == 200, upgrade.text
    assert upgrade.json()["session_id"] == original["session_id"]
    assert upgrade.json()["device_token"].startswith("web-device:")
    assert upgrade.json()["session_token"].startswith("web-session:")
    assert original["session_token"] not in upgrade.text
    assert original["device_token"] not in upgrade.text
    assert client.cookies.get("customer_web_session") == original["session_token"]


def test_browser_production_cookie_flags_and_account_revocation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    route_state: str,
) -> None:
    import app.customer_auth_routes as routes

    monkeypatch.setattr(routes, "is_customer_production", lambda: True)
    monkeypatch.setattr(routes, "customer_public_origin", lambda: "https://customer.example")
    client.base_url = "https://customer.example"
    client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD})
    web = {"X-Customer-Web": "1", "Origin": "https://customer.example"}
    login = client.post(
        "/api/customer/login",
        headers={
            **web,
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={
            "username": "alice",
            "password": VALID_PASSWORD,
            "device_fingerprint": "cookie-secure-device-0001",
        },
    )
    assert login.status_code == 200, login.text
    assert all("Secure" in value for value in login.headers.get_list("set-cookie"))
    headers = {**web, "Authorization": "Bearer " + login.json()["session_token"]}
    assert client.get("/api/customer/profile", headers=headers).status_code == 200
    with psycopg.connect(route_state) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE username = 'alice'")
    assert client.get("/api/customer/profile", headers=headers).status_code == 401
    assert client.post("/api/customer/sessions/heartbeat", headers=headers).status_code == 401


def test_password_login_profile_heartbeat_logout_and_no_device_bypass(client: TestClient) -> None:
    assert (
        client.post(
            REGISTER_PATH,
            json={
                "username": "alice",
                "password": VALID_PASSWORD,
            },
        ).status_code
        == 201
    )
    login = _password_login(client)
    assert login.status_code == 200, login.text
    body = login.json()
    headers = {"Authorization": "Bearer " + body["session_token"]}
    assert client.get("/api/customer/profile", headers=headers).json()["username"] == "alice"
    assert client.post("/api/customer/sessions/heartbeat", headers=headers).status_code == 200
    restored = client.post(
        "/api/customer/sessions/login",
        json={
            "session_token": body["session_token"],
        },
        headers={
            "Authorization": "Bearer " + body["device_token"],
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert restored.status_code == 200, restored.text
    assert (
        client.post(
            "/api/customer/sessions/logout",
            headers={
                **headers,
                "Idempotency-Key": str(uuid.uuid4()),
            },
        ).status_code
        == 204
    )
    bypass = client.post(
        "/api/customer/sessions/login",
        json={},
        headers={
            "Authorization": "Bearer " + body["device_token"],
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert bypass.status_code == 401, bypass.text
    assert _password_login(client).status_code == 200


def test_password_login_unlimited_devices_and_retains_identity_on_key_rotation(
    client: TestClient,
    route_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import secrets

    user_id = client.post(
        REGISTER_PATH,
        json={
            "username": "alice",
            "password": VALID_PASSWORD,
        },
    ).json()["user_id"]
    with psycopg.connect(route_state) as conn:
        conn.execute("UPDATE users SET max_devices = 1 WHERE id = %s", (user_id,))
    first = _password_login(client)
    assert first.status_code == 200, first.text
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", secrets.token_urlsafe(48))
    again = _password_login(client)
    assert again.status_code == 200, again.text
    assert again.json()["device_id"] == first.json()["device_id"]
    other = _password_login(client, device_fingerprint="cw077-web-device-0002", takeover=True)
    assert other.status_code == 200, other.text
    for index in range(3, 8):
        another = _password_login(client, device_fingerprint=f"cw077-web-device-{index:04}")
        assert another.status_code == 200, another.text
    for token in (again.json()["session_token"], other.json()["session_token"]):
        assert (
            client.post(
                "/api/customer/sessions/heartbeat",
                headers={
                    "Authorization": "Bearer " + token,
                },
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/api/customer/sessions/logout",
            headers={
                "Authorization": "Bearer " + again.json()["session_token"],
                "Idempotency-Key": str(uuid.uuid4()),
            },
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/customer/sessions/heartbeat",
            headers={
                "Authorization": "Bearer " + other.json()["session_token"],
            },
        ).status_code
        == 200
    )


def test_password_login_expired_session_can_be_replaced_without_activation(
    client: TestClient, route_state: str
) -> None:
    user = client.post(
        REGISTER_PATH,
        json={
            "username": "alice",
            "password": VALID_PASSWORD,
        },
    ).json()
    assert _password_login(client).status_code == 200
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE customer_session_state SET "
            "created_at = now() - interval '5 minutes', "
            "last_heartbeat_at = now() - interval '3 minutes', "
            "lease_until = now() - interval '1 minute' WHERE user_id = %s",
            (user["user_id"],),
        )
    response = _password_login(client)
    assert response.status_code == 200, response.text


def test_register_creates_user_and_wallet_atomically(client: TestClient) -> None:
    response = client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["username"] == "alice"
    assert body["display_name"] == "alice"
    user_id = body["user_id"]
    uuid.UUID(user_id)  # a valid UUID string

    # The secret never crosses the boundary: not a field, not anywhere in text.
    assert "password" not in body
    assert "password_hash" not in body
    assert VALID_PASSWORD not in response.text

    with psycopg.connect(_dedicated_dsn(CW076_DB_NAME)) as conn:
        row = conn.execute(
            "SELECT role, registration_source, password_hash FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "customer"
        assert row[1] == "self_register"
        stored_hash = row[2]
        assert stored_hash and stored_hash != VALID_PASSWORD
        assert verify_password(VALID_PASSWORD, stored_hash), "the stored hash must verify"
        wallet = conn.execute(
            "SELECT user_id FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()
        assert wallet is not None, "the wallet must be created in the same transaction"


def test_register_duplicate_username_is_409(client: TestClient) -> None:
    first = client.post(REGISTER_PATH, json={"username": "bob", "password": VALID_PASSWORD})
    assert first.status_code == 201, first.text
    second = client.post(REGISTER_PATH, json={"username": "bob", "password": VALID_PASSWORD})
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "USERNAME_TAKEN"
    with psycopg.connect(_dedicated_dsn(CW076_DB_NAME)) as conn:
        count = conn.execute("SELECT count(*) FROM users WHERE username = 'bob'").fetchone()[0]
    assert count == 1, "the rejected duplicate must not create a second account"


def test_register_trims_surrounding_whitespace(client: TestClient) -> None:
    response = client.post(
        REGISTER_PATH, json={"username": "  carol  ", "password": VALID_PASSWORD}
    )
    assert response.status_code == 201, response.text
    assert response.json()["username"] == "carol"


@pytest.mark.parametrize(
    "username",
    ["ab", "a" * 33, "has space", "bad@char", "semi;colon", "tab\tchar"],
)
def test_register_invalid_username_is_400(client: TestClient, username: str) -> None:
    response = client.post(REGISTER_PATH, json={"username": username, "password": VALID_PASSWORD})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "INVALID_USERNAME"


def test_register_whitespace_only_username_is_400(client: TestClient) -> None:
    response = client.post(REGISTER_PATH, json={"username": "   ", "password": VALID_PASSWORD})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "INVALID_USERNAME"


def test_register_weak_password_is_400_and_creates_nothing(client: TestClient) -> None:
    response = client.post(REGISTER_PATH, json={"username": "dave", "password": "short"})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "WEAK_PASSWORD"
    with psycopg.connect(_dedicated_dsn(CW076_DB_NAME)) as conn:
        count = conn.execute("SELECT count(*) FROM users WHERE username = 'dave'").fetchone()[0]
    assert count == 0, "a rejected password must not create an account"


def test_register_rejects_unknown_body_fields(client: TestClient) -> None:
    response = client.post(
        REGISTER_PATH,
        json={"username": "erin", "password": VALID_PASSWORD, "email": "erin@example.com"},
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Registration bonus — 注册赠送积分（registration_bonus_settings）
# ---------------------------------------------------------------------------


def _set_registration_bonus(credits: int) -> None:
    """直接 SQL 改配置行：本文件只测注册路径，设置路由有自己的契约测试。"""
    with psycopg.connect(_dedicated_dsn(CW076_DB_NAME), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO registration_bonus_settings (id, bonus_credits) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET bonus_credits = EXCLUDED.bonus_credits",
            (credits,),
        )


def test_register_with_default_bonus_off_keeps_zero_credit_wallet(
    client: TestClient,
) -> None:
    """种子默认 0 = 关闭：注册后既无赠送流水也无 0 元订单（既有口径回归）。"""
    response = client.post(
        REGISTER_PATH, json={"username": "bonus-off", "password": VALID_PASSWORD}
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["user_id"]
    with psycopg.connect(_dedicated_dsn(CW076_DB_NAME)) as conn:
        wallet = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()
        assert wallet is not None and wallet[0] == 0
        orders = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
        ledger = conn.execute(
            "SELECT count(*) FROM wallet_transactions WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
    assert orders == 0
    assert ledger == 0


def test_register_grants_configured_bonus_with_paid_zero_fen_order(
    client: TestClient,
) -> None:
    """配置 30 分：钱包 +30，落一张 PAID 0 元 admin_adjustment 单 + 一条 CHARGE。"""
    _set_registration_bonus(30)
    response = client.post(REGISTER_PATH, json={"username": "bonus-on", "password": VALID_PASSWORD})
    assert response.status_code == 201, response.text
    user_id = response.json()["user_id"]
    with psycopg.connect(_dedicated_dsn(CW076_DB_NAME)) as conn:
        credits = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
        order = conn.execute(
            "SELECT provider, status, amount_fen, credits, merchant_order_no "
            "FROM recharge_orders WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        charge = conn.execute(
            "SELECT type, available_delta, reserved_delta, idempotency_key, "
            "recharge_order_id FROM wallet_transactions WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    assert credits == 30
    assert order == ("admin_adjustment", "PAID", 0, 30, f"REGBONUS-{user_id}")
    assert charge is not None
    assert charge[0] == "CHARGE"
    assert charge[1] == 30
    assert charge[2] == 0
    assert charge[3].startswith("registration_bonus:charge:")
    # 流水必须挂在同一张订单上：账本对账要求每张 PAID 单恰有一条同额 CHARGE。
    assert charge[4] is not None and charge[4] != ""


def test_register_after_bonus_reset_to_zero_creates_no_ledger_rows(
    client: TestClient,
) -> None:
    """改回 0 后再注册：不产生订单与流水——关闭是彻底关闭，不是发放 0 条。"""
    _set_registration_bonus(0)
    response = client.post(
        REGISTER_PATH, json={"username": "bonus-reset", "password": VALID_PASSWORD}
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["user_id"]
    with psycopg.connect(_dedicated_dsn(CW076_DB_NAME)) as conn:
        credits = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
        orders = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
        ledger = conn.execute(
            "SELECT count(*) FROM wallet_transactions WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
    assert credits == 0
    assert orders == 0
    assert ledger == 0


def test_duplicate_register_conflict_leaves_exactly_one_bonus_ledger(
    client: TestClient,
) -> None:
    """用户名冲突的第二次注册 409 且不落任何赠送痕迹——发放只随成功事务发生。"""
    _set_registration_bonus(40)
    first = client.post(REGISTER_PATH, json={"username": "bonus-dup", "password": VALID_PASSWORD})
    assert first.status_code == 201, first.text
    second = client.post(REGISTER_PATH, json={"username": "bonus-dup", "password": VALID_PASSWORD})
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "USERNAME_TAKEN"
    user_id = first.json()["user_id"]
    with psycopg.connect(_dedicated_dsn(CW076_DB_NAME)) as conn:
        orders = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
        ledger = conn.execute(
            "SELECT count(*) FROM wallet_transactions WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
        credits = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
    assert orders == 1
    assert ledger == 1
    assert credits == 40


# ---------------------------------------------------------------------------
# Lane 4 — registration revision schema guards (own dedicated database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def migration_dsn() -> Iterator[str]:
    from alembic import command

    require_pg_or_explicit_skip(_pg_dsn())
    dsn = _dedicated_dsn(CW076_MIGRATION_DB_NAME)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{CW076_MIGRATION_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{CW076_MIGRATION_DB_NAME}"')
    command.upgrade(_alembic_config(dsn), "head")
    try:
        yield dsn
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{CW076_MIGRATION_DB_NAME}" WITH (FORCE)')


def test_registration_revision_adds_credential_columns_and_checks(migration_dsn: str) -> None:
    with psycopg.connect(migration_dsn) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users'"
            ).fetchall()
        }
        constraints = {
            row[0]
            for row in conn.execute(
                "SELECT c.conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = 'public' AND t.relname = 'users' AND c.contype = 'c'"
            ).fetchall()
        }
    assert {"password_hash", "registration_source"} <= columns
    assert {
        "ck_users_password_hash_not_blank",
        "ck_users_registration_source_known",
        "ck_users_self_register_has_password",
    } <= constraints


def test_registration_revision_rejects_self_register_without_password(migration_dsn: str) -> None:
    with psycopg.connect(migration_dsn) as conn:
        with pytest.raises(CheckViolation):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role, registration_source) "
                "VALUES (%s, %s, %s, 'customer', 'self_register')",
                (str(uuid.uuid4()), "nopass", "nopass"),
            )
        conn.rollback()


def test_registration_revision_rejects_blank_password_hash(migration_dsn: str) -> None:
    with psycopg.connect(migration_dsn) as conn:
        with pytest.raises(CheckViolation):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role, password_hash) "
                "VALUES (%s, %s, %s, 'customer', '   ')",
                (str(uuid.uuid4()), "blankhash", "blankhash"),
            )
        conn.rollback()


def test_registration_revision_rejects_unknown_registration_source(migration_dsn: str) -> None:
    with psycopg.connect(migration_dsn) as conn:
        with pytest.raises(CheckViolation):
            conn.execute(
                "INSERT INTO users "
                "(id, username, display_name, role, password_hash, registration_source) "
                "VALUES (%s, %s, %s, 'customer', %s, 'carrier_pigeon')",
                (str(uuid.uuid4()), "badsrc", "badsrc", hash_password(VALID_PASSWORD)),
            )
        conn.rollback()


def test_registration_revision_allows_a_credential_less_internal_account(
    migration_dsn: str,
) -> None:
    """The columns stay NULL-able for the admin/activation/bootstrap lanes."""
    user_id = str(uuid.uuid4())
    with psycopg.connect(migration_dsn) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'admin')",
            (user_id, "internal-acct", "internal-acct"),
        )
        row = conn.execute(
            "SELECT password_hash, registration_source FROM users WHERE id = %s", (user_id,)
        ).fetchone()
    assert row == (None, None)


def test_registration_revision_downgrade_guard_refuses_then_succeeds_symmetrically(
    migration_dsn: str,
) -> None:
    from alembic import command

    config = _alembic_config(migration_dsn)
    user_id = str(uuid.uuid4())
    with psycopg.connect(migration_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users "
            "(id, username, display_name, role, password_hash, registration_source) "
            "VALUES (%s, %s, %s, 'customer', %s, 'self_register')",
            (user_id, "guarded", "guarded", hash_password(VALID_PASSWORD)),
        )

    # A credentialed account blocks the downgrade and leaves the pointer at the head.
    with pytest.raises(RuntimeError, match="cannot downgrade 20260912T1400"):
        command.downgrade(config, PRIOR_REVISION)
    with psycopg.connect(migration_dsn) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            HEAD_REVISION
        )

    # Once the credentialed row is gone the downgrade succeeds symmetrically.
    with psycopg.connect(migration_dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM wallets WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
    command.downgrade(config, PRIOR_REVISION)
    with psycopg.connect(migration_dsn) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users'"
            ).fetchall()
        }
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            PRIOR_REVISION
        )
    assert "password_hash" not in columns
    assert "registration_source" not in columns

    # Restore head so this rehearsal cannot disturb any later test in the module.
    command.upgrade(config, "head")


@pytest.mark.parametrize("username,password", [("alice", "wrong-6"), ("missing", VALID_PASSWORD)])
def test_password_login_rejects_invalid_credentials_without_session(
    client: TestClient,
    route_state: str,
    username: str,
    password: str,
) -> None:
    client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD})
    response = _password_login(client, username=username, password=password)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_CREDENTIALS"
    assert password not in response.text
    with psycopg.connect(route_state) as conn:
        assert conn.execute("SELECT count(*) FROM customer_session_state").fetchone()[0] == 0


def test_disabled_password_account_cannot_login_heartbeat_or_read_profile(
    client: TestClient,
    route_state: str,
) -> None:
    user = client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD}).json()
    sessions = [
        _password_login(client, device_fingerprint=f"account-device-{i:04}").json()
        for i in range(2)
    ]
    with psycopg.connect(route_state) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = %s", (user["user_id"],))
    assert _password_login(client).status_code == 401
    for session in sessions:
        headers = {"Authorization": "Bearer " + session["session_token"]}
        assert client.get("/api/customer/profile", headers=headers).status_code == 401
        assert client.post("/api/customer/sessions/heartbeat", headers=headers).status_code == 401


def test_password_response_replay_retains_identity_during_key_rotation(
    client: TestClient,
    route_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import secrets

    client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD})
    headers = {"Idempotency-Key": "password-retry-stable"}
    body = {
        "username": "alice",
        "password": VALID_PASSWORD,
        "device_fingerprint": "retry-device-0001",
    }
    first = client.post("/api/customer/login", headers=headers, json=body)
    assert first.status_code == 200, first.text
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", secrets.token_urlsafe(48))
    replay = client.post("/api/customer/login", headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert replay.headers["Cache-Control"] == "no-store"
    assert replay.headers["X-Idempotent-Replay"] == "true"
    changed = client.post(
        "/api/customer/login",
        headers=headers,
        json={**body, "device_fingerprint": "retry-device-0002"},
    )
    assert changed.status_code == 409
    with psycopg.connect(route_state) as conn:
        assert conn.execute("SELECT count(*) FROM customer_devices").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM customer_session_events WHERE event = 'LOGIN'"
            ).fetchone()[0]
            == 1
        )
        conn.execute(
            "UPDATE customer_idempotency_envelopes SET ciphertext = 'invalid' "
            "WHERE operation = 'password_login'"
        )
    damaged = client.post("/api/customer/login", headers=headers, json=body)
    assert damaged.status_code == 503, damaged.text
    assert "session_token" not in damaged.text


def test_registration_and_failed_login_limits_persist_after_rejection(
    client: TestClient,
    route_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1")
    assert (
        client.post(
            REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD}
        ).status_code
        == 201
    )
    limited = client.post(REGISTER_PATH, json={"username": "bobby", "password": VALID_PASSWORD})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    assert _password_login(client, password="wrong-6").status_code == 401
    assert _password_login(client).status_code == 429
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute("SELECT count(*) FROM users WHERE username = 'bobby'").fetchone()[0] == 0
        )


def test_concurrent_password_retries_create_one_device_and_one_session(
    client: TestClient,
    route_state: str,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD})
    barrier = Barrier(4)

    def submit(_: int) -> Any:
        barrier.wait(timeout=30)
        return client.post(
            "/api/customer/login",
            headers={"Idempotency-Key": "concurrent-retry"},
            json={
                "username": "alice",
                "password": VALID_PASSWORD,
                "device_fingerprint": "retry-device-0001",
            },
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))
    assert [r.status_code for r in responses] == [200] * 4
    assert all(r.json() == responses[0].json() for r in responses)
    with psycopg.connect(route_state) as conn:
        assert conn.execute("SELECT count(*) FROM customer_devices").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM customer_session_state").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM customer_session_events WHERE event = 'LOGIN'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("renew_during", [None, "provider", "media"])
def test_password_customer_xiaohongshu_resolution_import_and_replay_on_postgres(
    client: TestClient,
    route_state: str,
    monkeypatch: pytest.MonkeyPatch,
    renew_during: str | None,
) -> None:
    import app.viral_import as domain
    import app.viral_import_routes as routes
    from app.db_portable import BusinessConnection
    from app.generation_worker import run_worker_once
    from app.storage import FakeStorageAdapter
    from app.viral_link import DouyidouHttpTransport, DouyidouLinkClient
    from app.viral_media import ViralMediaResult
    from app.viral_tikhub import ViralVideo

    class Transport(DouyidouHttpTransport):
        calls = 0

        def request(self, url: str, *, headers: Any) -> bytes:
            self.calls += 1
            if renew_during == "provider":
                renew_session()
            return b'{"code":0,"data":{"note_id":"66e012345678901234abcdef","video":["https://cdn.example/note.mp4"],"audio":["https://cdn.example/background-music.m4a"]}}'

    storage = FakeStorageAdapter(provider="fake", bucket="private")

    class Pipeline:
        def __init__(self, *, client: Any, storage: Any, **kwargs: Any) -> None:
            self.storage = storage

        def fetch(self, video: ViralVideo, *, prefer: str | None = None) -> ViralMediaResult:
            assert video.platform == "xiaohongshu"
            # 文案导入 Worker 首选音频归档（2026-09-30 拍板）；本桩固定回视频对象，
            # copy 导入对归档形态不挑（perform 只对 replica 强制视频）。
            assert prefer == "audio"
            stored = self.storage.put_object(
                "viral/xiaohongshu/note.mp4", b"contract-video", content_type="video/mp4"
            )
            return ViralMediaResult(
                kind="video",
                storage_uri=stored.uri,
                url="https://cdn.example/note.mp4",
                size=stored.size,
                content_type=stored.content_type,
                cache_hit=False,
                sha256=stored.sha256,
            )

    transport = Transport()
    resolver = DouyidouLinkClient(
        app_id="local-contract", app_secret="local-contract", transport=transport
    )
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    monkeypatch.setattr(routes, "get_media_storage", lambda conn: storage)
    monkeypatch.setattr(
        routes,
        "preflight_resolved_media",
        lambda *args, **kwargs: renew_session() if renew_during == "media" else None,
    )
    monkeypatch.setattr(domain, "ViralMediaPipeline", Pipeline)
    monkeypatch.setattr(domain, "viral_source_client_from_settings", lambda conn: None)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_runtime_controls (id, collection_enabled, import_enabled) "
            "VALUES (1, 1, 1) ON CONFLICT (id) DO UPDATE SET import_enabled = 1"
        )
    user = client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD}).json()
    session = _password_login(client).json()
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE customer_session_state SET lease_until="
            "(clock_timestamp() + interval '60 seconds')::text WHERE user_id=%s",
            (user["user_id"],),
        )

    def renew_session() -> None:
        renewed = client.post(
            "/api/customer/sessions/heartbeat",
            headers={"Authorization": "Bearer " + session["session_token"]},
        )
        assert renewed.status_code == 200, renewed.text

    headers = {
        "Authorization": "Bearer " + session["session_token"],
        "Idempotency-Key": "xhs-resolve",
    }
    payload = {"url": "https://xhslink.com/a/local-contract", "purpose": "copy"}
    resolved = client.post("/api/viral/link-resolutions", headers=headers, json=payload)
    assert resolved.status_code == 200, resolved.text
    replay = client.post("/api/viral/link-resolutions", headers=headers, json=payload)
    assert replay.json() == resolved.json()
    assert transport.calls == 1
    with psycopg.connect(route_state) as conn:
        # route_state 现在会重建 runtime_settings 单行（注册赠送的定价快照
        # 读它），这里改为 upsert 补上本用例需要的存储供应商与操作者。
        conn.execute(
            "INSERT INTO runtime_settings (id, max_generation_count_per_batch, "
            "max_concurrent_h3_tasks, active_storage_provider, updated_by_user_id) "
            "VALUES (1, 4, 2, 'cos', %s) "
            "ON CONFLICT (id) DO UPDATE SET active_storage_provider = 'cos', "
            "updated_by_user_id = EXCLUDED.updated_by_user_id",
            (user["user_id"],),
        )
    item = resolved.json()["item"]
    assert item["platform"] == "xiaohongshu"
    assert item["videoId"] == "66e012345678901234abcdef"
    headers["Idempotency-Key"] = resolved.json()["importIdempotencyKey"]
    task_body = {"platform": item["platform"], "videoId": item["videoId"], "purpose": "copy"}
    imported = client.post("/api/viral/videos/import-tasks", headers=headers, json=task_body)
    assert imported.status_code == 202, imported.text
    repeated = client.post("/api/viral/videos/import-tasks", headers=headers, json=task_body)
    assert repeated.json()["id"] == imported.json()["id"]
    with psycopg.connect(route_state) as raw:
        assert (
            run_worker_once(
                BusinessConnection.postgres(raw), worker_id="xhs-account-test", storage=storage
            )
            == 1
        )
    completed = client.get("/api/viral/import-tasks/" + imported.json()["id"], headers=headers)
    assert completed.json()["status"] == "SUCCEEDED", completed.text
    with psycopg.connect(route_state) as conn:
        assert conn.execute("SELECT count(*) FROM viral_import_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT owner_user_id FROM projects").fetchone()[0] == user["user_id"]
        assert (
            conn.execute(
                "SELECT project_id FROM assets WHERE id = %s", (completed.json()["sourceAssetId"],)
            ).fetchone()[0]
            == completed.json()["projectId"]
        )


@pytest.mark.parametrize("failure", ["lease", "verification", "storage"])
def test_link_media_failure_finishes_receipt_and_refunds(client, route_state, monkeypatch, failure):
    import app.viral_import_routes as routes
    from app.storage import FakeStorageAdapter, StorageBackendUnavailable
    from app.viral_link import ResolvedViralLink
    from app.viral_media_preparation import ViralMediaLeaseLost
    from app.viral_tikhub import ViralSourceError

    user = client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD}).json()
    session = _password_login(client).json()
    with psycopg.connect(route_state) as conn:
        conn.execute("DELETE FROM billing_tariffs WHERE service='link_resolution'")
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES('link_resolution',true,5,2)"
        )
        conn.execute("UPDATE wallets SET available_credits=20 WHERE user_id=%s", (user["user_id"],))

    class Resolver:
        calls = 0

        def resolve(self, url, *, purpose):
            self.calls += 1
            return ResolvedViralLink(
                platform="douyin",
                video_id="failed-media",
                title="video",
                author="",
                cover_url=None,
                video_url="https://cdn.example/video.mp4",
                audio_url=None,
                duration_ms=10000,
                source_description="",
            )

    class Pipeline:
        def __init__(self, **kwargs):
            pass

        def fetch(self, *args, **kwargs):
            raise {
                "lease": ViralMediaLeaseLost,
                "verification": ViralSourceError,
                "storage": StorageBackendUnavailable,
            }[failure]("private-token-must-not-leak")

    resolver = Resolver()
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    monkeypatch.setattr(
        routes, "get_media_storage", lambda conn: FakeStorageAdapter(provider="cos", bucket="test")
    )
    monkeypatch.setattr(routes, "ViralMediaPipeline", Pipeline)
    headers = {
        "Authorization": "Bearer " + session["session_token"],
        "Idempotency-Key": f"media-{failure}",
    }
    body = {"url": "https://v.douyin.com/test-media", "purpose": "replica"}
    result = client.post("/api/viral/link-resolutions", headers=headers, json=body)
    assert result.status_code == 503, result.text
    assert "private-token" not in result.text
    replay = client.post("/api/viral/link-resolutions", headers=headers, json=body)
    assert replay.json() == result.json() and resolver.calls == 1
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT status FROM viral_link_resolution_receipts WHERE owner_user_id=%s",
                (user["user_id"],),
            ).fetchone()[0]
            == "FAILED_SAFE"
        )
        assert conn.execute(
            "SELECT state,charged_credits FROM billing_operations WHERE user_id=%s",
            (user["user_id"],),
        ).fetchone() == ("FAILED", 0)
        assert (
            conn.execute(
                "SELECT available_credits FROM wallets WHERE user_id=%s", (user["user_id"],)
            ).fetchone()[0]
            == 20
        )


@pytest.mark.parametrize("operation", ["unbind", "revoke"])
def test_password_device_admin_revocation_and_signed_grants_are_isolated(
    client: TestClient, route_state: str, operation: str
) -> None:
    from datetime import UTC, datetime

    from fastapi import HTTPException

    from app.auth import authenticate_user
    from app.customer_auth import verify_session_context
    from app.customer_device_service import admin_unbind_device, revoke_device_credential
    from app.db_portable import BusinessConnection
    from app.media_routes import signed_asset_session_epoch, validate_signed_asset_grant

    user = client.post(REGISTER_PATH, json={"username": "alice", "password": VALID_PASSWORD}).json()
    first = _password_login(client).json()
    second = _password_login(client, device_fingerprint="password-device-two").json()
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin-test', 'admin-test', 'Admin', 'admin')"
        )
        raw.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES ('grant-project', %s, 'Grant')",
            (user["user_id"],),
        )
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES ('grant-asset', 'grant-project', "
            "'reference_video', 'cos://test/grant.mp4', %s, 8, 'video/mp4', %s)",
            ("0" * 64, user["user_id"]),
        )
    grants = []
    for session in (first, second):
        with psycopg.connect(route_state) as raw:
            conn = BusinessConnection.postgres(raw)
            conn.ctx = verify_session_context(
                raw, presentation_session_token=session["session_token"]
            )
            actor = authenticate_user(conn, user["user_id"])
            grant = signed_asset_session_epoch(conn, actor)
            assert grant == f"{session['session_id']}:{session['session_epoch']}"
            assert (
                validate_signed_asset_grant(
                    conn, user_id=user["user_id"], asset_id="grant-asset", session_epoch=grant
                )["id"]
                == "grant-asset"
            )
            grants.append(grant)
    assert grants[0] != grants[1]
    with psycopg.connect(route_state) as raw:
        fn = admin_unbind_device if operation == "unbind" else revoke_device_credential
        fn(
            raw,
            device_id=first["device_id"],
            admin_user_id="admin-test",
            reason="contract test",
            request_id="admin-device-test",
            server_now=datetime.now(UTC),
        )
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        with pytest.raises(HTTPException) as denied:
            validate_signed_asset_grant(
                conn, user_id=user["user_id"], asset_id="grant-asset", session_epoch=grants[0]
            )
        assert denied.value.status_code == 403
        assert (
            validate_signed_asset_grant(
                conn, user_id=user["user_id"], asset_id="grant-asset", session_epoch=grants[1]
            )["id"]
            == "grant-asset"
        )
        assert (
            raw.execute("SELECT activation_code_id FROM admin_device_events").fetchone()[0] is None
        )
    assert (
        client.post(
            "/api/customer/sessions/heartbeat",
            headers={"Authorization": "Bearer " + second["session_token"]},
        ).status_code
        == 200
    )
