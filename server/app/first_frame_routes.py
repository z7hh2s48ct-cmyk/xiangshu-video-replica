from __future__ import annotations

import json
import os
import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.async_compat import reject_legacy_sync_operation
from app.auth import AuthenticatedUser, Database
from app.bootstrap import is_customer_production
from app.customer_fence import BusinessDbDep, BusinessReadConn
from app.first_frames import (
    APILIO_DEFAULT_BASE_URL,
    FIRST_FRAME_SELECTION_KIND,
    ApilioFirstFrameQualityInspector,
    ApilioImageProvider,
    FakeFirstFrameQualityInspector,
    FakeImageProvider,
    FirstFrameQualityInspector,
    ImageProvider,
    complete_first_frame_generation,
    confirm_first_frame,
    current_first_frame_candidates,
    delete_created_first_frames,
    load_first_frame_generation_work,
    perform_first_frame_generation,
    prepare_first_frame_generation,
    store_first_frame_generation,
)
from app.image_tasks import (
    enqueue_first_frame_task,
    latest_image_task,
    load_image_task,
    require_first_frame_task_access,
)
from app.media_routes import get_media_storage
from app.permissions import remap_security_denial, require_project_access
from app.settings import SettingsRepository, SettingsUnavailableError
from app.source_frames import latest_version
from app.storage import StorageAdapter

router = APIRouter(prefix="/api", tags=["first-frames"])


class GenerateFirstFramesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal["gpt-image-2", "nano-banana-pro-2k"] = "gpt-image-2"
    prompt: str | None = Field(default=None, max_length=4000)
    replace_scene: bool = False
    quantity: int = Field(default=3, ge=1, le=3)
    aspect_ratio: Literal["9:16", "16:9", "1:1", "3:4", "4:3"] | None = None
    character_version_id: str | None = Field(default=None, min_length=1)
    character_reference_selection_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_character_binding(self) -> GenerateFirstFramesRequest:
        if (self.character_version_id is None) != (self.character_reference_selection_id is None):
            raise ValueError("character version and reference selection must be supplied together")
        return self


class EnqueueFirstFramesRequest(GenerateFirstFramesRequest):
    idempotency_key: str = Field(min_length=8, max_length=200)


FirstFrameTaskStage = Literal[
    "QUEUED",
    "PREPARING",
    "GENERATING",
    "VERIFYING",
    "SUCCEEDED",
    "FAILED",
    "NEEDS_REVIEW",
]


class FirstFrameTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    status: str
    stage: FirstFrameTaskStage
    attempt: int
    result_version_id: str | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


class ConfirmFirstFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_frame_asset_id: str = Field(min_length=1)
    # 显式确认未通过自动质检的候选；标记会随确认版本留痕。
    allow_unverified: bool = False
    # 历史版本放开：显式指定候选版本（可指向历史版本）时，该图从此版本里
    # 选定；缺省时仍只接受当前（最新）候选集。
    first_frame_candidates_version_id: str | None = Field(default=None, min_length=1)


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


def get_image_provider(conn: BusinessReadConn) -> ImageProvider:
    has_saved_apilio_config = (
        conn.execute(
            "SELECT 1 FROM provider_settings WHERE provider = %s",
            ("apilio",),
        ).fetchone()
        is not None
    )
    try:
        config = SettingsRepository(conn).load_provider_config("apilio")
    except SettingsUnavailableError as exc:
        if has_saved_apilio_config:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "APILIO_SETTINGS_UNAVAILABLE",
                    "message": (
                        "Apilio settings cannot be decrypted. Ask an administrator to repair them."
                    ),
                },
            ) from exc
        if is_customer_production():
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "APILIO_SETTINGS_REQUIRED",
                    "message": "图像生成服务尚未配置，请联系管理员。",
                },
            ) from exc
        return FakeImageProvider()
    if not config:
        if is_customer_production():
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "APILIO_SETTINGS_REQUIRED",
                    "message": "图像生成服务尚未配置，请联系管理员。",
                },
            )
        return FakeImageProvider()
    api_key = config.get("api_key")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "APILIO_SETTINGS_UNAVAILABLE",
                "message": (
                    "Apilio API Key is unavailable. Ask an administrator to repair settings."
                ),
            },
        )
    return ApilioImageProvider(
        api_key=api_key,
        # The desktop settings page does not expose a custom Apilio endpoint.
        # Keeping the origin fixed prevents an imported legacy base_url from
        # receiving the configured bearer token (same policy as the analysis path).
        base_url=APILIO_DEFAULT_BASE_URL,
    )


