"""T18 — administrator device operations API.

Application layer for the admin half of the customer device domain (code
checklist §3.2, frozen name ``admin_device_routes.py``; dev doc §6.2 /
§12.2 step 3 / §15): the device list, the pairing verification view an
operator reads before approving, and the three audited mutations —

- ``POST /api/control/device-pairings/{pairing_id}/approve`` — the
  administrator fallback approval lane, opened only when the first device
  of the activation is no longer ``BOUND`` (dev doc §12.2 step 3: the
  first *currently bound* device owns the approval; an admin never
  shortcuts a live one);
- ``POST /api/control/devices/{device_id}/unbind`` — release a bound
  device on the operator's side;
- ``POST /api/control/devices/{device_id}/revoke-credential`` — the
  stronger release for leaked credentials (the ``REVOKED`` terminal
  state, revision 028 shape).

Every write runs behind the T09 admin session / CSRF / RBAC gate and the
shared admin write contract (dev doc §15: real actor, reason,
confirmation, Idempotency-Key, request id) and lands exactly one
append-only ``admin_device_events`` row (revision 038) — the T18 DoD:
真实 actor、原因、二次确认和审计存在.

No-Go red lines: device fingerprints and token digests never leave the
store — the list and verification views carry display metadata and states
only. Reads are auditor-accessible, writes are admin-only.
"""

from __future__ import annotations

import logging
from datetime import datetime

import psycopg
from fastapi import APIRouter, Request, Response

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import (
    AdminWriteContract,
    DeferredHTTPWriteError,
)
from app.admin_write_contract import (
    write_with_idempotency as _write_with_idempotency,
)
from app.api_errors import http_error as _http
from app.customer_device_service import (
    ADMIN_APPROVE_FIRST_DEVICE_AVAILABLE,
    APPROVE_ALREADY_CONSUMED,
    APPROVE_EXPIRED,
    APPROVE_NOT_FOUND,
    OUTCOME_NOT_BOUND,
    OUTCOME_NOT_FOUND,
    REPLACE_ALREADY_APPROVED,
    admin_approve_pairing_request,
    admin_replace_device_for_pairing,
    admin_unbind_device,
    revoke_device_credential,
    server_now_utc,
)
from app.db_pg import pg_transaction
from app.sql_pagination import PAGE_CLAUSE, page_bounds

logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200

DEVICE_SERVICE_UNAVAILABLE = "DEVICE_SERVICE_UNAVAILABLE"
DEVICE_SERVICE_UNAVAILABLE_MESSAGE = "Device management requires the PostgreSQL runtime."

router = APIRouter(prefix="/api/control", tags=["admin-devices"])


class ReplaceDeviceContract(AdminWriteContract):
    replace_device_id: str


def _transaction_now(conn: psycopg.Connection) -> datetime:
    """The PostgreSQL transaction clock (the T17 approve precedent)."""
    now_row = conn.execute("SELECT now()").fetchone()
    return now_row[0] if now_row is not None else server_now_utc()


# ---------------------------------------------------------------------------
# Device list (read path, dev doc §6.2)
# ---------------------------------------------------------------------------


