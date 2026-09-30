from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

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
from app.control_auth import ControlUser, ControlWriter
from app.csv_export import spreadsheet_safe_cell
from app.db_portable import BusinessConnection
from app.external_calls import summarize_provider_message
from app.failure_runbook import failure_advice, failure_explanation
from app.material_thumbs import THUMBNAIL_URL_EXPIRES_IN
from app.media_routes import storage_for_asset
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
from app.sql_pagination import PAGE_CLAUSE
from app.storage import StorageBackendUnavailable
from app.zpay import deployment_config_from_environment

router = APIRouter(prefix="/api/control", tags=["control"])
logger = logging.getLogger(__name__)
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
_TRUTHY = {"1", "true", "yes", "on"}
# 诊断是定点查询：任务编号/问题编号理论上唯一，命中上限只是「一个请求号关联到
# 一批任务」时的安全阀，不做分页——检索无结果是常态而非异常。
ANALYSIS_DIAGNOSTIC_MATCH_LIMIT = 20
# 失败原因聚合的分组展示上限：分组按条数降序，尾部长尾（每个码 1 条的个别
# 客户问题）不在聚合卡里展开，避免一份几百行的清单。
FAILURE_REASON_GROUP_LIMIT = 20

OrderStatus = Literal["PENDING", "PAID", "FAILED", "CLOSED"]
# REFUND（20260923T1200_admin_refund_adjustment）：审计调账的反向记账类型，
# available_delta < 0，不挂订单/任务/计费轮次。
TransactionType = Literal["CHARGE", "RESERVE", "SETTLE", "RELEASE", "CONVERSION", "REFUND"]
GenerationRecordType = Literal[
    "VIDEO",
    "ORAL_VIDEO",
    # 口播分身与声音克隆是独立资源（oral_avatars / oral_voices），任务调用在写入侧
    # 就按这两个类型落库；列在这里，调用日志接口才能按各自主键直接查到（方案 P0-9）。
    "ORAL_AVATAR",
    "ORAL_VOICE",
    "FIRST_FRAME_IMAGE",
    "CHARACTER_SHEET_IMAGE",
    "CHARACTER_VIEW_IMAGE",
    "SOURCE_FRAME_AI_SCORE",
    "SOURCE_FRAME_PROCESS",
    "ANALYSIS",
]
# 调用日志的读取端另接受充值查单（RECHARGE_ORDER）：zpay.py 按商户单号落任务归属，
# 管理端用它定位一次充值到底请求了支付网关什么、对方怎么回（P0-9）。充值订单不是
# 生成记录，不进 GenerationRecordType——生成记录列表没有对应的任务表。
# 新增生成记录类型时，这里要同步补上，否则新类型的调用日志查不到。
ExternalCallRecordType = Literal[
    "VIDEO",
    "ORAL_VIDEO",
    "ORAL_AVATAR",
    "ORAL_VOICE",
    "FIRST_FRAME_IMAGE",
    "CHARACTER_SHEET_IMAGE",
    "CHARACTER_VIEW_IMAGE",
    "SOURCE_FRAME_AI_SCORE",
    "SOURCE_FRAME_PROCESS",
    "ANALYSIS",
    "RECHARGE_ORDER",
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
    # 方案 P0-10 / P0-11：每条失败记录都带中文处理建议与服务商原话（已脱敏），
    # 原话取自任务行或第三方接口调用日志，管理端据此判断该怎么处理。
    advice: str | None = None
    provider_error_code: str | None = None
    provider_message: str | None = None
    # 失败原因分类与处理人（方案 P1-1 / P2-1）：接口只给稳定代码，中文标签由
    # 前端词典翻译；与 advice 同源（failure_runbook 的同一张码表），一起填、
    # 一起为空。原话命中审核关键词时分类升为 CONTENT_REVIEW、处理人改为 SUPPORT。
    failure_category: str | None = None
    failure_owner: str | None = None
    # 方案 P1-1：失败任务的预扣积分是否已退回（存在 RELEASE 流水）。None 表示
    # 非失败状态或无法判定（SQLite 降级通道）。
    credits_refunded: bool | None = None
    # 方案 P2-2：该记录是否有可在线预览的成片/生成图产物。True 时管理端可用
    # 媒体端点查看原件（视频成片为视频，其余为图片）；缩略图另由签名直连端点提供。
    has_preview: bool = False


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
    """失败原因聚合：回答「上游到底为什么拒绝」以及「能不能重试」。

    方案 P1-3：从只覆盖拆解扩到全部任务类型（视频/口播/首帧/人物表/人物
    视图/取帧/拆解）——按「类型 + 错误码 + 失败阶段」分组计数，一眼看出
    是个别客户的问题还是整体故障。``reason`` 优先取任务行自带的上游诊断
    （拆解），没有时取该组样本任务在第三方接口调用日志里的服务商原话。

    ``advice`` 是 P2-2 runbook（``app.failure_runbook``）的译文，``failure_category``
    与 ``failure_owner`` 是方案 P1-1 的原因分类与处理人代码，标签由管理端词典翻译。
    """

    model_config = ConfigDict(extra="forbid")

    record_type: GenerationRecordType
    error_code: str | None
    failure_phase: str | None
    reason: str | None
    retryable: bool | None
    count: int
    advice: str | None = None
    # 聚合行同样带分类与处理人代码，便于「一眼看出这是谁的事」。
    failure_category: str | None = None
    failure_owner: str | None = None


class ControlGenerationRecordSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    counts: list[GenerationRecordCount]
    failure_reasons: list[AnalysisFailureReason]
    # 方案 P1「顶部聚合 4 张卡」的后端数据：成功率 / 失败数由 counts 推出，
    # 平均耗时只统计成功任务（失败任务的耗时没有运营含义）。
    succeeded_count: int = 0
    failed_count: int = 0
    success_rate_pct: float | None = None
    avg_duration_seconds: float | None = None


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
        f"""
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
        {PAGE_CLAUSE}
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
        {PAGE_CLAUSE}
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
        {PAGE_CLAUSE}
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
    status_group: Literal["queued", "running", "succeeded", "failed", "attention"] | None = None,
    record_type: GenerationRecordType | None = None,
    failure_phase: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    task_ref: Annotated[str | None, Query(max_length=200)] = None,
    project_name: Annotated[str | None, Query(max_length=100)] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ControlGenerationRecordPage:
    """``task_ref``：我方任务编号、8 位短编号、第三方任务号或第三方请求编号，
    任填一个都落到同一条记录（方案 P0-12）。``status_group`` 是 5 组运营口径
    （方案 P1），与显式 ``status`` 合并；``project_name`` 按项目名筛视频与
    拆解记录。"""
    records: list[ControlGenerationRecord] = []
    status = _merge_status(status, status_group)
    ref_filter = _task_ref_filter(conn, task_ref)
    scan_limit = offset + limit
    video_where, video_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("VIDEO",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    oral_where, oral_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("ORAL_VIDEO",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    first_where, first_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("FIRST_FRAME_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    sheet_where, sheet_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("CHARACTER_SHEET_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    view_where, view_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("CHARACTER_VIEW_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    source_where, source_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("SOURCE_FRAME_PROCESS", "SOURCE_FRAME_AI_SCORE"),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    analysis_where, analysis_params = _analysis_record_filters(
        postgres=conn.is_postgres,
        username=username,
        status=status,
        record_type=record_type,
        failure_phase=failure_phase,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    if record_type in {"SOURCE_FRAME_PROCESS", "SOURCE_FRAME_AI_SCORE"}:
        # 与 summary 聚合同口径：按审计留痕归类。json_valid/json_extract 只有
        # SQLite 有，PG 上直接报函数不存在；行渲染侧仍有 payload 兜底
        # （semantic_status in {...} or audit），极少数「只有 payload、没有审计」
        # 的历史行在筛选里不计入 AI 评分类，与 summary 计数一致。
        semantic_requested_sql = """
            EXISTS (
                SELECT 1 FROM audit_logs quality_audit
                WHERE quality_audit.action = 'source_frame.semantic_quality_started'
                  AND quality_audit.entity_id = task.id
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
                has_preview=row["result_reference"] is not None,
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
                has_preview=_has_candidate_asset(result),
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
                has_preview=bool(result.get("contact_sheet_asset_id")),
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
                has_preview=_has_candidate_asset(payload),
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
                has_preview=row["result_asset_id"] is not None,
                provider_reference=_optional_text(row["vendor_task_id"]),
                error_code=oral_error_codes.get(oral_status),
                error_message=_oral_admin_error_message(
                    status=oral_status,
                    raw_message=_optional_text(row["error_message"]),
                ),
                # 概要保持固定中文，原始报错（脱敏后）单独给出（P0-11）。
                provider_message=summarize_provider_message(_optional_text(row["error_message"])),
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
    page = records[offset : offset + limit]
    _attach_failure_explanations(conn, page)
    return ControlGenerationRecordPage(
        items=page,
        total=total,
        limit=limit,
        offset=offset,
    )


class ExternalCallSummary(BaseModel):
    """一次第三方接口调用的概要（不含响应体）。"""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    created_at: str
    provider: str
    model: str | None
    endpoint: str
    method: str | None
    url: str | None
    attempt: int | None
    http_status: int | None
    latency_ms: int | None
    outcome: str | None
    provider_task_id: str | None
    provider_request_id: str | None
    provider_error_code: str | None
    provider_message: str | None
    error_message: str | None
    # 请求摘要含客户提示词等内容：审计员只看元数据，这里对审计员置空。
    request_summary: Any = None
    response_body_bytes: int | None
    has_response_body: bool


class ExternalCallList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExternalCallSummary]
    # 匹配该记录的全部调用条数；items 最多 _CALL_LIST_LIMIT 条，
    # total 大于 items 长度时说明清单被截断（方案 #27）。
    total: int


class ExternalCallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    response_headers: dict[str, str] | None
    response_body: str | None
    response_body_bytes: int | None
    truncated: bool


class GenerationRecordThumbnail(BaseModel):
    """一条生成记录的派生缩略图地址（方案 P2-1）。

    只签入库时派生的小图（``<原对象键>.thumb.jpg``，480px）：原视频与原图仍走可吊销的
    代理通道，派生物按低敏感度接受 7 天窗口——与素材链路的既有口径一致。

    ``url`` 允许为空，三处都会为空：记录类型没有媒体（拆解/取帧）、结果资产已删、
    以及存储不是对象存储（本地盘不发外链）。管理端据此显示占位，不为历史记录补抽帧
    —— 那需要离线任务，另立。
    """

    model_config = ConfigDict(extra="forbid")

    record_type: str
    record_id: str
    url: str | None
    expires_in_seconds: int


# 结果落在单个资产上的记录类型 → 取该资产的 SQL。图片类的记录行只带版本号，经
# versions.result_asset_id 一跳取到产物；拆解与取帧没有媒体，不在表内。
_RECORD_ASSET_SQL: dict[str, str] = {
    "VIDEO": "SELECT result_asset_id AS asset_id FROM generation_tasks WHERE id = %s",
    "ORAL_VIDEO": "SELECT result_asset_id AS asset_id FROM oral_tasks WHERE id = %s",
    "FIRST_FRAME_IMAGE": (
        "SELECT versions.result_asset_id AS asset_id FROM first_frame_tasks AS task "
        "JOIN versions ON versions.id = task.result_version_id WHERE task.id = %s"
    ),
    "CHARACTER_SHEET_IMAGE": (
        "SELECT versions.result_asset_id AS asset_id FROM character_sheet_tasks AS task "
        "JOIN versions ON versions.id = task.result_version_id WHERE task.id = %s"
    ),
    "CHARACTER_VIEW_IMAGE": (
        "SELECT versions.result_asset_id AS asset_id FROM character_generation_tasks AS task "
        "JOIN versions ON versions.id = task.result_version_id WHERE task.id = %s"
    ),
}


def _record_result_asset_id(
    conn: BusinessConnection, *, record_type: str, record_id: str
) -> str | None:
    sql = _RECORD_ASSET_SQL.get(record_type)
    if sql is None:
        return None
    row = conn.execute(sql, (record_id,)).fetchone()
    return None if row is None else _optional_text(row["asset_id"])


def _signed_record_thumbnail_url(conn: BusinessConnection, asset_id: str) -> str | None:
    """为派生小图签出对象存储直连地址；非对象存储、无缩略图键、已删资产一律 None。"""
    row = conn.execute(
        "SELECT storage_uri, metadata_json, content_type FROM assets WHERE id = %s",
        (asset_id,),
    ).fetchone()
    if row is None:
        return None
    if not str(row["content_type"] or "").startswith(("video/", "image/")):
        return None
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except (TypeError, ValueError):
        return None
    thumbnail_key = metadata.get("thumbnail_key") if isinstance(metadata, dict) else None
    if not isinstance(thumbnail_key, str) or not thumbnail_key:
        # 历史资产没有记键（抽帧写入点上线前入库，或当初抽帧失败）。键虽可由原对象键
        # 确定性派生，但对象是否真被派生过不确定，签出去只会得到必然 404 的图——
        # 管理端宁可显示占位，也不给一张加载不出来的图。
        return None
    try:
        storage = storage_for_asset(conn, str(row["storage_uri"]))
    except StorageBackendUnavailable:
        return None
    if storage.provider != "cos":
        # 本地盘：缩略图只能由应用服务器代理转发，管理端不为此新增一条穿透通道
        # （原图已有可吊销的代理通道，需要时再统一）。返回 None 让前端显示占位。
        return None
    return storage.create_download_intent(
        thumbnail_key, expires_in=THUMBNAIL_URL_EXPIRES_IN, can_read=True
    ).url


@router.get(
    "/generation-records/{record_type}/{record_id}/thumbnail",
    response_model=GenerationRecordThumbnail,
)
def read_generation_record_thumbnail(
    conn: Database,
    actor: ControlUser,
    record_type: GenerationRecordType,
    record_id: str,
) -> GenerationRecordThumbnail:
    """签发该条生成记录的缩略图地址（方案 P2-1）。

    查看客户媒体要留痕：签出成功时写一条审计（与资产下载签发的既有口径一致）。
    """
    expires_in_seconds = int(THUMBNAIL_URL_EXPIRES_IN.total_seconds())
    empty = GenerationRecordThumbnail(
        record_type=record_type,
        record_id=record_id,
        url=None,
        expires_in_seconds=expires_in_seconds,
    )
    if not conn.is_postgres:
        return empty
    asset_id = _record_result_asset_id(conn, record_type=record_type, record_id=record_id)
    if asset_id is None:
        return empty
    url = _signed_record_thumbnail_url(conn, asset_id)
    if url is None:
        return empty
    write_audit(
        conn,
        actor=actor,
        action="generation_record.thumbnail_view",
        entity_type="asset",
        entity_id=asset_id,
        metadata={"record_type": record_type, "record_id": record_id},
    )
    return GenerationRecordThumbnail(
        record_type=record_type,
        record_id=record_id,
        url=url,
        expires_in_seconds=expires_in_seconds,
    )


_CALL_LIST_LIMIT = 200


@router.get(
    "/generation-records/{record_type}/{record_id}/calls",
    response_model=ExternalCallList,
)
def list_generation_record_calls(
    conn: Database,
    actor: ControlUser,
    record_type: ExternalCallRecordType,
    record_id: str,
) -> ExternalCallList:
    """某条生成记录（或充值订单）的全部第三方接口调用，按时间顺序（方案 P0-9）。"""
    if not conn.is_postgres:
        # 非 PG 环境下没有调用日志表：集合端点返回空清单，单体端点
        # （read_external_call_response）对必然不存在的 id 返回 404——
        # 两者是同一事实（这里没有调用日志）在集合/单体上的统一口径。
        return ExternalCallList(items=[], total=0)
    # 源画面两种记录类型共用一个工作流程，调用日志统一记为 SOURCE_FRAME；
    # 口播分身/声音克隆的日志本就按各自主类型落库，直接匹配即可。
    call_type = (
        "SOURCE_FRAME"
        if record_type in {"SOURCE_FRAME_PROCESS", "SOURCE_FRAME_AI_SCORE"}
        else record_type
    )
    rows = conn.execute(
        """
        SELECT id, created_at, provider, model, endpoint_name, method, url_redacted, attempt,
               http_status, latency_ms, outcome, provider_task_id, provider_request_id,
               provider_error_code, provider_message, error_message_redacted,
               request_summary_json, response_body_bytes,
               response_body IS NOT NULL AS has_response_body,
               count(*) OVER () AS total_count
        FROM external_call_logs
        WHERE task_type = %s AND task_id = %s
        ORDER BY created_at, id
        LIMIT %s
        """,
        (call_type, record_id, _CALL_LIST_LIMIT),
    ).fetchall()
    show_summary = actor.role != "auditor"
    return ExternalCallList(
        total=int(rows[0]["total_count"]) if rows else 0,
        items=[
            ExternalCallSummary(
                call_id=str(row["id"]),
                created_at=str(row["created_at"]),
                provider=str(row["provider"]),
                model=_optional_text(row["model"]),
                endpoint=str(row["endpoint_name"]),
                method=_optional_text(row["method"]),
                url=_optional_text(row["url_redacted"]),
                attempt=None if row["attempt"] is None else int(row["attempt"]),
                http_status=None if row["http_status"] is None else int(row["http_status"]),
                latency_ms=None if row["latency_ms"] is None else int(row["latency_ms"]),
                outcome=_optional_text(row["outcome"]),
                provider_task_id=_optional_text(row["provider_task_id"]),
                provider_request_id=_optional_text(row["provider_request_id"]),
                provider_error_code=_optional_text(row["provider_error_code"]),
                provider_message=_optional_text(row["provider_message"]),
                error_message=_optional_text(row["error_message_redacted"]),
                request_summary=_json_value(row["request_summary_json"]) if show_summary else None,
                response_body_bytes=(
                    None if row["response_body_bytes"] is None else int(row["response_body_bytes"])
                ),
                has_response_body=bool(row["has_response_body"]),
            )
            for row in rows
        ],
    )


@router.get("/external-calls/{call_id}/response", response_model=ExternalCallResponse)
def read_external_call_response(
    conn: Database,
    actor: ControlUser,
    call_id: str,
) -> ExternalCallResponse:
    """读取一次调用的原始响应（已脱敏）；每次查看都写高敏审计。

    审计员只能看调用概要：原始响应可能含客户内容，不对只读角色开放。
    """
    if actor.role == "auditor":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "EXTERNAL_CALL_RESPONSE_FORBIDDEN",
                "message": "审计员只能查看调用概要，不能查看接口原始响应。",
            },
        )
    if not conn.is_postgres:
        # 与清单端点同口径：非 PG 环境没有调用日志，清单为空、单体按 404。
        raise HTTPException(
            status_code=404,
            detail={"code": "EXTERNAL_CALL_NOT_FOUND", "message": "没有找到这次接口调用。"},
        )
    row = conn.execute(
        """
        SELECT id, task_type, task_id, response_headers_json, response_body,
               response_body_bytes
        FROM external_call_logs WHERE id = %s
        """,
        (call_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "EXTERNAL_CALL_NOT_FOUND", "message": "没有找到这次接口调用。"},
        )
    body = _optional_text(row["response_body"])
    body_bytes = None if row["response_body_bytes"] is None else int(row["response_body_bytes"])
    write_audit(
        conn,
        actor=actor,
        action="external_call.response_view",
        entity_type="external_call_log",
        entity_id=call_id,
        metadata={
            "task_type": _optional_text(row["task_type"]),
            "task_id": _optional_text(row["task_id"]),
        },
    )
    headers = _json_value(row["response_headers_json"])
    return ExternalCallResponse(
        call_id=str(row["id"]),
        response_headers=headers if isinstance(headers, dict) else None,
        response_body=body,
        response_body_bytes=body_bytes,
        truncated=body is not None
        and body_bytes is not None
        and len(body.encode("utf-8")) < body_bytes,
    )


