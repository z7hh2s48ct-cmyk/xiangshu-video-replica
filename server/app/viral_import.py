"""Durable import of a cached viral video into a user's project."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import content_store
from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.permissions import insert_audit, require_not_auditor, require_project_access
from app.storage import (
    StorageAdapter,
    StoredObject,
    require_storage_match,
    storage_object_ref_from_uri,
)
from app.viral_media import ViralMediaPipeline, ViralMediaResult
from app.viral_store import get_viral_video, viral_video_availability
from app.viral_tikhub import ViralSourceUnavailable, ViralVideo, viral_source_client_from_settings

logger = logging.getLogger(__name__)
VIRAL_IMPORT_LEASE_MINUTES = 20


class ViralImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels", "xiaohongshu"]
    video_id: str = Field(alias="videoId", min_length=1, max_length=256)
    purpose: Literal["copy", "replica"]
    project_id: str | None = Field(default=None, alias="projectId", max_length=128)


class ViralImportTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    platform: str
    video_id: str = Field(alias="videoId")
    purpose: str
    project_id: str = Field(alias="projectId")
    source_asset_id: str | None = Field(alias="sourceAssetId")
    media_kind: str | None = Field(alias="mediaKind")
    can_transcribe: bool = Field(alias="canTranscribe")
    can_analyze: bool = Field(alias="canAnalyze")
    error_code: str | None = Field(alias="errorCode")
    error_message: str | None = Field(alias="errorMessage")
    retryable: bool
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")


class ViralImportError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})
        self.retryable = retryable


def viral_import_task_response(row: sqlite3.Row) -> ViralImportTaskResponse:
    result = json.loads(str(row["result_json"])) if row["result_json"] else {}
    media_kind = result.get("mediaKind")
    return ViralImportTaskResponse(
        id=str(row["id"]),
        status=str(row["status"]),
        platform=str(row["platform"]),
        videoId=str(row["video_id"]),
        purpose=str(row["purpose"]),
        projectId=str(row["project_id"]),
        sourceAssetId=(str(row["source_asset_id"]) if row["source_asset_id"] else None),
        mediaKind=str(media_kind) if media_kind else None,
        canTranscribe=bool(result.get("canTranscribe", False)),
        canAnalyze=bool(result.get("canAnalyze", False)),
        errorCode=str(row["error_code"]) if row["error_code"] else None,
        errorMessage=(
            str(row["error_message_redacted"]) if row["error_message_redacted"] else None
        ),
        retryable=bool(row["retryable"]),
        createdAt=str(row["created_at"]),
        updatedAt=str(row["updated_at"]),
    )


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = True,
) -> ViralImportError:
    return ViralImportError(status_code, code, message, retryable=retryable)


def _request_hash(payload: dict[str, str | None]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def require_viral_import_enabled(conn: BusinessConnection) -> None:
    control = conn.execute(
        "SELECT import_enabled FROM viral_runtime_controls WHERE id = 1"
    ).fetchone()
    if control is None or not bool(control["import_enabled"]):
        raise _error(503, "VIRAL_IMPORT_DISABLED", "爆款视频导入已暂停，请稍后重试。")


def enqueue_viral_import_task(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    request: ViralImportRequest,
    idempotency_key: str,
) -> sqlite3.Row:
    require_viral_import_enabled(conn)
    require_not_auditor(
        conn,
        actor=actor,
        action="viral.import",
        entity_type="viral_video",
        entity_id=f"{request.platform}:{request.video_id}",
    )
    video = get_viral_video(conn, platform=request.platform, video_id=request.video_id)
    if video is None:
        raise _error(404, "VIRAL_VIDEO_NOT_FOUND", "爆款视频不存在或已下架。")
    if (
        viral_video_availability(conn, platform=request.platform, video_id=request.video_id)
        != "available"
    ):
        raise _error(409, "VIRAL_VIDEO_UNAVAILABLE", "该爆款视频当前不可用于创作。")
    if request.purpose == "replica" and not 4_000 <= video.duration_ms <= 15_000:
        raise _error(
            422,
            "VIRAL_IMPORT_DURATION_OUT_OF_RANGE",
            "参考视频需为 4–15 秒，请更换素材后重试。",
            retryable=False,
        )
    request_payload = {
        "platform": request.platform,
        "videoId": request.video_id,
        "purpose": request.purpose,
        "projectId": request.project_id,
    }
    request_hash = _request_hash(request_payload)
    replay = conn.execute(
        "SELECT * FROM viral_import_tasks WHERE owner_user_id = %s AND idempotency_key = %s",
        (actor.id, idempotency_key),
    ).fetchone()
    if replay is not None:
        if str(replay["request_hash"]) != request_hash:
            raise _error(409, "VIRAL_IMPORT_IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求。")
        if str(replay["status"]) == "FAILED" and bool(replay["retryable"]):
            conn.execute(
                """
                UPDATE viral_import_tasks SET status = 'PENDING', retryable = 0,
                    error_code = NULL, error_message_redacted = NULL,
                    locked_by = NULL, locked_until = NULL, completed_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'FAILED' AND retryable = 1
                """,
                (replay["id"],),
            )
            replay = conn.execute(
                "SELECT * FROM viral_import_tasks WHERE id = %s", (replay["id"],)
            ).fetchone()
            if replay is None:
                raise _error(409, "VIRAL_IMPORT_ENQUEUE_CONFLICT", "导入任务状态已变化。")
        return cast(sqlite3.Row, replay)

    created_project = request.project_id is None
    project_id = request.project_id or str(uuid4())
    if created_project:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            (project_id, actor.id, (video.title.strip() or "爆款视频复刻")[:120]),
        )
    else:
        project = require_project_access(
            conn, actor=actor, project_id=project_id, action="viral.import"
        )
        if str(project["owner_user_id"]) != actor.id:
            raise _error(403, "VIRAL_IMPORT_PROJECT_FORBIDDEN", "只能导入到当前用户的项目。")

    task_id = str(uuid4())
    inserted = conn.execute(
        """
        INSERT INTO viral_import_tasks (
            id, owner_user_id, project_id, platform, video_id, purpose,
            idempotency_key, request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING
        """,
        (
            task_id,
            actor.id,
            project_id,
            request.platform,
            request.video_id,
            request.purpose,
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    if inserted.rowcount == 0:
        if created_project:
            conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        replay = conn.execute(
            "SELECT * FROM viral_import_tasks WHERE owner_user_id = %s AND idempotency_key = %s",
            (actor.id, idempotency_key),
        ).fetchone()
        if replay is None:
            raise _error(409, "VIRAL_IMPORT_ENQUEUE_CONFLICT", "导入任务状态已变化，请重试。")
        if str(replay["request_hash"]) != request_hash:
            raise _error(409, "VIRAL_IMPORT_IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求。")
        return cast(sqlite3.Row, replay)
    row = conn.execute("SELECT * FROM viral_import_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        raise _error(409, "VIRAL_IMPORT_ENQUEUE_CONFLICT", "导入任务状态已变化，请重试。")
    insert_audit(
        conn,
        actor=actor,
        action="viral.import_enqueued",
        entity_type="viral_import_task",
        entity_id=task_id,
        metadata={"project_id": project_id, "platform": request.platform},
    )
    return cast(sqlite3.Row, row)


VIRAL_COPY_CONTENT_TYPE = "audio/mp4"


@dataclass(frozen=True)
class ViralCopyUpload:
    """客户端本地抽出的音轨（已落主存储）与它的实测时长。"""

    stored: StoredObject
    duration_seconds: float


@dataclass(frozen=True)
class ViralCopySource:
    """一次文案提取的素材登记结果：占位项目 + 音轨资产 + 导入任务行。"""

    project_id: str
    asset_id: str
    import_task_id: str


def viral_copy_request_hash(*, platform: str, video_id: str, size_bytes: int, sha256: str) -> str:
    return _request_hash(
        {
            "platform": platform,
            "videoId": video_id,
            "purpose": "copy",
            "sizeBytes": str(size_bytes),
            "sha256": sha256,
        }
    )


def register_viral_copy_source(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    video: ViralVideo,
    upload: ViralCopyUpload,
    idempotency_key: str,
) -> ViralCopySource:
    """登记客户端上传的文案音轨：占位项目 + ``reference_audio`` 资产 + 导入任务.

    爆款文案没有真实项目，而 ``script_from_audio_tasks`` 的 ``project_id`` 既非空
    又带外键，所以与既有 copy 导入一样现造一个占位项目；差别是**每个幂等键一个**
    ——``uq_script_from_audio_tasks_active_project`` 允许一个项目至多一条在制任务，
    共用一个占位项目会让并发的第二次提取被「已有任务在进行」挡掉。

    写一条 ``SUCCEEDED`` 的 ``viral_import_tasks`` 行有两个作用：它是幂等键的持有
    者，也是 ``script_from_audio._viral_source`` 把资产映射回 ``(platform,
    video_id)`` 的唯一凭据。视频身份由服务端按内容池校验后写在这里，客户端上传的
    只有音轨字节本身。
    """
    request_hash = viral_copy_request_hash(
        platform=video.platform,
        video_id=video.video_id,
        size_bytes=upload.stored.size,
        sha256=upload.stored.sha256,
    )
    replay = conn.execute(
        "SELECT * FROM viral_import_tasks WHERE owner_user_id = %s AND idempotency_key = %s",
        (actor.id, idempotency_key),
    ).fetchone()
    if replay is not None:
        return _viral_copy_replay(replay, request_hash=request_hash)

    project_id = str(uuid4())
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        (project_id, actor.id, (video.title.strip() or "爆款视频文案提取")[:120]),
    )
    task_id = str(uuid4())
    inserted = conn.execute(
        """
        INSERT INTO viral_import_tasks (
            id, owner_user_id, project_id, platform, video_id, purpose,
            idempotency_key, request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, 'copy', %s, %s, %s, 'SUCCEEDED')
        ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING
        """,
        (
            task_id,
            actor.id,
            project_id,
            video.platform,
            video.video_id,
            idempotency_key,
            request_hash,
            json.dumps(
                {
                    "platform": video.platform,
                    "videoId": video.video_id,
                    "purpose": "copy",
                    "sizeBytes": upload.stored.size,
                    "sha256": upload.stored.sha256,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )
    if inserted.rowcount == 0:
        # 并发同键提交：让出占位项目，回到赢家的那一行。
        conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        replay = conn.execute(
            "SELECT * FROM viral_import_tasks WHERE owner_user_id = %s AND idempotency_key = %s",
            (actor.id, idempotency_key),
        ).fetchone()
        if replay is None:
            raise _error(409, "VIRAL_COPY_ENQUEUE_CONFLICT", "提取任务状态已变化，请重试。")
        return _viral_copy_replay(replay, request_hash=request_hash)

    asset_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json
        ) VALUES (%s, %s, 'reference_audio', %s, %s, %s, %s, %s, %s)
        """,
        (
            asset_id,
            project_id,
            upload.stored.uri,
            upload.stored.sha256,
            upload.stored.size,
            VIRAL_COPY_CONTENT_TYPE,
            actor.id,
            json.dumps(
                {
                    "duration_seconds": upload.duration_seconds,
                    "platform": video.platform,
                    "video_id": video.video_id,
                    # 服务端写下的凭据：只有本端点登记的 reference_audio 才能作为
                    # 共享文案的身份依据（解析器下发的独立音轨可能是背景音乐）。
                    "viral_copy_upload": True,
                    "import_task_id": task_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )
    conn.execute(
        "UPDATE viral_import_tasks SET source_asset_id = %s, result_json = %s WHERE id = %s",
        (
            asset_id,
            json.dumps(
                {"mediaKind": "audio", "canTranscribe": True, "canAnalyze": False},
                ensure_ascii=False,
                sort_keys=True,
            ),
            task_id,
        ),
    )
    insert_audit(
        conn,
        actor=actor,
        action="viral.copy_enqueued",
        entity_type="viral_import_task",
        entity_id=task_id,
        metadata={
            "project_id": project_id,
            "platform": video.platform,
            "duration_seconds": upload.duration_seconds,
        },
    )
    return ViralCopySource(project_id=project_id, asset_id=asset_id, import_task_id=task_id)


def _viral_copy_replay(row: sqlite3.Row, *, request_hash: str) -> ViralCopySource:
    if str(row["request_hash"]) != request_hash:
        raise _error(409, "VIRAL_COPY_IDEMPOTENCY_CONFLICT", "幂等键已用于不同的提取请求。")
    if str(row["status"]) != "SUCCEEDED" or not row["source_asset_id"]:
        raise _error(409, "VIRAL_COPY_ENQUEUE_CONFLICT", "提取任务状态已变化，请重试。")
    return ViralCopySource(
        project_id=str(row["project_id"]),
        asset_id=str(row["source_asset_id"]),
        import_task_id=str(row["id"]),
    )


def load_viral_import_task(conn: BusinessConnection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM viral_import_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        raise _error(404, "VIRAL_IMPORT_TASK_NOT_FOUND", "导入任务不存在。")
    return cast(sqlite3.Row, row)


@dataclass(frozen=True)
class ViralImportLease:
    id: str
    worker_id: str
    owner_user_id: str
    project_id: str
    platform: str
    video_id: str
    purpose: str
    attempt: int


@dataclass(frozen=True)
class ViralImportWork:
    lease: ViralImportLease
    video: ViralVideo
    client: Any
    storage: StorageAdapter
    prefer: Literal["audio", "video"]


@dataclass(frozen=True)
class ViralImportOutcome:
    stored: StoredObject
    media_kind: Literal["audio", "video"]
    duration_seconds: float | None


def discard_viral_import_outcome(
    storage: StorageAdapter, *, outcome: ViralImportOutcome, actor_id: str
) -> None:
    """Retained for its call sites; no longer deletes anything.

    This used to delete the private project copy an import had just written when
    the asset row failed to commit. Imports no longer write a copy — the outcome
    points at the shared cache object — so there is nothing here that this task
    owns, and deleting it would destroy a video every other import depends on.
    The bytes are released (if ever) through ``content_objects.ref_count``.
    """
    return None


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def acquire_viral_import_task(
    conn: BusinessConnection, *, worker_id: str
) -> ViralImportLease | None:
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=VIRAL_IMPORT_LEASE_MINUTES))
    conn.execute(
        """
        UPDATE viral_import_tasks SET status = 'PENDING', locked_by = NULL,
            locked_until = NULL, error_code = NULL, error_message_redacted = NULL,
            retryable = 0, updated_at = %s
        WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    row = conn.execute(
        """
        UPDATE viral_import_tasks SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s, started_at = COALESCE(started_at, %s),
            updated_at = %s, error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM viral_import_tasks WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING' RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return ViralImportLease(
        id=str(row["id"]),
        worker_id=worker_id,
        owner_user_id=str(row["owner_user_id"]),
        project_id=str(row["project_id"]),
        platform=str(row["platform"]),
        video_id=str(row["video_id"]),
        purpose=str(row["purpose"]),
        attempt=int(row["attempt"]),
    )


def _require_lease(conn: BusinessConnection, lease: ViralImportLease) -> sqlite3.Row:
    now = _time_text(datetime.now(UTC))
    row = conn.execute(
        """
        SELECT * FROM viral_import_tasks
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (lease.id, lease.worker_id, lease.attempt, now),
    ).fetchone()
    if row is None:
        raise _error(409, "VIRAL_IMPORT_LEASE_LOST", "导入任务租约已失效。")
    return cast(sqlite3.Row, row)


def _require_project_owner(conn: BusinessConnection, lease: ViralImportLease) -> None:
    project = conn.execute(
        "SELECT owner_user_id FROM projects WHERE id = %s", (lease.project_id,)
    ).fetchone()
    if project is None or str(project["owner_user_id"]) != lease.owner_user_id:
        raise _error(
            409,
            "VIRAL_IMPORT_PROJECT_CHANGED",
            "目标项目已不存在或归属已变化。",
            retryable=False,
        )


def prepare_viral_import_task(
    conn: BusinessConnection, *, lease: ViralImportLease, storage: StorageAdapter
) -> ViralImportWork:
    _require_lease(conn, lease)
    require_viral_import_enabled(conn)
    _require_project_owner(conn, lease)
    video = get_viral_video(conn, platform=lease.platform, video_id=lease.video_id)
    if video is None:
        raise _error(
            404,
            "VIRAL_VIDEO_NOT_FOUND",
            "爆款视频不存在或已下架。",
            retryable=False,
        )
    if (
        viral_video_availability(conn, platform=lease.platform, video_id=lease.video_id)
        != "available"
    ):
        raise _error(
            409,
            "VIRAL_VIDEO_UNAVAILABLE",
            "该爆款视频当前不可用于创作。",
            retryable=False,
        )
    # A resolver's standalone audio may be background music. The complete video
    # is the authoritative source for both replication and speech extraction.
    prefer: Literal["audio", "video"] = "video"
    try:
        client = viral_source_client_from_settings(conn)
    except ViralSourceUnavailable:
        # Any already cached object remains importable while the source is offline.
        client = None
    return ViralImportWork(
        lease=lease,
        video=video,
        client=client,
        storage=storage,
        prefer=prefer,
    )


def perform_viral_import_task(work: ViralImportWork) -> ViralImportOutcome:
    media: ViralMediaResult = ViralMediaPipeline(
        client=None,
        storage=work.storage,
        shared=True,
        cached_only=True,
    ).fetch(work.video, prefer=work.prefer)
    if media.kind not in {"audio", "video"}:
        raise RuntimeError("viral import returned an unsupported media kind")
    if work.lease.purpose == "replica" and media.kind != "video":
        raise RuntimeError("viral replica import requires video media")
    source = storage_object_ref_from_uri(media.storage_uri)
    require_storage_match(work.storage, source)
    # The project no longer receives its own copy of the video. The media cache
    # already holds exactly one immutable object per (platform, video_id), and
    # every importing project now points at it, so ten imports of one viral
    # video cost one video's worth of bytes instead of ten. Verification
    # therefore re-reads the shared object rather than a copy: the same size and
    # digest checks still apply, they simply have no duplicate left to compare.
    stored = work.storage.head_object(source.key)
    if stored is None:
        raise RuntimeError("viral import source object is missing")
    if (
        stored.size != media.size
        or not stored.sha256
        or (media.sha256 and stored.sha256 != media.sha256)
    ):
        raise RuntimeError("viral import storage verification failed")
    return ViralImportOutcome(
        stored=stored,
        media_kind=cast(Literal["audio", "video"], media.kind),
        duration_seconds=(work.video.duration_ms / 1000 if work.video.duration_ms > 0 else None),
    )


def complete_viral_import_task(
    conn: BusinessConnection, *, lease: ViralImportLease, outcome: ViralImportOutcome
) -> None:
    _require_lease(conn, lease)
    _require_project_owner(conn, lease)
    # Register the shared bytes before pointing an asset at them. `pinned=True`
    # records that the media cache owns this object, not the asset graph: no
    # number of project deletions may force a re-download of a collected video.
    # `deduplicated` is True when an earlier import of the same platform video
    # already registered it, which is the signal that this project stored
    # nothing new.
    content, deduplicated = content_store.retain_content_object(
        conn,
        sha256=outcome.stored.sha256,
        size_bytes=outcome.stored.size,
        content_type=outcome.stored.content_type,
        provider=outcome.stored.provider,
        bucket=outcome.stored.bucket,
        object_key=outcome.stored.key,
        scope="global",
        pinned=True,
    )
    asset_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json, content_object_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            asset_id,
            lease.project_id,
            "reference_audio" if outcome.media_kind == "audio" else "reference_video",
            content.storage_uri,
            outcome.stored.sha256,
            outcome.stored.size,
            outcome.stored.content_type,
            lease.owner_user_id,
            json.dumps(
                {
                    "duration_seconds": outcome.duration_seconds,
                    "platform": lease.platform,
                    "video_id": lease.video_id,
                    "import_task_id": lease.id,
                    "shared_content": True,
                    "content_deduplicated": deduplicated,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            content.id,
        ),
    )
    result = {
        "projectId": lease.project_id,
        "sourceAssetId": asset_id,
        "mediaKind": outcome.media_kind,
        "canTranscribe": True,
        "canAnalyze": outcome.media_kind == "video",
    }
    now = _time_text(datetime.now(UTC))
    updated = conn.execute(
        """
        UPDATE viral_import_tasks SET status = 'SUCCEEDED', source_asset_id = %s,
            result_json = %s, locked_by = NULL, locked_until = NULL, retryable = 0,
            error_code = NULL, error_message_redacted = NULL, completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (
            asset_id,
            json.dumps(result, ensure_ascii=False, sort_keys=True),
            now,
            now,
            lease.id,
            lease.worker_id,
            lease.attempt,
            now,
        ),
    )
    if updated.rowcount != 1:
        raise _error(409, "VIRAL_IMPORT_LEASE_LOST", "导入任务租约已失效。")
    conn.commit()


def fail_viral_import_task(
    conn: BusinessConnection, *, lease: ViralImportLease, cause: Exception
) -> None:
    logger.warning("viral import task %s failed: %s", lease.id, type(cause).__name__)
    now = _time_text(datetime.now(UTC))
    message = "爆款视频导入失败，请稍后重试。"
    error_code = "VIRAL_IMPORT_FAILED"
    retryable = True
    if isinstance(cause, ViralImportError) and isinstance(cause.detail, dict):
        message = str(cause.detail.get("message") or message)
        error_code = str(cause.detail.get("code") or error_code)
        retryable = cause.retryable
    conn.execute(
        """
        UPDATE viral_import_tasks SET status = 'FAILED', locked_by = NULL,
            locked_until = NULL, error_code = %s,
            error_message_redacted = %s, retryable = %s, completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (
            error_code,
            message,
            1 if retryable else 0,
            now,
            now,
            lease.id,
            lease.worker_id,
            lease.attempt,
            now,
        ),
    )
    conn.commit()
