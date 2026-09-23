from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import content_store
from app.analysis import insert_version
from app.auth import CurrentUser, Role
from app.db_portable import BusinessConnection
from app.media import is_reference_video_asset
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

SOURCE_FRAME_CANDIDATES_KIND = "source_frame_candidates"
SOURCE_FRAME_SELECTION_KIND = "source_frame_selection"
SOURCE_FRAME_SCHEMA_VERSION = "b4.source-frame.v2"
SOURCE_FRAME_TIMESTAMPS_SECONDS = (0.5, 1.5, 2.5)
FFMPEG_TIMEOUT_SECONDS = 15
SCENE_BOUNDARY_THRESHOLD = 0.35
MAX_SCENE_BOUNDARY_CANDIDATES = 24
SOURCE_FRAME_TASK_LEASE_MINUTES = 5

logger = logging.getLogger(__name__)


def source_video_duration_seconds(asset: sqlite3.Row) -> float | None:
    try:
        metadata = json.loads(str(asset["metadata_json"] or "{}"))
        duration = metadata.get("duration_seconds") if isinstance(metadata, dict) else None
        if not isinstance(duration, (int, float, str)) or isinstance(duration, bool):
            return None
        value = float(duration)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if value > 0 else None


def adaptive_source_frame_timestamps(duration_seconds: float | None) -> tuple[float, ...]:
    """Include the opening state and representative later frames."""

    if duration_seconds is None or duration_seconds <= 0:
        return SOURCE_FRAME_TIMESTAMPS_SECONDS
    return tuple(round(duration_seconds * ratio, 3) for ratio in (0.0, 0.3, 0.5, 0.7, 0.9))


@dataclass(frozen=True)
class ExtractedSourceFrame:
    timestamp_seconds: float
    image: bytes
    technical_score: float | None = None


class SourceFrameCandidateAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_index: int = Field(ge=0)
    person_count: int = Field(ge=0)
    person_visibility_score: float = Field(ge=0, le=1)
    face_clarity_score: float = Field(ge=0, le=1)
    unobstructed_score: float = Field(ge=0, le=1)
    pose_suitability_score: float = Field(ge=0, le=1)
    motion_blur_detected: bool
    notes: list[str]


class SourceFrameSemanticInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[SourceFrameCandidateAssessment]
    provider: str
    model: str


@dataclass(frozen=True)
class SourceFrameExtractionPlan:
    actor: CurrentUser
    project_id: str
    asset_id: str
    source_storage_uri: str
    requested_timestamps: tuple[float, ...]


@dataclass(frozen=True)
class StoredSourceFrameCandidates:
    candidates: list[dict[str, object]]
    created_assets: list[tuple[str, str]]
    semantic_quality_status: Literal["VERIFIED", "UNAVAILABLE", "NOT_REQUESTED"]
    semantic_provider: str | None
    semantic_model: str | None


@dataclass(frozen=True)
class SourceFrameTaskLease:
    id: str
    created_by_user_id: str
    worker_id: str
    attempt: int


class SourceFrameExtractor(Protocol):
    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        timestamps_seconds: tuple[float, ...],
    ) -> list[ExtractedSourceFrame]: ...


class SourceFrameQualityInspector(Protocol):
    def inspect_source_frame_candidates(
        self,
        frames: list[ExtractedSourceFrame],
    ) -> SourceFrameSemanticInspection: ...


class SourceFrameExtractorUnavailable(RuntimeError):
    pass


class SourceFrameExtractionFailed(RuntimeError):
    pass


class SceneBoundaryDetectionFailed(RuntimeError):
    pass


