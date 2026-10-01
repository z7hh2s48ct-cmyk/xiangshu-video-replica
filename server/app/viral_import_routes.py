"""HTTP routes for durable viral-video project imports."""

import hashlib
import json
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from app.asr import AsrProviderError, max_inline_audio_bytes, max_inline_audio_seconds
from app.auth import AuthenticatedUser, Database
from app.billing_catalog import SERVICES
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection
from app.media import (
    DURATION_ROUNDING_TOLERANCE_SECONDS,
    MAX_DURATION_SECONDS,
    FFprobeVideoProbe,
    VideoProbe,
    VideoProbeFailed,
    VideoProbeUnavailable,
)
from app.media_routes import get_media_storage
from app.media_tools import (
    MediaToolUnavailable,
    probe_duration_seconds,
    resolve_media_binary,
)
from app.permissions import require_not_auditor
from app.script_from_audio import (
    SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES,
    cached_transcript,
    enqueue_script_from_audio_task,
)
from app.storage import StorageAdapter, StorageBackendUnavailable, StoredObject
from app.viral_import import (
    VIRAL_COPY_CONTENT_TYPE,
    ViralCopyUpload,
    ViralImportRequest,
    ViralImportTaskResponse,
    enqueue_viral_import_task,
    load_viral_import_task,
    register_viral_copy_source,
    require_viral_import_enabled,
    viral_import_task_response,
)
from app.viral_link import (
    ResolvedViralLink,
    ViralLinkError,
    douyidou_link_client_from_settings,
    normalize_supported_link,
    supported_link_platform,
)
from app.viral_media import (
    UrlFetcher,
    ViralMediaDNSUnavailable,
    ViralMediaError,
    ViralMediaPipeline,
)
from app.viral_media_preparation import ViralMediaBusy
from app.viral_routes import (
    VIRAL_COPY_SERVICE,
    ViralCopyBilling,
    ViralVideoItem,
    charge_viral_copy,
)
from app.viral_store import (
    LINK_IMPORT_CATEGORY,
    get_viral_video,
    upsert_viral_videos,
    viral_video_availability,
)
from app.viral_tikhub import ViralSourceError, ViralVideo

router = APIRouter(prefix="/api/viral", tags=["viral"])
logger = logging.getLogger(__name__)
_LINK_RECEIPT_LEASE = timedelta(minutes=2)
# 与上传/拆解预检保持一致：15 秒硬上限 + 舍入容差。
_MAX_LINK_DURATION_SECONDS = MAX_DURATION_SECONDS + DURATION_ROUNDING_TOLERANCE_SECONDS
_MAX_LINK_DURATION_MS = int(_MAX_LINK_DURATION_SECONDS * 1000)


class ViralLinkResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    purpose: Literal["copy", "replica"]


class ViralLinkResolutionResponse(BaseModel):
    item: ViralVideoItem
    import_idempotency_key: str = Field(alias="importIdempotencyKey")


