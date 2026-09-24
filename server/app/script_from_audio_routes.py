"""工作台"提取文案"异步任务路由（script-from-audio）。"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, status

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.permissions import require_project_access
from app.script_from_audio import (
    ScriptFromAudioRequest,
    ScriptFromAudioTaskResponse,
    enqueue_script_from_audio_task,
    latest_script_from_audio_task,
    load_script_from_audio_task,
    script_from_audio_task_response,
)

router = APIRouter(prefix="/api")


@router.post(
    "/projects/{project_id}/script-from-audio",
    response_model=ScriptFromAudioTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def extract_script_from_audio(
    project_id: str,
    request: ScriptFromAudioRequest,
    db: BusinessDbDep,
) -> ScriptFromAudioTaskResponse:
    with db.write() as (conn, actor):
        row = enqueue_script_from_audio_task(
            conn,
            actor=actor,
            project_id=project_id,
            source_asset_id=request.source_asset_id,
            idempotency_key=request.idempotency_key or str(uuid4()),
        )
        return script_from_audio_task_response(row)


@router.get(
    "/script-from-audio-tasks/{task_id}",
    response_model=ScriptFromAudioTaskResponse,
)
def read_script_from_audio_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ScriptFromAudioTaskResponse:
    row = load_script_from_audio_task(conn, task_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="project.script_from_audio_read",
    )
    return script_from_audio_task_response(row)


@router.get(
    "/projects/{project_id}/script-from-audio-tasks/latest",
    response_model=ScriptFromAudioTaskResponse | None,
)
def read_latest_script_from_audio_task(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ScriptFromAudioTaskResponse | None:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.script_from_audio_read",
    )
    row = latest_script_from_audio_task(conn, project_id=project_id)
    return None if row is None else script_from_audio_task_response(row)
