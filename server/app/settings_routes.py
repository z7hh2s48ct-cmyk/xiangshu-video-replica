from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictInt

from app.auth import AuthenticatedUser, CurrentUser, Database
from app.auth import get_database as auth_get_database
from app.db_portable import BusinessConnection
from app.permissions import require_role
from app.settings import (
    ProviderTester,
    ProviderTestResult,
    SettingsRepository,
    get_provider_tester,
    is_secret_field,
    merge_provider_config,
    remove_cos_lifecycle_rules,
    require_supported_provider,
)

router = APIRouter(prefix="/api/admin/settings", tags=["settings"])
logger = logging.getLogger(__name__)
get_database = auth_get_database


class ProviderSettingsRequest(BaseModel):
    config: dict[str, str] = Field(default_factory=dict)


class RuntimeSettingsRequest(BaseModel):
    max_generation_count_per_batch: int
    max_concurrent_h3_tasks: int
    active_storage_provider: Literal["cos", "local"] | None = None
    # M4/M5 review M2: the fair-queue rollout switch (revised ADR §4) gets an
    # audited write path — None leaves it unchanged; PostgreSQL-only (the
    # desktop SQLite lane keeps its legacy global FIFO and answers 422).
    fair_queue_enabled: bool | None = None


class BillingSettingsRequest(BaseModel):
    internal_base_unit_price_fen: StrictInt
    oral_unit_price_fen: StrictInt
    min_recharge_fen: StrictInt
    recharge_step_fen: StrictInt


class DiagnosticProviderResult(BaseModel):
    provider: str
    status: Literal["ok", "not_configured", "configured_only", "error"]
    configured_fields: list[str]
    adapter_capability: Literal["configuration_only", "connection_test"]
    test_kind: str
    http_status: int | None = None
    error_code: str | None = None
    failure_phase: str | None = None
    cleanup_failed: bool | None = None
    latency_ms: int | None = None
    message: str


class SettingsDiagnosticReport(BaseModel):
    id: str
    status: Literal["ok", "attention"]
    generated_at: str
    providers: list[DiagnosticProviderResult]
    download_url: str


def require_settings_admin(conn: Database, actor: AuthenticatedUser) -> CurrentUser:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action="settings.manage",
        entity_type="settings",
        entity_id="admin_settings",
    )
    return actor


SettingsAdmin = Annotated[CurrentUser, Depends(require_settings_admin)]


@router.get("")
def read_settings(
    conn: Database,
    _: SettingsAdmin,
) -> dict[str, object]:
    repo = SettingsRepository(conn)
    return {
        "providers": repo.read_all_provider_configs(),
        "runtime": repo.read_runtime_settings(),
        "billing": repo.read_billing_settings(),
    }


@router.put("/providers/{provider}")
def update_provider_settings(
    provider: str,
    payload: ProviderSettingsRequest,
    conn: Database,
    admin: SettingsAdmin,
) -> dict[str, object]:
    provider_name = require_supported_provider(provider)
    repo = SettingsRepository(conn)
    try:
        saved_config = repo.load_provider_config(provider_name)
        incoming_config = merge_provider_config(saved_config, payload.config)
        result = repo.save_provider_config(
            provider_name,
            incoming_config,
            actor_user_id=admin.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SETTINGS", "message": str(exc)},
        ) from exc

    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="provider_settings.update",
        entity_type="provider_settings",
        entity_id=provider_name,
        metadata_json=f'{{"provider":"{provider_name}"}}',
    )

    lifecycle = None
    if provider_name == "cos":
        lifecycle = remove_cos_lifecycle_rules(incoming_config, actor_id=admin.id)
        write_audit_log(
            conn,
            actor_user_id=admin.id,
            action=f"cos_lifecycle.{lifecycle['status']}",
            entity_type="provider_settings",
            entity_id="cos",
            metadata_json=json.dumps({"status": lifecycle["status"]}, sort_keys=True),
        )
    if lifecycle is not None and isinstance(result, dict):
        result["lifecycle"] = lifecycle
    return result


