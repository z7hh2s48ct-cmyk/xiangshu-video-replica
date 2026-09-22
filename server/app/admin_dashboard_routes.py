"""Read-only dashboard; financial totals share the itemized reporting facts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter

from app.admin_auth_routes import AdminReader
from app.billing_catalog import SERVICES
from app.billing_reports import statistics
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection

router = APIRouter(prefix="/api/control", tags=["admin-dashboard"])

# 上海日界表达式：timestamptz → 上海挂钟日期文本
_SHANGHAI_DATE = "(%s AT TIME ZONE 'Asia/Shanghai')::date"


def _one(conn: Any, sql: str) -> Any:
    """单值聚合查询：fetchone 保证非空（聚合无 GROUP BY 恒返一行）。"""
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


def _day_expr(column: str) -> str:
    return _SHANGHAI_DATE % f"{column}::timestamp AT TIME ZONE 'UTC'"


def _timestamptz_day_expr(column: str) -> str:
    return _SHANGHAI_DATE % column


def _number_or_none(value: Any) -> float | None:
    # Keep the dashboard's numeric JSON contract; all accounting happens in Decimal upstream.
    return float(value) if value is not None else None


@router.get("/dashboard/summary")
def dashboard_summary(_actor: AdminReader) -> dict[str, Any]:
    with pg_transaction(isolation="REPEATABLE READ") as conn:
        today = _one(conn, "SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date")
        economics = statistics(
            BusinessConnection.postgres(conn), start=today - timedelta(days=6), end=today
        )
        financial_by_day = {item["period"]: item for item in economics["periods"]}
        today_financial = financial_by_day.get(today.isoformat(), {})
        today_generation = conn.execute(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (
                       WHERE status = 'SUCCEEDED'
                         AND archive_status IN ('ARCHIVED', 'DIRECT')
                   ) AS succeeded
            FROM generation_tasks
            WHERE {_day_expr("created_at_utc")} = {_timestamptz_day_expr("now()")}
            """
        ).fetchone()
        assert today_generation is not None
        today_oral = conn.execute(
            f"""
            SELECT count(*), count(*) FILTER (WHERE status = 'SUCCEEDED')
            FROM oral_tasks
            WHERE {_day_expr("created_at")} = {_timestamptz_day_expr("now()")}
            """
        ).fetchone()
        assert today_oral is not None

        trend_rows = conn.execute(
            f"""
            WITH days AS (
                SELECT generate_series(
                    (now() AT TIME ZONE 'Asia/Shanghai')::date - 6,
                    (now() AT TIME ZONE 'Asia/Shanghai')::date,
                    interval '1 day'
                )::date AS day
            ), generation_by_day AS (
                SELECT {_day_expr("created_at_utc")} AS day,
                       count(*) FILTER (
                           WHERE status = 'SUCCEEDED'
                             AND archive_status IN ('ARCHIVED', 'DIRECT')
                       ) AS succeeded,
                       count(*) FILTER (WHERE status = 'FAILED') AS failed
                FROM generation_tasks
                WHERE created_at_utc >= (
                    ((now() AT TIME ZONE 'Asia/Shanghai')::date - 6)::timestamp
                    AT TIME ZONE 'Asia/Shanghai'
                )
                GROUP BY 1
            ), oral_by_day AS (
                SELECT {_day_expr("created_at")} AS day,
                       count(*) FILTER (WHERE status = 'SUCCEEDED') AS succeeded,
                       count(*) FILTER (WHERE status = 'FAILED') AS failed
                FROM oral_tasks
                WHERE {_day_expr("created_at")} >=
                    (now() AT TIME ZONE 'Asia/Shanghai')::date - 6
                GROUP BY 1
            )
            SELECT days.day,
                   COALESCE(generation_by_day.succeeded, 0) + COALESCE(oral_by_day.succeeded, 0),
                   COALESCE(generation_by_day.failed, 0) + COALESCE(oral_by_day.failed, 0)
            FROM days
            LEFT JOIN generation_by_day USING (day)
            LEFT JOIN oral_by_day USING (day)
            ORDER BY days.day
            """
        ).fetchall()
        active_customers = int(
            _one(
                conn,
                """
                SELECT count(*) FROM users
                WHERE role = 'customer' AND is_active = 1
                """,
            )
        )
        today_recharge_fen = int(
            _one(
                conn,
                f"""
                SELECT COALESCE(SUM(amount_fen), 0) FROM recharge_orders
                WHERE status = 'PAID'
                  AND paid_at IS NOT NULL
                  AND {_day_expr("paid_at")} = {_timestamptz_day_expr("now()")}
                """,
            )
        )
        today_recharge_orders = int(
            _one(
                conn,
                f"""
                SELECT count(*) FROM recharge_orders
                WHERE status = 'PAID' AND paid_at IS NOT NULL
                  AND {_day_expr("paid_at")} = {_timestamptz_day_expr("now()")}
                """,
            )
        )
        failed_tasks_7d = int(
            _one(
                conn,
                f"""
                SELECT
                    (SELECT count(*) FROM generation_tasks
                     WHERE status = 'FAILED'
                       AND created_at_utc >= (
                           ((now() AT TIME ZONE 'Asia/Shanghai')::date - 6)::timestamp
                           AT TIME ZONE 'Asia/Shanghai'
                       ))
                  + (SELECT count(*) FROM oral_tasks
                     WHERE status = 'FAILED'
                       AND {_day_expr("created_at")} >=
                           (now() AT TIME ZONE 'Asia/Shanghai')::date - 6)
                """,
            )
        )
        # 拆解是付费上游调用，却在总览里完全没有脉络（2026-09-20 事故）。
        # 单列计数 + 上游原因，不并入 failed_tasks_7d：那是「生成 = 视频+口播」
        # 的口径，混入拆解会让历史对比失真。
        analysis_failures_7d = int(
            _one(
                conn,
                f"""
                SELECT count(*) FROM analysis_tasks
                WHERE status = 'FAILED'
                  AND {_day_expr("created_at")} >=
                      (now() AT TIME ZONE 'Asia/Shanghai')::date - 6
                """,
            )
        )
        analysis_failure_reasons = [
            {
                "error_code": row[0],
                "failure_phase": row[1],
                "reason": row[2],
                "count": int(row[3]),
            }
            for row in conn.execute(
                f"""
                SELECT error_code,
                       failure_phase,
                       upstream_diagnostic_json ->> 'reason' AS reason,
                       count(*) AS total
                FROM analysis_tasks
                WHERE status = 'FAILED'
                  AND {_day_expr("created_at")} >=
                      (now() AT TIME ZONE 'Asia/Shanghai')::date - 6
                GROUP BY 1, 2, 3
                ORDER BY total DESC, error_code ASC, failure_phase ASC
                LIMIT 3
                """
            ).fetchall()
        ]
        reconciliation_problems = int(
            _one(
                conn,
                """
                SELECT
                  (SELECT count(*) FROM wallets w
                     WHERE w.available_credits + w.reserved_credits <>
                           COALESCE((SELECT SUM(available_delta + reserved_delta)
                             FROM wallet_transactions wt WHERE wt.user_id = w.user_id), 0))
                + (SELECT count(*) FROM recharge_orders o
                     WHERE o.status = 'PAID' AND NOT EXISTS (
                         SELECT 1 FROM wallet_transactions wt
                         WHERE wt.recharge_order_id = o.id AND wt.type = 'CHARGE'))
                + (SELECT count(*) FROM wallet_transactions wt
                     WHERE wt.type = 'CHARGE' AND NOT EXISTS (
                         SELECT 1 FROM recharge_orders o
                         WHERE o.id = wt.recharge_order_id AND o.status = 'PAID'))
                """,
            )
        )
        configured = {
            row[0]
            for row in conn.execute(
                "SELECT service FROM billing_tariffs WHERE unit_cost_fen IS NOT NULL"
            ).fetchall()
        }
        unconfigured_rates = sum(
            item.customer_charge_allowed and key not in configured for key, item in SERVICES.items()
        )

    trend = [
        {
            "day": str(row[0]),
            "succeeded": int(row[1]),
            "failed": int(row[2]),
            "cost_fen": _number_or_none(financial_by_day.get(str(row[0]), {}).get("cost_fen", 0)),
            "legacy_cost_records": financial_by_day.get(str(row[0]), {}).get(
                "legacy_cost_count", 0
            ),
        }
        for row in trend_rows
    ]
    generation_count = int(today_generation[0]) + int(today_oral[0])
    succeeded = int(today_generation[1]) + int(today_oral[1])
    margin = today_financial.get("profit_margin")
    return {
        "today": {
            "generation_count": generation_count,
            "succeeded": succeeded,
            "success_rate_pct": (
                None if generation_count == 0 else round(succeeded / generation_count * 100, 1)
            ),
            "output_seconds": float(today_financial.get("video_seconds", 0)),
            "cost_fen": _number_or_none(today_financial.get("cost_fen", 0)),
            "revenue_fen": _number_or_none(today_financial.get("revenue_fen", 0)),
            "gross_fen": _number_or_none(today_financial.get("profit_fen", 0)),
            "margin_pct": None if margin is None else float(round(margin * 100, 1)),
            "pending_operations": today_financial.get("pending_count", 0),
            "unknown_revenue_operations": today_financial.get("unknown_revenue_count", 0),
            "legacy_cost_records": today_financial.get("legacy_cost_count", 0),
            "legacy_settlements": today_financial.get("legacy_settlement_count", 0),
            "active_customers": active_customers,
            "recharge_fen": today_recharge_fen,
            "recharge_orders": today_recharge_orders,
        },
        "trend": trend,
        "todos": {
            "failed_tasks_7d": failed_tasks_7d,
            "analysis_failures_7d": analysis_failures_7d,
            "analysis_failure_reasons": analysis_failure_reasons,
            "reconciliation_problems": reconciliation_problems,
            "unconfigured_rates": unconfigured_rates,
            "unknown_cost_records": today_financial.get("unknown_cost_count", 0),
        },
    }
