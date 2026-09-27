"""Unified Studio material projection over existing business sources.

Assets remain the physical-file source of truth. Provider-hosted H3 DIRECT
results are projected as read-only virtual materials until a separate, explicit
archive action creates a real asset. User preferences never grant access.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import CurrentUser
from app.character_identity import jpeg_dimensions
from app.content_store import (
    delete_object_outside_content_namespace,
    find_content_object,
    retain_content_object,
    retain_existing_content_object,
)
from app.db_portable import BusinessConnection
from app.material_thumbs import (
    extract_image_thumbnail_jpeg,
    extract_thumbnail_jpeg,
    store_video_thumbnail,
)
from app.media import (
    MAX_UPLOAD_BYTES,
    UPLOAD_INTENT_EXPIRES_IN,
    storage_key_from_uri,
)
from app.media_tools import (
    MediaToolFailed,
    MediaToolUnavailable,
    MediaValidationFailed,
    inspect_media_bytes,
    probe_duration_seconds,
    resolve_media_binary,
)
from app.permissions import require_asset_access, require_not_auditor, write_audit
from app.sql_pagination import PAGE_CLAUSE
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    UploadedObjectSizeMismatch,
    read_uploaded_object,
    require_storage_match,
    storage_object_ref_from_uri,
    store_verified_upload,
)

MaterialMediaType = Literal["image", "video", "audio"]
MaterialSource = Literal["upload", "project", "character", "oral", "generation"]
# MATERIAL-UX-03：列表排序维度。created_desc 为默认，与既有行为逐字一致。
MaterialSort = Literal["created_desc", "created_asc", "title_asc", "size_desc"]
MaterialStatus = Literal["uploading", "ready", "unavailable"]
MaterialDelivery = Literal["stored", "direct"]
AudioPurpose = Literal["oral_audio", "voice_clone", "reference"]
# MATERIAL-UX-08：方向筛选维度（按 aspect_ratio 派生：>1.05 横屏、<0.95 竖屏、其余方形）。
MaterialOrientation = Literal["portrait", "landscape", "square"]

IMAGE_UPLOAD_LIMIT = 10 * 1024 * 1024
VOICE_CLONE_UPLOAD_LIMIT = 20 * 1024 * 1024
# R2V 参考音频时长上下限：与参考视频同口径，生成端也按 2–15 秒校验
# （前端拦截 + 后端音频探测双保险）。
MIN_REFERENCE_AUDIO_SECONDS = 2.0
MAX_REFERENCE_AUDIO_SECONDS = 15.0
# 音频格式白名单按用途收窄：口播只认 MP3；参考放开到常见容器；声音克隆不在表内，
# 沿用 ALLOWED_UPLOADS 全集（含 WMA/WMV 等仅克隆可解码的容器）。
AUDIO_SUFFIXES_BY_PURPOSE: dict[AudioPurpose, frozenset[str]] = {
    "oral_audio": frozenset({".mp3"}),
    "reference": frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}),
}
ALLOWED_UPLOADS: dict[tuple[str, str], tuple[MaterialMediaType, str]] = {
    (".jpg", "image/jpeg"): ("image", ".jpg"),
    (".jpeg", "image/jpeg"): ("image", ".jpg"),
    (".png", "image/png"): ("image", ".png"),
    (".mp3", "audio/mpeg"): ("audio", ".mp3"),
    (".wav", "audio/wav"): ("audio", ".wav"),
    (".wav", "audio/x-wav"): ("audio", ".wav"),
    (".m4a", "audio/mp4"): ("audio", ".m4a"),
    (".m4a", "audio/x-m4a"): ("audio", ".m4a"),
    (".aac", "audio/aac"): ("audio", ".aac"),
    (".flac", "audio/flac"): ("audio", ".flac"),
    (".flac", "audio/x-flac"): ("audio", ".flac"),
    (".ogg", "audio/ogg"): ("audio", ".ogg"),
    (".opus", "audio/ogg"): ("audio", ".opus"),
    (".opus", "audio/opus"): ("audio", ".opus"),
    (".wma", "audio/x-ms-wma"): ("audio", ".wma"),
    (".wma", "audio/wma"): ("audio", ".wma"),
    (".aiff", "audio/aiff"): ("audio", ".aiff"),
    (".aiff", "audio/x-aiff"): ("audio", ".aiff"),
    (".aif", "audio/aiff"): ("audio", ".aif"),
    (".aif", "audio/x-aiff"): ("audio", ".aif"),
    (".amr", "audio/amr"): ("audio", ".amr"),
    # WMV is accepted as a voice sample container only; its audio track is
    # decoded and normalized before the voice provider sees it.
    (".wmv", "video/x-ms-wmv"): ("audio", ".wmv"),
    (".mp4", "video/mp4"): ("video", ".mp4"),
    (".mov", "video/quicktime"): ("video", ".mov"),
}
ASSET_KIND_FOR_MEDIA: dict[MaterialMediaType, str] = {
    "image": "material_image",
    "video": "material_video",
    "audio": "material_audio",
}


class MaterialCharacterView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    view_type: str


class MaterialItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    owner_user_id: str
    asset_id: str | None
    generation_task_id: str | None
    project_id: str | None
    person_id: str | None
    # MATERIAL-UX-03：归属对象显示名（人物分支=display_name；项目/成片分支=projects.name）。
    # 无对应对象的分支（如「我的上传」）为 None，前端需容错。
    person_name: str | None = None
    project_title: str | None = None
    # MATERIAL-UX-05：用户侧标签（偏好层 tags_json，全量覆盖语义；无偏好行为 []）。
    tags: list[str] = Field(default_factory=list)
    # MATERIAL-UX-08：宽高与比例（assets.metadata_json 派生；存量素材/直出无存档为 None）。
    width: int | None = None
    height: int | None = None
    aspect_ratio: float | None = None
    # MATERIAL-UX-07：音频用途三态（metadata 派生；非音频/缺省为 None）。
    audio_purpose: AudioPurpose | None = None
    title: str
    group: str
    media_type: MaterialMediaType
    source: MaterialSource
    status: MaterialStatus
    delivery: MaterialDelivery
    content_type: str | None
    size_bytes: int | None
    duration_seconds: float | None
    created_at: str
    hidden: bool
    saved: bool
    composite: bool = False
    preview_asset_id: str | None = None
    # MATERIAL-THUMBS-B：缩略图对象键（assets.metadata_json 派生；历史素材为 None）。
    thumbnail_key: str | None = None
    character_views: list[MaterialCharacterView] = Field(default_factory=list)
    allowed_uses: list[str]
    allowed_actions: list[str]


class MaterialPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MaterialItem]
    page: int
    page_size: int
    total: int


class MaterialResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MaterialItem]
    unavailable_ids: list[str]


class MaterialUploadIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    title: str | None = Field(default=None, min_length=1, max_length=120)
    group: str | None = Field(default=None, min_length=1, max_length=80)
    audio_purpose: AudioPurpose | None = None
    duration_seconds: float | None = Field(default=None, gt=0)


class MaterialUploadIntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_id: str
    asset_id: str
    storage_key: str | None
    method: str
    url: str
    headers: dict[str, str]
    expires_at: str
    # False when the same bytes are already registered for this owner: the asset
    # is created complete and the client must skip both the transfer and the
    # /complete call. Defaults to True so every existing construction keeps the
    # old behaviour.
    upload_required: bool = True
    reused_from_asset_id: str | None = None


class MaterialUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=120)
    group: str | None = Field(default=None, max_length=80)
    hidden: bool | None = None
    # MATERIAL-UX-05：None=未提交（不动既有标签）；list=全量覆盖（[] 即清空）。
    tags: list[str] | None = None


class MaterialGroupItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    count: int


class MaterialTagItem(BaseModel):
    """MATERIAL-UX-05：当前用户可见素材的标签聚合计数。"""

    model_config = ConfigDict(extra="forbid")

    tag: str
    count: int


class MaterialUsage(BaseModel):
    """MATERIAL-UX-10：引用该素材的一个任务。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    kind: Literal["generation", "oral"]
    status: str
    created_at: str


class MaterialUsagesResponse(BaseModel):
    """MATERIAL-UX-10：单素材按需的使用记录（不做列表批量聚合）。"""

    model_config = ConfigDict(extra="forbid")

    total: int
    items: list[MaterialUsage]


class MaterialGroupsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MaterialGroupItem]


class MaterialBulkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str | None = Field(default=None, max_length=80)
    hidden: bool | None = None
    # MATERIAL-UX-05：语义同单条 PATCH——None 不动，list 全量覆盖。
    tags: list[str] | None = None


class MaterialBulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_ids: list[str] = Field(min_length=1, max_length=100)
    update: MaterialBulkUpdate


class MaterialBulkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated: int
    skipped: int


@dataclass(frozen=True)
class PreparedMaterialUpload:
    asset_id: str
    owner_user_id: str
    storage_uri: str
    storage_key: str
    media_type: MaterialMediaType
    content_type: str
    requested_size_bytes: int
    expected_sha256: str | None
    audio_purpose: AudioPurpose | None = None
    requested_duration_seconds: float | None = None


