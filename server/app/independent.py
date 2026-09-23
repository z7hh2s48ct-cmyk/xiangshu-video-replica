"""Standalone video creation domain (C2 独立创作).

文生（T2V）/ 图生（I2V）/ 参考生（R2V）视频的脱离项目上下文创建通道。
批次挂在 ``generation_batches``（project_id 为 NULL、creation_kind 为
"independent"）上，从建批那一刻起即复用复刻流的既有管线：公平队列、
worker 提交/轮询/归档、钱包按秒计费（RESERVE/SETTLE/RELEASE）、任务中心
列表与对账。本模块只负责创建路径的准入与快照，不重建任何管线实体。

供应商中立：表、行与对客文案不得出现数据源供应商名称 (红线同 oral 域)。

扩展模式（尾帧 / T2V / R2V）始终开放，无门禁控制。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any, Literal
from uuid import uuid4

import psycopg.errors as psycopg_errors
from pydantic import BaseModel, ConfigDict, Field

from app.bootstrap import is_customer_production
from app.db_portable import BusinessConnection
from app.generation import (
    H3_MODEL,
    BatchResult,
    H3ProviderSettingsUnavailable,
    _enforce_acceptance_generation_limit,
    _reserve_generation_credit,
    content_hash,
    ensure_user_queue_cursor,
    generation_error,
    get_generation_batch,
    metaso_h3_provider_from_settings,
    read_runtime_limits,
    require_cos_first_frame_storage,
)
from app.operation_costs import snapshot_generation_rates
from app.permissions import require_asset_access, require_not_auditor

IndependentMode = Literal["t2v", "i2v", "l2v", "r2v"]

_MODE_UPPPER = {"t2v": "T2V", "i2v": "I2V", "r2v": "R2V", "l2v": "I2V"}
# 首帧/尾帧/参考图允许的资产类别：用户素材图片与既有图片资产通道。
_FRAME_IMAGE_KINDS = {
    "image",
    "material_image",
    "first_frame",
    "character_source_image",
    "character_contact_sheet",
    "character_approved_image",
}
# R2V 多模态参考允许的视频/音频资产类别：用户素材通道（materials）产物。
# 与“复刻源视频”（kind=reference_video，被拆解的原始爆款）区分——那不是 H3
# R2V 的生成参考输入。
_REFERENCE_VIDEO_KINDS = {"video", "material_video"}
_REFERENCE_AUDIO_KINDS = {"audio", "material_audio", "oral_audio"}
# R2V 参考素材允许的类别并集：统一混合列表 reference_asset_ids 里的资产按
# kind 自动分流到图片/视频/音频三个 role。
_REFERENCE_ANY_KINDS = _FRAME_IMAGE_KINDS | _REFERENCE_VIDEO_KINDS | _REFERENCE_AUDIO_KINDS
MAX_REFERENCE_IMAGES = 8
MAX_REFERENCE_VIDEOS = 3
MAX_REFERENCE_AUDIOS = 3
# 混合输入总上限独立于各类上限（H3最多12份参考文件）。
_MAX_REFERENCE_TOTAL = 12


class IndependentVideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    mode: IndependentMode
    prompt_text: str = Field(min_length=1, max_length=7000)
    first_frame_asset_id: str | None = Field(default=None, min_length=1)
    last_frame_asset_id: str | None = Field(default=None, min_length=1)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=_MAX_REFERENCE_TOTAL)
    output_duration_seconds: int = Field(ge=4, le=15)
    resolution: Literal["768P", "2K"] = "768P"
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] = "adaptive"
    quantity: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    provider: Literal["fake_h3", "metaso"] = "fake_h3"


class IndependentCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extended_modes_enabled: bool = True
    t2v_enabled: bool = True
    i2v_enabled: bool = True
    r2v_enabled: bool = True
    last_frame_enabled: bool = True
    l2v_enabled: bool = False
    max_reference_images: int = MAX_REFERENCE_IMAGES
    max_reference_videos: int = MAX_REFERENCE_VIDEOS
    max_reference_audios: int = MAX_REFERENCE_AUDIOS
    max_quantity: int


def read_independent_capabilities(conn: BusinessConnection) -> IndependentCapabilities:
    # 所有能力默认全部开放
    return IndependentCapabilities(
        extended_modes_enabled=True,
        t2v_enabled=True,
        i2v_enabled=True,
        r2v_enabled=True,
        last_frame_enabled=True,
        max_quantity=read_runtime_limits(conn)["max_generation_count_per_batch"],
    )


def _independent_request_hash(request: IndependentVideoRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    return content_hash(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _find_independent_batch(
    conn: BusinessConnection, *, actor_id: str, key: str
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT id, request_hash
        FROM generation_batches
        WHERE created_by_user_id = %s AND project_id IS NULL AND idempotency_key = %s
        """,
        (actor_id, key),
    ).fetchone()
    return row


