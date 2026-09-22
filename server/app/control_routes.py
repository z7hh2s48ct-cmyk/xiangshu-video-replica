from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.admin_dates import append_admin_date_filters
from app.admin_write_contract import (
    AdminWriteContract,
)
from app.admin_write_contract import (
    require_write_contract as _require_write_contract,
)
from app.admin_write_contract import (
    write_with_idempotency as _write_with_idempotency,
)
from app.auth import Database, Role
from app.billing_catalog import SERVICES
from app.control_auth import ControlUser
from app.csv_export import spreadsheet_safe_cell
from app.db_portable import BusinessConnection
from app.failure_runbook import failure_advice
from app.ops_metrics import get_or_create_request_id
from app.permissions import write_audit
from app.security_rate_limit import (
    DIMENSION_CONTROL_EXPORT_ACCOUNT,
    consume_rate_limit,
    control_export_account_limit,
    rate_limit_window_seconds,
)
from app.settings import (
    ProviderName,
    ProviderTester,
    ProviderTestResult,
    SettingsRepository,
    get_provider_tester,
    merge_provider_config,
    remove_cos_lifecycle_rules,
    require_supported_provider,
)
from app.zpay import deployment_config_from_environment

router = APIRouter(prefix="/api/control", tags=["control"])
logger = logging.getLogger(__name__)
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
_TRUTHY = {"1", "true", "yes", "on"}
# 诊断是定点查询：任务编号/问题编号理论上唯一，命中上限只是「一个请求号关联到
# 一批任务」时的安全阀，不做分页——检索无结果是常态而非异常。
ANALYSIS_DIAGNOSTIC_MATCH_LIMIT = 20

OrderStatus = Literal["PENDING", "PAID", "FAILED", "CLOSED"]
# REFUND（20260923T1200_admin_refund_adjustment）：审计调账的反向记账类型，
# available_delta < 0，不挂订单/任务/计费轮次。
TransactionType = Literal["CHARGE", "RESERVE", "SETTLE", "RELEASE", "CONVERSION", "REFUND"]
GenerationRecordType = Literal[
    "VIDEO",
    "ORAL_VIDEO",
    "FIRST_FRAME_IMAGE",
    "CHARACTER_SHEET_IMAGE",
    "CHARACTER_VIEW_IMAGE",
    "SOURCE_FRAME_AI_SCORE",
    "SOURCE_FRAME_PROCESS",
    "ANALYSIS",
]
ProviderCostStatus = Literal["KNOWN", "ESTIMATED", "UNAVAILABLE", "NOT_APPLICABLE"]
RecordDataStatus = Literal["VALID", "UNAVAILABLE", "CORRUPTED"]


class AccountWallet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    display_name: str
    role: Role
    is_active: bool
    available_credits: int
    reserved_credits: int
    active_token_count: int


class AccountWalletPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AccountWallet]
    total: int
    limit: int
    offset: int


class ControlRechargeOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    username: str
    display_name: str
    order_no: str
    status: OrderStatus
    amount_fen: int
    credits: int
    channel: str
    provider: str
    # ZPay settles into provider_trade_no; WeChat Native is constrained to keep
    # that column NULL and put its transaction_id here (migration 083). Reading
    # only one of them leaves every WeChat order without a trade reference, so
    # reconciliation, refunds and support tickets have nothing to match on.
    provider_trade_no: str | None
    transaction_id: str | None
    created_at: str
    paid_at: str | None


class ControlRechargeOrderPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ControlRechargeOrder]
    total: int
    limit: int
    offset: int


class ControlWalletTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    username: str
    type: TransactionType
    available_delta: int
    reserved_delta: int
    recharge_order_id: str | None
    task_id: str | None
    billing_round: int | None
    created_at: str
    available_balance_after: int | None
    reserved_balance_after: int | None
    oral_task_id: str | None = None
    billing_operation_id: str | None = None
    source_id: str | None = None
    service: str | None = None
    service_name: str | None = None


class ControlWalletTransactionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ControlWalletTransaction]
    total: int
    limit: int
    offset: int


class ControlGenerationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    record_type: GenerationRecordType
    operation: str
    user_id: str
    username: str
    display_name: str
    project_id: str | None
    project_name: str | None
    status: str
    provider: str | None
    model: str | None
    provider_cost: float | None
    provider_cost_status: ProviderCostStatus
    record_data_status: RecordDataStatus
    charged_credits: int
    result_reference: str | None
    provider_reference: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    completed_at: str | None
    # 视频拆解专用：P0-2 落库的失败诊断。只有 analysis_tasks 有这些列，其他类型
    # 保持 None——「失败阶段/可否重试/上游原话」过去只活在日志里，管理端看不见。
    failure_phase: str | None = None
    retryable: bool | None = None
    upstream_status: int | None = None
    upstream_reason: str | None = None


class ControlGenerationRecordPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ControlGenerationRecord]
    total: int
    limit: int
    offset: int


class GenerationRecordCount(BaseModel):
    """生成记录聚合的一格：某类型 × 某状态的条数（原始状态，不做语义归并）。"""

    model_config = ConfigDict(extra="forbid")

    record_type: GenerationRecordType
    status: str
    count: int


class AnalysisFailureReason(BaseModel):
    """拆解失败原因聚合：回答「上游到底为什么拒绝」以及「能不能重试」。

    ``advice`` 是 P2-2 runbook（``app.failure_runbook``）的译文：管理端聚合
    列表直接展示，用户/客服不必拿内部错误码去别处搜索。
    """

    model_config = ConfigDict(extra="forbid")

    error_code: str | None
    failure_phase: str | None
    reason: str | None
    retryable: bool
    count: int
    advice: str | None = None


class ControlGenerationRecordSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    counts: list[GenerationRecordCount]
    failure_reasons: list[AnalysisFailureReason]


class AnalysisDiagnosticAttempt(BaseModel):
    """一次拆解尝试的结论：P1-6 按 attempt 归档进 ``analysis_task_attempts``。"""

    model_config = ConfigDict(extra="forbid")

    attempt: int
    status: str
    error_code: str | None
    error_message: str | None
    failure_phase: str | None
    retryable: bool
    upstream_status: int | None
    upstream_reason: str | None
    request_id: str | None
    created_at: str
    completed_at: str | None
    advice: str | None = None


