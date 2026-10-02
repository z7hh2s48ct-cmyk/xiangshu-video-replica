"""T34 / A10 — admin audit log API.

Read-only endpoint for operators and auditors. The 2026-09-02 admin-console
assessment (A10) replaced the single-table ADMIN_ADJUSTMENT view with one
UNION across every audited admin surface, so "查审计" answers the whole
question instead of one ledger slice:

- ``admin_adjustments`` (039)      → ADMIN_ADJUSTMENT
- ``admin_device_events`` (038)    → ADMIN_DEVICE_<EVENT>
- ``customer_session_events`` (029) → ADMIN_SESSION_<EVENT> — only rows an
  administrator acted on (see the branch predicate); see below.
- ``activation_code_events`` (027) → ACTIVATION_CODE_<EVENT>
- ``activation_code_deliveries`` (027) → ACTIVATION_CODE_DELIVERED
- ``audit_logs`` (001)             → the action itself (runtime switches,
  control exports, payment syncs, security denials, …)

``customer_session_events`` is a *domain* table: it records the customer's own
session traffic too (LOGIN, LOGOUT, and a HEARTBEAT row per lease renewal). An
audit trail must not drown in that, so the branch keeps only rows where the
actor is **not** the session's owner — i.e. an administrator forcing the
session down. Self-logout writes ``actor_user_id = user_id`` and system sweeps
(TIMEOUT) write no actor at all, so both fall outside the predicate by
construction. Reading the domain table (rather than only logging new revokes)
is deliberate: revoked sessions are already on record, and this way they become
auditable without a backfill.

Filters: ``event_type`` (exact match on the unified type), ``actor_user_id``,
``target_user_id`` and a ``created_from``/``created_to`` ISO timestamp range.
All fail closed on the SQLite lane (the union reads PG-only JSON operators).

The acting administrator is resolved through ``users`` wherever the source
table stores a real actor id; machine-only rows surface with an empty actor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from fastapi import APIRouter
from fastapi import Response as HttpResponse

from app.admin_audit_business import audit_business_fields
from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_dates import append_admin_date_filters, utc_timestamp_sql
from app.api_errors import http_error as _http
from app.auth import CurrentUser, Role
from app.csv_export import spreadsheet_safe_cell
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.db_portable import BusinessConnection
from app.permissions import write_audit
from app.security_rate_limit import (
    DIMENSION_CONTROL_EXPORT_ACCOUNT,
    consume_rate_limit,
    control_export_account_limit,
    rate_limit_window_seconds,
)
from app.sql_pagination import PAGE_CLAUSE, page_bounds

router = APIRouter(prefix="/api/control", tags=["admin-audit"])

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
AUDIT_SERVICE_UNAVAILABLE = "AUDIT_SERVICE_UNAVAILABLE"
AUDIT_SERVICE_UNAVAILABLE_MESSAGE = "Audit log requires the PostgreSQL runtime."

# 审计事件分组（方案 P1 审计中心改造）：「分组 → 事件」两级筛选的映射表。
# 键是分组 id，值是该组包含的统一事件类型（与 _UNION_SQL 产出的 event_type
# 同一口径）；前端下拉按此分组渲染，后端按值集合过滤。
EVENT_GROUPS: dict[str, tuple[str, ...]] = {
    "content": ("viral_runtime.update", "viral_platform.probe"),
    # 资金：调账（开通/赠送/补偿/退款）与查单补单都落在 admin_adjustments
    # 与 payment.sync 两个事件名上，来源单类型列里有更细的语义。
    "funds": (
        "ADMIN_ADJUSTMENT",
        "payment.sync",
        "payment.ledger_repair",
        "customer_adjustment.create",
        "customer_package.grant",
    ),
    "pricing": (
        "operation_rate.update",
        "billing.tariff.update",
        "customer_pricing.update",
        "customer_unit_price.update",
        "customer_unit_price.reset",
        "recharge_package.create",
        "recharge_package.update",
        "customer_discount.create",
        "customer_discount.deactivate",
        "customer_package.grant",
        "registration_bonus.settings.update",
    ),
    "account": (
        "customer.suspend",
        "customer.resume",
        "customer_annotation.update",
        "customer.annotations.update",
        "admin.activation_code.archived",
        "ACTIVATION_CODE_SUSPENDED",
        "ACTIVATION_CODE_RESUMED",
        "ACTIVATION_CODE_REVOKED",
        "ACTIVATION_CODE_ACTIVATED",
        "ADMIN_DEVICE_PAIRING_ADMIN_APPROVED",
        "ADMIN_DEVICE_REPLACE",
        "ADMIN_DEVICE_DISABLE",
        "ADMIN_DEVICE_UNBIND",
        "ADMIN_SESSION_LOGOUT",
        "h3.account.update",
        "admin.activation_code_batch.created",
        "ACTIVATION_CODE_ISSUED",
        "ACTIVATION_CODE_ARCHIVED",
        "ACTIVATION_CODE_DELIVERED",
    ),
    "system": (
        "provider_settings.update",
        "provider_settings.paid_test",
        "billing_settings.update",
        "zpay_settings.update",
        "admin_team.create",
        "admin_team.update",
        "admin_team.password_reset",
        "team.member.create",
        "team.member.update",
        "team.member.password_reset",
        "runtime_settings.update",
        "payment.provider.update",
        "payment.wechat.update",
        "control.reconciliation.read",
    ),
    # 密钥与导出：整组高敏。
    "secret_export": (
        "provider_settings.secret_reveal",
        "admin.activation_code.revealed",
        "admin.activation_code.revealed_replay",
        "external_call.response_view",
        "control.export",
    ),
    "login": ("admin_session.password_login", "admin_session.exchange"),
}

# 高敏事件（方案 P1）：列表整行标红 + P2 推送通知告警接收人。退款扣减按
# 来源单类型在行级判定（ADMIN_ADJUSTMENT 里只有 REFUND_APPROVAL 是退款）。
SENSITIVE_EVENTS: frozenset[str] = frozenset(
    {
        "provider_settings.secret_reveal",
        "admin.activation_code.revealed",
        "admin.activation_code.revealed_replay",
        "external_call.response_view",
        "control.export",
        "customer_pricing.update",
        "zpay_settings.update",
        "billing.tariff.update",
        "operation_rate.update",
        "payment.provider.update",
        "payment.wechat.update",
        "admin_session.exchange",
    }
)


def _is_sensitive(event_type: str, source_document_type: str) -> bool:
    if event_type == "ADMIN_ADJUSTMENT":
        return source_document_type == "REFUND_APPROVAL"
    return event_type in SENSITIVE_EVENTS


def _format_created_at(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else ""


_UNION_SQL = """
    SELECT aa.id, 'ADMIN_ADJUSTMENT', aa.admin_user_id, u.username,
           aa.target_user_id, aa.source_document_type, aa.source_document_ref,
           aa.reason, aa.request_id, aa.created_at::timestamptz,
           ''::text, NULL::integer, NULL::integer,
           jsonb_build_object('changes', jsonb_build_object('available_credits',
             jsonb_build_object('before', aa.balance_before,
                                'after', aa.balance_after))), 'admin'::text
    FROM admin_adjustments aa
    JOIN users u ON u.id = aa.admin_user_id
    UNION ALL
    SELECT de.id, CASE de.event
           WHEN 'DEVICE_ADMIN_UNBOUND' THEN 'ADMIN_DEVICE_UNBIND'
           WHEN 'DEVICE_CREDENTIAL_REVOKED' THEN 'ADMIN_DEVICE_DISABLE'
           ELSE 'ADMIN_DEVICE_' || de.event END, de.admin_user_id, u.username,
           de.target_user_id, 'DEVICE', COALESCE(de.device_id, ''),
           de.reason, de.request_id, de.created_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb, 'admin'::text
    FROM admin_device_events de
    JOIN users u ON u.id = de.admin_user_id
    UNION ALL
    SELECT cse.id, 'ADMIN_SESSION_' || cse.event,
           COALESCE(cse.actor_user_id, ''), COALESCE(u5.username, ''),
           cse.user_id, 'CUSTOMER_SESSION', cse.session_id,
           COALESCE(cse.reason, ''), COALESCE(cse.request_id, ''),
           cse.created_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb, 'admin'::text
    FROM customer_session_events cse
    LEFT JOIN users u5 ON u5.id = cse.actor_user_id
    WHERE cse.actor_user_id IS NOT NULL AND cse.actor_user_id <> cse.user_id
    UNION ALL
    SELECT ae.id, 'ACTIVATION_CODE_' || ae.event,
           COALESCE(ae.actor_user_id, ''), COALESCE(u2.username, ''),
           COALESCE(code.bound_user_id, ''), 'ACTIVATION_CODE', ae.code_id,
           COALESCE(ae.reason, ''), COALESCE(ae.request_id, ''), ae.created_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb,
           CASE WHEN ae.actor_user_id IS NULL THEN 'system'
             WHEN u2.role IN ('admin','auditor') THEN 'admin' ELSE 'customer' END
    FROM activation_code_events ae
    LEFT JOIN users u2 ON u2.id = ae.actor_user_id
    LEFT JOIN activation_codes code ON code.id = ae.code_id
    UNION ALL
    SELECT d.id, 'ACTIVATION_CODE_DELIVERED', d.delivered_by_user_id, u3.username,
           COALESCE(code.bound_user_id, ''), 'ACTIVATION_CODE_DELIVERY',
           COALESCE(d.external_order_ref, ''), COALESCE(d.note, ''),
           '', d.delivered_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb, 'admin'::text
    FROM activation_code_deliveries d
    JOIN users u3 ON u3.id = d.delivered_by_user_id
    LEFT JOIN activation_codes code ON code.id = d.code_id
    UNION ALL
    SELECT al.id, al.action, COALESCE(al.actor_user_id, ''),
           COALESCE(u4.username, ''),
            CASE WHEN al.entity_type IN
                 ('user', 'customer_unit_price', 'customer_annotation', 'team_member')
                THEN al.entity_id
                WHEN u4.role NOT IN ('admin', 'auditor') THEN al.actor_user_id
                ELSE '' END,
           al.entity_type, al.entity_id,
           COALESCE(al.metadata_json::json ->> 'reason', ''),
           COALESCE(al.metadata_json::json ->> 'request_id', ''),
           al.created_at::timestamptz,
           CASE
               WHEN al.action = 'operation_rate.update'
                   THEN COALESCE(al.metadata_json::jsonb ->> 'subject', al.entity_id)
               WHEN al.action IN ('customer_unit_price.update', 'customer_unit_price.reset')
                   THEN 'customer_unit_price'
               WHEN al.action = 'billing.tariff.update'
                   THEN al.entity_id
               ELSE ''
           END,
           CASE
               WHEN al.action IN (
                   'operation_rate.update',
                   'customer_unit_price.update',
                   'customer_unit_price.reset'
               ) AND jsonb_typeof(al.metadata_json::jsonb -> 'old_unit_price_fen') = 'number'
                   THEN (al.metadata_json::jsonb ->> 'old_unit_price_fen')::integer
               ELSE NULL
           END,
           CASE
               WHEN al.action IN (
                   'operation_rate.update',
                   'customer_unit_price.update',
                   'customer_unit_price.reset'
               ) AND jsonb_typeof(al.metadata_json::jsonb -> 'new_unit_price_fen') = 'number'
                   THEN (al.metadata_json::jsonb ->> 'new_unit_price_fen')::integer
               ELSE NULL
           END,
            CASE
                WHEN al.action IN ('customer_unit_price.update', 'customer_unit_price.reset')
                    AND jsonb_typeof(al.metadata_json::jsonb -> 'price_change') = 'object'
                    THEN jsonb_build_object('changes', jsonb_build_object(
                        'customer_unit_price', al.metadata_json::jsonb -> 'price_change'))
                WHEN al.action IN ('customer_unit_price.update', 'customer_unit_price.reset')
                    AND al.metadata_json::jsonb ? 'old_unit_price_fen'
                    AND al.metadata_json::jsonb ? 'new_unit_price_fen'
                    AND jsonb_typeof(al.metadata_json::jsonb -> 'old_unit_price_fen')
                        IN ('number','null')
                    AND jsonb_typeof(al.metadata_json::jsonb -> 'new_unit_price_fen')
                        IN ('number','null')
                    THEN jsonb_build_object('changes', jsonb_build_object('customer_unit_price',
                        jsonb_build_object(
                            'before', jsonb_build_object(
                                'mode', CASE
                                    WHEN jsonb_typeof(
                                        al.metadata_json::jsonb -> 'old_unit_price_fen')='null'
                                    THEN 'DEFAULT' ELSE 'CUSTOM' END,
                                'custom_unit_price_fen',
                                al.metadata_json::jsonb -> 'old_unit_price_fen',
                                'effective_unit_price_fen',
                                al.metadata_json::jsonb -> 'old_unit_price_fen'),
                            'after', jsonb_build_object(
                                'mode', CASE
                                    WHEN jsonb_typeof(
                                        al.metadata_json::jsonb -> 'new_unit_price_fen')='null'
                                    THEN 'DEFAULT' ELSE 'CUSTOM' END,
                                'custom_unit_price_fen',
                                al.metadata_json::jsonb -> 'new_unit_price_fen',
                                'effective_unit_price_fen',
                                al.metadata_json::jsonb -> 'new_unit_price_fen'))))
                WHEN al.action = 'billing.tariff.update'
                   THEN jsonb_build_object(
                       'old', al.metadata_json::jsonb -> 'old',
                        'new', al.metadata_json::jsonb -> 'new'
                    )
                WHEN al.action = 'viral_runtime.update'
                    THEN jsonb_build_object('changes', al.metadata_json::jsonb -> 'changes')
               ELSE jsonb_build_object('metadata',al.metadata_json::jsonb,
                    'old',al.metadata_json::jsonb -> 'old',
                    'new',al.metadata_json::jsonb -> 'new',
                    'changes',al.metadata_json::jsonb -> 'changes')
           END,
           CASE WHEN al.actor_user_id IS NULL THEN 'system'
                WHEN u4.role IN ('admin', 'auditor') THEN 'admin' ELSE 'customer' END
    FROM audit_logs al
    LEFT JOIN users u4 ON u4.id = al.actor_user_id