def _validated_frame_asset(
    conn: BusinessConnection,
    *,
    actor: Any,
    asset_id: str,
    role: str,
    provider: str,
    allowed_kinds: set[str] = _FRAME_IMAGE_KINDS,
    kind_phrase: str = "an image",
) -> dict[str, Any]:
    asset = require_asset_access(conn, actor=actor, asset_id=asset_id, action="independent.create")
    if str(asset["kind"]) not in allowed_kinds:
        raise generation_error(
            422,
            "INDEPENDENT_ASSET_KIND_UNSUPPORTED",
            f"{role} must be {kind_phrase} asset.",
        )
    storage_uri = str(asset["storage_uri"] or "")
    if not storage_uri:
        raise generation_error(
            422,
            "INDEPENDENT_ASSET_STORAGE_MISSING",
            f"{role} asset has no stored content yet.",
        )
    if provider == "metaso":
        require_cos_first_frame_storage(conn, storage_uri=storage_uri)
    duration = None
    if role == "Reference" and str(asset["kind"]) in (
        _REFERENCE_VIDEO_KINDS | _REFERENCE_AUDIO_KINDS
    ):
        try:
            metadata = json.loads(str(asset["metadata_json"] or "{}"))
        except (TypeError, ValueError):
            metadata = {}
        duration = metadata.get("duration_seconds") if isinstance(metadata, dict) else None
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or not 2 <= duration <= 15
        ):
            raise generation_error(
                422,
                "INDEPENDENT_REFERENCE_DURATION_INVALID",
                "参考视频/音频每段须为2–15秒；缺少时长的历史素材请重新上传后选取。",
            )
    return {
        "asset_id": str(asset["id"]),
        "uri": storage_uri,
        "kind": str(asset["kind"]),
        "duration_seconds": duration,
    }


def _validate_independent_mode_assets(request: IndependentVideoRequest) -> None:
    """

    抽成不触库的纯函数，便于在无 PostgreSQL 的单测。
    T2V/R2V/I2V(含双帧/参考生) 始终可提交，无门禁拦截。
    每类数量上限（图≤8/视≤3/音≤3）依赖资产 kind，在触库分流后由
    ``_validate_reference_kind_limits``校验。
    """
    # T2V / R2V / I2V+ 尾帧始终开放，无门禁检查
    uses_tail_frame = request.last_frame_asset_id is not None
    uses_references = bool(request.reference_asset_ids)
    if request.mode == "t2v" and (request.first_frame_asset_id or uses_tail_frame):
        raise generation_error(
            422, "INDEPENDENT_MODE_ASSET_CONFLICT", "文生视频不能携带首帧或尾帧。"
        )
    if request.mode in {"t2v", "i2v"} and uses_references:
        raise generation_error(
            422,
            "INDEPENDENT_MODE_ASSET_CONFLICT",
            "参考素材仅支持参考生视频(R2V)。",
        )
    # BUG-1（2026-09-19 对 MiniMax-H3 真实核对）：纯文本 T2V 供应商要求 ratio
    # 必填且不能为 adaptive，否则 400（err 2013）。这里在建批入口就拦下，给
    # 用户可读文案，而不是等 worker 提交时才失败。前端在 t2v 模式也会隐藏
    # “自动”选项，二者一致。放在素材矩阵校验之后，保持既有错误码优先级。
    if request.mode == "t2v" and request.ratio == "adaptive":
        raise generation_error(
            422,
            "INDEPENDENT_T2V_RATIO_REQUIRED",
            "文生视频需指定具体画面比例（如 16:9），不支持自动/自适应。",
        )
    if request.mode == "i2v" and not request.first_frame_asset_id:
        raise generation_error(
            422, "INDEPENDENT_FIRST_FRAME_REQUIRED", "图生视频需要选择首帧图片。"
        )
    if request.mode == "r2v":
        if request.first_frame_asset_id or uses_tail_frame:
            raise generation_error(
                422,
                "INDEPENDENT_MODE_ASSET_CONFLICT",
                "参考生视频不能携带首帧或尾帧。",
            )
        if not uses_references:
            raise generation_error(
                422,
                "INDEPENDENT_REFERENCE_REQUIRED",
                "参考生视频至少选择一个参考素材。",
            )
        if len(set(request.reference_asset_ids)) != len(request.reference_asset_ids):
            raise generation_error(
                422,
                "INDEPENDENT_REFERENCE_DUPLICATE",
                "参考素材不能重复选择。",
            )


