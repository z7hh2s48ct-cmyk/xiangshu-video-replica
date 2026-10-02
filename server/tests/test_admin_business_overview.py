"""经营看板聚合端点测试（方案 P1）。

独立 PG 库（w15_business_overview_test）；种子覆盖：本/上一区间的实付充值
（含线下开通）、全历史首笔实付（新增付费）、缺成本证据的请求、以及用流水
回放上一区间末钱包快照所需的 CHARGE 账本。
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

TEST_KEY = "w15-biz-overview-test-hmac-key-0123456789ab"

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
W15_DB_NAME = "w15_business_overview_test"
FIXTURE_DAY = date(2026, 10, 1)


@pytest.fixture()
def repair_order(overview_pg_dsn: str) -> Iterator[tuple[str, str, str]]:
    suffix = uuid4().hex
    user_id, order_id, order_no = (
        f"repair-user-{suffix}",
        f"repair-order-{suffix}",
        f"TEST-REPAIR-{suffix}",
    )
    with psycopg.connect(overview_pg_dsn) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES (%s, %s, '虚构补记客户', 'customer', 1)",
            (user_id, user_id),
        )
        raw.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, 0, 0)",
            (user_id,),
        )
        raw.execute(
            "INSERT INTO recharge_orders (id, user_id, provider, amount_fen, credits, status, "
            "merchant_order_no, provider_trade_no, base_unit_price_fen_snapshot, "
            "charged_unit_price_fen_snapshot, min_recharge_fen_snapshot, "
            "recharge_step_fen_snapshot, paid_at) "
            "VALUES (%s, %s, 'zpay', 20000, 20, 'PAID', %s, %s, "
            "1000, 1000, 10000, 1000, CURRENT_TIMESTAMP)",
            (order_id, user_id, order_no, f"TEST-TRADE-{suffix}"),
        )
    try:
        yield user_id, order_id, order_no
    finally:
        with psycopg.connect(overview_pg_dsn) as raw:
            raw.execute(
                "DELETE FROM billing_credit_lots WHERE id IN "
                "(SELECT id FROM wallet_transactions WHERE recharge_order_id = %s)",
                (order_id,),
            )
            raw.execute("DELETE FROM wallet_transactions WHERE recharge_order_id = %s", (order_id,))
            raw.execute("DELETE FROM recharge_orders WHERE id = %s", (order_id,))
            raw.execute("DELETE FROM wallets WHERE user_id = %s", (user_id,))
            raw.execute("DELETE FROM users WHERE id = %s", (user_id,))


def test_paid_ledger_repair_route_is_real_audited_and_idempotent(
    recon_client: TestClient,
    recon_headers: dict[str, str],
    overview_pg_dsn: str,
    repair_order: tuple[str, str, str],
) -> None:
    user_id, order_id, order_no = repair_order
    path = f"/api/control/recharge-orders/{order_no}/repair-ledger"
    body = {"confirm": True, "reason": "核对虚构已支付订单缺账"}
    headers = {**recon_headers, "Idempotency-Key": "test-paid-repair"}
    response = recon_client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json() == {"order_no": order_no, "outcome": "repaired", "credits": 20}
    repeat = recon_client.post(path, headers=headers, json=body)
    assert repeat.status_code == 200
    assert repeat.json() == response.json()
    assert repeat.headers["X-Idempotent-Replay"] == "true"
    fresh = recon_client.post(
        path, headers={**headers, "Idempotency-Key": "test-paid-repair-fresh"}, json=body
    )
    assert fresh.json()["outcome"] == "already_recorded"
    conflict = recon_client.post(path, headers=headers, json={**body, "reason": "变更原因"})
    assert conflict.status_code == 409
    missing = recon_client.get(
        "/api/control/billing-reconciliation/items", params={"anomaly": "paid_without_charge"}
    ).json()
    assert order_id not in {item["order_id"] for item in missing["items"]}
    with psycopg.connect(overview_pg_dsn) as raw:
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            == 20
        )
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM wallet_transactions "
                "WHERE recharge_order_id = %s AND type = 'CHARGE'",
                (order_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM audit_logs "
                "WHERE action = 'payment.ledger_repair' AND entity_id = %s",
                (order_no,),
            ).fetchone()[0]
            == 2
        )


def test_paid_ledger_repair_concurrent_calls_do_not_double_credit(
    overview_pg_dsn: str,
    repair_order: tuple[str, str, str],
) -> None:
    from app.db_portable import BusinessConnection
    from app.payment_routes import _repair_paid_order_ledger

    user_id, order_id, order_no = repair_order

    def repair(_: int) -> str:
        with psycopg.connect(overview_pg_dsn) as raw:
            return _repair_paid_order_ledger(BusinessConnection.postgres(raw), order_no).outcome

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(repair, range(4)))
    assert outcomes.count("repaired") == 1
    assert outcomes.count("already_recorded") == 3
    with psycopg.connect(overview_pg_dsn) as raw:
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            == 20
        )
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM wallet_transactions WHERE recharge_order_id = %s", (order_id,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("status", ["PENDING", "FAILED", "CLOSED"])
def test_paid_ledger_repair_refuses_other_statuses(
    overview_pg_dsn: str,
    repair_order: tuple[str, str, str],
    status: str,
) -> None:
    from app.db_portable import BusinessConnection
    from app.payment_routes import _repair_paid_order_ledger

    user_id, order_id, order_no = repair_order
    with psycopg.connect(overview_pg_dsn) as raw:
        raw.execute(
            "UPDATE recharge_orders SET status = %s, provider_trade_no = NULL, paid_at = NULL "
            "WHERE id = %s",
            (status, order_id),
        )
        with pytest.raises(HTTPException) as refused:
            _repair_paid_order_ledger(BusinessConnection.postgres(raw), order_no)
        assert refused.value.status_code == 409
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            == 0
        )


def test_paid_ledger_repair_refuses_wallet_without_matching_ledger(
    overview_pg_dsn: str,
    repair_order: tuple[str, str, str],
) -> None:
    from app.db_portable import BusinessConnection
    from app.payment_routes import _repair_paid_order_ledger

    user_id, order_id, order_no = repair_order
    with psycopg.connect(overview_pg_dsn) as raw:
        raw.execute("UPDATE wallets SET available_credits = 20 WHERE user_id = %s", (user_id,))
        with pytest.raises(HTTPException) as refused:
            _repair_paid_order_ledger(BusinessConnection.postgres(raw), order_no)
        assert refused.value.detail["code"] == "LEDGER_REPAIR_WALLET_MISMATCH"
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM wallet_transactions WHERE recharge_order_id = %s", (order_id,)
            ).fetchone()[0]
            == 0
        )


def test_paid_ledger_repair_auditor_is_denied_without_wallet_change(
    recon_client: TestClient,
    overview_pg_dsn: str,
    repair_order: tuple[str, str, str],
) -> None:
    user_id, order_id, order_no = repair_order
    session = password_admin_session(recon_client, "auditor_u")
    assert session.status_code == 201
    response = recon_client.post(
        f"/api/control/recharge-orders/{order_no}/repair-ledger",
        headers={
            ADMIN_CSRF_HEADER: session.json()["csrf_token"],
            "Idempotency-Key": "auditor-repair-denied",
        },
        json={"confirm": True, "reason": "虚构只读边界验证"},
    )
    assert response.status_code == 403
    with psycopg.connect(overview_pg_dsn) as raw:
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM wallet_transactions WHERE recharge_order_id = %s", (order_id,)
            ).fetchone()[0]
            == 0
        )


# UTC 文本列（paid_at / created_at）的统一写法：先取上海挂钟日，再锚定当天
# 固定时刻，避免「昨天」在午夜附近漂进今天。
_TODAY_UTC = (
    "to_char((date '2026-10-01' + time '{time}') "
    "AT TIME ZONE 'Asia/Shanghai' AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
)
_YESTERDAY_UTC = (
    "to_char((date '2026-10-01' - 1 + time '{time}') "
    "AT TIME ZONE 'Asia/Shanghai' AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
)
_TODAY_TSTZ = "((date '2026-10-01' + time '{time}') AT TIME ZONE 'Asia/Shanghai')"
_YESTERDAY_TSTZ = "((date '2026-10-01' - 1 + time '{time}') AT TIME ZONE 'Asia/Shanghai')"


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _w15_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{W15_DB_NAME}"


@pytest.fixture(scope="module")
def overview_pg_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{W15_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{W15_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _w15_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_w15_dsn(), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role, is_active)
            VALUES ('cust_1', 'customer-1', '客户一公司', 'customer', 1),
                   ('cust_2', 'customer-2', '', 'customer', 1),
                   ('admin_u', 'admin_u', '管理员', 'admin', 1),
                   ('auditor_u', 'auditor_u', '审计员', 'auditor', 1)
            """
        )
        # 充值单：今日两笔实付（含一笔线下开通）、昨日一笔、今日一笔未付。
        # ck_recharge_orders_credit_calculation：credits × 档价快照 = 金额。
        for order_id, user, provider, amount, credits, status, paid_expr in (
            ("order_today_1", "cust_1", "zpay", 10000, 10, "PAID", _TODAY_UTC.format(time="01:00")),
            (
                "order_today_offline",
                "cust_2",
                "admin_adjustment",
                5000,
                5,
                "PAID",
                _TODAY_UTC.format(time="02:00"),
            ),
            (
                "order_prev",
                "cust_1",
                "zpay",
                20000,
                20,
                "PAID",
                _YESTERDAY_UTC.format(time="13:00"),
            ),
            ("order_pending", "cust_1", "zpay", 11000, 11, "PENDING", "NULL"),
        ):
            # paid_at/created_at 是 TEXT 列：when 表达式内联，其余走参数。
            conn.execute(
                f"""
                INSERT INTO recharge_orders (id, user_id, provider, amount_fen, credits,
                    status, merchant_order_no, provider_trade_no,
                    base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                    min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                    paid_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1000, 1000, 10000, 1000,
                        {paid_expr}, {_TODAY_UTC.format(time="00:30")})
                """,
                (
                    order_id,
                    user,
                    provider,
                    amount,
                    credits,
                    status,
                    f"BIZ-{order_id}",
                    # 交易号只在实付单上：PAID 且非线下开通（provider_trade_no 约束）。
                    f"trade-{order_id}"
                    if status == "PAID" and provider != "admin_adjustment"
                    else None,
                ),
            )
        # Explicit synthetic verified offline payment, not a computed compensation amount.
        conn.execute(
            "UPDATE recharge_orders SET payment_method='offline' WHERE id='order_today_offline'"
        )
        # 钱包与 CHARGE 账本：cust_1 今日 300/冻结 5，cust_2 今日 50。
        # 钱包必须与 CHARGE 账本配平（无 RESERVE 行就不能有冻结余额），
        # 否则资金概览与对账用例会因夹具自身的不一致而互相干扰。
        conn.execute(
            """
            INSERT INTO wallets (user_id, available_credits, reserved_credits)
            VALUES ('cust_1', 300, 0), ('cust_2', 50, 0)
            """
        )
        for tx_id, user, delta, order_id, when in (
            ("tx_prev", "cust_1", 200, "order_prev", _YESTERDAY_UTC.format(time="13:00")),
            ("tx_today_1", "cust_1", 100, "order_today_1", _TODAY_UTC.format(time="01:00")),
            (
                "tx_today_2",
                "cust_2",
                50,
                "order_today_offline",
                _TODAY_UTC.format(time="02:00"),
            ),
        ):
            conn.execute(
                f"""
                INSERT INTO wallet_transactions (id, user_id, type, available_delta,
                    reserved_delta, recharge_order_id, idempotency_key, created_at)
                VALUES (%s, %s, 'CHARGE', %s, 0, %s, %s, {when})
                """,
                (tx_id, user, delta, order_id, f"biz:{tx_id}"),
            )
        # 积分兑换比例 1 元 = 100 积分（1 积分 = 1 分），折合金额可直接断言。
        conn.execute(
            "UPDATE customer_credit_pricing SET config_json = %s WHERE id = 1",
            ('{"points_per_yuan": 100}',),
        )
        _billing_fact(
            conn,
            when_expr=_TODAY_TSTZ.format(time="03:00"),
            service="video_768p",
            module="video",
            unit="second",
            revenue=100,
            costs=("2.5",),
        )
        # 缺成本证据：只计待核对条数，不把金额当 0。
        _billing_fact(
            conn,
            when_expr=_TODAY_TSTZ.format(time="04:00"),
            service="oral",
            module="oral",
            unit="second",
            revenue=0,
            costs=(None,),
        )
        # 上一区间的消耗事实（环比基数）。
        _billing_fact(
            conn,
            when_expr=_YESTERDAY_TSTZ.format(time="05:00"),
            service="oral",
            module="oral",
            unit="second",
            revenue=9000,
            costs=("900",),
        )
    try:
        yield _w15_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{W15_DB_NAME}" WITH (FORCE)')


