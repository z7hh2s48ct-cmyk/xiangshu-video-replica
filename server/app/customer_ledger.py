"""客户消费记录：把同一计费周期的暂扣 / 实扣 / 退回合并成一条「任务」来读。

为什么在服务端合并：账本是按行分页的，而一条任务的暂扣行与退回行在时间上相隔几分钟，
中间夹着同批其他任务的几十行——在客户端按当前页折叠，就会出现「退回在第 1 页、暂扣
落在第 2 页」，第 2 页上一条已经全额退回的任务只剩一行「暂扣 -8 积分」，用户于是只看到
扣钱、看不到退回。合并必须发生在分页**之前**，让一页里的每一条都是完整的一笔。

原始逐笔流水（``/customer/wallet/transactions``、CSV 导出）保持不变，仍是对账的事实来源。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, cast

import psycopg

from app.billing_catalog import SERVICES
from app.sql_pagination import PAGE_CLAUSE
from app.wallet_routes import (
    LedgerOutcome,
    WalletLedgerCounts,
    WalletLedgerEntry,
    WalletLedgerPage,
    WalletTransactionResponse,
    pricing_breakdown,
)

# 参与「计费周期」合并的三种流水；入账、历史转换、退款调账各自独立成行。
CYCLE_TYPES: tuple[str, ...] = ("RESERVE", "SETTLE", "RELEASE")
_CYCLE_TYPE_SQL = "('RESERVE','SETTLE','RELEASE')"
# 周期内按资金实际走向排（同一事务写入的行时间戳相同，不能靠时间序）。
_TYPE_ORDER = {"RESERVE": 0, "SETTLE": 1, "RELEASE": 2}

LedgerOutcomeFilter = Literal["pending", "completed", "refunded", "posted"]
# 筛选项 → 条目结果。「有退回」同时包含部分成功与整笔退回。
_FILTER_OUTCOMES: dict[str, tuple[str, ...]] = {
    "pending": ("PENDING",),
    "completed": ("COMPLETED",),
    "refunded": ("PARTIAL", "FAILED"),
    "posted": ("POSTED",),
}

LedgerBusiness = Literal[
    "video",
    "oral",
    "recharge",
    "character",
    "first_frame",
    "analysis",
    "rewrite",
    "asr",
    "link_resolution",
    "prompt_optimize",
    "avatar_clone",
    "voice_clone",
    "viral_data",
]

# 逐笔流水行的读取口径：旧的逐笔列表与新的合并列表共用，两边字段不会漂移。
LEDGER_ROW_FROM = (
    " FROM wallet_transactions wt LEFT JOIN customer_api_keys k ON k.id = "
    "wt.api_key_id AND k.user_id = wt.user_id "
    "LEFT JOIN billing_operations op ON op.id=wt.billing_operation_id "
    "AND (op.user_id=wt.user_id OR op.user_id=wt.actor_user_id) "
    "LEFT JOIN recharge_orders credit_order ON credit_order.id = wt.recharge_order_id "
    "AND credit_order.user_id = wt.user_id "
    "LEFT JOIN admin_adjustments credit_adjustment ON "
    "credit_adjustment.recharge_order_id = credit_order.id "
    "AND credit_adjustment.target_user_id = wt.user_id WHERE "
)

LEDGER_ROW_SELECT = """
            SELECT wt.id, wt.user_id, wt.type, wt.available_delta, wt.reserved_delta,
                   wt.recharge_order_id, wt.task_id, wt.oral_task_id,
                   wt.billing_round, wt.created_at,
                   wt.api_key_id, k.token_group_id, k.label, k.credential_version, wt.auth_source,
                   wt.pricing_snapshot_json,
                   (SELECT task.batch_id FROM generation_tasks task WHERE task.id = wt.task_id),
                   CASE WHEN wt.type = 'CHARGE' THEN
                     COALESCE(credit_adjustment.source_document_type, credit_order.provider)
                   END, wt.billing_operation_id,
                   (SELECT o.service FROM billing_operations o WHERE o.id=wt.billing_operation_id),
                   wt.actor_user_id,
                   (SELECT u.display_name FROM users u WHERE u.id=wt.actor_user_id),
                   -- P1-7：配对态按全量账本算，组内每行同值。分页把 RESERVE 与
                   -- 结算/退回切开、或按类型筛选后只剩一行时，界面仍知道它是
                   -- 「已结算」还是「退回」；(billing_operation_id,type) 索引支撑
                   -- 这两个 EXISTS。
                   CASE
                     WHEN wt.billing_operation_id IS NULL THEN NULL
                     WHEN EXISTS (
                       SELECT 1 FROM wallet_transactions settled
                       WHERE settled.billing_operation_id = wt.billing_operation_id
                         AND settled.type = 'SETTLE'
                     ) THEN 'SETTLED'
                     WHEN EXISTS (
                       SELECT 1 FROM wallet_transactions released
                       WHERE released.billing_operation_id = wt.billing_operation_id
                         AND released.type = 'RELEASE'
                     ) THEN 'RELEASED'
                     ELSE 'PENDING'
                   END
            """


def ledger_filter_clauses(
    *,
    wallet_owner_id: str,
    sub_account_id: str | None,
    token_group_id: str | None,
    auth_source: str | None,
    transaction_type: str | None,
    business: str | None,
    started_at: datetime | None,
    ended_at: datetime | None,
) -> tuple[list[str], list[object]]:
    """流水筛选 → SQL 子句与参数（**列表与 CSV 导出共用**）。

    这两条路径此前各拼一套，于是同一组筛选在两边语义不同：导出「视频生成」查
    ``op.service = 'video'`` 而列表查 ``task_id IS NOT NULL OR service IN
    (video_768p, video_2k)``；``historical`` 一边映射成 ``IS NULL`` 一边做等值比较；
    时间上界一边开区间一边闭区间。客户看到的现象是「界面有行、导出的 CSV 只有表头」。
    共用一份是唯一能让两边不漂移的写法——新增筛选项只需要改这里一处。
    """
    clauses = ["wt.user_id = %s"]
    params: list[object] = [wallet_owner_id]
    if sub_account_id:
        clauses.append("wt.actor_user_id = %s")
        params.append(sub_account_id)
    if token_group_id:
        clauses.append("k.token_group_id = %s")
        params.append(token_group_id)
    if auth_source == "historical":
        clauses.append("wt.auth_source IS NULL")
    elif auth_source:
        clauses.append("wt.auth_source = %s")
        params.append(auth_source)
    if transaction_type:
        clauses.append("wt.type = %s")
        params.append(transaction_type)
    if business:
        clauses.append(
            {
                "video": "(wt.task_id IS NOT NULL OR op.service IN ('video_768p','video_2k'))",
                "oral": "(wt.oral_task_id IS NOT NULL OR op.service = 'oral')",
                "recharge": "wt.type = 'CHARGE'",
            }.get(business, "op.service = %s")
        )
        if business not in {"video", "oral", "recharge"}:
            params.append(business)
    if started_at:
        clauses.append("wt.created_at::timestamptz >= %s")
        params.append(started_at)
    if ended_at:
        # 开区间：结束日期在调用方已 +1 天，边界那一瞬不该算进来。
        clauses.append("wt.created_at::timestamptz < %s")
        params.append(ended_at)
    return clauses, params


def ledger_row_response(row: Sequence[Any]) -> WalletTransactionResponse:
    """One customer ledger row, priced by the retail side of its frozen snapshot.

    P0-3: the customer must be able to check a delta against the unit price,
    usage, discount and rounding it was priced with. The projection is a
    whitelist — the snapshot's cost side never crosses this boundary.

    P1-7: ``pair_state`` names the row's billing-cycle group state so the
    client can fold RESERVE→SETTLE/RELEASE into one entry.
    """
    pricing = pricing_breakdown(json.loads(row[15]) if row[15] else None)
    return WalletTransactionResponse(
        id=str(row[0]),
        user_id=str(row[1]),
        type=row[2],
        available_delta=int(row[3]),
        reserved_delta=int(row[4]),
        recharge_order_id=str(row[5]) if row[5] is not None else None,
        task_id=str(row[6]) if row[6] is not None else None,
        oral_task_id=str(row[7]) if row[7] is not None else None,
        billing_round=int(row[8]) if row[8] is not None else None,
        created_at=str(row[9]),
        api_key_id=row[10],
        token_group_id=row[11],
        token_label=row[12],
        credential_version=row[13],
        auth_source=row[14],
        credit_price_version=pricing.version if pricing else None,
        generation_batch_id=row[16],
        credit_source=row[17],
        billing_operation_id=row[18],
        service=row[19],
        service_name=SERVICES[row[19]].name if row[19] in SERVICES else None,
        actor_user_id=str(row[20]) if row[20] is not None else None,
        actor_name=row[21],
        pricing=pricing,
        pair_state=row[22],
    )


# 条目键：计费周期用 operation id，其余流水用自己的行 id（两者都是 UUID，不会相撞）。
_ENTRY_KEY_SQL = (
    f"CASE WHEN wt.billing_operation_id IS NOT NULL AND wt.type IN {_CYCLE_TYPE_SQL} "
    "THEN wt.billing_operation_id ELSE wt.id END"
)
_KEYED_FROM = (
    " FROM wallet_transactions wt "
    "LEFT JOIN customer_api_keys k ON k.id = wt.api_key_id AND k.user_id = wt.user_id "
    "LEFT JOIN billing_operations op ON op.id = wt.billing_operation_id "
    "AND (op.user_id = wt.user_id OR op.user_id = wt.actor_user_id) WHERE "
)


def _has_row_sql(entry_key: str, ledger_type: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM wallet_transactions x "
        f"WHERE x.billing_operation_id = {entry_key} AND x.type = '{ledger_type}')"
    )


def disable_jit(conn: psycopg.Connection[Any]) -> None:
    """本事务内关闭 PostgreSQL 的 JIT 编译（``SET LOCAL``，事务结束自动还原）。

    合并查询里 CASE + 哈希子查询会被规划器估得很贵，从而触发 JIT：1 万条任务的账本上，
    同一条查询关掉 JIT 是十几毫秒、打开则每次都花 300 毫秒以上重新编译表达式，
    而且随账本变大只会更糟。这类短查询的执行时间远小于 JIT 的编译开销，关掉才是对的。
    ``SET LOCAL`` 只影响当前事务，连接放回池里时不会带走这个设置。
    """
    conn.execute("SET LOCAL jit = off")


def entry_ctes(clauses: Sequence[str]) -> str:
    """三段 CTE：命中筛选的行 → 按条目归并 → 判定条目结果。

    - ``keyed``：命中筛选的**行**，带上所属条目键；筛选（含时间范围）只决定
      「这一条要不要出现」，不会把一条任务切成半截——取行时另按条目键补全整组。
    - ``classified``：结果按**全量账本**判定（EXISTS 走 (billing_operation_id, type)
      唯一索引），而不是按命中筛选的那几行：时间筛选恰好切在暂扣与退回之间时，
      结果仍是「已退回」，不会误判成「生成中」。
    """
    settled = _has_row_sql("e.entry_key", "SETTLE")
    released = _has_row_sql("e.entry_key", "RELEASE")
    return (
        "WITH keyed AS (SELECT wt.id, wt.type, wt.ledger_sequence, wt.created_at, "
        "wt.actor_user_id, wt.available_delta, wt.reserved_delta, "
        f"{_ENTRY_KEY_SQL} AS entry_key, "
        f"(wt.billing_operation_id IS NOT NULL AND wt.type IN {_CYCLE_TYPE_SQL}) AS is_cycle"
        + _KEYED_FROM
        + " AND ".join(clauses)
        + "), entries AS (SELECT entry_key, bool_or(is_cycle) AS is_cycle, "
        "MAX(ledger_sequence) AS seq, MAX(created_at) AS latest_at, MAX(id) AS latest_id "
        "FROM keyed GROUP BY entry_key), "
        "classified AS (SELECT e.*, CASE "
        "WHEN NOT e.is_cycle THEN 'POSTED' "
        f"WHEN {settled} THEN CASE WHEN {released} THEN 'PARTIAL' ELSE 'COMPLETED' END "
        f"WHEN {released} THEN 'FAILED' "
        "ELSE 'PENDING' END AS outcome FROM entries e) "
    )


def outcome_placeholders(outcome: LedgerOutcomeFilter | None) -> tuple[str, list[str]]:
    """结果筛选 → (SQL 片段, 参数)。不筛选时片段为空串。"""
    if outcome is None:
        return "", []
    outcomes = _FILTER_OUTCOMES[outcome]
    return f"outcome IN ({', '.join('%s' for _ in outcomes)})", list(outcomes)


def assemble_entries(
    page: Sequence[tuple[str, bool, str]],
    rows: Sequence[WalletTransactionResponse],
) -> list[WalletLedgerEntry]:
    """把一页条目键与它们的逐笔流水拼成响应。纯函数，不碰数据库。

    ``page`` 是 ``(条目键, 是否计费周期, 结果)``，顺序即响应顺序；``rows`` 可以乱序，
    也可能因为 admin_adjustments 的 LEFT JOIN 出现重复行，按行 id 去重（先到先得）。
    """
    members_by_key: dict[tuple[bool, str], list[WalletTransactionResponse]] = {}
    seen: set[str] = set()
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        is_cycle = row.billing_operation_id is not None and row.type in CYCLE_TYPES
        key = (is_cycle, str(row.billing_operation_id) if is_cycle else row.id)
        members_by_key.setdefault(key, []).append(row)

    entries: list[WalletLedgerEntry] = []
    for entry_key, is_cycle, outcome in page:
        members = members_by_key.get((is_cycle, entry_key))
        if not members:
            # 账本只追加不删除，正常不会缺；缺了宁可少一条也不要编造一条空的。
            continue
        members.sort(
            key=lambda row: (
                _TYPE_ORDER.get(row.type, len(_TYPE_ORDER)) if is_cycle else 0,
                row.created_at,
                row.id,
            )
        )
        entries.append(
            WalletLedgerEntry(
                key=entry_key,
                kind="cycle" if is_cycle else "row",
                outcome=cast(LedgerOutcome, outcome),
                reserved_credits=sum(r.reserved_delta for r in members if r.type == "RESERVE"),
                charged_credits=sum(-r.reserved_delta for r in members if r.type == "SETTLE"),
                refunded_credits=sum(r.available_delta for r in members if r.type == "RELEASE"),
                net_available_delta=sum(r.available_delta for r in members),
                started_at=min(r.created_at for r in members),
                updated_at=max(r.created_at for r in members),
                rows=members,
            )
        )
    return entries


def read_ledger_page(
    conn: psycopg.Connection[Any],
    *,
    wallet_owner_id: str,
    sub_account_id: str | None,
    token_group_id: str | None,
    auth_source: str | None,
    business: str | None,
    started_at: datetime | None,
    ended_at: datetime | None,
    outcome: LedgerOutcomeFilter | None,
    limit: int,
    offset: int,
    group_by_sub_account: bool,
) -> WalletLedgerPage:
    """一页「合并后的流水」：分页、总数、各结果的计数都以**条目**为单位。"""
    disable_jit(conn)
    clauses, params = ledger_filter_clauses(
        wallet_owner_id=wallet_owner_id,
        sub_account_id=sub_account_id,
        token_group_id=token_group_id,
        auth_source=auth_source,
        transaction_type=None,
        business=business,
        started_at=started_at,
        ended_at=ended_at,
    )
    prefix = entry_ctes(clauses)
    outcome_sql, outcome_params = outcome_placeholders(outcome)

    # 各结果的条目数不受结果筛选影响：筛选条上的数字要能告诉用户「切过去有几条」。
    count_rows = conn.execute(
        prefix + "SELECT outcome, COUNT(*) FROM classified GROUP BY outcome", params
    ).fetchall()
    by_outcome = {str(row[0]): int(row[1]) for row in count_rows}
    counts = WalletLedgerCounts(
        total=sum(by_outcome.values()),
        pending=by_outcome.get("PENDING", 0),
        completed=by_outcome.get("COMPLETED", 0),
        refunded=by_outcome.get("PARTIAL", 0) + by_outcome.get("FAILED", 0),
        posted=by_outcome.get("POSTED", 0),
    )
    total = (
        counts.total
        if outcome is None
        else sum(by_outcome.get(name, 0) for name in _FILTER_OUTCOMES[outcome])
    )

    page_rows = conn.execute(
        prefix
        + "SELECT entry_key, is_cycle, outcome FROM classified"
        + (f" WHERE {outcome_sql}" if outcome_sql else "")
        + " ORDER BY (seq IS NULL), seq DESC, latest_at DESC, latest_id DESC "
        + PAGE_CLAUSE,
        [*params, *outcome_params, limit, offset],
    ).fetchall()
    page = [(str(row[0]), bool(row[1]), str(row[2])) for row in page_rows]

    items: list[WalletLedgerEntry] = []
    if page:
        cycle_keys = [key for key, is_cycle, _ in page if is_cycle]
        row_keys = [key for key, is_cycle, _ in page if not is_cycle]
        branches: list[str] = []
        row_params: list[object] = [wallet_owner_id]
        if cycle_keys:
            branches.append(
                f"(wt.billing_operation_id IN ({', '.join('%s' for _ in cycle_keys)}) "
                f"AND wt.type IN {_CYCLE_TYPE_SQL})"
            )
            row_params.extend(cycle_keys)
        if row_keys:
            branches.append(f"wt.id IN ({', '.join('%s' for _ in row_keys)})")
            row_params.extend(row_keys)
        # 取整组而不是「命中筛选的那几行」：一条任务的暂扣与退回可能一个在筛选范围内、
        # 一个在范围外，只取一半就又回到了逐行看账的老问题。
        rows = conn.execute(
            LEDGER_ROW_SELECT
            + LEDGER_ROW_FROM
            + "wt.user_id = %s AND ("
            + " OR ".join(branches)
            + ") ORDER BY (wt.ledger_sequence IS NULL), wt.ledger_sequence ASC, "
            "wt.created_at ASC, wt.id ASC",
            row_params,
        ).fetchall()
        items = assemble_entries(page, [ledger_row_response(row) for row in rows])

    return WalletLedgerPage(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        counts=counts,
        sub_account_summary=(
            _sub_account_summary(conn, prefix, params, outcome_sql, outcome_params)
            if group_by_sub_account
            else None
        ),
    )


def _sub_account_summary(
    conn: psycopg.Connection[Any],
    prefix: str,
    params: Sequence[object],
    outcome_sql: str,
    outcome_params: Sequence[str],
) -> list[dict[str, str | int]]:
    """按子账号汇总当前筛选下的实扣与退回（口径同逐笔列表，见 recharge_routes 旧注释）。

    ``transaction_count`` 数的是**条目**（一条任务算一次），与下方列表的条数对得上；
    逐笔口径下同一条任务会被数成三笔，摘要说 3 笔、列表里只有 1 条。
    """
    rows = conn.execute(
        prefix + "SELECT kr.actor_user_id, "
        "(SELECT u.display_name FROM users u WHERE u.id = kr.actor_user_id), "
        "COALESCE(SUM(CASE WHEN kr.type = 'SETTLE' THEN -kr.reserved_delta ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN kr.type = 'RELEASE' THEN kr.available_delta ELSE 0 END), 0), "
        "COUNT(DISTINCT kr.entry_key) "
        "FROM keyed kr JOIN classified c ON c.entry_key = kr.entry_key "
        "WHERE kr.actor_user_id IS NOT NULL"
        + (f" AND c.{outcome_sql}" if outcome_sql else "")
        # 不设 LIMIT：摘要的用途就是和下方列表对账，静默只回前 N 个会让
        # 「列表里有、摘要里没有」再次发生。
        + " GROUP BY kr.actor_user_id ORDER BY 3 DESC",
        [*params, *outcome_params],
    ).fetchall()
    return [
        {
            "sub_account_id": str(row[0]),
            "sub_account_name": row[1] or "未知子账号",
            "debit_total": int(row[2]),
            "credit_total": int(row[3]),
            "transaction_count": int(row[4]),
        }
        for row in rows
    ]