@router.post("/providers/{provider}/secrets/{field}/reveal")
def reveal_provider_secret(
    provider: str,
    field: str,
    conn: Database,
    admin: SettingsAdmin,
) -> JSONResponse:
    provider_name = require_supported_provider(provider)
    if not is_secret_field(field):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SETTINGS_FIELD_NOT_SECRET",
                "message": "该字段不是密钥。",
            },
        )

    value = SettingsRepository(conn).load_provider_config(provider_name).get(field)
    if not value:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SETTINGS_SECRET_NOT_CONFIGURED",
                "message": "该密钥尚未配置。",
            },
        )

    # 只记录“谁查看了哪个字段”；明文密钥不进日志、审计或普通设置快照。
    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="provider_settings.secret_reveal",
        entity_type="provider_settings",
        entity_id=provider_name,
        metadata_json=json.dumps(
            {"provider": provider_name, "field": field},
            sort_keys=True,
        ),
    )
    return JSONResponse(
        content={"value": value},
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/runtime")
def update_runtime_settings(
    payload: RuntimeSettingsRequest,
    conn: Database,
    admin: SettingsAdmin,
) -> dict[str, int | str]:
    repo = SettingsRepository(conn)
    try:
        current = repo.read_runtime_settings()
        result = repo.save_runtime_settings(
            max_generation_count_per_batch=payload.max_generation_count_per_batch,
            max_concurrent_h3_tasks=payload.max_concurrent_h3_tasks,
            active_storage_provider=str(
                payload.active_storage_provider or current["active_storage_provider"]
            ),
            actor_user_id=admin.id,
            fair_queue_enabled=payload.fair_queue_enabled,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SETTINGS", "message": str(exc)},
        ) from exc

    # The queue mode is a production rollout decision (revised ADR §4) —
    # record it in the audit metadata, not just the generic limits label.
    audit_metadata: dict[str, object] = {"setting": "runtime_limits"}
    if payload.fair_queue_enabled is not None:
        audit_metadata["fair_queue_enabled"] = payload.fair_queue_enabled
    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="runtime_settings.update",
        entity_type="runtime_settings",
        entity_id="1",
        metadata_json=json.dumps(audit_metadata, sort_keys=True),
    )
    return result


@router.patch("/billing")
def update_billing_settings(
    payload: BillingSettingsRequest,
    conn: Database,
    admin: SettingsAdmin,
) -> dict[str, int]:
    try:
        result = SettingsRepository(conn).save_billing_settings(
            internal_base_unit_price_fen=payload.internal_base_unit_price_fen,
            oral_unit_price_fen=payload.oral_unit_price_fen,
            min_recharge_fen=payload.min_recharge_fen,
            recharge_step_fen=payload.recharge_step_fen,
            actor_user_id=admin.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SETTINGS", "message": str(exc)},
        ) from exc

    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="billing_settings.update",
        entity_type="runtime_settings",
        entity_id="1",
        metadata_json='{"setting":"internal_billing"}',
    )
    return result


@router.post(
    "/providers/{provider}/connection-test",
    response_model=ProviderTestResult,
    response_model_exclude_none=True,
)
def connection_test(
    provider: str,
    conn: Database,
    _: SettingsAdmin,
    tester: ProviderTester = Depends(get_provider_tester),
) -> ProviderTestResult:
    provider_name = require_supported_provider(provider)
    config = SettingsRepository(conn).load_provider_config(provider_name)
    return tester.connection_test(provider_name, config)


@router.post(
    "/providers/{provider}/paid-test",
    response_model=ProviderTestResult,
    response_model_exclude_none=True,
)
def paid_test(
    provider: str,
    conn: Database,
    _: SettingsAdmin,
    tester: ProviderTester = Depends(get_provider_tester),
) -> ProviderTestResult:
    provider_name = require_supported_provider(provider)
    config = SettingsRepository(conn).load_provider_config(provider_name)
    return tester.paid_test(provider_name, config)