@router.get("/devices")
def list_devices(
    actor: AdminReader,
    status: str | None = None,
    activation_code_id: str | None = None,
    user_id: str | None = None,
    platform: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List devices with display metadata only — digests never leave the store."""
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("cd.status = %s")
        params.append(status)
    if activation_code_id:
        clauses.append("cd.activation_code_id = %s")
        params.append(activation_code_id)
    if user_id:
        clauses.append("cd.user_id = %s")
        params.append(user_id)
    if platform:
        clauses.append("cd.platform = %s")
        params.append(platform)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with pg_transaction() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) FROM customer_devices cd "
                "JOIN users u ON u.id = cd.user_id "
                "LEFT JOIN activation_codes ac ON ac.id = cd.activation_code_id "
                f"{where}",
                params,
            ).fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            rows = conn.execute(
                "SELECT cd.id, cd.activation_code_id, cd.user_id, cd.slot_no, "
                "cd.display_name, cd.platform, cd.status, cd.bound_at, cd.unbound_at, "
                "cd.revoked_at, u.username, ac.masked_code, css.last_heartbeat_at, "
                "(cd.status = 'BOUND' AND "
                " css.lease_until::timestamptz > clock_timestamp()) AS online "
                "FROM customer_devices cd "
                "JOIN users u ON u.id = cd.user_id "
                "LEFT JOIN activation_codes ac ON ac.id = cd.activation_code_id "
                "LEFT JOIN customer_session_state css ON css.device_id = cd.id "
                f"{where} ORDER BY cd.bound_at DESC {PAGE_CLAUSE}",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            summary_row = conn.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE cd.status = 'BOUND'),
                  COUNT(*) FILTER (
                    WHERE cd.status = 'BOUND'
                      AND css.lease_until::timestamptz > clock_timestamp()
                  ),
                  COUNT(*) FILTER (
                    WHERE cd.revoked_at::timestamptz >= date_trunc('day', clock_timestamp())
                  ),
                  COUNT(*) FILTER (WHERE cd.status = 'UNBOUND')
                FROM customer_devices cd
                LEFT JOIN customer_session_state css ON css.device_id = cd.id
                """
            ).fetchone()
            # 方案 P1「登录与设备」顶部三张卡的后两项：
            # 达上限 = 在绑设备数 ≥ users.max_devices 的客户；频繁更换 = 近 24h
            # 解绑+再绑定动作 ≥ 2 的客户（只有解绑或只有绑定不算「换」）。
            attention_row = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM (
                     SELECT cd.user_id FROM customer_devices cd
                     JOIN users u ON u.id = cd.user_id
                     WHERE cd.status = 'BOUND'
                     GROUP BY cd.user_id, u.max_devices
                     HAVING COUNT(*) >= u.max_devices
                  ) full_users),
                  (SELECT COUNT(*) FROM (
                     SELECT cd.user_id FROM customer_devices cd
                     WHERE (cd.unbound_at::timestamptz >= clock_timestamp() - interval '24 hours'
                            OR cd.revoked_at::timestamptz
                                >= clock_timestamp() - interval '24 hours')
                       AND cd.bound_at::timestamptz
                            >= clock_timestamp() - interval '24 hours'
                     GROUP BY cd.user_id
                     HAVING COUNT(*) >= 2
                  ) churny_users)
                """
            ).fetchone()
    except RuntimeError as exc:
        raise _http(503, DEVICE_SERVICE_UNAVAILABLE, DEVICE_SERVICE_UNAVAILABLE_MESSAGE) from exc
    items = [
        {
            "device_id": str(row[0]),
            "activation_code_id": str(row[1]) if row[1] is not None else None,
            "user_id": str(row[2]),
            "slot_no": int(row[3]),
            "display_name": row[4],
            "platform": row[5],
            "status": str(row[6]),
            "bound_at": row[7],
            "unbound_at": row[8],
            "revoked_at": row[9],
            "username": str(row[10]),
            "activation_code": str(row[11]) if row[11] is not None else "",
            "last_heartbeat_at": row[12],
            "online": bool(row[13]),
        }
        for row in rows
    ]
    # A5：返回 total，前端不再用"取满一页"启发式翻页。
    return {
        "items": items,
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "summary": {
            "bound": int(summary_row[0]) if summary_row else 0,
            "online": int(summary_row[1]) if summary_row else 0,
            "revoked_today": int(summary_row[2]) if summary_row else 0,
            "unbound": int(summary_row[3]) if summary_row else 0,
            "at_slot_limit": int(attention_row[0]) if attention_row else 0,
            "frequent_swaps_24h": int(attention_row[1]) if attention_row else 0,
        },
    }


# ---------------------------------------------------------------------------
# Pairing verification view (read path, dev doc §12.2 step 3)
# ---------------------------------------------------------------------------


@router.get("/device-pairings/{pairing_id}")
def get_device_pairing(pairing_id: str, actor: AdminReader) -> dict[str, object]:
    """The verification view an operator reads before approving.

    Dev doc §12.2 step 3: the admin verifies the issuance record (the code,
    its delivery and the activation fact) before opening the fallback lane
    (connector review P2: the delivery records — who delivered the code, on
    which channel, to which recipient — are part of the required evidence).
    ``admin_lane`` summarises whether the fallback is open — a snapshot for
    the operator's eyes only; the write path re-validates under lock.
    """
    try:
        with pg_transaction() as conn:
            pairing = conn.execute(
                "SELECT id, activation_code_id, display_name, platform, status, "
                "created_at, expires_at, approved_at, approved_by_device_id, "
                "approved_by_admin_user_id, consumed_at, consumed_device_id "
                "FROM device_pairing_requests WHERE id = %s",
                (pairing_id,),
            ).fetchone()
            if pairing is None:
                raise _http(404, "PAIRING_NOT_FOUND", "Unknown device pairing request.")
            code_id = str(pairing[1])
            code = conn.execute(
                "SELECT id, masked_code, status, issued_at, bound_user_id "
                "FROM activation_codes WHERE id = %s",
                (code_id,),
            ).fetchone()
            deliveries = conn.execute(
                "SELECT id, channel, external_order_ref, recipient_ref, "
                "delivered_by_user_id, delivered_at "
                "FROM activation_code_deliveries WHERE code_id = %s "
                "ORDER BY delivered_at ASC",
                (code_id,),
            ).fetchall()
            activation = conn.execute(
                "SELECT user_id, activated_at, first_device_id "
                "FROM activation_code_activations WHERE code_id = %s",
                (code_id,),
            ).fetchone()
            first_device = None
            admin_lane_open = activation is not None
            if activation is not None and activation[2] is not None:
                first_device = conn.execute(
                    "SELECT id, status, display_name, slot_no FROM customer_devices WHERE id = %s",
                    (str(activation[2]),),
                ).fetchone()
                admin_lane_open = first_device is None or str(first_device[1]) != "BOUND"
    except RuntimeError as exc:
        raise _http(503, DEVICE_SERVICE_UNAVAILABLE, DEVICE_SERVICE_UNAVAILABLE_MESSAGE) from exc
    return {
        "pairing": {
            "id": str(pairing[0]),
            "activation_code_id": code_id,
            "display_name": pairing[2],
            "platform": pairing[3],
            "status": str(pairing[4]),
            "created_at": pairing[5],
            "expires_at": pairing[6],
            "approved_at": pairing[7],
            "approved_by_device_id": pairing[8],
            "approved_by_admin_user_id": pairing[9],
            "consumed_at": pairing[10],
            "consumed_device_id": pairing[11],
        },
        "code": None
        if code is None
        else {
            "code_id": str(code[0]),
            "masked_code": str(code[1]),
            "status": str(code[2]),
            "issued_at": code[3],
            "bound_user_id": code[4],
        },
        # Issuance evidence (dev doc §12.2 step 3): who delivered the code,
        # through which channel, to which recipient — never the code itself
        # (the table stores no plaintext by design, 027/ACT-01).
        "deliveries": [
            {
                "delivery_id": str(row[0]),
                "channel": str(row[1]),
                "external_order_ref": row[2],
                "recipient_ref": row[3],
                "delivered_by_user_id": str(row[4]),
                "delivered_at": row[5],
            }
            for row in deliveries
        ],
        "activation": None
        if activation is None
        else {
            "user_id": str(activation[0]),
            "activated_at": activation[1],
            "first_device_id": activation[2],
        },
        "first_device": None
        if first_device is None
        else {
            "device_id": str(first_device[0]),
            "status": str(first_device[1]),
            "display_name": first_device[2],
            "slot_no": int(first_device[3]),
        },
        # A missing activation fact keeps the lane closed too, but for a
        # different reason than a live first device — and the write path
        # answers 404 PAIRING_NOT_FOUND there (fail-closed), so the label
        # must not claim the first device is bound (session review P3).
        "admin_lane": (
            "OPEN"
            if admin_lane_open
            else ("CLOSED_NO_ACTIVATION" if activation is None else "CLOSED_FIRST_DEVICE_BOUND")
        ),
    }


# ---------------------------------------------------------------------------
# Admin-verified pairing approval (dev doc §12.2 step 3 / §15)
# ---------------------------------------------------------------------------


@router.post("/device-pairings/{pairing_id}/approve")
def admin_approve_device_pairing(
    pairing_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Approve a PENDING pairing via the administrator fallback lane."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        outcome = admin_approve_pairing_request(
            conn,
            pairing_id=pairing_id,
            admin_user_id=actor.user_id,
            reason=body.reason.strip(),
            request_id=request_id,
            server_now=_transaction_now(conn),
        )
        if outcome == APPROVE_NOT_FOUND:
            raise _http(404, "PAIRING_NOT_FOUND", "Unknown device pairing request.")
        if outcome == ADMIN_APPROVE_FIRST_DEVICE_AVAILABLE:
            # §12.2 step 3: a live first device owns the approval — the
            # administrator must not shortcut it (fail closed for this lane).
            raise _http(
                403,
                "PAIRING_FIRST_DEVICE_AVAILABLE",
                "The first device of this activation is still bound and must "
                "approve the pairing itself.",
            )
        if outcome == APPROVE_ALREADY_CONSUMED:
            raise _http(
                409,
                "PAIRING_ALREADY_CONSUMED",
                "This pairing request was already consumed.",
            )
        if outcome == APPROVE_EXPIRED:
            # The lazy PENDING/APPROVED → EXPIRED flip already ran inside the
            # service — a plain HTTPException would roll it back (the T17
            # route answers outside its ``with`` block for the same reason).
            # The deferred error commits the flip, snapshots the 409 for the
            # idempotent replay and re-raises after the commit.
            raise DeferredHTTPWriteError(
                409,
                "PAIRING_EXPIRED",
                "This pairing request has expired; a new one must be created.",
            )
        # APPROVE_APPROVED or APPROVE_ALREADY_APPROVED — both answer the
        # state (the T17 route precedent); the outcome field tells them apart.
        logger.info(
            "device pairing admin-approved: pairing=%s actor=%s request=%s outcome=%s",
            pairing_id,
            actor.user_id,
            request_id,
            outcome,
        )
        return {
            "pairing_id": pairing_id,
            "status": "APPROVED",
            "outcome": outcome,
            "request_id": request_id,
        }

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=DEVICE_SERVICE_UNAVAILABLE,
        unavailable_message=DEVICE_SERVICE_UNAVAILABLE_MESSAGE,
    )


@router.post("/device-pairings/{pairing_id}/replace-device")
def admin_replace_device_pairing(
    pairing_id: str,
    body: ReplaceDeviceContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Replace one bound device with an approved pending pairing."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        outcome = admin_replace_device_for_pairing(
            conn,
            pairing_id=pairing_id,
            replace_device_id=body.replace_device_id,
            admin_user_id=actor.user_id,
            reason=body.reason.strip(),
            request_id=request_id,
            server_now=_transaction_now(conn),
        )
        if outcome == APPROVE_NOT_FOUND:
            raise _http(404, "PAIRING_NOT_FOUND", "Unknown device pairing request.")
        if outcome == OUTCOME_NOT_FOUND:
            raise _http(404, "DEVICE_NOT_FOUND", "The replacement device was not found.")
        if outcome == OUTCOME_NOT_BOUND:
            raise _http(409, "DEVICE_ALREADY_RELEASED", "The replacement device is not bound.")
        if outcome == APPROVE_ALREADY_CONSUMED:
            raise _http(409, "PAIRING_ALREADY_CONSUMED", "This pairing was already consumed.")
        if outcome == REPLACE_ALREADY_APPROVED:
            raise _http(409, "PAIRING_ALREADY_APPROVED", "This pairing was already approved.")
        if outcome == APPROVE_EXPIRED:
            raise DeferredHTTPWriteError(
                409,
                "PAIRING_EXPIRED",
                "This pairing request has expired; a new one must be created.",
            )
        logger.warning(
            "device replaced by admin: pairing=%s old_device=%s actor=%s request=%s",
            pairing_id,
            body.replace_device_id,
            actor.user_id,
            request_id,
        )
        return {
            "pairing_id": pairing_id,
            "status": "APPROVED",
            "replaced_device_id": body.replace_device_id,
            "request_id": request_id,
        }

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=DEVICE_SERVICE_UNAVAILABLE,
        unavailable_message=DEVICE_SERVICE_UNAVAILABLE_MESSAGE,
    )


# ---------------------------------------------------------------------------
# Admin unbind and credential revocation (dev doc §9.2 / §15)
# ---------------------------------------------------------------------------


@router.post("/devices/{device_id}/unbind")
def admin_unbind_device_route(
    device_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Release a bound device as the administrator (slot freed, history kept)."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        outcome = admin_unbind_device(
            conn,
            device_id=device_id,
            admin_user_id=actor.user_id,
            reason=body.reason.strip(),
            request_id=request_id,
            server_now=_transaction_now(conn),
        )
        if outcome == OUTCOME_NOT_FOUND:
            raise _http(404, "DEVICE_NOT_FOUND", "Unknown device.")
        if outcome == OUTCOME_NOT_BOUND:
            raise _http(
                409,
                "DEVICE_ALREADY_RELEASED",
                "This device is not currently bound.",
            )
        logger.info(
            "device admin-unbound: device=%s actor=%s request=%s",
            device_id,
            actor.user_id,
            request_id,
        )
        return {
            "device_id": device_id,
            "status": "UNBOUND",
            "outcome": outcome,
            "request_id": request_id,
        }

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=DEVICE_SERVICE_UNAVAILABLE,
        unavailable_message=DEVICE_SERVICE_UNAVAILABLE_MESSAGE,
    )


@router.post("/devices/{device_id}/revoke-credential")
def admin_revoke_device_credential(
    device_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Revoke a bound device's credential permanently (the leaked-device lane)."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        outcome = revoke_device_credential(
            conn,
            device_id=device_id,
            admin_user_id=actor.user_id,
            reason=body.reason.strip(),
            request_id=request_id,
            server_now=_transaction_now(conn),
        )
        if outcome == OUTCOME_NOT_FOUND:
            raise _http(404, "DEVICE_NOT_FOUND", "Unknown device.")
        if outcome == OUTCOME_NOT_BOUND:
            raise _http(
                409,
                "DEVICE_ALREADY_RELEASED",
                "This device is not currently bound.",
            )
        logger.info(
            "device credential admin-revoked: device=%s actor=%s request=%s",
            device_id,
            actor.user_id,
            request_id,
        )
        return {
            "device_id": device_id,
            "status": "REVOKED",
            "outcome": outcome,
            "request_id": request_id,
        }

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=DEVICE_SERVICE_UNAVAILABLE,
        unavailable_message=DEVICE_SERVICE_UNAVAILABLE_MESSAGE,
    )