@dataclass(frozen=True)
class ProbedMaterialUpload:
    prepared: PreparedMaterialUpload
    storage_uri: str
    sha256: str
    size_bytes: int
    duration_seconds: float | None = None
    # MATERIAL-THUMBS-B：视频素材的首帧 JPEG 字节（探测期抽出；落存储与记键
    # 延后到持久化之后——dedup 可能改写最终对象键，且外部 I/O 不得进入写事务）。
    thumbnail_jpeg: bytes | None = None
    # MATERIAL-UX-08：探测期顺手取宽高（视频来自 ffprobe，图片来自头解析）；
    # 取不到为 None，不影响上传结果。
    width: int | None = None
    height: int | None = None


def material_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# MATERIAL-UX-05：标签服务端规整上限。trim/去空/去重静默做，超限显式拒绝（可预期的
# 失败优于静默截断）；字符数按 Unicode 码点计，中文一字一符。
TAG_MAX_PER_MATERIAL = 20
TAG_MAX_LENGTH = 40


def _probe_image_dimensions(content: bytes, suffix: str) -> tuple[int, int] | None:
    """MATERIAL-UX-08：图片宽高头解析（PNG/JPEG/WebP），尽力而为。

    与人物库源图校验（image_dimensions，失败即 422）不同——素材库方向信息是
    增值数据，任何解析失败都返回 None，绝不阻塞上传主链路。
    """
    try:
        if (
            suffix == ".png"
            and len(content) >= 24
            and content[:8] == b"\x89PNG\r\n\x1a\n"
            and content[12:16] == b"IHDR"
        ):
            width, height = struct.unpack(">II", content[16:24])
            return (width, height) if width and height else None
        if suffix in (".jpg", ".jpeg"):
            return jpeg_dimensions(content)
        if len(content) >= 25 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            chunk = content[12:16]
            if chunk == b"VP8X" and len(content) >= 30:
                width = 1 + int.from_bytes(content[24:27], "little")
                height = 1 + int.from_bytes(content[27:30], "little")
                return (width, height) if width and height else None
            if chunk == b"VP8 " and len(content) >= 30:
                # 有损 WebP 的 FourCC 是 "VP8 "（第四个字符是空格）。
                width = int.from_bytes(content[26:28], "little") & 0x3FFF
                height = int.from_bytes(content[28:30], "little") & 0x3FFF
                return (width, height) if width and height else None
            if chunk == b"VP8L" and len(content) >= 25 and content[20] == 0x2F:
                bits = int.from_bytes(content[21:25], "little")
                width = (bits & 0x3FFF) + 1
                height = ((bits >> 14) & 0x3FFF) + 1
                return (width, height) if width and height else None
    except (struct.error, ValueError, IndexError, HTTPException):
        # 人物库的 jpeg_dimensions 校验失败走 HTTPException——在这里统一降级为
        # “无方向信息”，方向缺失绝不影响上传主链路。
        return None
    return None


def _decode_tags(raw: Any) -> list[str]:
    """MATERIAL-UX-05：偏好行 tags_json（TEXT-JSON）容错解析为字符串列表。"""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return [str(tag) for tag in parsed] if isinstance(parsed, list) else []
    if isinstance(raw, list):
        return [str(tag) for tag in raw]
    return []


def _normalize_tags(raw: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(tag.strip() for tag in raw if tag.strip()))
    if len(normalized) > TAG_MAX_PER_MATERIAL or any(
        len(tag) > TAG_MAX_LENGTH for tag in normalized
    ):
        raise material_error(
            422,
            "MATERIAL_TAGS_INVALID",
            f"每条素材最多 {TAG_MAX_PER_MATERIAL} 个标签，单个标签不超过 {TAG_MAX_LENGTH} 字。",
        )
    return normalized


def parse_material_id(material_id: str) -> tuple[Literal["asset", "generation"], str]:
    source_type, separator, source_id = material_id.partition(":")
    if source_type not in {"asset", "generation"} or not separator or not source_id:
        raise material_error(404, "MATERIAL_NOT_FOUND", "素材不存在。")
    return cast(Literal["asset", "generation"], source_type), source_id


def validate_upload_request(
    *, filename: str, content_type: str, size_bytes: int
) -> tuple[MaterialMediaType, str]:
    suffix = Path(filename).suffix.lower()
    matched = ALLOWED_UPLOADS.get((suffix, content_type.lower()))
    if matched is None:
        raise material_error(
            415,
            "MATERIAL_TYPE_UNSUPPORTED",
            "仅支持常见图片、视频和音频格式。",
        )
    media_type, safe_suffix = matched
    limit = IMAGE_UPLOAD_LIMIT if media_type == "image" else MAX_UPLOAD_BYTES
    if size_bytes > limit:
        raise material_error(413, "MATERIAL_TOO_LARGE", "素材文件超过允许大小。")
    return media_type, safe_suffix


def validate_audio_contract(
    *,
    media_type: MaterialMediaType,
    audio_purpose: AudioPurpose | None,
    duration_seconds: float | None,
) -> None:
    if media_type != "audio":
        if audio_purpose is not None or duration_seconds is not None:
            raise material_error(
                422,
                "MATERIAL_AUDIO_PURPOSE_INVALID",
                "非音频素材不能声明音频用途或时长。",
            )
        return
    if audio_purpose is None:
        raise material_error(
            422,
            "MATERIAL_AUDIO_PURPOSE_REQUIRED",
            "音频素材必须声明完整口播、声音克隆或参考用途。",
        )
    if duration_seconds is None and audio_purpose == "voice_clone":
        # Some browsers cannot read WMA/WMV duration. Upload completion probes
        # the actual bytes and enforces the authoritative 5–180 second limit.
        return
    if duration_seconds is None:
        raise material_error(
            422,
            "MATERIAL_AUDIO_DURATION_REQUIRED",
            "音频用途素材必须提供可验证时长。",
        )
    if audio_purpose == "voice_clone" and not 5 <= duration_seconds <= 180:
        raise material_error(
            422,
            "MATERIAL_AUDIO_DURATION_INVALID",
            "声音克隆样本时长必须为 5–180 秒。",
        )
    if audio_purpose == "reference" and not (
        MIN_REFERENCE_AUDIO_SECONDS <= duration_seconds <= MAX_REFERENCE_AUDIO_SECONDS
    ):
        raise material_error(
            422,
            "MATERIAL_AUDIO_DURATION_INVALID",
            "参考音频时长须为 2–15 秒。",
        )


def validate_audio_purpose_suffix(*, audio_purpose: AudioPurpose | None, safe_suffix: str) -> None:
    """按用途收窄可用的音频容器；声音克隆不在表内，沿用 ALLOWED_UPLOADS 全集。"""
    if audio_purpose is None:
        return
    allowed_suffixes = AUDIO_SUFFIXES_BY_PURPOSE.get(audio_purpose)
    if allowed_suffixes is not None and safe_suffix not in allowed_suffixes:
        raise material_error(
            415,
            "MATERIAL_TYPE_UNSUPPORTED",
            "完整口播音频仅支持 MP3；参考音频支持 MP3、WAV、M4A、AAC、FLAC、OGG、OPUS。",
        )


def probe_audio_duration(content: bytes, suffix: str = ".mp3") -> float | None:
    """Probe口播/参考音频时长，不做整段解码。

    ``MediaToolUnavailable`` 按调用方语义上抛（→503），其余探测失败返回
    ``None``，由调用方给出 422：缺 ffprobe 是环境问题，不该伪装成文件损坏。
    """
    safe_suffix = suffix if suffix.startswith(".") and suffix[1:].isalnum() else ".mp3"
    ffprobe = resolve_media_binary("ffprobe")
    try:
        # Windows does not let ffprobe reopen an active delete-on-close handle.
        with tempfile.TemporaryDirectory(prefix="material-audio-") as directory:
            audio_path = Path(directory) / f"source{safe_suffix}"
            audio_path.write_bytes(content)
            return probe_duration_seconds(ffprobe, audio_path)
    except OSError:
        return None