def _link_request_hash(normalized_url: str, purpose: str) -> str:
    payload = json.dumps(
        {"purpose": purpose, "url": normalized_url},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _claim_link_receipt(
    conn: BusinessConnection,
    *,
    owner_user_id: str,
    idempotency_key: str,
    normalized_url: str,
    purpose: str,
) -> tuple[Any, bool]:
    request_hash = _link_request_hash(normalized_url, purpose)
    receipt_id = str(uuid4())
    lease_owner = str(uuid4())
    lease_expires_at = (datetime.now(UTC) + _LINK_RECEIPT_LEASE).isoformat()
    conn.execute(
        """
        INSERT INTO viral_link_resolution_receipts (
            id, owner_user_id, idempotency_key, normalized_url, purpose,
            request_hash, status, lease_owner, lease_expires_at
        ) VALUES (%s, %s, %s, %s, %s, %s, 'PREPARED', %s, %s)
        ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING
        """,
        (
            receipt_id,
            owner_user_id,
            idempotency_key,
            normalized_url,
            purpose,
            request_hash,
            lease_owner,
            lease_expires_at,
        ),
    )
    row = conn.execute(
        """SELECT * FROM viral_link_resolution_receipts
        WHERE owner_user_id = %s AND idempotency_key = %s""",
        (owner_user_id, idempotency_key),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail={"code": "VIRAL_LINK_RECEIPT_CONFLICT"})
    if str(row["request_hash"]) != request_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VIRAL_LINK_IDEMPOTENCY_CONFLICT",
                "message": "幂等键已用于不同的视频链接请求。",
            },
        )
    claimed = str(row["id"]) == receipt_id
    if not claimed and str(row["status"]) == "PREPARED":
        claimed = (
            conn.execute(
                """
                UPDATE viral_link_resolution_receipts
                SET lease_owner = %s, lease_expires_at = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'PREPARED'
                  AND lease_expires_at::timestamptz < now()
                """,
                (lease_owner, lease_expires_at, row["id"]),
            ).rowcount
            == 1
        )
    elif not claimed and str(row["status"]) == "REQUEST_SENT":
        conn.execute(
            """
            UPDATE viral_link_resolution_receipts
            SET status = 'UNCERTAIN', completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'REQUEST_SENT'
              AND lease_expires_at::timestamptz < now()
            """,
            (row["id"],),
        )
    current = conn.execute(
        "SELECT * FROM viral_link_resolution_receipts WHERE id = %s", (row["id"],)
    ).fetchone()
    return current, claimed


def _mark_link_request_sent(conn: BusinessConnection, *, receipt_id: str, lease_owner: str) -> None:
    updated = conn.execute(
        """
        UPDATE viral_link_resolution_receipts
        SET status = 'REQUEST_SENT', updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'PREPARED' AND lease_owner = %s
        """,
        (receipt_id, lease_owner),
    )
    if updated.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VIRAL_LINK_IN_PROGRESS",
                "message": "该视频链接正在解析，请使用原请求稍后重试。",
            },
        )


def _receipt_replay(row: Any) -> ViralLinkResolutionResponse:
    status_value = str(row["status"])
    if status_value == "SUCCEEDED" and row["response_json"]:
        return ViralLinkResolutionResponse.model_validate_json(str(row["response_json"]))
    if status_value == "FAILED_SAFE":
        raise HTTPException(
            status_code=int(row["error_status"] or 422),
            detail={
                "code": str(row["error_code"] or "VIRAL_LINK_PARSE_FAILED"),
                "message": str(row["error_message_redacted"] or "视频链接无法解析。"),
            },
        )
    if status_value == "UNCERTAIN":
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_LINK_SUBMISSION_UNCERTAIN",
                "message": "视频链接解析结果未知，请勿更换幂等键重复调用；请上传 MP4/MOV 文件。",
            },
        )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "VIRAL_LINK_IN_PROGRESS",
            "message": "该视频链接正在解析，请使用原请求稍后重试。",
        },
    )


def _record_link_failure(
    conn: BusinessConnection, *, receipt_id: str, error: ViralLinkError
) -> None:
    status_value = "UNCERTAIN" if error.uncertain else "FAILED_SAFE"
    conn.execute(
        """
        UPDATE viral_link_resolution_receipts
        SET status = %s, error_status = %s, error_code = %s,
            error_message_redacted = %s, completed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'REQUEST_SENT'
        """,
        (status_value, error.status_code, error.code, error.message, receipt_id),
    )


def _resolved_video(resolved: ResolvedViralLink) -> ViralVideo:
    # 链接导入只保证媒体本身可复刻；互动字段随解析上游给的走，缺的留空（`likes`
    # 无「未知」取值，只能写 0），管理端按「字段待补全」提示，不拿 0 当真实互动。
    metadata = resolved.metadata
    return ViralVideo(
        platform=resolved.platform,
        video_id=resolved.video_id,
        category=LINK_IMPORT_CATEGORY,
        title=resolved.title,
        author=resolved.author,
        author_avatar=metadata.author_avatar,
        verified=metadata.verified,
        cover_url=resolved.cover_url,
        duration_ms=resolved.duration_ms,
        likes=metadata.likes or 0,
        comments=metadata.comments,
        shares=metadata.shares,
        collects=metadata.collects,
        published_at=metadata.published_at,
        published_display=None,
        like_display=None,
        tags=list(metadata.tags),
        play_url=resolved.video_url,
        audio_url=resolved.audio_url,
        native={
            "source_description": resolved.source_description,
            "link_resolved": True,
        },
    )


