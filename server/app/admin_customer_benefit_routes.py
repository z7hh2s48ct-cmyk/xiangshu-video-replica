"""管理端客户权益：代客开通套餐（线下已付款）与手工折扣.

需求（2026-09-27 用户确认）：

1. 客户线下已付款，管理员在客户详情里选一个充值套餐直接开通：积分 + 折扣权益都给，
   折扣沿用套餐的「最近覆盖」。落账形状与客户自助 ZPay 买套餐一致——PAID 套餐订单
   （冻结 ``package_snapshot_json``）+ CHARGE 流水 + 钱包入账 + 套餐权益行，外加一条
   ``admin_adjustments`` 审计（来源单 ``OFFLINE_PAYMENT`` + 收款凭证号）。对账、统计与
   客户侧展示因此不必区分「线上买的」和「后台开的」。
2. 管理员可为单个客户直接设置手工折扣（不挂套餐），手工折扣优先于套餐折扣。

为什么不复用 ``POST /customers/{id}/adjustments``：调账的积分由操作员填写、金额按单价
换算；套餐开通的积分与金额都由套餐定义，操作员能填的只有「哪个套餐」和凭证号。塞进
同一请求体只会多出一组互斥字段与分支校验，所以单开端点，内部复用调账的计价快照、
作用域推断与自助拦截。

写契约与调账相同：真实管理员会话、Idempotency-Key、confirm、非空 reason；
管理员不得给自己开通套餐或设折扣。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import ConfigDict, Field, StrictInt

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_customer_routes import (
    _current_billing_snapshot,
    _deny_admin_self_service,
    _infer_pricing_scope,
    _write_with_idempotency,
)
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.admin_write_contract import transaction_now_iso as _transaction_now_iso
from app.customer_benefits import (
    CustomerDiscountView,
    DiscountNotFound,
    DiscountNotManual,
    create_manual_discount,
    deactivate_manual_discount,
    list_customer_discounts,
    lock_customer_discounts,
)
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.db_portable import BusinessConnection
from app.recharge_packages import build_package_snapshot, grant_discount_from_snapshot, read_package

router = APIRouter(prefix="/api/control", tags=["admin-customer-benefits"])
logger = logging.getLogger(__name__)

OFFLINE_PAYMENT_SOURCE_TYPE = "OFFLINE_PAYMENT"
MAX_INT4 = 2_147_483_647


class PackageGrantRequest(AdminWriteRequest):
    model_config = ConfigDict(extra="forbid")

    package_id: str
    # 操作员确认时看到的套餐版本：套餐在此期间被改过就拒绝，避免按客户没付过的
    # 金额/积分/折扣开通。
    package_version: StrictInt
    # 线下收款凭证号（转账流水号等），与 admin_adjustments.source_document_ref 对齐。
    source_document_ref: str


class ManualDiscountRequest(AdminWriteRequest):
    model_config = ConfigDict(extra="forbid")

    discount_rate: Decimal
    applicable_interfaces: list[str] = Field(default_factory=list)
    valid_until: datetime | None = None


def _discount_payload(view: CustomerDiscountView) -> dict[str, object]:
    return {
        "id": view.id,
        "discount_rate": str(view.discount_rate),
        "applicable_interfaces": list(view.applicable_interfaces),
        "priority": view.priority,
        "is_active": view.is_active,
        "state": view.state,
        "valid_from": view.valid_from.isoformat(),
        "valid_until": view.valid_until.isoformat() if view.valid_until is not None else None,
        "source": view.source,
        "source_recharge_order_id": view.source_recharge_order_id,
        "package_name": view.package_name,
        "created_at": view.created_at.isoformat(),
    }


def _require_user(conn: psycopg.Connection, user_id: str) -> None:
    if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
        raise _http(404, "USER_NOT_FOUND", "客户不存在。")


def _write_discount_audit(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    action: str,
    discount_id: str,
    metadata: dict[str, object],
) -> None:
    conn.execute(
        "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, "
        "metadata_json) VALUES (%s, %s, %s, 'customer_discount', %s, %s)",
        (
            str(uuid.uuid4()),
            actor_user_id,
            action,
            discount_id,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
        ),
    )


# ---------------------------------------------------------------------------
# 代客开通套餐（线下已付款）
# ---------------------------------------------------------------------------


@router.post("/customers/{user_id}/package-grants", status_code=201)
def grant_customer_package(
    user_id: str,
    body: PackageGrantRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        reason = body.reason.strip()
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer_package.grant",
                reason=reason,
                request_id=request_id,
            )
        source_document_ref = body.source_document_ref.strip()
        if not source_document_ref:
            raise _http(400, "PACKAGE_GRANT_VALIDATION_FAILED", "请填写线下收款凭证号。")
        _require_user(conn, user_id)
        if conn.execute("SELECT 1 FROM wallets WHERE user_id = %s", (user_id,)).fetchone() is None:
            raise _http(404, "WALLET_NOT_FOUND", "客户钱包不存在。")

        business_conn = BusinessConnection.postgres(conn)
        package = read_package(business_conn, body.package_id)
        if package is None:
            raise _http(404, "RECHARGE_PACKAGE_NOT_FOUND", "充值套餐不存在。")
        if not package.is_active:
            raise _http(409, "RECHARGE_PACKAGE_INACTIVE", "充值套餐已停用，不能开通。")
        if package.version != body.package_version:
            raise _http(
                409,
                "RECHARGE_PACKAGE_VERSION_CONFLICT",
                "套餐已被修改，请刷新后核对金额与权益再开通。",
            )

        pricing_scope = _infer_pricing_scope(conn, user_id)
        billing = _current_billing_snapshot(conn, user_id=user_id, pricing_scope=pricing_scope)
        # 平台最低充值额对套餐单同样生效（ck_recharge_orders_amount_minimum）；
        # 先给出可读的 422，而不是让约束冲突变成 500。
        if package.amount_fen < billing["min_recharge_fen"]:
            raise _http(
                422,
                "RECHARGE_PACKAGE_BELOW_MINIMUM",
                "套餐金额低于平台最低充值额，不能开通。",
            )

        adjustment_id = str(uuid.uuid4())
        order_id = str(uuid.uuid4())
        paid_at = _transaction_now_iso(conn)
        snapshot = build_package_snapshot(package)
        conn.execute(
            """
            INSERT INTO recharge_orders
            (id, user_id, merchant_order_no, provider, status, pricing_scope,
             base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
             min_recharge_fen_snapshot, recharge_step_fen_snapshot,
             amount_fen, credits, paid_at, package_id, package_snapshot_json)
            VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                order_id,
                user_id,
                f"ADJ-{adjustment_id}",
                pricing_scope,
                billing["internal_base_unit_price_fen"],
                billing["charged_unit_price_fen"],
                billing["min_recharge_fen"],
                billing["recharge_step_fen"],
                package.amount_fen,
                package.credits,
                paid_at,
                package.id,
                json.dumps(snapshot, ensure_ascii=False),
            ),
        )
        charge_id = f"admin_adjustment:charge:{order_id}"
        conn.execute(
            """
            INSERT INTO wallet_transactions
            (id, user_id, type, available_delta, reserved_delta, recharge_order_id,
             task_id, billing_round, idempotency_key, auth_source)
            VALUES (%s, %s, 'CHARGE', %s, 0, %s, NULL, NULL, %s, 'internal')
            """,
            (charge_id, user_id, package.credits, order_id, charge_id),
        )
        # 先比较再相加：避免 available_credits + credits 本身溢出 int4（与调账同一护栏）。
        updated = conn.execute(
            "UPDATE wallets SET available_credits = available_credits + %s "
            "WHERE user_id = %s AND available_credits <= %s - %s "
            "RETURNING available_credits",
            (package.credits, user_id, MAX_INT4, package.credits),
        ).fetchone()
        if updated is None:
            raise _http(
                400,
                "PACKAGE_GRANT_VALIDATION_FAILED",
                "开通后客户余额将超出上限，请联系技术人员处理。",
            )
        # 与 ZPay 结算同一入口：幂等、最近覆盖、同客户串行都由它负责。快照刚由启用中的
        # 套餐生成，校验失败只可能是程序缺陷——让整笔事务回滚，不留「给了积分没给权益」。
        discount_id = grant_discount_from_snapshot(
            business_conn,
            user_id=user_id,
            source_recharge_order_id=order_id,
            package_snapshot=snapshot,
        )
        conn.execute(
            """
            INSERT INTO admin_adjustments
            (id, recharge_order_id, target_user_id, admin_user_id,
             source_document_type, source_document_ref, reason, request_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                adjustment_id,
                order_id,
                user_id,
                actor.user_id,
                OFFLINE_PAYMENT_SOURCE_TYPE,
                source_document_ref,
                reason,
                request_id,
                paid_at,
            ),
        )
        logger.info(
            "admin package grant: adjustment=%s order=%s user=%s package=%s "
            "credits=%d discount=%s actor=%s request=%s",
            adjustment_id,
            order_id,
            user_id,
            package.id,
            package.credits,
            discount_id,
            actor.user_id,
            request_id,
        )
        return {
            "adjustment_id": adjustment_id,
            "order_id": order_id,
            "package_id": package.id,
            "package_name": package.name,
            "amount_fen": package.amount_fen,
            "credits": package.credits,
            "discount_id": discount_id,
            "discount_rate": (
                str(package.discount_rate) if package.discount_rate is not None else None
            ),
            "discount_interfaces": list(package.discount_interfaces),
            "pricing_scope": pricing_scope,
            "wallet_balance_after": int(updated[0]),
            "source_document_type": OFFLINE_PAYMENT_SOURCE_TYPE,
            "source_document_ref": source_document_ref,
            "request_id": request_id,
        }

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=201,
        unavailable_code="PACKAGE_GRANT_UNAVAILABLE",
        unavailable_message="套餐开通暂不可用。",
    )


# ---------------------------------------------------------------------------
# 手工折扣
# ---------------------------------------------------------------------------


@router.get("/customers/{user_id}/discounts")
def read_customer_discounts(
    user_id: str, response: Response, actor: AdminReader
) -> dict[str, object]:
    del actor
    response.headers["Cache-Control"] = "no-store"
    try:
        with pg_transaction() as conn:
            items = list_customer_discounts(conn, user_id)
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, "CUSTOMER_DISCOUNT_UNAVAILABLE", "客户折扣暂不可用。") from exc
    return {"items": [_discount_payload(item) for item in items]}


@router.post("/customers/{user_id}/discounts", status_code=201)
def create_customer_discount(
    user_id: str,
    body: ManualDiscountRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        reason = body.reason.strip()
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer_discount.create",
                reason=reason,
                request_id=request_id,
            )
        _require_user(conn, user_id)
        lock_customer_discounts(conn, user_id)
        try:
            discount_id, replaced = create_manual_discount(
                conn,
                user_id=user_id,
                discount_rate=body.discount_rate,
                applicable_interfaces=body.applicable_interfaces,
                valid_until=body.valid_until,
                actor_user_id=actor.user_id,
            )
        except (ValueError, TypeError) as exc:
            raise _http(422, "INVALID_CUSTOMER_DISCOUNT", str(exc)) from exc
        created = next(
            item for item in list_customer_discounts(conn, user_id) if item.id == discount_id
        )
        _write_discount_audit(
            conn,
            actor_user_id=actor.user_id,
            action="customer_discount.create",
            discount_id=discount_id,
            metadata={
                "user_id": user_id,
                "discount_rate": str(created.discount_rate),
                "applicable_interfaces": list(created.applicable_interfaces),
                "valid_until": created.valid_until,
                "replaced_discount_ids": replaced,
                "reason": reason,
                "request_id": request_id,
            },
        )
        return {
            "discount": _discount_payload(created),
            "replaced_discount_ids": replaced,
            "request_id": request_id,
        }

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=201,
        unavailable_code="CUSTOMER_DISCOUNT_UNAVAILABLE",
        unavailable_message="客户折扣暂不可用。",
    )


@router.post("/customers/{user_id}/discounts/{discount_id}/deactivate")
def deactivate_customer_discount(
    user_id: str,
    discount_id: str,
    body: AdminWriteRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        reason = body.reason.strip()
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer_discount.deactivate",
                reason=reason,
                request_id=request_id,
            )
        lock_customer_discounts(conn, user_id)
        try:
            was_active = deactivate_manual_discount(conn, user_id=user_id, discount_id=discount_id)
        except DiscountNotFound as exc:
            raise _http(404, "CUSTOMER_DISCOUNT_NOT_FOUND", "折扣不存在。") from exc
        except DiscountNotManual as exc:
            raise _http(
                409,
                "CUSTOMER_DISCOUNT_NOT_MANUAL",
                "套餐权益由后续开通的套餐自动覆盖，不能手工停用。",
            ) from exc
        _write_discount_audit(
            conn,
            actor_user_id=actor.user_id,
            action="customer_discount.deactivate",
            discount_id=discount_id,
            metadata={
                "user_id": user_id,
                "was_active": was_active,
                "reason": reason,
                "request_id": request_id,
            },
        )
        return {"discount_id": discount_id, "was_active": was_active, "request_id": request_id}

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code="CUSTOMER_DISCOUNT_UNAVAILABLE",
        unavailable_message="客户折扣暂不可用。",
    )