def _candidate_cte() -> str:
    return """
    WITH character_sheets AS (
        SELECT version.id AS version_id, asset.id AS asset_id,
               identity.id AS person_id, identity.owner_user_id,
               identity.display_name, persona.name,
               persona.appearance_constraints_json::jsonb ->> 'appearance_type' AS appearance_type
        FROM character_versions AS version
        JOIN character_personas AS persona ON persona.id = version.persona_id
        JOIN person_identities AS identity ON identity.id = persona.identity_id
        JOIN assets AS asset
          ON asset.id = version.publication_snapshot_json::jsonb ->> 'contact_sheet_asset_id'
         AND asset.kind = 'character_contact_sheet'
        WHERE version.status = 'PUBLISHED' AND identity.owner_user_id IS NOT NULL
    ), material_candidates AS (
        SELECT
            'asset' AS source_type,
            asset.id AS source_id,
            project.owner_user_id,
            asset.id AS asset_id,
            NULL AS generation_task_id,
            project.id AS project_id,
            NULL AS person_id,
            project.name AS base_title,
            '项目素材' AS base_group,
            'project' AS source,
            asset.content_type,
            asset.size_bytes,
            asset.metadata_json,
            asset.created_at,
            CASE
                WHEN asset.content_type LIKE 'image/%%' THEN 'image'
                WHEN asset.content_type LIKE 'audio/%%' THEN 'audio'
                ELSE 'video'
            END AS media_type,
            CASE WHEN asset.size_bytes > 0 THEN 'ready' ELSE 'uploading' END AS status,
            'stored' AS delivery,
            NULL AS person_name,
            project.name AS project_title
        FROM assets AS asset
        JOIN projects AS project ON project.id = asset.project_id
        WHERE (asset.content_type LIKE 'image/%%'
           OR asset.content_type LIKE 'audio/%%'
           OR asset.content_type LIKE 'video/%%')
          AND NOT EXISTS (SELECT 1 FROM generation_tasks task WHERE task.result_asset_id=asset.id)

        UNION ALL

        SELECT
            'asset', asset.id, asset.created_by_user_id, asset.id, NULL,
            NULL, NULL, asset.id, '我的上传', 'upload', asset.content_type,
            asset.size_bytes, asset.metadata_json, asset.created_at,
            CASE asset.kind
                WHEN 'material_image' THEN 'image'
                WHEN 'material_audio' THEN 'audio'
                ELSE 'video'
            END,
            CASE WHEN asset.size_bytes > 0 AND asset.sha256 != '' THEN 'ready' ELSE 'uploading' END,
            'stored',
            NULL, NULL
        FROM assets AS asset
        WHERE asset.project_id IS NULL
          AND asset.kind IN ('material_image', 'material_audio', 'material_video')
          AND asset.created_by_user_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM generation_tasks task WHERE task.result_asset_id=asset.id)

        UNION ALL

        SELECT
            'asset', asset.id, oral.owner_user_id, asset.id, NULL,
            NULL, oral.identity_id, oral.title, '口播成片', 'oral',
            asset.content_type, asset.size_bytes, asset.metadata_json,
            asset.created_at, 'video', 'ready', 'stored',
            oral_identity.display_name, NULL
        FROM oral_tasks AS oral
        JOIN assets AS asset ON asset.id = oral.result_asset_id
        LEFT JOIN person_identities AS oral_identity ON oral_identity.id = oral.identity_id
        WHERE oral.status = 'SUCCEEDED'

        UNION ALL

        SELECT DISTINCT
            'asset', asset.id, sheet.owner_user_id, asset.id, NULL,
            NULL, sheet.person_id,
            sheet.display_name || ' · ' || CASE WHEN sheet.appearance_type = 'scene'
                THEN sheet.name ELSE '基础五视图' END,
            CASE WHEN sheet.appearance_type = 'scene' THEN '场景形象照' ELSE '基础五视图' END,
            'character', asset.content_type, asset.size_bytes, asset.metadata_json,
            asset.created_at, 'image', 'ready', 'stored',
            sheet.display_name, NULL
        FROM character_sheets AS sheet
        JOIN assets AS asset ON asset.id = sheet.asset_id

        UNION ALL

        SELECT DISTINCT
            'asset', asset.id, identity.owner_user_id, asset.id, NULL,
            NULL, identity.id,
            identity.display_name || ' · 人物素材', '人物素材', 'character',
            asset.content_type, asset.size_bytes, asset.metadata_json,
            asset.created_at, 'image', 'ready', 'stored',
            identity.display_name, NULL
        FROM character_assets AS character_asset
        JOIN assets AS asset ON asset.id = character_asset.asset_id
        JOIN character_versions AS version
          ON version.id = character_asset.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        JOIN person_identities AS identity ON identity.id = persona.identity_id
        WHERE character_asset.review_status = 'APPROVED'
          AND character_asset.is_published_selection = 1
          AND version.status = 'PUBLISHED'
          AND identity.owner_user_id IS NOT NULL

        UNION ALL

        SELECT
            CASE WHEN asset.id IS NULL THEN 'generation' ELSE 'asset' END,
            COALESCE(asset.id,task.id), COALESCE(project.owner_user_id,batch.created_by_user_id),
            asset.id, task.id,
            project.id, NULL,
            COALESCE(batch.display_name, project.name, '视频生成') || ' · 成片',
            '任务结果', 'generation', 'video/mp4', asset.size_bytes,
            COALESCE(asset.metadata_json,'{}'),
            COALESCE(asset.created_at,task.created_at), 'video', 'ready',
            CASE WHEN asset.id IS NULL THEN 'direct' ELSE 'stored' END,
            NULL, project.name
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        LEFT JOIN projects AS project ON project.id = batch.project_id
        LEFT JOIN assets AS asset ON asset.id = task.result_asset_id
        WHERE (asset.id IS NOT NULL AND asset.size_bytes > 0 AND asset.sha256 != '')
           OR (task.status='SUCCEEDED' AND task.archive_status='DIRECT'
               AND task.result_asset_id IS NULL AND task.superseded_by_task_id IS NULL
               AND task.provider_result_url IS NOT NULL AND task.provider_result_url != ''
               AND NOT EXISTS (
                   SELECT 1 FROM customer_batch_visibility AS visibility
                   WHERE visibility.user_id = %s AND visibility.batch_id = batch.id
               ))
    )
    """


def _scope_clause(actor: CurrentUser) -> tuple[str, list[object]]:
    if actor.role in {"employee", "customer"}:
        return "candidate.owner_user_id = %s", [actor.id]
    return "1 = 1", []


def _grouped_character_clause() -> str:
    # Keep derived IDs resolvable for saved drafts, but list/count one sheet per set.
    # A hidden sheet must not make its five derived images reappear.
    return """NOT (candidate.source = 'character' AND EXISTS (
        SELECT 1 FROM character_assets AS view
        JOIN character_sheets AS sheet ON sheet.version_id = view.character_version_id
        WHERE view.asset_id = candidate.asset_id AND sheet.asset_id != candidate.asset_id
    ))"""


def _orientation_clause() -> str:
    """MATERIAL-UX-08：按 metadata 里的宽高派生方向（与前端阈值一致）。

    metadata_json 是 TEXT-JSON（仓库约定），筛选时按需 ::jsonb 提取；缺宽高的
    存量素材对任何方向筛选都不命中（派生为 NULL），前端提示“上传后生效”。
    """
    ratio = (
        "CAST(candidate.metadata_json::jsonb->>'width' AS float) / "
        "NULLIF(CAST(candidate.metadata_json::jsonb->>'height' AS float), 0)"
    )
    return f"""(CASE
        WHEN {ratio} IS NULL THEN NULL
        WHEN {ratio} > 1.05 THEN 'landscape'
        WHEN {ratio} < 0.95 THEN 'portrait'
        ELSE 'square'
    END) = %s"""


def _group_expression() -> str:
    """有效分组（override 优先，空串回落 base_group），与 MaterialItem.group 同口径。

    读侧统一 BTRIM：存量数据可能带首尾空格（历史 upload intent 未 strip），
    若不 trim，导航会展示“点进去为空”的幽灵分组。
    """
    return "COALESCE(NULLIF(BTRIM(preference.group_override), ''), candidate.base_group)"


def _order_by_clause(sort: str) -> str:
    """列表排序（MATERIAL-UX-03）。

    默认 ``created_desc`` 与既有 ORDER BY 逐字一致（回归护栏）；tiebreaker 保证
    跨分支稳定分页；``size_desc`` 用 NULLS LAST 让无存档直出成片（size_bytes
    IS NULL）稳定落在最后。``title_asc`` 按有效标题（override 优先）升序。
    """
    tiebreaker = "candidate.source_type DESC, candidate.source_id DESC"
    if sort == "created_asc":
        return f"candidate.created_at ASC, {tiebreaker}"
    if sort == "title_asc":
        return (
            "COALESCE(preference.title_override, candidate.base_title) ASC, "
            f"candidate.created_at DESC, {tiebreaker}"
        )
    if sort == "size_desc":
        return f"candidate.size_bytes DESC NULLS LAST, {tiebreaker}"
    return f"candidate.created_at DESC, {tiebreaker}"