def get_first_frame_quality_inspector(conn: BusinessReadConn) -> FirstFrameQualityInspector:
    if os.environ.get("VIDEO_REPLICA_FAKE_FIRST_FRAME_QUALITY_INSPECTOR") == "1":
        if is_customer_production():
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "FAKE_FIRST_FRAME_QUALITY_FORBIDDEN",
                    "message": "客户生产环境禁止使用模拟首帧质检器。",
                },
            )
        return FakeFirstFrameQualityInspector()
    has_saved_apilio_config = (
        conn.execute(
            "SELECT 1 FROM provider_settings WHERE provider = %s",
            ("apilio",),
        ).fetchone()
        is not None
    )
    try:
        config = SettingsRepository(conn).load_provider_config("apilio")
    except SettingsUnavailableError as exc:
        if has_saved_apilio_config or is_customer_production():
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "FIRST_FRAME_QUALITY_SETTINGS_UNAVAILABLE",
                    "message": "首帧自动质检服务尚未正确配置，请联系管理员。",
                },
            ) from exc
        return FakeFirstFrameQualityInspector()
    if not config:
        if is_customer_production():
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "FIRST_FRAME_QUALITY_SETTINGS_REQUIRED",
                    "message": "首帧自动质检服务尚未配置，请联系管理员。",
                },
            )
        return FakeFirstFrameQualityInspector()
    api_key = config.get("analysis_api_key") or config.get("api_key")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "FIRST_FRAME_QUALITY_SETTINGS_UNAVAILABLE",
                "message": "首帧自动质检密钥不可用，请联系管理员。",
            },
        )
    return ApilioFirstFrameQualityInspector(
        api_key=api_key,
        base_url=APILIO_DEFAULT_BASE_URL,
    )


FirstFrameStorage = Annotated[StorageAdapter, Depends(get_media_storage)]
InjectedImageProvider = Annotated[ImageProvider, Depends(get_image_provider)]


def require_async_first_frame_route(project_id: str) -> None:
    reject_legacy_sync_operation(
        replacement=f"/api/projects/{project_id}/first-frame-tasks",
    )


@router.post(
    "/projects/{project_id}/first-frames/generate",
    response_model=VersionResponse,
    dependencies=[Depends(require_async_first_frame_route)],
)
def generate_project_first_frames(
    project_id: str,
    request: GenerateFirstFramesRequest,
    storage: FirstFrameStorage,
    provider: InjectedImageProvider,
    db: BusinessDbDep,
) -> VersionResponse:
    with db.write() as (conn, actor):
        plan = prepare_first_frame_generation(
            conn,
            project_id=project_id,
            actor=actor,
            model=request.model,
            prompt=request.prompt,
            quantity=request.quantity,
            aspect_ratio=request.aspect_ratio,
            replace_scene=request.replace_scene,
            character_version_id=request.character_version_id,
            character_reference_selection_id=request.character_reference_selection_id,
        )
    # COS reads, provider generation and COS writes routinely take 1–3
    # minutes. They
    # must run after the fenced customer transaction releases its session-row
    # lock; otherwise every concurrent desktop request appears to be offline.
    work = load_first_frame_generation_work(plan, storage=storage)
    generated = perform_first_frame_generation(
        work,
        provider=provider,
    )
    stored = store_first_frame_generation(work, storage=storage, generated=generated)
    try:
        with db.write() as (conn, _actor):
            row = complete_first_frame_generation(
                conn,
                work=work,
                provider=provider,
                stored=stored,
            )
    except Exception:
        delete_created_first_frames(
            storage,
            stored.created_assets,
            actor_id=work.actor.id,
        )
        raise
    return version_response(row)