def _json_value(value: object) -> Any:
    """jsonb 列在 psycopg 下已是 Python 对象；兼容以文本存放的情形。"""
    if value is None or isinstance(value, dict | list):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


@router.get("/generation-records/summary", response_model=ControlGenerationRecordSummary)
def summarize_generation_records(
    conn: Database,
    _actor: ControlUser,
    username: str | None = None,
    status: str | None = None,
    status_group: Literal["queued", "running", "succeeded", "failed", "attention"] | None = None,
    record_type: GenerationRecordType | None = None,
    failure_phase: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    task_ref: Annotated[str | None, Query(max_length=200)] = None,
    project_name: Annotated[str | None, Query(max_length=100)] = None,
) -> ControlGenerationRecordSummary:
    """与列表同筛选口径的聚合。

    生成记录列表是分页的，管理端无法靠自己汇总，「筛选后 3 条失败」与「聚合里
    还有 12 条」会互相打脸；因此聚合与列表共用同一批过滤器，并额外回答「拆解
    为什么失败、能不能重试」——这正是 2026-09-20 事故里完全缺失的视角。

    ``task_ref`` 与列表同一口径（方案 P0-12）：给了编号就必须落到同一条记录，
    否则列表能搜到、聚合却还算全量，两边数字又对不上。
    """
    ref_filter = _task_ref_filter(conn, task_ref)
    status = _merge_status(status, status_group)
    video_where, video_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("VIDEO",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    oral_where, oral_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("ORAL_VIDEO",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    first_where, first_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("FIRST_FRAME_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    sheet_where, sheet_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("CHARACTER_SHEET_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    view_where, view_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("CHARACTER_VIEW_IMAGE",),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    source_where, source_params = _generation_record_filters(
        postgres=conn.is_postgres,
        record_types=("SOURCE_FRAME_PROCESS", "SOURCE_FRAME_AI_SCORE"),
        username=username,
        status=status,
        record_type=record_type,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
    )
    analysis_where, analysis_params = _analysis_record_filters(
        postgres=conn.is_postgres,
        username=username,
        status=status,
        record_type=record_type,
        failure_phase=failure_phase,
        created_from=created_from,
        created_to=created_to,
        task_ref=ref_filter,
        project_name=project_name,
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
        SELECT record_type, status, COUNT(*) AS total,
           AVG(duration) AS avg_duration FROM (
            SELECT 'VIDEO' AS record_type, task.status,
                   CASE WHEN task.status = 'SUCCEEDED'
                        THEN EXTRACT(EPOCH FROM (
                          task.completed_at::timestamp AT TIME ZONE 'UTC'
                          - task.created_at_utc))
                        ELSE NULL END AS duration
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            JOIN users ON users.id = batch.created_by_user_id
            {video_where}
            UNION ALL
            SELECT 'ORAL_VIDEO', task.status, NULL AS duration
            FROM oral_tasks AS task
            JOIN users ON users.id = task.owner_user_id
            {oral_where}
            UNION ALL
            SELECT 'FIRST_FRAME_IMAGE', task.status,
                   CASE WHEN task.status = 'SUCCEEDED'
                        THEN EXTRACT(EPOCH FROM (
                          task.completed_at::timestamp AT TIME ZONE 'UTC'
                          - task.created_at::timestamp AT TIME ZONE 'UTC'))
                        ELSE NULL END AS duration
            FROM first_frame_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {first_where}
            UNION ALL
            SELECT 'CHARACTER_SHEET_IMAGE', task.status,
                   CASE WHEN task.status = 'SUCCEEDED'
                        THEN EXTRACT(EPOCH FROM (
                          task.completed_at::timestamp AT TIME ZONE 'UTC'
                          - task.created_at::timestamp AT TIME ZONE 'UTC'))
                        ELSE NULL END AS duration
            FROM character_sheet_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {sheet_where}
            UNION ALL
            SELECT 'CHARACTER_VIEW_IMAGE', task.status,
                   CASE WHEN task.status = 'SUCCEEDED'
                        THEN EXTRACT(EPOCH FROM (
                          task.completed_at::timestamp AT TIME ZONE 'UTC'
                          - task.created_at::timestamp AT TIME ZONE 'UTC'))
                        ELSE NULL END AS duration
            FROM character_generation_tasks AS task
            JOIN users ON users.id = task.created_by
            {view_where}
            UNION ALL
            SELECT CASE WHEN {semantic_requested_sql}
                        THEN 'SOURCE_FRAME_AI_SCORE' ELSE 'SOURCE_FRAME_PROCESS' END,
                   task.status,
                   CASE WHEN task.status = 'SUCCEEDED'
                        THEN EXTRACT(EPOCH FROM (
                          task.completed_at::timestamp AT TIME ZONE 'UTC'
                          - task.created_at::timestamp AT TIME ZONE 'UTC'))
                        ELSE NULL END AS duration
            FROM source_frame_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {source_where}
            UNION ALL
            SELECT 'ANALYSIS', task.status,
                   CASE WHEN task.status = 'SUCCEEDED'
                        THEN EXTRACT(EPOCH FROM (
                          task.completed_at::timestamp AT TIME ZONE 'UTC'
                          - task.created_at::timestamp AT TIME ZONE 'UTC'))
                        ELSE NULL END AS duration
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
    # 4 张聚合卡的派生口径（方案 P1）：
    # - 成功 = SUCCEEDED；失败 = 5 组口径里的「失败」组成员（含两种取消拼写）。
    # - 平均耗时 = 成功任务耗时的加权平均（各类型 AVG(duration) × 样本数），
    #   只有 PG 能算出 duration，SQLite 开发库上保持 None 而不是假装 0 秒。
    succeeded = sum(item.count for item in counts if item.status == "SUCCEEDED")
    failed_statuses = set(_status_values(_STATUS_GROUPS["failed"]))
    failed = sum(item.count for item in counts if item.status in failed_statuses)
    total_count = sum(item.count for item in counts)
    duration_weighted = 0.0
    duration_samples = 0
    for row in rows:
        avg = row["avg_duration"]
        if avg is not None and str(row["status"]) == "SUCCEEDED":
            duration_weighted += float(avg) * int(row["total"])
            duration_samples += int(row["total"])
    return ControlGenerationRecordSummary(
        total=total_count,
        counts=counts,
        failure_reasons=_generation_failure_reasons(
            conn,
            username=username,
            status=status,
            record_type=record_type,
            failure_phase=failure_phase,
            created_from=created_from,
            created_to=created_to,
            task_ref=ref_filter,
            project_name=project_name,
        ),
        succeeded_count=succeeded,
        failed_count=failed,
        success_rate_pct=(None if total_count == 0 else round(succeeded / total_count * 100, 1)),
        avg_duration_seconds=(
            None if duration_samples == 0 else round(duration_weighted / duration_samples, 1)
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


@router.get("/billing-reconciliation/items")
def read_reconciliation_items(
    conn: Database,
    actor: ControlUser,
    anomaly: Literal["wallet_mismatch", "paid_without_charge", "charge_without_paid_order"] = Query(
        ...
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """对账异常明细（方案 P1 资金中心）：三类清单按类型分页，条数与汇总一致。

    总览待办与资金中心的异常数字必须「点进去条数一致」，所以这里的筛选口径
    逐字复用 /billing-reconciliation 汇总里的三个子查询，只加客户信息与分页。
    """
    write_audit(
        conn,
        actor=actor,
        action="control.reconciliation.read",
        entity_type="control_ledger",
        entity_id=f"billing_reconciliation:{anomaly}",
    )
    page = " LIMIT %s OFFSET %s"
    if anomaly == "wallet_mismatch":
        base = """
            FROM wallets w
            JOIN users u ON u.id = w.user_id
            LEFT JOIN (
                SELECT user_id,
                       SUM(available_delta) AS available_total,
                       SUM(reserved_delta) AS reserved_total
                FROM wallet_transactions
                GROUP BY user_id
            ) AS ledger ON ledger.user_id = w.user_id
            WHERE w.available_credits <> COALESCE(ledger.available_total, 0)
               OR w.reserved_credits <> COALESCE(ledger.reserved_total, 0)
        """
        select = (
            "SELECT w.user_id, u.username, "
            "COALESCE(NULLIF(u.display_name, ''), u.username) AS display_name, "
            "w.available_credits, w.reserved_credits, "
            "COALESCE(ledger.available_total, 0) AS ledger_available_credits, "
            "COALESCE(ledger.reserved_total, 0) AS ledger_reserved_credits "
        )
        order = " ORDER BY w.user_id"
    elif anomaly == "paid_without_charge":
        base = """
            FROM recharge_orders o
            LEFT JOIN users u ON u.id = o.user_id
            WHERE o.status = 'PAID' AND NOT EXISTS (
                SELECT 1 FROM wallet_transactions wt
                WHERE wt.recharge_order_id = o.id AND wt.type = 'CHARGE')
        """
        select = (
            "SELECT o.id AS order_id, o.user_id, u.username, "
            "COALESCE(NULLIF(u.display_name, ''), u.username) AS display_name, "
            "o.provider, o.amount_fen, o.credits, o.paid_at, o.created_at "
        )
        order = " ORDER BY o.paid_at DESC, o.id"
    else:
        base = """
            FROM wallet_transactions tx
            JOIN recharge_orders o ON o.id = tx.recharge_order_id
            LEFT JOIN users u ON u.id = tx.user_id
            WHERE tx.type = 'CHARGE' AND o.status <> 'PAID'
        """
        select = (
            "SELECT tx.id AS transaction_id, tx.user_id, u.username, "
            "COALESCE(NULLIF(u.display_name, ''), u.username) AS display_name, "
            "tx.available_delta, tx.recharge_order_id AS order_id, "
            "tx.created_at, o.status AS order_status, o.provider "
        )
        order = " ORDER BY tx.created_at DESC, tx.id"
    rows = conn.execute(
        f"{select}{base}{order}{page}",  # noqa: S608 -- 排序方向为常量，无用户输入。
        (limit, offset),
    ).fetchall()
    total_row = conn.execute(f"SELECT COUNT(*) {base}").fetchone()  # noqa: S608
    assert total_row is not None
    return {
        "anomaly": anomaly,
        "items": [dict(row) for row in rows],
        "total": int(total_row[0]),
        "limit": limit,
        "offset": offset,
    }


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

    审计写在探针**成功返回之后**：探针抛错（含尚未接入真实客户端的服务的 501 存根、
    以及提交结果不确定的 502）时没有可供 attest 的成功事实；而且写契约的
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
    # B3 (2026-09-22 review): bulk export is a data-egress action, not a view —
    # it requires write-level authority (auditor 403 AUDITOR_READ_ONLY), the
    # same policy customers.csv already applied. Auditors keep their read-only
    # views of these rows on the pages; only the one-shot full dump is removed.
    actor: ControlWriter,
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
    # B3 (2026-09-22 review): same write-level gate as recharge-orders.csv —
    # one GET pulls the whole wallet ledger out of the platform in bulk.
    actor: ControlWriter,
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


@dataclass(frozen=True)
class TaskRefFilter:
    """编号检索：命中的我方任务编号集合 + 短编号前缀（小写 LIKE 模式）。"""

    ids: tuple[str, ...]
    prefix: str | None


_SHORT_REF_PATTERN = re.compile(r"[0-9A-Za-z-]{6,64}")


def _task_ref_filter(conn: BusinessConnection, task_ref: str | None) -> TaskRefFilter | None:
    """把任一编号解析成我方任务编号（方案 P0-12）。

    第三方任务号、第三方请求编号与我方请求编号（调用日志的 request_id，与审计、
    服务日志同源）先查调用日志，再查各任务表自带的第三方任务号列（视频、人物
    视图、口播）；首帧任务没有这一列，只能靠调用日志反查。
    """
    ref = (task_ref or "").strip()
    if not ref:
        return None
    ids: set[str] = {ref}
    if conn.is_postgres:
        rows = conn.execute(
            """
            SELECT task_id AS id FROM external_call_logs
            WHERE task_id IS NOT NULL
              AND (provider_task_id = %s OR provider_request_id = %s OR request_id = %s)
            UNION SELECT id FROM generation_tasks WHERE provider_task_id = %s
            UNION SELECT id FROM character_generation_tasks WHERE provider_task_id = %s
            UNION SELECT id FROM oral_tasks WHERE vendor_task_id = %s
            """,
            (ref, ref, ref, ref, ref, ref),
        ).fetchall()
        ids.update(str(row["id"]) for row in rows)
    # 任务编号是小写 UUID；客户端给客户看的短编号是前 8 位大写。
    prefix = f"{ref.lower()}%" if _SHORT_REF_PATTERN.fullmatch(ref) else None
    return TaskRefFilter(ids=tuple(sorted(ids)), prefix=prefix)


_EXPLAINED_STATUSES = frozenset(
    {
        "FAILED",
        "SUBMISSION_UNCERTAIN",
        "UNKNOWN",
        "ARCHIVE_FAILED",
        "CANCELED",
        "CANCELLED",
    }
)


def _attach_failure_explanations(
    conn: BusinessConnection, records: list[ControlGenerationRecord]
) -> None:
    """给当前页的失败记录补中文处理建议/分类/处理人与服务商原话（方案 P0-10 / P0-9 / P1-1）。

    原话优先取任务行自带的（口播），没有时取该任务最近一次失败调用的日志。
    分类取 failure_runbook 的静态映射；原话命中审核关键词时升级为「内容审核」。
    """
    if conn.is_postgres:
        wanted = [
            record
            for record in records
            if record.status in _EXPLAINED_STATUSES and record.provider_message is None
        ]
        if wanted:
            ids = [record.record_id for record in wanted]
            rows = conn.execute(
                f"""
                SELECT DISTINCT ON (task_id) task_id, provider_error_code, provider_message,
                       error_message_redacted
                FROM external_call_logs
                WHERE task_id IN ({", ".join(["%s"] * len(ids))})
                  AND outcome IS NOT NULL AND outcome <> 'SUCCEEDED'
                ORDER BY task_id, created_at DESC
                """,  # noqa: S608
                tuple(ids),
            ).fetchall()
            latest = {str(row["task_id"]): row for row in rows}
            for record in wanted:
                row = latest.get(record.record_id)
                if row is None:
                    continue
                record.provider_error_code = _optional_text(row["provider_error_code"])
                record.provider_message = _optional_text(row["provider_message"]) or _optional_text(
                    row["error_message_redacted"]
                )
    # 建议/分类/处理人统一在这里落地：原话先取到，审核升级才能生效。
    for record in records:
        if not record.error_code:
            continue
        explanation = failure_explanation(
            record.error_code, provider_message=record.provider_message
        )
        if explanation is None:
            continue
        if record.advice is None:
            record.advice = explanation.advice
        record.failure_category = explanation.category
        record.failure_owner = explanation.owner
    _attach_credit_refunds(conn, records)


def _attach_credit_refunds(
    conn: BusinessConnection, records: list[ControlGenerationRecord]
) -> None:
    """标记失败记录的预扣积分是否已退回（存在 RELEASE 流水）。

    三路归集：视频线挂 ``task_id``、口播挂 ``oral_task_id``、图片/分析经
    ``billing_operations.source_id`` 桥接（其余类型的钱包行不挂任务列）。
    """
    wanted = [record for record in records if record.status in _EXPLAINED_STATUSES]
    if not wanted:
        return
    for record in wanted:
        if not conn.is_postgres:
            record.credits_refunded = None
    if not conn.is_postgres:
        return
    ids = [record.record_id for record in wanted]
    placeholders = ", ".join(["%s"] * len(ids))
    rows = conn.execute(
        f"""
        SELECT released.ref FROM (
            SELECT wt.task_id AS ref FROM wallet_transactions wt
            WHERE wt.type = 'RELEASE' AND wt.task_id IN ({placeholders})
            UNION
            SELECT wt.oral_task_id AS ref FROM wallet_transactions wt
            WHERE wt.type = 'RELEASE' AND wt.oral_task_id IN ({placeholders})
            UNION
            SELECT op.source_id AS ref FROM wallet_transactions wt
            JOIN billing_operations op ON op.id = wt.billing_operation_id
            WHERE wt.type = 'RELEASE' AND op.source_id IN ({placeholders})
        ) AS released
        """,  # noqa: S608
        tuple(ids) * 3,
    ).fetchall()
    refunded = {str(row["ref"]) for row in rows}
    for record in wanted:
        record.credits_refunded = record.record_id in refunded


# 5 组运营口径（方案 P1 生成记录改造）：与前端 GENERATION_STATUS_FILTERS 同集。
# 后端提供 status_group 入参，避免每个调用方都背一份逗号拼写表。
_STATUS_GROUPS: dict[str, str] = {
    "queued": "CREATED,QUEUED,PENDING",
    "running": "SUBMITTING,SUBMITTED,RUNNING,RETRYING,ARCHIVING",
    "succeeded": "SUCCEEDED",
    "failed": "FAILED,CANCELED,CANCELLED,ARCHIVE_FAILED",
    "attention": "UNKNOWN,SUBMISSION_UNCERTAIN",
}


def _merge_status(status: str | None, status_group: str | None) -> str | None:
    """status_group 展开成底层状态后与显式 status 合并（去重、保序）。"""
    merged: list[str] = []
    for source in (status, _STATUS_GROUPS.get(status_group or "", None)):
        if not source:
            continue
        for value in _status_values(source):
            if value not in merged:
                merged.append(value)
    return ",".join(merged) if merged else None


def _status_values(status: str | None) -> list[str]:
    """状态筛选接受逗号分隔的多个值。

    同一业务状态在不同任务表里有两种拼写（CANCELED / CANCELLED）：管理端
    下拉只显示一个「已取消」，一次请求要把两种拼写都查到（方案 P0-6）。
    """
    if not status:
        return []
    return [value for value in (part.strip() for part in status.split(",")) if value]


def _generation_record_filters(
    *,
    postgres: bool = False,
    record_types: tuple[GenerationRecordType, ...],
    username: str | None,
    status: str | None,
    record_type: GenerationRecordType | None,
    created_from: str | None,
    created_to: str | None,
    task_ref: TaskRefFilter | None = None,
    project_name: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    if record_type is not None and record_type not in record_types:
        clauses.append("1 = 0")
    if task_ref is not None:
        ref_clauses = [f"task.id IN ({', '.join(['%s'] * len(task_ref.ids))})"]
        params.extend(task_ref.ids)
        if task_ref.prefix:
            ref_clauses.append("task.id LIKE %s")
            params.append(task_ref.prefix)
        clauses.append(f"({' OR '.join(ref_clauses)})")
    if username:
        clauses.append("users.username LIKE %s")
        params.append(f"%{username}%")
    if project_name and project_name.strip():
        # 项目名筛选（方案 P1）：只有视频任务挂在项目下（batch → projects）；
        # 其余类型（口播/图片/拆解）没有项目维度，筛项目时如实不出现在结果里。
        if "VIDEO" in record_types:
            clauses.append(
                "EXISTS (SELECT 1 FROM generation_batches gb "
                "JOIN projects p ON p.id = gb.project_id "
                "WHERE gb.id = task.batch_id AND p.name LIKE %s)"
            )
            params.append(f"%{project_name.strip()}%")
        else:
            clauses.append("1 = 0")
    statuses = _status_values(status)
    if statuses:
        clauses.append(f"task.status IN ({', '.join(['%s'] * len(statuses))})")
        params.extend(statuses)
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
    task_ref: TaskRefFilter | None = None,
    project_name: str | None = None,
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
        task_ref=task_ref,
        # 拆解任务自带 project_id，项目名条件在下方用真表达式拼，
        # 不走通用分支的 1 = 0。
        project_name=None,
    )
    if project_name and project_name.strip():
        conjunction = "AND" if where else "WHERE"
        where = (
            f"{where} {conjunction} EXISTS ("
            "SELECT 1 FROM projects p WHERE p.id = task.project_id "
            "AND p.name LIKE %s)"
        )
        params = (*params, f"%{project_name.strip()}%")
    if failure_phase:
        conjunction = "AND" if where else "WHERE"
        where = f"{where} {conjunction} task.failure_phase = %s"
        params = (*params, failure_phase)
    return where, params


def _generation_failure_reasons(
    conn: BusinessConnection,
    *,
    username: str | None,
    status: str | None,
    record_type: GenerationRecordType | None,
    failure_phase: str | None,
    created_from: str | None,
    created_to: str | None,
    task_ref: TaskRefFilter | None = None,
    project_name: str | None = None,
) -> list[AnalysisFailureReason]:
    """按「为什么失败」聚合失败行：类型 × 错误码 × 失败阶段（方案 P1-3）。

    覆盖全部任务类型（视频/口播/首帧/人物表/人物视图/取帧/拆解），不再是
    只看拆解——「近一小时整体故障」与「个别客户问题」在同一个视图里一眼能分。

    原因（``reason``）是「错误码 + 服务商原话」里的原话：拆解取 P0-2 落库的
    上游诊断（``->>`` 只有 PG 的 jsonb 支持；SQLite 开发库没有该列，自然也
    没有原因），其余类型取该组样本任务在调用日志里的最新失败原话。

    当前视图里根本没有失败行（状态过滤不是 FAILED）时返回空——不给一份与
    列表无关的失败清单。
    """
    if status and "FAILED" not in _status_values(status):
        return []

    def group_where(types: tuple[GenerationRecordType, ...]) -> tuple[str, tuple[str, ...]]:
        return _generation_record_filters(
            postgres=conn.is_postgres,
            record_types=types,
            username=username,
            status="FAILED",
            record_type=record_type,
            created_from=created_from,
            created_to=created_to,
            task_ref=task_ref,
            project_name=project_name,
        )

    video_where, video_params = group_where(("VIDEO",))
    oral_where, oral_params = group_where(("ORAL_VIDEO",))
    first_where, first_params = group_where(("FIRST_FRAME_IMAGE",))
    sheet_where, sheet_params = group_where(("CHARACTER_SHEET_IMAGE",))
    view_where, view_params = group_where(("CHARACTER_VIEW_IMAGE",))
    source_where, source_params = group_where(("SOURCE_FRAME_PROCESS", "SOURCE_FRAME_AI_SCORE"))
    analysis_where, analysis_params = _analysis_record_filters(
        postgres=conn.is_postgres,
        username=username,
        status="FAILED",
        record_type=record_type,
        failure_phase=failure_phase,
        created_from=created_from,
        created_to=created_to,
        task_ref=task_ref,
    )
    # 拆解行有专属的失败阶段/可否重试/上游原因列，其余表没有：用 CAST 置空，
    # PG 与 SQLite 都认这一写法（``::`` 只有 PG 支持）。
    analysis_reason = (
        "CAST(task.upstream_diagnostic_json ->> 'reason' AS text)" if conn.is_postgres else "NULL"
    )
    semantic_requested_sql = """
        EXISTS (
            SELECT 1 FROM audit_logs quality_audit
            WHERE quality_audit.action = 'source_frame.semantic_quality_started'
              AND quality_audit.entity_id = task.id
        )
    """
    rows = conn.execute(
        f"""
        SELECT record_type, error_code, failure_phase, reason, retryable,
               COUNT(*) AS total, MIN(sample_id) AS sample_id
        FROM (
            SELECT 'VIDEO' AS record_type, task.error_code AS error_code,
                   CAST(NULL AS text) AS failure_phase, CAST(NULL AS text) AS reason,
                   CAST(NULL AS boolean) AS retryable, task.id AS sample_id
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            JOIN users ON users.id = batch.created_by_user_id
            {video_where}
            UNION ALL
            -- 口播表没有 error_code 列（只有原始 error_message）：管理端展示码
            -- 按状态映射，与记录组装处（oral_error_codes）同一口径；
            -- 直接取列会 UndefinedColumn（2026-09-29 回归修复）。
            SELECT 'ORAL_VIDEO',
                   CASE task.status
                       WHEN 'SUBMISSION_UNCERTAIN' THEN 'ORAL_SUBMISSION_UNCERTAIN'
                       WHEN 'ARCHIVE_FAILED' THEN 'ORAL_ARCHIVE_FAILED'
                       ELSE 'ORAL_TASK_FAILED'
                   END,
                   CAST(NULL AS text), CAST(NULL AS text),
                   CAST(NULL AS boolean), task.id
            FROM oral_tasks AS task
            JOIN users ON users.id = task.owner_user_id
            {oral_where}
            UNION ALL
            SELECT 'FIRST_FRAME_IMAGE', task.error_code,
                   CAST(NULL AS text), CAST(NULL AS text),
                   CAST(NULL AS boolean), task.id
            FROM first_frame_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {first_where}
            UNION ALL
            SELECT 'CHARACTER_SHEET_IMAGE', task.error_code,
                   CAST(NULL AS text), CAST(NULL AS text),
                   CAST(NULL AS boolean), task.id
            FROM character_sheet_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {sheet_where}
            UNION ALL
            SELECT 'CHARACTER_VIEW_IMAGE', task.error_code,
                   CAST(NULL AS text), CAST(NULL AS text),
                   CAST(NULL AS boolean), task.id
            FROM character_generation_tasks AS task
            JOIN users ON users.id = task.created_by
            {view_where}
            UNION ALL
            SELECT CASE WHEN {semantic_requested_sql}
                        THEN 'SOURCE_FRAME_AI_SCORE' ELSE 'SOURCE_FRAME_PROCESS' END,
                   task.error_code,
                   CAST(NULL AS text), CAST(NULL AS text),
                   CAST(NULL AS boolean), task.id
            FROM source_frame_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {source_where}
            UNION ALL
            -- retryable 在拆解表是 integer（0/1），其余分支是 boolean：UNION 要求
            -- 类型一致，用布尔表达式归一（PG 出 boolean，SQLite 出 0/1，均可）。
            SELECT 'ANALYSIS', task.error_code, task.failure_phase,
                   {analysis_reason}, (task.retryable = 1), task.id
            FROM analysis_tasks AS task
            JOIN users ON users.id = task.created_by_user_id
            {analysis_where}
        ) AS failure_rows
        GROUP BY record_type, error_code, failure_phase, reason, retryable
        ORDER BY total DESC, record_type ASC, error_code ASC
        LIMIT %s
        """,  # noqa: S608
        (
            *video_params,
            *oral_params,
            *first_params,
            *sheet_params,
            *view_params,
            *source_params,
            *analysis_params,
            FAILURE_REASON_GROUP_LIMIT,
        ),
    ).fetchall()
    # 没有任务行原因的组，用样本任务在调用日志里的最近一次失败原话补上，
    # 让「同因聚合」保留服务商原话这个关键判据。
    pending: list[tuple[Any, str | None, str | None, str | None]] = []
    sample_ids: list[str] = []
    for row in rows:
        error_code = _optional_text(row["error_code"])
        reason = _optional_text(row["reason"])
        sample_id = _optional_text(row["sample_id"])
        pending.append((row, error_code, reason, sample_id))
        if reason is None and sample_id:
            sample_ids.append(sample_id)
    sample_messages: dict[str, str] = {}
    if conn.is_postgres and sample_ids:
        call_rows = conn.execute(
            f"""
            SELECT DISTINCT ON (task_id) task_id, provider_message, error_message_redacted
            FROM external_call_logs
            WHERE task_id IN ({", ".join(["%s"] * len(sample_ids))})
              AND outcome IS NOT NULL AND outcome <> 'SUCCEEDED'
            ORDER BY task_id, created_at DESC
            """,  # noqa: S608
            tuple(sample_ids),
        ).fetchall()
        for call_row in call_rows:
            message = _optional_text(call_row["provider_message"]) or _optional_text(
                call_row["error_message_redacted"]
            )
            if message:
                sample_messages[str(call_row["task_id"])] = message
    items: list[AnalysisFailureReason] = []
    for row, error_code, reason, sample_id in pending:
        if reason is None and sample_id:
            reason = sample_messages.get(sample_id)
        explanation = failure_explanation(error_code, provider_message=reason)
        items.append(
            AnalysisFailureReason(
                record_type=cast(GenerationRecordType, str(row["record_type"])),
                error_code=error_code,
                failure_phase=_optional_text(row["failure_phase"]),
                reason=reason,
                retryable=None if row["retryable"] is None else bool(row["retryable"]),
                count=int(row["total"]),
                advice=explanation.advice if explanation is not None else None,
                failure_category=explanation.category if explanation is not None else None,
                failure_owner=explanation.owner if explanation is not None else None,
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


def _has_candidate_asset(payload: dict[str, object]) -> bool:
    """结果版本候选池里是否有可预览的产物资产（P2-2 缩略图的数据前提）。"""
    candidates = payload.get("candidates")
    return isinstance(candidates, list) and any(
        isinstance(candidate, dict) and candidate.get("asset_id") for candidate in candidates
    )


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
    has_preview: bool = False,
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
        has_preview=has_preview,
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