def preflight_resolved_media(
    resolved: ResolvedViralLink, *, purpose: str, storage: StorageAdapter
) -> None:
    # 链接解析已带时长时，先拦截超过 15 秒上限的参考视频，避免无谓下载与后续静默失败；
    # 提取文案（copy）以原视频音轨转写，不受复刻的 15 秒上限约束。
    if purpose != "copy" and resolved.duration_ms > _MAX_LINK_DURATION_MS:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_DURATION_EXCEEDED",
            f"视频时长约 {round(resolved.duration_ms / 1000)} 秒，超过 15 秒上限，"
            "无法复刻；请截取 15 秒以内片段后重试。",
            retryable=False,
        )
    # 文案链路直接缓存解析返回的音频（2026-09-30 拍板：上游音频已可直接用于转写），
    # 不再为抽口播强制下载整条视频；复刻仍以完整视频为准。解析未带音频地址时管线
    # 自动回落到视频下载（见 viral_media._resolve_kind）。
    prefer = "audio" if purpose == "copy" else "video"

    def validate(content: Path, kind: str, content_type: str | None) -> None:
        validate_resolved_media_content(
            content,
            kind=kind,
            content_type=content_type,
            probe=FFprobeVideoProbe(),
            purpose=purpose,
        )

    try:
        ViralMediaPipeline(
            client=None,
            storage=storage,
            # 2026-09-30 拍板：链接下载不再套用上传体的 50MB 上限（长视频/长音频照常
            # 拉取），25 秒超时保留；UrlFetcher 自带的 512MB 防滥用硬顶仍在。
            fetcher=UrlFetcher(timeout_seconds=25.0),
            validator=validate,
            shared=True,
        ).fetch(_resolved_video(resolved), prefer=prefer)
    except ViralLinkError:
        raise
    except ViralMediaBusy as exc:
        raise ViralLinkError(
            503,
            "VIRAL_MEDIA_PREPARATION_BUSY",
            "该视频素材正在准备中，请稍后重试。",
            retryable=True,
        ) from exc
    except ViralMediaDNSUnavailable as exc:
        raise ViralLinkError(
            503,
            "VIRAL_LINK_MEDIA_DNS_UNAVAILABLE",
            "当前网络无法获取视频媒体地址，请检查代理或 DNS 设置后重试。",
            retryable=False,
        ) from exc
    except ViralMediaError as exc:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_INVALID",
            "链接媒体不可用，请上传 MP4 或 MOV 文件。",
            retryable=False,
        ) from exc
    except (ViralSourceError, StorageBackendUnavailable) as exc:
        raise ViralLinkError(
            503,
            "VIRAL_MEDIA_PREPARATION_FAILED",
            "云端素材准备暂未完成，请稍后重试。",
            retryable=True,
        ) from exc


