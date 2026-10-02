"""Admin fee catalog and economics, customer-safe tariff publication and quotes."""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field, model_validator

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import AdminWriteContract, write_with_idempotency
from app.billing_catalog import SERVICES, Tariff, read_tariff, retail_snapshot
from app.billing_history import pricing_versions, retail_version, tariff_versions
from app.billing_reports import (
    operation_rows,
    source_action_detail,
    source_action_rows,
    statistics,
)
from app.csv_export import spreadsheet_safe_cell
from app.customer_fence import customer_read_transaction
from app.customer_pricing import read_pricing
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection

router = APIRouter(tags=["itemized-billing"])


@router.get("/api/control/billing/viral-collections")
def collection_batches(
    _actor: AdminReader,
    start: date,
    end: date,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    from app.viral_collection_billing import collection_batch_rows

    with pg_transaction() as raw:
        rows = collection_batch_rows(
            BusinessConnection.postgres(raw), start=start, end=end, limit=limit, offset=offset
        )
        return {"items": rows, "total": rows[0]["total_count"] if rows else 0}


@router.get("/api/control/billing/viral-collections/{batch_id}/charges")
def collection_charges(
    batch_id: str,
    _actor: AdminReader,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    from app.viral_collection_billing import collection_charge_rows

    with pg_transaction() as raw:
        rows = collection_charge_rows(
            BusinessConnection.postgres(raw), batch_id=batch_id, limit=limit, offset=offset
        )
        return {"items": rows, "total": rows[0]["total_count"] if rows else 0}


def catalog(conn: BusinessConnection, *, admin: bool) -> list[dict[str, Any]]:
    result = []
    editors: dict[str, tuple[Any, Any, Any, Any]] = {}
    if admin:
        # 一次读全：最后修改人/时间随科目回显，避免每行一条查询。
        editors = {
            str(row[0]): row
            for row in conn.execute(
                "SELECT t.service, t.updated_at, t.updated_by_user_id, COALESCE(u.username, '') "
                "FROM billing_tariffs t LEFT JOIN users u ON u.id = t.updated_by_user_id"
            ).fetchall()
        }
    for key, service in SERVICES.items():
        if not admin and not service.customer_charge_allowed:
            continue
        # Global pricing always precedes the tariff lock, matching admin writes.
        quote = retail_snapshot(conn, key, 1) if not admin else None
        tariff = read_tariff(conn, key)
        item: dict[str, Any] = {
            "service": key,
            "name": service.name,
            "unit": service.unit,
            "module": service.module,
            "customer_charge_allowed": service.customer_charge_allowed,
            "configured": tariff is not None,
        }
        if admin:
            editor = editors.get(key)
            item["provider"] = service.provider
            item["zero_cost_platform"] = service.zero_cost_platform
            item["tariff"] = (tariff or Tariff()).model_dump(mode="json")
            item["updated_at"] = (
                editor[1].isoformat() if editor is not None and editor[1] is not None else None
            )
            item["updated_by_user_id"] = editor[2] if editor is not None else None
            item["updated_by"] = (editor[3] or None) if editor is not None else None
        else:
            item["quote"] = quote
        result.append(item)
    return result


class TariffUpdate(AdminWriteContract):
    service: str
    expected_version: int = Field(ge=0)
    tariff: Tariff
    expected_pricing_version: int | None = Field(default=None, ge=0)


def admin_catalog_response(conn: BusinessConnection) -> dict[str, Any]:
    version, pricing = read_pricing(conn)
    services = catalog(conn, admin=True)
    for service in services:
        tariff = service["tariff"]
        credits = tariff["unit_credits"]
        unit_price = (
            Decimal(str(credits)) * 100 / Decimal(pricing.points_per_yuan)
            if credits is not None and pricing is not None
            else None
        )
        cost = tariff["unit_cost_fen"]
        service["unit_price_fen"] = str(unit_price) if unit_price is not None else None
        service["gross_margin_percent"] = (
            str((unit_price - Decimal(str(cost))) / unit_price * 100)
            if tariff["enabled"]
            and service["customer_charge_allowed"]
            and unit_price is not None
            and unit_price > 0
            and cost is not None
            else None
        )
    return {
        "services": services,
        "pricing": {
            "version": version,
            "points_per_yuan": pricing.points_per_yuan if pricing else None,
        },
    }


@router.get("/api/control/billing/catalog")
def admin_catalog(_actor: AdminReader, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as raw:
        return admin_catalog_response(BusinessConnection.postgres(raw))


@router.put("/api/control/billing/tariff")
def update_tariff(
    payload: TariffUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, Any]:
    if payload.service not in SERVICES:
        raise HTTPException(422, detail="未知计费科目")
    if payload.tariff.enabled and (
        payload.tariff.unit_credits is None or not SERVICES[payload.service].customer_charge_allowed
    ):
        raise HTTPException(422, detail="该科目不允许启用用户收费，或尚未填写售价")
    if SERVICES[payload.service].zero_cost_platform and payload.tariff.unit_cost_fen not in {
        None,
        0,
    }:
        raise HTTPException(422, detail="云存储和支付通道按零费用核算")

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, Any]:
        conn = BusinessConnection.postgres(raw)
        # Serialize missing-row inserts as well as updates; one publication truth per subject.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("billing:tariffs",))
        pricing_version, _pricing = read_pricing(conn)
        if (
            payload.expected_pricing_version is not None
            and payload.expected_pricing_version != pricing_version
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "PRICING_VERSION_CONFLICT",
                    "message": "充值换算已变化，请取消编辑并重新读取价格后重试。",
                },
            )
        old = read_tariff(conn, payload.service)
        if (old.version if old else 0) != payload.expected_version:
            raise HTTPException(
                409,
                detail={
                    "code": "PRICING_VERSION_CONFLICT",
                    "message": "价格已变化，请刷新后重试。",
                },
            )
        tariff = payload.tariff
        conn.execute(
            """
            INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen,
              unit_rounding,updated_by_user_id)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(service) DO UPDATE SET
              enabled=excluded.enabled,unit_credits=excluded.unit_credits,unit_cost_fen=excluded.unit_cost_fen,
              unit_rounding=excluded.unit_rounding,updated_by_user_id=excluded.updated_by_user_id,
              version=billing_tariffs.version+1,updated_at=now()
        """,
            (
                payload.service,
                tariff.enabled,
                tariff.unit_credits,
                tariff.unit_cost_fen,
                tariff.unit_rounding,
                actor.user_id,
            ),
        )
        conn.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES (%s,%s,'billing.tariff.update','billing_tariff',%s,%s)",
            (
                str(uuid4()),
                actor.user_id,
                payload.service,
                json.dumps(
                    {
                        "old": old.model_dump(mode="json") if old else None,
                        "new": {
                            **tariff.model_dump(mode="json"),
                            "version": (old.version if old else 0) + 1,
                        },
                        "reason": payload.reason,
                        "request_id": request_id,
                    }
                ),
            ),
        )
        return admin_catalog_response(conn)

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="PRICING_UNAVAILABLE",
        unavailable_message="计价配置暂不可用。",
    )