class AnalysisDiagnosticRecord(BaseModel):
    """一个拆解任务的诊断全貌：任务行回答「最后一次」，attempts 回答「每次」。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    request_id: str | None
    username: str
    display_name: str
    project_id: str | None
    project_name: str | None
    status: str
    attempt: int
    error_code: str | None
    error_message: str | None
    failure_phase: str | None
    retryable: bool | None
    upstream_status: int | None
    upstream_reason: str | None
    created_at: str
    completed_at: str | None
    advice: str | None = None
    attempts: list[AnalysisDiagnosticAttempt]


class AnalysisDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AnalysisDiagnosticRecord]
    total: int


class ReconciliationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wallet_count: int
    wallet_mismatch_count: int
    paid_order_without_charge_count: int
    charge_without_paid_order_count: int
    pending_order_count: int


class ZPaySettingsUpdate(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    pid: str = Field(min_length=1)
    key: str | None = None
    enabled_channels: list[Literal["alipay", "wxpay"]] = Field(min_length=1)


class BillingSettingsUpdate(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    internal_base_unit_price_fen: StrictInt
    oral_unit_price_fen: StrictInt
    min_recharge_fen: StrictInt
    recharge_step_fen: StrictInt


class BillingSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_base_unit_price_fen: int
    charged_unit_price_fen: int
    oral_unit_price_fen: int
    min_recharge_fen: int
    recharge_step_fen: int


class MaskedZPaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["zpay"]
    configured: bool
    config: dict[str, str]


class DeploymentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_url: str
    notify_url: str
    return_url: str


class MaskedProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    configured: bool
    config: dict[str, str]


class RuntimeSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_generation_count_per_batch: int
    max_concurrent_h3_tasks: int
    active_storage_provider: Literal["cos", "local"]


class RuntimeSettingsUpdate(AdminWriteContract, RuntimeSettingsSnapshot):
    pass


class ControlProviderSettingsUpdate(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, str] = Field(default_factory=dict)


class ControlProviderPaidTestRequest(AdminWriteContract):
    """付费探针的写契约信封（confirm + reason；幂等键走 header）。

    与同段的免费 `connection-test`（普通 POST）不同：付费探针可能真实扣费，
    因此按「敏感写」处理，走 `_run_control_settings_write` 的同一套信封。
    """

    model_config = ConfigDict(extra="forbid")


class ControlSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing: BillingSettingsSnapshot
    zpay: MaskedZPaySettings
    deployment: DeploymentSettings
    providers: dict[ProviderName, MaskedProviderSettings]
    runtime: RuntimeSettingsSnapshot


def _runtime_settings_snapshot(settings: dict[str, int | str]) -> RuntimeSettingsSnapshot:
    return RuntimeSettingsSnapshot(
        max_generation_count_per_batch=int(settings["max_generation_count_per_batch"]),
        max_concurrent_h3_tasks=int(settings["max_concurrent_h3_tasks"]),
        active_storage_provider=cast(
            Literal["cos", "local"],
            str(settings["active_storage_provider"]),
        ),
    )


def _deployment_settings_snapshot() -> DeploymentSettings:
    try:
        deployment = deployment_config_from_environment()
    except ValueError:
        return DeploymentSettings(gateway_url="", notify_url="", return_url="")
    return DeploymentSettings(
        gateway_url=deployment.gateway_url,
        notify_url=deployment.notify_url,
        return_url=deployment.return_url,
    )


def _is_customer_production() -> bool:
    return os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class _ControlWriteActor:
    user_id: str


def _run_control_settings_write(
    request: Request,
    response: Response,
    actor: ControlUser,
    body: AdminWriteContract,
    conn: Database,
    business: Callable[[BusinessConnection, str], dict[str, object]],
) -> dict[str, object]:
    if not _is_customer_production():
        _require_write_contract(request, body)
        return business(conn, get_or_create_request_id(request))

    def pg_business(raw_conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        return business(BusinessConnection.postgres(raw_conn), request_id)

    return _write_with_idempotency(
        request,
        response,
        _ControlWriteActor(user_id=actor.id),
        body,
        pg_business,
        success_status=200,
        unavailable_code="CONTROL_SETTINGS_UNAVAILABLE",
        unavailable_message="Control settings writes require the PostgreSQL runtime.",
    )


def _invalid_settings_http(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message})


def _update_control_provider_settings_business(
    conn: BusinessConnection,
    *,
    actor: ControlUser,
    provider_name: ProviderName,
    config: dict[str, str],
    reason: str,
    request_id: str,
) -> dict[str, object]:
    try:
        repo = SettingsRepository(conn)
        current = repo.load_provider_config(provider_name)
        merged = merge_provider_config(current, config)
        saved = repo.save_provider_config(
            provider_name,
            merged,
            actor_user_id=actor.id,
        )
    except ValueError as exc:
        raise _invalid_settings_http("INVALID_SERVICE_SETTINGS", str(exc)) from exc
    write_audit(
        conn,
        actor=actor,
        action="provider_settings.update",
        entity_type="provider_settings",
        entity_id=provider_name,
        metadata={
            "provider": provider_name,
            "reason": reason.strip(),
            "request_id": request_id,
        },
    )
    if provider_name == "cos":
        lifecycle = remove_cos_lifecycle_rules(merged, actor_id=actor.id)
        write_audit(
            conn,
            actor=actor,
            action=f"cos_lifecycle.{lifecycle['status']}",
            entity_type="provider_settings",
            entity_id="cos",
            metadata={
                "status": lifecycle["status"],
                "reason": reason.strip(),
                "request_id": request_id,
            },
        )
    return cast(dict[str, object], saved)


def _update_control_runtime_settings_business(
    conn: BusinessConnection,
    *,
    actor: ControlUser,
    payload: RuntimeSettingsUpdate,
    request_id: str,
) -> dict[str, object]:
    try:
        saved = SettingsRepository(conn).save_runtime_settings(
            max_generation_count_per_batch=payload.max_generation_count_per_batch,
            max_concurrent_h3_tasks=payload.max_concurrent_h3_tasks,
            active_storage_provider=payload.active_storage_provider,
            actor_user_id=actor.id,
        )
    except ValueError as exc:
        raise _invalid_settings_http("INVALID_RUNTIME_SETTINGS", str(exc)) from exc
    write_audit(
        conn,
        actor=actor,
        action="runtime_settings.update",
        entity_type="runtime_settings",
        entity_id="1",
        metadata={
            "setting": "runtime_limits",
            "reason": payload.reason.strip(),
            "request_id": request_id,
        },
    )
    return cast(dict[str, object], saved)


def _update_control_zpay_settings_business(
    conn: BusinessConnection,
    *,
    actor: ControlUser,
    payload: ZPaySettingsUpdate,
    request_id: str,
) -> dict[str, object]:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('payment:settings'))")
    try:
        repo = SettingsRepository(conn)
        current = repo.load_zpay_config()
        incoming_key = (payload.key or "").strip()
        if incoming_key.startswith("********") or not incoming_key:
            incoming_key = current.get("key", "")
        result = repo.save_zpay_config(
            {
                "pid": payload.pid,
                "key": incoming_key,
                "enabled_channels": ",".join(payload.enabled_channels),
            },
            actor_user_id=actor.id,
        )
    except ValueError as exc:
        raise _invalid_settings_http("INVALID_ZPAY_SETTINGS", str(exc)) from exc
    write_audit(
        conn,
        actor=actor,
        action="zpay_settings.update",
        entity_type="provider_settings",
        entity_id="zpay",
        metadata={
            "enabled_channels": payload.enabled_channels,
            "reason": payload.reason.strip(),
            "request_id": request_id,
        },
    )
    return cast(dict[str, object], result)


def _update_control_billing_settings_business(
    conn: BusinessConnection,
    *,
    actor: ControlUser,
    payload: BillingSettingsUpdate,
    request_id: str,
) -> dict[str, object]:
    try:
        result = SettingsRepository(conn).save_billing_settings(
            internal_base_unit_price_fen=payload.internal_base_unit_price_fen,
            oral_unit_price_fen=payload.oral_unit_price_fen,
            min_recharge_fen=payload.min_recharge_fen,
            recharge_step_fen=payload.recharge_step_fen,
            actor_user_id=actor.id,
        )
    except ValueError as exc:
        raise _invalid_settings_http("INVALID_BILLING_SETTINGS", str(exc)) from exc
    write_audit(
        conn,
        actor=actor,
        action="billing_settings.update",
        entity_type="runtime_settings",
        entity_id="1",
        metadata={
            "scope": "INTERNAL",
            "reason": payload.reason.strip(),
            "request_id": request_id,
        },
    )
    return cast(dict[str, object], result)


@router.get("/accounts", response_model=AccountWalletPage)
def list_accounts(
    conn: Database,
    _actor: ControlUser,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> AccountWalletPage:
    total = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    rows = conn.execute(
        """
        SELECT
            users.id,
            users.username,
            users.display_name,
            users.role,
            users.is_active,
            wallets.available_credits,
            wallets.reserved_credits,
            COUNT(internal_access_tokens.id) AS active_token_count
        FROM users
        JOIN wallets ON wallets.user_id = users.id
        LEFT JOIN internal_access_tokens
          ON internal_access_tokens.user_id = users.id
         AND internal_access_tokens.revoked_at IS NULL
        GROUP BY users.id, wallets.available_credits, wallets.reserved_credits
        ORDER BY users.username, users.id
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    ).fetchall()
    return AccountWalletPage(
        items=[
            AccountWallet(
                id=str(row["id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                role=cast(Role, str(row["role"])),
                is_active=bool(row["is_active"]),
                available_credits=int(row["available_credits"]),
                reserved_credits=int(row["reserved_credits"]),
                active_token_count=int(row["active_token_count"]),
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/recharge-orders", response_model=ControlRechargeOrderPage)
def list_recharge_orders(
    conn: Database,
    _actor: ControlUser,
    status: OrderStatus | None = None,
    user_id: str | None = None,
    username: str | None = None,
    channel: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ControlRechargeOrderPage:
    where, params = _order_filters(
        postgres=conn.is_postgres,
        status=status,
        user_id=user_id,
        username=username,
        channel=channel,
        created_from=created_from,
        created_to=created_to,
    )
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM recharge_orders AS orders "
            f"JOIN users ON users.id = orders.user_id {where}",  # noqa: S608
            params,
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT
            orders.id,
            orders.user_id,
            users.username,
            users.display_name,
            orders.merchant_order_no AS order_no,
            orders.status,
            orders.amount_fen,
            orders.credits,
            COALESCE(orders.channel, '') AS channel,
            orders.provider,
            orders.provider_trade_no,
            orders.transaction_id,
            orders.created_at,
            orders.paid_at
        FROM recharge_orders AS orders
        JOIN users ON users.id = orders.user_id
        {where}
        ORDER BY orders.created_at DESC, orders.id DESC
        LIMIT %s OFFSET %s
        """,  # noqa: S608
        (*params, limit, offset),
    ).fetchall()
    return ControlRechargeOrderPage(
        items=[ControlRechargeOrder(**dict(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/wallet-transactions", response_model=ControlWalletTransactionPage)
def list_wallet_transactions(
    conn: Database,
    _actor: ControlUser,
    user_id: str | None = None,
    type: TransactionType | None = None,
    username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ControlWalletTransactionPage:
    where, params = _transaction_filters(
        postgres=conn.is_postgres,
        user_id=user_id,
        transaction_type=type,
        username=username,
        created_from=created_from,
        created_to=created_to,
    )
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM wallet_transactions AS tx "
            f"JOIN users ON users.id = tx.user_id {where}",  # noqa: S608
            params,
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT
            tx.id,
            tx.user_id,
            users.username,
            tx.type,
            tx.available_delta,
            tx.reserved_delta,
            tx.recharge_order_id,
            tx.task_id,
            tx.oral_task_id,
            tx.billing_operation_id,
            (SELECT op.source_id FROM billing_operations op
             WHERE op.id=tx.billing_operation_id AND op.user_id=tx.user_id) AS source_id,
            (SELECT op.service FROM billing_operations op
             WHERE op.id=tx.billing_operation_id AND op.user_id=tx.user_id) AS service,
            tx.billing_round,
            tx.created_at,
            CASE WHEN tx.ledger_sequence IS NULL THEN NULL ELSE
                (SELECT COALESCE(SUM(prev.available_delta), 0)
                 FROM wallet_transactions prev
                 WHERE prev.user_id = tx.user_id
                   AND (prev.ledger_sequence IS NULL
                        OR prev.ledger_sequence <= tx.ledger_sequence))
            END AS available_balance_after,
            CASE WHEN tx.ledger_sequence IS NULL THEN NULL ELSE
                (SELECT COALESCE(SUM(prev.reserved_delta), 0)
                 FROM wallet_transactions prev
                 WHERE prev.user_id = tx.user_id
                   AND (prev.ledger_sequence IS NULL
                        OR prev.ledger_sequence <= tx.ledger_sequence))
            END AS reserved_balance_after
        FROM wallet_transactions AS tx
        JOIN users ON users.id = tx.user_id
        {where}
        ORDER BY (tx.ledger_sequence IS NULL), tx.ledger_sequence DESC,
                 tx.created_at DESC, tx.id DESC
        LIMIT %s OFFSET %s
        """,  # noqa: S608
        (*params, limit, offset),
    ).fetchall()
    return ControlWalletTransactionPage(
        items=[
            ControlWalletTransaction(
                **dict(row),
                service_name=SERVICES[row["service"]].name if row["service"] in SERVICES else None,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/generation-records", response_model=ControlGenerationRecordPage)
def list_generation_records(
    conn: Database,
    _actor: ControlUser,
    username: str | None = None,
    status: str | None = None,
    record_type: GenerationRecordType | None = None,
    failure_phase: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ControlGenerationRecordPage:
    records: list[ControlGenerationRecord] = []
    scan_limit = offset + limit
    video_where, video_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("VIDEO",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    oral_where, oral_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("ORAL_VIDEO",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    first_where, first_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("FIRST_FRAME_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    sheet_where, sheet_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("CHARACTER_SHEET_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    view_where, view_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("CHARACTER_VIEW_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    source_where, source_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("SOURCE_FRAME_PROCESS", "SOURCE_FRAME_AI_SCORE"),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    analysis_where, analysis_params = _analysis_record_filters(
        postgres=conn.is_postgres,
        username=username,
        status=status,
        record_type=record_type,
        failure_phase=failure_phase,
        created_from=created_from,
        created_to=created_to,
    )
    if record_type in {"SOURCE_FRAME_PROCESS", "SOURCE_FRAME_AI_SCORE"}:
        semantic_requested_sql = """
            (
              CASE WHEN json_valid(versions.payload_json)
                THEN json_extract(
                  versions.payload_json, '$.semantic_quality_status'
                ) IN ('VERIFIED', 'UNAVAILABLE')
                ELSE 0
              END
              OR EXISTS (
                SELECT 1 FROM audit_logs quality_audit
                WHERE quality_audit.action = 'source_frame.semantic_quality_started'
                  AND quality_audit.entity_id = task.id
              )
            )
        """
        source_type_clause = (
            semantic_requested_sql
            if record_type == "SOURCE_FRAME_AI_SCORE"
            else f"NOT {semantic_requested_sql}"
        )
        conjunction = "AND" if source_where else "WHERE"
        source_where = f"{source_where} {conjunction} {source_type_clause}"
    total = int(
        conn.execute(
            f"""
            SELECT
                (SELECT COUNT(*) FROM generation_tasks task
                 JOIN generation_batches batch ON batch.id = task.batch_id
                 JOIN users ON users.id = batch.created_by_user_id {video_where})
              + (SELECT COUNT(*) FROM first_frame_tasks task
                 JOIN users ON users.id = task.created_by_user_id {first_where})
              + (SELECT COUNT(*) FROM character_sheet_tasks task
                 JOIN users ON users.id = task.created_by_user_id {sheet_where})
              + (SELECT COUNT(*) FROM character_generation_tasks task
                 JOIN users ON users.id = task.created_by {view_where})
              + (SELECT COUNT(*) FROM source_frame_tasks task
                 JOIN users ON users.id = task.created_by_user_id
                 LEFT JOIN versions ON versions.id = task.result_version_id {source_where})
              + (SELECT COUNT(*) FROM oral_tasks task
                 JOIN users ON users.id = task.owner_user_id {oral_where})
              + (SELECT COUNT(*) FROM analysis_tasks task
                 JOIN users ON users.id = task.created_by_user_id {analysis_where})
                AS total
            """,  # noqa: S608
            (
                *video_params,
                *first_params,
                *sheet_params,
                *view_params,
                *source_params,
                *oral_params,
                *analysis_params,
            ),
        ).fetchone()["total"]
    )

    video_rows = conn.execute(
        f"""
        SELECT
            task.id, task.generation_mode AS operation, task.status,
            task.provider, task.model, task.actual_cost, task.estimated_cost,
            task.result_asset_id AS result_reference, task.provider_task_id,
            task.error_code,
            task.error_message_redacted AS error_message,
            task.created_at, task.completed_at,
            batch.created_by_user_id AS user_id,
            users.username, users.display_name,
            batch.project_id, projects.name AS project_name,
            COALESCE((
                SELECT SUM(-tx.reserved_delta)
                FROM wallet_transactions AS tx
                WHERE tx.task_id = task.id AND tx.type = 'SETTLE'
            ), 0) AS charged_credits
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        JOIN users ON users.id = batch.created_by_user_id
        LEFT JOIN projects ON projects.id = batch.project_id
        {video_where}
        ORDER BY task.created_at DESC, task.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*video_params, scan_limit),
    ).fetchall()
    for row in video_rows:
        provider_cost = row["actual_cost"]
        # Video completion multiplies measured usage by the frozen configured
        # rate. The legacy actual_cost field is not a supplier invoice amount.
        provider_cost_status: ProviderCostStatus = "ESTIMATED"
        if provider_cost is None:
            provider_cost = row["estimated_cost"]
            provider_cost_status = "ESTIMATED"
        if provider_cost is None:
            provider_cost_status = "UNAVAILABLE"
        records.append(
            ControlGenerationRecord(
                record_id=str(row["id"]),
                record_type="VIDEO",
                operation=str(row["operation"]),
                user_id=str(row["user_id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                project_id=None if row["project_id"] is None else str(row["project_id"]),
                project_name=None if row["project_name"] is None else str(row["project_name"]),
                status=str(row["status"]),
                provider=str(row["provider"]),
                model=str(row["model"]),
                provider_cost=None if provider_cost is None else float(provider_cost),
                provider_cost_status=provider_cost_status,
                record_data_status="VALID",
                charged_credits=int(row["charged_credits"]),
                result_reference=(
                    None if row["result_reference"] is None else str(row["result_reference"])
                ),
                provider_reference=_optional_text(row["provider_task_id"]),
                error_code=None if row["error_code"] is None else str(row["error_code"]),
                error_message=_optional_text(row["error_message"]),
                created_at=str(row["created_at"]),
                completed_at=(None if row["completed_at"] is None else str(row["completed_at"])),
            )
        )

    first_frame_rows = conn.execute(
        f"""
        SELECT task.*, users.username, users.display_name,
               projects.name AS project_name, versions.payload_json
        FROM first_frame_tasks AS task
        JOIN users ON users.id = task.created_by_user_id
        JOIN projects ON projects.id = task.project_id
        LEFT JOIN versions ON versions.id = task.result_version_id
        {first_where}
        ORDER BY task.created_at DESC, task.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*first_params, scan_limit),
    ).fetchall()
    for row in first_frame_rows:
        request, request_status = _json_object(
            row["request_json"],
            record_id=str(row["id"]),
            field_name="request_json",
        )
        result, result_status = _json_object(
            row["payload_json"],
            record_id=str(row["id"]),
            field_name="payload_json",
        )
        task_result, task_result_status = _json_object(
            row["result_json"],
            record_id=str(row["id"]),
            field_name="result_json",
        )
        execution = _execution_metadata(task_result)
        records.append(
            _image_generation_record(
                conn=conn,
                row=row,
                record_type="FIRST_FRAME_IMAGE",
                operation="GENERATE",
                provider=_optional_text(result.get("provider") or execution.get("provider")),
                model=_optional_text(
                    result.get("model") or execution.get("model") or request.get("model")
                ),
                result_reference=_optional_text(row["result_version_id"]),
                record_data_status=_combined_record_data_status(
                    request_status,
                    result_status,
                    task_result_status,
                ),
            )
        )

    sheet_rows = conn.execute(
        f"""
        SELECT task.*, users.username, users.display_name,
               projects.name AS project_name
        FROM character_sheet_tasks AS task
        JOIN users ON users.id = task.created_by_user_id
        LEFT JOIN projects ON projects.id = task.project_id
        {sheet_where}
        ORDER BY task.created_at DESC, task.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*sheet_params, scan_limit),
    ).fetchall()
    for row in sheet_rows:
        result, result_status = _json_object(
            row["result_json"],
            record_id=str(row["id"]),
            field_name="result_json",
        )
        execution = _execution_metadata(result)
        generation_source = _optional_text(result.get("generation_source"))
        provider = _optional_text(result.get("provider") or execution.get("provider"))
        if provider is None and generation_source not in {None, "image_provider"}:
            provider = generation_source
        records.append(
            _image_generation_record(
                conn=conn,
                row=row,
                record_type="CHARACTER_SHEET_IMAGE",
                operation=str(row["operation"]),
                provider=provider,
                model=_optional_text(result.get("model") or execution.get("model")),
                result_reference=_optional_text(row["result_version_id"]),
                record_data_status=result_status,
            )
        )

    character_view_rows = conn.execute(
        f"""
        SELECT task.*, users.username, users.display_name
        FROM character_generation_tasks AS task
        JOIN users ON users.id = task.created_by
        {view_where}
        ORDER BY task.created_at DESC, task.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*view_params, scan_limit),
    ).fetchall()
    for row in character_view_rows:
        cost = None if row["cost_amount"] is None else float(row["cost_amount"])
        records.append(
            ControlGenerationRecord(
                record_id=str(row["id"]),
                record_type="CHARACTER_VIEW_IMAGE",
                operation=str(row["view_type"]),
                user_id=str(row["created_by"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                project_id=None,
                project_name=None,
                status=str(row["status"]),
                provider=str(row["provider"]),
                model=str(row["model"]),
                provider_cost=cost,
                provider_cost_status="UNAVAILABLE" if cost is None else "KNOWN",
                record_data_status="VALID",
                charged_credits=0,
                result_reference=_optional_text(row["provider_task_id"]),
                provider_reference=_optional_text(row["provider_task_id"]),
                error_code=_optional_text(row["error_code"]),
                error_message=_optional_text(row["error_message_redacted"]),
                created_at=str(row["created_at"]),
                completed_at=_optional_text(row["completed_at"]),
            )
        )

    source_rows = conn.execute(
        f"""
        SELECT task.*, users.username, users.display_name,
               projects.name AS project_name, versions.payload_json
        FROM source_frame_tasks AS task
        JOIN users ON users.id = task.created_by_user_id
        JOIN projects ON projects.id = task.project_id
        LEFT JOIN versions ON versions.id = task.result_version_id
        {source_where}
        ORDER BY task.created_at DESC, task.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*source_params, scan_limit),
    ).fetchall()
    source_task_ids = [str(row["id"]) for row in source_rows]
    source_quality_audits = []
    if source_task_ids:
        placeholders = ", ".join("%s" for _ in source_task_ids)
        source_quality_audits = conn.execute(
            f"""
            SELECT entity_id, metadata_json
            FROM audit_logs
            WHERE action = 'source_frame.semantic_quality_started'
              AND entity_id IN ({placeholders})
            ORDER BY created_at DESC, id DESC
            """,
            tuple(source_task_ids),
        ).fetchall()
    source_quality_by_task: dict[str, object] = {}
    for audit in source_quality_audits:
        source_quality_by_task.setdefault(str(audit["entity_id"]), audit["metadata_json"])
    for row in source_rows:
        payload, payload_status = _json_object(
            row["payload_json"],
            record_id=str(row["id"]),
            field_name="payload_json",
        )
        semantic_status = payload.get("semantic_quality_status")
        audit_payload, _ = _json_object(
            source_quality_by_task.get(str(row["id"])),
            record_id=str(row["id"]),
            field_name="source_quality_audit",
        )
        semantic_requested = semantic_status in {"VERIFIED", "UNAVAILABLE"} or bool(audit_payload)
        semantic_unknown = payload_status == "CORRUPTED"
        records.append(
            ControlGenerationRecord(
                record_id=str(row["id"]),
                record_type=(
                    "SOURCE_FRAME_AI_SCORE" if semantic_requested else "SOURCE_FRAME_PROCESS"
                ),
                operation=(
                    "SCORE_CANDIDATES"
                    if semantic_requested
                    else "UNKNOWN"
                    if semantic_unknown
                    else "EXTRACT_CANDIDATES"
                ),
                user_id=str(row["created_by_user_id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                project_id=str(row["project_id"]),
                project_name=str(row["project_name"]),
                status=str(row["status"]),
                provider=_optional_text(
                    payload.get("semantic_provider") or audit_payload.get("provider")
                ),
                model=_optional_text(payload.get("semantic_model") or audit_payload.get("model")),
                provider_cost=None,
                provider_cost_status=(
                    "UNAVAILABLE" if semantic_requested or semantic_unknown else "NOT_APPLICABLE"
                ),
                record_data_status=payload_status,
                charged_credits=0,
                result_reference=_optional_text(row["result_version_id"]),
                provider_reference=None,
                error_code=_optional_text(row["error_code"]),
                error_message=_optional_text(row["error_message_redacted"]),
                created_at=str(row["created_at"]),
                completed_at=_optional_text(row["completed_at"]),
            )
        )

    oral_rows = conn.execute(
        f"""
        SELECT
            task.id, task.mode AS operation, task.status,
            task.vendor_task_id, task.result_asset_id, task.error_message,
            task.created_at, task.updated_at,
            task.owner_user_id AS user_id,
            users.username, users.display_name,
            COALESCE((
                SELECT SUM(-tx.reserved_delta)
                FROM wallet_transactions AS tx
                WHERE tx.oral_task_id = task.id AND tx.type = 'SETTLE'
            ), 0) AS charged_credits
        FROM oral_tasks AS task
        JOIN users ON users.id = task.owner_user_id
        {oral_where}
        ORDER BY task.created_at DESC, task.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*oral_params, scan_limit),
    ).fetchall()
    terminal_oral_statuses = {
        "SUBMISSION_UNCERTAIN",
        "ARCHIVE_FAILED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    }
    oral_error_codes = {
        "SUBMISSION_UNCERTAIN": "ORAL_SUBMISSION_UNCERTAIN",
        "ARCHIVE_FAILED": "ORAL_ARCHIVE_FAILED",
        "FAILED": "ORAL_TASK_FAILED",
    }
    for row in oral_rows:
        oral_status = str(row["status"])
        records.append(
            ControlGenerationRecord(
                record_id=str(row["id"]),
                record_type="ORAL_VIDEO",
                operation=str(row["operation"]),
                user_id=str(row["user_id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                project_id=None,
                project_name=None,
                status=oral_status,
                provider="hifly",
                model=None,
                provider_cost=None,
                provider_cost_status="UNAVAILABLE",
                record_data_status="VALID",
                charged_credits=int(row["charged_credits"]),
                result_reference=_optional_text(row["result_asset_id"]),
                provider_reference=_optional_text(row["vendor_task_id"]),
                error_code=oral_error_codes.get(oral_status),
                error_message=_oral_admin_error_message(
                    status=oral_status,
                    raw_message=_optional_text(row["error_message"]),
                ),
                created_at=str(row["created_at"]),
                completed_at=(
                    str(row["updated_at"]) if oral_status in terminal_oral_statuses else None
                ),
            )
        )

    analysis_rows = conn.execute(
        f"""
        SELECT task.*, users.username, users.display_name,
               projects.name AS project_name, versions.payload_json
        FROM analysis_tasks AS task
        JOIN users ON users.id = task.created_by_user_id
        LEFT JOIN projects ON projects.id = task.project_id
        LEFT JOIN versions ON versions.id = task.result_version_id
        {analysis_where}
        ORDER BY task.created_at DESC, task.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*analysis_params, scan_limit),
    ).fetchall()
    for row in analysis_rows:
        row_status = str(row["status"])
        payload, payload_status = _json_object(
            row["payload_json"],
            record_id=str(row["id"]),
            field_name="payload_json",
        )
        provider_ref = payload.get("provider_response_ref")
        executed = provider_ref if isinstance(provider_ref, dict) else {}
        diagnostic_status, diagnostic_reason = _upstream_diagnostic(
            row["upstream_diagnostic_json"] if conn.is_postgres else None
        )
        charged_credits, provider_cost = analysis_task_billing(
            conn, task_id=str(row["id"]), user_id=str(row["created_by_user_id"])
        )
        records.append(
            ControlGenerationRecord(
                record_id=str(row["id"]),
                record_type="ANALYSIS",
                operation="ANALYZE_VIDEO",
                user_id=str(row["created_by_user_id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                project_id=str(row["project_id"]),
                project_name=_optional_text(row["project_name"]),
                status=row_status,
                # 失败行没有结果版本，上游服务/模型无处可取；不编造 apilio_gemini。
                provider=_optional_text(executed.get("provider")),
                model=_optional_text(executed.get("model")),
                provider_cost=provider_cost,
                provider_cost_status=("ESTIMATED" if provider_cost is not None else "UNAVAILABLE"),
                record_data_status=payload_status,
                charged_credits=charged_credits,
                result_reference=_optional_text(row["result_version_id"]),
                provider_reference=_optional_text(executed.get("response_id")),
                error_code=_optional_text(row["error_code"]),
                error_message=_optional_text(row["error_message_redacted"]),
                created_at=str(row["created_at"]),
                completed_at=_optional_text(row["completed_at"]),
                failure_phase=_optional_text(row["failure_phase"]),
                # 该列在成功/进行中行上是默认 0——直接透出会把「不适用」说成
                # 「不可重试」。只有失败行才谈「能不能重试」。
                retryable=(
                    None
                    if row_status != "FAILED" or row["retryable"] is None
                    else bool(row["retryable"])
                ),
                upstream_status=diagnostic_status,
                upstream_reason=diagnostic_reason,
            )
        )

    records.sort(key=lambda item: (item.created_at, item.record_id), reverse=True)
    return ControlGenerationRecordPage(
        items=records[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/generation-records/summary", response_model=ControlGenerationRecordSummary)
def summarize_generation_records(
    conn: Database,
    _actor: ControlUser,
    username: str | None = None,
    status: str | None = None,
    record_type: GenerationRecordType | None = None,
    failure_phase: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> ControlGenerationRecordSummary:
    """与列表同筛选口径的聚合。

    生成记录列表是分页的，管理端无法靠自己汇总，「筛选后 3 条失败」与「聚合里
    还有 12 条」会互相打脸；因此聚合与列表共用同一批过滤器，并额外回答「拆解
    为什么失败、能不能重试」——这正是 2026-09-20 事故里完全缺失的视角。
    """
    video_where, video_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("VIDEO",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    oral_where, oral_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("ORAL_VIDEO",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    first_where, first_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("FIRST_FRAME_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    sheet_where, sheet_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("CHARACTER_SHEET_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    view_where, view_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("CHARACTER_VIEW_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    source_where, source_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("SOURCE_FRAME_PROCESS", "SOURCE_FRAME_AI_SCORE"),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    analysis_where, analysis_params = _analysis_record_filters(
        postgres=conn.is_postgres,
        username=username,
        status=status,
        record_type=record_type,
        failure_phase=failure_phase,
        created_from=created_from,
        created_to=created_to,
    )
    # 源画面分支按审计留痕归类：列表里的 json_valid/json_extract 只有 SQLite 有，
    # PG 上会直接报函数不存在。列表还会按 payload 的 semantic_quality_status 兜底，
    # 所以极少数「只有 payload、没有审计」的历史行不在这份计数里。
    semantic_requested_sql = """
        EXISTS (
            SELECT 1 FROM audit_logs quality_audit
            WHERE quality_audit.action = 'source_frame.semantic_quality_started'
              AND quality_audit.entity_id = task.id
        )
    """
    rows = conn.execute(
        f"""
        SELECT record_type, status, COUNT(*) AS total FROM (
            SELECT 'VIDEO' AS record_type, task.status AS status
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            JOIN users ON users.id = batch.created_by_user_id
            {video_where}
            UNION ALL
            SELECT 'ORAL_VIDEO', task.status
            FROM oral_tasks AS task
            JOIN users ON users.id = task.owner_user_id
            {oral_where}
            UNION ALL
            SELECT 'FIRST_FRAME_IMAGE', task.status
            FROM first_frame_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {first_where}
            UNION ALL
            SELECT 'CHARACTER_SHEET_IMAGE', task.status
            FROM character_sheet_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {sheet_where}
            UNION ALL
            SELECT 'CHARACTER_VIEW_IMAGE', task.status
            FROM character_generation_tasks AS task
            JOIN users ON users.id = task.created_by
            {view_where}
            UNION ALL
            SELECT CASE WHEN {semantic_requested_sql}
                        THEN 'SOURCE_FRAME_AI_SCORE' ELSE 'SOURCE_FRAME_PROCESS' END,
                   task.status
            FROM source_frame_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {source_where}
            UNION ALL
            SELECT 'ANALYSIS', task.status
            FROM analysis_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {analysis_where}
        ) AS record_groups
        GROUP BY record_type, status
        ORDER BY record_type, status
        """,  # noqa: S608
        (
            *video_params,
            *oral_params,
            *first_params,
            *sheet_params,
            *view_params,
            *source_params,
            *analysis_params,
        ),
    ).fetchall()
    counts = [
        GenerationRecordCount(
            record_type=cast(GenerationRecordType, str(row["record_type"])),
            status=str(row["status"]),
            count=int(row["total"]),
        )
        for row in rows
    ]
    return ControlGenerationRecordSummary(
        total=sum(item.count for item in counts),
        counts=counts,
        failure_reasons=_analysis_failure_reasons(
            conn,
            username=username,
            status=status,
            record_type=record_type,
            failure_phase=failure_phase,
            created_from=created_from,
            created_to=created_to,
        ),
    )


@router.get("/analysis-diagnostics", response_model=AnalysisDiagnosticsResponse)
def get_analysis_diagnostics(
    conn: Database,
    _actor: ControlUser,
    task_id: str | None = None,
    request_id: str | None = None,
) -> AnalysisDiagnosticsResponse:
    """任务诊断视图：按任务编号 / 问题编号直查失败历史与上游诊断。

    报障入口只有卡片上的「任务编号 + 问题编号」；生成记录列表回答的是「最后一次
    怎么样了」，重试前的结论只在 ``analysis_task_attempts`` 里逐次留痕。两个
    条件都不给时拒绝——诊断是定点查询，不做全量日志浏览。
    """
    if not task_id and not request_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ANALYSIS_DIAGNOSTICS_QUERY_REQUIRED",
                "message": "Provide task_id or request_id to inspect one task.",
            },
        )
    clauses: list[str] = []
    params: list[str] = []
    if task_id:
        clauses.append("task.id = %s")
        params.append(task_id)
    if request_id:
        if conn.is_postgres:
            # 入队请求号落在任务行；失败当场的请求号只出现在尝试行——两者都要认。
            clauses.append(
                "(task.request_id = %s OR EXISTS ("
                "SELECT 1 FROM analysis_task_attempts attempt_row"
                " WHERE attempt_row.task_id = task.id"
                " AND attempt_row.request_id = %s))"
            )
            params.extend((request_id, request_id))
        else:
            # attempts 是 PG-only 表；SQLite 档案只认任务行上的请求号。
            clauses.append("task.request_id = %s")
            params.append(request_id)
    rows = conn.execute(
        f"""
        SELECT task.*, users.username, users.display_name,
               projects.name AS project_name
        FROM analysis_tasks AS task
        JOIN users ON users.id = task.created_by_user_id
        LEFT JOIN projects ON projects.id = task.project_id
        WHERE {" AND ".join(clauses)}
        ORDER BY task.created_at DESC, task.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*params, ANALYSIS_DIAGNOSTIC_MATCH_LIMIT),
    ).fetchall()
    task_ids = [str(row["id"]) for row in rows]
    attempts_by_task: dict[str, list[sqlite3.Row]] = {}
    # attempts 是 PG-only 表（迁移在 SQLite 档案里跳过）；开发环境降级为
    # 「无重试历史」，生产 PG 走全量。
    if task_ids and conn.is_postgres:
        placeholders = ", ".join("%s" for _ in task_ids)
        attempt_rows = conn.execute(
            f"""
            SELECT task_id, attempt, status, error_code, error_message_redacted,
                   failure_phase, retryable, upstream_diagnostic_json, request_id,
                   completed_at, created_at
            FROM analysis_task_attempts
            WHERE task_id IN ({placeholders})
            ORDER BY task_id, attempt
            """,  # noqa: S608
            tuple(task_ids),
        ).fetchall()
        for attempt_row in attempt_rows:
            attempts_by_task.setdefault(str(attempt_row["task_id"]), []).append(attempt_row)
    items = [
        _analysis_diagnostic_record(
            row,
            attempts=attempts_by_task.get(str(row["id"]), []),
            postgres=conn.is_postgres,
        )
        for row in rows
    ]
    return AnalysisDiagnosticsResponse(items=items, total=len(items))


@router.get("/billing-reconciliation", response_model=ReconciliationSummary)
def read_reconciliation(conn: Database, actor: ControlUser) -> ReconciliationSummary:
    # 对账读也是账务敏感读：留痕（不占导出预算，随读频次走）。
    write_audit(
        conn,
        actor=actor,
        action="control.reconciliation.read",
        entity_type="control_ledger",
        entity_id="billing_reconciliation",
    )
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM wallets) AS wallet_count,
            (
                SELECT COUNT(*)
                FROM wallets
                LEFT JOIN (
                    SELECT
                        user_id,
                        SUM(available_delta) AS available_total,
                        SUM(reserved_delta) AS reserved_total
                    FROM wallet_transactions
                    GROUP BY user_id
                ) AS ledger ON ledger.user_id = wallets.user_id
                WHERE wallets.available_credits != COALESCE(ledger.available_total, 0)
                   OR wallets.reserved_credits != COALESCE(ledger.reserved_total, 0)
            ) AS wallet_mismatch_count,
            (
                SELECT COUNT(*)
                FROM recharge_orders AS orders
                LEFT JOIN wallet_transactions AS tx
                  ON tx.recharge_order_id = orders.id AND tx.type = 'CHARGE'
                WHERE orders.status = 'PAID' AND tx.id IS NULL
            ) AS paid_order_without_charge_count,
            (
                SELECT COUNT(*)
                FROM wallet_transactions AS tx
                JOIN recharge_orders AS orders ON orders.id = tx.recharge_order_id
                WHERE tx.type = 'CHARGE' AND orders.status != 'PAID'
            ) AS charge_without_paid_order_count,
            (
                SELECT COUNT(*) FROM recharge_orders WHERE status = 'PENDING'
            ) AS pending_order_count
        """
    ).fetchone()
    return ReconciliationSummary(**dict(row))


@router.get("/settings", response_model=ControlSettingsSnapshot)
def read_control_settings(conn: Database, _actor: ControlUser) -> ControlSettingsSnapshot:
    repo = SettingsRepository(conn)
    return ControlSettingsSnapshot(
        billing=BillingSettingsSnapshot(**repo.read_billing_settings()),
        zpay=MaskedZPaySettings(**repo.read_zpay_config()),
        deployment=_deployment_settings_snapshot(),
        providers={
            cast(ProviderName, provider): MaskedProviderSettings(**config)
            for provider, config in repo.read_all_provider_configs().items()
        },
        runtime=_runtime_settings_snapshot(repo.read_runtime_settings()),
    )


@router.put(
    "/settings/providers/{provider}",
    response_model=MaskedProviderSettings,
)
def update_control_provider_settings(
    provider: str,
    payload: ControlProviderSettingsUpdate,
    conn: Database,
    actor: ControlUser,
    request: Request,
    response: Response,
) -> MaskedProviderSettings:
    provider_name = require_supported_provider(provider)
    try:
        saved = _run_control_settings_write(
            request,
            response,
            actor,
            payload,
            conn,
            lambda current_conn, request_id: _update_control_provider_settings_business(
                current_conn,
                actor=actor,
                provider_name=provider_name,
                config=payload.config,
                reason=payload.reason,
                request_id=request_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SERVICE_SETTINGS", "message": str(exc)},
        ) from exc
    return MaskedProviderSettings.model_validate(saved)


@router.post(
    "/settings/providers/{provider}/connection-test",
    response_model=ProviderTestResult,
    response_model_exclude_none=True,
)
def test_control_provider_connection(
    provider: str,
    conn: Database,
    _actor: ControlUser,
    tester: ProviderTester = Depends(get_provider_tester),
) -> ProviderTestResult:
    provider_name = require_supported_provider(provider)
    config = SettingsRepository(conn).load_provider_config(provider_name)
    return tester.connection_test(provider_name, config)


@router.post(
    "/settings/providers/{provider}/paid-test",
    response_model=ProviderTestResult,
    response_model_exclude_none=True,
)
def paid_test_control_provider(
    provider: str,
    payload: ControlProviderPaidTestRequest,
    conn: Database,
    actor: ControlUser,
    request: Request,
    response: Response,
    tester: ProviderTester = Depends(get_provider_tester),
) -> ProviderTestResult:
    """付费探针（管理端入口）：验证供应商账号能否真正跑通一次**计费**调用。

    与紧邻的免费 `connection-test` 是**同构但不同级**的一对：免费探针只读、
    普通 POST；付费探针可能真实扣费，因此按敏感写走既有管理写契约
    （`Idempotency-Key` + `confirm` + 非空 `reason`），并在同一事务里落一条
    `provider_settings.paid_test` 审计——旧 `/api/admin` 版不写审计，而
    同文件的 `diagnostic-test` 写，这条不对称在此处补齐。

    幂等键不是仪式：一次网络歧义重试若变成第二次付费调用就是真实的重复扣费，
    快照层让重放直接回放首次结果（`X-Idempotent-Replay: true`）。

    审计写在探针**成功返回之后**：探针抛错（含当前真实供应商客户端尚未接入的
    501 存根）时没有任何付费动作发生，也就没有可 attest 的事实；而且写契约的
    事务语义会让抛错前的写入回滚，提前写审计反而会得到「开发态留下、生产态被
    回滚」的不一致。
    """
    provider_name = require_supported_provider(provider)

    def business(current_conn: BusinessConnection, request_id: str) -> dict[str, object]:
        config = SettingsRepository(current_conn).load_provider_config(provider_name)
        result = tester.paid_test(provider_name, config)
        write_audit(
            current_conn,
            actor=actor,
            action="provider_settings.paid_test",
            entity_type="provider_settings",
            entity_id=provider_name,
            metadata={
                "provider": provider_name,
                "reason": payload.reason.strip(),
                "request_id": request_id,
                "test_kind": result.test_kind,
                "status": result.status,
            },
        )
        return result.model_dump(exclude_none=True)

    written = _run_control_settings_write(request, response, actor, payload, conn, business)
    return ProviderTestResult.model_validate(written)


@router.patch("/settings/runtime", response_model=RuntimeSettingsSnapshot)
def update_control_runtime_settings(
    payload: RuntimeSettingsUpdate,
    conn: Database,
    actor: ControlUser,
    request: Request,
    response: Response,
) -> RuntimeSettingsSnapshot:
    try:
        saved = _run_control_settings_write(
            request,
            response,
            actor,
            payload,
            conn,
            lambda current_conn, request_id: _update_control_runtime_settings_business(
                current_conn,
                actor=actor,
                payload=payload,
                request_id=request_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_RUNTIME_SETTINGS", "message": str(exc)},
        ) from exc
    return RuntimeSettingsSnapshot.model_validate(saved)


@router.patch("/settings/zpay", response_model=MaskedZPaySettings)
def update_zpay_settings(
    payload: ZPaySettingsUpdate,
    conn: Database,
    actor: ControlUser,
    request: Request,
    response: Response,
) -> MaskedZPaySettings:
    try:
        result = _run_control_settings_write(
            request,
            response,
            actor,
            payload,
            conn,
            lambda current_conn, request_id: _update_control_zpay_settings_business(
                current_conn,
                actor=actor,
                payload=payload,
                request_id=request_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_ZPAY_SETTINGS", "message": str(exc)},
        ) from exc
    return MaskedZPaySettings.model_validate(result)


@router.patch("/settings/billing", response_model=BillingSettingsSnapshot)
def update_control_billing_settings(
    payload: BillingSettingsUpdate,
    conn: Database,
    actor: ControlUser,
    request: Request,
    response: Response,
) -> BillingSettingsSnapshot:
    try:
        result = _run_control_settings_write(
            request,
            response,
            actor,
            payload,
            conn,
            lambda current_conn, request_id: _update_control_billing_settings_business(
                current_conn,
                actor=actor,
                payload=payload,
                request_id=request_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_BILLING_SETTINGS", "message": str(exc)},
        ) from exc
    return BillingSettingsSnapshot.model_validate(result)


@router.get("/recharge-orders.csv")
def export_recharge_orders_csv(
    conn: Database,
    actor: ControlUser,
    status: OrderStatus | None = None,
    user_id: str | None = None,
    username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    channel: str | None = None,
    limit: int = Query(default=5000, ge=1, le=5000),
) -> Response:
    _guard_ledger_export(
        conn,
        actor,
        kind="recharge_orders",
        filters={
            key: value
            for key, value in [
                ("user_id", user_id),
                ("username", username),
                ("created_from", created_from),
                ("created_to", created_to),
                ("status", status),
                ("channel", channel),
            ]
            if value
        },
        row_limit=limit,
    )
    where, params = _order_filters(
        status=status,
        user_id=user_id,
        channel=channel,
        username=username,
        created_from=created_from,
        created_to=created_to,
        postgres=conn.is_postgres,
    )
    rows = conn.execute(
        f"""
        SELECT
            orders.merchant_order_no,
            orders.user_id,
            users.username,
            orders.amount_fen,
            orders.credits,
            orders.status,
            COALESCE(orders.channel, '') AS channel,
            orders.provider,
            COALESCE(orders.provider_trade_no, '') AS provider_trade_no,
            COALESCE(orders.transaction_id, '') AS transaction_id,
            orders.created_at,
            COALESCE(orders.paid_at, '') AS paid_at,
            COUNT(*) OVER () AS export_total
        FROM recharge_orders AS orders
        JOIN users ON users.id = orders.user_id
        {where}
        ORDER BY orders.created_at DESC, orders.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*params, limit),
    ).fetchall()
    return _csv_response(
        filename="recharge-orders.csv",
        headers=(
            "order_no",
            "user_id",
            "username",
            "amount_fen",
            "credits",
            "status",
            "channel",
            "provider",
            "provider_trade_no",
            "transaction_id",
            "created_at",
            "paid_at",
        ),
        rows=rows,
        total=int(rows[0]["export_total"]) if rows else 0,
    )


@router.get("/wallet-transactions.csv")
def export_wallet_transactions_csv(
    conn: Database,
    actor: ControlUser,
    user_id: str | None = None,
    type: TransactionType | None = None,
    username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = Query(default=5000, ge=1, le=5000),
) -> Response:
    _guard_ledger_export(
        conn,
        actor,
        kind="wallet_transactions",
        filters={
            key: value
            for key, value in [
                ("user_id", user_id),
                ("username", username),
                ("created_from", created_from),
                ("created_to", created_to),
                ("type", type),
            ]
            if value
        },
        row_limit=limit,
    )
    where, params = _transaction_filters(
        user_id=user_id,
        transaction_type=type,
        username=username,
        created_from=created_from,
        created_to=created_to,
        postgres=conn.is_postgres,
    )
    rows = conn.execute(
        f"""
        SELECT
            tx.id,
            tx.user_id,
            users.username,
            tx.type,
            tx.available_delta,
            tx.reserved_delta,
            COALESCE(tx.recharge_order_id, '') AS recharge_order_id,
            COALESCE(tx.task_id, '') AS task_id,
            COALESCE(tx.oral_task_id, '') AS oral_task_id,
            COALESCE(CAST(tx.billing_round AS TEXT), '') AS billing_round,
            tx.created_at,
            COUNT(*) OVER () AS export_total
        FROM wallet_transactions AS tx
        JOIN users ON users.id = tx.user_id
        {where}
        ORDER BY tx.created_at DESC, tx.id DESC
        LIMIT %s
        """,  # noqa: S608
        (*params, limit),
    ).fetchall()
    return _csv_response(
        filename="wallet-transactions.csv",
        headers=(
            "id",
            "user_id",
            "username",
            "type",
            "available_delta",
            "reserved_delta",
            "recharge_order_id",
            "task_id",
            "oral_task_id",
            "billing_round",
            "created_at",
        ),
        rows=rows,
        total=int(rows[0]["export_total"]) if rows else 0,
    )


def _guard_ledger_export(
    conn: BusinessConnection,
    actor: ControlUser,
    *,
    kind: str,
    filters: dict[str, str],
    row_limit: int,
) -> None:
    """Audit + rate-limit one control-plane ledger export (A2, 2026-09-02).

    A one-shot dump of the whole ledger is the most sensitive read in the
    system: it now lands an ``audit_logs`` row naming the operator and spends
    one hit of a shared per-account budget (429 + Retry-After once exhausted).
    The identifier is hashed like every other rate-limit bucket identity. The
    budget is spent on the shared PostgreSQL limiter explicitly (the activation
    precedent) so the SQLite internal lane simply skips it instead of pointing
    the shared-window SQL at a non-PG clock.
    """
    if conn.is_postgres:
        decision = consume_rate_limit(
            conn.raw,
            dimension=DIMENSION_CONTROL_EXPORT_ACCOUNT,
            identifier=hashlib.sha256(actor.id.encode("utf-8")).hexdigest(),
            limit=control_export_account_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "CONTROL_EXPORT_RATE_LIMITED",
                    "message": "Too many ledger exports; retry after the cooldown.",
                },
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
    write_audit(
        conn,
        actor=actor,
        action="control.export",
        entity_type="control_ledger",
        entity_id=kind,
        metadata={"filters": filters, "limit": row_limit},
    )


def _order_filters(
    *,
    postgres: bool = False,
    status: OrderStatus | None,
    user_id: str | None,
    username: str | None = None,
    channel: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    if status is not None:
        clauses.append("orders.status = %s")
        params.append(status)
    if user_id is not None:
        clauses.append("orders.user_id = %s")
        params.append(user_id)
    if username:
        clauses.append("users.username LIKE %s")
        params.append(f"%{username}%")
    if channel:
        clauses.append("orders.channel = %s")
        params.append(channel)
    append_admin_date_filters(
        clauses,
        params,
        column="orders.created_at",
        created_from=created_from,
        created_to=created_to,
        postgres=postgres,
    )
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", tuple(params))


def _transaction_filters(
    *,
    postgres: bool = False,
    user_id: str | None,
    transaction_type: TransactionType | None,
    username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    if user_id is not None:
        clauses.append("tx.user_id = %s")
        params.append(user_id)
    if transaction_type is not None:
        clauses.append("tx.type = %s")
        params.append(transaction_type)
    if username:
        clauses.append("users.username LIKE %s")
        params.append(f"%{username}%")
    append_admin_date_filters(
        clauses,
        params,
        column="tx.created_at",
        created_from=created_from,
        created_to=created_to,
        postgres=postgres,
    )
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", tuple(params))


def _generation_record_filters(
    *,
    postgres: bool = False,
    record_types: tuple[GenerationRecordType, ...],
    username: str | None,
    status: str | None,
    record_type: GenerationRecordType | None,
    created_from: str | None,
    created_to: str | None,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    if record_type is not None and record_type not in record_types:
        clauses.append("1 = 0")
    if username:
        clauses.append("users.username LIKE %s")
        params.append(f"%{username}%")
    if status:
        clauses.append("task.status = %s")
        params.append(status)
    append_admin_date_filters(
        clauses,
        params,
        column="task.created_at",
        created_from=created_from,
        created_to=created_to,
        postgres=postgres,
    )
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", tuple(params))


def _analysis_record_filters(
    *,
    postgres: bool = False,
    username: str | None,
    status: str | None,
    record_type: GenerationRecordType | None,
    failure_phase: str | None,
    created_from: str | None,
    created_to: str | None,
) -> tuple[str, tuple[str, ...]]:
    """拆解分支的过滤器。

    ``failure_phase`` 只在这里拼接：只有 ``analysis_tasks`` 有该列，泄漏到其他
    分支会变成 SQL 报错（500），而不是「查不到」。
    """
    where, params = _generation_record_filters(
        postgres=postgres,
        record_types=("ANALYSIS",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
    )
    if failure_phase:
        conjunction = "AND" if where else "WHERE"
        where = f"{where} {conjunction} task.failure_phase = %s"
        params = (*params, failure_phase)
    return where, params


def _analysis_failure_reasons(
    conn: BusinessConnection,
    *,
    username: str | None,
    status: str | None,
    record_type: GenerationRecordType | None,
    failure_phase: str | None,
    created_from: str | None,
    created_to: str | None,
) -> list[AnalysisFailureReason]:
    """按「为什么失败」聚合拆解失败行：错误码 × 失败阶段 × 上游原话 × 可否重试。

    当前视图里根本没有失败行（状态过滤不是 FAILED）时返回空——不给一份与
    列表无关的失败清单。原因取自 P0-2 落库的上游诊断（``->>`` 只有 PG 的
    jsonb 支持；SQLite 开发库没有该列，自然也没有原因）。
    """
    if status and status != "FAILED":
        return []
    where, params = _analysis_record_filters(
        postgres=conn.is_postgres,
        username=username,
        status="FAILED",
        record_type=record_type,
        failure_phase=failure_phase,
        created_from=created_from,
        created_to=created_to,
    )
    reason_expr = "(task.upstream_diagnostic_json ->> 'reason')" if conn.is_postgres else "NULL"
    rows = conn.execute(
        f"""
        SELECT task.error_code AS error_code,
               task.failure_phase AS failure_phase,
               {reason_expr} AS reason,
               task.retryable AS retryable,
               COUNT(*) AS total
        FROM analysis_tasks AS task
        JOIN users ON users.id = task.created_by_user_id
        {where}
        GROUP BY task.error_code, task.failure_phase, {reason_expr}, task.retryable
        ORDER BY total DESC, task.error_code ASC, task.failure_phase ASC
        """,  # noqa: S608
        params,
    ).fetchall()
    items: list[AnalysisFailureReason] = []
    for row in rows:
        error_code = _optional_text(row["error_code"])
        items.append(
            AnalysisFailureReason(
                error_code=error_code,
                failure_phase=_optional_text(row["failure_phase"]),
                reason=_optional_text(row["reason"]),
                retryable=bool(row["retryable"]),
                count=int(row["total"]),
                advice=failure_advice(error_code),
            )
        )
    return items


def _oral_admin_error_message(*, status: str, raw_message: str | None) -> str | None:
    messages = {
        "SUBMISSION_UNCERTAIN": "数字人服务提交结果未知，请人工核对供应商任务。",
        "ARCHIVE_FAILED": "口播成片归档失败，请核对存储状态。",
        "FAILED": "数字人口播生成失败，请核对供应商任务和服务配置。",
    }
    if message := messages.get(status):
        return message
    return "口播任务处理异常，请核对任务状态。" if raw_message else None


def _image_generation_record(
    *,
    conn: BusinessConnection,
    row: sqlite3.Row,
    record_type: Literal["FIRST_FRAME_IMAGE", "CHARACTER_SHEET_IMAGE"],
    operation: str,
    provider: str | None,
    model: str | None,
    result_reference: str | None,
    record_data_status: RecordDataStatus,
) -> ControlGenerationRecord:
    billing = image_task_billing(
        conn, task_id=str(row["id"]), user_id=str(row["created_by_user_id"])
    )
    return ControlGenerationRecord(
        record_id=str(row["id"]),
        record_type=record_type,
        operation=operation,
        user_id=str(row["created_by_user_id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        project_name=None if row["project_name"] is None else str(row["project_name"]),
        status=str(row["status"]),
        provider=provider,
        model=model,
        provider_cost=billing[1],
        provider_cost_status=(
            "NOT_APPLICABLE"
            if provider in {"fake", "uploaded", "local_placeholder"}
            else "UNAVAILABLE"
            if billing[1] is None
            else "ESTIMATED"
        ),
        record_data_status=record_data_status,
        charged_credits=billing[0],
        result_reference=result_reference,
        provider_reference=None,
        error_code=_optional_text(row["error_code"]),
        error_message=_optional_text(row["error_message_redacted"]),
        created_at=str(row["created_at"]),
        completed_at=_optional_text(row["completed_at"]),
    )


def image_task_billing(
    conn: BusinessConnection,
    *,
    task_id: str,
    user_id: str,
    services: tuple[str, ...] = ("character", "first_frame"),
) -> tuple[int, float | None]:
    """Read settled points separately from attempts; retries must not multiply charges.

    Attempt costs use frozen configured prices, not a supplier invoice. If even
    one attempt lacks cost evidence the total remains unknown, rather than zero.
    ``services`` scopes the ledger read: image tasks keep the default, video
    analysis passes its own service name.
    """
    placeholders = ", ".join("%s" for _ in services)
    row = conn.execute(
        "SELECT COALESCE(SUM(charged_credits),0) FROM billing_operations "
        f"WHERE source_id=%s AND user_id=%s AND service IN ({placeholders})",  # noqa: S608
        (task_id, user_id, *services),
    ).fetchone()
    attempts = conn.execute(
        "SELECT COUNT(*), COUNT(a.cost_fen), SUM(a.cost_fen) FROM billing_attempts a "
        "JOIN billing_operations op ON op.id=a.operation_id "
        "WHERE op.source_id=%s AND op.user_id=%s "
        f"AND op.service IN ({placeholders})",  # noqa: S608
        (task_id, user_id, *services),
    ).fetchone()
    cost = float(attempts[2]) / 100 if attempts[0] and attempts[0] == attempts[1] else None
    return int(row[0]), cost


def analysis_task_billing(
    conn: BusinessConnection,
    *,
    task_id: str,
    user_id: str,
) -> tuple[int, float | None]:
    """视频拆解的结算证据：与图片任务共用账本口径，service 固定为 analysis。"""
    return image_task_billing(conn, task_id=task_id, user_id=user_id, services=("analysis",))


def _json_object(
    raw: object,
    *,
    record_id: str,
    field_name: str,
) -> tuple[dict[str, object], RecordDataStatus]:
    if raw is None:
        return {}, "UNAVAILABLE"
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.error(
            "generation record payload is corrupt: record_id=%s field=%s",
            record_id,
            field_name,
        )
        return {}, "CORRUPTED"
    if not isinstance(value, dict):
        logger.error(
            "generation record payload is not an object: record_id=%s field=%s",
            record_id,
            field_name,
        )
        return {}, "CORRUPTED"
    return cast(dict[str, object], value), "VALID"


def _combined_record_data_status(
    *statuses: RecordDataStatus,
) -> RecordDataStatus:
    if "CORRUPTED" in statuses:
        return "CORRUPTED"
    if "UNAVAILABLE" in statuses:
        return "UNAVAILABLE"
    return "VALID"


def _execution_metadata(payload: dict[str, object]) -> dict[str, object]:
    execution = payload.get("execution")
    return cast(dict[str, object], execution) if isinstance(execution, dict) else {}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _upstream_diagnostic(raw: object) -> tuple[int | None, str | None]:
    """回读 P0-2 落在 ``analysis_tasks.upstream_diagnostic_json`` 的诊断。

    psycopg 的 jsonb 回读是 dict，但文本形态（旧数据/其他驱动）也要认；
    坏数据只降级为「无诊断」，不能让整个列表 500。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, None
    if not isinstance(raw, dict):
        return None, None
    status = raw.get("http_status")
    # bool 是 int 的子类：True 不能当 HTTP 状态码。
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    return status, _optional_text(raw.get("reason"))


def _analysis_diagnostic_record(
    row: sqlite3.Row,
    *,
    attempts: list[sqlite3.Row],
    postgres: bool,
) -> AnalysisDiagnosticRecord:
    """把任务行 + 尝试历史拼成诊断记录。

    ``upstream_diagnostic_json`` 是 PG-only 列（SQLite 档案没有），降级为
    「无诊断」而不是报错；attempt 三态（FAILED/INTERRUPTED/SUPERSEDED）原样
    透出，管理端按状态标注语义。
    """
    row_status = str(row["status"])
    diagnostic_status, diagnostic_reason = _upstream_diagnostic(
        row["upstream_diagnostic_json"] if postgres else None
    )
    attempt_items: list[AnalysisDiagnosticAttempt] = []
    for attempt in attempts:
        attempt_status, attempt_reason = _upstream_diagnostic(attempt["upstream_diagnostic_json"])
        attempt_code = _optional_text(attempt["error_code"])
        attempt_items.append(
            AnalysisDiagnosticAttempt(
                attempt=int(attempt["attempt"]),
                status=str(attempt["status"]),
                error_code=attempt_code,
                error_message=_optional_text(attempt["error_message_redacted"]),
                failure_phase=_optional_text(attempt["failure_phase"]),
                retryable=bool(attempt["retryable"]),
                upstream_status=attempt_status,
                upstream_reason=attempt_reason,
                request_id=_optional_text(attempt["request_id"]),
                created_at=str(attempt["created_at"]),
                completed_at=_optional_text(attempt["completed_at"]),
                advice=failure_advice(attempt_code),
            )
        )
    record_code = _optional_text(row["error_code"])
    return AnalysisDiagnosticRecord(
        task_id=str(row["id"]),
        request_id=_optional_text(row["request_id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        project_name=_optional_text(row["project_name"]),
        status=row_status,
        attempt=int(row["attempt"]),
        error_code=record_code,
        error_message=_optional_text(row["error_message_redacted"]),
        failure_phase=_optional_text(row["failure_phase"]),
        # 与生成记录同款语义：非失败行不谈「能不能重试」，不用默认 0 冒充。
        retryable=(
            None if row_status != "FAILED" or row["retryable"] is None else bool(row["retryable"])
        ),
        upstream_status=diagnostic_status,
        upstream_reason=diagnostic_reason,
        created_at=str(row["created_at"]),
        completed_at=_optional_text(row["completed_at"]),
        advice=failure_advice(record_code),
        attempts=attempt_items,
    )


def _csv_response(
    *,
    filename: str,
    headers: tuple[str, ...],
    rows: list[sqlite3.Row],
    total: int | None = None,
) -> Response:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([spreadsheet_safe_cell(row[index]) for index in range(len(headers))])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            **(
                {
                    "X-Export-Total": str(total),
                    "X-Export-Returned": str(len(rows)),
                    "X-Export-Truncated": str(total > len(rows)).lower(),
                }
                if total is not None
                else {}
            ),
            "Access-Control-Expose-Headers": (
                "X-Export-Total, X-Export-Returned, X-Export-Truncated"
            ),
        },
    )