def validate_resolved_media_content(
    content: bytes | Path,
    *,
    kind: str,
    content_type: str | None,
    probe: VideoProbe,
    purpose: str,
) -> None:
    source_path = content if isinstance(content, Path) else None
    if source_path is not None:
        with source_path.open("rb") as source:
            content = source.read(12)
    content = cast(bytes, content)
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    expected_prefix = "audio/" if kind == "audio" else "video/"
    mp4_container = len(content) >= 12 and content[4:8] == b"ftyp"
    if kind == "audio":
        magic_valid = (
            mp4_container
            or content.startswith(b"ID3")
            or (len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0)
        )
    else:
        magic_valid = mp4_container
    if not normalized_type.startswith(expected_prefix) or not magic_valid:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_INVALID",
            "链接媒体格式无效，请上传 MP4 或 MOV 文件。",
            retryable=False,
        )
    try:
        filename = "source.m4a" if mp4_container else "source.mp3"
        metadata = (
            cast(FFprobeVideoProbe, probe).probe_file(source_path)
            if source_path is not None
            else probe.probe(content, filename=filename if kind == "audio" else "source.mp4")
        )
    except VideoProbeUnavailable as exc:
        raise ViralLinkError(
            503,
            "VIRAL_LINK_MEDIA_PROBE_UNAVAILABLE",
            "视频检测服务暂不可用，请上传 MP4 或 MOV 文件。",
            retryable=False,
        ) from exc
    except VideoProbeFailed as exc:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_INVALID",
            "链接媒体无法播放，请上传 MP4 或 MOV 文件。",
            retryable=False,
        ) from exc
    if metadata.duration_seconds <= 0:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_INVALID",
            "链接媒体无法播放，请上传 MP4 或 MOV 文件。",
            retryable=False,
        )
    # 仅复刻链路的视频受 15 秒上限约束；提取文案（copy）与口播声音样本（audio）
    # 时长可较长，不在此拦截。
    if (
        purpose != "copy"
        and kind == "video"
        and metadata.duration_seconds > _MAX_LINK_DURATION_SECONDS
    ):
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_DURATION_EXCEEDED",
            f"视频实测时长 {round(metadata.duration_seconds)} 秒，超过 15 秒上限，"
            "无法复刻；请截取 15 秒以内片段后重试。",
            retryable=False,
        )


@router.post(
    "/link-resolutions",
    response_model=ViralLinkResolutionResponse,
    responses={409: {}, 410: {}, 502: {}, 503: {}, 504: {}},
)
def resolve_viral_link(
    request: ViralLinkResolutionRequest,
    db: BusinessDbDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ViralLinkResolutionResponse:
    key = idempotency_key.strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_LINK_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    try:
        normalized_url = normalize_supported_link(request.url)
        with db.write() as (conn, actor):
            with conn:
                require_not_auditor(
                    conn,
                    actor=actor,
                    action="viral.link.resolve",
                    entity_type="viral_link",
                    entity_id=supported_link_platform(normalized_url),
                )
                receipt, created = _claim_link_receipt(
                    conn,
                    owner_user_id=actor.id,
                    idempotency_key=key,
                    normalized_url=normalized_url,
                    purpose=request.purpose,
                )
            if created:
                with conn:
                    resolver = douyidou_link_client_from_settings(conn)
                    from app.usage_billing import accept_operation

                    accept_operation(
                        conn,
                        user_id=actor.id,
                        service="link_resolution",
                        source_id=str(receipt["id"]),
                        units=1,
                    )
        if not created:
            return _receipt_replay(receipt)
        with db.write() as (conn, sending_actor):
            with conn:
                if sending_actor.id != actor.id:
                    raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
                _mark_link_request_sent(
                    conn,
                    receipt_id=str(receipt["id"]),
                    lease_owner=str(receipt["lease_owner"]),
                )
                from app.usage_billing import begin_source_attempt

                begin_source_attempt(conn, str(receipt["id"]))
        resolved = resolver.resolve(normalized_url, purpose=request.purpose)
        with db.write() as (conn, _cost_actor):
            from app.usage_billing import complete_source_attempt

            complete_source_attempt(conn, str(receipt["id"]), usage=1)
        with db.write() as (conn, media_actor):
            if media_actor.id != actor.id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            storage = get_media_storage(conn)
        preflight_resolved_media(resolved, purpose=request.purpose, storage=storage)
    except ViralLinkError as exc:
        if "receipt" in locals():
            with db.write() as (conn, _actor):
                with conn:
                    _record_link_failure(conn, receipt_id=str(receipt["id"]), error=exc)
                    from app.usage_billing import complete_source_attempt, finish_source

                    complete_source_attempt(conn, str(receipt["id"]), usage=None)
                    finish_source(conn, str(receipt["id"]), units=0, succeeded=False)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    video = _resolved_video(resolved)
    import_key = (
        "viral-link-"
        + hashlib.sha256(
            f"{actor.id}:{video.platform}:{video.video_id}:{request.purpose}".encode()
        ).hexdigest()
    )
    response = ViralLinkResolutionResponse(
        item=ViralVideoItem(**video.to_client_dict()),
        importIdempotencyKey=import_key,
    )
    with db.write() as (conn, completed_actor):
        with conn:
            if completed_actor.id != actor.id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            updated = conn.execute(
                """
                UPDATE viral_link_resolution_receipts
                SET status = 'SUCCEEDED', response_json = %s,
                    completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'REQUEST_SENT' AND lease_owner = %s
                """,
                (
                    response.model_dump_json(by_alias=True),
                    receipt["id"],
                    receipt["lease_owner"],
                ),
            )
            if updated.rowcount != 1:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "VIRAL_LINK_SUBMISSION_UNCERTAIN",
                        "message": "视频链接解析结果未知，请上传 MP4/MOV 文件。",
                    },
                )
            from app.usage_billing import finish_source

            finish_source(conn, str(receipt["id"]), units=1, succeeded=True)
            upsert_viral_videos(conn, [video], commit=False)
    return response