"""
_UNION_SQL = _UNION_SQL.replace("aa.created_at::timestamptz", utc_timestamp_sql("aa.created_at"))
_UNION_SQL = _UNION_SQL.replace("de.created_at::timestamptz", utc_timestamp_sql("de.created_at"))
_UNION_SQL = _UNION_SQL.replace("cse.created_at::timestamptz", utc_timestamp_sql("cse.created_at"))
_UNION_SQL = _UNION_SQL.replace("ae.created_at::timestamptz", utc_timestamp_sql("ae.created_at"))
_UNION_SQL = _UNION_SQL.replace("d.delivered_at::timestamptz", utc_timestamp_sql("d.delivered_at"))
_UNION_SQL = _UNION_SQL.replace("al.created_at::timestamptz", utc_timestamp_sql("al.created_at"))


@router.get("/audit-log.csv")
def export_audit_log_csv(
    actor: AdminWriter,
    event_type: str | None = None,
    event_group: str | None = None,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    actor_username: str | None = None,
    target_username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    scope: Literal["admin", "customer", "all"] = "admin",
    limit: int = 5000,
) -> HttpResponse:
    """审计导出（方案 P1）：与列表同筛选口径的整表 CSV，走 control.export 审计。

    复用 customers.csv 的限流维度：导出是数据出境动作，读级角色不放行整表
    转储，auditor 需要导出时由管理员执行或走行级查看——所以依赖写级门槛
    （``AdminWriter``，与 customers.csv 及其它整表导出一致），而不只是能登录即可。
    """
    import csv as csv_mod
    import hashlib as hashlib_mod
    import io as io_mod

    if limit < 1 or limit > 5000:
        raise _http(422, "AUDIT_EXPORT_LIMIT_INVALID", "导出条数需在 1–5000 之间。")
    bounded_limit, _ = page_bounds(limit, 0, max_limit=5000)
    clauses: list[str] = []
    params: list[object] = []
    if scope != "all":
        clauses.append("ev.actor_scope = %s")
        params.append(scope)
    if event_type:
        clauses.append("ev.event_type = %s")
        params.append(event_type)
    if event_group:
        groups = [key.strip() for key in event_group.split(",") if key.strip()]
        unknown = [key for key in groups if key not in EVENT_GROUPS]
        if unknown:
            raise _http(422, "AUDIT_EVENT_GROUP_UNKNOWN", "未知的事件分组。")
        merged: list[str] = []
        for key in groups:
            merged.extend(EVENT_GROUPS[key])
        if merged:
            clauses.append("ev.event_type = ANY(%s)")
            params.append(merged)
    if actor_user_id:
        clauses.append("ev.actor_user_id = %s")
        params.append(actor_user_id)
    if target_user_id:
        clauses.append("ev.target_user_id = %s")
        params.append(target_user_id)
    if actor_username:
        clauses.append("ev.actor_username ILIKE %s")
        params.append(f"%{actor_username}%")
    if target_username:
        clauses.append("(tu.username ILIKE %s OR tu.display_name ILIKE %s)")
        params.extend([f"%{target_username}%"] * 2)
    append_admin_date_filters(
        clauses, params, column="ev.created_at", created_from=created_from, created_to=created_to
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    try:
        with pg_transaction(isolation="REPEATABLE READ") as conn:
            decision = consume_rate_limit(
                conn,
                dimension=DIMENSION_CONTROL_EXPORT_ACCOUNT,
                identifier=hashlib_mod.sha256(actor.user_id.encode("utf-8")).hexdigest(),
                limit=control_export_account_limit(),
                window_seconds=rate_limit_window_seconds(),
            )
            if not decision.allowed:
                raise _http(
                    429,
                    "CONTROL_EXPORT_RATE_LIMITED",
                    "Too many audit exports; retry after the cooldown.",
                )
            write_audit(
                BusinessConnection.postgres(conn),
                actor=CurrentUser(
                    id=actor.user_id,
                    username=actor.username,
                    display_name=actor.display_name,
                    role=cast(Role, actor.role),
                ),
                action="control.export",
                entity_type="control_ledger",
                entity_id="audit_log",
                metadata={
                    "filters": {
                        "event_type": event_type,
                        "event_group": event_group,
                        "actor_user_id": actor_user_id,
                        "target_user_id": target_user_id,
                        "actor_username": actor_username,
                        "target_username": target_username,
                        "created_from": created_from,
                        "created_to": created_to,
                        "scope": scope,
                    },
                    "limit": bounded_limit,
                },
            )
            rows = conn.execute(
                f"""
                SELECT ev.event_type, ev.created_at, ev.actor_username,
                       COALESCE(tu.username, '') AS target_username,
                       ev.source_document_type, ev.source_document_ref,
                       ev.reason,ev.change_subject,ev.old_unit_price_fen,
                       ev.new_unit_price_fen,ev.change_detail,COALESCE(tu.display_name,'')
                FROM ({_UNION_SQL}) AS ev(event_id, event_type, actor_user_id,
                                          actor_username, target_user_id,
                                          source_document_type,
                                          source_document_ref, reason,
                                          request_id, created_at,
                                          change_subject, old_unit_price_fen,
                                          new_unit_price_fen, change_detail,
                                          actor_scope)
                LEFT JOIN users tu ON tu.id = ev.target_user_id
                {where}
                ORDER BY ev.created_at DESC, ev.event_id
                LIMIT %s
                """,  # noqa: S608
                tuple(params) + (bounded_limit,),
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, AUDIT_SERVICE_UNAVAILABLE, AUDIT_SERVICE_UNAVAILABLE_MESSAGE) from exc

    buffer = io_mod.StringIO()
    writer = csv_mod.writer(buffer)
    writer.writerow(
        [
            "event_type",
            "created_at",
            "actor_username",
            "target_username",
            "source_document_type",
            "source_document_ref",
            "reason",
            "event_label",
            "event_group_label",
            "target_label",
            "change_summary",
            "sensitive",
        ]
    )
    for row in rows:
        fields = audit_business_fields(
            {
                "event_type": str(row[0]),
                "source_document_type": str(row[4]),
                "source_document_ref": str(row[5]),
                "target_username": str(row[3]),
                "change_subject": row[7],
                "old_unit_price_fen": row[8],
                "new_unit_price_fen": row[9],
                "change_detail": row[10],
                "target_company_name": row[11],
            },
            EVENT_GROUPS,
        )
        business_row = (
            *row[:7],
            fields["event_label"],
            fields["event_group_label"],
            fields["target_label"],
            fields["change_summary"],
            _is_sensitive(str(row[0]), str(row[4])) or fields["negative_adjustment"],
        )
        writer.writerow([spreadsheet_safe_cell(str(value)) for value in business_row])
    payload = buffer.getvalue().encode("utf-8")
    return HttpResponse(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )


@router.get("/audit-log")
def list_audit_log(
    actor: AdminReader,
    event_type: str | None = None,
    event_group: str | None = None,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    actor_username: str | None = None,
    target_username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    scope: Literal["admin", "customer", "all"] = "admin",
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List the unified audit trail with pagination and combined filters.

    Both admin and auditor roles can access this endpoint (read-only).

    ``scope`` 默认 ``admin``：客户在工作台的日常动作（建项目、读素材等）也写在
    ``audit_logs``，整表并入会淹没管理员操作（方案 P0-3）；客户详情的「操作
    记录」用 ``scope=customer`` + ``target_user_id`` 查看某位客户自己的动作。
    """
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)

    clauses: list[str] = []
    params: list[object] = []
    if scope != "all":
        clauses.append("ev.actor_scope = %s")
        params.append(scope)
    if event_type:
        clauses.append("ev.event_type = %s")
        params.append(event_type)
    if event_group:
        # 「分组 → 事件」两级筛选的组级入参；逗号分隔多组，映射成事件集合
        # 交给 ANY 过滤。未登记的组名显式 422，避免静默空列表。
        groups = [key.strip() for key in event_group.split(",") if key.strip()]
        unknown = [key for key in groups if key not in EVENT_GROUPS]
        if unknown:
            raise _http(422, "AUDIT_EVENT_GROUP_UNKNOWN", "未知的事件分组。")
        merged: list[str] = []
        for key in groups:
            merged.extend(EVENT_GROUPS[key])
        if merged:
            clauses.append("ev.event_type = ANY(%s)")
            params.append(merged)
    if actor_user_id:
        clauses.append("ev.actor_user_id = %s")
        params.append(actor_user_id)
    if target_user_id:
        clauses.append("ev.target_user_id = %s")
        params.append(target_user_id)
    if actor_username:
        clauses.append("ev.actor_username ILIKE %s")
        params.append(f"%{actor_username}%")
    if target_username:
        clauses.append("(tu.username ILIKE %s OR tu.display_name ILIKE %s)")
        params.extend([f"%{target_username}%"] * 2)
    append_admin_date_filters(
        clauses, params, column="ev.created_at", created_from=created_from, created_to=created_to
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    try:
        with pg_transaction(isolation="REPEATABLE READ") as conn:
            rows = conn.execute(
                f"""
                SELECT ev.event_id, ev.event_type, ev.actor_user_id,
                       ev.actor_username, ev.target_user_id,
                       COALESCE(tu.username, '') AS target_username,
                       ev.source_document_type, ev.source_document_ref,
                       ev.reason, ev.request_id, ev.created_at,
                       ev.change_subject, ev.old_unit_price_fen,
                       ev.new_unit_price_fen, ev.change_detail,COALESCE(tu.display_name,'')
                FROM ({_UNION_SQL}) AS ev(event_id, event_type, actor_user_id,
                                          actor_username, target_user_id,
                                          source_document_type,
                                          source_document_ref, reason,
                                          request_id, created_at,
                                          change_subject, old_unit_price_fen,
                                          new_unit_price_fen, change_detail,
                                          actor_scope)
                LEFT JOIN users tu ON tu.id = ev.target_user_id
                {where}
                ORDER BY ev.created_at DESC, ev.event_id
                {PAGE_CLAUSE}
                """,
                (*params, bounded_limit, bounded_offset),
            ).fetchall()

            # A12 评估结论：
            # total 必须精确，所以这里把 _UNION_SQL 再展开一次——与上面的列表同
            # FROM、同 JOIN、同一个 `where` 字符串（users.id 是主键，连接不放大行
            # 数），两者行集恒等，实测 count 与列表全量取回的行数逐项相等。
            # 该 COUNT 也不是本端点的瓶颈：6 表 85.5 万行 / UNION 45.5 万行下
            # COUNT 38–79 ms，同一数据的列表查询 314–766 ms。单语句"共享一次扫描"
            # （COUNT(*) OVER () / MATERIALIZED CTE）实测更慢且 limit=0、offset 越界
            # 时拿不到 total；缓存/近似计数违反精确性红线。真正的杠杆是第 3 源
            # （customer_session_events 全表扫只留 1/2000 行）的 partial index，需要
            # 新迁移（迁移链同步成本见该文档 §3.1），触发线见 §4。
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) FROM ({_UNION_SQL}) AS ev(
                    event_id, event_type, actor_user_id, actor_username,
                    target_user_id, source_document_type, source_document_ref,
                    reason, request_id, created_at, change_subject,
                    old_unit_price_fen, new_unit_price_fen, change_detail,
                    actor_scope)
                LEFT JOIN users tu ON tu.id = ev.target_user_id
                {where}
                """,
                tuple(params),
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            AUDIT_SERVICE_UNAVAILABLE,
            AUDIT_SERVICE_UNAVAILABLE_MESSAGE,
        ) from exc

    items = [
        {
            "event_id": str(row[0]),
            "event_type": str(row[1]),
            "actor_user_id": str(row[2]),
            "actor_username": str(row[3]),
            "target_user_id": str(row[4]),
            "target_username": str(row[5]),
            "source_document_type": str(row[6]),
            "source_document_ref": str(row[7]),
            "reason": str(row[8]),
            "request_id": str(row[9]),
            "created_at": _format_created_at(row[10]),
            "change_subject": str(row[11]) if row[11] else None,
            "old_unit_price_fen": int(row[12]) if row[12] is not None else None,
            "new_unit_price_fen": int(row[13]) if row[13] is not None else None,
            "change_detail": row[14] if row[14] is not None else None,
            "sensitive": _is_sensitive(str(row[1]), str(row[6])),
            "target_company_name": str(row[15]),
        }
        for row in rows
    ]

    for item in items:
        business = audit_business_fields(item, EVENT_GROUPS)
        item.update(business)
        item["sensitive"] = bool(item["sensitive"] or business["negative_adjustment"])
        item.pop("negative_adjustment", None)

    total = int(total_row[0]) if total_row is not None else 0

    return {
        "items": items,
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }
