"""充值套餐路由 — 客户只读档位 + 管理端配置（R-A / R-C 契约）.

- 客户：``GET /api/customer/recharge-packages``（仅启用档位；走客户会话围栏，
  与 ``/api/customer/recharge-orders`` 同 ``recharge`` scope）。
- 管理端：``GET/POST/PUT /api/control/settings/recharge-packages``（AdminReader /
  AdminWriter 读写分离；写走 ``write_with_idempotency`` 幂等快照 + advisory lock +
  audit_logs，乐观锁 ``expected_version`` 冲突 409）。

定价冻结在**下单**：本模块只维护配置；订单侧由 ``recharge_routes`` 写
``package_snapshot_json``，结算侧由 ``zpay_payments`` 授予权益（R-D）。
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictInt

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import AdminWriteContract, write_with_idempotency
from app.customer_fence import customer_read_transaction
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.recharge_packages import (
    PackageNotFound,
    PackageVersionConflict,
    RechargePackage,
    create_package,
    list_packages,
    update_package,
)

router = APIRouter(tags=["recharge-packages"])

# 与 customer_pricing 的 'billing:tariffs' 同族：套餐配置的串行化写锁。
PACKAGE_LOCK_SCOPE = "billing:recharge-packages"


class RechargePackageView(BaseModel):
    """套餐的对外形状（客户与管理端共用；时间戳为 ISO 字符串）."""

    id: str
    name: str
    amount_fen: int
    credits: int
    # 折扣率 4 位小数字符串（如 "0.9000"）；None = 无权益档位。
    discount_rate: str | None
    discount_interfaces: list[str]
    sort_order: int
    is_active: bool
    version: int
    created_at: str | None = None
    updated_at: str | None = None


class RechargePackageListResponse(BaseModel):
    items: list[RechargePackageView]


class RechargePackageWrite(AdminWriteContract):
    """管理端新建/更新套餐的草稿（含写契约字段）."""

    name: str
    amount_fen: StrictInt
    credits: StrictInt
    discount_rate: Decimal | None = None
    discount_interfaces: list[str] = Field(default_factory=list)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True


class RechargePackageUpdate(RechargePackageWrite):
    expected_version: int = Field(ge=0)


def _view(package: RechargePackage) -> RechargePackageView:
    return RechargePackageView(
        id=package.id,
        name=package.name,
        amount_fen=package.amount_fen,
        credits=package.credits,
        discount_rate=str(package.discount_rate) if package.discount_rate is not None else None,
        discount_interfaces=list(package.discount_interfaces),
        sort_order=package.sort_order,
        is_active=package.is_active,
        version=package.version,
        created_at=package.created_at.isoformat() if package.created_at is not None else None,
        updated_at=package.updated_at.isoformat() if package.updated_at is not None else None,
    )


@router.get("/api/customer/recharge-packages", response_model=RechargePackageListResponse)
def customer_recharge_packages(request: Request, response: Response) -> RechargePackageListResponse:
    """客户侧档位列表：只返回启用行，按 ``sort_order, id`` 稳定排序."""
    response.headers["Cache-Control"] = "no-store"
    with customer_read_transaction(request) as (conn, _user_id):
        packages = list_packages(BusinessConnection.postgres(conn), active_only=True)
        return RechargePackageListResponse(items=[_view(package) for package in packages])


@router.get("/api/control/settings/recharge-packages", response_model=RechargePackageListResponse)
def admin_recharge_packages(_actor: AdminReader, response: Response) -> RechargePackageListResponse:
    """管理端档位列表：含停用行（后台需要看到全部配置）."""
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as conn:
        packages = list_packages(BusinessConnection.postgres(conn))
        return RechargePackageListResponse(items=[_view(package) for package in packages])


def _write_package_audit(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    action: str,
    package: RechargePackage,
    reason: str,
    request_id: str,
    old: dict[str, object] | None = None,
) -> None:
    metadata: dict[str, object] = {
        "package_id": package.id,
        "name": package.name,
        "amount_fen": package.amount_fen,
        "credits": package.credits,
        "discount_rate": str(package.discount_rate) if package.discount_rate is not None else None,
        "discount_interfaces": list(package.discount_interfaces),
        "sort_order": package.sort_order,
        "is_active": package.is_active,
        "version": package.version,
        "reason": reason,
        "request_id": request_id,
    }
    if old is not None:
        metadata["old"] = old
    metadata["new"] = {
        key: value
        for key, value in metadata.items()
        if key not in {"old", "reason", "request_id", "package_id"}
    }
    conn.execute(
        "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, "
        "metadata_json) VALUES (%s, %s, %s, 'recharge_package', %s, %s)",
        (
            str(uuid4()),
            actor_user_id,
            action,
            package.id,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )


def _invalid_package_error(exc: ValueError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "INVALID_RECHARGE_PACKAGE", "message": str(exc)},
    )


@router.post(
    "/api/control/settings/recharge-packages",
    response_model=RechargePackageView,
    status_code=201,
)
def create_recharge_package(
    payload: RechargePackageWrite,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """新建套餐（写契约 + 幂等快照 + advisory lock + 审计）."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (PACKAGE_LOCK_SCOPE,))
        try:
            package = create_package(
                BusinessConnection.postgres(conn),
                name=payload.name,
                amount_fen=payload.amount_fen,
                credits=payload.credits,
                discount_rate=payload.discount_rate,
                discount_interfaces=payload.discount_interfaces,
                sort_order=payload.sort_order,
                is_active=payload.is_active,
                actor_user_id=actor.user_id,
            )
        except ValueError as exc:
            raise _invalid_package_error(exc) from exc
        _write_package_audit(
            conn,
            actor_user_id=actor.user_id,
            action="recharge_package.create",
            package=package,
            reason=payload.reason,
            request_id=request_id,
        )
        return _view(package).model_dump()

    response.headers["Cache-Control"] = "no-store"
    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=201,
        unavailable_code="RECHARGE_PACKAGE_UNAVAILABLE",
        unavailable_message="充值套餐管理暂不可用。",
    )