class FFmpegSceneBoundaryDetector:
    """Return bounded FFmpeg scene-change candidates for model review."""

    def detect(
        self,
        content: bytes,
        *,
        filename: str,
        duration_seconds: float,
    ) -> tuple[float, ...]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SourceFrameExtractorUnavailable("ffmpeg is required for scene detection")

        suffix = Path(filename).suffix.lower()
        with tempfile.TemporaryDirectory(prefix="video-replica-scene-boundary-") as directory:
            video_path = Path(directory) / f"reference{suffix}"
            with video_path.open("wb") as video_file:
                video_file.write(content)
            try:
                result = subprocess.run(
                    [
                        ffmpeg,
                        "-v",
                        "info",
                        "-i",
                        str(video_path),
                        "-vf",
                        f"select=gt(scene\\,{SCENE_BOUNDARY_THRESHOLD}),showinfo",
                        "-an",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    check=False,
                    timeout=FFMPEG_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise SceneBoundaryDetectionFailed("ffmpeg scene detection timed out") from exc
            if result.returncode != 0:
                raise SceneBoundaryDetectionFailed("ffmpeg could not detect scene boundaries")

        candidates = [
            round(float(value), 3)
            for value in re.findall(rb"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr)
            if 0.05 < float(value) < duration_seconds - 0.05
        ]
        unique_candidates = tuple(dict.fromkeys(candidates))
        if len(unique_candidates) <= MAX_SCENE_BOUNDARY_CANDIDATES:
            return unique_candidates
        last_index = len(unique_candidates) - 1
        return tuple(
            unique_candidates[round(index * last_index / (MAX_SCENE_BOUNDARY_CANDIDATES - 1))]
            for index in range(MAX_SCENE_BOUNDARY_CANDIDATES)
        )


class FFmpegSourceFrameExtractor:
    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        timestamps_seconds: tuple[float, ...],
    ) -> list[ExtractedSourceFrame]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SourceFrameExtractorUnavailable("ffmpeg is required for source frame extraction")

        suffix = Path(filename).suffix.lower()
        # NamedTemporaryFile holds a share-none handle for its whole lifetime
        # on Windows, so a separate ffmpeg process cannot open the video at
        # all. Write a real file inside a private temporary directory and
        # close it before invoking ffmpeg; the directory cleanup still
        # removes everything on exit.
        with tempfile.TemporaryDirectory(prefix="video-replica-source-frame-") as directory:
            directory_path = Path(directory)
            video_path = str(directory_path / f"reference{suffix}")
            with open(video_path, "wb") as video_file:
                video_file.write(content)
            image_paths = [
                directory_path / f"frame-{index}.jpg" for index in range(len(timestamps_seconds))
            ]
            grayscale_paths = [
                directory_path / f"frame-{index}.gray" for index in range(len(timestamps_seconds))
            ]
            command = [ffmpeg, "-v", "error", "-y"]
            for timestamp in timestamps_seconds:
                command.extend(["-ss", str(timestamp), "-i", video_path])
            for index, (image_path, grayscale_path) in enumerate(
                zip(image_paths, grayscale_paths, strict=True)
            ):
                command.extend(
                    [
                        "-map",
                        f"{index}:v:0",
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        str(image_path),
                        "-map",
                        f"{index}:v:0",
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=160:-2,format=gray",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "gray",
                        str(grayscale_path),
                    ]
                )
            if not timestamps_seconds:
                return []
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    timeout=min(45, FFMPEG_TIMEOUT_SECONDS + 5 * len(timestamps_seconds)),
                )
            except subprocess.TimeoutExpired as exc:
                raise SourceFrameExtractionFailed("ffmpeg timed out") from exc
            if result.returncode != 0:
                raise SourceFrameExtractionFailed("ffmpeg could not extract source frames")
            frames: list[ExtractedSourceFrame] = []
            for timestamp, image_path, grayscale_path in zip(
                timestamps_seconds,
                image_paths,
                grayscale_paths,
                strict=True,
            ):
                image = image_path.read_bytes() if image_path.exists() else b""
                grayscale = grayscale_path.read_bytes() if grayscale_path.exists() else b""
                if not image or not grayscale:
                    raise SourceFrameExtractionFailed("ffmpeg could not extract a source frame")
                frames.append(
                    ExtractedSourceFrame(
                        timestamp_seconds=timestamp,
                        image=image,
                        technical_score=score_grayscale_frame(grayscale),
                    )
                )
            return frames


def score_grayscale_frame(pixels: bytes) -> float:
    if len(pixels) < 2:
        return 0.0
    count = len(pixels)
    average = sum(pixels) / count
    variance = sum((value - average) ** 2 for value in pixels) / count
    contrast = min(1.0, variance**0.5 / 64)
    detail = sum(abs(left - right) for left, right in zip(pixels, pixels[1:])) / (count - 1)
    sharpness = min(1.0, detail / 32)
    exposure = max(0.0, 1.0 - abs(average - 127.5) / 127.5)
    return float(round(0.6 * sharpness + 0.25 * contrast + 0.15 * exposure, 3))


def prepare_source_frame_extraction(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
    actor: CurrentUser,
    timestamps_seconds: tuple[float, ...] | None = None,
) -> SourceFrameExtractionPlan:
    require_not_auditor(
        conn,
        actor=actor,
        action="source_frame.extract",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="source_frame.extract")
    asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="source_frame.extract",
    )
    if str(asset["project_id"]) != project_id:
        raise source_frame_error(
            400,
            "ASSET_PROJECT_MISMATCH",
            "Asset does not belong to the requested project.",
        )
    if not is_reference_video_asset(asset):
        raise source_frame_error(
            422,
            "SOURCE_FRAME_ASSET_NOT_REFERENCE_VIDEO",
            "Source frames require a reference video asset.",
        )
    if not str(asset["sha256"]) or int(asset["size_bytes"]) <= 0:
        raise source_frame_error(
            409,
            "REFERENCE_VIDEO_NOT_READY",
            "Reference video upload is not ready for source frame extraction.",
        )

    duration_seconds = source_video_duration_seconds(asset)
    requested_timestamps = timestamps_seconds or adaptive_source_frame_timestamps(duration_seconds)
    if duration_seconds is not None and any(
        timestamp >= duration_seconds for timestamp in requested_timestamps
    ):
        raise source_frame_error(
            422,
            "SOURCE_FRAME_TIMESTAMP_OUT_OF_RANGE",
            "Source frame timestamps must be inside the reference video duration.",
        )

    return SourceFrameExtractionPlan(
        actor=actor,
        project_id=project_id,
        asset_id=asset_id,
        source_storage_uri=str(asset["storage_uri"]),
        requested_timestamps=requested_timestamps,
    )