@router.post(
    "/import-tasks",
    response_model=ViralImportTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
@router.post(
    "/videos/import-tasks",
    response_model=ViralImportTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_viral_import_task(
    request: ViralImportRequest,
    db: BusinessDbDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ViralImportTaskResponse:
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_IMPORT_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    with db.write() as (conn, actor):
        with conn:
            row = enqueue_viral_import_task(conn, actor=actor, request=request, idempotency_key=key)
        return viral_import_task_response(row)


@router.get("/import-tasks/{task_id}", response_model=ViralImportTaskResponse)
def read_viral_import_task(
    task_id: str, conn: Database, actor: AuthenticatedUser
) -> ViralImportTaskResponse:
    row = load_viral_import_task(conn, task_id)
    if str(row["owner_user_id"]) != actor.id:
        raise HTTPException(
            status_code=404,
            detail={"code": "VIRAL_IMPORT_TASK_NOT_FOUND", "message": "导入任务不存在。"},
        )
    return viral_import_task_response(row)


# ---------------------------------------------------------------- 文案（本地抽音轨）

_COPY_PLATFORMS = ("douyin", "wechat_channels", "xiaohongshu")
_VIRAL_COPY_OBJECT_PREFIX = "viral/copy"
# 与工作台上传链路同一科目：ASR 按秒计价（billing_catalog.SERVICES）。
_COPY_ASR_SERVICE = "asr"


class ViralCopyExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 共享缓存命中：直接带文案，不必让用户白等一次上传，但配送同样计费（已购则复用）。
    text: str | None = None
    updated_at: str | None = Field(default=None, alias="updatedAt")
    billing: ViralCopyBilling | None = None
    # 未命中：客户端按 taskId 轮询转写；projectId/sourceAssetId 供文案工坊续接草稿。
    project_id: str | None = Field(default=None, alias="projectId")
    source_asset_id: str | None = Field(default=None, alias="sourceAssetId")
    task_id: str | None = Field(default=None, alias="taskId")


def _require_available_viral_video(
    conn: BusinessConnection, *, platform: str, video_id: str
) -> ViralVideo:
    video = get_viral_video(conn, platform=platform, video_id=video_id)
    if video is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "VIRAL_VIDEO_NOT_FOUND", "message": "该爆款视频不存在。"},
        )
    if viral_video_availability(conn, platform=platform, video_id=video_id) != "available":
        raise HTTPException(
            status_code=409,
            detail={"code": "VIRAL_VIDEO_UNAVAILABLE", "message": "该爆款视频当前不可用于创作。"},
        )
    return video


def _viral_copy_object_key(owner_user_id: str, idempotency_key: str) -> str:
    """按 (用户, 幂等键) 定址，重放覆盖同一对象而不是留下孤儿.

    同一把幂等键上的请求体恒定——复用它换不同字节会先被 ``request_hash`` 挡下，
    所以覆盖写不会改变任何已登记资产的内容。
    """
    digest = hashlib.sha256(f"{owner_user_id}:{idempotency_key}".encode()).hexdigest()
    return f"{_VIRAL_COPY_OBJECT_PREFIX}/{digest}.m4a"


async def _read_viral_copy_audio(file: UploadFile, *, inline_max_bytes: int | None) -> bytes:
    if file.size is not None and file.size > SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES:
        raise _viral_copy_too_large()
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_COPY_AUDIO_EMPTY",
                "message": "上传的音轨为空，请在客户端重新提取后再试。",
            },
        )
    if len(content) > SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES:
        raise _viral_copy_too_large()
    # 抽取端固定产出 M4A（单声道 16kHz AAC，见 client/src-tauri/src/viral_audio.rs），
    # provider 的 Flash 请求也写死 format=m4a，所以这里只认 MP4 容器。
    if len(content) < 12 or content[4:8] != b"ftyp":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_COPY_AUDIO_INVALID",
                "message": "音轨格式无效，请在客户端重新提取后再试。",
            },
        )
    if inline_max_bytes is not None and len(content) > inline_max_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "VIRAL_COPY_AUDIO_TOO_LARGE",
                "message": (
                    f"音轨超过本地直传上限（{inline_max_bytes // (1024 * 1024)} MB），"
                    "请截取更短的片段后重试。"
                ),
            },
        )
    return content


