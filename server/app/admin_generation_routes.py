"""管理端生成记录一键处理（方案 P1-4）与成片预览（方案 P2-2）。

原地重试直接复用视频线的 ``retry_generation_task``：能否重试由业务按任务
状态与错误码裁决（``generation.py`` 的既有规则），管理端不另写一套判断。
补偿积分复用既有 ``POST /api/control/customers/{user_id}/adjustments``
（CREDIT_COMPENSATION）；「复制给客户的说明」是纯前端动作，不落在这里。

写契约（dev doc §15）与其它管理端写一致：``Idempotency-Key`` header 同时
充当业务层幂等键——``retry_generation_task`` 按 (actor, task, action, key)
落 ``generation_task_operations``，同键重放返回当前任务态而不是重复改状态。
因此这里不套 ``admin_write_idempotency`` 快照层：双层幂等只会让重放的语义
更含糊（业务层的才是资金安全的真源）。

成片预览沿用客户素材库的那套派生缩略图（``material_thumbs``）：键由原对象
确定性派生、缺失时现场抽帧补齐、失败降级 404。记录 → 产物的映射按类型各
自解析（视频取任务行资产、图片取结果版本候选池、人物表取 result_json），
不引入新表也不回填历史数据。
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ConfigDict

from app.admin_auth_routes import AdminActor, AdminReader, AdminWriter
from app.admin_write_contract import AdminWriteContract, require_write_contract
from app.auth import CurrentUser
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.generation import GenerationTaskRetryRequest, retry_generation_task
from app.material_thumbs import ensure_thumbnail_object, thumbnail_key_for
from app.media import storage_key_from_uri
from app.media_routes import read_stored_object, storage_for_asset
from app.permissions import write_audit
from app.storage import StorageAdapter

router = APIRouter(prefix="/api/control", tags=["admin-generation"])

# 业务裁决的英文 message → 运营中文文案；错误码原样保留（前端契约不变）。
# 「一键处理」是运营动作，拒绝原因必须让操作者直接读懂，而不是替翻译一层
# 英文。这里的码与 ``retry_generation_task`` 直接抛出的集合一一对应。
_RETRY_ERROR_MESSAGES: dict[str, str] = {
    "TASK_NOT_FOUND": "任务不存在，或该记录不是视频生成任务（原地重试仅支持视频任务）。",
    "TASK_SUPERSEDED": "该任务已被新任务替代，不能再重试。",
    "ARCHIVE_RESULT_UNAVAILABLE": "已付款的成片无法再从服务商恢复，存档重试不可用。",
    "TASK_RETRY_NOT_ALLOWED": "任务当前状态不允许原地重试，请刷新后确认最新状态。",
    "REQUIRES_PAID_REGENERATION": "该任务可能已触达服务商或属于质量问题，不能原地免费重试。",
    "MUST_RECONCILE_SUBMISSION": "任务提交结果未知，请先核对服务商是否已受理。",
    "ADMIN_BILLING_CONFIRMATION_REQUIRED": "该任务需先确认服务商未扣费，才能重新入队。",
    "IDEMPOTENCY_CONFLICT": "该幂等键已用于不同的重试请求，请刷新页面后重试。",
    "INSUFFICIENT_CREDITS": "客户可用积分不足，无法重新预扣该次生成。",
    "BILLING_INVARIANT_VIOLATION": "任务计费状态异常，重试已被阻止，请交技术排查。",
}


class AdminGenerationRetryRequest(AdminWriteContract):
    """一键重试请求体：``reason`` 直接作为业务层的 ``retry_reason`` 留痕。"""

    model_config = ConfigDict(extra="forbid")


def _admin_to_current_user(actor: AdminActor) -> CurrentUser:
    # 与 admin_first_frame_routes 同一约定：审计与幂等记录里署真实的管理员
    # 账号；预扣积分的对象由业务层按批次归属取任务所有者，不在这里指定。
    return CurrentUser(
        id=actor.user_id,
        username=actor.username,
        display_name=actor.display_name,
        role="admin",
    )


def _localized_retry_error(exc: HTTPException) -> HTTPException:
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code")
        if isinstance(code, str) and code in _RETRY_ERROR_MESSAGES:
            return HTTPException(
                status_code=exc.status_code,
                detail={"code": code, "message": _RETRY_ERROR_MESSAGES[code]},
            )
    return exc


@router.post("/generation-records/{record_id}/retry")
def retry_generation_record(
    record_id: str,
    request: Request,
    actor: AdminWriter,
    body: AdminGenerationRetryRequest,
) -> dict[str, object]:
    """按业务规则原地重试一条视频生成记录（仅 ``generation_tasks``）。

    成功即已重新入队：``PRE_PROVIDER`` 路径回到 PENDING 并重新预扣任务
    所有者的积分；``ARCHIVE_ONLY`` 路径重新排队恢复已付款成片的存档。
    """
    idempotency_key, reason = require_write_contract(request, body)
    try:
        with pg_transaction() as raw:
            result = retry_generation_task(
                BusinessConnection.postgres(raw),
                task_id=record_id,
                actor=_admin_to_current_user(actor),
                request=GenerationTaskRetryRequest(
                    idempotency_key=idempotency_key,
                    retry_reason=reason,
                ),
            )
    except HTTPException as exc:
        raise _localized_retry_error(exc) from exc
    return {
        "task_id": result.id,
        "status": result.status,
        "archive_status": result.archive_status,
    }


# ---------------------------------------------------------------------------
# 成片预览（方案 P2-2）
# ---------------------------------------------------------------------------

# 可预览的记录类型。拆解（结果只有 JSON）与人物视图（产物未归档）不在列，
# 路径参数因此直接 422，而不是让调用方去猜哪类记录「碰巧没有媒体」。
MediaRecordType = Literal[
    "VIDEO",
    "ORAL_VIDEO",
    "FIRST_FRAME_IMAGE",
    "CHARACTER_SHEET_IMAGE",
    "SOURCE_FRAME_AI_SCORE",
    "SOURCE_FRAME_PROCESS",
]


def _require_media_viewer(actor: AdminActor) -> None:
    """客户生成内容只对 admin 开放；审计员的记录页保持去内容化。"""
    if actor.role != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "GENERATION_RECORD_MEDIA_FORBIDDEN",
                "message": "审计员只能查看记录概要，不能查看客户生成内容。",
            },
        )


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _json_object(raw: object) -> dict[str, object]:
    """损坏/缺失的结果 JSON 一律按「没有产物」降级，不把 500 抛给列表页。"""
    if raw is None:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _latest_candidate_asset_id(raw: object) -> str | None:
    """取结果版本 payload 候选池里最后一张图资产。

    首帧的版本 payload 是「历史候选池 + 本次生成」的拼接（单张重生成要
    携带旧池供确认），末尾必然是本任务刚生成的图；取帧的候选池整体来自
    本任务。两者都取最新一张作为记录缩略图。
    """
    candidates = _json_object(raw).get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in reversed(candidates):
        if isinstance(candidate, dict):
            asset_id = _text(candidate.get("asset_id"))
            if asset_id is not None:
                return asset_id
    return None


def _record_asset_id(
    conn: BusinessConnection,
    *,
    record_type: MediaRecordType,
    record_id: str,
) -> tuple[bool, str | None]:
    """定位一条生成记录的产物资产；返回 (记录是否存在于对应表, 资产 id)。

    各类型的产物落点不同：视频成片是任务行的 ``result_asset_id``；首帧/取帧
    的产物在结果版本 payload 的候选数组里；人物表的成片在任务 ``result_json``
    的 ``contact_sheet_asset_id``。
    """
    if record_type in {"VIDEO", "ORAL_VIDEO"}:
        table = "generation_tasks" if record_type == "VIDEO" else "oral_tasks"
        row = conn.execute(
            f"SELECT result_asset_id FROM {table} WHERE id = %s",  # noqa: S608
            (record_id,),
        ).fetchone()
        if row is None:
            return (False, None)
        return (True, _text(row["result_asset_id"]))
    if record_type == "CHARACTER_SHEET_IMAGE":
        row = conn.execute(
            "SELECT result_json FROM character_sheet_tasks WHERE id = %s",
            (record_id,),
        ).fetchone()
        if row is None:
            return (False, None)
        return (True, _text(_json_object(row["result_json"]).get("contact_sheet_asset_id")))
    table = "first_frame_tasks" if record_type == "FIRST_FRAME_IMAGE" else "source_frame_tasks"
    row = conn.execute(
        "SELECT versions.payload_json "
        f"FROM {table} AS task "  # noqa: S608
        "LEFT JOIN versions ON versions.id = task.result_version_id "
        "WHERE task.id = %s",
        (record_id,),
    ).fetchone()
    if row is None:
        return (False, None)
    return (True, _latest_candidate_asset_id(row["payload_json"]))


def _resolve_record_object(
    conn: BusinessConnection,
    *,
    record_type: MediaRecordType,
    record_id: str,
) -> tuple[StorageAdapter, str]:
    """解析记录产物资产的存储适配器与对象键；不可预览时抛 404。

    「记录不存在」与「记录存在但产物不可用」用不同的错误码区分：前者是
    调用方拿错了编号，后者是历史链路常态（未归档、资产已清理），前端对
    两者的展示也应当不同。
    """
    found, asset_id = _record_asset_id(conn, record_type=record_type, record_id=record_id)
    if not found:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "GENERATION_RECORD_NOT_FOUND",
                "message": "没有找到这条生成记录。",
            },
        )
    if asset_id is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MEDIA_RESULT_UNAVAILABLE",
                "message": "这条记录没有可预览的成片产物（历史记录或产物尚未归档）。",
            },
        )
    asset = conn.execute("SELECT storage_uri FROM assets WHERE id = %s", (asset_id,)).fetchone()
    if asset is None or not asset["storage_uri"]:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MEDIA_RESULT_UNAVAILABLE",
                "message": "这条记录的成片资产已被清理，无法预览。",
            },
        )
    storage_uri = str(asset["storage_uri"])
    return storage_for_asset(conn, storage_uri), storage_key_from_uri(storage_uri)


@router.get("/generation-records/{record_type}/{record_id}/thumbnail")
def get_generation_record_thumbnail(
    record_type: MediaRecordType,
    record_id: str,
    actor: AdminReader,
) -> Response:
    """生成记录行的产物缩略图（方案 P2-2）。

    与客户素材库同一套派生缩略图：键由原对象确定性派生，缺失时现场从原
    对象抽帧补齐（幂等），补齐失败按 404 由前端降级占位。缩略图是 480px
    低敏派生图、列表内逐行内嵌，不写审计——逐张审计会把一次翻页变成上
    百条噪声；真正的高敏读取（看原片）在 content 端点写。
    """
    _require_media_viewer(actor)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        storage, object_key = _resolve_record_object(
            conn, record_type=record_type, record_id=record_id
        )
    thumbnail_key = thumbnail_key_for(object_key)
    if not ensure_thumbnail_object(storage, thumbnail_key):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MEDIA_PREVIEW_UNAVAILABLE",
                "message": "这条记录的缩略图暂不可用。",
            },
        )
    # 缩略图键由内容确定性派生，重建必换键，可以缓存；管理端地址未签名，
    # 窗口取 1 小时（客户侧的 7 天与签名同寿，这里没有那层绑定）。
    return read_stored_object(
        storage,
        object_key=thumbnail_key,
        cache_control="private, max-age=3600",
        disposition="inline",
    )


@router.get("/generation-records/{record_type}/{record_id}/content")
def get_generation_record_content(
    record_type: MediaRecordType,
    record_id: str,
    request: Request,
    actor: AdminReader,
) -> Response:
    """「查看成片」：按 Range 流式读取客户生成内容（方案 P2-2）。

    客户内容是高敏数据，每次查看写 ``generation_record.content_view`` 审计
    （与 ``external_call.response_view`` 同一口径）。``inline`` 让浏览器页内
    播放视频/展示图片，Range 支持拖动进度条。
    """
    _require_media_viewer(actor)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        storage, object_key = _resolve_record_object(
            conn, record_type=record_type, record_id=record_id
        )
        write_audit(
            conn,
            actor=_admin_to_current_user(actor),
            action="generation_record.content_view",
            entity_type="generation_record",
            entity_id=record_id,
            metadata={"record_type": record_type},
            commit=False,
        )
    return read_stored_object(
        storage,
        object_key=object_key,
        range_header=request.headers.get("range"),
        disposition="inline",
    )