def _billing_fact(
    raw: psycopg.Connection,
    *,
    when_expr: str,
    service: str,
    module: str,
    unit: str,
    revenue: int | None,
    costs: tuple[str | None, ...],
) -> None:
    # when_expr 是本模块常量里的 SQL 表达式（timestamptz），必须内联而非参数绑定。
    operation = str(uuid4())
    raw.execute(
        "INSERT INTO billing_operations(id,user_id,service,module,source_id,unit,budget_units,"
        "pricing_snapshot_json,state,reserved_credits,charged_credits,actual_units,revenue_fen,"
        f"created_at,completed_at) VALUES(%s,'cust_1',%s,%s,%s,%s,10,'{{}}','SUCCEEDED',10,10,1,"
        f"%s,{when_expr},{when_expr})",
        (operation, service, module, operation, unit, revenue),
    )
    for index, cost in enumerate(costs):
        raw.execute(
            "INSERT INTO billing_attempts(id,operation_id,attempt_key,service,provider,unit,"
            "usage,cost_fen,state,completed_at) VALUES(%s,%s,%s,%s,'apilio','call',1,%s,"
            f"%s,{when_expr})",
            (
                str(uuid4()),
                operation,
                str(index),
                service,
                cost,
                # 成本证据状态与金额联动：缺金额即 UNKNOWN（待核对）。
                "ACTUAL" if cost is not None else "UNKNOWN",
            ),
        )