def _viral_copy_too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={"code": "VIRAL_COPY_AUDIO_TOO_LARGE", "message": "音轨文件过大，请压缩后重试。"},
    )


def _probe_viral_copy_duration(content: bytes) -> float:
    try:
        ffprobe_path = resolve_media_binary("ffprobe")
    except MediaToolUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "VIRAL_COPY_MEDIA_TOOL_MISSING", "message": str(exc)},
        ) from exc
    with tempfile.TemporaryDirectory(prefix="viral-copy-") as tmp_dir:
        media_path = Path(tmp_dir) / "copy-audio.m4a"
        media_path.write_bytes(content)
        duration = probe_duration_seconds(ffprobe_path, media_path)
    if duration is None or duration <= 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_COPY_AUDIO_INVALID",
                "message": "音轨无法解析，请在客户端重新提取后再试。",
            },
        )
    return duration


def _validate_viral_copy_duration(
    *, duration: float, video: ViralVideo, inline_max_seconds: float | None
) -> None:
    """时长校验：先与内容池登记的时长对齐，再按直传上限拦截.

    内容池的时长是服务端按 platform+videoId 从平台取回的，而共享文案缓存是跨用户
    复用的，所以必须确认上传的音轨确实属于这支视频：一条随便什么录音都能写进别人的
    文案区，是这里唯一能廉价设下的门槛（缓存命中不经过本校验，命中的是各自链路上
    已核对过的结果）。
    """
    expected = video.duration_ms / 1000 if video.duration_ms > 0 else None
    if expected is not None and not (expected * 0.75 - 3 <= duration <= expected * 1.25 + 3):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_COPY_AUDIO_DURATION_MISMATCH",
                "message": "音轨时长与该视频不符，请重新缓存该视频后再提取文案。",
            },
        )
    if inline_max_seconds is not None and duration > inline_max_seconds:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_COPY_AUDIO_TOO_LONG",
                "message": (
                    f"音轨时长约 {round(duration)} 秒，超过本地直传上限 "
                    f"{round(inline_max_seconds)} 秒，无法转写。"
                ),
            },
        )


def _inline_audio_limits(
    conn: BusinessConnection, storage: StorageAdapter
) -> tuple[int | None, float | None]:
    """本地盘走 base64 直传时才有大小/时长上限；云存储走签名 URL，不受其约束."""
    if storage.provider != "local":
        return None, None
    try:
        return max_inline_audio_bytes(), max_inline_audio_seconds(conn)
    except AsrProviderError as exc:
        logger.warning("viral copy ASR limits unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_COPY_SERVICE_UNAVAILABLE",
                "message": "语音转写服务暂不可用，请联系管理员检查配置。",
            },
        ) from exc


def _store_viral_copy_audio(storage: StorageAdapter, *, key: str, content: bytes) -> StoredObject:
    try:
        return storage.put_object(key, content, content_type=VIRAL_COPY_CONTENT_TYPE)
    except StorageBackendUnavailable as exc:
        logger.warning("viral copy upload failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_COPY_STORAGE_UNAVAILABLE",
                "message": "素材存储暂不可用，请稍后重试。",
            },
        ) from exc


