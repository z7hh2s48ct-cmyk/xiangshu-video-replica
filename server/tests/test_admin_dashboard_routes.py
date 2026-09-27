"""W15 — 总览仪表盘聚合端点测试。

独立 PG 库（w15_dashboard_test）；种子：客户用户、今日一成一败两个任务、
PENDING 配对、5 天内过期的可激活码、今日 PAID 充值单。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

TEST_KEY = "w15-dash-test-hmac-key-0123456789abcdef"

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
W15_DB_NAME = "w15_dashboard_test"


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _w15_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{W15_DB_NAME}"


@pytest.fixture(scope="module")
def dashboard_pg_dsn() -> Iterator[str]:
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
            VALUES ('cust_1', 'customer-1', '客户一', 'customer', 1),
                   ('admin_u', 'admin_u', '管理员', 'admin', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO projects (id, owner_user_id, name)
            VALUES ('p1', 'cust_1', '项目')
            """
        )
        conn.execute(
            """
            INSERT INTO generation_batches (id, project_id, created_by_user_id,
                idempotency_key, request_hash, request_snapshot_json)
            VALUES ('b1', 'p1', 'cust_1', 'k', 'h', '{}')
            """
        )
        # 今日一成一败
        conn.execute(
            """
            INSERT INTO generation_tasks (id, batch_id, generation_mode, provider,
                model, status, archive_status, created_at_utc)
            VALUES ('t_ok', 'b1', 'I2V', 'metaso', 'MiniMax-H3', 'SUCCEEDED',
                    'DIRECT', now()),
                   ('t_bad', 'b1', 'I2V', 'metaso', 'MiniMax-H3', 'FAILED',
                    'PENDING', now())
            """
        )
        # PAID 充值单（今日）
        conn.execute(
            """
            INSERT INTO recharge_orders (id, user_id, provider, amount_fen, credits,
                status, merchant_order_no, provider_trade_no,
                base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                paid_at, created_at)
            VALUES ('order_1', 'cust_1', 'zpay', 10000, 10, 'PAID',
                    'W15-ORDER-1', 'trade-w15-0001', 1000, 1000, 10000, 1000,
                    to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
                    to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
            """
        )
        # 钱包 + 与 PAID 订单配平的 CHARGE 流水（对账一致性 = 0）
        conn.execute(
            """
            INSERT INTO wallets (user_id, available_credits, reserved_credits)
            VALUES ('cust_1', 10, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (id, user_id, type, available_delta,
                reserved_delta, recharge_order_id, idempotency_key, created_at)
            VALUES ('tx_charge_1', 'cust_1', 'CHARGE', 10, 0, 'order_1',
                    'zpay:charge:order_1',
                    to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
            """
        )
        # 激活码批次 + 5 天内过期的可激活码
        conn.execute(
            """
            INSERT INTO activation_code_batches (id, name, face_value_fen,
                unit_price_fen_snapshot, credits_snapshot, quantity,
                activation_expires_at, status, created_by_user_id)
            VALUES ('batch_exp', '即将过期批次', 0, 0, 0, 1,
                    now() + make_interval(days => 5), 'OPEN', 'admin_u')
            """
        )
        conn.execute(
            """
            INSERT INTO activation_codes (id, batch_id, code_digest,
                digest_key_version, masked_code, status, issued_at)
            VALUES ('code_exp', 'batch_exp', 'digest-exp', 1,
                    'XS04-EXPI***', 'ISSUED', now())
            """
        )
        # PENDING 且未过期的配对申请
        conn.execute(
            """
            INSERT INTO device_pairing_requests (id, activation_code_id,
                candidate_fingerprint_hmac, candidate_fingerprint_key_version,
                display_name, platform, status, expires_at)
            VALUES ('pair_1', 'code_exp', 'hmac-pending', 1,
                    '新笔记本', 'windows', 'PENDING',
                    now() + interval '10 minutes')
            """
        )
    try:
        yield _w15_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{W15_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def dash_app(monkeypatch: pytest.MonkeyPatch, dashboard_pg_dsn: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_dashboard_routes import router as admin_dashboard_router

    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, dashboard_pg_dsn)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_dashboard_router)
    try:
        yield app
    finally:
        close_pg_pool()