@router.put(
    "/api/control/settings/recharge-packages/{package_id}",
    response_model=RechargePackageView,
)
def update_recharge_package(
    package_id: str,
    payload: RechargePackageUpdate,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """更新套餐（乐观锁：``expected_version`` 不符 → 409，重读重试）."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (PACKAGE_LOCK_SCOPE,))
        current = (
            BusinessConnection.postgres(conn)
            .execute(
                "SELECT id, name, amount_fen, credits, discount_rate, discount_interfaces, "
                "sort_order, is_active, version FROM recharge_packages WHERE id = %s",
                (package_id,),
            )
            .fetchone()
        )
        try:
            package = update_package(
                BusinessConnection.postgres(conn),
                package_id=package_id,
                expected_version=payload.expected_version,
                name=payload.name,
                amount_fen=payload.amount_fen,
                credits=payload.credits,
                discount_rate=payload.discount_rate,
                discount_interfaces=payload.discount_interfaces,
                sort_order=payload.sort_order,
                is_active=payload.is_active,
                actor_user_id=actor.user_id,
            )
        except PackageNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "RECHARGE_PACKAGE_NOT_FOUND", "message": "充值套餐不存在。"},
            ) from exc
        except PackageVersionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RECHARGE_PACKAGE_VERSION_CONFLICT",
                    "message": "套餐已被修改，请刷新后重试。",
                },
            ) from exc
        except ValueError as exc:
            raise _invalid_package_error(exc) from exc
        old = (
            {
                "name": str(current["name"]),
                "amount_fen": int(current["amount_fen"]),
                "credits": int(current["credits"]),
                "discount_rate": str(current["discount_rate"])
                if current["discount_rate"] is not None
                else None,
                "discount_interfaces": json.loads(str(current["discount_interfaces"])),
                "sort_order": int(current["sort_order"]),
                "is_active": bool(current["is_active"]),
                "version": int(current["version"]),
            }
            if current is not None
            else None
        )
        _write_package_audit(
            conn,
            actor_user_id=actor.user_id,
            action="recharge_package.update",
            package=package,
            reason=payload.reason,
            request_id=request_id,
            old=old,
        )
        return _view(package).model_dump()

    response.headers["Cache-Control"] = "no-store"
    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="RECHARGE_PACKAGE_UNAVAILABLE",
        unavailable_message="充值套餐管理暂不可用。",
    )