def _read_rows(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    media_type: str | None,
    source: str | None,
    query: str | None,
    include_hidden: bool,
    limit: int | None,
    offset: int = 0,
    group: str | None = None,
    sort: str = "created_desc",
    person_id: str | None = None,
    project_id: str | None = None,
    tag: str | None = None,
    orientation: str | None = None,
    trashed: bool = False,
    material_ids: list[tuple[Literal["asset", "generation"], str]] | None = None,
) -> list[Any]:
    scope, scope_params = _scope_clause(actor)
    clauses = [scope]
    if material_ids is None:
        clauses.append(_grouped_character_clause())
    parameters: list[object] = [actor.id, actor.id, *scope_params]
    if trashed:
        # MATERIAL-UX-09：回收站视图——只看已移除（hidden=1）。
        clauses.append("COALESCE(preference.hidden, 0) = 1")
    elif not include_hidden:
        clauses.append("COALESCE(preference.hidden, 0) = 0")
    if media_type:
        clauses.append("candidate.media_type = %s")
        parameters.append(media_type)
    if source:
        clauses.append("candidate.source = %s")
        parameters.append(source)
    if query:
        clauses.append("LOWER(COALESCE(preference.title_override, candidate.base_title)) LIKE %s")
        parameters.append(f"%{query.lower()}%")
    if group is not None:
        # 空串即「未分组」：base_group 恒非空，因此空集返回属预期语义保留。
        clauses.append(f"{_group_expression()} = %s")
        parameters.append(group)
    if project_id is not None:
        clauses.append("candidate.project_id = %s")
        parameters.append(project_id)
    if person_id is not None:
        clauses.append("candidate.person_id = %s")
        parameters.append(person_id)
    if tag is not None:
        # MATERIAL-UX-05：JSONB 数组包含（函数形式避开操作符字符的驱动差异）。
        clauses.append("jsonb_exists(COALESCE(preference.tags_json::jsonb, '[]'::jsonb), %s)")
        parameters.append(tag)
    if orientation is not None:
        # MATERIAL-UX-08：方向筛选（metadata 宽高派生）。
        clauses.append(_orientation_clause())
        parameters.append(orientation)
    if material_ids:
        clauses.append(
            "("
            + " OR ".join(
                "(candidate.source_type = %s AND candidate.source_id = %s)" for _ in material_ids
            )
            + ")"
        )
        for source_type, source_id in material_ids:
            parameters.extend([source_type, source_id])
    pagination = ""
    if limit is not None:
        pagination = PAGE_CLAUSE
        parameters.extend([limit, offset])
    return conn.execute(
        _candidate_cte()
        + f"""
        SELECT
            candidate.*,
            (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                'asset_id', reference.asset_id, 'view_type', reference.view_type
            ) ORDER BY reference.view_type), '[]'::jsonb)::text FROM (
                SELECT DISTINCT view.asset_id, view.view_type
                FROM character_assets AS view
                JOIN character_sheets AS sheet ON sheet.version_id = view.character_version_id
                WHERE sheet.asset_id = candidate.asset_id AND candidate.source = 'character'
                  AND view.review_status = 'APPROVED' AND view.is_published_selection = 1
            ) AS reference) AS character_views_json,
            preference.title_override,
            preference.group_override,
            COALESCE(preference.tags_json, '[]') AS tags_json,
            COALESCE(preference.hidden, 0) AS hidden
        FROM material_candidates AS candidate
        LEFT JOIN studio_material_preferences AS preference
          ON preference.user_id = %s
         AND preference.source_type = candidate.source_type
         AND preference.source_id = candidate.source_id
        WHERE {" AND ".join(clauses)}
        ORDER BY {_order_by_clause(sort)}
        {pagination}
        """,
        tuple(parameters),
    ).fetchall()


def _count_rows(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    media_type: str | None,
    source: str | None,
    query: str | None,
    group: str | None = None,
    person_id: str | None = None,
    project_id: str | None = None,
    tag: str | None = None,
    orientation: str | None = None,
    trashed: bool = False,
) -> int:
    scope, scope_params = _scope_clause(actor)
    clauses = [
        scope,
        # MATERIAL-UX-09：trashed 视图只看 hidden=1，默认只看未移除。
        "COALESCE(preference.hidden, 0) = 1" if trashed else "COALESCE(preference.hidden, 0) = 0",
        _grouped_character_clause(),
    ]
    parameters: list[object] = [actor.id, actor.id, *scope_params]
    if media_type:
        clauses.append("candidate.media_type = %s")
        parameters.append(media_type)
    if source:
        clauses.append("candidate.source = %s")
        parameters.append(source)
    if query:
        clauses.append("LOWER(COALESCE(preference.title_override, candidate.base_title)) LIKE %s")
        parameters.append(f"%{query.lower()}%")
    if group is not None:
        clauses.append(f"{_group_expression()} = %s")
        parameters.append(group)
    if project_id is not None:
        clauses.append("candidate.project_id = %s")
        parameters.append(project_id)
    if person_id is not None:
        clauses.append("candidate.person_id = %s")
        parameters.append(person_id)
    if tag is not None:
        clauses.append("jsonb_exists(COALESCE(preference.tags_json::jsonb, '[]'::jsonb), %s)")
        parameters.append(tag)
    if orientation is not None:
        clauses.append(_orientation_clause())
        parameters.append(orientation)
    row = conn.execute(
        _candidate_cte()
        + f"""
        SELECT COUNT(*) AS total
        FROM material_candidates AS candidate
        LEFT JOIN studio_material_preferences AS preference
          ON preference.user_id = %s
         AND preference.source_type = candidate.source_type
         AND preference.source_id = candidate.source_id
        WHERE {" AND ".join(clauses)}
        """,
        tuple(parameters),
    ).fetchone()
    return int(row["total"]) if row is not None else 0