@router.post(
    "/projects/{project_id}/first-frame-tasks",
    response_model=FirstFrameTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_project_first_frame_task(
    project_id: str,
    request: EnqueueFirstFramesRequest,
    db: BusinessDbDep,
) -> FirstFrameTaskResponse:
    """Persist an image request without holding the customer session open."""

    with db.write() as (conn, actor):
        row = enqueue_first_frame_task(
            conn,
            actor=actor,
            project_id=project_id,
            model=request.model,
            prompt=request.prompt,
            quantity=request.quantity,
            character_version_id=request.character_version_id,
            character_reference_selection_id=request.character_reference_selection_id,
            idempotency_key=request.idempotency_key,
            aspect_ratio=request.aspect_ratio,
            replace_scene=request.replace_scene,
        )
        return first_frame_task_response(row)


@router.get("/first-frame-tasks/{task_id}", response_model=FirstFrameTaskResponse)
def read_first_frame_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> FirstFrameTaskResponse:
    row = load_image_task(conn, table="first_frame_tasks", task_id=task_id)
    try:
        require_first_frame_task_access(conn, actor=actor, row=row)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise remap_security_denial(
                exc,
                status_code=404,
                detail={"code": "IMAGE_TASK_NOT_FOUND", "message": "生成任务不存在。"},
            ) from exc
        raise
    return first_frame_task_response(row)


@router.get(
    "/projects/{project_id}/first-frame-tasks/active-or-latest",
    response_model=FirstFrameTaskResponse | None,
)
def read_latest_first_frame_task(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> FirstFrameTaskResponse | None:
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.task.read")
    row = latest_image_task(
        conn,
        table="first_frame_tasks",
        owner_column="project_id",
        owner_id=project_id,
    )
    return None if row is None else first_frame_task_response(row)


@router.get("/projects/{project_id}/first-frames/latest", response_model=VersionResponse | None)
def read_latest_first_frames(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse | None:
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.read")
    try:
        row = current_first_frame_candidates(conn, project_id=project_id)
    except HTTPException as exc:
        if (
            exc.status_code == 409
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "FIRST_FRAME_CANDIDATES_NOT_FOUND"
        ):
            return None
        raise
    return version_response(row)


@router.get("/projects/{project_id}/first-frames/history", response_model=list[VersionResponse])
def read_first_frame_history(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[VersionResponse]:
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.read")
    rows = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json,
               created_by_user_id, created_at
        FROM versions
        WHERE project_id = %s AND kind = %s
        ORDER BY version_number DESC
        LIMIT 20
        """,
        (project_id, "first_frame_candidates"),
    ).fetchall()
    return [version_response(row) for row in rows]


@router.post("/projects/{project_id}/first-frames/confirm", response_model=VersionResponse)
def confirm_project_first_frame(
    project_id: str,
    request: ConfirmFirstFrameRequest,
    db: BusinessDbDep,
) -> VersionResponse:
    with db.write() as (conn, actor):
        return version_response(
            confirm_first_frame(
                conn,
                project_id=project_id,
                first_frame_asset_id=request.first_frame_asset_id,
                actor=actor,
                allow_unverified=request.allow_unverified,
                first_frame_candidates_version_id=request.first_frame_candidates_version_id,
            )
        )


@router.get(
    "/projects/{project_id}/first-frames/selection/latest",
    response_model=VersionResponse | None,
)
def read_latest_first_frame_selection(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse | None:
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.read")
    row = latest_version(conn, project_id, FIRST_FRAME_SELECTION_KIND)
    if row is None:
        return None
    # 历史版本放开：已确认首帧可能指向旧候选版本（用户显式选择了历史图）。
    # 这里只如实读取记录，不再用“必须等于最新候选版本”把已确认结果整体
    # 置为 409；过期与否由 H3 生成门禁与前端警示承担。
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


def first_frame_task_response(row: sqlite3.Row) -> FirstFrameTaskResponse:
    return FirstFrameTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        status=str(row["status"]),
        stage=first_frame_task_stage(row),
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


def first_frame_task_stage(row: sqlite3.Row) -> FirstFrameTaskStage:
    task_status = str(row["status"])
    if task_status == "PENDING":
        return "QUEUED"
    if task_status == "SUCCEEDED":
        return "SUCCEEDED"
    if task_status == "FAILED":
        return "FAILED"
    if task_status == "SUBMISSION_UNCERTAIN":
        return "NEEDS_REVIEW"

    result_json = row["result_json"]
    if result_json is None:
        return "PREPARING"
    try:
        result = json.loads(str(result_json))
    except json.JSONDecodeError:
        return "PREPARING"
    if not isinstance(result, dict):
        return "PREPARING"
    if isinstance(result.get("checkpoint"), dict):
        return "VERIFYING"
    if isinstance(result.get("execution"), dict):
        return "GENERATING"
    return "PREPARING"
