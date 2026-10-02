"""Read-only dashboard; financial totals share the itemized reporting facts."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from fastapi import APIRouter, HTTPException

from app.admin_auth_routes import AdminReader
from app.admin_cash_evidence import CASH_ORDER_ELIGIBLE, NONCASH_ADJUSTMENT, OFFLINE_CASH_EVIDENCE
from app.admin_customer_metrics import utc_text_timestamp
from app.billing_catalog import SERVICES
from app.billing_reports import UNMETERED_DELIVERED_CHARGE, date_bounds, statistics
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
    return _SHANGHAI_DATE % utc_text_timestamp(column)


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
        reconciliation_problems = _reconciliation_problem_count(conn)
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


# ---------------------------------------------------------------------------
# 经营看板（方案 P1）：给老板的一屏结论。充值与消耗两条收入口径严格分开；
# 成本只累计已有证据的金额，缺证据只计条数，绝不冒充零成本。
# ---------------------------------------------------------------------------

# 自定义区间上限：看板聚合按日/按客户展开，放开到无限区间会把全表扫进内存。
_MAX_OVERVIEW_RANGE_DAYS = 366

# 与 billing_reports.statistics 同一成本口径的底层事实：
# 成本证据 = billing_effective_attempts；已交付却零调用证据的请求按 1 条待核对计。
_FACTS_CTE = """
WITH costs AS (
  SELECT operation_id, COUNT(*) AS calls,
         SUM(effective_cost_fen) AS known_cost,
         COUNT(*) FILTER (WHERE effective_cost_fen IS NULL) AS unknown_cost
  FROM billing_effective_attempts GROUP BY operation_id
), facts AS (
  SELECT o.user_id, o.service, o.state, o.revenue_fen,
         COALESCE(c.calls, 0) AS calls,
         COALESCE(c.known_cost, 0) AS known_cost,
         COALESCE(c.unknown_cost, 0) + CASE WHEN {unmetered} THEN 1 ELSE 0 END AS unknown_cost
  FROM billing_operations o LEFT JOIN costs c ON c.operation_id = o.id
  WHERE COALESCE(o.completed_at, o.created_at) >= %s
    AND COALESCE(o.completed_at, o.created_at) < %s
)
"""


def _unmetered_expr() -> str:
    return UNMETERED_DELIVERED_CHARGE.format(calls="COALESCE(c.calls, 0)")


def _recharge_metrics(conn: Any, *, lower: Any, upper: Any) -> dict[str, int]:
    """充值实收与付费客户数（含线下开通：它同样落 PAID 的 recharge_orders）。

    新增付费 = 全历史首笔实付落在本区间的客户，所以子查询不能只看区间内行。
    """
    row = conn.execute(
        f"""
        WITH paid AS (
            SELECT user_id, amount_fen,
                   {utc_text_timestamp("paid_at")} AS paid_utc
            FROM recharge_orders ro
            WHERE status = 'PAID' AND paid_at IS NOT NULL AND amount_fen > 0
              AND {CASH_ORDER_ELIGIBLE}
        )
        SELECT COALESCE(SUM(amount_fen), 0) AS recharge_fen,
               COUNT(DISTINCT user_id) AS paying_customers,
               COUNT(DISTINCT user_id) FILTER (
                   WHERE user_id IN (
                       SELECT user_id FROM paid
                       GROUP BY user_id
                       HAVING MIN(paid_utc) >= %s AND MIN(paid_utc) < %s
                   )
               ) AS new_paying_customers
        FROM paid
        WHERE paid_utc >= %s AND paid_utc < %s
        """,
        (lower, upper, lower, upper),
    ).fetchone()
    assert row is not None
    return {
        "recharge_fen": int(row["recharge_fen"] or 0),
        "paying_customers": int(row["paying_customers"] or 0),
        "new_paying_customers": int(row["new_paying_customers"] or 0),
    }


def _consumption_facts(conn: Any, *, lower: Any, upper: Any) -> list[dict[str, Any]]:
    """区间内按科目聚合的确认收入/已知成本/待核对计数（金额跳过缺证据行）。"""
    rows = conn.execute(
        _FACTS_CTE.format(unmetered=_unmetered_expr())
        + """
        SELECT service,
               COALESCE(SUM(revenue_fen), 0) AS revenue_fen,
               COALESCE(SUM(known_cost), 0) AS known_cost_fen,
               COUNT(*) FILTER (WHERE unknown_cost > 0) AS unknown_cost_count,
               COUNT(*) FILTER (WHERE state = 'PENDING') AS pending_count,
               COUNT(*) FILTER (WHERE revenue_fen IS NULL) AS unknown_revenue_count
        FROM facts
        GROUP BY service
        ORDER BY COALESCE(SUM(revenue_fen), 0) DESC,
                 COALESCE(SUM(known_cost), 0) DESC, service
        """,
        (lower, upper),
    ).fetchall()
    result = []
    for row in rows:
        service = str(row["service"])
        known = SERVICES.get(service)
        result.append(
            {
                "service": service,
                "label": known.name if known else service,
                "revenue_fen": float(row["revenue_fen"] or 0),
                "cost_fen": float(row["known_cost_fen"] or 0),
                "unknown_cost_count": int(row["unknown_cost_count"] or 0),
                "pending_count": int(row["pending_count"] or 0),
                "unknown_revenue_count": int(row["unknown_revenue_count"] or 0),
            }
        )
    return result


def _top_customers(conn: Any, *, lower: Any, upper: Any, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        _FACTS_CTE.format(unmetered=_unmetered_expr())
        + """
        SELECT f.user_id, u.username,
               COALESCE(NULLIF(u.display_name, ''), u.username) AS display_name,
               COALESCE(SUM(f.revenue_fen), 0) AS revenue_fen
        FROM facts f JOIN users u ON u.id = f.user_id
        GROUP BY f.user_id, u.username, u.display_name
        ORDER BY COALESCE(SUM(f.revenue_fen), 0) DESC, f.user_id
        LIMIT %s
        """,
        (lower, upper, limit),
    ).fetchall()
    return [
        {
            "user_id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "revenue_fen": float(row["revenue_fen"] or 0),
        }
        for row in rows
    ]


def _points_per_yuan(conn: Any) -> int | None:
    """积分兑换比例（1 元 = N 积分）；未配置时返回 None，折算金额不猜。"""
    pricing_row = conn.execute(
        "SELECT config_json FROM customer_credit_pricing WHERE id = 1"
    ).fetchone()
    if pricing_row is None or not pricing_row["config_json"]:
        return None
    try:
        config = json.loads(str(pricing_row["config_json"]))
    except (TypeError, ValueError):
        return None
    if isinstance(config, dict):
        raw = config.get("points_per_yuan")
        if isinstance(raw, int) and raw > 0:
            return raw
    return None


def _customer_wallet_totals(conn: Any) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(w.available_credits), 0) AS available_credits,
               COALESCE(SUM(w.reserved_credits), 0) AS reserved_credits
        FROM wallets w JOIN users u ON u.id = w.user_id
        WHERE u.role = 'customer'
        """
    ).fetchone()
    assert row is not None
    return int(row["available_credits"] or 0), int(row["reserved_credits"] or 0)