@pytest.fixture()
def overview_app(monkeypatch: pytest.MonkeyPatch, overview_pg_dsn: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_dashboard_routes import router as admin_dashboard_router

    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, overview_pg_dsn)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_dashboard_router)
    from app.export_controller import router as export_router

    app.include_router(export_router)
    try:
        yield app
    finally:
        close_pg_pool()


@pytest.fixture()
def recon_app(monkeypatch: pytest.MonkeyPatch, overview_pg_dsn: str) -> Iterator[FastAPI]:
    """对账明细端点挂在 control 路由下：需要客户生产模式让 ControlUser 走
    管理员会话（否则是代理 token 认证，测试里未配置 → 503）。"""
    from app.admin_auth_routes import router as admin_auth_router
    from app.control_routes import router as control_router
    from app.payment_routes import router as payment_router

    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, overview_pg_dsn)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(control_router)
    app.include_router(payment_router)
    try:
        yield app
    finally:
        close_pg_pool()


@pytest.fixture()
def client(overview_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(overview_app) as test_client:
        yield test_client


@pytest.fixture()
def recon_client(recon_app: FastAPI) -> Iterator[TestClient]:
    # 客户生产模式下管理员会话 cookie 带 Secure 标记，必须用 https base_url。
    with TestClient(recon_app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    response = password_admin_session(client, "admin_u")
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


@pytest.fixture()
def recon_headers(recon_client: TestClient) -> dict[str, str]:
    response = password_admin_session(recon_client, "admin_u")
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _get(client: TestClient, headers: dict[str, str], start: str, end: str):
    return client.get(
        "/api/control/business/overview",
        params={"start": start, "end": end},
        headers=headers,
    )


def test_business_overview_requires_admin_session(client: TestClient) -> None:
    assert (
        client.get(
            "/api/control/business/overview", params={"start": "2026-09-01", "end": "2026-09-30"}
        ).status_code
        == 401
    )


def test_business_overview_rejects_bad_ranges(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    assert _get(client, admin_headers, "2026-09-30", "2026-09-01").status_code == 422
    # 367 天越界；366 天恰好放行。
    assert _get(client, admin_headers, "2025-01-01", "2026-01-03").status_code == 422
    assert _get(client, admin_headers, "2025-01-02", "2026-01-02").status_code == 200


def test_business_overview_period_metrics(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    # 上海「今天」由数据库给出，避免用例自身时区漂移。
    with psycopg.connect(_w15_dsn()) as raw:
        today = raw.execute("SELECT date '2026-10-01'").fetchone()[0]
        yesterday = today - timedelta(days=1)
    response = _get(client, admin_headers, today.isoformat(), today.isoformat())
    assert response.status_code == 200, response.text
    payload = response.json()

    metrics = payload["metrics"]
    # 充值实收 = 今日两笔 PAID（线上 + 线下开通），未付单不计。
    assert metrics["recharge_fen"] == 15000
    assert metrics["paying_customers"] == 2
    # 只有 cust_2 的全历史首笔实付落在今日；cust_1 昨日已付过。
    assert metrics["new_paying_customers"] == 1
    # 确认收入只累计有证据的金额；缺成本证据的行只进待核对条数。
    assert metrics["revenue_fen"] == 100
    assert metrics["cost_fen"] == 2.5
    assert metrics["unknown_cost_count"] == 1
    assert metrics["pending_count"] == 0
    # 预收 = 可用 + 冻结；1 元 = 100 积分时折合金额数值等于积分数。
    assert metrics["prepaid_credits"] == 350
    assert metrics["prepaid_fen"] == 350

    prev = payload["prev"]
    assert prev["recharge_fen"] == 20000
    assert prev["paying_customers"] == 1
    assert prev["new_paying_customers"] == 1
    assert prev["revenue_fen"] == 9000
    assert prev["cost_fen"] == 900
    # 流水回放：当前余额减去今日之后的增量 = 上一区间末快照。
    assert prev["prepaid_credits"] == 200
    assert prev["prepaid_fen"] == 200

    # 趋势按日展开；缺成本证据那天的成本为 None（待核对），收入照常。
    today_daily = [d for d in payload["daily"] if d["day"] == today.isoformat()]
    assert len(today_daily) == 1
    assert today_daily[0]["revenue_fen"] == 100
    assert today_daily[0]["cost_fen"] is None

    modules = {m["service"]: m for m in payload["modules"]}
    assert modules["video_768p"]["label"] == "视频生成 · 768P"
    assert modules["video_768p"]["revenue_fen"] == 100
    assert modules["video_768p"]["cost_fen"] == 2.5
    assert modules["oral"]["unknown_cost_count"] == 1

    top = payload["top_customers"]
    assert [t["user_id"] for t in top] == ["cust_1"]
    assert top[0]["display_name"] == "客户一公司"
    assert top[0]["revenue_fen"] == 100

    # 单日区间的上一等长区间 = 昨天单日。
    assert payload["prev_start"] == yesterday.isoformat()
    assert payload["prev_end"] == yesterday.isoformat()


@pytest.mark.parametrize(
    ("start", "expected_recharge", "expected_orders", "expected_online_orders"),
    [(date(2026, 9, 30), 35000, 3, 2), (date(2026, 10, 1), 15000, 2, 1)],
)
def test_funds_summary_breaks_channels_and_flags_reconciliation(
    admin_headers: dict[str, str],
    client: TestClient,
    start: date,
    expected_recharge: int,
    expected_orders: int,
    expected_online_orders: int,
) -> None:
    """资金概览：渠道分解、线下收款、赠送、预收与对账异常总数。"""
    response = client.get(
        "/api/control/funds/summary",
        params={"start": start.isoformat(), "end": FIXTURE_DAY.isoformat()},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    # 固定月初日期：昨日属于上月，不应污染本月资金汇总。
    assert payload["recharge_fen"] == expected_recharge
    assert payload["orders"] == expected_orders
    assert payload["offline_fen"] == 5000
    assert payload["grant_credits"] == 0
    assert payload["refund_credits"] == 0
    assert payload["net_fen"] == payload["recharge_fen"]
    channels = {item["provider"]: item for item in payload["by_channel"]}
    assert channels["zpay"]["orders"] == expected_online_orders
    assert channels["admin_adjustment"]["amount_fen"] == 5000
    assert payload["prepaid_credits"] == 350
    assert payload["prepaid_fen"] == 350
    # 夹具账本与钱包配平，对账异常应为 0。
    assert payload["reconciliation_problems"] == 0


def test_funds_summary_counts_refund_credits(
    admin_headers: dict[str, str], client: TestClient, overview_pg_dsn: str
) -> None:
    """退款扣减（REFUND 流水）计入资金概览并折算金额；净收入相应减少。"""
    with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
        today = raw.execute("SELECT date '2026-10-01'").fetchone()[0]
        raw.execute(
            f"""
            INSERT INTO wallet_transactions (id, user_id, type, available_delta,
                reserved_delta, idempotency_key, created_at)
            VALUES ('tx_refund_1', 'cust_1', 'REFUND', -30, 0, 'biz:tx_refund_1',
                    {_TODAY_UTC.format(time="09:00")})
            """
        )
        raw.execute(
            "UPDATE wallets SET available_credits = available_credits - 30 WHERE user_id = 'cust_1'"
        )
    try:
        response = client.get(
            "/api/control/funds/summary",
            params={"start": today.isoformat(), "end": today.isoformat()},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["refund_credits"] == 30
        assert payload["refund_fen"] == 30
        assert payload["net_fen"] == payload["recharge_fen"] - 30
        assert payload["prepaid_credits"] == 320
    finally:
        with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
            raw.execute("DELETE FROM wallet_transactions WHERE id = 'tx_refund_1'")
            raw.execute(
                "UPDATE wallets SET available_credits = available_credits + 30 "
                "WHERE user_id = 'cust_1'"
            )


def test_reconciliation_items_page_each_anomaly_type(
    recon_headers: dict[str, str], recon_client: TestClient, overview_pg_dsn: str
) -> None:
    """对账明细三类清单：条数与汇总口径一致，含客户名称。"""
    with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
        # 构造三类异常：PAID 无 CHARGE、CHARGE 无 PAID 订单、钱包与账本分桶不齐。
        raw.execute(
            f"""
            INSERT INTO recharge_orders (id, user_id, provider, amount_fen, credits,
                status, merchant_order_no, provider_trade_no,
                base_unit_price_fen_snapshot,
                charged_unit_price_fen_snapshot, min_recharge_fen_snapshot,
                recharge_step_fen_snapshot, paid_at, created_at)
            VALUES ('order_orphan_paid', 'cust_2', 'zpay', 10000, 10, 'PAID',
                    'BIZ-orphan-paid', 'trade-orphan-paid', 1000, 1000, 10000, 1000,
                    {_TODAY_UTC.format(time="10:00")}, {_TODAY_UTC.format(time="10:00")})
            """
        )
        raw.execute(
            f"""
            INSERT INTO wallet_transactions (id, user_id, type, available_delta,
                reserved_delta, recharge_order_id, idempotency_key, created_at)
            VALUES ('tx_orphan_charge', 'cust_1', 'CHARGE', 10, 0, 'order_pending',
                    'biz:tx_orphan_charge', {_TODAY_UTC.format(time="11:00")})
            """
        )
        raw.execute("UPDATE wallets SET reserved_credits = 6 WHERE user_id = 'cust_1'")
    try:
        for anomaly, expected_total in (
            ("paid_without_charge", 1),
            ("charge_without_paid_order", 1),
            ("wallet_mismatch", 1),
        ):
            response = recon_client.get(
                "/api/control/billing-reconciliation/items",
                params={"anomaly": anomaly},
                headers=recon_headers,
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["total"] == expected_total, (anomaly, payload)
            assert len(payload["items"]) == expected_total
        first = recon_client.get(
            "/api/control/billing-reconciliation/items",
            params={"anomaly": "paid_without_charge"},
            headers=recon_headers,
        ).json()["items"][0]
        assert first["order_id"] == "order_orphan_paid"
        assert first["order_no"] == "BIZ-orphan-paid"
        # display_name 为空的客户回退显示用户名。
        assert first["display_name"] == "customer-2"
        assert first["username"] == "customer-2"
        path = "/api/control/billing-reconciliation/verify"
        for anomaly in ["wallet_mismatch", "charge_without_paid_order"]:
            query = {"anomaly": anomaly}
            item = recon_client.get(
                "/api/control/billing-reconciliation/items",
                params=query,
                headers=recon_headers,
            ).json()["items"][0]
            identity = item["user_id"] if anomaly == "wallet_mismatch" else item["transaction_id"]
            if anomaly == "charge_without_paid_order":
                assert item["order_no"] == "BIZ-order_pending"
            body = {
                "anomaly": anomaly,
                "entity_id": identity,
                "snapshot": item["snapshot"],
                "confirm": True,
                "reason": "隔离核对：差异仍待处理",
            }
            headers = {**recon_headers, "Idempotency-Key": f"verification-{anomaly}"}
            with psycopg.connect(overview_pg_dsn) as raw:
                before = raw.execute("SELECT * FROM wallets ORDER BY user_id").fetchall()
                ledger_count = raw.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0]
            verified = recon_client.post(path, headers=headers, json=body)
            assert verified.status_code == 200, verified.text
            replay = recon_client.post(path, headers=headers, json=body)
            assert replay.status_code == 200 and replay.json() == verified.json()
            assert replay.headers["X-Idempotent-Replay"] == "true"
            refreshed = recon_client.get(
                "/api/control/billing-reconciliation/items",
                params=query,
                headers=recon_headers,
            ).json()
            assert refreshed["total"] == 1
            record = refreshed["items"][0]["verification"]
            assert record["state"] == "verified" and record["reason"] == body["reason"]
            assert record["operator"] and record["at"]
            with psycopg.connect(overview_pg_dsn) as raw:
                assert raw.execute("SELECT * FROM wallets ORDER BY user_id").fetchall() == before
                assert (
                    raw.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0]
                    == ledger_count
                )
                assert (
                    raw.execute(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE action='control.reconciliation.verify' "
                        "AND entity_id=%s",
                        (f"{anomaly}:{identity}",),
                    ).fetchone()[0]
                    == 1
                )
                if anomaly == "wallet_mismatch":
                    raw.execute("UPDATE wallets SET reserved_credits=7 WHERE user_id='cust_1'")
                else:
                    raw.execute(
                        "UPDATE recharge_orders SET status='CLOSED' WHERE id='order_pending'"
                    )
            changed = recon_client.get(
                "/api/control/billing-reconciliation/items",
                params=query,
                headers=recon_headers,
            ).json()["items"][0]
            assert changed["verification"]["state"] == "needs_recheck"
            stale = recon_client.post(
                path, headers={**recon_headers, "Idempotency-Key": f"stale-{anomaly}"}, json=body
            )
            assert stale.status_code == 409, stale.text
    finally:
        with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
            raw.execute("DELETE FROM billing_credit_lots WHERE id = 'tx_orphan_charge'")
            raw.execute("DELETE FROM wallet_transactions WHERE id = 'tx_orphan_charge'")
            raw.execute("DELETE FROM recharge_orders WHERE id = 'order_orphan_paid'")
            raw.execute("UPDATE wallets SET reserved_credits = 5 WHERE user_id = 'cust_1'")
            raw.execute("UPDATE recharge_orders SET status='PENDING' WHERE id='order_pending'")


def test_reconciliation_items_reject_unknown_anomaly(
    recon_headers: dict[str, str], recon_client: TestClient
) -> None:
    response = recon_client.get(
        "/api/control/billing-reconciliation/items",
        params={"anomaly": "something_else"},
        headers=recon_headers,
    )
    assert response.status_code == 422


def test_zero_amount_grants_are_not_paying_or_first_payment(
    admin_headers: dict[str, str],
    client: TestClient,
    overview_pg_dsn: str,
) -> None:
    with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES ('gift_only', 'gift-only', '赠送样本', 'customer', 1)"
        )
        for order_id, user_id, paid in (
            ("zero_before_real_payment", "cust_2", _YESTERDAY_UTC.format(time="01:00")),
            ("zero_only_payment", "gift_only", _TODAY_UTC.format(time="01:00")),
        ):
            raw.execute(
                f"""INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider,
                    status, pricing_scope, amount_fen, credits,
                    base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                    min_recharge_fen_snapshot, recharge_step_fen_snapshot, paid_at)
                VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', 'INTERNAL', 0, 30,
                        1000, 1000, 10000, 1000, {paid})""",
                (order_id, user_id, order_id),
            )
    try:
        response = _get(client, admin_headers, FIXTURE_DAY.isoformat(), FIXTURE_DAY.isoformat())
        assert response.status_code == 200, response.text
        metrics = response.json()["metrics"]
        assert metrics["recharge_fen"] == 15000
        assert metrics["paying_customers"] == 2
        assert metrics["new_paying_customers"] == 1
        assert response.json()["prev"]["paying_customers"] == 1
    finally:
        with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
            raw.execute(
                "DELETE FROM recharge_orders WHERE id IN "
                "('zero_before_real_payment', 'zero_only_payment')"
            )
            raw.execute("DELETE FROM users WHERE id = 'gift_only'")


def test_auditor_reads_reconciliation_but_cannot_sync(
    recon_client: TestClient,
    overview_pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import payment_routes

    def reject_gateway(*_args: object, **_kwargs: object) -> None:
        pytest.fail("审计员写请求不应触达支付网关")

    monkeypatch.setattr(payment_routes, "get_payment_provider", reject_gateway)
    login = password_admin_session(recon_client, "auditor_u")
    assert login.status_code == 201, login.text
    headers = {ADMIN_CSRF_HEADER: login.json()["csrf_token"]}
    before = None
    with psycopg.connect(overview_pg_dsn) as raw:
        before = raw.execute(
            "SELECT user_id, available_credits, reserved_credits FROM wallets ORDER BY user_id"
        ).fetchall()
    listing = recon_client.get(
        "/api/control/billing-reconciliation/items",
        params={"anomaly": "paid_without_charge"},
        headers=headers,
    )
    assert listing.status_code == 200, listing.text
    rejected = recon_client.post(
        "/api/control/recharge-orders/BIZ-order_today_1/sync",
        headers={**headers, "Idempotency-Key": "auditor-sync-rejected"},
        json={"confirm": True, "reason": "只读角色权限回归"},
    )
    assert rejected.status_code == 403, rejected.text
    assert (
        recon_client.post(
            "/api/control/billing-reconciliation/verify",
            headers={**headers, "Idempotency-Key": "auditor-verification-rejected"},
            json={
                "anomaly": "wallet_mismatch",
                "entity_id": "cust_1",
                "snapshot": "0" * 64,
                "confirm": True,
                "reason": "只读角色禁止写核对记录",
            },
        ).status_code
        == 403
    )
    with psycopg.connect(overview_pg_dsn) as raw:
        after = raw.execute(
            "SELECT user_id, available_credits, reserved_credits FROM wallets ORDER BY user_id"
        ).fetchall()
    assert after == before


def test_business_margin_unknown_zero_and_negative_share_trend_contract(
    client: TestClient, admin_headers: dict[str, str], overview_pg_dsn: str
) -> None:
    current = _get(client, admin_headers, "2026-10-01", "2026-10-01").json()
    assert current["metrics"]["gross_fen"] is None
    assert current["metrics"]["margin_pct"] is None
    assert current["daily"][0]["known_cost_fen"] == 2.5
    assert current["daily"][0]["margin_pct"] is None
    # 已完成账务事实不可改写：新建独立日期样本，测试也不绕过不可变触发器。
    for day, revenue, cost in (
        ("2026-10-03", 100, "202.5"),
        ("2026-10-04", None, "202.5"),
        ("2026-10-05", 0, "202.5"),
    ):
        with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
            _billing_fact(
                raw,
                when_expr=f"'{day} 04:00:00+08'::timestamptz",
                service="oral",
                module="oral",
                unit="second",
                revenue=revenue,
                costs=(cost,),
            )
    complete = _get(client, admin_headers, "2026-10-03", "2026-10-03").json()
    assert complete["metrics"]["gross_fen"] == -102.5
    assert complete["metrics"]["margin_pct"] == -102.5
    assert complete["daily"][0]["margin_pct"] == -102.5
    uncertain = _get(client, admin_headers, "2026-10-04", "2026-10-04").json()
    assert uncertain["metrics"]["unknown_revenue_count"] == 1
    assert uncertain["metrics"]["gross_fen"] is None
    assert uncertain["daily"][0]["margin_pct"] is None
    zero = _get(client, admin_headers, "2026-10-05", "2026-10-05").json()
    assert zero["metrics"]["gross_fen"] == -202.5
    assert zero["metrics"]["margin_pct"] is None


def test_same_provider_actual_methods_are_separate_and_missing_is_unknown(
    client: TestClient, admin_headers: dict[str, str], overview_pg_dsn: str
) -> None:
    with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
        raw.execute("UPDATE recharge_orders SET payment_method='alipay' WHERE id='order_today_1'")
        raw.execute("UPDATE recharge_orders SET payment_method='wxpay' WHERE id='order_prev'")
    try:
        payload = client.get(
            "/api/control/funds/summary",
            params={"start": "2026-09-30", "end": "2026-10-01"},
            headers=admin_headers,
        ).json()
        methods = {item["method"]: item for item in payload["by_method"]}
        assert methods["alipay"]["amount_fen"] == 10000
        assert methods["wxpay"]["amount_fen"] == 20000
        assert methods["offline"]["amount_fen"] == 5000
        assert sum(item["amount_fen"] for item in methods.values()) == payload["recharge_fen"]
        with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
            raw.execute("UPDATE recharge_orders SET payment_method=NULL WHERE id='order_today_1'")
        unknown = client.get(
            "/api/control/funds/summary",
            params={"start": "2026-10-01", "end": "2026-10-01"},
            headers=admin_headers,
        ).json()
        assert (
            next(item for item in unknown["by_method"] if item["method"] == "unknown")["amount_fen"]
            == 10000
        )
    finally:
        with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
            raw.execute(
                "UPDATE recharge_orders SET payment_method=NULL "
                "WHERE id IN ('order_today_1','order_prev')"
            )


@pytest.mark.parametrize("kind", ["business", "funds"])
def test_summary_export_matches_selected_range_and_preserves_unknown(
    client: TestClient, admin_headers: dict[str, str], kind: str
) -> None:
    import csv
    import io

    summary = client.get(
        f"/api/control/{kind}/{'overview' if kind == 'business' else 'summary'}",
        params={"start": "2026-10-01", "end": "2026-10-01"},
        headers=admin_headers,
    ).json()
    response = client.post(
        "/api/control/reports/summary/export",
        headers=admin_headers,
        json={"kind": kind, "start_date": "2026-10-01", "end_date": "2026-10-01"},
    )
    assert response.status_code == 200, response.text
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][:4] == ["区间开始", "2026-10-01", "区间结束", "2026-10-01"]
    metrics = summary["metrics"] if kind == "business" else summary
    assert ["充值实收（分）", str(metrics["recharge_fen"])] in rows
    assert ["当前预收积分余额", str(metrics["prepaid_credits"])] in rows
    if kind == "business":
        assert ["毛利率（%）", "待核对"] in rows
        assert ["已知成本（分）", "2.5"] in rows
    else:
        assert ["支付方式未知", "1", "10000"] in rows
        assert ["线下转账", "1", "5000"] in rows
    assert response.headers["cache-control"] == "no-store"


def test_summary_export_rejects_auditor_csrf_and_invalid_range(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    body = {"kind": "business", "start_date": "2026-10-01", "end_date": "2026-10-01"}
    assert client.post("/api/control/reports/summary/export", json=body).status_code == 403
    assert (
        client.post(
            "/api/control/reports/summary/export",
            headers=admin_headers,
            json={**body, "end_date": "2026-09-30"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/control/reports/summary/export",
            headers=admin_headers,
            json={**body, "end_date": "2028-01-01"},
        ).status_code
        == 422
    )
    session = password_admin_session(client, "auditor_u")
    assert session.status_code == 201
    assert (
        client.post(
            "/api/control/reports/summary/export",
            headers={ADMIN_CSRF_HEADER: session.json()["csrf_token"]},
            json=body,
        ).status_code
        == 403
    )


def test_fake_payment_confirmation_persists_actual_method_not_preselected_channel(
    client: TestClient, admin_headers: dict[str, str], overview_pg_dsn: str
) -> None:
    from app.db_portable import BusinessConnection
    from app.zpay_payments import confirm_recharge_payment

    with psycopg.connect(overview_pg_dsn) as raw:
        raw.execute("UPDATE recharge_orders SET channel='alipay' WHERE id='order_pending'")
        confirm_recharge_payment(
            BusinessConnection.postgres(raw),
            merchant_order_no="BIZ-order_pending",
            provider_trade_no="synthetic-wxpay-confirmed",
            amount_fen=11000,
            channel="wxpay",
            source_digest="synthetic-confirmation-digest",
            allowed_channels=("alipay", "wxpay"),
        )
        method = raw.execute(
            "SELECT channel,payment_method FROM recharge_orders WHERE id='order_pending'"
        ).fetchone()
        assert method["channel"] == "alipay"
        assert method["payment_method"] == "wxpay"
    summary = client.get(
        "/api/control/funds/summary",
        params={"start": "2019-01-01", "end": "2026-12-31"},
        headers=admin_headers,
    ).json()
    assert (
        next(item for item in summary["by_method"] if item["method"] == "wxpay")["amount_fen"]
        == 11000
    )


@pytest.mark.parametrize("legacy_kind", ["cost", "settlement"])
def test_business_card_trend_and_csv_keep_legacy_evidence_unknown(
    client: TestClient, admin_headers: dict[str, str], overview_pg_dsn: str, legacy_kind: str
) -> None:
    day = "2026-10-06" if legacy_kind == "cost" else "2026-10-07"
    with psycopg.connect(overview_pg_dsn, autocommit=True) as raw:
        _billing_fact(
            raw,
            when_expr=f"'{day} 04:00:00+08'::timestamptz",
            service="oral",
            module="oral",
            unit="second",
            revenue=100,
            costs=("20",),
        )
        if legacy_kind == "cost":
            raw.execute(
                "INSERT INTO operation_cost_records(id,source_type,source_id,subje"
                "ct,user_id,unit,unit_price_fen,usage_amount,cost_fen,status,occur"
                "red_at) VALUES ('legacy-business-cost','generation','legacy','vid"
                "eo_generation_768p','cust_1','second',5,1,5,'ACTUAL',%s)",
                (f"{day}T02:00:00Z",),
            )
        else:
            raw.execute(
                "INSERT INTO projects(id,owner_user_id,name) VALUES ('legacy-busin"
                "ess-p','cust_1','历史项目')"
            )
            raw.execute(
                "INSERT INTO generation_batches(id,project_id,created_by_user_id,i"
                "dempotency_key,request_hash,request_snapshot_json) VALUES ('legac"
                "y-business-b','legacy-business-p','cust_1','legacy-business-b','h"
                "ash','{}')"
            )
            raw.execute(
                "INSERT INTO generation_tasks(id,batch_id,provider,model,status,ar"
                "chive_status) VALUES ('legacy-business-task','legacy-business-b',"
                "'metaso','h3','SUCCEEDED','ARCHIVED')"
            )
            raw.execute(
                "INSERT INTO wallet_transactions(id,user_id,type,available_delta,r"
                "eserved_delta,task_id,billing_round,idempotency_key,created_at) V"
                "ALUES ('legacy-business-settle','cust_1','SETTLE',0,-1,'legacy-bu"
                "siness-task',99,'legacy-business-settle',%s)",
                (f"{day}T02:00:00Z",),
            )
    report = _get(client, admin_headers, day, day).json()
    field = "legacy_cost_count" if legacy_kind == "cost" else "legacy_settlement_count"
    assert report["metrics"][field] == report["daily"][0][field] == 1
    assert report["metrics"]["gross_fen"] is report["metrics"]["margin_pct"] is None
    assert report["daily"][0]["margin_pct"] is None
    exported = client.post(
        "/api/control/reports/summary/export",
        headers=admin_headers,
        json={"kind": "business", "start_date": day, "end_date": day},
    )
    assert exported.status_code == 200, exported.text
    assert "毛利（分）,待核对" in exported.text and "毛利率（%）,待核对" in exported.text