@router.get("/api/customer/billing/catalog")
def customer_catalog(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    with customer_read_transaction(request) as (raw, _):
        return {"services": catalog(BusinessConnection.postgres(raw), admin=False)}


@router.get("/api/customer/billing/quote")
def customer_quote(
    request: Request, response: Response, service: str, units: str = "1"
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        with customer_read_transaction(request) as (raw, _):
            return retail_snapshot(BusinessConnection.postgres(raw), service, units)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/api/control/billing/tariff-history")
def tariff_history(
    _actor: AdminReader,
    service: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """P2-3：单个科目的价目版本序列（审计重建 + 当前行兜底），供历史账单回查。"""
    try:
        with pg_transaction() as raw:
            return tariff_versions(BusinessConnection.postgres(raw), service, limit=limit)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/api/control/billing/pricing-history")
def pricing_history(
    _actor: AdminReader, limit: int = Query(default=20, ge=1, le=100)
) -> dict[str, Any]:
    """P2-3：充值换算与折扣配置的版本序列，同样以审计为源。"""
    with pg_transaction() as raw:
        return pricing_versions(BusinessConnection.postgres(raw), limit=limit)


@router.get("/api/customer/billing/price-version")
def customer_price_version(
    request: Request, response: Response, service: str, version: int
) -> dict[str, object]:
    """P2-3：账单上的「费率 V{n}」按科目回查当时对外价目，只回公开字段。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        with customer_read_transaction(request) as (raw, _):
            return retail_version(BusinessConnection.postgres(raw), service, version)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


class BillingQuoteRequest(AdminWriteContract):
    """价格试算入参（方案 P1 价格与套餐）：客户可选、业务必选、数量按业务单位."""

    service: str
    units: Decimal = Field(gt=0, le=1_000_000)
    user_id: str | None = None


@router.post("/api/control/billing/quote")
def billing_quote(
    payload: BillingQuoteRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """价格试算（只读）：输入客户（可选）、业务、数量，输出原价、命中折扣、
    实扣积分、折合金额、我方成本与毛利。

    计算必须调用与结算同一套计价函数（``retail_snapshot``），前端不复刻公式
    （方案 P1 验收：试算与实际扣费一致）。写契约仅用于留痕与幂等——本接口
    不落任何业务数据。
    """

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        service = payload.service.strip()
        if service not in SERVICES:
            raise HTTPException(422, detail="未知计费科目")
        business_conn = BusinessConnection.postgres(conn)
        if payload.user_id is not None:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE id = %s", (payload.user_id.strip(),)
            ).fetchone()
            if exists is None:
                raise HTTPException(404, detail="客户不存在")
        snapshot = retail_snapshot(
            business_conn,
            service,
            payload.units,
            user_id=payload.user_id.strip() if payload.user_id else None,
        )
        tariff = read_tariff(business_conn, service)
        _, config = read_pricing(business_conn)
        points_per_yuan = config.points_per_yuan if config else None
        credits = Decimal(str(snapshot["credits"]))
        unit_cost = tariff.unit_cost_fen if tariff and tariff.unit_cost_fen is not None else None
        cost_fen = (
            None if unit_cost is None else (unit_cost * payload.units).quantize(Decimal("0.01"))
        )
        nominal_fen = (
            None
            if points_per_yuan is None
            else (credits * 100 / Decimal(points_per_yuan)).quantize(Decimal("0.01"))
        )
        return {
            "service": service,
            "label": SERVICES[service].name,
            "units": str(payload.units),
            "unit": SERVICES[service].unit,
            "credits": str(credits),
            "unit_credits": str(snapshot.get("unit_credits", "")),
            "discount_basis_points": snapshot.get("discount_basis_points"),
            "discount_rate": (
                str(snapshot["discount_rate"])
                if snapshot.get("discount_rate") is not None
                else None
            ),
            "discount_source": snapshot.get("discount_source"),
            "nominal_fen": str(nominal_fen) if nominal_fen is not None else None,
            "cost_fen": str(cost_fen) if cost_fen is not None else None,
            "gross_fen": (
                str((nominal_fen - cost_fen).quantize(Decimal("0.01")))
                if nominal_fen is not None and cost_fen is not None
                else None
            ),
            "request_id": request_id,
        }

    return write_with_idempotency(request, response, actor, payload, business, success_status=200)


@router.get("/api/control/billing/operations")
def operations(
    _actor: AdminReader,
    start: date,
    end: date,
    user_id: str | None = None,
    username: str | None = None,
    service: str | None = None,
    module: str | None = None,
    provider: str | None = None,
    attention: Literal["pending", "unknown_cost"] | None = None,
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with pg_transaction() as raw:
        rows = operation_rows(
            BusinessConnection.postgres(raw),
            start=start,
            end=end,
            user_id=user_id,
            username=username,
            service=service,
            module=module,
            provider=provider,
            attention=attention,
            limit=limit,
            offset=offset,
        )
        return {"items": rows, "total": rows[0]["total_count"] if rows else 0}


@router.get("/api/control/billing/statistics")
def report(
    _actor: AdminReader,
    start: date,
    end: date,
    grain: str = "day",
    user_id: str | None = None,
    username: str | None = None,
    service: str | None = None,
    module: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    with pg_transaction(isolation="REPEATABLE READ") as raw:
        return statistics(
            BusinessConnection.postgres(raw),
            start=start,
            end=end,
            grain=grain,
            user_id=user_id,
            username=username,
            service=service,
            module=module,
            provider=provider,
        )


@router.get("/api/control/billing/operations/{operation_id}")
def operation_detail(operation_id: str, _actor: AdminReader) -> dict[str, Any]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = operation_rows(
            conn, start=date(2000, 1, 1), end=date(9998, 12, 31), operation_id=operation_id
        )
        if not rows:
            raise HTTPException(404, detail="计费请求不存在")
        rows[0]["attempts"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM billing_effective_attempts WHERE operation_id=%s ORDER BY "
                "created_at,id",
                (operation_id,),
            ).fetchall()
        ]
        rows[0]["evidence"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM billing_evidence WHERE operation_id=%s ORDER BY created_at,id",
                (operation_id,),
            ).fetchall()
        ]
        return rows[0]


@router.get("/api/control/billing/source-actions")
def source_actions(
    _actor: AdminReader,
    start: date,
    end: date,
    user_id: str | None = None,
    username: str | None = None,
    service: str | None = None,
    module: str | None = None,
    provider: str | None = None,
    platform: bool = False,
    attention: Literal["pending", "unknown_cost"] | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """P1-5：按业务动作（source_id）聚合的经营视图，仍受同一套筛选参数约束。"""
    with pg_transaction() as raw:
        rows = source_action_rows(
            BusinessConnection.postgres(raw),
            start=start,
            end=end,
            user_id=user_id,
            username=username,
            service=service,
            module=module,
            provider=provider,
            platform=platform,
            attention=attention,
            limit=limit,
            offset=offset,
        )
        return {"items": rows, "total": rows[0]["total_count"] if rows else 0}


@router.get("/api/control/billing/source-actions/{source_id}")
def source_action_panorama(
    source_id: str,
    _actor: AdminReader,
    user_id: str | None = None,
    platform: bool = False,
) -> dict[str, Any]:
    """P1-5：一次操作的全部生成与供应商调用；作用域必须明确，避免混账。"""
    if user_id is None and not platform:
        raise HTTPException(422, detail="请指明用户或平台范围后再查看操作全景")
    with pg_transaction() as raw:
        detail = source_action_detail(
            BusinessConnection.postgres(raw),
            source_id=source_id,
            user_id=user_id,
            platform=platform,
        )
        if detail is None:
            raise HTTPException(404, detail="该操作不存在")
        return detail


@router.get("/api/control/billing/export")
def export(
    # GET 批量导出按仓库 B3 约定挂写级权限：数据出境动作看意图而非 HTTP 方法，
    # auditor（只读角色）不得整表拉取营收/成本明细。
    _actor: AdminWriter,
    start: date,
    end: date,
    user_id: str | None = None,
    username: str | None = None,
    service: str | None = None,
    module: str | None = None,
    provider: str | None = None,
    attention: Literal["pending", "unknown_cost"] | None = None,
) -> Response:
    with pg_transaction() as raw:
        rows = operation_rows(
            BusinessConnection.postgres(raw),
            start=start,
            end=end,
            user_id=user_id,
            username=username,
            service=service,
            module=module,
            provider=provider,
            attention=attention,
            limit=5000,
        )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    keys = [
        "id",
        "username",
        "service",
        "state",
        "unit",
        "actual_units",
        "charged_credits",
        "revenue_fen",
        "cost_fen",
        "profit_fen",
        "completed_at",
    ]
    writer.writerow(keys)
    for row in rows:
        writer.writerow([spreadsheet_safe_cell(row[key]) for key in keys])
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=billing-operations.csv",
            "X-Export-Truncated": str(bool(rows and rows[0]["total_count"] > 5000)).lower(),
        },
    )


class EvidenceUpdate(AdminWriteContract):
    operation_id: str
    attempt_id: str | None = None
    units: Decimal | None = Field(default=None, gt=0, le=2147483647, decimal_places=6)
    cost_fen: Decimal | None = Field(default=None, ge=0, le=1000000000000, decimal_places=8)
    reference: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_kind(self) -> EvidenceUpdate:
        if not self.reference.strip() or not (
            (self.attempt_id is None and self.units is not None and self.cost_fen is None)
            or (self.attempt_id is not None and self.units is None and self.cost_fen is not None)
        ):
            raise ValueError("请提供时长或单次调用成本，并填写核对凭据")
        return self


@router.post("/api/control/billing/evidence")
def append_evidence(
    payload: EvidenceUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, Any]:
    from app.billing_evidence import record_evidence

    def business(raw: psycopg.Connection, _request_id: str) -> dict[str, Any]:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("billing:evidence:" + payload.operation_id,),
        )
        return {
            "evidence_id": record_evidence(
                conn,
                operation_id=payload.operation_id,
                actor_id=actor.user_id,
                reference=payload.reference.strip(),
                reason=payload.reason.strip(),
                attempt_id=payload.attempt_id,
                units=payload.units,
                cost_fen=payload.cost_fen,
            )
        }

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="BILLING_EVIDENCE_UNAVAILABLE",
        unavailable_message="核对记录暂不可用。",
    )