@router.post(
    "/videos/copy",
    response_model=ViralCopyExtractionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def extract_viral_video_copy(
    response: Response,
    db: BusinessDbDep,
    platform: Annotated[str, Form()],
    video_id: Annotated[str, Form(alias="videoId")],
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ViralCopyExtractionResponse:
    """客户端本地抽出的音轨 → 上传 → ASR 转写 → 回填（决策 #15 / #18）.

    这是「停用采集」的前提：文案链路不再需要平台留存的媒体归档。客户端上传的是它
    自己从**本地缓存的原视频**里抽出的单声道 m4a，服务端只做校验、登记与转写编排。

    计费与工作台上传链路同一科目（``asr`` 按秒）：预留写在任务入队事务里，结算与
    清理都交给既有 Worker。共享文案缓存命中时直接返回文案——不建任务、不预留转写费，
    但交付本身照样扣一次「获取文案」费（``viral_copy``，同账号同视频只扣一次），
    否则这条秒回就成了绕开 ``/videos/copy/claim`` 的免费旁路。

    入库走既有 ``script_from_audio_tasks`` 租约体系（用户 2026-09-23 拍板方案 a）：
    断点续跑、``SUBMISSION_UNCERTAIN`` 不确定态、按秒预留结算与 ``viral_script_cache``
    的历史回溯都由该链路原样提供，这里不另开一条轻量写入路径。
    """
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_COPY_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    if platform not in _COPY_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="viral.copy",
            entity_type="viral_video",
            entity_id=f"{platform}:{video_id}",
        )
        # 与既有导入共用管理端的「暂停导入」开关：对用户而言这是同一个动作
        # （提取文案原本就走导入链路），暂停时不该只剩这条路还能用。
        require_viral_import_enabled(conn)
        video = _require_available_viral_video(conn, platform=platform, video_id=video_id)
        # 共享缓存命中：不必让用户白等一次上传，但「秒回」不等于免费——这条文案的
        # 交付同样要扣一次「获取文案」费（本账号已购则复用）。未命中才走上传转写。
        hit = cached_transcript(conn, platform=platform, video_id=video_id)
        if hit is not None:
            charged, deduped = charge_viral_copy(
                conn, actor=actor, platform=platform, video_id=video_id
            )
            response.status_code = status.HTTP_200_OK
            return ViralCopyExtractionResponse(
                text=hit.result.text,
                updatedAt=hit.updated_at,
                billing=ViralCopyBilling(
                    charged=charged,
                    unit=SERVICES[VIRAL_COPY_SERVICE].unit,
                    deduped=deduped,
                ),
            )
        storage = get_media_storage(conn)
        inline_max_bytes, inline_max_seconds = _inline_audio_limits(conn, storage)
        owner_user_id = actor.id
    content = await _read_viral_copy_audio(file, inline_max_bytes=inline_max_bytes)
    duration = _probe_viral_copy_duration(content)
    _validate_viral_copy_duration(
        duration=duration, video=video, inline_max_seconds=inline_max_seconds
    )
    stored = _store_viral_copy_audio(
        storage, key=_viral_copy_object_key(owner_user_id, key), content=content
    )
    with db.write() as (conn, actor):
        with conn:
            # 上传体在两个事务之间过手：会话若被换掉，素材不能落到新身份名下。
            if actor.id != owner_user_id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            # 循环导入：viral_search_routes 反向依赖 viral_routes 的 ViralVideoItem。
            from app.viral_search_routes import require_priced_viral_service

            require_priced_viral_service(conn, _COPY_ASR_SERVICE, error_code="VIRAL_COPY_UNPRICED")
            source = register_viral_copy_source(
                conn,
                actor=actor,
                video=video,
                upload=ViralCopyUpload(stored=stored, duration_seconds=duration),
                idempotency_key=key,
            )
            row = enqueue_script_from_audio_task(
                conn,
                actor=actor,
                project_id=source.project_id,
                source_asset_id=source.asset_id,
                idempotency_key=key,
            )
    return ViralCopyExtractionResponse(
        projectId=source.project_id,
        sourceAssetId=source.asset_id,
        taskId=str(row["id"]),
    )