def _reconciliation_problem_count(conn: Any) -> int:
    """对账异常总数，逐桶比口径与 /billing-reconciliation 一致。"""
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM wallets w
             LEFT JOIN (
                 SELECT user_id,
                        SUM(available_delta) AS available_total,
                        SUM(reserved_delta) AS reserved_total
                 FROM wallet_transactions
                 GROUP BY user_id
             ) AS ledger ON ledger.user_id = w.user_id
             WHERE w.available_credits <> COALESCE(ledger.available_total, 0)
                OR w.reserved_credits <> COALESCE(ledger.reserved_total, 0))
        + (SELECT count(*) FROM recharge_orders o
             WHERE o.status = 'PAID' AND NOT EXISTS (
                 SELECT 1 FROM wallet_transactions wt
                 WHERE wt.recharge_order_id = o.id AND wt.type = 'CHARGE'))
        + (SELECT count(*) FROM wallet_transactions wt
             WHERE wt.type = 'CHARGE' AND NOT EXISTS (
                 SELECT 1 FROM recharge_orders o
                 WHERE o.id = wt.recharge_order_id AND o.status = 'PAID'))
        """
    ).fetchone()
    assert row is not None
    return int(row[0])


def _prepaid_snapshot(conn: Any, *, since: Any) -> tuple[int, int | None, int, int | None]:
    """预收积分余额：当前快照 + 用流水回放出的上一区间末快照。

    余额是时点数，环比必须回到上一区间末那一刻：钱包当前值减去
    ``since`` 之后的全部流水增量，等价于账本在 ``since`` 的重放。
    折合金额依赖积分兑换比例；比例未配置时只给积分数，不猜金额。
    """
    current = conn.execute(
        """
        SELECT COALESCE(SUM(w.available_credits), 0) AS available_credits,
               COALESCE(SUM(w.reserved_credits), 0) AS reserved_credits
        FROM wallets w JOIN users u ON u.id = w.user_id
        WHERE u.role = 'customer'
        """
    ).fetchone()
    assert current is not None
    deltas = conn.execute(
        f"""
        SELECT COALESCE(SUM(t.available_delta), 0) AS available_delta,
               COALESCE(SUM(t.reserved_delta), 0) AS reserved_delta
        FROM wallet_transactions t JOIN users u ON u.id = t.user_id
        WHERE u.role = 'customer'
          AND {utc_text_timestamp("t.created_at")} >= %s
        """,
        (since,),
    ).fetchone()
    assert deltas is not None
    cur_available = int(current["available_credits"] or 0)
    cur_reserved = int(current["reserved_credits"] or 0)
    prev_available = cur_available - int(deltas["available_delta"] or 0)
    prev_reserved = cur_reserved - int(deltas["reserved_delta"] or 0)

    points_per_yuan = _points_per_yuan(conn)

    def _fen(credits: int) -> int | None:
        if points_per_yuan is None:
            return None
        return int(
            (Decimal(credits) * 100 / Decimal(points_per_yuan)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    return (
        cur_available + cur_reserved,
        _fen(cur_available + cur_reserved),
        prev_available + prev_reserved,
        _fen(prev_available + prev_reserved),
    )


@router.get("/business/overview")
def business_overview(start: date, end: date, _actor: AdminReader) -> dict[str, Any]:
    if (end - start).days > _MAX_OVERVIEW_RANGE_DAYS:
        raise HTTPException(422, detail="经营看板单次最多查询 366 天")
    lower, upper = date_bounds(start, end)
    length = (end - start).days + 1
    prev_start = start - timedelta(days=length)
    prev_end = start - timedelta(days=1)
    prev_lower, prev_upper = date_bounds(prev_start, prev_end)
    with pg_transaction(isolation="REPEATABLE READ") as conn:
        # 裸连接的行是元组；命名行访问必须统一走 BusinessConnection 包装，
        # 不能依赖「池里某条连接恰好被之前的请求改过 row factory」。
        business_conn = BusinessConnection.postgres(conn)
        cur_recharge = _recharge_metrics(business_conn, lower=lower, upper=upper)
        prev_recharge = _recharge_metrics(business_conn, lower=prev_lower, upper=prev_upper)
        cur_modules = _consumption_facts(business_conn, lower=lower, upper=upper)
        prev_modules = _consumption_facts(business_conn, lower=prev_lower, upper=prev_upper)
        top_customers = _top_customers(business_conn, lower=lower, upper=upper)
        prepaid_credits, prepaid_fen, prev_prepaid_credits, prev_prepaid_fen = _prepaid_snapshot(
            business_conn, since=prev_upper
        )
        economics = statistics(business_conn, start=start, end=end, grain="day")
        prev_economics = statistics(business_conn, start=prev_start, end=prev_end, grain="day")
        daily = [
            {
                "day": item["period"],
                "revenue_fen": _number_or_none(item.get("revenue_fen")),
                "cost_fen": _number_or_none(item.get("cost_fen")),
                "known_revenue_fen": _number_or_none(item.get("known_revenue_fen")),
                "known_cost_fen": _number_or_none(item.get("known_cost_fen")),
                "margin_pct": (
                    None
                    if item.get("profit_margin") is None
                    else float(item["profit_margin"] * 100)
                ),
                "unknown_cost_count": item.get("unknown_cost_count", 0),
                "unknown_revenue_count": item.get("unknown_revenue_count", 0),
                "pending_count": item.get("pending_count", 0),
                "legacy_cost_count": item.get("legacy_cost_count", 0),
                "legacy_settlement_count": item.get("legacy_settlement_count", 0),
            }
            for item in economics["periods"]
        ]

    def _consumption_totals(modules: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "revenue_fen": float(sum(Decimal(str(m["revenue_fen"])) for m in modules)),
            "cost_fen": float(sum(Decimal(str(m["cost_fen"])) for m in modules)),
            "unknown_cost_count": sum(m["unknown_cost_count"] for m in modules),
            "pending_count": sum(m["pending_count"] for m in modules),
            "unknown_revenue_count": sum(m["unknown_revenue_count"] for m in modules),
        }

    cur_totals = _consumption_totals(cur_modules)
    prev_totals = _consumption_totals(prev_modules)
    for totals, source in ((cur_totals, economics), (prev_totals, prev_economics)):
        totals["legacy_cost_count"] = source["totals"].get("legacy_cost_count", 0)
        totals["legacy_settlement_count"] = source["totals"].get("legacy_settlement_count", 0)
    for totals in (cur_totals, prev_totals):
        complete = not any(
            totals[k]
            for k in (
                "unknown_cost_count",
                "unknown_revenue_count",
                "pending_count",
                "legacy_cost_count",
                "legacy_settlement_count",
            )
        )
        totals["gross_fen"] = (
            float(Decimal(str(totals["revenue_fen"])) - Decimal(str(totals["cost_fen"])))
            if complete
            else None
        )
        totals["margin_pct"] = (
            float(Decimal(str(totals["gross_fen"])) / Decimal(str(totals["revenue_fen"])) * 100)
            if complete and totals["revenue_fen"] > 0
            else None
        )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "prev_start": prev_start.isoformat(),
        "prev_end": prev_end.isoformat(),
        "metrics": {
            **cur_recharge,
            **cur_totals,
            "prepaid_credits": prepaid_credits,
            "prepaid_fen": prepaid_fen,
        },
        "prev": {
            **prev_recharge,
            **prev_totals,
            "prepaid_credits": prev_prepaid_credits,
            "prepaid_fen": prev_prepaid_fen,
        },
        "daily": daily,
        "modules": cur_modules,
        "top_customers": top_customers,
    }


# ---------------------------------------------------------------------------
# 资金概览（方案 P1 资金中心）：财务在一个入口看钱的进出。
# ---------------------------------------------------------------------------
_FUND_CHANNEL_LABELS = ("zpay", "wechat_native", "admin_adjustment")


@router.get("/funds/summary")
def funds_summary(start: date, end: date, _actor: AdminReader) -> dict[str, Any]:
    lower, upper = date_bounds(start, end)
    with pg_transaction(isolation="REPEATABLE READ") as conn:
        # 命名行访问统一走 BusinessConnection 包装（见 business_overview 注释）。
        business_conn = BusinessConnection.postgres(conn)
        rows = business_conn.execute(
            f"""
            SELECT provider, CASE WHEN {OFFLINE_CASH_EVIDENCE} THEN 'offline'
              ELSE payment_method END AS payment_method, COUNT(*) AS orders,
                   COALESCE(SUM(amount_fen), 0) AS amount_fen
            FROM recharge_orders ro
            WHERE status = 'PAID' AND paid_at IS NOT NULL AND amount_fen > 0
              AND {CASH_ORDER_ELIGIBLE}
              AND {utc_text_timestamp("paid_at")} >= %s
              AND {utc_text_timestamp("paid_at")} < %s
            GROUP BY provider, 2
            """,
            (lower, upper),
        ).fetchall()
        free_row = business_conn.execute(
            f"""
            SELECT COALESCE(SUM(credits), 0)
            FROM recharge_orders ro
            WHERE status = 'PAID' AND provider = 'admin_adjustment'
              AND (amount_fen = 0 OR {NONCASH_ADJUSTMENT})
              AND {utc_text_timestamp("paid_at")} >= %s
              AND {utc_text_timestamp("paid_at")} < %s
            """,
            (lower, upper),
        ).fetchone()
        assert free_row is not None
        unverified = business_conn.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(amount_fen),0) FROM recharge_orders ro
            WHERE provider='admin_adjustment' AND status='PAID' AND amount_fen>0
              AND NOT ({CASH_ORDER_ELIGIBLE}) AND NOT ({NONCASH_ADJUSTMENT})
              AND {utc_text_timestamp("paid_at")} >=%s
              AND {utc_text_timestamp("paid_at")} <%s""",
            (lower, upper),
        ).fetchone()
        refund_row = business_conn.execute(
            f"""
            SELECT COALESCE(SUM(-t.available_delta), 0)
            FROM wallet_transactions t JOIN users u ON u.id = t.user_id
            WHERE u.role = 'customer' AND t.type = 'REFUND'
              AND {utc_text_timestamp("t.created_at")} >= %s
              AND {utc_text_timestamp("t.created_at")} < %s
            """,
            (lower, upper),
        ).fetchone()
        assert refund_row is not None
        available_credits, reserved_credits = _customer_wallet_totals(business_conn)
        points_per_yuan = _points_per_yuan(business_conn)
        problems = _reconciliation_problem_count(business_conn)

    def _fen(credits: int) -> int | None:
        if points_per_yuan is None:
            return None
        return int(
            (Decimal(credits) * 100 / Decimal(points_per_yuan)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    physical_channels: list[dict[str, Any]] = [
        {
            "provider": str(item["provider"]),
            "method": (
                item["payment_method"]
                if item["payment_method"] in {"alipay", "wxpay", "offline"}
                else "unknown"
            ),
            "orders": int(item["orders"]),
            "amount_fen": int(item["amount_fen"]),
        }
        for item in (cast(dict[str, Any], dict(row)) for row in rows)
    ]
    # 通道承担多种支付方式；只使用支付核验后持久化的实际 payment_method，缺失即未知。
    method_totals: dict[str, dict[str, Any]] = {}
    provider_totals: dict[str, dict[str, Any]] = {}
    for item in physical_channels:
        for key, field, target in (
            (item["method"], "method", method_totals),
            (item["provider"], "provider", provider_totals),
        ):
            group = target.setdefault(key, {field: key, "orders": 0, "amount_fen": 0})
            group["orders"] += item["orders"]
            group["amount_fen"] += item["amount_fen"]
    by_channel = list(provider_totals.values())
    by_method = sorted(method_totals.values(), key=lambda item: item["method"])
    recharge_fen = sum(item["amount_fen"] for item in by_method)
    # 线下收款 = 管理员代开的已收款套餐（金额 > 0）；金额为 0 的同渠道行是赠送。
    offline_fen = sum(item["amount_fen"] for item in by_method if item["method"] == "offline")
    grant_credits = int(free_row[0] or 0)
    refund_credits = int(refund_row[0] or 0)
    refund_fen = _fen(refund_credits)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "recharge_fen": recharge_fen,
        "orders": sum(item["orders"] for item in by_channel),
        "offline_fen": offline_fen,
        "grant_credits": grant_credits,
        "refund_credits": refund_credits,
        "refund_fen": refund_fen,
        # 净收入 = 充值实收 − 退款扣减；兑换比例未配置时退款只有积分数。
        "net_fen": None if refund_fen is None else recharge_fen - refund_fen,
        "by_channel": by_channel,
        "by_method": by_method,
        "unverified_manual_orders": int(unverified[0]) if unverified else 0,
        "unverified_manual_fen": int(unverified[1]) if unverified else 0,
        "prepaid_credits": available_credits + reserved_credits,
        "prepaid_fen": _fen(available_credits + reserved_credits),
        "reconciliation_problems": problems,
    }