def perform_source_frame_extraction(
    plan: SourceFrameExtractionPlan,
    *,
    storage: StorageAdapter,
    extractor: SourceFrameExtractor,
    quality_inspector: SourceFrameQualityInspector | None = None,
    before_quality_call: Callable[[], None] | None = None,
) -> StoredSourceFrameCandidates:
    try:
        reference = storage_object_ref_from_uri(plan.source_storage_uri)
        require_storage_match(storage, reference)
        video = storage.get_object(reference.key)
    except (KeyError, OSError, StorageBackendUnavailable, ValueError) as exc:
        raise source_frame_error(
            503,
            "SOURCE_FRAME_STORAGE_UNAVAILABLE",
            "Reference video storage is temporarily unavailable.",
        ) from exc

    try:
        frames = extractor.extract(
            video,
            filename=Path(reference.key).name,
            timestamps_seconds=plan.requested_timestamps,
        )
    except SourceFrameExtractorUnavailable as exc:
        raise source_frame_error(
            503,
            "SOURCE_FRAME_EXTRACTOR_UNAVAILABLE",
            "ffmpeg is required for source frame extraction.",
        ) from exc
    except SourceFrameExtractionFailed as exc:
        raise source_frame_error(
            422,
            "SOURCE_FRAME_EXTRACTION_FAILED",
            "No usable source frame could be extracted from the reference video.",
        ) from exc

    if not frames:
        raise source_frame_error(
            422,
            "SOURCE_FRAME_EXTRACTION_FAILED",
            "No usable source frame could be extracted from the reference video.",
        )
    if any(not frame.image for frame in frames):
        raise source_frame_error(
            422,
            "SOURCE_FRAME_EXTRACTION_FAILED",
            "No usable source frame could be extracted from the reference video.",
        )

    semantic_quality_status: Literal["VERIFIED", "UNAVAILABLE", "NOT_REQUESTED"] = "NOT_REQUESTED"
    semantic_provider: str | None = None
    semantic_model: str | None = None
    assessments_by_index: dict[int, SourceFrameCandidateAssessment] = {}
    if quality_inspector is not None:
        if before_quality_call is not None:
            before_quality_call()
        try:
            semantic = quality_inspector.inspect_source_frame_candidates(frames)
            assessments_by_index = {
                assessment.candidate_index: assessment for assessment in semantic.candidates
            }
            if set(assessments_by_index) != set(range(len(frames))):
                raise ValueError("source-frame semantic inspection does not match candidates")
            semantic_quality_status = "VERIFIED"
            semantic_provider = semantic.provider
            semantic_model = semantic.model
        except (RuntimeError, ValueError) as exc:
            semantic_quality_status = "UNAVAILABLE"
            semantic_provider = cast(str | None, getattr(quality_inspector, "provider_name", None))
            semantic_model = cast(str | None, getattr(quality_inspector, "model", None))
            logger.warning(
                "source-frame semantic inspection unavailable",
                extra={"project_id": plan.project_id, "error_type": type(exc).__name__},
            )

    created_assets: list[tuple[str, str]] = []
    try:
        candidates: list[dict[str, object]] = []
        for index, frame in enumerate(frames):
            frame_asset_id = str(uuid4())
            storage_key = f"projects/{plan.project_id}/source-frames/{frame_asset_id}.jpg"
            created_assets.append((frame_asset_id, storage_key))
            stored = storage.put_object(storage_key, frame.image, content_type="image/jpeg")
            technical_score = (
                float(frame.technical_score) if frame.technical_score is not None else 0.0
            )
            assessment = assessments_by_index.get(index)
            semantic_score = (
                source_frame_semantic_score(assessment) if assessment is not None else None
            )
            combined_score = (
                round(0.4 * technical_score + 0.6 * semantic_score, 3)
                if semantic_score is not None
                else frame.technical_score
            )
            candidates.append(
                {
                    "asset_id": frame_asset_id,
                    "timestamp_seconds": frame.timestamp_seconds,
                    "score": combined_score,
                    "technical_score": frame.technical_score,
                    "semantic_score": semantic_score,
                    "semantic_assessment": (
                        assessment.model_dump(mode="json") if assessment is not None else None
                    ),
                    "selection_reason": (
                        "综合评分包含人物完整度、面部清晰度、遮挡、姿态、运动模糊及技术画质。"
                        if semantic_score is not None
                        else "语义质检暂不可用，请查看候选画面后手动确认。"
                    ),
                    "storage_uri": stored.uri,
                    "sha256": stored.sha256 or hashlib.sha256(frame.image).hexdigest(),
                    "size_bytes": stored.size,
                }
            )
        candidates.sort(key=technical_score_of_candidate, reverse=True)
        return StoredSourceFrameCandidates(
            candidates=candidates,
            created_assets=created_assets,
            semantic_quality_status=semantic_quality_status,
            semantic_provider=semantic_provider,
            semantic_model=semantic_model,
        )
    except (OSError, StorageBackendUnavailable, ValueError) as exc:
        delete_created_source_frames(
            storage,
            created_assets,
            actor_id=plan.actor.id,
        )
        raise source_frame_error(
            503,
            "SOURCE_FRAME_STORAGE_UNAVAILABLE",
            "Source frame storage is temporarily unavailable.",
        ) from exc