def _validate_reference_kind_limits(
    *, image_count: int, video_count: int, audio_count: int
) -> None:
    """R2V 每类参考数量上限（图≤8/视≤3/音≤3）。

    统一混合列表按资产 kind 分流后调用；抽成不触库的纯函数便于单测。
    """
    if image_count > MAX_REFERENCE_IMAGES:
        raise generation_error(
            422,
            "INDEPENDENT_REFERENCE_LIMIT_EXCEEDED",
            f"参考图最多 {MAX_REFERENCE_IMAGES} 张。",
        )
    if video_count > MAX_REFERENCE_VIDEOS:
        raise generation_error(
            422,
            "INDEPENDENT_REFERENCE_LIMIT_EXCEEDED",
            f"参考视频最多 {MAX_REFERENCE_VIDEOS} 个。",
        )
    if audio_count > MAX_REFERENCE_AUDIOS:
        raise generation_error(
            422,
            "INDEPENDENT_REFERENCE_LIMIT_EXCEEDED",
            f"参考音频最多 {MAX_REFERENCE_AUDIOS} 个。",
        )


def create_independent_batch(
    conn: BusinessConnection,
    *,
    actor: Any,
    request: IndependentVideoRequest,
) -> BatchResult:
    require_not_auditor(
        conn,
        actor=actor,
        action="independent.create",
        entity_type="generation_batch",
        entity_id="independent",
    )

    mode_upper = _MODE_UPPPER[request.mode]
    _validate_independent_mode_assets(request)

    # 与复刻流同源的生产红线：客户生产禁止模拟任务；metaso 需配置就绪并
    # 遵守付费试用限额。仅对“真正的新提交”生效，幂等回放在此之前返回。
    if request.provider == "fake_h3" and is_customer_production():
        raise generation_error(
            503,
            "FAKE_H3_PROVIDER_FORBIDDEN",
            "Customer production cannot create simulated H3 generation tasks.",
        )
    if request.provider == "metaso":
        _enforce_acceptance_generation_limit(
            conn,
            user_id=actor.id,
            requested_quantity=request.quantity,
        )

    request_hash = _independent_request_hash(request)
    existing = _find_independent_batch(conn, actor_id=actor.id, key=request.idempotency_key)
    if existing is not None:
        if str(existing["request_hash"]) != request_hash:
            raise generation_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This idempotency key was already used for a different request.",
            )
        return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)

    runtime = read_runtime_limits(conn)
    if request.quantity > runtime["max_generation_count_per_batch"]:
        raise generation_error(
            422,
            "QUANTITY_EXCEEDS_LIMIT",
            f"quantity must be less than or equal to {runtime['max_generation_count_per_batch']}",
        )
    if request.provider == "metaso":
        try:
            metaso_h3_provider_from_settings(conn)
        except H3ProviderSettingsUnavailable as exc:
            raise generation_error(
                503,
                "METASO_SETTINGS_UNAVAILABLE",
                "视频生成服务尚未配置完成，请联系管理员。",
            ) from exc

    first_frame = (
        _validated_frame_asset(
            conn,
            actor=actor,
            asset_id=request.first_frame_asset_id,
            role="First frame",
            provider=request.provider,
        )
        if request.first_frame_asset_id
        else None
    )
    last_frame = (
        _validated_frame_asset(
            conn,
            actor=actor,
            asset_id=request.last_frame_asset_id,
            role="Last frame",
            provider=request.provider,
        )
        if request.last_frame_asset_id
        else None
    )
    # 统一混合列表：逐个解析参考素材并按资产 kind 自动分流到图片/视频/音频。
    # 图片保留 name（build_h3_request 的 image content 需要 name+url），视频/
    # 音频只携带 uri（build_h3_request 只读 url）。
    reference_images: list[dict[str, str]] = []
    reference_videos: list[dict[str, str]] = []
    reference_audios: list[dict[str, str]] = []
    reference_labels: dict[str, str] = {}
    reference_seconds = {"video": 0.0, "audio": 0.0}
    for index, asset_id in enumerate(request.reference_asset_ids, start=1):
        resolved = _validated_frame_asset(
            conn,
            actor=actor,
            asset_id=asset_id,
            role="Reference",
            provider=request.provider,
            allowed_kinds=_REFERENCE_ANY_KINDS,
            kind_phrase="an image, video, or audio",
        )
        kind = resolved["kind"]
        if kind in _FRAME_IMAGE_KINDS:
            reference_images.append(
                {
                    "asset_id": resolved["asset_id"],
                    "uri": resolved["uri"],
                    "name": f"ref-{len(reference_images) + 1}",
                }
            )
            reference_labels[str(index)] = f"<Picture {len(reference_images)}>"
        elif kind in _REFERENCE_VIDEO_KINDS:
            reference_seconds["video"] += resolved["duration_seconds"]
            reference_videos.append({"asset_id": resolved["asset_id"], "uri": resolved["uri"]})
            reference_labels[str(index)] = f"<Video {len(reference_videos)}>"
        else:
            reference_seconds["audio"] += resolved["duration_seconds"]
            reference_audios.append({"asset_id": resolved["asset_id"], "uri": resolved["uri"]})
            reference_labels[str(index)] = f"<Audio {len(reference_audios)}>"
    _validate_reference_kind_limits(
        image_count=len(reference_images),
        video_count=len(reference_videos),
        audio_count=len(reference_audios),
    )
    for kind, seconds in reference_seconds.items():
        if seconds > 15:
            label = "视频" if kind == "video" else "音频"
            raise generation_error(
                422,
                "INDEPENDENT_REFERENCE_DURATION_LIMIT_EXCEEDED",
                f"参考{label}累计时长不能超过15秒，请移除部分素材或裁剪后重试。",
            )
    # UI 的 @N 按混合素材排列，供应商标签则按媒体类型独立编号。
    # 原文保留在批次快照，仅编译发给模型的文本；不改写邮箱等普通内容。
    provider_prompt = (
        re.sub(
            r"(?<![A-Za-z0-9_@])@([1-9]\d*)(?!\d)",
            lambda match: reference_labels.get(match[1], match[0]),
            request.prompt_text,
        )
        if mode_upper == "R2V"
        else request.prompt_text
    )

    from typing import cast

    from app.h3_prompts import Mode, prompt_issues

    labels = (
        list(reference_labels.values())
        if mode_upper == "R2V"
        else (
            ["<Picture 1>", "<Picture 2>"] if last_frame else ["<Picture 1>"] if first_frame else []
        )
    )
    prompt_mode = (
        "Ref2VA"
        if mode_upper == "R2V"
        else "FL2VA"
        if last_frame
        else "I2VA"
        if first_frame
        else "T2VA"
    )
    issues = prompt_issues(
        provider_prompt,
        mode=cast(Mode, prompt_mode),
        duration=request.output_duration_seconds,
        labels=labels,
        strict=False,
    )
    if issues:
        raise generation_error(422, issues[0].code, issues[0].message)

    try:
        conn.execute("BEGIN IMMEDIATE")
        concurrent_existing = _find_independent_batch(
            conn, actor_id=actor.id, key=request.idempotency_key
        )
        if concurrent_existing is not None:
            conn.rollback()
            if str(concurrent_existing["request_hash"]) != request_hash:
                raise generation_error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This idempotency key was already used for a different request.",
                )
            return get_generation_batch(conn, batch_id=str(concurrent_existing["id"]), actor=actor)

        batch_id = str(uuid4())
        request_snapshot = {
            "schema_version": "independent.v1",
            "display_name": request.display_name,
            "generation_mode": mode_upper,
            "quantity": request.quantity,
            "prompt_text": request.prompt_text,
            "output_duration_seconds": request.output_duration_seconds,
            "resolution": request.resolution,
            "ratio": request.ratio,
            "provider": request.provider,
            "model": H3_MODEL,
            "first_frame_asset_id": request.first_frame_asset_id,
            "last_frame_asset_id": request.last_frame_asset_id,
            "reference_asset_ids": list(request.reference_asset_ids),
        }
        task_prompt_snapshot: dict[str, Any] = {
            "schema_version": "independent.v1",
            "generation_mode": mode_upper,
            "prompt_text": provider_prompt,
            "reference_labels": reference_labels,
            "output_duration_seconds": request.output_duration_seconds,
            "resolution": request.resolution,
            "ratio": request.ratio,
            "first_frame_uri": first_frame["uri"] if first_frame else None,
            "last_frame_uri": last_frame["uri"] if last_frame else None,
            "reference_images": reference_images,
            "reference_videos": reference_videos,
            "reference_audios": reference_audios,
        }
        conn.execute(
            """
            INSERT INTO generation_batches (
                id,
                project_id,
                created_by_user_id,
                display_name,
                idempotency_key,
                request_hash,
                request_snapshot_json,
                creation_kind,
                status
            )
            VALUES (%s, NULL, %s, %s, %s, %s, %s, 'independent', 'QUEUED')
            """,
            (
                batch_id,
                actor.id,
                request.display_name.strip() if request.display_name else "视频生成",
                request.idempotency_key,
                request_hash,
                json.dumps(request_snapshot, ensure_ascii=True, sort_keys=True),
            ),
        )
        for _ in range(request.quantity):
            task_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO generation_tasks (
                    id,
                    batch_id,
                    generation_mode,
                    provider,
                    model,
                    status,
                    archive_status,
                    quality_status,
                    prompt_snapshot_json,
                    next_poll_at,
                    billed_seconds
                )
                VALUES (%s, %s, %s, %s, %s, 'PENDING', 'PENDING', 'PENDING', %s,
                        CURRENT_TIMESTAMP, %s)
                """,
                (
                    task_id,
                    batch_id,
                    mode_upper,
                    request.provider,
                    H3_MODEL,
                    json.dumps(task_prompt_snapshot, ensure_ascii=True, sort_keys=True),
                    request.output_duration_seconds,
                ),
            )
            snapshot_generation_rates(
                conn,
                task_id=task_id,
                resolution=request.resolution,
                billed_seconds=request.output_duration_seconds,
            )
            _reserve_generation_credit(
                conn,
                user_id=actor.id,
                task_id=task_id,
                seconds=request.output_duration_seconds,
            )
        ensure_user_queue_cursor(conn, user_id=actor.id)
        conn.commit()
    except (sqlite3.IntegrityError, psycopg_errors.UniqueViolation):
        # SQLite：写锁保证事务内复查可见，可安全回放；
        # PG：NULL project 不受 UNIQUE 约束保护，由 075 的部分唯一索引兜底。
        # fenced 事务冲突后已中止，无法在同事务内回放——返回 409 让客户端
        # 重试（重试会命中幂等回放）。
        conn.rollback()
        if conn.is_postgres:
            raise generation_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "Duplicate concurrent submission; retry to fetch the same batch.",
            ) from None
        existing = _find_independent_batch(conn, actor_id=actor.id, key=request.idempotency_key)
        if existing is not None:
            if str(existing["request_hash"]) != request_hash:
                raise generation_error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This idempotency key was already used for a different request.",
                )
            return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)
        raise
    except Exception:
        conn.rollback()
        raise
    return get_generation_batch(conn, batch_id=batch_id, actor=actor)
