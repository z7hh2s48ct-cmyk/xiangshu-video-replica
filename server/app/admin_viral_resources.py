"""素材资源事件的账单核对只追加证据，不改客户余额或历史计量事实。"""

import json
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Query, Request, Response
from pydantic import Field

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import (
    AdminWriteContract,
    http_error,
    require_write_contract,
    write_with_idempotency,
)
from app.billing_reports import date_bounds
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.viral_resource_metering import resource_records, resource_summary

router = APIRouter(prefix="/viral", tags=["admin-runtime"])


class ResourceCostVerification(AdminWriteContract):
    cost_fen: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    bill_reference: str = Field(min_length=3, max_length=200)
    expected_evidence_id: str | None = Field(default=None, max_length=100)


@router.get("/resource-events")
def read_resource_events(
    _actor: AdminReader,
    platform: Literal["douyin", "wechat_channels"] | None = None,
    video_id: Annotated[str, Query(max_length=2048)] | None = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
) -> dict[str, object]:
    if bool(start) != bool(end) or (start and end and end < start):
        raise http_error(422, "VIRAL_RESOURCE_RANGE_INVALID", "请填写有效的起止日期。")
    lower, upper = date_bounds(start, end) if start and end else (None, None)
    with pg_transaction() as raw:
        rows = resource_records(
            BusinessConnection.postgres(raw),
            platform=platform,
            video_id=video_id,
            lower=lower,
            upper=upper,
        )
    summary = resource_summary(rows)
    summary["records"] = rows[offset : offset + limit]
    return {**summary, "total": len(rows), "offset": offset, "limit": limit}


@router.post("/resource-events/{measurement_id}/verify-cost")
def verify_resource_cost(
    measurement_id: str,
    payload: ResourceCostVerification,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    require_write_contract(request, payload)
    if len(payload.bill_reference.strip()) < 3:
        raise http_error(422, "VIRAL_BILL_REFERENCE_REQUIRED", "请填写真实账单或凭证编号。")

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        original = raw.execute(
            "SELECT metadata_json FROM audit_logs WHERE id=%s AND "
            "action='viral_resource.started' AND entity_type='viral_resource' FOR UPDATE",
            (measurement_id,),
        ).fetchone()
        if original is None:
            raise http_error(404, "VIRAL_RESOURCE_NOT_FOUND", "素材计量事件不存在。")
        final = raw.execute(
            "SELECT id FROM audit_logs WHERE action='viral_resource.finished' "
            "AND entity_id=%s LIMIT 1",
            (measurement_id,),
        ).fetchone()
        if final is None:
            raise http_error(409, "VIRAL_RESOURCE_PENDING", "计量事件尚未终结，请先核对事件结果。")
        latest = raw.execute(
            "SELECT id FROM audit_logs WHERE action='viral_resource.cost_verified' "
            "AND entity_id=%s ORDER BY created_at DESC,id DESC LIMIT 1",
            (measurement_id,),
        ).fetchone()
        if (str(latest[0]) if latest else None) != payload.expected_evidence_id:
            raise http_error(409, "CONFLICT", "账单证据已变化，请刷新后重新核对。")
        raw.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("viral-bill:" + payload.bill_reference.strip(),),
        )
        reused = raw.execute(
            "SELECT id FROM audit_logs WHERE action='viral_resource.cost_verified' "
            "AND entity_id<>%s AND metadata_json::jsonb->>'bill_reference'=%s LIMIT 1",
            (measurement_id, payload.bill_reference.strip()),
        ).fetchone()
        if reused:
            raise http_error(
                409, "VIRAL_BILL_LINE_USED", "该账单明细已归属其他事件，不能重复分摊。"
            )
        ident = str(uuid4())
        verified_at = raw.execute("SELECT now()").fetchone()
        assert verified_at is not None
        evidence: dict[str, object] = {
            "id": ident,
            "measurement_id": measurement_id,
            "cost_fen": str(payload.cost_fen),
            "bill_reference": payload.bill_reference.strip(),
            "reason": payload.reason,
            "request_id": request_id,
            "operator_id": actor.user_id,
            "verified_at": str(verified_at[0]),
        }
        raw.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_resource.cost_verified','viral_resource',%s,%s)",
            (ident, actor.user_id, measurement_id, json.dumps(evidence, ensure_ascii=False)),
        )
        return evidence

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="VIRAL_RESOURCE_UNAVAILABLE",
        unavailable_message="素材计量证据暂不可用。",
    )