@router.post("/diagnostic-test", response_model=SettingsDiagnosticReport)
def run_settings_diagnostic(
    conn: Database,
    admin: SettingsAdmin,
    tester: ProviderTester = Depends(get_provider_tester),
) -> SettingsDiagnosticReport:
    repo = SettingsRepository(conn)
    results: list[DiagnosticProviderResult] = []

    for provider in ("metaso", "apilio", "cos", "deepseek"):
        config = repo.load_provider_config(provider)
        configured_fields = sorted(config)
        if not config:
            results.append(
                DiagnosticProviderResult(
                    provider=provider,
                    status="not_configured",
                    configured_fields=[],
                    adapter_capability="configuration_only",
                    test_kind="configuration",
                    message="尚未保存该服务的必要参数。",
                )
            )
            continue

        try:
            started_at = time.monotonic()
            provider_result = tester.connection_test(provider, config)
        except HTTPException as exc:
            logger.warning(
                "Provider diagnostic failed for %s with HTTP status %s",
                provider,
                exc.status_code,
            )
            results.append(
                DiagnosticProviderResult(
                    provider=provider,
                    status="error",
                    configured_fields=configured_fields,
                    adapter_capability="connection_test",
                    test_kind="connection",
                    http_status=exc.status_code,
                    error_code=error_code_from_http_exception(exc),
                    failure_phase=failure_phase_from_http_exception(exc),
                    cleanup_failed=cleanup_failed_from_http_exception(exc),
                    latency_ms=elapsed_milliseconds(started_at),
                    message="测试接口返回错误；请下载诊断日志查看错误码。",
                )
            )
            continue
        except Exception as exc:
            logger.warning(
                "Provider diagnostic raised %s for %s",
                type(exc).__name__,
                provider,
            )
            results.append(
                DiagnosticProviderResult(
                    provider=provider,
                    status="error",
                    configured_fields=configured_fields,
                    adapter_capability="connection_test",
                    test_kind="connection",
                    error_code="DIAGNOSTIC_INTERNAL_ERROR",
                    latency_ms=elapsed_milliseconds(started_at),
                    message="测试过程发生内部错误；请下载诊断日志并检查本地服务日志。",
                )
            )
            continue

        if provider_result.status == "ok":
            status: Literal["ok", "not_configured", "configured_only", "error"] = "ok"
        elif provider_result.status == "configured_only":
            status = "configured_only"
        elif provider_result.status == "not_configured":
            status = "not_configured"
        else:
            status = "error"
        message = (
            configured_only_message(provider)
            if status == "configured_only"
            else {
                "ok": "连接测试通过。",
                "not_configured": "缺少必要参数。",
                "error": "连接测试失败；请下载诊断日志查看上下文。",
            }[status]
        )
        results.append(
            DiagnosticProviderResult(
                provider=provider,
                status=status,
                configured_fields=configured_fields,
                adapter_capability=(
                    "configuration_only" if status == "configured_only" else "connection_test"
                ),
                test_kind=provider_result.test_kind,
                latency_ms=elapsed_milliseconds(started_at),
                message=message,
            )
        )

    report_id = str(uuid.uuid4())
    report_status: Literal["ok", "attention"] = (
        "ok"
        if all(result.status in {"ok", "configured_only"} for result in results)
        else "attention"
    )
    download_url = f"/api/admin/settings/diagnostic-reports/{report_id}/download"
    report = SettingsDiagnosticReport(
        id=report_id,
        status=report_status,
        generated_at=datetime.now(UTC).isoformat(),
        providers=results,
        download_url=download_url,
    )
    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="settings.diagnostic_test",
        entity_type="settings_diagnostic",
        entity_id=report_id,
        metadata_json=json.dumps(report.model_dump(), ensure_ascii=False, sort_keys=True),
    )
    return report


@router.get("/diagnostic-reports/{report_id}/download")
def download_settings_diagnostic(
    report_id: str,
    conn: Database,
    _: SettingsAdmin,
) -> Response:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM audit_logs
        WHERE entity_id = %s AND action = 'settings.diagnostic_test'
        """,
        (report_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "DIAGNOSTIC_REPORT_NOT_FOUND"})

    return Response(
        content=str(row["metadata_json"]),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="settings-diagnostic-{report_id}.json"'
        },
    )


def error_code_from_http_exception(error: HTTPException) -> str:
    if isinstance(error.detail, dict) and isinstance(error.detail.get("code"), str):
        return str(error.detail["code"])
    return "PROVIDER_CONNECTION_ERROR"


def failure_phase_from_http_exception(error: HTTPException) -> str | None:
    if isinstance(error.detail, dict) and isinstance(error.detail.get("failure_phase"), str):
        return str(error.detail["failure_phase"])
    return None


def cleanup_failed_from_http_exception(error: HTTPException) -> bool | None:
    if isinstance(error.detail, dict) and isinstance(error.detail.get("cleanup_failed"), bool):
        return bool(error.detail["cleanup_failed"])
    return None


def elapsed_milliseconds(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def configured_only_message(provider: str) -> str:
    if provider == "metaso":
        return "".join(
            (
                "H3 参数已保存。测试设置不会提交会产生费用的生成任务；",
                "实际任务由生成 Worker 处理。",
            )
        )
    if provider == "apilio":
        return "".join(
            (
                "模型服务参数已保存。视频拆解和首帧任务会按需调用；",
                "测试设置不会发起计费模型请求。",
            )
        )
    if provider == "deepseek":
        return "".join(
            (
                "DeepSeek 文本 AI 参数已保存。提交文案二创等文本任务时会实际调用；",
                "测试设置不会发起计费请求。",
            )
        )
    return "参数已保存；本次测试未发起外部调用。"


def write_audit_log(
    conn: BusinessConnection,
    *,
    actor_user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata_json: str,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO audit_logs (
                id,
                actor_user_id,
                action,
                entity_type,
                entity_id,
                metadata_json
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                actor_user_id,
                action,
                entity_type,
                entity_id,
                metadata_json,
            ),
        )
