"""One Shanghai month and reconciled supplier-cost scope for display and scheduling."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.admin_dates import SHANGHAI, utc_timestamp_sql
from app.db_portable import BusinessConnection


def collection_budget(conn: BusinessConnection, *, now: datetime | None = None) -> dict[str, Any]:
    current = (now or conn.execute("SELECT now()").fetchone()[0]).astimezone(SHANGHAI)
    lower = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    upper = (
        lower.replace(year=lower.year + 1, month=1)
        if lower.month == 12
        else lower.replace(month=lower.month + 1)
    )
    completed = utc_timestamp_sql("a.completed_at")
    created = utc_timestamp_sql("a.created_at")
    row = conn.execute(
        f"""SELECT COALESCE(sum(a.effective_cost_fen),0) AS known,
          count(*) FILTER(WHERE a.effective_cost_fen IS NULL AND a.state<>'PENDING') AS unknown,
          count(*) FILTER(WHERE a.state='PENDING') AS pending
        FROM billing_effective_attempts a JOIN billing_operations o ON o.id=a.operation_id
        WHERE o.user_id IS NULL AND (o.service='viral_data' OR o.collection_batch_id IS NOT NULL)
          AND COALESCE({completed},{created}) >= %s
          AND COALESCE({completed},{created}) < %s""",
        (lower.astimezone(UTC), upper.astimezone(UTC)),
    ).fetchone()
    control = conn.execute(
        "SELECT monthly_budget_fen FROM viral_runtime_controls WHERE id=1"
    ).fetchone()
    budget = Decimal(control[0]) if control and control[0] is not None else None
    known = Decimal(row["known"])
    status = (
        "unlimited"
        if budget is None
        else "exhausted"
        if known >= budget
        else "warning"
        if known * 100 >= budget * 80
        else "normal"
    )
    return {
        "month_spend_fen": float(known),
        "month_unknown_cost_count": int(row["unknown"]),
        "month_pending_cost_count": int(row["pending"]),
        "month_total_cost_fen": None if row["unknown"] or row["pending"] else float(known),
        "budget_status": status,
        "budget_usage_percent": float(known * 100 / budget) if budget else None,
        "budget_period_start": lower.isoformat(),
        "budget_period_end": upper.isoformat(),
        "budget_scope": (
            "上海自然月的平台数据接口成本，含定时/手动采集、后台实时搜索和数据刷新；"
            "采用财务核对后的供应商成本，不重复计入客户积分收入。未知和进行中费用单列，"
            "达到预算80%提醒、已知成本达到100%暂停新定时批次，手动采集仍可知情执行。"
            "下载流量及存储未接入计量，另行核对。"
        ),
    }
