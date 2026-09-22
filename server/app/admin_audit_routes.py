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

from fastapi import APIRouter

from app.admin_auth_routes import AdminReader
from app.admin_dates import append_admin_date_filters, utc_timestamp_sql
from app.api_errors import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-audit"])

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
AUDIT_SERVICE_UNAVAILABLE = "AUDIT_SERVICE_UNAVAILABLE"
AUDIT_SERVICE_UNAVAILABLE_MESSAGE = "Audit log requires the PostgreSQL runtime."


def _format_created_at(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else ""


_UNION_SQL = """
    SELECT aa.id, 'ADMIN_ADJUSTMENT', aa.admin_user_id, u.username,
           aa.target_user_id, aa.source_document_type, aa.source_document_ref,
           aa.reason, aa.request_id, aa.created_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb
    FROM admin_adjustments aa
    JOIN users u ON u.id = aa.admin_user_id
    UNION ALL
    SELECT de.id, 'ADMIN_DEVICE_' || de.event, de.admin_user_id, u.username,
           de.target_user_id, 'DEVICE', COALESCE(de.device_id, ''),
           de.reason, de.request_id, de.created_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb
    FROM admin_device_events de
    JOIN users u ON u.id = de.admin_user_id
    UNION ALL
    SELECT cse.id, 'ADMIN_SESSION_' || cse.event,
           COALESCE(cse.actor_user_id, ''), COALESCE(u5.username, ''),
           cse.user_id, 'CUSTOMER_SESSION', cse.session_id,
           COALESCE(cse.reason, ''), COALESCE(cse.request_id, ''),
           cse.created_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb
    FROM customer_session_events cse
    LEFT JOIN users u5 ON u5.id = cse.actor_user_id
    WHERE cse.actor_user_id IS NOT NULL AND cse.actor_user_id <> cse.user_id
    UNION ALL
    SELECT ae.id, 'ACTIVATION_CODE_' || ae.event,
           COALESCE(ae.actor_user_id, ''), COALESCE(u2.username, ''),
           COALESCE(code.bound_user_id, ''), 'ACTIVATION_CODE', ae.code_id,
           COALESCE(ae.reason, ''), COALESCE(ae.request_id, ''), ae.created_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb
    FROM activation_code_events ae
    LEFT JOIN users u2 ON u2.id = ae.actor_user_id
    LEFT JOIN activation_codes code ON code.id = ae.code_id
    UNION ALL
    SELECT d.id, 'ACTIVATION_CODE_DELIVERED', d.delivered_by_user_id, u3.username,
           COALESCE(code.bound_user_id, ''), 'ACTIVATION_CODE_DELIVERY',
           COALESCE(d.external_order_ref, ''), COALESCE(d.note, ''),
           '', d.delivered_at::timestamptz,
           ''::text, NULL::integer, NULL::integer, NULL::jsonb
    FROM activation_code_deliveries d
    JOIN users u3 ON u3.id = d.delivered_by_user_id
    LEFT JOIN activation_codes code ON code.id = d.code_id
    UNION ALL
    SELECT al.id, al.action, COALESCE(al.actor_user_id, ''),
           COALESCE(u4.username, ''),
           CASE WHEN al.entity_type IN ('user', 'customer_unit_price')
                THEN al.entity_id ELSE '' END,
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
               WHEN al.action = 'billing.tariff.update'
                   THEN jsonb_build_object(
                       'old', al.metadata_json::jsonb -> 'old',
                       'new', al.metadata_json::jsonb -> 'new'
                   )
               ELSE NULL
           END
    FROM audit_logs al
    LEFT JOIN users u4 ON u4.id = al.actor_user_id
"""
_UNION_SQL = _UNION_SQL.replace("aa.created_at::timestamptz", utc_timestamp_sql("aa.created_at"))
_UNION_SQL = _UNION_SQL.replace("de.created_at::timestamptz", utc_timestamp_sql("de.created_at"))
_UNION_SQL = _UNION_SQL.replace("cse.created_at::timestamptz", utc_timestamp_sql("cse.created_at"))
_UNION_SQL = _UNION_SQL.replace("ae.created_at::timestamptz", utc_timestamp_sql("ae.created_at"))
_UNION_SQL = _UNION_SQL.replace("d.delivered_at::timestamptz", utc_timestamp_sql("d.delivered_at"))
_UNION_SQL = _UNION_SQL.replace("al.created_at::timestamptz", utc_timestamp_sql("al.created_at"))


@router.get("/audit-log")
def list_audit_log(
    actor: AdminReader,
    event_type: str | None = None,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    actor_username: str | None = None,
    target_username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List the unified audit trail with pagination and combined filters.

    Both admin and auditor roles can access this endpoint (read-only).
    """
    bounded_limit = max(0, min(limit, MAX_LIST_LIMIT))
    bounded_offset = max(0, offset)

    clauses: list[str] = []
    params: list[object] = []
    if event_type:
        clauses.append("ev.event_type = %s")
        params.append(event_type)
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
        clauses.append("tu.username ILIKE %s")
        params.append(f"%{target_username}%")
    append_admin_date_filters(
        clauses, params, column="ev.created_at", created_from=created_from, created_to=created_to
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT ev.event_id, ev.event_type, ev.actor_user_id,
                       ev.actor_username, ev.target_user_id,
                       COALESCE(tu.username, '') AS target_username,
                       ev.source_document_type, ev.source_document_ref,
                       ev.reason, ev.request_id, ev.created_at,
                       ev.change_subject, ev.old_unit_price_fen,
                       ev.new_unit_price_fen, ev.change_detail
                FROM ({_UNION_SQL}) AS ev(event_id, event_type, actor_user_id,
                                          actor_username, target_user_id,
                                          source_document_type,
                                          source_document_ref, reason,
                                          request_id, created_at,
                                          change_subject, old_unit_price_fen,
                                          new_unit_price_fen, change_detail)
                LEFT JOIN users tu ON tu.id = ev.target_user_id
                {where}
                ORDER BY ev.created_at DESC, ev.event_id
                LIMIT %s OFFSET %s
                """,
                (*params, bounded_limit, bounded_offset),
            ).fetchall()

            total_row = conn.execute(
                f"""
                SELECT COUNT(*) FROM ({_UNION_SQL}) AS ev(
                    event_id, event_type, actor_user_id, actor_username,
                    target_user_id, source_document_type, source_document_ref,
                    reason, request_id, created_at, change_subject,
                    old_unit_price_fen, new_unit_price_fen, change_detail)
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
        }
        for row in rows
    ]

    total = int(total_row[0]) if total_row is not None else 0

    return {
        "items": items,
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }
