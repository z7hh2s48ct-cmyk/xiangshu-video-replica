"""HTTP routes for durable viral-video project imports."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection
from app.media import (
    DURATION_ROUNDING_TOLERANCE_SECONDS,
    MAX_DURATION_SECONDS,
    MAX_UPLOAD_BYTES,
    FFprobeVideoProbe,
    VideoProbe,
    VideoProbeFailed,
    VideoProbeUnavailable,
)
from app.media_routes import get_media_storage
from app.permissions import require_not_auditor
from app.storage import StorageAdapter, StorageBackendUnavailable
from app.viral_import import (
    ViralImportRequest,
    ViralImportTaskResponse,
    enqueue_viral_import_task,
    load_viral_import_task,
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
from app.viral_routes import ViralVideoItem
from app.viral_store import upsert_viral_videos
from app.viral_tikhub import ViralSourceError, ViralVideo

router = APIRouter(prefix="/api/viral", tags=["viral"])
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
    return ViralVideo(
        platform=resolved.platform,
        video_id=resolved.video_id,
        category="链接导入",
        title=resolved.title,
        author=resolved.author,
        author_avatar=None,
        verified=False,
        cover_url=resolved.cover_url,
        duration_ms=resolved.duration_ms,
        likes=0,
        comments=None,
        shares=None,
        collects=None,
        published_at=None,
        published_display=None,
        like_display=None,
        tags=[],
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
    # Resolver audio URLs may point to a video's background music instead of its
    # spoken soundtrack. Always cache the full video so copy extraction uses the
    # audio track embedded in the original upload.
    prefer = "video"

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
            fetcher=UrlFetcher(timeout_seconds=25.0, max_bytes=MAX_UPLOAD_BYTES),
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