def complete_source_frame_extraction(
    conn: BusinessConnection,
    *,
    plan: SourceFrameExtractionPlan,
    stored: StoredSourceFrameCandidates,
    commit: bool = True,
) -> sqlite3.Row:
    try:
        for candidate in stored.candidates:
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
                    created_by_user_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    candidate["asset_id"],
                    plan.project_id,
                    "source_frame",
                    candidate["storage_uri"],
                    candidate["sha256"],
                    candidate["size_bytes"],
                    "image/jpeg",
                    plan.actor.id,
                ),
            )
        row = insert_version(
            conn,
            project_id=plan.project_id,
            asset_id=plan.asset_id,
            kind=SOURCE_FRAME_CANDIDATES_KIND,
            created_by_user_id=plan.actor.id,
            payload={
                "schema_version": SOURCE_FRAME_SCHEMA_VERSION,
                "source_asset_id": plan.asset_id,
                "requested_timestamps_seconds": list(plan.requested_timestamps),
                "semantic_quality_status": stored.semantic_quality_status,
                "semantic_provider": stored.semantic_provider,
                "semantic_model": stored.semantic_model,
                "candidates": stored.candidates,
            },
            commit=False,
        )
        write_audit(
            conn,
            actor=plan.actor,
            action="source_frame.extract",
            entity_type="version",
            entity_id=str(row["id"]),
            metadata={
                "project_id": plan.project_id,
                "source_asset_id": plan.asset_id,
            },
            commit=False,
        )
        if commit:
            conn.commit()
        return row
    except sqlite3.Error as exc:
        if commit:
            conn.rollback()
        raise source_frame_error(
            500,
            "SOURCE_FRAME_PERSIST_FAILED",
            "Source frame candidates could not be saved. Extract them again.",
        ) from exc


