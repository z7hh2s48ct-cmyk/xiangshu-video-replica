"""子账号月度额度：读取、当月用量聚合与超限强制（CUSTOMER-CENTER-V2 Phase 3a）.

母账号通过 ``sub_account_quotas``（迁移 20260921T1200）给某个子账号设一行
「每月最多消耗的积分」；无行 = 不限。当月用量不落库，实时聚合
``wallet_transactions``：

- 白名单 ``type IN ('RESERVE', 'RELEASE')`` 且 ``actor_user_id = 子账号``：
  RESERVE 的 ``available_delta = -credits``（预扣），RELEASE 的 ``= +refund``
  （结算退回）。SETTLE 的 ``available_delta`` 恒 0，不参与。
- 口径 = ``-SUM(available_delta)``：在途（已预扣未结算）也占用额度（保守，
  防并发刷额度），结算退回即时释放（最终只按实际结算量计）。
- 月度边界按上海自然月（``admin_dates.SHANGHAI``，与「上海业务日」惯例一致）。
  月份归属锚定**操作发起月**：带 ``billing_operation_id`` 的流水按
  ``billing_operations.created_at``（timestamptz）判定——跨月 RELEASE 退回的
  是原操作所属月的额度，不会在次月凭空多出额度；老式（无 operation）流水回退
  到 ``wallet_transactions.created_at``（text，经 ``utc_timestamp_sql`` 归一）。

超限即冻结：``accept_operation`` 在预扣前调用 ``enforce_sub_account_quota``，
超限抛 403 ``SUB_ACCOUNT_QUOTA_EXCEEDED``。不引入持久「冻结状态」——判定
始终由「当月已用 + 本次预扣 > 额度」实时得出；母账号与无限额子账号直通。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import HTTPException

from app.admin_dates import SHANGHAI, utc_timestamp_sql

# 防呆上限：一次设置最多 10 亿积分（防误输入的护栏，不是业务天花板）。
MAX_MONTHLY_QUOTA_CREDITS = 1_000_000_000


class QuotaConnection(Protocol):
    """额度读写需要的最小连接面。

    与 ``content_store.SqlConnection`` 同理：``params`` 取 ``Any``（参数逆变，
    收窄会让某一侧连接无法通过协议匹配）——本模块同时被 ``usage_billing`` 的
    ``BusinessConnection`` 与客户路由里的裸 ``psycopg`` 连接调用。
    """

    def execute(self, sql: str, params: Any = ...) -> Any: ...


def shanghai_month_start_iso(now: datetime | None = None) -> str:
    """当前上海自然月起点（月初 00:00 +08:00）的 UTC ISO 文本。"""
    current = (now or datetime.now(tz=UTC)).astimezone(SHANGHAI)
    month_start = datetime(current.year, current.month, 1, tzinfo=SHANGHAI)
    return month_start.astimezone(UTC).isoformat()


def read_monthly_quota(conn: QuotaConnection, user_id: str) -> int | None:
    """该账号的月度额度；``None`` = 不限（无行）。"""
    row = conn.execute(
        "SELECT monthly_credits FROM sub_account_quotas WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def read_quota_used(
    conn: QuotaConnection,
    actor_id: str,
    *,
    month_start_iso: str | None = None,
) -> int:
    """本月该 actor 的额度占用（预扣累计 − 退回累计；在途计入；跨月退回归原月）。"""
    row = conn.execute(
        _USAGE_SQL_ONE,
        (actor_id, month_start_iso or shanghai_month_start_iso()),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def read_quota_used_map(
    conn: QuotaConnection,
    actor_ids: Sequence[str],
    *,
    month_start_iso: str | None = None,
) -> dict[str, int]:
    """批量取多个 actor 的本月额度占用（列表端点防 N+1）；缺省视为 0。"""
    unique_ids = [str(actor_id) for actor_id in dict.fromkeys(actor_ids)]
    if not unique_ids:
        return {}
    rows = conn.execute(
        _USAGE_SQL_MANY,
        (unique_ids, month_start_iso or shanghai_month_start_iso()),
    ).fetchall()
    usage = {str(row[0]): int(row[1]) for row in rows}
    return {actor_id: usage.get(actor_id, 0) for actor_id in unique_ids}


def enforce_sub_account_quota(
    conn: QuotaConnection,
    *,
    actor_id: str,
    additional_credits: int,
) -> None:
    """预扣前强制：``已用 + 本次 > 额度`` → 403；母账号/无限额子账号 no-op。

    调用点必须已经持有该 actor 的用户级锁（``accept_operation`` 的
    ``pg_advisory_xact_lock('billing:user:<actor>')``）：并发预扣被串行化后，
    同事务内的聚合读不重不漏。
    """
    if additional_credits <= 0:
        return
    quota = read_monthly_quota(conn, actor_id)
    if quota is None:
        return
    used = read_quota_used(conn, actor_id)
    if used + additional_credits > quota:
        raise HTTPException(
            403,
            detail={
                "code": "SUB_ACCOUNT_QUOTA_EXCEEDED",
                "message": (
                    f"本月额度已用完（已用 {used} / 限额 {quota} 积分），请联系母账号调整额度。"
                ),
            },
        )


# 月份归属锚定「操作发起月」：带 operation 的流水（``_ledger`` 对同一计费单的
# RESERVE/RELEASE 都写同一 ``billing_operation_id``）按 billing_operations 的
# timestamptz ``created_at`` 判定——跨月退回调整的是原操作所属月。老式无
# operation 流水回退到 ``wallet_transactions.created_at``（text 时间戳，经
# utc_timestamp_sql 归一为 timestamptz）。列名是源码常量，白名单校验由 helper
# 完成。
_USAGE_EXPRESSION = utc_timestamp_sql("wt.created_at")
_USAGE_MONTH_ANCHOR = f"COALESCE(o.created_at, {_USAGE_EXPRESSION})"
_USAGE_SQL_ONE = (
    "SELECT COALESCE(-SUM(wt.available_delta), 0) FROM wallet_transactions wt "
    "LEFT JOIN billing_operations o ON o.id = wt.billing_operation_id "
    "WHERE wt.actor_user_id = %s AND wt.type IN ('RESERVE', 'RELEASE') "
    f"AND {_USAGE_MONTH_ANCHOR} >= %s::timestamptz"
)
_USAGE_SQL_MANY = (
    "SELECT wt.actor_user_id, COALESCE(-SUM(wt.available_delta), 0) "
    "FROM wallet_transactions wt "
    "LEFT JOIN billing_operations o ON o.id = wt.billing_operation_id "
    "WHERE wt.actor_user_id = ANY(%s) AND wt.type IN ('RESERVE', 'RELEASE') "
    f"AND {_USAGE_MONTH_ANCHOR} >= %s::timestamptz "
    "GROUP BY wt.actor_user_id"
)
