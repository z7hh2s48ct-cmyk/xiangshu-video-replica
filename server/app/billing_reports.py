"""Operation-level economics; unknown evidence never becomes zero profit."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.db_portable import BusinessConnection
from app.sql_pagination import PAGE_CLAUSE

# A delivered request that took the customer's credits always ran a provider call.
# Recording none is missing evidence, not proof the call was free: pricing the
# absence at zero publishes full-margin profit for any subject whose metering was
# never wired, and does it silently.
#
# Excluded, because each legitimately reaches a terminal state with no attempt:
# failed and free requests never reach a paid provider, and a shared-collection
# charge carries its cost on the platform request it was split from, which is why
# its profit is withheld separately as ``shared_cost_unallocated``.
#
# ``{calls}`` is the aggregated attempt count of the surrounding query.
UNMETERED_DELIVERED_CHARGE = (
    "(o.state='SUCCEEDED' AND o.charged_credits>0 AND COALESCE({calls},0)=0"
    " AND o.collection_batch_id IS NULL)"
)


def date_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    if end.year > 9998:
        raise HTTPException(422, detail="统计结束年份最多为 9998")
    if end < start:
        raise HTTPException(422, detail="结束日期不能早于开始日期")
    zone = ZoneInfo("Asia/Shanghai")
    return datetime.combine(start, time.min, zone), datetime.combine(
        end + timedelta(days=1), time.min, zone
    )


def operation_rows(
    conn: BusinessConnection,
    *,
    start: date,
    end: date,
    user_id: str | None = None,
    service: str | None = None,
    module: str | None = None,
    provider: str | None = None,
    limit: int = 5000,
    offset: int = 0,
    operation_id: str | None = None,
) -> list[dict[str, Any]]:
    lower, upper = date_bounds(start, end)
    # Aggregate provider attempts before joining the single revenue fact.
    unmetered = UNMETERED_DELIVERED_CHARGE.format(calls="c.attempt_count")
    rows = conn.execute(
        f"""
        SELECT o.*, COALESCE(u.username,'平台后台') AS username, COALESCE(c.attempt_count,0) AS
          attempt_count,
          COALESCE(c.known_cost_fen,0) AS known_cost_fen,
          COALESCE(c.unknown_cost_count,0)+CASE WHEN {unmetered} THEN 1 ELSE 0 END AS
            unknown_cost_count,
          CASE WHEN COALESCE(c.unknown_cost_count,0)=0 AND o.state<>'PENDING'
            AND NOT {unmetered}
            THEN COALESCE(c.known_cost_fen,0) ELSE NULL END AS cost_fen,
          count(*) OVER() AS total_count
        FROM billing_operations o LEFT JOIN users u ON u.id=o.user_id
        LEFT JOIN (
          SELECT operation_id,count(*) AS attempt_count,sum(effective_cost_fen) AS known_cost_fen,
            count(*) FILTER(WHERE effective_cost_fen IS NULL) AS unknown_cost_count
          FROM billing_effective_attempts GROUP BY operation_id
        ) c ON c.operation_id=o.id
        WHERE COALESCE(o.completed_at,o.created_at)>=%s AND COALESCE(o.completed_at,
          o.created_at)<%s
          AND (%s::text IS NULL OR o.user_id=%s) AND (%s::text IS NULL OR o.service=%s)
          AND (%s::text IS NULL OR o.module=%s) AND (%s::text IS NULL OR o.id=%s)
          AND (%s::text IS NULL OR EXISTS(SELECT 1 FROM billing_attempts a
            JOIN billing_operations parent ON parent.id=a.operation_id
            WHERE (parent.id=o.id OR (o.collection_batch_id IS NOT NULL
              AND parent.id=o.source_id AND parent.collection_batch_id=o.collection_batch_id))
            AND a.provider=%s))
        ORDER BY COALESCE(o.completed_at,o.created_at) DESC,o.id DESC {PAGE_CLAUSE}
    """,
        (
            lower,
            upper,
            user_id,
            user_id,
            service,
            service,
            module,
            module,
            operation_id,
            operation_id,
            provider,
            provider,
            limit,
            offset,
        ),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        known = (
            item["cost_fen"] is not None
            and item["revenue_fen"] is not None
            and item["state"] != "PENDING"
        )
        item["profit_fen"] = item["revenue_fen"] - item["cost_fen"] if known else None
        item["shared_cost_unallocated"] = bool(item["collection_batch_id"] and item["user_id"])
        if item["shared_cost_unallocated"]:
            item["profit_fen"] = None
        result.append(item)
    return result


def source_action_rows(
    conn: BusinessConnection,
    *,
    start: date,
    end: date,
    user_id: str | None = None,
    service: str | None = None,
    module: str | None = None,
    provider: str | None = None,
    source_id: str | None = None,
    platform: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """P1-5 业务动作全景：把一个 ``source_id`` 下的所有请求折成一行。

    一次业务动作可能横跨多个科目（主科目 + 内部修复等）、多次重试，还会带着
    质检这类辅助调用；逐条列出请求时管理员要自己心算，动作级账就对不上。
    聚合口径与 ``operation_rows`` 一致：成本证据不齐（未结算 / 调用成本待核对 /
    已交付却没有任何调用证据）时整行成本为 None，绝不把缺证据当零成本。

    分组键是 ``(user_id, source_id)``：``user_id`` 为 NULL 的平台请求单独成行。
    成本列里 ``inspection_cost_fen`` 是质检（``quality_inspection``）调用成本的
    小计，已含在 ``known_cost_fen`` 内，便于核对质检花了多少钱。
    """
    lower, upper = date_bounds(start, end)
    unmetered = UNMETERED_DELIVERED_CHARGE.format(calls="COALESCE(c.attempt_count,0)")
    rows = conn.execute(
        f"""
        WITH scoped AS (
          SELECT o.* FROM billing_operations o
          WHERE COALESCE(o.completed_at,o.created_at)>=%s AND COALESCE(o.completed_at,
            o.created_at)<%s
            AND (%s::text IS NULL OR o.user_id=%s)
            AND (NOT %s::boolean OR o.user_id IS NULL)
            AND (%s::text IS NULL OR o.service=%s)
            AND (%s::text IS NULL OR o.module=%s)
            AND (%s::text IS NULL OR o.source_id=%s)
            AND (%s::text IS NULL OR EXISTS(SELECT 1 FROM billing_attempts a
              JOIN billing_operations parent ON parent.id=a.operation_id
              WHERE (parent.id=o.id OR (o.collection_batch_id IS NOT NULL
                AND parent.id=o.source_id AND parent.collection_batch_id=o.collection_batch_id))
              AND a.provider=%s))
        ), calls AS (
          SELECT operation_id,count(*) AS attempt_count,
            count(*) FILTER(WHERE effective_cost_fen IS NULL) AS unknown_attempt_count,
            sum(effective_cost_fen) AS known_cost,
            count(*) FILTER(WHERE service='quality_inspection') AS inspection_attempt_count,
            count(*) FILTER(WHERE service='quality_inspection' AND effective_cost_fen IS NULL)
              AS inspection_unknown_count,
            sum(effective_cost_fen) FILTER(WHERE service='quality_inspection')
              AS inspection_cost
          FROM billing_effective_attempts GROUP BY operation_id
        )
        SELECT o.user_id,o.source_id,COALESCE(u.username,'平台后台') AS username,
          count(*) AS operation_count,
          count(*) FILTER(WHERE o.state='PENDING') AS pending_count,
          count(*) FILTER(WHERE o.state IN ('FAILED','CANCELLED')) AS failed_count,
          sum(o.reserved_credits) AS reserved_credits,sum(o.charged_credits) AS charged_credits,
          sum(COALESCE(c.attempt_count,0)) AS attempt_count,
          sum(COALESCE(c.unknown_attempt_count,0)) AS unknown_attempt_count,
          sum(COALESCE(c.inspection_attempt_count,0)) AS inspection_attempt_count,
          sum(COALESCE(c.inspection_unknown_count,0)) AS inspection_unknown_count,
          sum(COALESCE(c.known_cost,0)) AS known_cost_fen,
          sum(COALESCE(c.inspection_cost,0)) AS inspection_cost_fen,
          count(*) FILTER(WHERE {unmetered})+sum(COALESCE(c.unknown_attempt_count,0))
            AS unknown_cost_count,
          array_agg(DISTINCT o.service ORDER BY o.service) AS services,
          array_agg(DISTINCT o.module ORDER BY o.module) AS modules,
          min(COALESCE(o.completed_at,o.created_at)) AS first_at,
          max(COALESCE(o.completed_at,o.created_at)) AS last_at,
          count(*) OVER() AS total_count
        FROM scoped o LEFT JOIN users u ON u.id=o.user_id
        LEFT JOIN calls c ON c.operation_id=o.id
        GROUP BY o.user_id,o.source_id,u.username
        ORDER BY max(COALESCE(o.completed_at,o.created_at)) DESC,o.source_id DESC
        {PAGE_CLAUSE}
    """,
        (
            lower,
            upper,
            user_id,
            user_id,
            platform,
            service,
            service,
            module,
            module,
            source_id,
            source_id,
            provider,
            provider,
            limit,
            offset,
        ),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["pending_count"] = int(item["pending_count"] or 0)
        item["failed_count"] = int(item["failed_count"] or 0)
        item["attempt_count"] = int(item["attempt_count"] or 0)
        item["unknown_cost_count"] = int(item["unknown_cost_count"] or 0)
        item["inspection_attempt_count"] = int(item["inspection_attempt_count"] or 0)
        item["inspection_unknown_count"] = int(item["inspection_unknown_count"] or 0)
        item["known_cost_fen"] = item["known_cost_fen"] or 0
        item["inspection_cost_fen"] = item["inspection_cost_fen"] or 0
        complete = not (item["pending_count"] or item["unknown_cost_count"])
        item["cost_fen"] = item["known_cost_fen"] if complete else None
        if item["inspection_unknown_count"]:
            item["inspection_cost_fen"] = None
        result.append(item)
    return result


def source_action_detail(
    conn: BusinessConnection,
    *,
    source_id: str,
    user_id: str | None = None,
    platform: bool = False,
) -> dict[str, Any] | None:
    """P1-5：一个业务动作的全景 —— 动作汇总 + 每个科目请求及其供应商调用。

    作用域必须明确（客户 ``user_id`` 或 ``platform``），否则同一动作编号在多个
    用户名下会混成一份无法定责的明细。共享采集的公共成本记在平台请求行上，
    客户行的 ``source_id`` 指向该请求编号，但不把平台成本并进客户行。
    """
    actions = source_action_rows(
        conn,
        start=date(2000, 1, 1),
        end=date(9998, 12, 31),
        user_id=user_id,
        source_id=source_id,
        platform=platform,
    )
    if not actions:
        return None
    operations = []
    for row in conn.execute(
        "SELECT o.*,COALESCE(u.username,'平台后台') AS username FROM billing_operations o "
        "LEFT JOIN users u ON u.id=o.user_id WHERE o.source_id=%s "
        "AND (%s::text IS NULL OR o.user_id=%s) AND (NOT %s::boolean OR o.user_id IS NULL)"
        " ORDER BY o.created_at,o.id",
        (source_id, user_id, user_id, platform),
    ).fetchall():
        item = dict(row)
        item["attempts"] = [
            dict(attempt)
            for attempt in conn.execute(
                "SELECT * FROM billing_effective_attempts WHERE operation_id=%s ORDER BY "
                "created_at,id",
                (item["id"],),
            ).fetchall()
        ]
        operations.append(item)
    return {"action": actions[0], "operations": operations, "scopes": actions}


def statistics(
    conn: BusinessConnection,
    *,
    start: date,
    end: date,
    grain: str = "day",
    user_id: str | None = None,
    service: str | None = None,
    module: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    if grain not in {"day", "week", "month", "year"}:
        raise HTTPException(422, detail="不支持的统计周期")
    lower, upper = date_bounds(start, end)
    unmetered = UNMETERED_DELIVERED_CHARGE.format(calls="c.calls")
    rows = conn.execute(
        f"""
        WITH costs AS (
          SELECT operation_id,count(*) AS calls,sum(effective_cost_fen) AS known_cost,
            count(*) FILTER(WHERE effective_cost_fen IS NULL) AS unknown_cost
          FROM billing_effective_attempts GROUP BY operation_id
        ), facts AS (
          SELECT o.*,COALESCE(c.calls,0) AS calls,COALESCE(c.known_cost,0) AS known_cost,
            COALESCE(c.unknown_cost,0)+CASE WHEN {unmetered} THEN 1 ELSE 0 END AS unknown_cost
          FROM billing_operations o LEFT JOIN costs c ON c.operation_id=o.id
          WHERE COALESCE(o.completed_at,o.created_at)>=%s AND COALESCE(o.completed_at,
            o.created_at)<%s
            AND (%s::text IS NULL OR o.user_id=%s) AND (%s::text IS NULL OR o.service=%s)
            AND (%s::text IS NULL OR o.module=%s)
            AND (%s::text IS NULL OR EXISTS(SELECT 1 FROM billing_attempts a
              JOIN billing_operations parent ON parent.id=a.operation_id
              WHERE (parent.id=o.id OR (o.collection_batch_id IS NOT NULL
                AND parent.id=o.source_id AND parent.collection_batch_id=o.collection_batch_id))
              AND a.provider=%s))
        )
        SELECT date_trunc(%s,COALESCE(completed_at,created_at) AT TIME ZONE 'Asia/Shanghai')
          AS period,
          count(*) AS operation_count, count(*) FILTER(WHERE charged_credits>0) AS charged_count,
          count(*) FILTER(WHERE reserved_credits=0) AS free_count,
          count(*) FILTER(WHERE state='PENDING') AS pending_count,
          sum(calls) AS provider_call_count, sum(charged_credits) AS charged_credits,
          sum(CASE WHEN state<>'PENDING' THEN reserved_credits-charged_credits ELSE 0 END) AS
            refunded_credits,
          sum(known_cost) AS known_cost_fen,sum(revenue_fen) AS known_revenue_fen,
          count(*) FILTER(WHERE unknown_cost>0) AS unknown_cost_count,
          count(*) FILTER(WHERE revenue_fen IS NULL) AS unknown_revenue_count,
          count(*) FILTER(WHERE collection_batch_id IS NOT NULL AND user_id IS NOT NULL) AS
            shared_collection_charge_count,
          sum(CASE WHEN reserved_credits=0 OR (state<>'PENDING' AND charged_credits=0) THEN
            known_cost ELSE 0 END) AS platform_cost_fen,
          sum(CASE WHEN unit='second' THEN actual_units ELSE 0 END) AS seconds,
          sum(CASE WHEN unit='image' THEN actual_units ELSE 0 END) AS images,
          sum(CASE WHEN unit='call' AND user_id IS NOT NULL THEN actual_units ELSE 0 END) AS calls,
          sum(CASE WHEN service IN ('video_768p','video_2k','oral') AND state='SUCCEEDED'
            THEN actual_units ELSE 0 END) AS video_seconds
        FROM facts GROUP BY GROUPING SETS ((period),()) ORDER BY period NULLS LAST
    """,
        (
            lower,
            upper,
            user_id,
            user_id,
            service,
            service,
            module,
            module,
            provider,
            provider,
            grain,
        ),
    ).fetchall()
    metrics = {
        row["period"].date().isoformat() if row["period"] else None: {
            key: value or 0 for key, value in dict(row).items() if key != "period"
        }
        for row in rows
    }
    # Old facts have no proven operation/provider attribution. Expose their date/customer
    # coverage even under service filters; never price them with today's tariff or count
    # the compatibility mirror of a linked attempt/settlement a second time.
    legacy_rows = conn.execute(
        """
        WITH legacy AS (
          SELECT occurred_at AS at,user_id,1 AS costs,0 AS settlements
          FROM operation_cost_records WHERE billing_attempt_id IS NULL
            AND occurred_at >= %s AND occurred_at < %s
          UNION ALL
          SELECT created_at::timestamp AT TIME ZONE 'UTC',user_id,0,1
          FROM wallet_transactions WHERE type='SETTLE' AND billing_operation_id IS NULL
            AND (created_at::timestamp AT TIME ZONE 'UTC') >= %s
            AND (created_at::timestamp AT TIME ZONE 'UTC') < %s
        )
        SELECT date_trunc(%s,at AT TIME ZONE 'Asia/Shanghai') AS period,
          sum(costs) AS legacy_cost_count,sum(settlements) AS legacy_settlement_count
        FROM legacy WHERE (%s::text IS NULL OR user_id=%s)
        GROUP BY GROUPING SETS ((period),())
        """,
        (lower, upper, lower, upper, grain, user_id, user_id),
    ).fetchall()
    empty = dict.fromkeys(metrics[None], 0)
    for row in legacy_rows:
        period = row["period"].date().isoformat() if row["period"] else None
        item = metrics.setdefault(period, empty.copy())
        item["legacy_cost_count"] = int(row["legacy_cost_count"] or 0)
        item["legacy_settlement_count"] = int(row["legacy_settlement_count"] or 0)
    for period, item in metrics.items():
        item["period"] = period
        item.setdefault("legacy_cost_count", 0)
        item.setdefault("legacy_settlement_count", 0)
        cost_complete = not (
            item["unknown_cost_count"] or item["pending_count"] or item["legacy_cost_count"]
        )
        revenue_complete = not (
            item["unknown_revenue_count"]
            or item["pending_count"]
            or item["legacy_settlement_count"]
        )
        revenue, cost = item["known_revenue_fen"], item["known_cost_fen"]
        item["cost_fen"] = cost if cost_complete else None
        item["revenue_fen"] = revenue if revenue_complete else None
        complete = (
            cost_complete
            and revenue_complete
            and not (user_id is not None and item["shared_collection_charge_count"])
        )
        item["profit_fen"] = revenue - cost if complete else None
        item["profit_margin"] = (revenue - cost) / revenue if complete and revenue else None
    return {
        "timezone": "Asia/Shanghai",
        "grain": grain,
        "start": start,
        "end": end,
        "totals": metrics[None],
        "periods": [metrics[key] for key in sorted(key for key in metrics if key is not None)],
        "legacy_scope": "date_and_customer",
        "basis": "请求结算归属周期；未结算请求按受理时间列示。成本或收入证据未齐时利润待核对。"
        "共享采集成本只记在平台请求，筛选单个客户时公共成本未分摊，请以采集批次核算利润。"
        "成本口径为上游接口调用，不含云存储与支付通道费用，此处利润为毛利而非净利。",
    }
