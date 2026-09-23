"""T34 — admin customer session API.

Read-only endpoint for operators and auditors to view the live customer
session state (revision 029 ``customer_session_state``: one row per user)
joined to the bound device.

Fail-closed runtime: SQLite/missing DSN returns 503 SESSION_SERVICE_UNAVAILABLE
instead of falling back to legacy control identity (the T12/T18 precedent).
"""

from __future__ import annotations

import logging

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import Field, StrictInt

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import AdminWriteContract, transaction_now_iso, write_with_idempotency
from app.api_errors import http_error as _http
from app.customer_session_service import revoke_session
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.sql_pagination import PAGE_CLAUSE, page_bounds

router = APIRouter(prefix="/api/control", tags=["admin-sessions"])
logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
SESSION_SERVICE_UNAVAILABLE = "SESSION_SERVICE_UNAVAILABLE"
SESSION_SERVICE_UNAVAILABLE_MESSAGE = "Session management requires the PostgreSQL runtime."


class SessionRevokeRequest(AdminWriteContract):
    session_epoch: StrictInt = Field(ge=1)


@router.post("/customer-sessions/{session_id}/revoke")
def revoke_customer_session(
    session_id: str,
    body: SessionRevokeRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT user_id, device_id, session_epoch FROM customer_session_state "
            "WHERE session_id=%s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if row is None:
            raise _http(404, "CUSTOMER_SESSION_NOT_FOUND", "Customer session no longer exists.")
        if int(row[2]) != body.session_epoch:
            raise _http(
                409, "CUSTOMER_SESSION_CHANGED", "Session changed; refresh before retrying."
            )
        revoke_session(
            conn,
            user_id=str(row[0]),
            device_id=str(row[1]),
            actor_user_id=actor.user_id,
            reason=body.reason.strip(),
            request_id=request_id,
            now_iso=transaction_now_iso(conn),
        )
        logger.warning(
            "customer session ended: user=%s actor=%s request=%s", row[0], actor.user_id, request_id
        )
        return {"user_id": str(row[0]), "session_epoch": int(row[2]) + 1, "request_id": request_id}

    return write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=SESSION_SERVICE_UNAVAILABLE,
        unavailable_message=SESSION_SERVICE_UNAVAILABLE_MESSAGE,
    )


@router.get("/customer-sessions/live")
def list_live_sessions(
    actor: AdminReader,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """Overview of every currently live customer session (A11, 2026-09-02).

    Same liveness semantics as the per-customer view (DB-clock lease check),
    across all users instead of one — the console "今日概览/会话" entry point
    so operators no longer need to know a customer id upfront.
    """
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)

    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT css.session_id, css.user_id, u.username,
                       css.device_id, css.session_epoch,
                       css.lease_until, css.last_heartbeat_at,
                       css.created_at, css.updated_at,
                       cd.display_name, cd.platform, cd.slot_no, cd.status
                FROM customer_session_state css
                JOIN customer_devices cd ON cd.id = css.device_id
                JOIN users u ON u.id = css.user_id
                WHERE cd.status = 'BOUND'
                  AND css.lease_until::timestamptz > clock_timestamp()
                ORDER BY css.created_at DESC, css.session_id
                {PAGE_CLAUSE}
                """,
                (bounded_limit, bounded_offset),
            ).fetchall()

            total_row = conn.execute(
                """
                SELECT COUNT(*) FROM customer_session_state css
                JOIN customer_devices cd ON cd.id = css.device_id
                WHERE cd.status = 'BOUND'
                  AND css.lease_until::timestamptz > clock_timestamp()
                """
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            SESSION_SERVICE_UNAVAILABLE,
            SESSION_SERVICE_UNAVAILABLE_MESSAGE,
        ) from exc

    items = [
        {
            "session_id": str(row[0]),
            "user_id": str(row[1]),
            "username": row[2],
            "device_id": str(row[3]),
            "session_epoch": int(row[4]),
            "lease_until": str(row[5]) if row[5] is not None else "",
            "last_heartbeat_at": str(row[6]) if row[6] is not None else "",
            "created_at": str(row[7]) if row[7] is not None else "",
            "updated_at": str(row[8]) if row[8] is not None else "",
            "device_name": row[9],
            "platform": str(row[10]),
            "slot_no": int(row[11]),
            "device_status": str(row[12]),
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


@router.get("/customers/{user_id}/sessions")
def list_customer_sessions(
    user_id: str,
    actor: AdminReader,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List the live session state for a target customer.

    The 029 model keeps exactly one session row per user
    (``customer_session_state``, primary key *is* ``user_id``) so the list is
    at most one row; device columns come from the bound ``customer_devices``
    row and ``status`` filters on the device status (BOUND/UNBOUND/REVOKED).
    Both admin and auditor roles can read (read-only).

    "Live" is judged on the PostgreSQL clock: logout, revocation and natural
    lease expiry keep the row (the lease is pulled into the past, the T16/T19
    pattern) rather than deleting it, so a row's existence alone is not a
    live session.
    """
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)

    clauses: list[str] = [
        "css.user_id = %s",
        "cd.status = 'BOUND'",
        # Logout/revocation/expiry pull lease_until into the past instead of
        # deleting the row; without this predicate administrators would see
        # a supposedly live session indefinitely. The column is text holding
        # mixed ISO / PG-text timestamps, so compare on the cast (the 029
        # CHECK ``lease_until::timestamptz > created_at::timestamptz``
        # guarantees every stored value parses) against the PostgreSQL clock.
        "css.lease_until::timestamptz > clock_timestamp()",
    ]
    params: list[object] = [user_id]

    if status:
        clauses.append("cd.status = %s")
        params.append(status)

    where = f"WHERE {' AND '.join(clauses)}"

    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT css.session_id, css.user_id, u.username,
                       css.device_id, css.session_epoch,
                       css.lease_until, css.last_heartbeat_at,
                       css.created_at, css.updated_at,
                       cd.display_name, cd.platform, cd.slot_no, cd.status
                FROM customer_session_state css
                JOIN customer_devices cd ON cd.id = css.device_id
                JOIN users u ON u.id = css.user_id
                {where}
                ORDER BY css.created_at DESC, css.session_id
                {PAGE_CLAUSE}
                """,
                (*params, bounded_limit, bounded_offset),
            ).fetchall()

            total_row = conn.execute(
                f"""
                SELECT COUNT(*) FROM customer_session_state css
                JOIN customer_devices cd ON cd.id = css.device_id
                {where}
                """,
                tuple(params),
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            SESSION_SERVICE_UNAVAILABLE,
            SESSION_SERVICE_UNAVAILABLE_MESSAGE,
        ) from exc

    items = [
        {
            "session_id": str(row[0]),
            "user_id": str(row[1]),
            "username": row[2],
            "device_id": str(row[3]),
            "session_epoch": int(row[4]),
            "lease_until": str(row[5]) if row[5] is not None else "",
            "last_heartbeat_at": str(row[6]) if row[6] is not None else "",
            "created_at": str(row[7]) if row[7] is not None else "",
            "updated_at": str(row[8]) if row[8] is not None else "",
            "device_name": row[9],
            "platform": str(row[10]),
            "slot_no": int(row[11]),
            "device_status": str(row[12]),
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
