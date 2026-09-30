"""注册赠送积分设置的 PG 契约套件。

钉住三件事：
- ``GET/PUT /api/control/settings/registration-bonus`` 的读写契约：种子默认
  0、范围校验、写契约（Idempotency-Key / confirm / reason）、幂等重放与
  冲突、审计快照（before/after + reason）、审计员只读。
- 配置行缺失时读侧 fail-closed（503），不展示假状态。
- :func:`grant_registration_bonus` 的落账形状：PAID 0 元 admin_adjustment 单
  + 同额 CHARGE 流水 + 钱包自增三行同事务；bonus=0 是彻底 no-op。注册端点
  把两者串起来的行为由 test_customer_registration.py 的注册路径覆盖。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-registration-bonus-minimum-48-bytes",
)

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.admin_auth_routes import AdminActor, get_admin_actor
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.registration_bonus_routes import grant_registration_bonus
from app.registration_bonus_routes import router as registration_bonus_router

BONUS_DB_NAME = "registration_bonus_settings_test"

_SETTINGS_PATH = "/api/control/settings/registration-bonus"
_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_REPLAY_HEADER = "X-Idempotent-Replay"

# 单行配置表引用 users（updated_by 外键），显式列入清理清单；种子行随后重建。
_RESET_TABLES = "audit_logs, users, registration_bonus_settings"


def _pg_dsn() -> str:
    return os.environ.get(
        "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
    )


def _actor(role: str) -> AdminActor:
    return AdminActor(
        user_id="admin_u",
        username="admin_u",
        display_name="Admin",
        role=role,
        is_super_admin=False,
        auth_method="password",
        session_id="sess-1",
        session_expires_at="2030-01-01T00:00:00Z",
        last_activity_at="2030-01-01T00:00:00Z",
    )


@pytest.fixture(scope="module")
def bonus_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(BONUS_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(BONUS_DB_NAME)


@pytest.fixture()
def seeded(bonus_dsn: str) -> str:
    """清库并重建最小依赖：一个 admin、一个 auditor、配置种子行。"""
    with psycopg.connect(bonus_dsn, autocommit=True) as conn:
        # append-only 审计触发器拒绝 TRUNCATE：按 P1-4 先例临时关触发器再清。
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {_RESET_TABLES} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) VALUES "
            "('admin_u', 'admin_u', 'Admin', 'admin', 1), "
            "('auditor_u', 'auditor_u', 'Auditor', 'auditor', 1)"
        )
        conn.execute(
            "INSERT INTO registration_bonus_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
        )
        # TRUNCATE users CASCADE 同样连坐 runtime_settings（updated_by_user_id
        # 外键引用 users）——grant 的定价快照读它，必须重建单行。
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen) "
            "VALUES (1, 4, 2, 1000, 10000, 1000)"
        )
    return bonus_dsn


@pytest.fixture()
def api(seeded: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, seeded)
    app = FastAPI()
    app.include_router(registration_bonus_router)
    app.dependency_overrides[get_admin_actor] = lambda: _actor("admin")
    with TestClient(app) as test_client:
        yield test_client
    close_pg_pool()


def _put_settings(api: TestClient, *, key: str | None = None, **overrides: object) -> object:
    body: dict[str, object] = {
        "confirm": True,
        "reason": "调整注册赠送",
        "bonus_credits": 100,
    }
    body.update(overrides)
    return api.put(
        _SETTINGS_PATH,
        headers={_IDEMPOTENCY_KEY_HEADER: key if key is not None else f"key-{uuid.uuid4()}"},
        json=body,
    )


# ---------------------------------------------------------------------------
# 读侧
# ---------------------------------------------------------------------------


def test_settings_route_returns_defaults(api: TestClient) -> None:
    response = api.get(_SETTINGS_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    # 迁移种子默认 0（关闭赠送）：升级瞬间注册行为不变。
    assert body["bonus_credits"] == 0
    assert body["updated_by_user_id"] is None
    assert body["updated_by_display_name"] is None
    assert body["updated_at"]  # 迁移默认 clock_timestamp 写入


def test_settings_row_missing_fails_closed(api: TestClient, seeded: str) -> None:
    with psycopg.connect(seeded, autocommit=True) as conn:
        conn.execute("DELETE FROM registration_bonus_settings")
    response = api.get(_SETTINGS_PATH)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "REGISTRATION_BONUS_SETTINGS_SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 写侧契约
# ---------------------------------------------------------------------------


def test_update_settings_round_trip(api: TestClient) -> None:
    updated = _put_settings(api, bonus_credits=30)
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["bonus_credits"] == 30
    assert body["updated_by_user_id"] == "admin_u"
    assert body["updated_by_display_name"] == "Admin"

    round_trip = api.get(_SETTINGS_PATH).json()
    assert round_trip["bonus_credits"] == 30
    assert round_trip["updated_by_user_id"] == "admin_u"


def test_update_settings_allows_zero_to_disable(api: TestClient) -> None:
    assert _put_settings(api, bonus_credits=50).status_code == 200
    disabled = _put_settings(api, bonus_credits=0)
    assert disabled.status_code == 200
    assert disabled.json()["bonus_credits"] == 0


def test_update_settings_requires_write_contract(api: TestClient) -> None:
    no_key = _put_settings(api, key="")
    assert no_key.status_code == 400
    assert no_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    no_confirm = _put_settings(api, confirm=False)
    assert no_confirm.status_code == 400
    assert no_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    no_reason = _put_settings(api, reason="   ")
    assert no_reason.status_code == 400
    assert no_reason.json()["detail"]["code"] == "REASON_REQUIRED"


def test_update_settings_replay_and_conflict(api: TestClient) -> None:
    key = f"key-{uuid.uuid4()}"
    first = _put_settings(api, key=key, bonus_credits=25)
    assert first.status_code == 200, first.text

    replayed = _put_settings(api, key=key, bonus_credits=25)
    assert replayed.status_code == 200
    assert replayed.headers.get(_REPLAY_HEADER) == "true"
    assert replayed.json()["bonus_credits"] == 25

    conflict = _put_settings(api, key=key, bonus_credits=35)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_update_settings_validates_ranges(api: TestClient) -> None:
    assert _put_settings(api, bonus_credits=-1).status_code == 422
    assert _put_settings(api, bonus_credits=2147483648).status_code == 422
    # StrictInt：字符串数字不接受，防止「30」悄悄被 Pydantic 强转。
    assert _put_settings(api, bonus_credits="30").status_code == 422


def test_update_settings_is_audited(api: TestClient, seeded: str) -> None:
    assert _put_settings(api, bonus_credits=20, reason="新客获客活动，注册送 20").status_code == 200
    with psycopg.connect(seeded) as conn:
        row = conn.execute(
            "SELECT actor_user_id, entity_type, entity_id, metadata_json FROM audit_logs "
            "WHERE action = 'registration_bonus.settings.update'"
        ).fetchone()
    assert row is not None
    assert row[0] == "admin_u"
    assert row[1] == "registration_bonus_settings"
    assert row[2] == "1"
    metadata = json.loads(row[3])
    assert metadata["reason"] == "新客获客活动，注册送 20"
    assert metadata["old"]["bonus_credits"] == 0
    assert metadata["new"]["bonus_credits"] == 20


def test_auditor_reads_but_cannot_write(seeded: str, monkeypatch: pytest.MonkeyPatch) -> None:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, seeded)
    app = FastAPI()
    app.include_router(registration_bonus_router)
    app.dependency_overrides[get_admin_actor] = lambda: _actor("auditor")
    with TestClient(app) as auditor_client:
        read = auditor_client.get(_SETTINGS_PATH)
        assert read.status_code == 200
        denied = _put_settings(auditor_client, bonus_credits=10)
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "AUDITOR_READ_ONLY"
    close_pg_pool()
    # 被拒的写不落配置：种子值保持 0。
    with psycopg.connect(seeded) as conn:
        credits = conn.execute(
            "SELECT bonus_credits FROM registration_bonus_settings WHERE id = 1"
        ).fetchone()[0]
    assert credits == 0


# ---------------------------------------------------------------------------
# 发放函数的落账形状（与注册端点的串联由 test_customer_registration.py 覆盖）
# ---------------------------------------------------------------------------


def _seed_wallet_owner(dsn: str) -> str:
    user_id = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'customer')",
            (user_id, f"u-{user_id[:8]}", "WalletOwner"),
        )
        conn.execute("INSERT INTO wallets (user_id) VALUES (%s)", (user_id,))
    return user_id


def test_grant_registration_bonus_charges_wallet_and_order(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = _seed_wallet_owner(seeded)
    with psycopg.connect(seeded, autocommit=True) as conn:
        conn.execute("UPDATE registration_bonus_settings SET bonus_credits = 12 WHERE id = 1")
    # pg_transaction 读环境变量取池：本用例不经过 api 夹具，需自行指向专用库。
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, seeded)
    with pg_transaction() as conn:
        granted = grant_registration_bonus(conn, user_id)
    assert granted == 12
    with psycopg.connect(seeded) as conn:
        credits = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()[0]
        order = conn.execute(
            "SELECT provider, status, amount_fen, credits, merchant_order_no "
            "FROM recharge_orders WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        charge = conn.execute(
            "SELECT type, available_delta, reserved_delta, idempotency_key "
            "FROM wallet_transactions WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    assert credits == 12
    assert order == ("admin_adjustment", "PAID", 0, 12, f"REGBONUS-{user_id}")
    assert charge[0] == "CHARGE"
    assert charge[1] == 12
    assert charge[2] == 0
    assert charge[3].startswith("registration_bonus:charge:")


def test_grant_registration_bonus_zero_is_a_no_op(
    seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = _seed_wallet_owner(seeded)
    # 种子默认 0（seeded 夹具未改），不需要再写一次。
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, seeded)
    with pg_transaction() as conn:
        granted = grant_registration_bonus(conn, user_id)
    assert granted == 0
    with psycopg.connect(seeded) as conn:
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