def _metadata(raw: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _duration(metadata: dict[str, Any]) -> float | None:
    value = metadata.get("duration_seconds")
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _row_title(row: Any, metadata: dict[str, Any]) -> str:
    override = row["title_override"]
    if isinstance(override, str) and override.strip():
        return override.strip()
    original = metadata.get("original_filename")
    if row["source"] == "upload" and isinstance(original, str) and original.strip():
        return original.strip()
    return str(row["base_title"])


def material_item(row: Any) -> MaterialItem:
    metadata = _metadata(row["metadata_json"])
    ready = str(row["status"]) == "ready"
    direct = str(row["delivery"]) == "direct"
    media_type = str(row["media_type"])
    composite = (
        row["source"] == "character" and metadata.get("purpose") == "five_view_contact_sheet"
    )
    views = [
        MaterialCharacterView.model_validate(value)
        for value in json.loads(row["character_views_json"])
    ]
    preferred = next(
        (
            view
            for kind in ("FRONT_FULL", "FRONT_HALF", "FRONT_FACE")
            for view in views
            if view.view_type == kind
        ),
        views[0] if views else None,
    )
    uses: list[str] = []
    if ready and not direct:
        if media_type == "image":
            uses = (
                ["reference"]
                if composite
                else ["original_frame", "first_frame", "tail_frame", "reference"]
            )
        elif media_type == "audio":
            purpose = metadata.get("audio_purpose")
            duration = _duration(metadata)
            duration_valid = duration is not None and duration > 0
            if purpose == "voice_clone" and duration is not None:
                duration_valid = 5 <= duration <= 180
            elif purpose == "reference" and duration is not None:
                duration_valid = (
                    MIN_REFERENCE_AUDIO_SECONDS <= duration <= MAX_REFERENCE_AUDIO_SECONDS
                )
            if (
                purpose in {"oral_audio", "voice_clone", "reference"}
                and duration_valid
                and metadata.get("audio_duration_verified") is True
            ):
                uses = [purpose]
        elif media_type == "video":
            uses = ["reference"]
    actions = ["preview"] if ready else []
    if ready and not direct:
        actions.append("download")
    if direct:
        actions.append("hide")
    else:
        actions.extend(["rename", "hide"])
    group_override = row["group_override"]
    return MaterialItem(
        id=f"{row['source_type']}:{row['source_id']}",
        owner_user_id=str(row["owner_user_id"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        generation_task_id=(
            None if row["generation_task_id"] is None else str(row["generation_task_id"])
        ),
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        person_id=None if row["person_id"] is None else str(row["person_id"]),
        person_name=None if row["person_name"] is None else str(row["person_name"]),
        project_title=None if row["project_title"] is None else str(row["project_title"]),
        tags=_decode_tags(row["tags_json"]),
        width=metadata.get("width") if isinstance(metadata.get("width"), int) else None,
        height=metadata.get("height") if isinstance(metadata.get("height"), int) else None,
        aspect_ratio=(
            float(metadata["aspect_ratio"])
            if isinstance(metadata.get("aspect_ratio"), (int, float))
            else None
        ),
        audio_purpose=(
            cast(
                AudioPurpose,
                metadata["audio_purpose"],
            )
            if metadata.get("audio_purpose") in {"oral_audio", "voice_clone", "reference"}
            else None
        ),
        title=_row_title(row, metadata),
        group=(
            group_override.strip()
            if isinstance(group_override, str) and group_override.strip()
            else str(row["base_group"])
        ),
        media_type=media_type,  # type: ignore[arg-type]
        source=str(row["source"]),  # type: ignore[arg-type]
        status=str(row["status"]),  # type: ignore[arg-type]
        delivery=str(row["delivery"]),  # type: ignore[arg-type]
        content_type=None if row["content_type"] is None else str(row["content_type"]),
        size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
        duration_seconds=_duration(metadata),
        created_at=str(row["created_at"]),
        hidden=bool(row["hidden"]),
        saved=ready and not direct,
        composite=composite,
        preview_asset_id=preferred.asset_id if composite and preferred else None,
        thumbnail_key=(
            metadata["thumbnail_key"]
            if isinstance(metadata.get("thumbnail_key"), str) and metadata["thumbnail_key"]
            else None
        ),
        character_views=views if composite else [],
        allowed_uses=uses,
        allowed_actions=actions,
    )


def list_materials(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    media_type: MaterialMediaType | None,
    source: MaterialSource | None,
    query: str | None,
    page: int,
    page_size: int,
    group: str | None = None,
    sort: MaterialSort = "created_desc",
    person_id: str | None = None,
    project_id: str | None = None,
    tag: str | None = None,
    orientation: MaterialOrientation | None = None,
    trashed: bool = False,
) -> MaterialPage:
    total = _count_rows(
        conn,
        actor=actor,
        media_type=media_type,
        source=source,
        query=query,
        group=group,
        person_id=person_id,
        project_id=project_id,
        tag=tag,
        orientation=orientation,
        trashed=trashed,
    )
    start = (page - 1) * page_size
    rows = _read_rows(
        conn,
        actor=actor,
        media_type=media_type,
        source=source,
        query=query,
        include_hidden=False,
        limit=page_size,
        offset=start,
        group=group,
        sort=sort,
        person_id=person_id,
        project_id=project_id,
        tag=tag,
        orientation=orientation,
        trashed=trashed,
    )
    return MaterialPage(
        items=[material_item(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


def list_material_groups(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
) -> MaterialGroupsResponse:
    """按有效分组聚合当前用户可见、未隐藏的候选素材（同一张 contact sheet 只计一条）。"""
    scope, scope_params = _scope_clause(actor)
    rows = conn.execute(
        _candidate_cte()
        + f"""
        SELECT {_group_expression()} AS name, COUNT(*) AS count
        FROM material_candidates AS candidate
        LEFT JOIN studio_material_preferences AS preference
          ON preference.user_id = %s
         AND preference.source_type = candidate.source_type
         AND preference.source_id = candidate.source_id
        WHERE {scope}
          AND COALESCE(preference.hidden, 0) = 0
          AND {_grouped_character_clause()}
        GROUP BY name
        ORDER BY count DESC, name ASC
        """,
        tuple([actor.id, actor.id, *scope_params]),
    ).fetchall()
    return MaterialGroupsResponse(
        items=[MaterialGroupItem(name=str(row["name"]), count=int(row["count"])) for row in rows]
    )


def list_material_tags(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
) -> list[MaterialTagItem]:
    """MATERIAL-UX-05：按标签聚合当前用户可见、未隐藏的候选素材。

    展开的是「偏好层标签」，未打标签的素材不产生行；owner 围栏、隐藏过滤与
    人物去重口径与列表/分组聚合完全一致。
    """
    scope, scope_params = _scope_clause(actor)
    rows = conn.execute(
        _candidate_cte()
        + f"""
        SELECT tag.value AS tag, COUNT(*) AS count
        FROM material_candidates AS candidate
        LEFT JOIN studio_material_preferences AS preference
          ON preference.user_id = %s
         AND preference.source_type = candidate.source_type
         AND preference.source_id = candidate.source_id
        CROSS JOIN LATERAL jsonb_array_elements_text(
            COALESCE(preference.tags_json::jsonb, '[]'::jsonb)
        ) AS tag(value)
        WHERE {scope}
          AND COALESCE(preference.hidden, 0) = 0
          AND {_grouped_character_clause()}
        GROUP BY tag.value
        ORDER BY count DESC, tag.value ASC
        """,
        tuple([actor.id, actor.id, *scope_params]),
    ).fetchall()
    return [MaterialTagItem(tag=str(row["tag"]), count=int(row["count"])) for row in rows]


def list_material_usages(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_id: str,
) -> MaterialUsagesResponse:
    """MATERIAL-UX-10：按需查询单个素材被哪些任务引用。

    只对 asset 来源素材有意义（直出成片本身就是任务产物，无“被引用”概念）；
    owner 围栏：generation 任务经 batch 归属校验，oral 任务按 owner 直属。
    最多返回 20 条（total 用窗口计数给出全量），避免详情面板长列表。
    """
    require_material(conn, actor=actor, material_id=material_id)
    source_type, source_id = parse_material_id(material_id)
    if source_type == "generation":
        return MaterialUsagesResponse(total=0, items=[])
    rows = conn.execute(
        """
        SELECT usage.kind, usage.task_id, usage.status, usage.created_at,
               COUNT(*) OVER () AS total_count
        FROM (
            SELECT 'generation' AS kind, task.id AS task_id, task.status AS status,
                   task.created_at AS created_at
            FROM generation_tasks task
            JOIN generation_batches batch ON batch.id = task.batch_id
            WHERE task.result_asset_id = %s AND batch.created_by_user_id = %s
            UNION ALL
            SELECT 'oral' AS kind, oral.id AS task_id, oral.status AS status,
                   oral.created_at AS created_at
            FROM oral_tasks oral
            WHERE oral.result_asset_id = %s AND oral.owner_user_id = %s
        ) AS usage
        ORDER BY usage.created_at DESC
        LIMIT 20
        """,
        (source_id, actor.id, source_id, actor.id),
    ).fetchall()
    items = [
        MaterialUsage(
            task_id=str(row["task_id"]),
            kind=cast(Literal["generation", "oral"], row["kind"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]
    total = int(rows[0]["total_count"]) if rows else 0
    return MaterialUsagesResponse(total=total, items=items)


def resolve_materials(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_ids: list[str],
) -> MaterialResolveResponse:
    deduplicated = list(dict.fromkeys(material_ids))
    parsed_ids = [parse_material_id(material_id) for material_id in deduplicated]
    rows = _read_rows(
        conn,
        actor=actor,
        media_type=None,
        source=None,
        query=None,
        include_hidden=True,
        limit=None,
        material_ids=parsed_ids,
    )
    by_id = {item.id: item for item in map(material_item, rows)}
    items = [by_id[item_id] for item_id in deduplicated if item_id in by_id]
    return MaterialResolveResponse(
        items=items,
        unavailable_ids=[item_id for item_id in deduplicated if item_id not in by_id],
    )


def require_material(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_id: str,
) -> MaterialItem:
    resolved = resolve_materials(conn, actor=actor, material_ids=[material_id])
    if not resolved.items:
        raise material_error(404, "MATERIAL_NOT_FOUND", "素材不存在。")
    return resolved.items[0]


def _reuse_registered_material(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    request: MaterialUploadIntentRequest,
    media_type: MaterialMediaType,
    safe_suffix: str,
) -> MaterialUploadIntentResponse | None:
    """Reuse bytes this owner already registered, skipping the transfer entirely.

    Returns None when there is nothing to reuse, so the caller falls through to
    the normal upload intent. The lookup is deliberately scoped to the owner:
    an identical file uploaded by another customer must never be served from
    their object, because a material is typically private footage, a voice
    recording or a portrait.
    """
    if not request.sha256:
        return None
    existing = find_content_object(
        conn,
        sha256=request.sha256,
        size_bytes=request.size_bytes,
        provider=storage.provider,
        bucket=storage.bucket,
        scope="user",
        owner_user_id=actor.id,
    )
    if existing is None:
        return None
    # The registry can outlive the object (manual deletion, lifecycle expiry).
    # Only skip the transfer when the bytes are actually still there.
    if storage.head_object(existing.object_key) is None:
        return None
    source = conn.execute(
        "SELECT id, metadata_json FROM assets WHERE content_object_id = %s "
        "ORDER BY CASE WHEN metadata_json::jsonb ->> 'audio_duration_verified' = 'true' "
        "THEN 0 ELSE 1 END, created_at LIMIT 1",
        (existing.id,),
    ).fetchone()
    source_metadata = _metadata(source["metadata_json"]) if source is not None else {}
    if media_type == "audio":
        duration = source_metadata.get("duration_seconds")
        if (
            source_metadata.get("audio_duration_verified") is not True
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration <= 0
        ):
            # Older rows may lack trustworthy probe metadata. Upload and probe
            # again rather than blessing the duration supplied by the client.
            return None
        validate_audio_contract(
            media_type=media_type,
            audio_purpose=request.audio_purpose,
            duration_seconds=duration,
        )
        # 复用是不上传的「秒传」：容器来自源素材，按源文件名判定用途是否可用，
        # 否则同名不同容器的重命名上传会绕过用途白名单。
        validate_audio_purpose_suffix(
            audio_purpose=request.audio_purpose,
            safe_suffix=(
                Path(str(source_metadata.get("original_filename") or "")).suffix.lower()
                or safe_suffix
            ),
        )
    content = retain_existing_content_object(conn, existing.id)
    asset_id = str(uuid4())
    metadata: dict[str, Any] = {
        "upload_status": "READY",
        "object_key": content.object_key,
        "original_filename": request.filename,
        "requested_size_bytes": request.size_bytes,
        "requested_content_type": request.content_type,
        "expected_sha256": request.sha256,
        "content_deduplicated": True,
    }
    if request.audio_purpose is not None:
        metadata["audio_purpose"] = request.audio_purpose
        metadata["duration_seconds"] = source_metadata["duration_seconds"]
        metadata["audio_duration_verified"] = True
    elif "duration_seconds" in source_metadata:
        # Carry the probing work over from the upload that stored these bytes;
        # re-probing would cost a download and a media-tool invocation.
        metadata["duration_seconds"] = source_metadata["duration_seconds"]
    reused_from = None if source is None else str(source["id"])
    with conn:
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, metadata_json, created_by_user_id, content_object_id
            ) VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                ASSET_KIND_FOR_MEDIA[media_type],
                content.storage_uri,
                request.sha256,
                request.size_bytes,
                request.content_type,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                actor.id,
                content.id,
            ),
        )
        if request.title or request.group:
            _upsert_preference(
                conn,
                actor_id=actor.id,
                source_type="asset",
                source_id=asset_id,
                title=request.title,
                group=request.group,
                hidden=False,
            )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.upload_deduplicated",
            entity_type="asset",
            entity_id=asset_id,
            metadata={
                "media_type": media_type,
                "size_bytes": request.size_bytes,
                "reused_from_asset_id": reused_from,
                "sha256_prefix": request.sha256[:12],
                "scope": "user",
            },
            commit=False,
        )
    return MaterialUploadIntentResponse(
        material_id=f"asset:{asset_id}",
        asset_id=asset_id,
        storage_key=content.object_key,
        method="",
        url="",
        headers={},
        expires_at="",
        upload_required=False,
        reused_from_asset_id=reused_from,
    )


def create_material_upload_intent(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    request: MaterialUploadIntentRequest,
) -> MaterialUploadIntentResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.material.upload_intent",
        entity_type="material",
        entity_id="new",
    )
    media_type, safe_suffix = validate_upload_request(
        filename=request.filename,
        content_type=request.content_type,
        size_bytes=request.size_bytes,
    )
    validate_audio_contract(
        media_type=media_type,
        audio_purpose=request.audio_purpose,
        duration_seconds=request.duration_seconds,
    )
    if media_type == "audio":
        validate_audio_purpose_suffix(audio_purpose=request.audio_purpose, safe_suffix=safe_suffix)
    if request.audio_purpose == "voice_clone" and request.size_bytes > VOICE_CLONE_UPLOAD_LIMIT:
        raise material_error(413, "MATERIAL_TOO_LARGE", "声音克隆样本不能超过 20MB。")
    reuse = _reuse_registered_material(
        conn,
        actor=actor,
        storage=storage,
        request=request,
        media_type=media_type,
        safe_suffix=safe_suffix,
    )
    if reuse is not None:
        return reuse
    asset_id = str(uuid4())
    object_key = f"materials/{actor.id}/{asset_id}/original{safe_suffix}"
    intent = storage.create_upload_intent(
        object_key,
        content_type=request.content_type,
        expires_in=UPLOAD_INTENT_EXPIRES_IN,
        size_bytes=request.size_bytes,
    )
    metadata = {
        "upload_status": "PENDING",
        "object_key": intent.key,
        "original_filename": request.filename,
        "requested_size_bytes": request.size_bytes,
        "requested_content_type": request.content_type,
        "expected_sha256": request.sha256,
        "intent_expires_at": intent.expires_at.isoformat(),
        "upload_source_uri": f"{storage.provider}://{storage.bucket}/{intent.key}",
    }
    if request.audio_purpose is not None:
        metadata["audio_purpose"] = request.audio_purpose
        metadata["duration_seconds"] = request.duration_seconds
    storage_uri = f"{storage.provider}://{storage.bucket}/{intent.key}"
    with conn:
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, metadata_json, created_by_user_id
            ) VALUES (%s, NULL, %s, %s, '', 0, %s, %s, %s)
            """,
            (
                asset_id,
                ASSET_KIND_FOR_MEDIA[media_type],
                storage_uri,
                request.content_type,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                actor.id,
            ),
        )
        if request.title or request.group:
            _upsert_preference(
                conn,
                actor_id=actor.id,
                source_type="asset",
                source_id=asset_id,
                title=request.title,
                group=request.group,
                hidden=False,
            )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.upload_intent",
            entity_type="asset",
            entity_id=asset_id,
            metadata={
                "media_type": media_type,
                "size_bytes": request.size_bytes,
                "upload_source_uri": storage_uri,
                "intent_expires_at": intent.expires_at.isoformat(),
            },
            commit=False,
        )
    return MaterialUploadIntentResponse(
        material_id=f"asset:{asset_id}",
        asset_id=asset_id,
        storage_key=intent.key,
        method=intent.method,
        url=intent.url,
        headers=intent.headers,
        expires_at=intent.expires_at.isoformat(),
    )


def prepare_material_upload(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    asset_id: str,
    pending_only: bool = False,
) -> PreparedMaterialUpload:
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="studio.material.upload_complete",
    )
    kind = str(row["kind"])
    media_by_kind = {value: key for key, value in ASSET_KIND_FOR_MEDIA.items()}
    media_type = media_by_kind.get(kind)
    if media_type is None:
        raise material_error(409, "MATERIAL_UPLOAD_INVALID", "该素材不是通用上传任务。")
    metadata = _metadata(row["metadata_json"])
    if pending_only and metadata.get("upload_status") != "PENDING":
        raise material_error(409, "MATERIAL_UPLOAD_INVALID", "该上传已完成或失效，请重新创建上传。")
    requested_size: object
    if metadata.get("upload_status") == "READY":
        requested_size = int(row["size_bytes"])
    elif metadata.get("upload_status") == "PENDING":
        requested_size = metadata.get("requested_size_bytes")
    else:
        raise material_error(409, "MATERIAL_UPLOAD_INVALID", "上传状态无效。")
    if not isinstance(requested_size, int) or requested_size <= 0:
        raise material_error(409, "MATERIAL_UPLOAD_INVALID", "上传记录不完整。")
    storage_uri = str(row["storage_uri"])
    reference = storage_object_ref_from_uri(storage_uri)
    return PreparedMaterialUpload(
        asset_id=asset_id,
        owner_user_id=actor.id,
        storage_uri=storage_uri,
        storage_key=reference.key,
        media_type=media_type,
        content_type=str(row["content_type"]),
        requested_size_bytes=requested_size,
        expected_sha256=(
            str(row["sha256"])
            if metadata.get("upload_status") == "READY"
            else str(metadata["expected_sha256"])
            if metadata.get("expected_sha256")
            else None
        ),
        audio_purpose=(
            cast(AudioPurpose, metadata["audio_purpose"])
            if metadata.get("audio_purpose") in {"oral_audio", "voice_clone", "reference"}
            else None
        ),
        requested_duration_seconds=(
            float(metadata["duration_seconds"])
            if isinstance(metadata.get("duration_seconds"), (int, float))
            else None
        ),
    )


def probe_material_upload(
    prepared: PreparedMaterialUpload,
    *,
    storage: StorageAdapter,
) -> ProbedMaterialUpload:
    reference = storage_object_ref_from_uri(prepared.storage_uri)
    require_storage_match(storage, reference)
    stored = storage.head_object(prepared.storage_key)
    if stored is None:
        raise material_error(409, "MATERIAL_OBJECT_MISSING", "上传文件尚未到达存储。")
    if stored.size != prepared.requested_size_bytes:
        raise material_error(409, "MATERIAL_SIZE_MISMATCH", "上传文件大小不一致。")
    try:
        content = read_uploaded_object(
            storage,
            prepared.storage_key,
            expected_size=prepared.requested_size_bytes,
            max_bytes=IMAGE_UPLOAD_LIMIT if prepared.media_type == "image" else MAX_UPLOAD_BYTES,
        )
    except UploadedObjectSizeMismatch as exc:
        raise material_error(409, "MATERIAL_SIZE_MISMATCH", "上传文件大小不一致。") from exc
    except OSError as exc:
        raise StorageBackendUnavailable("material object read failed") from exc
    suffix = Path(prepared.storage_key).suffix.lower() or ".bin"
    content_matches = (
        _audio_content_matches_suffix(content, suffix)
        if prepared.media_type == "audio"
        else _content_matches(prepared.media_type, content)
    )
    if not content_matches:
        raise material_error(422, "MATERIAL_CONTENT_INVALID", "文件内容与素材类型不匹配。")
    duration_seconds = None
    thumbnail_jpeg: bytes | None = None
    probed_width: int | None = None
    probed_height: int | None = None
    probed_dimensions: tuple[int, int] | None = None
    if prepared.media_type == "video":
        try:
            inspection = inspect_media_bytes(
                content, suffix=".mp4", expected_type="video", min_duration_seconds=0.001
            )
            duration_seconds = inspection.duration_seconds
            # MATERIAL-UX-08：ffprobe 已返回宽高，顺手记录（缺失不阻塞）。
            probed_width = inspection.width
            probed_height = inspection.height
        except MediaValidationFailed as exc:
            raise material_error(422, "MATERIAL_VIDEO_INVALID", "无法读取有效视频及时长。") from exc
        except (MediaToolFailed, MediaToolUnavailable) as exc:
            raise material_error(
                503, "MATERIAL_VIDEO_PROBE_UNAVAILABLE", "视频校验服务暂不可用，请稍后重试。"
            ) from exc
        thumbnail_jpeg = extract_thumbnail_jpeg(content)
    if prepared.media_type == "image":
        # MATERIAL-UX-08：图片宽高头解析，失败只损失方向信息，不影响上传。
        probed_dimensions = _probe_image_dimensions(content, suffix)
    if prepared.media_type == "image":
        # MATERIAL-UX-02：图片素材复用同一抽帧派生（ffmpeg 把单帧图缩成
        # ≤960×480 的 JPEG，超宽合成图也有宽度上界）；网格瓦片因此不再直拉
        # 原图（手机照片可达 10MB/张）。失败返回 None 只损失缩略图，上传结果
        # 不受影响。
        thumbnail_jpeg = extract_image_thumbnail_jpeg(content)
    if prepared.media_type == "audio" and prepared.audio_purpose is not None:
        if prepared.audio_purpose == "voice_clone":
            try:
                inspection = inspect_media_bytes(
                    content,
                    suffix=suffix,
                    expected_type="audio",
                    min_duration_seconds=5,
                    max_duration_seconds=180,
                    local_input_only=True,
                )
                duration_seconds = inspection.duration_seconds
            except MediaValidationFailed as exc:
                raise material_error(
                    422,
                    "MATERIAL_AUDIO_INVALID",
                    "无法读取有效音频或时长不符合要求。",
                ) from exc
            except (MediaToolFailed, MediaToolUnavailable) as exc:
                raise material_error(
                    503,
                    "MATERIAL_AUDIO_PROBE_UNAVAILABLE",
                    "音频校验服务暂不可用，请稍后重试。",
                ) from exc
        else:
            try:
                duration_seconds = probe_audio_duration(content, suffix)
            except MediaToolUnavailable as exc:
                raise material_error(
                    503,
                    "MATERIAL_AUDIO_PROBE_UNAVAILABLE",
                    "音频校验服务暂不可用，请稍后重试。",
                ) from exc
        if duration_seconds is None:
            raise material_error(422, "MATERIAL_AUDIO_INVALID", "无法读取音频时长。")
        if prepared.audio_purpose == "reference" and not (
            MIN_REFERENCE_AUDIO_SECONDS <= duration_seconds <= MAX_REFERENCE_AUDIO_SECONDS
        ):
            raise material_error(
                422,
                "MATERIAL_AUDIO_DURATION_INVALID",
                "参考音频时长须为 2–15 秒。",
            )
        requested_duration = prepared.requested_duration_seconds
        if requested_duration is not None and abs(duration_seconds - requested_duration) > 1:
            raise material_error(
                422,
                "MATERIAL_AUDIO_DURATION_MISMATCH",
                "音频实际时长与上传声明不一致。",
            )
    digest = hashlib.sha256(content).hexdigest()
    if prepared.expected_sha256 and digest != prepared.expected_sha256:
        raise material_error(409, "MATERIAL_HASH_MISMATCH", "上传文件校验失败。")
    stored = store_verified_upload(
        storage,
        asset_id=prepared.asset_id,
        source_key=prepared.storage_key,
        content=content,
        content_type=prepared.content_type,
    )
    if probed_dimensions is not None:
        probed_width, probed_height = probed_dimensions
    return ProbedMaterialUpload(
        prepared=prepared,
        storage_uri=stored.uri,
        sha256=digest,
        size_bytes=stored.size,
        duration_seconds=duration_seconds,
        thumbnail_jpeg=thumbnail_jpeg,
        width=probed_width,
        height=probed_height,
    )


def attach_video_thumbnail(
    conn: BusinessConnection,
    *,
    asset_id: str,
    thumbnail_jpeg: bytes,
    storage: StorageAdapter,
) -> None:
    """MATERIAL-THUMBS-B / MATERIAL-UX-02：持久化完成后把缩略图落到最终对象旁并记键.

    视频与图片素材共用本链路（ffmpeg 对单帧图片同样能产出 JPEG 缩略图）。
    必须在写事务之外调用（存储 PUT 是外部 I/O）；dedup 可能改写最终对象键，
    因此读取持久化后的 storage_uri 派生缩略图键。任何失败只损失缩略图。
    """
    row = conn.execute(
        "SELECT storage_uri, content_type FROM assets WHERE id = %s", (asset_id,)
    ).fetchone()
    if row is None or row["content_type"] is None:
        return
    content_type = str(row["content_type"])
    if not content_type.startswith(("video/", "image/")):
        return
    key = store_video_thumbnail(
        storage,
        storage_key_from_uri(str(row["storage_uri"])),
        thumbnail_jpeg,
        is_image=content_type.startswith("image/"),
    )
    if key is None:
        return
    with conn:
        current = conn.execute(
            "SELECT metadata_json FROM assets WHERE id = %s FOR UPDATE", (asset_id,)
        ).fetchone()
        metadata = _metadata(current["metadata_json"] if current is not None else "{}")
        metadata["thumbnail_key"] = key
        conn.execute(
            "UPDATE assets SET metadata_json = %s WHERE id = %s",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), asset_id),
        )


def persist_material_upload(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    probed: ProbedMaterialUpload,
    storage: StorageAdapter | None = None,
) -> MaterialItem:
    conn.execute(
        "SELECT id FROM assets WHERE id=%s FOR UPDATE", (probed.prepared.asset_id,)
    ).fetchone()
    prepared = prepare_material_upload(conn, actor=actor, asset_id=probed.prepared.asset_id)
    if prepared.storage_uri != probed.prepared.storage_uri:
        raise material_error(409, "MATERIAL_UPLOAD_CHANGED", "上传记录已变化。")
    row = conn.execute(
        "SELECT metadata_json FROM assets WHERE id = %s", (prepared.asset_id,)
    ).fetchone()
    metadata = _metadata(row["metadata_json"] if row is not None else "{}")
    metadata["upload_status"] = "READY"
    if probed.duration_seconds is not None:
        metadata["duration_seconds"] = probed.duration_seconds
    # MATERIAL-UX-08：宽高与方向派生数据（存量素材缺字段时前端优雅降级）。
    if probed.width and probed.height:
        metadata["width"] = probed.width
        metadata["height"] = probed.height
        metadata["aspect_ratio"] = round(probed.width / probed.height, 2)
    if prepared.media_type == "video" and probed.duration_seconds is not None:
        metadata["video_duration_verified"] = True
    if prepared.media_type == "audio" and probed.duration_seconds is not None:
        metadata["audio_duration_verified"] = True
        validate_audio_contract(
            media_type="audio",
            audio_purpose=metadata.get("audio_purpose"),
            duration_seconds=probed.duration_seconds,
        )
    # Register the bytes so a later upload of the same file can reuse them
    # instead of transferring them again. Materials stay user-scoped on purpose:
    # two customers uploading an identical file must never resolve to one object,
    # because a material is often a person's own footage, voice or portrait.
    ref = storage_object_ref_from_uri(probed.storage_uri)
    content, deduplicated = retain_content_object(
        conn,
        sha256=probed.sha256,
        size_bytes=probed.size_bytes,
        content_type=prepared.content_type,
        provider=ref.provider,
        bucket=ref.bucket,
        object_key=ref.key,
        scope="user",
        owner_user_id=actor.id,
    )
    # A concurrent upload of the same bytes won the registration, so this asset
    # points at the object that already existed and the copy this request just
    # transferred is an orphan. Deleting it is best-effort: leaving it costs
    # storage but never breaks a reference, and upload_cleanup reaps it later.
    orphan_key = ref.key if ref.key != content.object_key else None
    metadata["content_deduplicated"] = deduplicated
    with conn:
        conn.execute(
            """
            UPDATE assets
            SET storage_uri = %s, sha256 = %s, size_bytes = %s, metadata_json = %s,
                content_object_id = %s
            WHERE id = %s
            """,
            (
                content.storage_uri,
                probed.sha256,
                probed.size_bytes,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                content.id,
                prepared.asset_id,
            ),
        )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.upload_complete",
            entity_type="asset",
            entity_id=prepared.asset_id,
            metadata={
                "media_type": prepared.media_type,
                "size_bytes": probed.size_bytes,
                "content_deduplicated": deduplicated,
            },
            commit=False,
        )
        result = require_material(conn, actor=actor, material_id=f"asset:{prepared.asset_id}")
    if orphan_key is not None and storage is not None:
        delete_object_outside_content_namespace(storage, orphan_key, actor_id=actor.id)
    return result


def update_material(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_id: str,
    request: MaterialUpdateRequest,
) -> MaterialItem:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.material.update",
        entity_type="material",
        entity_id=material_id,
    )
    source_type, source_id = parse_material_id(material_id)
    current = require_material(conn, actor=actor, material_id=material_id)
    if source_type == "generation" and (request.title is not None or request.group is not None):
        raise material_error(409, "MATERIAL_ACTION_UNAVAILABLE", "直出成片暂不支持重命名。")
    if (
        request.title is None
        and request.group is None
        and request.hidden is None
        and request.tags is None
    ):
        raise material_error(422, "MATERIAL_UPDATE_EMPTY", "至少提交一项修改。")
    title = request.title.strip() if isinstance(request.title, str) else None
    group = request.group.strip() if isinstance(request.group, str) else None
    # MATERIAL-UX-05：None=未提交不动；list=全量覆盖（规整后）。
    tags = _normalize_tags(request.tags) if request.tags is not None else None
    if request.title is not None and not title:
        raise material_error(422, "MATERIAL_TITLE_EMPTY", "素材名称不能为空。")
    if request.group is not None and not group:
        raise material_error(422, "MATERIAL_GROUP_EMPTY", "素材分组不能为空。")
    with conn:
        _upsert_preference(
            conn,
            actor_id=actor.id,
            source_type=source_type,
            source_id=source_id,
            title=title,
            group=group,
            hidden=current.hidden if request.hidden is None else request.hidden,
            tags=tags,
        )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.update",
            entity_type="material",
            entity_id=current.id,
            metadata={
                "title_changed": request.title is not None,
                "group_changed": request.group is not None,
                "hidden_changed": request.hidden is not None,
                "tags_changed": request.tags is not None,
            },
            commit=False,
        )
        result = require_material(conn, actor=actor, material_id=material_id)
    return result


def bulk_update_materials(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    request: MaterialBulkRequest,
) -> MaterialBulkResult:
    """逐条 owner 校验的批量维护；直出成片的 group 变更逐条跳过。

    在调用者的单一写事务内处理；只写一条汇总审计，不逐条写。
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.material.bulk_update",
        entity_type="material",
        entity_id="bulk",
    )
    fields = request.update.model_fields_set
    if not fields:
        raise material_error(422, "MATERIAL_UPDATE_EMPTY", "至少提交一项修改。")
    submitted_group = request.update.group
    group = submitted_group.strip() if isinstance(submitted_group, str) else None
    if "group" in fields and submitted_group is not None and not group:
        raise material_error(422, "MATERIAL_GROUP_EMPTY", "素材分组不能为空。")
    set_group = "group" in fields and group is not None
    clear_group = "group" in fields and group is None
    hidden = request.update.hidden
    # MATERIAL-UX-05：批量标签全量覆盖（规整一次，逐条应用）；None=不动。
    set_tags = _normalize_tags(request.update.tags) if request.update.tags is not None else None
    updated = 0
    skipped = 0
    with conn:
        # 去重并固定顺序：重复 id 不多计 updated，同时消除乱序并发批量间的行锁死锁面。
        for material_id in sorted(dict.fromkeys(request.material_ids)):
            try:
                source_type, source_id = parse_material_id(material_id)
                current = require_material(conn, actor=actor, material_id=material_id)
                if source_type == "generation" and "group" in fields:
                    # 单条 409（直出成片不支持分组）的批量降级：逐条跳过。
                    skipped += 1
                    continue
                target_hidden = current.hidden if hidden is None else hidden
                if set_group:
                    _upsert_preference(
                        conn,
                        actor_id=actor.id,
                        source_type=source_type,
                        source_id=source_id,
                        title=None,
                        group=group,
                        hidden=target_hidden,
                        tags=set_tags,
                    )
                elif clear_group:
                    _upsert_preference(
                        conn,
                        actor_id=actor.id,
                        source_type=source_type,
                        source_id=source_id,
                        title=None,
                        group=None,
                        hidden=target_hidden,
                        clear_group=True,
                        tags=set_tags,
                    )
                else:
                    # 仅 hidden：preserve_overrides 语义，绝不覆盖 title/group。
                    _upsert_preference(
                        conn,
                        actor_id=actor.id,
                        source_type=source_type,
                        source_id=source_id,
                        title=None,
                        group=None,
                        hidden=target_hidden,
                        preserve_overrides=True,
                        tags=set_tags,
                    )
                updated += 1
            except HTTPException:
                skipped += 1
        write_audit(
            conn,
            actor=actor,
            action="studio.material.bulk_update",
            entity_type="material",
            entity_id="bulk",
            metadata={
                "requested": len(request.material_ids),
                "updated": updated,
                "skipped": skipped,
                "group_changed": "group" in fields,
                "hidden_changed": "hidden" in fields,
                "tags_changed": "tags" in fields,
            },
            commit=False,
        )
    return MaterialBulkResult(updated=updated, skipped=skipped)


def hide_material(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_id: str,
) -> None:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.material.hide",
        entity_type="material",
        entity_id=material_id,
    )
    current = require_material(conn, actor=actor, material_id=material_id)
    source_type, source_id = parse_material_id(material_id)
    with conn:
        _upsert_preference(
            conn,
            actor_id=actor.id,
            source_type=source_type,
            source_id=source_id,
            title=None,
            group=None,
            hidden=True,
            preserve_overrides=True,
        )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.hide",
            entity_type="material",
            entity_id=current.id,
            metadata={},
            commit=False,
        )


