from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from fractions import Fraction
from math import gcd
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from fastapi import HTTPException

from app.analysis import (
    enqueue_analysis_task,
    find_analysis_version_for_asset,
    find_latest_analysis_task,
)
from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.material_thumbs import store_video_thumbnail
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.storage import (
    StorageAdapter,
    require_storage_match,
    storage_object_ref_from_uri,
    store_verified_upload,
)

ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime"}
ALLOWED_SUFFIXES = {".mp4", ".mov"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MIN_DURATION_SECONDS = 4.0
MAX_DURATION_SECONDS = 15.0
DURATION_ROUNDING_TOLERANCE_SECONDS = 0.1
UPLOAD_INTENT_EXPIRES_IN = timedelta(minutes=15)
FFPROBE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None


@dataclass(frozen=True)
class CreatedUploadIntent:
    asset_id: str
    project_id: str
    storage_key: str
    method: str | None
    url: str | None
    headers: dict[str, str]
    expires_at: str | None
    upload_required: bool


@dataclass(frozen=True)
class CompletedUpload:
    asset_id: str
    project_id: str
    status: str
    storage_uri: str
    sha256: str
    size_bytes: int
    content_type: str
    metadata: VideoMetadata
    analysis_task_id: str | None
    analysis_task_status: str | None


@dataclass(frozen=True)
class PreparedUploadCompletion:
    asset_id: str
    project_id: str
    storage_uri: str
    storage_key: str
    content_type: str | None
    expected_size: int | None = None
    expected_sha256: str | None = None
    already_verified: bool = False


@dataclass(frozen=True)
class ProbedUploadCompletion:
    prepared: PreparedUploadCompletion
    storage_uri: str
    sha256: str
    size_bytes: int
    content_type: str
    metadata: VideoMetadata
    # MATERIAL-THUMBS-B：首帧缩略图对象键（探测期已落存储；失败为 None 不阻塞上传）。
    thumbnail_key: str | None = None


class VideoProbe(Protocol):
    def probe(self, content: bytes, *, filename: str) -> VideoMetadata: ...


class VideoProbeUnavailable(RuntimeError):
    pass


class VideoProbeFailed(RuntimeError):
    pass


class FFprobeVideoProbe:
    def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
        with tempfile.TemporaryDirectory(prefix="media-probe-") as directory:
            source = Path(directory) / f"source{Path(filename).suffix.lower()}"
            source.write_bytes(content)
            return self.probe_file(source)

    def probe_file(self, source: Path | str) -> VideoMetadata:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            raise VideoProbeUnavailable("ffprobe is required for video precheck")
        command = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate",
            "-of",
            "json",
            str(source),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=FFPROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise VideoProbeFailed("ffprobe timed out") from exc
        if result.returncode != 0:
            raise VideoProbeFailed("ffprobe could not read video metadata")

        try:
            payload = json.loads(result.stdout)
            duration = float(payload["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VideoProbeFailed("ffprobe returned invalid video metadata") from exc

        stream: dict[str, Any] = next(
            (item for item in payload.get("streams", []) if item.get("codec_type") == "video"), {}
        )
        width, height, fps = None, None, None
        try:
            width, height = int(stream["width"]), int(stream["height"])
            fps = float(Fraction(stream["avg_frame_rate"]))
            if width <= 0 or height <= 0 or fps <= 0:
                raise ValueError("invalid stream metadata")
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            width, height, fps = None, None, None
        return VideoMetadata(duration_seconds=duration, width=width, height=height, fps=fps)


def create_upload_intent(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    project_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    sha256: str | None = None,
    purpose: str = "replica",
) -> CreatedUploadIntent:
    require_not_auditor(
        conn,
        actor=actor,
        action="asset.upload_intent.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="asset.upload_intent.create",
    )
    validate_upload_request(filename=filename, content_type=content_type, size_bytes=size_bytes)

    if sha256:
        reused = reuse_owned_completed_upload(
            conn,
            actor=actor,
            storage=storage,
            project_id=project_id,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        if reused is not None:
            return reused

    asset_id = str(uuid4())
    storage_key = f"projects/{project_id}/uploads/{asset_id}/{Path(filename).name}"
    intent = storage.create_upload_intent(
        storage_key,
        content_type=content_type,
        expires_in=UPLOAD_INTENT_EXPIRES_IN,
        size_bytes=size_bytes,
    )
    storage_uri = f"{storage.provider}://{storage.bucket}/{intent.key}"

    with conn:
        conn.execute(
            """
            INSERT INTO assets (
                id,
                project_id,
                kind,
                storage_uri,
                sha256,
                size_bytes,
                content_type,
                created_by_user_id,
                metadata_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                project_id,
                "reference_video",
                storage_uri,
                "",
                0,
                content_type,
                actor.id,
                json.dumps(
                    {
                        "upload_status": "PENDING",
                        "upload_purpose": purpose,
                        "requested_size_bytes": size_bytes,
                        "requested_sha256": sha256,
                        "intent_expires_at": intent.expires_at.isoformat(),
                        "upload_source_uri": storage_uri,
                    }
                ),
            ),
        )

    write_audit(
        conn,
        actor=actor,
        action="asset.upload_intent.create",
        entity_type="asset",
        entity_id=asset_id,
        metadata={
            "project_id": project_id,
            "storage_key": intent.key,
            "upload_source_uri": storage_uri,
            "intent_expires_at": intent.expires_at.isoformat(),
        },
    )
    return CreatedUploadIntent(
        asset_id=asset_id,
        project_id=project_id,
        storage_key=intent.key,
        method=intent.method,
        url=intent.url,
        headers=intent.headers,
        expires_at=intent.expires_at.isoformat(),
        upload_required=True,
    )


def reuse_owned_completed_upload(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    project_id: str,
    sha256: str,
    size_bytes: int,
) -> CreatedUploadIntent | None:
    source = conn.execute(
        """
        SELECT assets.*
        FROM assets
        JOIN projects ON projects.id = assets.project_id
        WHERE projects.owner_user_id = %s
          AND assets.kind = 'reference_video'
          AND assets.sha256 = %s
          AND assets.size_bytes = %s
          AND assets.size_bytes > 0
        ORDER BY assets.created_at DESC, assets.id DESC
        LIMIT 1
        """,
        (actor.id, sha256, size_bytes),
    ).fetchone()
    if source is None:
        return None
    try:
        reference = storage_object_ref_from_uri(str(source["storage_uri"]))
    except ValueError:
        return None
    if reference.provider != storage.provider or reference.bucket != storage.bucket:
        return None
    stored = storage.head_object(reference.key)
    if stored is None or stored.size != size_bytes:
        # The database can outlive a manually removed/lifecycle-expired object.
        # Only skip the transfer when the original bytes still exist.
        return None

    source_asset_id = str(source["id"])
    asset_id = source_asset_id
    if str(source["project_id"]) != project_id:
        asset_id = str(uuid4())
        reused_metadata = json.loads(str(source["metadata_json"]))
        for field in ("upload_source_uri", "intent_expires_at", "cleanup_object_cursor"):
            reused_metadata.pop(field, None)
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, metadata_json, created_by_user_id
            ) VALUES (%s, %s, 'reference_video', %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                project_id,
                str(source["storage_uri"]),
                str(source["sha256"]),
                int(source["size_bytes"]),
                None if source["content_type"] is None else str(source["content_type"]),
                json.dumps(reused_metadata, sort_keys=True),
                actor.id,
            ),
        )
    conn.execute(
        "UPDATE projects SET status = 'REFERENCE_READY', updated_at = CURRENT_TIMESTAMP "
        "WHERE id = %s",
        (project_id,),
    )
    write_audit(
        conn,
        actor=actor,
        action="asset.upload_deduplicated",
        entity_type="asset",
        entity_id=asset_id,
        metadata={
            "project_id": project_id,
            "source_asset_id": source_asset_id,
            "sha256_prefix": sha256[:12],
        },
    )
    return CreatedUploadIntent(
        asset_id=asset_id,
        project_id=project_id,
        storage_key=reference.key,
        method=None,
        url=None,
        headers={},
        expires_at=None,
        upload_required=False,
    )


def prepare_upload_completion(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    asset_id: str,
) -> PreparedUploadCompletion:
    require_not_auditor(
        conn,
        actor=actor,
        action="asset.upload_complete",
        entity_type="asset",
        entity_id=asset_id,
    )
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="asset.upload_complete",
    )
    if not is_reference_video_asset(row):
        raise media_error(
            409,
            "ASSET_NOT_REFERENCE_VIDEO",
            "Only reference video uploads can be completed here.",
        )
    storage_uri = str(row["storage_uri"])
    upload_metadata = json.loads(str(row["metadata_json"]))
    if upload_metadata.get("upload_status") == "EXPIRED":
        raise media_error(409, "UPLOAD_EXPIRED", "Upload expired; create a new upload intent.")
    expected_size = upload_metadata.get("requested_size_bytes")
    expected_sha256 = upload_metadata.get("requested_sha256")
    if int(row["size_bytes"]) > 0 and str(row["sha256"]):
        expected_size, expected_sha256 = int(row["size_bytes"]), str(row["sha256"])
    return PreparedUploadCompletion(
        asset_id=asset_id,
        project_id=str(row["project_id"]),
        storage_uri=storage_uri,
        storage_key=storage_key_from_uri(storage_uri),
        content_type=None if row["content_type"] is None else str(row["content_type"]),
        expected_size=expected_size if isinstance(expected_size, int) else None,
        expected_sha256=expected_sha256 if isinstance(expected_sha256, str) else None,
        already_verified=(
            "/verified-uploads/" in storage_uri
            and int(row["size_bytes"]) > 0
            and bool(row["sha256"])
        ),
    )


def probe_upload_completion(
    prepared: PreparedUploadCompletion,
    *,
    storage: StorageAdapter,
    probe: VideoProbe,
) -> ProbedUploadCompletion:
    reference = storage_object_ref_from_uri(prepared.storage_uri)
    require_storage_match(storage, reference)
    storage_key = prepared.storage_key
    stored = storage.head_object(storage_key)
    if stored is None:
        raise media_error(
            409,
            "UPLOAD_OBJECT_MISSING",
            "Uploaded object is not available yet. Retry completion after upload finishes.",
        )

    content_type = prepared.content_type or stored.content_type
    validate_upload_request(
        filename=Path(storage_key).name,
        content_type=content_type,
        size_bytes=stored.size,
    )
    if prepared.expected_size is not None and stored.size != prepared.expected_size:
        raise media_error(
            409, "UPLOAD_SIZE_MISMATCH", "Uploaded size differs from the upload intent."
        )
    # Read once with a hard byte budget, then probe/hash/persist exactly those bytes.
    # HEAD metadata alone cannot protect against an overwrite between HEAD and GET.
    chunks = storage.iter_object(storage_key)
    content_buffer = bytearray()
    try:
        for chunk in chunks:
            if len(content_buffer) + len(chunk) > MAX_UPLOAD_BYTES:
                raise media_error(413, "MEDIA_TOO_LARGE", "Video upload must be 50MB or smaller.")
            if len(content_buffer) + len(chunk) > stored.size:
                raise media_error(
                    409, "UPLOAD_SIZE_MISMATCH", "Object changed during verification."
                )
            content_buffer.extend(chunk)
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()
    if len(content_buffer) != stored.size:
        raise media_error(409, "UPLOAD_SIZE_MISMATCH", "Object changed during verification.")
    content = bytes(content_buffer)
    content_sha256 = hashlib.sha256(content).hexdigest()
    if prepared.expected_sha256 and content_sha256 != prepared.expected_sha256.lower():
        raise media_error(409, "UPLOAD_HASH_MISMATCH", "Uploaded content differs from its digest.")
    metadata = probe_video(probe, content, filename=Path(storage_key).name)
    validate_duration(metadata.duration_seconds)
    # This key is never the subject of a client PUT grant. Concurrent completions
    # with different bytes produce different keys; the database picks one below.
    verified = (
        stored
        if prepared.already_verified
        else store_verified_upload(
            storage,
            asset_id=prepared.asset_id,
            source_key=storage_key,
            content=content,
            content_type=content_type,
        )
    )
    # MATERIAL-THUMBS-B：抽帧放在探测期（写事务之外）；失败只损失缩略图。
    thumbnail_key = store_video_thumbnail(storage, verified.key, content)
    return ProbedUploadCompletion(
        prepared=prepared,
        storage_uri=verified.uri,
        sha256=content_sha256,
        size_bytes=stored.size,
        content_type=content_type,
        metadata=metadata,
        thumbnail_key=thumbnail_key,
    )


def persist_upload_completion(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    probed: ProbedUploadCompletion,
    request_id: str | None = None,
) -> CompletedUpload:
    prepared = probed.prepared
    require_not_auditor(
        conn,
        actor=actor,
        action="asset.upload_complete",
        entity_type="asset",
        entity_id=prepared.asset_id,
    )
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=prepared.asset_id,
        action="asset.upload_complete",
    )
    row = conn.execute(
        "SELECT * FROM assets WHERE id = %s FOR UPDATE", (prepared.asset_id,)
    ).fetchone()
    if row is None:
        raise media_error(409, "UPLOAD_STATE_CHANGED", "Upload was removed during verification.")
    metadata = json.loads(str(row["metadata_json"]))
    if probed.thumbnail_key:
        metadata["thumbnail_key"] = probed.thumbnail_key
    if probed.metadata.width and probed.metadata.height and probed.metadata.fps:
        width, height = probed.metadata.width, probed.metadata.height
        divisor = gcd(width, height)
        metadata.update(
            {
                "resolution": f"{width}x{height}",
                "fps": probed.metadata.fps,
                "aspect_ratio": f"{width // divisor}:{height // divisor}",
            }
        )

    if metadata.get("upload_status") == "EXPIRED":
        raise media_error(409, "UPLOAD_EXPIRED", "Upload expired during verification.")
    if (
        not is_reference_video_asset(row)
        or str(row["project_id"]) != prepared.project_id
        or str(row["storage_uri"]) != prepared.storage_uri
    ):
        raise media_error(
            409,
            "UPLOAD_STATE_CHANGED",
            "Upload state changed while the object was being checked; restart completion.",
        )

    analysis_task: sqlite3.Row | None = None
    with conn:
        conn.execute(
            """
            UPDATE assets
            SET
                kind = 'reference_video',
                storage_uri = %s,
                sha256 = %s,
                size_bytes = %s,
                content_type = %s,
                metadata_json = %s
            WHERE id = %s
            """,
            (
                probed.storage_uri,
                probed.sha256,
                probed.size_bytes,
                probed.content_type,
                json.dumps(
                    {
                        **metadata,
                        "duration_seconds": probed.metadata.duration_seconds,
                        "upload_status": "COMPLETE",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                prepared.asset_id,
            ),
        )
        conn.execute(
            "UPDATE projects SET status = %s WHERE id = %s",
            ("REFERENCE_READY", prepared.project_id),
        )
        project_id = prepared.project_id
        analysis_task = find_latest_analysis_task(
            conn,
            project_id=project_id,
            asset_id=prepared.asset_id,
        )
        if (
            analysis_task is None
            and metadata.get("upload_purpose", "replica") == "replica"
            and _automatic_analysis_input_ready(conn, probed.storage_uri)
            and find_analysis_version_for_asset(
                conn,
                project_id=project_id,
                asset_id=prepared.asset_id,
            )
            is None
        ):
            analysis_task, created = enqueue_analysis_task(
                conn,
                project_id=project_id,
                asset_id=prepared.asset_id,
                created_by_user_id=actor.id,
                duration_seconds=probed.metadata.duration_seconds,
                request_id=request_id,
            )
            if created:
                write_audit(
                    conn,
                    actor=actor,
                    action="analysis.task_enqueued",
                    entity_type="analysis_task",
                    entity_id=str(analysis_task["id"]),
                    metadata={
                        "project_id": project_id,
                        "asset_id": prepared.asset_id,
                        "asset_sha256": probed.sha256,
                        "trigger": "asset.upload_complete",
                        "request_id": request_id,
                    },
                    commit=False,
                )
        write_audit(
            conn,
            actor=actor,
            action="asset.upload_complete",
            entity_type="asset",
            entity_id=prepared.asset_id,
            metadata={
                "project_id": prepared.project_id,
                "duration_seconds": probed.metadata.duration_seconds,
            },
            commit=False,
        )
    return CompletedUpload(
        asset_id=prepared.asset_id,
        project_id=prepared.project_id,
        status="uploaded",
        storage_uri=probed.storage_uri,
        sha256=probed.sha256,
        size_bytes=probed.size_bytes,
        content_type=probed.content_type,
        metadata=probed.metadata,
        analysis_task_id=(None if analysis_task is None else str(analysis_task["id"])),
        analysis_task_status=(None if analysis_task is None else str(analysis_task["status"])),
    )


def complete_upload(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    probe: VideoProbe,
    asset_id: str,
    request_id: str | None = None,
) -> CompletedUpload:
    """Compatibility wrapper for non-route callers; external I/O stays outside writes."""
    prepared = prepare_upload_completion(conn, actor=actor, asset_id=asset_id)
    probed = probe_upload_completion(prepared, storage=storage, probe=probe)
    return persist_upload_completion(conn, actor=actor, probed=probed, request_id=request_id)


def _automatic_analysis_input_ready(conn: BusinessConnection, asset_uri: str) -> bool:
    if not asset_uri.startswith("local://"):
        return True
    # The interactive analysis route explains the unavailable prerequisite.
    # Upload completion itself must succeed without reserving a doomed task.
    from app.analysis_routes import get_video_analysis_provider

    try:
        return not get_video_analysis_provider(conn).requires_https_video_url
    except HTTPException:
        return False


def is_reference_video_asset(row: sqlite3.Row) -> bool:
    if str(row["kind"]) == "reference_video":
        return True
    return str(row["kind"]) == "video" and f"/projects/{row['project_id']}/uploads/" in str(
        row["storage_uri"]
    )


def validate_upload_request(*, filename: str, content_type: str, size_bytes: int) -> None:
    suffix = Path(filename).suffix.lower()
    if content_type not in ALLOWED_CONTENT_TYPES or suffix not in ALLOWED_SUFFIXES:
        raise media_error(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "Only MP4 and MOV videos are accepted.",
        )
    if size_bytes > MAX_UPLOAD_BYTES:
        raise media_error(413, "MEDIA_TOO_LARGE", "Video upload must be 50MB or smaller.")
    if size_bytes < 0:
        raise media_error(422, "INVALID_MEDIA_SIZE", "Video size must be non-negative.")


def probe_video(probe: VideoProbe, content: bytes, *, filename: str) -> VideoMetadata:
    try:
        return probe.probe(content, filename=filename)
    except VideoProbeUnavailable as exc:
        raise media_error(
            503,
            "VIDEO_PROBE_UNAVAILABLE",
            "视频检测服务暂不可用，请稍后重试。",
        ) from exc
    except VideoProbeFailed as exc:
        raise media_error(
            422,
            "VIDEO_PROBE_FAILED",
            "Video metadata could not be read.",
        ) from exc


def validate_duration(duration_seconds: float) -> None:
    if (
        duration_seconds < MIN_DURATION_SECONDS - DURATION_ROUNDING_TOLERANCE_SECONDS
        or duration_seconds > MAX_DURATION_SECONDS + DURATION_ROUNDING_TOLERANCE_SECONDS
    ):
        raise media_error(
            422,
            "VIDEO_DURATION_OUT_OF_RANGE",
            f"检测到 {duration_seconds:.2f} 秒；参考视频需为 4–15 秒。",
        )


def storage_key_from_uri(uri: str) -> str:
    return storage_object_ref_from_uri(uri).key


def media_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