@pytest.fixture()
def client(dash_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(dash_app) as test_client:
        yield test_client


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    return _exchange(client)


def _exchange(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def test_dashboard_requires_admin_session(client: TestClient) -> None:
    assert client.get("/api/control/dashboard/summary").status_code == 401


def test_dashboard_summary_counts(admin_headers: dict[str, str], client: TestClient) -> None:
    response = client.get("/api/control/dashboard/summary", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["today"]["generation_count"] == 2
    assert payload["today"]["succeeded"] == 1
    assert payload["today"]["success_rate_pct"] == 50.0
    assert payload["today"]["output_seconds"] == 0
    assert payload["today"]["cost_fen"] == 0
    assert payload["today"]["gross_fen"] == 0
    assert payload["today"]["active_customers"] == 1
    assert payload["today"]["recharge_fen"] == 10000
    assert payload["today"]["recharge_orders"] == 1

    todos = payload["todos"]
    assert todos["failed_tasks_7d"] == 1
    assert todos["reconciliation_problems"] == 0
    from app.billing_catalog import SERVICES

    assert todos["unconfigured_rates"] == sum(s.customer_charge_allowed for s in SERVICES.values())
    assert todos["unknown_cost_records"] == 0
    assert "device_slots" not in payload
    assert "online_devices" not in payload["today"]
    assert "pending_pairings" not in todos
    assert "expiring_codes_7d" not in todos

    # 近 7 日趋势包含今日（一成一败）
    assert len(payload["trend"]) == 7
    today_trend = [t for t in payload["trend"] if t["succeeded"] == 1]
    assert len(today_trend) == 1
    assert today_trend[0]["failed"] == 1
    assert today_trend[0]["cost_fen"] == 0


def test_failed_todo_counts_oral_failures_when_no_video_failed(
    admin_headers: dict[str, str], client: TestClient, dashboard_pg_dsn: str
) -> None:
    with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
        conn.execute("UPDATE generation_tasks SET status = 'SUCCEEDED' WHERE id = 't_bad'")
        conn.execute(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                source_quality_status, status, created_by
            ) VALUES (
                'dashboard-oral-identity', 'cust_1', '口播人物',
                'AUTHORIZED', 'PASSED', 'ACTIVE', 'cust_1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO oral_avatars (
                id, identity_id, owner_user_id, title, status,
                source_kind, source_asset_id
            ) VALUES (
                'dashboard-oral-avatar', 'dashboard-oral-identity', 'cust_1',
                '口播分身', 'READY', 'IMAGE', 'dashboard-source'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, mode, title,
                status, estimated_cost_fen, error_message, idempotency_key,
                request_hash, submission_state, provider_charge_state
            ) VALUES (
                'dashboard-oral-failed', 'cust_1', 'dashboard-oral-identity',
                'dashboard-oral-avatar', 'TTS', '失败口播', 'FAILED', 350,
                '数字人服务生成失败', 'dashboard-oral-failed-key',
                'dashboard-oral-failed-hash', 'FAILED', 'NOT_CHARGED'
            )
            """
        )
    try:
        response = client.get("/api/control/dashboard/summary", headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["todos"]["failed_tasks_7d"] == 1
        assert response.json()["today"]["generation_count"] == 3
        assert response.json()["trend"][-1]["failed"] == 1
        with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE oral_tasks SET status = 'SUCCEEDED' WHERE id = 'dashboard-oral-failed'"
            )
        succeeded = client.get("/api/control/dashboard/summary", headers=admin_headers).json()
        assert succeeded["today"]["succeeded"] == 2
        assert succeeded["trend"][-1]["succeeded"] == 2
    finally:
        with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM oral_tasks WHERE id = 'dashboard-oral-failed'")
            conn.execute("DELETE FROM oral_avatars WHERE id = 'dashboard-oral-avatar'")
            conn.execute("DELETE FROM person_identities WHERE id = 'dashboard-oral-identity'")
            conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 't_bad'")


def test_dashboard_day_expressions_ignore_database_session_timezone(
    dashboard_pg_dsn: str,
) -> None:
    from app.admin_dashboard_routes import _day_expr, _timestamptz_day_expr

    utc_text = _day_expr("'2026-09-04 18:00:00'")
    instant = _timestamptz_day_expr("TIMESTAMPTZ '2026-09-04 18:00:00+00'")
    with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
        conn.execute("SET TIME ZONE 'America/Los_Angeles'")
        row = conn.execute(f"SELECT {utc_text}, {instant}").fetchone()

    assert row[0].isoformat() == "2026-09-05"
    assert row[1].isoformat() == "2026-09-05"


def test_cost_trend_includes_the_complete_first_shanghai_day(
    admin_headers: dict[str, str], client: TestClient, dashboard_pg_dsn: str
) -> None:
    with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, generation_mode, provider, model, status,
                archive_status, created_at_utc
            ) VALUES (
                'task_first_shanghai_day', 'b1', 'I2V', 'metaso', 'MiniMax-H3',
                'SUCCEEDED', 'DIRECT',
                (((now() AT TIME ZONE 'Asia/Shanghai')::date - 6 + time '00:10')
                    AT TIME ZONE 'Asia/Shanghai')
            ), (
                'failed_first_shanghai_day', 'b1', 'I2V', 'metaso', 'MiniMax-H3',
                'FAILED', 'PENDING',
                (((now() AT TIME ZONE 'Asia/Shanghai')::date - 6 + time '00:10')
                    AT TIME ZONE 'Asia/Shanghai')
            )
            """
        )
        conn.execute(
            """
            INSERT INTO operation_cost_records (
                id, source_type, source_id, subject, user_id,
                resolution, unit, usage_amount, unit_price_fen, cost_fen,
                status, occurred_at, completed_at
            ) VALUES (
                'cost_first_shanghai_day', 'test', 'first-day',
                'video_generation_768p', 'cust_1', '768P', 'second',
                1, 77, 77, 'ACTUAL',
                (((now() AT TIME ZONE 'Asia/Shanghai')::date - 6 + time '00:10')
                    AT TIME ZONE 'Asia/Shanghai'),
                now()
            )
            """
        )

    response = client.get("/api/control/dashboard/summary", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert response.json()["trend"][0]["cost_fen"] is None
    assert response.json()["trend"][0]["legacy_cost_records"] == 1
    assert response.json()["trend"][0]["succeeded"] == 1
    assert response.json()["trend"][0]["failed"] == 1
    assert response.json()["todos"]["failed_tasks_7d"] == 2


def _billing_fact(
    raw,
    *,
    when,
    revenue=0,
    reserved=0,
    charged=0,
    state="SUCCEEDED",
    costs=("2.5",),
    service="analysis",
    units=1,
):
    operation = str(uuid4())
    raw.execute(
        "INSERT INTO billing_operations(id,user_id,service,module,source_id,unit,budget_units,"
        "pricing_snapshot_json,state,reserved_credits,charged_credits,actual_units,revenue_fen,"
        "created_at,completed_at) VALUES(%s,'cust_1',%s,%s,%s,%s,10,'{}',%s,%s,%s,%s,%s,%s,%s)",
        (
            operation,
            service,
            "oral" if service == "oral" else "video" if service.startswith("video_") else "replica",
            operation,
            "second" if service.startswith("video_") or service == "oral" else "call",
            state,
            reserved,
            charged,
            units,
            revenue,
            when,
            None if state == "PENDING" else when,
        ),
    )
    for index, cost in enumerate(costs):
        raw.execute(
            "INSERT INTO billing_attempts(id,operation_id,attempt_key,service,provider,unit,"
            "usage,cost_fen,state,completed_at) VALUES(%s,%s,%s,%s,'apilio','call',1,%s,%s,%s)",
            (
                str(uuid4()),
                operation,
                str(index),
                service,
                cost,
                "ACTUAL" if cost is not None else "UNKNOWN",
                when,
            ),
        )
    return operation


def test_dashboard_and_statistics_share_settled_financial_facts(
    admin_headers,
    client,
    dashboard_pg_dsn,
):
    from app.billing_reports import date_bounds, statistics
    from app.db_portable import BusinessConnection

    with psycopg.connect(dashboard_pg_dsn) as raw:
        today = raw.execute("SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date").fetchone()[0]
        lower, upper = date_bounds(today, today)
        # Paid partial delivery, gifted credits, and a failed/refunded request.
        _billing_fact(
            raw,
            when=lower,
            revenue=100,
            reserved=10,
            charged=6,
            costs=("2.5", "3"),
            service="video_768p",
            units=6,
        )
        _billing_fact(raw, when=lower, revenue=0, reserved=4, charged=4, costs=("1",))
        _billing_fact(raw, when=lower, service="oral", units=Decimal("9.08"), costs=("0",))
        _billing_fact(raw, when=lower, service="oral", units=13, state="FAILED", costs=("0",))
        _billing_fact(
            raw, when=lower, revenue=0, reserved=7, charged=0, state="FAILED", costs=("0.5",)
        )
        # Exclusive Shanghai upper bound; adjacent days must not enter today's totals.
        _billing_fact(raw, when=lower - timedelta(seconds=1), revenue=9000, costs=("900",))
        _billing_fact(raw, when=upper, revenue=8000, costs=("800",))
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES ('analysis',false,NULL,0),('first_frame',true,0,NULL)"
        )

    def compare():
        with psycopg.connect(dashboard_pg_dsn) as raw:
            report = statistics(BusinessConnection.postgres(raw), start=today, end=today)
        response = client.get("/api/control/dashboard/summary", headers=admin_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        for dashboard_key, report_key in (
            ("cost_fen", "cost_fen"),
            ("revenue_fen", "revenue_fen"),
            ("gross_fen", "profit_fen"),
        ):
            value = payload["today"][dashboard_key]
            assert (Decimal(str(value)) if value is not None else None) == report["totals"][
                report_key
            ]
        assert payload["trend"][-1]["cost_fen"] == payload["today"]["cost_fen"]
        return payload, report["totals"]

    payload, totals = compare()
    assert totals["known_cost_fen"] == Decimal("7")
    assert totals["known_revenue_fen"] == 100
    assert totals["profit_fen"] == 93
    assert totals["refunded_credits"] == 11
    assert payload["today"]["recharge_fen"] == 10000  # Never added to consumption income.
    assert payload["today"]["output_seconds"] == 15.08
    assert payload["today"]["margin_pct"] == 93
    # Zero cost is configured, NULL is not。本用例只为 analysis 配了成本单价，故计数 =
    # 全部对客收费科目 - analysis。其中含五个爆款科目（viral_search /
    # viral_search_refresh / viral_statistics / viral_detail / viral_copy，均未配置
    # 费率 = NULL），所以是 17。
    assert payload["todos"]["unconfigured_rates"] == 17

    with psycopg.connect(dashboard_pg_dsn) as raw:
        _billing_fact(raw, when=lower, revenue=None, costs=("1",))
    payload, totals = compare()
    assert payload["today"]["revenue_fen"] is None
    assert payload["today"]["gross_fen"] is None
    assert totals["cost_fen"] == 8

    with psycopg.connect(dashboard_pg_dsn) as raw:
        _billing_fact(raw, when=lower, costs=(None,))
    payload, totals = compare()
    assert totals["cost_fen"] is None
    assert payload["todos"]["unknown_cost_records"] == 1

    with psycopg.connect(dashboard_pg_dsn) as raw:
        _billing_fact(raw, when=lower, revenue=None, state="PENDING", costs=())
    payload, totals = compare()
    assert totals["pending_count"] == payload["today"]["pending_operations"] == 1
    assert payload["today"]["margin_pct"] is None


def test_legacy_costs_and_settlements_are_visible_but_never_repriced(dashboard_pg_dsn):
    from datetime import date

    from app.billing_reports import statistics
    from app.db_portable import BusinessConnection

    with psycopg.connect(dashboard_pg_dsn) as raw:
        raw.execute(
            "INSERT INTO operation_cost_records(id,source_type,source_id,subject,user_id,unit,"
            "unit_price_fen,usage_amount,cost_fen,status,occurred_at) VALUES "
            "('legacy-only','generation','legacy-only','video_generation_768p','cust_1','second',"
            "5,1,5,'ACTUAL','2024-02-29T16:00:00Z')"
        )
        raw.execute(
            "INSERT INTO wallet_transactions(id,user_id,type,available_delta,reserved_delta,"
            "task_id,billing_round,idempotency_key,created_at) VALUES "
            "('legacy-settle','cust_1','SETTLE',0,-1,'t_ok',99,'legacy-settle',"
            "'2024-02-29 16:00:00')"
        )
        report = statistics(
            BusinessConnection.postgres(raw), start=date(2024, 3, 1), end=date(2024, 3, 1)
        )
        totals = report["totals"]
        assert totals["operation_count"] == 0
        assert totals["legacy_cost_count"] == totals["legacy_settlement_count"] == 1
        assert totals["known_cost_fen"] == totals["known_revenue_fen"] == 0
        assert totals["cost_fen"] is totals["revenue_fen"] is totals["profit_fen"] is None
        assert report["periods"][0]["period"] == "2024-03-01"
        empty = statistics(
            BusinessConnection.postgres(raw), start=date(2024, 2, 29), end=date(2024, 2, 29)
        )
        assert empty["totals"]["profit_fen"] == 0
        assert empty["totals"]["legacy_cost_count"] == 0


def test_linked_compatibility_records_are_not_counted_twice_and_pending_is_not_zero(
    dashboard_pg_dsn,
):
    from datetime import date, datetime

    from app.billing_reports import statistics
    from app.db_portable import BusinessConnection

    with psycopg.connect(dashboard_pg_dsn) as raw:
        when = datetime(2025, 1, 1, tzinfo=UTC)
        operation = _billing_fact(raw, when=when, reserved=1, charged=1, revenue=3, costs=("2",))
        attempt = raw.execute(
            "SELECT id FROM billing_attempts WHERE operation_id=%s", (operation,)
        ).fetchone()[0]
        raw.execute(
            "INSERT INTO operation_cost_records(id,source_type,source_id,subject,user_id,unit,"
            "unit_price_fen,usage_amount,cost_fen,status,occurred_at,billing_attempt_id) VALUES "
            "('linked-mirror','analysis','linked-mirror','video_analysis_768p','cust_1','call',"
            "2,1,2,'ACTUAL',%s,%s)",
            (when, attempt),
        )
        raw.execute(
            "INSERT INTO wallet_transactions(id,user_id,type,available_delta,reserved_delta,"
            "billing_operation_id,billing_round,idempotency_key,created_at) VALUES "
            "('linked-settle','cust_1','SETTLE',0,-1,%s,1,'linked-settle','2025-01-01 00:00:00')",
            (operation,),
        )
        conn = BusinessConnection.postgres(raw)
        totals = statistics(conn, start=date(2025, 1, 1), end=date(2025, 1, 1))["totals"]
        assert totals["legacy_cost_count"] == totals["legacy_settlement_count"] == 0
        assert totals["cost_fen"] == 2 and totals["profit_fen"] == 1
        _billing_fact(raw, when=when, state="PENDING", revenue=None, costs=())
        totals = statistics(conn, start=date(2025, 1, 1), end=date(2025, 1, 1))["totals"]
        assert totals["unknown_cost_count"] == 0 and totals["pending_count"] == 1
        assert totals["cost_fen"] is totals["profit_fen"] is None
        assert totals["known_cost_fen"] == 2


ANALYSIS_UPSTREAM_REASON = "model gemini-3.8-flash is not available"


def test_dashboard_surfaces_analysis_failures_with_upstream_reasons(
    admin_headers: dict[str, str], client: TestClient, dashboard_pg_dsn: str
) -> None:
    """2026-09-20 事故的另一半：拆解 100% 失败，总览待办里却什么都没有。

    拆解不并入既有的 failed_tasks_7d（那是「生成 = 视频+口播」的口径，混进去
    会让历史对比失真），而是单列一条带上游原因的待办——否则客服只看到「失败」
    两个字，不知道是模型下架、限流还是断网。
    """
    before = client.get("/api/control/dashboard/summary", headers=admin_headers).json()
    assert before["todos"]["analysis_failures_7d"] == 0

    diagnostics = (
        (
            "dash-analysis-http-1",
            30,
            "ANALYSIS_PROVIDER_FAILED",
            "http",
            {"http_status": 400, "failure_phase": "http", "reason": ANALYSIS_UPSTREAM_REASON},
        ),
        (
            "dash-analysis-http-2",
            20,
            "ANALYSIS_PROVIDER_FAILED",
            "http",
            {"http_status": 400, "failure_phase": "http", "reason": ANALYSIS_UPSTREAM_REASON},
        ),
        (
            "dash-analysis-network",
            10,
            "ANALYSIS_PROVIDER_UNREACHABLE",
            "network",
            {"http_status": None, "failure_phase": "network", "reason": "URLError"},
        ),
    )
    with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "created_by_user_id) VALUES ('dash-analysis-asset', 'p1', 'reference_video', "
            "'cos://bucket/dash-analysis.mp4', %s, 1024, 'cust_1')",
            ("b" * 64,),
        )
        for task_id, minutes_ago, code, phase, diagnostic in diagnostics:
            conn.execute(
                "INSERT INTO analysis_tasks (id, project_id, asset_id, created_by_user_id, "
                "duration_seconds, status, attempt, error_code, failure_phase, retryable, "
                "upstream_diagnostic_json, created_at, updated_at, completed_at) VALUES "
                "(%s, 'p1', 'dash-analysis-asset', 'cust_1', 8, 'FAILED', 1, %s, %s, 1, %s, "
                "to_char(now() AT TIME ZONE 'UTC' - make_interval(mins => %s), "
                "'YYYY-MM-DD HH24:MI:SS'), "
                "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), "
                "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))",
                (task_id, code, phase, json.dumps(diagnostic), minutes_ago),
            )
    try:
        response = client.get("/api/control/dashboard/summary", headers=admin_headers)

        assert response.status_code == 200, response.text
        todos = response.json()["todos"]
        assert todos["analysis_failures_7d"] == 3
        # 旧口径不受影响：拆解不混入「生成」计数。
        assert todos["failed_tasks_7d"] == before["todos"]["failed_tasks_7d"]
        reasons = todos["analysis_failure_reasons"]
        assert reasons[0] == {
            "error_code": "ANALYSIS_PROVIDER_FAILED",
            "failure_phase": "http",
            "reason": ANALYSIS_UPSTREAM_REASON,
            "count": 2,
        }
        assert reasons[1]["error_code"] == "ANALYSIS_PROVIDER_UNREACHABLE"
        assert reasons[1]["failure_phase"] == "network"
        assert reasons[1]["reason"] == "URLError"
        assert reasons[1]["count"] == 1
    finally:
        with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM analysis_tasks WHERE id LIKE 'dash-analysis-%'")
            conn.execute("DELETE FROM assets WHERE id = 'dash-analysis-asset'")


def test_reconciliation_counts_per_bucket_not_just_the_total(
    admin_headers: dict[str, str], client: TestClient, dashboard_pg_dsn: str
) -> None:
    """桶间搬移必须被发现：总览的口径要与资金页的对账一致.

    先前总览比的是「可用 + 冻结」两桶**之和**，而
    ``/api/control/billing-reconciliation`` 是**逐桶**比（`control_routes.py`
    的 available_total / reserved_total 分桶聚合）。于是当钱包是
    available=5/reserved=5、账本仍是 available_total=10/reserved_total=0 时，
    两桶之和都等于 10 → 总览报 0 而资金页报 1（2026-09-12 评审 P2 记载的
    「对账 todo 与 reconciliation 计数口径微差」）。

    钱从哪个桶出账正是对账要抓的东西，漏报比误报危险。这条用例构造的正是
    「两桶之和相等、分桶不等」这一唯一能区分两种口径的状态 —— 若把实现改回
    求和比较，它会立刻变红。

    模块级夹具共用同一个库，故结束前把钱包改回原值。
    """
    with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
        before = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'cust_1'"
        ).fetchone()
        assert before == (10, 0), before
        try:
            conn.execute(
                "UPDATE wallets SET available_credits = 5, reserved_credits = 5 "
                "WHERE user_id = 'cust_1'"
            )
            response = client.get("/api/control/dashboard/summary", headers=admin_headers)
            assert response.status_code == 200, response.text
            assert response.json()["todos"]["reconciliation_problems"] == 1
        finally:
            conn.execute(
                "UPDATE wallets SET available_credits = %s, reserved_credits = %s "
                "WHERE user_id = 'cust_1'",
                before,
            )
