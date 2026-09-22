"""Customer price publication and audited administrator configuration."""

import json
from decimal import Decimal
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import AdminWriteContract, write_with_idempotency
from app.billing_catalog import SERVICES, Tariff, read_tariff, retail_snapshot
from app.customer_fence import customer_read_transaction
from app.customer_pricing import PricingConfig, read_pricing
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection

router = APIRouter(tags=["customer-pricing"])


class PriceEntry(BaseModel):
    subject: str
    name: str
    specification: str
    unit: str
    unit_credits: float
    configurable: bool


class PricingResponse(BaseModel):
    version: int
    configured: bool
    config: PricingConfig | None
    prices: list[PriceEntry]
    recharge_rounding: str = "按支付金额换算，向下取整到整数积分"


class PricingUpdate(AdminWriteContract):
    expected_version: int = Field(ge=0)
    config: PricingConfig


def pricing_response(conn: BusinessConnection, *, admin: bool = False) -> PricingResponse:
    version, config = read_pricing(conn)

    prices = []
    for subject, service in SERVICES.items():
        if not admin and not service.customer_charge_allowed:
            continue
        snapshot = retail_snapshot(conn, subject, 1)
        prices.append(
            PriceEntry(
                subject=subject,
                name=service.name,
                specification="按实际用量逐项扣分"
                if snapshot["enabled"]
                else "平台承担，用户不扣分",
                unit={"second": "秒", "image": "张", "call": "次"}[service.unit],
                unit_credits=float(str(snapshot["unit_credits"])) if snapshot["enabled"] else 0,
                configurable=service.customer_charge_allowed,
            )
        )
    return PricingResponse(
        version=version, configured=config is not None, config=config, prices=prices
    )


@router.get("/api/customer/pricing", response_model=PricingResponse)
def customer_prices(request: Request, response: Response) -> PricingResponse:
    response.headers["Cache-Control"] = "no-store"
    with customer_read_transaction(request) as (conn, _):
        return pricing_response(BusinessConnection.postgres(conn))


@router.get("/api/control/settings/customer-pricing", response_model=PricingResponse)
def admin_prices(_actor: AdminReader, response: Response) -> PricingResponse:
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as conn:
        return pricing_response(BusinessConnection.postgres(conn), admin=True)


@router.put("/api/control/settings/customer-pricing", response_model=PricingResponse)
def update_prices(
    payload: PricingUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("billing:tariffs",))
        old = conn.execute(
            "SELECT version, config_json FROM customer_credit_pricing WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if old is None or int(old[0]) != payload.expected_version:
            raise HTTPException(
                409,
                detail={
                    "code": "PRICING_VERSION_CONFLICT",
                    "message": "价格已被修改，请刷新后重试。",
                },
            )
        conn.execute(
            "UPDATE customer_credit_pricing SET config_json = %s, version = version + 1, "
            "updated_at = now() WHERE id = 1",
            (payload.config.model_dump_json(),),
        )
        # Legacy administrator callers can still explicitly configure the three original subjects.
        # Exchange-only forms omit these fields and never publish a usage charge.
        bridge = BusinessConnection.postgres(conn)
        for subject in ("video_768p", "video_2k", "oral"):
            value = getattr(payload.config, subject)
            if value is not None:
                previous = read_tariff(bridge, subject)
                conn.execute(
                    "INSERT INTO billing_tariffs(service,enabled,unit_credits,updated_by_user_id) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT(service) DO UPDATE SET "
                    "enabled=excluded.enabled, "
                    "unit_credits=excluded.unit_credits,version=billing_tariffs.version+1,"
                    "updated_at=now(), "
                    "updated_by_user_id=excluded.updated_by_user_id",
                    (subject, value > 0, value, actor.user_id),
                )
                # BILLING-OBS P2-3：同一版本轴上的隐式上调同样要留科目审计，
                # 否则版本序列断档，历史账单的「费率 V{n}」无法事后回查。
                conn.execute(
                    "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, "
                    "metadata_json) VALUES (%s, %s, 'billing.tariff.update', 'billing_tariff', "
                    "%s, %s)",
                    (
                        str(uuid4()),
                        actor.user_id,
                        subject,
                        json.dumps(
                            {
                                "old": previous.model_dump(mode="json") if previous else None,
                                "new": Tariff(
                                    enabled=value > 0,
                                    unit_credits=Decimal(value).quantize(Decimal("0.000001")),
                                    unit_cost_fen=previous.unit_cost_fen if previous else None,
                                    unit_rounding=previous.unit_rounding if previous else "ceil",
                                    version=(previous.version if previous else 0) + 1,
                                ).model_dump(mode="json"),
                                "reason": payload.reason,
                                "request_id": request_id,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, "
            "metadata_json) VALUES (%s, %s, 'customer_pricing.update', "
            "'customer_pricing', '1', %s)",
            (
                str(uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "old": json.loads(old[1]) if old[1] else None,
                        "new": payload.config.model_dump(),
                        "version": int(old[0]) + 1,
                        "reason": payload.reason,
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return pricing_response(BusinessConnection.postgres(conn), admin=True).model_dump()

    response.headers["Cache-Control"] = "no-store"
    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="PRICING_UNAVAILABLE",
        unavailable_message="积分配置暂不可用。",
    )
