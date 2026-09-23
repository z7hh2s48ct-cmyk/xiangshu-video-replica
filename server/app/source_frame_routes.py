from __future__ import annotations

import json
import math
import sqlite3
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.media_routes import get_media_storage
from app.permissions import remap_security_denial, require_project_access
from app.source_frames import (
    SOURCE_FRAME_CANDIDATES_KIND,
    SOURCE_FRAME_SELECTION_KIND,
    FFmpegSourceFrameExtractor,
    SourceFrameExtractor,
    cancel_source_frame_task,
    confirm_source_frame,
    enqueue_source_frame_task,
    latest_source_frame_task,
    latest_version,
    load_source_frame_task,
)
from app.storage import StorageAdapter

router = APIRouter(prefix="/api", tags=["source-frames"])


class ExtractSourceFramesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    timestamps_seconds: list[float] | None = Field(default=None, min_length=1, max_length=5)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)

    @model_validator(mode="after")
    def validate_timestamps(self) -> ExtractSourceFramesRequest:
        if self.timestamps_seconds is None:
            return self
        if any(
            not math.isfinite(timestamp) or timestamp < 0 for timestamp in self.timestamps_seconds
        ):
            raise ValueError("source frame timestamps must be non-negative and finite")
        if len(set(self.timestamps_seconds)) != len(self.timestamps_seconds):
            raise ValueError("source frame timestamps must be unique")
        return self


class SourceFrameCharacterFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orientation: Literal["FRONT", "LEFT_45", "RIGHT_45", "LEFT_SIDE", "RIGHT_SIDE"]
    shot_size: Literal["CLOSE_UP", "HALF_BODY", "FULL_BODY"]
    face_visible: bool
    body_completeness: Literal["FACE_ONLY", "UPPER_BODY", "FULL_BODY", "PARTIAL"]


class ConfirmSourceFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_frame_asset_id: str = Field(min_length=1)
    character_features: SourceFrameCharacterFeatures | None = None


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    asset_id: str | None
    kind: str
    version_number: int
    payload: dict[str, Any]
    created_by_user_id: str | None
    created_at: str


class SourceFrameTaskResponse(BaseModel):
    id: str
    project_id: str
    asset_id: str
    timestamps_seconds: list[float]
    status: str
    attempt: int
    result_version_id: str | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


def get_source_frame_extractor() -> SourceFrameExtractor:
    return FFmpegSourceFrameExtractor()


SourceFrameStorage = Annotated[StorageAdapter, Depends(get_media_storage)]
InjectedSourceFrameExtractor = Annotated[SourceFrameExtractor, Depends(get_source_frame_extractor)]


@router.post(
    "/projects/{project_id}/source-frames/extract",
    response_model=SourceFrameTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def extract_project_source_frames(
    project_id: str,
    request: ExtractSourceFramesRequest,
    db: BusinessDbDep,
) -> SourceFrameTaskResponse:
    with db.write() as (conn, actor):
        row = enqueue_source_frame_task(
            conn,
            actor=actor,
            project_id=project_id,
            asset_id=request.asset_id,
            timestamps_seconds=(
                None if request.timestamps_seconds is None else tuple(request.timestamps_seconds)
            ),
            idempotency_key=request.idempotency_key or str(uuid4()),
        )
        return source_frame_task_response(row)


@router.get(
    "/source-frame-tasks/{task_id}",
    response_model=SourceFrameTaskResponse,
)
def read_source_frame_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> SourceFrameTaskResponse:
    row = load_source_frame_task(conn, task_id)
    try:
        require_project_access(
            conn,
            actor=actor,
            project_id=str(row["project_id"]),
            action="source_frame.task.read",
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            raise remap_security_denial(
                exc,
                status_code=404,
                detail={"code": "SOURCE_FRAME_TASK_NOT_FOUND", "message": "取帧任务不存在。"},
            ) from exc
        raise
    return source_frame_task_response(row)


@router.post(
    "/source-frame-tasks/{task_id}/cancel",
    response_model=SourceFrameTaskResponse,
)
def cancel_project_source_frame_task(
    task_id: str,
    db: BusinessDbDep,
) -> SourceFrameTaskResponse:
    with db.write() as (conn, actor):
        row = cancel_source_frame_task(conn, task_id=task_id, actor=actor)
        return source_frame_task_response(row)


@router.get(
    "/projects/{project_id}/source-frame-tasks/latest",
    response_model=SourceFrameTaskResponse | None,
)
def read_latest_source_frame_task(
    project_id: str,
    asset_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> SourceFrameTaskResponse | None:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="source_frame.task.read",
    )
    row = latest_source_frame_task(
        conn,
        project_id=project_id,
        asset_id=asset_id,
    )
    return None if row is None else source_frame_task_response(row)


@router.get("/projects/{project_id}/source-frames/latest", response_model=VersionResponse | None)
def read_latest_source_frames(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse | None:
    require_project_access(conn, actor=actor, project_id=project_id, action="source_frame.read")
    row = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if row is None:
        return None
    return version_response(row)


@router.post("/projects/{project_id}/source-frames/confirm", response_model=VersionResponse)
def confirm_project_source_frame(
    project_id: str,
    request: ConfirmSourceFrameRequest,
    db: BusinessDbDep,
) -> VersionResponse:
    with db.write() as (conn, actor):
        row = confirm_source_frame(
            conn,
            project_id=project_id,
            source_frame_asset_id=request.source_frame_asset_id,
            actor=actor,
            character_features=(
                None
                if request.character_features is None
                else request.character_features.model_dump()
            ),
        )
        return version_response(row)


@router.get(
    "/projects/{project_id}/source-frames/selection/latest",
    response_model=VersionResponse | None,
)
def read_latest_source_frame_selection(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse | None:
    require_project_access(conn, actor=actor, project_id=project_id, action="source_frame.read")
    row = latest_version(conn, project_id, SOURCE_FRAME_SELECTION_KIND)
    if row is None:
        return None
    payload = json.loads(str(row["payload_json"]))
    candidate_version_id = payload.get("source_frame_candidates_version_id")
    current_candidates = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if current_candidates is None or candidate_version_id != str(current_candidates["id"]):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOURCE_FRAME_SELECTION_STALE",
                "message": "Select a source frame from the latest candidate set.",
            },
        )
    return version_response(row)


def version_response(row: sqlite3.Row) -> VersionResponse:
    return VersionResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        kind=str(row["kind"]),
        version_number=int(row["version_number"]),
        payload=json.loads(str(row["payload_json"])),
        created_by_user_id=None
        if row["created_by_user_id"] is None
        else str(row["created_by_user_id"]),
        created_at=str(row["created_at"]),
    )


def source_frame_task_response(row: sqlite3.Row) -> SourceFrameTaskResponse:
    request = json.loads(str(row["request_json"]))
    timestamps = request.get("timestamps_seconds")
    return SourceFrameTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=str(row["asset_id"]),
        timestamps_seconds=(
            [float(value) for value in timestamps] if isinstance(timestamps, list) else []
        ),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        result_version_id=(
            None if row["result_version_id"] is None else str(row["result_version_id"])
        ),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=(
            None if row["error_message_redacted"] is None else str(row["error_message_redacted"])
        ),
        retryable=bool(row["retryable"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        completed_at=(None if row["completed_at"] is None else str(row["completed_at"])),
    )