def enqueue_source_frame_task(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    asset_id: str,
    timestamps_seconds: tuple[float, ...] | None,
    idempotency_key: str,
) -> sqlite3.Row:
    plan = prepare_source_frame_extraction(
        conn,
        project_id=project_id,
        asset_id=asset_id,
        actor=actor,
        timestamps_seconds=timestamps_seconds,
    )
    request_payload = {
        "asset_id": asset_id,
        "timestamps_seconds": list(plan.requested_timestamps),
    }
    request_hash = hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    replay = conn.execute(
        """
        SELECT * FROM source_frame_tasks
        WHERE project_id = %s AND idempotency_key = %s
        """,
        (project_id, idempotency_key),
    ).fetchone()
    if replay is not None:
        if str(replay["request_hash"]) != request_hash:
            raise source_frame_error(
                409,
                "SOURCE_FRAME_TASK_IDEMPOTENCY_CONFLICT",
                "取帧参数已经变化，请重新提交。",
            )
        return cast(sqlite3.Row, replay)

    active = conn.execute(
        """
        SELECT * FROM source_frame_tasks
        WHERE project_id = %s AND asset_id = %s
          AND status IN ('PENDING','RUNNING')
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id, asset_id),
    ).fetchone()
    if active is not None:
        return cast(sqlite3.Row, active)

    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO source_frame_tasks (
            id, project_id, asset_id, created_by_user_id, idempotency_key,
            request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT DO NOTHING
        """,
        (
            task_id,
            project_id,
            asset_id,
            actor.id,
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = conn.execute(
        "SELECT * FROM source_frame_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT * FROM source_frame_tasks
            WHERE project_id = %s AND asset_id = %s
              AND status IN ('PENDING','RUNNING')
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (project_id, asset_id),
        ).fetchone()
    if row is None:
        raise source_frame_error(
            409,
            "SOURCE_FRAME_TASK_ENQUEUE_CONFLICT",
            "取帧任务状态已变化，请重试。",
        )
    write_audit(
        conn,
        actor=actor,
        action="source_frame.task_enqueued",
        entity_type="source_frame_task",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "source_asset_id": asset_id},
    )
    return cast(sqlite3.Row, row)


def acquire_source_frame_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> SourceFrameTaskLease | None:
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(
        datetime.now(UTC) + timedelta(minutes=SOURCE_FRAME_TASK_LEASE_MINUTES)
    )
    conn.execute(
        """
        UPDATE source_frame_tasks
        SET status = 'FAILED', locked_by = NULL, locked_until = NULL,
            error_code = 'SOURCE_FRAME_TASK_RECOVERY_REQUIRED',
            error_message_redacted = '取帧任务执行中断，请重新开始。',
            retryable = 1, completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now, now),
    )
    row = conn.execute(
        """
        UPDATE source_frame_tasks
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM source_frame_tasks
            WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return SourceFrameTaskLease(
        id=str(row["id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
    )


def prepare_source_frame_task(
    conn: BusinessConnection,
    *,
    lease: SourceFrameTaskLease,
) -> SourceFrameExtractionPlan:
    row = require_owned_source_frame_task(conn, lease)
    payload = json.loads(str(row["request_json"]))
    raw_timestamps = payload.get("timestamps_seconds")
    if not isinstance(raw_timestamps, list):
        raise RuntimeError("source frame task timestamps are unavailable")
    actor = load_source_frame_task_actor(conn, lease.created_by_user_id)
    return prepare_source_frame_extraction(
        conn,
        project_id=str(row["project_id"]),
        asset_id=str(row["asset_id"]),
        actor=actor,
        timestamps_seconds=tuple(float(value) for value in raw_timestamps),
    )


def record_source_frame_quality_started(
    conn: BusinessConnection,
    *,
    lease: SourceFrameTaskLease,
    provider: str | None,
    model: str | None,
) -> None:
    row = require_owned_source_frame_task(conn, lease)
    actor = load_source_frame_task_actor(conn, lease.created_by_user_id)
    write_audit(
        conn,
        actor=actor,
        action="source_frame.semantic_quality_started",
        entity_type="source_frame_task",
        entity_id=lease.id,
        metadata={
            "project_id": str(row["project_id"]),
            "provider": provider,
            "model": model,
            "attempt": lease.attempt,
        },
        commit=False,
    )
    conn.commit()


def complete_source_frame_task(
    conn: BusinessConnection,
    *,
    lease: SourceFrameTaskLease,
    plan: SourceFrameExtractionPlan,
    stored: StoredSourceFrameCandidates,
) -> sqlite3.Row:
    now = _time_text(datetime.now(UTC))
    with conn:
        require_owned_source_frame_task(conn, lease)
        version = complete_source_frame_extraction(
            conn,
            plan=plan,
            stored=stored,
            commit=False,
        )
        updated = conn.execute(
            """
            UPDATE source_frame_tasks
            SET status = 'SUCCEEDED', result_version_id = %s,
                locked_by = NULL, locked_until = NULL,
                completed_at = %s, updated_at = %s, retryable = 0
            WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            """,
            (str(version["id"]), now, now, lease.id, lease.worker_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("source frame task lease was lost")
    return version


def fail_source_frame_task(
    conn: BusinessConnection,
    *,
    lease: SourceFrameTaskLease,
    cause: Exception,
) -> None:
    code = "SOURCE_FRAME_TASK_FAILED"
    message = "候选源画面提取失败，请稍后重试。"
    retryable = True
    if isinstance(cause, HTTPException):
        detail: dict[str, Any] = cause.detail if isinstance(cause.detail, dict) else {}
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
        retryable = cause.status_code in {429, 502, 503, 504}
    elif isinstance(cause, (StorageBackendUnavailable, OSError)):
        code = "SOURCE_FRAME_STORAGE_UNAVAILABLE"
        message = "素材库暂不可用，请稍后重试。"
    now = _time_text(datetime.now(UTC))
    conn.execute(
        """
        UPDATE source_frame_tasks
        SET status = 'FAILED', error_code = %s,
            error_message_redacted = %s, retryable = %s,
            locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (
            code,
            message,
            1 if retryable else 0,
            now,
            now,
            lease.id,
            lease.worker_id,
        ),
    )
    conn.commit()


def load_source_frame_task(
    conn: BusinessConnection,
    task_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM source_frame_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        raise source_frame_error(
            404,
            "SOURCE_FRAME_TASK_NOT_FOUND",
            "取帧任务不存在。",
        )
    return cast(sqlite3.Row, row)


def cancel_source_frame_task(
    conn: BusinessConnection,
    *,
    task_id: str,
    actor: CurrentUser,
) -> sqlite3.Row:
    row = load_source_frame_task(conn, task_id)
    project_id = str(row["project_id"])
    require_not_auditor(
        conn,
        actor=actor,
        action="source_frame.task.cancel",
        entity_type="source_frame_task",
        entity_id=task_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="source_frame.task.cancel",
    )
    status = str(row["status"])
    if status == "RUNNING":
        raise source_frame_error(
            409,
            "SOURCE_FRAME_TASK_RUNNING",
            "任务正在执行，完成或超时后才能重新开始。",
        )
    if status != "PENDING":
        return row
    now = _time_text(datetime.now(UTC))
    updated = conn.execute(
        """
        UPDATE source_frame_tasks
        SET status = 'FAILED', locked_by = NULL, locked_until = NULL,
            error_code = 'SOURCE_FRAME_TASK_CANCELLED',
            error_message_redacted = '取帧任务已停止，可以重新开始。',
            retryable = 1, completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'PENDING'
        RETURNING *
        """,
        (now, now, task_id),
    ).fetchone()
    if updated is None:
        conn.commit()
        return load_source_frame_task(conn, task_id)
    write_audit(
        conn,
        actor=actor,
        action="source_frame.task_cancelled",
        entity_type="source_frame_task",
        entity_id=task_id,
        metadata={"project_id": project_id},
        commit=False,
    )
    conn.commit()
    return cast(sqlite3.Row, updated)


def latest_source_frame_task(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            """
            SELECT * FROM source_frame_tasks
            WHERE project_id = %s AND asset_id = %s
            ORDER BY CASE WHEN status IN ('PENDING','RUNNING') THEN 0 ELSE 1 END,
                     created_at DESC, id DESC LIMIT 1
            """,
            (project_id, asset_id),
        ).fetchone(),
    )


def require_owned_source_frame_task(
    conn: BusinessConnection,
    lease: SourceFrameTaskLease,
) -> sqlite3.Row:
    row = load_source_frame_task(conn, lease.id)
    if str(row["status"]) != "RUNNING" or str(row["locked_by"]) != lease.worker_id:
        raise RuntimeError("source frame task lease was lost")
    return row


def load_source_frame_task_actor(
    conn: BusinessConnection,
    user_id: str,
) -> CurrentUser:
    row = conn.execute(
        "SELECT id, username, display_name, role FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("source frame task actor is unavailable")
    return CurrentUser(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=cast(Role, str(row["role"])),
    )


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def delete_created_source_frames(
    storage: StorageAdapter,
    created_assets: list[tuple[str, str]],
    *,
    actor_id: str,
) -> None:
    for _, storage_key in created_assets:
        try:
            content_store.delete_object_outside_content_namespace(
                storage, storage_key, actor_id=actor_id
            )
        except (OSError, StorageBackendUnavailable):
            pass


def technical_score_of_candidate(candidate: dict[str, object]) -> float:
    score = candidate["score"]
    return float(score) if isinstance(score, int | float) else -1.0


def source_frame_semantic_score(assessment: SourceFrameCandidateAssessment) -> float:
    if assessment.person_count != 1 or assessment.motion_blur_detected:
        return 0.0
    return round(
        (
            assessment.person_visibility_score
            + assessment.face_clarity_score
            + assessment.unobstructed_score
            + assessment.pose_suitability_score
        )
        / 4,
        3,
    )


def confirm_source_frame(
    conn: BusinessConnection,
    *,
    project_id: str,
    source_frame_asset_id: str,
    actor: CurrentUser,
    character_features: dict[str, object] | None = None,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="source_frame.confirm",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="source_frame.confirm")
    candidate_version = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if candidate_version is None:
        raise source_frame_error(
            409,
            "SOURCE_FRAME_CANDIDATES_NOT_FOUND",
            "Extract source frame candidates before confirming one.",
        )
    payload = json.loads(str(candidate_version["payload_json"]))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise source_frame_error(
            409,
            "SOURCE_FRAME_CANDIDATES_INVALID",
            "Source frame candidates are invalid. Extract them again.",
        )
    candidate = next(
        (
            value
            for value in candidates
            if isinstance(value, dict) and value.get("asset_id") == source_frame_asset_id
        ),
        None,
    )
    if candidate is None:
        raise source_frame_error(
            422,
            "SOURCE_FRAME_CANDIDATE_NOT_FOUND",
            "The selected source frame is not in the latest candidate set.",
        )
    frame_asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=source_frame_asset_id,
        action="source_frame.confirm",
    )
    if str(frame_asset["project_id"]) != project_id or str(frame_asset["kind"]) != "source_frame":
        raise source_frame_error(
            422,
            "SOURCE_FRAME_CANDIDATE_NOT_FOUND",
            "The selected source frame is not valid for this project.",
        )

    selection_payload: dict[str, object] = {
        "schema_version": SOURCE_FRAME_SCHEMA_VERSION,
        "source_frame_candidates_version_id": str(candidate_version["id"]),
        "source_frame_asset_id": source_frame_asset_id,
        "timestamp_seconds": candidate["timestamp_seconds"],
    }
    if character_features is not None:
        selection_payload["character_features"] = character_features

    row = insert_version(
        conn,
        project_id=project_id,
        asset_id=source_frame_asset_id,
        kind=SOURCE_FRAME_SELECTION_KIND,
        created_by_user_id=actor.id,
        payload=selection_payload,
    )
    write_audit(
        conn,
        actor=actor,
        action="source_frame.confirm",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "source_frame_asset_id": source_frame_asset_id},
    )
    return row


def latest_version(
    conn: BusinessConnection,
    project_id: str,
    kind: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id,
               created_at
        FROM versions
        WHERE project_id = %s AND kind = %s
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id, kind),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def source_frame_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
