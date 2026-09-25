"""子账号月度额度：表结构、聚合口径、超限强制与 API（CUSTOMER-CENTER-V2 Phase 3a）.

``sub_account_quotas``（迁移 20260921T1200）给子账号设「每月最多消耗积分」，
无行 = 不限；当月用量不落库，实时聚合 ``wallet_transactions``
（``-SUM(available_delta)``，白名单 RESERVE/RELEASE，在途计入、结算退回即时
释放，SETTLE 不参与）。本文件按四层锁住这份契约：

- **表结构**（自建 ``t_sub_account_quota`` 专库，raw admin CREATE/DROP，同
  ``test_sub_account_schema`` 模式）：PK = user_id、FK CASCADE、CHECK >= 0、
  text 默认值，以及有额度行时的 fail-closed downgrade 守卫；
- **聚合口径**：RESERVE 计入、RELEASE 退回、SETTLE 忽略、他人流水不算，
  上海自然月边界（月初整点含、前一秒不含）与 text 时间戳两种格式的归一；
  带 operation 的流水按操作发起月归属（跨月 RELEASE 不放宽次月额度）；
- **强制**：``enforce_sub_account_quota`` 无行直通、additional <= 0 提前返回、
  ``已用 + 本次 == 额度`` 放行、超一即 403 ``SUB_ACCOUNT_QUOTA_EXCEEDED``；
- **API + 端到端**：PUT quota 设置/更新/清除与 422/404/403 围栏、列表/改名/
  个人中心的额度字段，以及 ``accept_operation`` 在真实钱包上的超限 403、
  结算退回释放额度、母账号直通。

路线用例复用 ``test_customer_registration`` 的共享迁移库（``route_state``）；
专库名不入 ``pg_test_kit.RECORDED_TEST_DATABASES``（该 allowlist 只保护
kit 自己的 ``create_test_database``）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import require_pg_or_explicit_skip
from test_customer_registration import (
    client as registration_client,  # noqa: F401
)
from test_customer_registration import (
    registration_dsn,  # noqa: F401
    route_state,  # noqa: F401
)

from app.admin_dates import SHANGHAI
from app.customer_sub_account_routes import router as customer_sub_account_router
from app.db_portable import BusinessConnection
from app.sub_account_quota import (
    MAX_MONTHLY_QUOTA_CREDITS,
    enforce_sub_account_quota,
    read_monthly_quota,
    read_quota_used,
    read_quota_used_map,
    shanghai_month_start_iso,
)
from app.usage_billing import accept_operation, finish_operation

SUB_ACCOUNTS_PATH = "/api/customer/sub-accounts"
PROFILE_PATH = "/api/customer/profile"

MASTER_PASSWORD = "master-pass-9"
SUB_PASSWORD = "sub-pass-9"

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
QUOTA_TABLE = "sub_account_quotas"
QUOTA_DB = "t_sub_account_quota"

# 链尾：本分支的 20260923T1200_admin_refund_adjustment 按手册 §3 重挂于
# main 链尾（20260922T2200_material_preference_tags）之上；其后依次叠加
# 20260923T1800 部署垫片、20260924T0000 交易号唯一索引与
# 20260924T0100 口播提交时刻列与 20260924T0200 口播隐藏偏好表，故链尾为该值。
_HEAD_REVISION = "20260926T0000_viral_homepage_rank"
# _PRIOR_REVISION 是本迁移自身的 down_revision，用于降级断言。
# 合并 main 后 1200 重挂到 20260922T1200_recharge_packages（手册 §3）；Phase 3b
# permissions 与其后的 analysis 三迁移（failure_diagnostic / request_id /
# task_attempts）追加为链尾。降级到它只回退本迁移（及清零后的 permissions 空表），
# 不触碰其下的 sub_accounts 守卫。
_PRIOR_REVISION = "20260922T1200_recharge_packages"


@pytest.fixture()
def client(registration_client, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    # The wallet read decrypts the customer billing settings through
    # SettingsRepository, so the Fernet root key joins the registration env.
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    registration_client.app.include_router(customer_sub_account_router)
    return registration_client


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip(_pg_dsn())


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _drop_database(db_name: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def _create_database(db_name: str) -> str:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db_name}"')
    return _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"


def _alembic_config(dsn: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    return config


def _upgrade(dsn: str, target: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(dsn), target)


def _downgrade(dsn: str, target: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(dsn), target)


@pytest.fixture()
def quota_dsn() -> str:
    """Fresh database at head: one MASTER ('m1') and three SUBs ('s1'..'s3').

    Each test owns the database and drops it on teardown, so rows never leak
    between cases; tests seed their own quota rows / ledger chains on top.
    """
    dsn = _create_database(QUOTA_DB)
    _upgrade(dsn, "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES ('m1', 'm1', 'Master 1')"
        )
        for sub_id in ("s1", "s2", "s3"):
            conn.execute(
                "INSERT INTO users (id, username, display_name, parent_user_id, account_type) "
                "VALUES (%s, %s, %s, 'm1', 'SUB')",
                (sub_id, sub_id, sub_id),
            )
    try:
        yield dsn
    finally:
        _drop_database(QUOTA_DB)


def _column(conn: psycopg.Connection, table: str, name: str):  # type: ignore[no-untyped-def]
    """(data_type, is_nullable, column_default) for one column, or None if absent."""
    return conn.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, name),
    ).fetchone()


def _insert_quota(conn: psycopg.Connection, user_id: str, credits: int) -> None:
    conn.execute(
        "INSERT INTO sub_account_quotas (user_id, monthly_credits) VALUES (%s, %s)",
        (user_id, credits),
    )


def _seed_task(conn: psycopg.Connection, *, owner_id: str, seq: str) -> str:
    """Seed projects→generation_batches→generation_tasks for ``owner_id``.

    ``wallet_transactions.task_id`` is FK→``generation_tasks.id`` (022), so a
    ledger row needs this chain to exist first. Returns the task id.
    """
    project_id, batch_id, task_id = f"p-{seq}", f"b-{seq}", f"t-{seq}"
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        (project_id, owner_id, project_id),
    )
    conn.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json) "
        "VALUES (%s, %s, %s, %s, %s, '{}')",
        (batch_id, project_id, owner_id, f"k-{seq}", f"h-{seq}"),
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, provider, model) "
        "VALUES (%s, %s, 'apilio', 'h3')",
        (task_id, batch_id),
    )
    return task_id


def _ledger_row(
    conn: psycopg.Connection,
    *,
    tx_id: str,
    owner_id: str,
    actor_id: str,
    task_id: str,
    kind: str,
    credits: int,
    idem: str,
    created_at: str | None = None,
    operation_id: str | None = None,
) -> None:
    """Append one ``ck_wallet_transactions_shape``-legal wallet row.

    RESERVE 预扣 ``(-c, +c)``；SETTLE 结算 ``(0, -c)``（available 恒 0，聚合
    忽略）；RELEASE 退回 ``(+c, -c)``。``actor_user_id`` 是聚合口径的键。
    ``operation_id`` 把流水挂到计费单上：月份归属随之锚定操作发起月。
    """
    available, reserved = {
        "RESERVE": (-credits, credits),
        "SETTLE": (0, -credits),
        "RELEASE": (credits, -credits),
    }[kind]
    columns = (
        "id, user_id, type, available_delta, reserved_delta, task_id, "
        "billing_round, idempotency_key, actor_user_id"
    )
    values: list[object] = [
        tx_id,
        owner_id,
        kind,
        available,
        reserved,
        task_id,
        1,
        idem,
        actor_id,
    ]
    if operation_id is not None:
        columns += ", billing_operation_id"
        values.append(operation_id)
    if created_at is not None:
        columns += ", created_at"
        values.append(created_at)
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(f"INSERT INTO wallet_transactions ({columns}) VALUES ({placeholders})", values)


def _seed_operation(
    conn: psycopg.Connection,
    *,
    op_id: str,
    owner_id: str,
    credits: int,
    created_at: str,
) -> None:
    """Seed one ``billing_operations`` row — the month-attribution anchor.

    ``created_at``（timestamptz）是操作发起时刻：挂在该 operation 上的
    RESERVE/RELEASE 一律按它归属月份（跨月退回调整原月）。
    """
    conn.execute(
        "INSERT INTO billing_operations (id, user_id, service, module, source_id, "
        "request_fingerprint, pricing_snapshot_json, unit, budget_units, "
        "reserved_credits, created_at) "
        "VALUES (%s, %s, 'asr', 'analysis', %s, '', '{}', 'second', 2, %s, %s)",
        (op_id, owner_id, op_id, credits, created_at),
    )


def _fingerprint() -> str:
    return "fp-" + uuid4().hex + uuid4().hex


def _login(client, username: str, password: str):
    return client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "username": username,
            "password": password,
            "device_fingerprint": _fingerprint(),
        },
    )


def _master_session(client, username: str):
    """Register a master and log it in; returns (headers, login body)."""
    registered = client.post(
        "/api/customer/register", json={"username": username, "password": MASTER_PASSWORD}
    )
    assert registered.status_code == 201, registered.text
    login = _login(client, username, MASTER_PASSWORD)
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["session_token"]}, login.json()


def _sub_session(client, username: str, password: str = SUB_PASSWORD):
    """Log a sub-account in; returns (headers, login body)."""
    login = _login(client, username, password)
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["session_token"]}, login.json()


def _create_sub(
    client,
    headers,
    *,
    username: str,
    monthly_quota_credits: int | None = None,
    password: str | None = SUB_PASSWORD,
):
    body: dict[str, object] = {"username": username, "display_name": "配额子账号"}
    if password is not None:
        body["password"] = password
    if monthly_quota_credits is not None:
        body["monthly_quota_credits"] = monthly_quota_credits
    return client.post(SUB_ACCOUNTS_PATH, headers=headers, json=body)


def _detail_code(response) -> str:
    return response.json()["detail"]["code"]


# ---------------------------------------------------------------------------
# 20260921T1200: table shape, row constraints, cascade, downgrade guard
# ---------------------------------------------------------------------------


def test_quota_table_columns_constraints_and_defaults(quota_dsn: str) -> None:
    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _HEAD_REVISION

        user_id = _column(conn, QUOTA_TABLE, "user_id")
        assert user_id is not None, "sub_account_quotas.user_id missing at head"
        assert user_id[0] == "text" and user_id[1] == "NO"

        monthly = _column(conn, QUOTA_TABLE, "monthly_credits")
        assert monthly is not None, "sub_account_quotas.monthly_credits missing at head"
        assert monthly[0] == "bigint" and monthly[1] == "NO"

        for name in ("created_at", "updated_at"):
            column = _column(conn, QUOTA_TABLE, name)
            assert column is not None, f"sub_account_quotas.{name} missing at head"
            assert column[0] == "text" and column[1] == "NO"
            assert column[2] is not None and "CURRENT_TIMESTAMP" in column[2]

        primary = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'sub_account_quotas'::regclass AND contype = 'p'"
        ).fetchone()
        assert primary is not None and "user_id" in primary[0]

        foreign = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'sub_account_quotas'::regclass AND contype = 'f'"
        ).fetchone()
        assert foreign is not None, "sub_account_quotas.user_id FK missing at head"
        assert "users(id)" in foreign[0] and "CASCADE" in foreign[0]

        checks = [
            row[0]
            for row in conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'sub_account_quotas'::regclass AND contype = 'c'"
            )
        ]
        assert any("monthly_credits >= 0" in definition for definition in checks), checks


def test_quota_row_rejects_bad_shapes_and_cascades_with_the_sub(quota_dsn: str) -> None:
    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        _insert_quota(conn, "s1", 500)
        # PK：一行一账号（PUT 走 ON CONFLICT 更新，不依赖重复插入）。
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_quota(conn, "s1", 100)
        # ck_sub_account_quotas_monthly_credits：负数被拒。
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_quota(conn, "s2", -1)
        # FK：额度行只能挂在真实用户上。
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_quota(conn, "ghost", 100)

        # ON DELETE CASCADE：子账号被真删时额度行随行清理。
        conn.execute("DELETE FROM users WHERE id = 's1'")
        assert conn.execute(f"SELECT count(*) FROM {QUOTA_TABLE}").fetchone()[0] == 0

        # 母账号删除级联到子账号，再级联到额度行。
        _insert_quota(conn, "s3", 700)
        conn.execute("DELETE FROM users WHERE id = 'm1'")
        assert conn.execute(f"SELECT count(*) FROM {QUOTA_TABLE}").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_downgrade_guard_refuses_while_quota_rows_exist(quota_dsn: str) -> None:
    """Quota rows present -> RuntimeError and the whole chain rolls back to head."""
    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        _insert_quota(conn, "s1", 100)

    with pytest.raises(RuntimeError, match="sub-account quotas exist"):
        _downgrade(quota_dsn, _PRIOR_REVISION)

    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        # The downgrade runs in one transaction, so the guard rolls it all back.
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _HEAD_REVISION, "failed downgrade must leave the chain at head"
        assert _column(conn, QUOTA_TABLE, "user_id") is not None

        conn.execute(f"DELETE FROM {QUOTA_TABLE}")

    _downgrade(quota_dsn, _PRIOR_REVISION)

    with psycopg.connect(quota_dsn) as conn:
        versions = {row[0] for row in conn.execute("SELECT version_num FROM alembic_version")}
        assert versions == {_PRIOR_REVISION}
        assert _column(conn, QUOTA_TABLE, "user_id") is None


# ---------------------------------------------------------------------------
# Usage aggregation: -SUM(available_delta) over the RESERVE/RELEASE whitelist
# ---------------------------------------------------------------------------


def test_month_start_is_the_shanghai_calendar_month_start() -> None:
    now = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)  # 上海 11:00
    start = datetime.fromisoformat(shanghai_month_start_iso(now))
    assert start.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == "2026-09-01 00:00"
    # 上海 9 月 1 日 00:30（= UTC 8 月 31 日 16:30）仍属 9 月。
    early = datetime(2026, 9, 1, 0, 30, tzinfo=SHANGHAI)
    early_start = datetime.fromisoformat(shanghai_month_start_iso(early))
    assert early_start.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == "2026-09-01 00:00"
    # 上海 8 月 31 日 23:30 属 8 月：月初 = 7 月 31 日 16:00 UTC。
    late = datetime(2026, 8, 31, 23, 30, tzinfo=SHANGHAI)
    assert shanghai_month_start_iso(late) == "2026-07-31T16:00:00+00:00"


def test_usage_aggregation_counts_reserve_minus_release_and_ignores_settle(
    quota_dsn: str,
) -> None:
    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        assert read_monthly_quota(conn, "s1") is None, "无行 = 不限"
        _insert_quota(conn, "s1", 42)
        assert read_monthly_quota(conn, "s1") == 42

        task_a = _seed_task(conn, owner_id="m1", seq="agg-a")
        task_b = _seed_task(conn, owner_id="m1", seq="agg-b")
        # 整单结清：SETTLE 的 available_delta 恒 0，占用只由 RESERVE 计入。
        _ledger_row(
            conn,
            tx_id="tx-agg-a-reserve",
            owner_id="m1",
            actor_id="s1",
            task_id=task_a,
            kind="RESERVE",
            credits=7,
            idem="idem-agg-a1",
        )
        _ledger_row(
            conn,
            tx_id="tx-agg-a-settle",
            owner_id="m1",
            actor_id="s1",
            task_id=task_a,
            kind="SETTLE",
            credits=7,
            idem="idem-agg-a2",
        )
        # 预扣 5、结算退回 2：实际只占 3。
        _ledger_row(
            conn,
            tx_id="tx-agg-b-reserve",
            owner_id="m1",
            actor_id="s1",
            task_id=task_b,
            kind="RESERVE",
            credits=5,
            idem="idem-agg-b1",
        )
        _ledger_row(
            conn,
            tx_id="tx-agg-b-release",
            owner_id="m1",
            actor_id="s1",
            task_id=task_b,
            kind="RELEASE",
            credits=2,
            idem="idem-agg-b2",
        )

        assert read_quota_used(conn, "s1") == 10
        assert read_quota_used(conn, "s2") == 0, "无流水即零占用"
        assert read_quota_used(conn, "m1") == 0, "钱包主人的流水不算在任何子账号头上"

        assert read_quota_used_map(conn, ["s1", "s2", "ghost"]) == {
            "s1": 10,
            "s2": 0,
            "ghost": 0,
        }
        assert read_quota_used_map(conn, []) == {}


def test_month_boundary_and_text_timestamp_normalization(quota_dsn: str) -> None:
    month_start = shanghai_month_start_iso()
    just_before = (datetime.fromisoformat(month_start) - timedelta(seconds=1)).isoformat()
    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        task_in = _seed_task(conn, owner_id="m1", seq="mb-in")
        _ledger_row(
            conn,
            tx_id="tx-mb-in",
            owner_id="m1",
            actor_id="s1",
            task_id=task_in,
            kind="RESERVE",
            credits=3,
            idem="idem-mb-in",
            created_at=month_start,
        )
        assert read_quota_used(conn, "s1") == 3, "月初整点（含）计入当月"

        task_before = _seed_task(conn, owner_id="m1", seq="mb-before")
        _ledger_row(
            conn,
            tx_id="tx-mb-before",
            owner_id="m1",
            actor_id="s1",
            task_id=task_before,
            kind="RESERVE",
            credits=9,
            idem="idem-mb-before",
            created_at=just_before,
        )
        assert read_quota_used(conn, "s1") == 3, "月初前一秒属上月，不计入"

        # 空格格式（老 SQLite lane 的 text 时间戳）：走 ELSE 分支按 UTC 解释，
        # 跨月同样不计入。
        task_space_old = _seed_task(conn, owner_id="m1", seq="mb-space-old")
        _ledger_row(
            conn,
            tx_id="tx-mb-space-old",
            owner_id="m1",
            actor_id="s1",
            task_id=task_space_old,
            kind="RESERVE",
            credits=9,
            idem="idem-mb-space-old",
            created_at="2020-01-01 00:00:00",
        )
        assert read_quota_used(conn, "s1") == 3, "空格格式跨月同样不计入"

        # 空格格式但落在本月：必须计入。
        task_space_now = _seed_task(conn, owner_id="m1", seq="mb-space-now")
        _ledger_row(
            conn,
            tx_id="tx-mb-space-now",
            owner_id="m1",
            actor_id="s1",
            task_id=task_space_now,
            kind="RESERVE",
            credits=2,
            idem="idem-mb-space-now",
            created_at=datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S"),
        )
        assert read_quota_used(conn, "s1") == 5, "空格格式在月内同样计入"


def test_cross_month_release_adjusts_the_operation_month_not_the_next(quota_dsn: str) -> None:
    """月份归属 = 操作发起月：跨月 RELEASE 退回原月，不放宽次月额度（评审 P1）."""
    current_start = shanghai_month_start_iso()
    now_shanghai = datetime.now(tz=SHANGHAI)
    prev_year, prev_month = (
        (now_shanghai.year - 1, 12)
        if now_shanghai.month == 1
        else (now_shanghai.year, now_shanghai.month - 1)
    )
    prev_start = datetime(prev_year, prev_month, 1, tzinfo=SHANGHAI).astimezone(UTC).isoformat()
    prev_instant = (datetime.fromisoformat(prev_start) + timedelta(days=1)).isoformat()
    current_instant = (datetime.fromisoformat(current_start) + timedelta(days=1)).isoformat()

    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        _insert_quota(conn, "s1", 100)
        task_prev = _seed_task(conn, owner_id="m1", seq="xm-prev")
        task_now = _seed_task(conn, owner_id="m1", seq="xm-now")

        # 上月操作预扣 100（上月满额），次月才整单退回。
        _seed_operation(
            conn, op_id="op-xm-prev", owner_id="m1", credits=100, created_at=prev_instant
        )
        _ledger_row(
            conn,
            tx_id="tx-xm-prev-reserve",
            owner_id="m1",
            actor_id="s1",
            task_id=task_prev,
            kind="RESERVE",
            credits=100,
            idem="idem-xm-1",
            created_at=prev_instant,
            operation_id="op-xm-prev",
        )
        _ledger_row(
            conn,
            tx_id="tx-xm-prev-release",
            owner_id="m1",
            actor_id="s1",
            task_id=task_prev,
            kind="RELEASE",
            credits=100,
            idem="idem-xm-2",
            created_at=current_instant,
            operation_id="op-xm-prev",
        )

        # 次月视角：退回不得在次月形成 -100（旧实现按流水自身时间过滤会放宽到 2×）。
        assert read_quota_used(conn, "s1") == 0
        assert read_quota_used_map(conn, ["s1"]) == {"s1": 0}
        # 原月视角：预扣与退回都对冲在原月（净 0），退回没有丢失。
        assert read_quota_used(conn, "s1", month_start_iso=prev_start) == 0

        # 强制层：次月可用额度就是 100，第 101 积分被拒（旧实现会放行）。
        enforce_sub_account_quota(conn, actor_id="s1", additional_credits=100)
        with pytest.raises(HTTPException) as blocked:
            enforce_sub_account_quota(conn, actor_id="s1", additional_credits=101)
        assert blocked.value.status_code == 403
        assert blocked.value.detail["code"] == "SUB_ACCOUNT_QUOTA_EXCEEDED"
        assert "已用 0" in blocked.value.detail["message"]

        # 月内退回仍即时释放：本月操作预扣 5、退回 2 → 本月占用 3。
        _seed_operation(
            conn, op_id="op-xm-now", owner_id="m1", credits=5, created_at=current_instant
        )
        _ledger_row(
            conn,
            tx_id="tx-xm-now-reserve",
            owner_id="m1",
            actor_id="s1",
            task_id=task_now,
            kind="RESERVE",
            credits=5,
            idem="idem-xm-3",
            created_at=current_instant,
            operation_id="op-xm-now",
        )
        _ledger_row(
            conn,
            tx_id="tx-xm-now-release",
            owner_id="m1",
            actor_id="s1",
            task_id=task_now,
            kind="RELEASE",
            credits=2,
            idem="idem-xm-4",
            created_at=current_instant,
            operation_id="op-xm-now",
        )
        assert read_quota_used(conn, "s1") == 3


# ---------------------------------------------------------------------------
# Enforcement: unlimited / early return / exact boundary / one-credit-over 403
# ---------------------------------------------------------------------------


def test_enforce_is_a_noop_without_a_row_or_without_new_spend(quota_dsn: str) -> None:
    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        # 无额度行 = 不限：任意高的预扣都直通。
        enforce_sub_account_quota(conn, actor_id="s2", additional_credits=10**9)

        _insert_quota(conn, "s1", 10)
        task_id = _seed_task(conn, owner_id="m1", seq="enf-noop")
        _ledger_row(
            conn,
            tx_id="tx-enf-noop",
            owner_id="m1",
            actor_id="s1",
            task_id=task_id,
            kind="RESERVE",
            credits=10,
            idem="idem-enf-noop",
        )
        # additional <= 0 提前返回：已站在额度上限上也不抛（退回/结算路径）。
        enforce_sub_account_quota(conn, actor_id="s1", additional_credits=0)
        enforce_sub_account_quota(conn, actor_id="s1", additional_credits=-5)


def test_enforce_allows_the_exact_boundary_and_blocks_the_next_credit(quota_dsn: str) -> None:
    with psycopg.connect(quota_dsn, autocommit=True) as conn:
        _insert_quota(conn, "s1", 10)
        task_id = _seed_task(conn, owner_id="m1", seq="enf-bound")
        _ledger_row(
            conn,
            tx_id="tx-enf-bound",
            owner_id="m1",
            actor_id="s1",
            task_id=task_id,
            kind="RESERVE",
            credits=4,
            idem="idem-enf-bound",
        )

        # 已用 4 + 本次 6 == 额度 10：放行。
        enforce_sub_account_quota(conn, actor_id="s1", additional_credits=6)

        with pytest.raises(HTTPException) as blocked:
            enforce_sub_account_quota(conn, actor_id="s1", additional_credits=7)
        assert blocked.value.status_code == 403
        assert blocked.value.detail["code"] == "SUB_ACCOUNT_QUOTA_EXCEEDED"
        assert "已用 4" in blocked.value.detail["message"]
        assert "限额 10" in blocked.value.detail["message"]

        # 额度为 0：任何正向预扣都被拒（下界而不是禁用）。
        _insert_quota(conn, "s3", 0)
        with pytest.raises(HTTPException):
            enforce_sub_account_quota(conn, actor_id="s3", additional_credits=1)
        enforce_sub_account_quota(conn, actor_id="s3", additional_credits=0)


# ---------------------------------------------------------------------------
# API: PUT quota set/update/clear + validation; list / patch / profile fields
# ---------------------------------------------------------------------------


def test_put_quota_sets_updates_clears_and_validates(client, route_state: str) -> None:
    master_headers, _ = _master_session(client, "quota_master_01")
    created = _create_sub(
        client, master_headers, username="quota_sub_01", monthly_quota_credits=500
    )
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["monthly_quota_credits"] == 500
    assert sub["quota_used_credits"] == 0
    assert sub["quota_remaining_credits"] == 500
    quota_path = f"{SUB_ACCOUNTS_PATH}/{sub['id']}/quota"
    with psycopg.connect(route_state) as conn:
        stored = conn.execute(
            f"SELECT monthly_credits FROM {QUOTA_TABLE} WHERE user_id = %s", (sub["id"],)
        ).fetchone()
    assert stored == (500,)

    updated = client.put(quota_path, headers=master_headers, json={"monthly_quota_credits": 250})
    assert updated.status_code == 200, updated.text
    assert updated.json()["monthly_quota_credits"] == 250
    assert updated.json()["quota_remaining_credits"] == 250
    assert updated.json()["quota_used_credits"] == 0

    cleared = client.put(quota_path, headers=master_headers, json={"monthly_quota_credits": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["monthly_quota_credits"] is None
    assert cleared.json()["quota_remaining_credits"] is None
    with psycopg.connect(route_state) as conn:
        leftover = conn.execute(
            f"SELECT 1 FROM {QUOTA_TABLE} WHERE user_id = %s", (sub["id"],)
        ).fetchone()
        audits = conn.execute(
            "SELECT count(*) FROM audit_logs WHERE entity_id = %s "
            "AND action = 'customer.sub_account.quota.set'",
            (sub["id"],),
        ).fetchone()
    assert leftover is None, "显式 null 必须删除额度行（恢复不限）"
    assert audits == (2,), "两次成功的设置各留一条审计；校验失败的不留"

    for bad in (
        {},  # 必填：缺键是客户端 bug，绝不当静默清除
        {"monthly_quota_credits": -1},
        {"monthly_quota_credits": MAX_MONTHLY_QUOTA_CREDITS + 1},
        {"monthly_quota_credits": 5, "unexpected": True},
    ):
        rejected = client.put(quota_path, headers=master_headers, json=bad)
        assert rejected.status_code == 422, (bad, rejected.text)
    with psycopg.connect(route_state) as conn:
        audits_after = conn.execute(
            "SELECT count(*) FROM audit_logs WHERE entity_id = %s "
            "AND action = 'customer.sub_account.quota.set'",
            (sub["id"],),
        ).fetchone()
    assert audits_after == (2,)


def test_quota_endpoint_fences_master_and_ownership(client) -> None:
    master_a_headers, master_a = _master_session(client, "quota_master_a")
    master_b_headers, _ = _master_session(client, "quota_master_b")
    b_sub = _create_sub(client, master_b_headers, username="quota_b_worker").json()
    a_sub = _create_sub(client, master_a_headers, username="quota_a_worker").json()
    body = {"monthly_quota_credits": 100}

    # 异主 → 与其它子账号端点共用的单一 404（不泄露存在性）。
    foreign = client.put(
        f"{SUB_ACCOUNTS_PATH}/{b_sub['id']}/quota", headers=master_a_headers, json=body
    )
    assert foreign.status_code == 404
    assert _detail_code(foreign) == "SUB_ACCOUNT_NOT_FOUND"

    unknown = client.put(
        f"{SUB_ACCOUNTS_PATH}/sub-does-not-exist/quota", headers=master_a_headers, json=body
    )
    assert unknown.status_code == 404
    assert _detail_code(unknown) == "SUB_ACCOUNT_NOT_FOUND"

    # 母账号改自己（不是子账号行）→ 同一个 404。
    self_put = client.put(
        f"{SUB_ACCOUNTS_PATH}/{master_a['user_id']}/quota",
        headers=master_a_headers,
        json=body,
    )
    assert self_put.status_code == 404, self_put.text

    # 子账号会话 → 仅限母账号的管理通道 403。
    sub_headers, _ = _sub_session(client, "quota_a_worker")
    refused = client.put(f"{SUB_ACCOUNTS_PATH}/{a_sub['id']}/quota", headers=sub_headers, json=body)
    assert refused.status_code == 403
    assert _detail_code(refused) == "MASTER_ACCOUNT_REQUIRED"
    assert client.put(f"{SUB_ACCOUNTS_PATH}/{a_sub['id']}/quota", json=body).status_code == 401

    # 全部被拒后没有任何额度行落库。
    listing = client.get(SUB_ACCOUNTS_PATH, headers=master_b_headers)
    assert listing.json()["sub_accounts"][0]["monthly_quota_credits"] is None


def test_list_patch_and_profile_report_quota_and_month_usage(client, route_state: str) -> None:
    master_headers, master = _master_session(client, "quota_master_03")
    capped = _create_sub(
        client, master_headers, username="quota_capped", monthly_quota_credits=1000
    ).json()
    uncapped = _create_sub(client, master_headers, username="quota_free").json()

    with psycopg.connect(route_state, autocommit=True) as conn:
        task_id = _seed_task(conn, owner_id=master["user_id"], seq="sq-api-1")
        _ledger_row(
            conn,
            tx_id="tx-sq-api-1",
            owner_id=master["user_id"],
            actor_id=capped["id"],
            task_id=task_id,
            kind="RESERVE",
            credits=1200,
            idem="idem-sq-api-1",
        )

    listing = client.get(SUB_ACCOUNTS_PATH, headers=master_headers)
    assert listing.status_code == 200, listing.text
    by_id = {item["id"]: item for item in listing.json()["sub_accounts"]}
    assert by_id[capped["id"]]["monthly_quota_credits"] == 1000
    assert by_id[capped["id"]]["quota_used_credits"] == 1200
    assert by_id[capped["id"]]["quota_remaining_credits"] == 0, "超额时剩余钳到 0 而非负数"
    assert by_id[uncapped["id"]]["monthly_quota_credits"] is None
    assert by_id[uncapped["id"]]["quota_used_credits"] == 0
    assert by_id[uncapped["id"]]["quota_remaining_credits"] is None

    renamed = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{capped['id']}",
        headers=master_headers,
        json={"display_name": "配额助理"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["monthly_quota_credits"] == 1000
    assert renamed.json()["quota_used_credits"] == 1200

    capped_headers, _ = _sub_session(client, "quota_capped")
    capped_profile = client.get(PROFILE_PATH, headers=capped_headers)
    assert capped_profile.status_code == 200, capped_profile.text
    assert capped_profile.json()["monthly_quota_credits"] == 1000
    assert capped_profile.json()["quota_used_credits"] == 1200

    free_headers, _ = _sub_session(client, "quota_free")
    free_profile = client.get(PROFILE_PATH, headers=free_headers)
    assert free_profile.json()["monthly_quota_credits"] is None, "无行 = 不限"
    assert free_profile.json()["quota_used_credits"] == 0

    master_profile = client.get(PROFILE_PATH, headers=master_headers)
    assert master_profile.json()["monthly_quota_credits"] is None
    assert master_profile.json()["quota_used_credits"] is None, "母账号没有额度视图"


# ---------------------------------------------------------------------------
# End to end: accept_operation over the real shared wallet
# ---------------------------------------------------------------------------


def test_accept_operation_enforces_quota_and_release_frees_room(client, route_state: str) -> None:
    master_headers, master = _master_session(client, "quota_master_04")
    sub = _create_sub(
        client, master_headers, username="quota_spender", monthly_quota_credits=10
    ).json()
    sub_id = sub["id"]

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)

        def wallet_state() -> tuple[int, int]:
            # BusinessConnection 会把行工厂换成 _NamedRow，读回来先归一成 tuple。
            row = raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
                (master["user_id"],),
            ).fetchone()
            return int(row[0]), int(row[1])

        updated = raw.execute(
            "UPDATE wallets SET available_credits = 100, reserved_credits = 0 WHERE user_id = %s",
            (master["user_id"],),
        )
        assert updated.rowcount == 1, "注册即建钱包行"
        raw.execute(
            "INSERT INTO billing_tariffs(service, enabled, unit_credits, unit_cost_fen) "
            "VALUES ('asr', true, 4, 1)"
        )
        _seed_task(raw, owner_id=master["user_id"], seq="sq-e2e")

        # 2 秒 × 4 积分 = 8：额度 10 内，预扣落在母账号钱包上（actor = 子账号）。
        op1 = accept_operation(conn, user_id=sub_id, service="asr", source_id="sq-e2e-1", units=2)
        wallet = wallet_state()
        assert wallet == (92, 8), wallet

        # 再预扣 4：已用 8 + 4 > 10 → 403，且不碰钱包、不建计费单。
        with pytest.raises(HTTPException) as blocked:
            accept_operation(conn, user_id=sub_id, service="asr", source_id="sq-e2e-2", units=1)
        assert blocked.value.status_code == 403
        assert blocked.value.detail["code"] == "SUB_ACCOUNT_QUOTA_EXCEEDED"
        wallet = wallet_state()
        assert wallet == (92, 8), "被拒的预扣不得产生任何钱包变化"
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id = %s", (sub_id,)
            ).fetchone()[0]
            == 1
        )

        # 结算退回即时释放：按 1 秒结算，退回 4、整单预扣清零 → 已用 4。
        finish_operation(conn, operation_id=op1, units=1, succeeded=True)
        wallet = wallet_state()
        assert wallet == (96, 0), wallet

        # 退回后的 4 积分空位可以继续用；已用 4 + 新预扣 4 = 8 ≤ 10。
        accept_operation(conn, user_id=sub_id, service="asr", source_id="sq-e2e-3", units=1)
        wallet = wallet_state()
        assert wallet == (92, 4), wallet

        # 在途计入：已结算 4 + 在途 4 + 本次 4 > 10，再次被拒。
        with pytest.raises(HTTPException) as blocked_again:
            accept_operation(conn, user_id=sub_id, service="asr", source_id="sq-e2e-4", units=1)
        assert blocked_again.value.detail["code"] == "SUB_ACCOUNT_QUOTA_EXCEEDED"

        # 母账号（钱包主人）直通：即便有人绕过 API 直接给它插额度行，校验只
        # 在 actor != 钱包主人时触发。
        raw.execute(
            f"INSERT INTO {QUOTA_TABLE} (user_id, monthly_credits) VALUES (%s, 1)",
            (master["user_id"],),
        )
        master_op = accept_operation(
            conn, user_id=master["user_id"], service="asr", source_id="sq-e2e-master", units=3
        )
        assert master_op
        wallet = wallet_state()
        assert wallet == (80, 16), wallet

    # 提交后从子账号视角读个人中心：结算 4 + 在途 4 = 8。
    sub_headers, _ = _sub_session(client, "quota_spender")
    profile = client.get(PROFILE_PATH, headers=sub_headers)
    assert profile.status_code == 200, profile.text
    assert profile.json()["monthly_quota_credits"] == 10
    assert profile.json()["quota_used_credits"] == 8, "在途计入 + 结算退回即时释放"