def _upsert_preference(
    conn: BusinessConnection,
    *,
    actor_id: str,
    source_type: str,
    source_id: str,
    title: str | None,
    group: str | None,
    hidden: bool,
    preserve_overrides: bool = False,
    clear_group: bool = False,
    tags: list[str] | None = None,
) -> None:
    # 写入侧统一 strip：与单条 PATCH / bulk 一致，避免新增带首尾空格的 override。
    if isinstance(group, str):
        group = group.strip() or None
    # MATERIAL-UX-05：tags 参数语义——None=不动既有标签；list（含 []）=全量覆盖。
    # INSERT 无既有行时落到 '[]'；UPDATE 用 COALESCE 保留。列是 TEXT-JSON，直接存文本。
    tags_json = json.dumps(tags, ensure_ascii=False) if tags is not None else None
    if preserve_overrides:
        conn.execute(
            """
            INSERT INTO studio_material_preferences (
                user_id, source_type, source_id, hidden, tags_json
            ) VALUES (%s, %s, %s, %s, COALESCE(%s, '[]'))
            ON CONFLICT (user_id, source_type, source_id) DO UPDATE SET
                hidden = excluded.hidden,
                tags_json = COALESCE(%s, studio_material_preferences.tags_json),
                updated_at = CURRENT_TIMESTAMP
            """,
            (actor_id, source_type, source_id, 1 if hidden else 0, tags_json, tags_json),
        )
        return
    if clear_group:
        # 显式清除 override：COALESCE 保留语义做不到，只能置回 NULL。
        conn.execute(
            """
            INSERT INTO studio_material_preferences (
                user_id, source_type, source_id, hidden, tags_json
            ) VALUES (%s, %s, %s, %s, COALESCE(%s, '[]'))
            ON CONFLICT (user_id, source_type, source_id) DO UPDATE SET
                group_override = NULL,
                hidden = excluded.hidden,
                tags_json = COALESCE(%s, studio_material_preferences.tags_json),
                updated_at = CURRENT_TIMESTAMP
            """,
            (actor_id, source_type, source_id, 1 if hidden else 0, tags_json, tags_json),
        )
        return
    conn.execute(
        """
        INSERT INTO studio_material_preferences (
            user_id, source_type, source_id, title_override, group_override, hidden, tags_json
        ) VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, '[]'))
        ON CONFLICT (user_id, source_type, source_id) DO UPDATE SET
            title_override = COALESCE(excluded.title_override,
                                      studio_material_preferences.title_override),
            group_override = COALESCE(excluded.group_override,
                                      studio_material_preferences.group_override),
            hidden = excluded.hidden,
            tags_json = COALESCE(%s, studio_material_preferences.tags_json),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            actor_id,
            source_type,
            source_id,
            title,
            group,
            1 if hidden else 0,
            tags_json,
            tags_json,
        ),
    )


def _content_matches(media_type: MaterialMediaType, content: bytes) -> bool:
    if media_type == "image":
        return content.startswith(b"\x89PNG\r\n\x1a\n") or content.startswith(b"\xff\xd8\xff")
    if media_type == "audio":
        return bool(content)
    return len(content) >= 12 and content[4:8] == b"ftyp"


def _audio_content_matches_suffix(content: bytes, suffix: str) -> bool:
    """Reject playlists and mislabeled containers before invoking media tools."""
    if suffix == ".mp3":
        return content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
        )
    if suffix == ".wav":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WAVE"
    if suffix == ".m4a":
        return len(content) >= 12 and content[4:8] == b"ftyp"
    if suffix == ".aac":
        return len(content) >= 2 and content[0] == 0xFF and content[1] & 0xF6 == 0xF0
    if suffix == ".flac":
        return content.startswith(b"fLaC")
    if suffix in {".ogg", ".opus"}:
        return content.startswith(b"OggS")
    if suffix in {".wma", ".wmv"}:
        return content.startswith(
            b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"
        )
    if suffix in {".aiff", ".aif"}:
        return (
            len(content) >= 12
            and content.startswith(b"FORM")
            and content[8:12] in {b"AIFF", b"AIFC"}
        )
    if suffix == ".amr":
        return content.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
    return False
