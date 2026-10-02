"""Customer-safe short references; diagnostic payloads stay in the control plane."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.auth import AuthenticatedUser, Database

router = APIRouter(prefix="/api/task-references", tags=["task-references"])
TaskType = Literal[
    "VIDEO",
    "FIRST_FRAME_IMAGE",
    "CHARACTER_SHEET_IMAGE",
    "CHARACTER_VIEW_IMAGE",
    "ANALYSIS",
    "SOURCE_FRAME",
    "ORAL_VIDEO",
    "ORAL_AVATAR",
    "ORAL_VOICE",
]

SOURCES = {
    "VIDEO": (
        "generation_tasks task JOIN generation_batches batch ON batch.id=task.batch_id",
        "batch.created_by_user_id",
    ),
    "FIRST_FRAME_IMAGE": ("first_frame_tasks task", "task.created_by_user_id"),
    "CHARACTER_SHEET_IMAGE": ("character_sheet_tasks task", "task.created_by_user_id"),
    "CHARACTER_VIEW_IMAGE": ("character_generation_tasks task", "task.created_by"),
    "ANALYSIS": ("analysis_tasks task", "task.created_by_user_id"),
    "SOURCE_FRAME": ("source_frame_tasks task", "task.created_by_user_id"),
    "ORAL_VIDEO": ("oral_tasks task", "task.owner_user_id"),
    "ORAL_AVATAR": ("oral_avatars task", "task.owner_user_id"),
    "ORAL_VOICE": ("oral_voices task", "task.owner_user_id"),
}


class CustomerTaskReference(BaseModel):
    short_ref: str


@router.get("/{task_type}/{task_id}", response_model=CustomerTaskReference)
def customer_task_reference(
    conn: Database, actor: AuthenticatedUser, response: Response, task_type: TaskType, task_id: str
) -> CustomerTaskReference:
    response.headers["Cache-Control"] = "no-store"
    source, owner = SOURCES[task_type]
    row = conn.execute(
        f"SELECT {owner} AS owner FROM {source} WHERE task.id=%s",
        (task_id,),  # noqa: S608
    ).fetchone()
    # Hide both existence and diagnostic identifiers for another customer's task.
    if row is None or (actor.role not in {"admin", "auditor"} and str(row[0]) != actor.id):
        raise HTTPException(404, detail={"code": "TASK_REFERENCE_NOT_FOUND"})
    ref = conn.execute(
        "SELECT short_ref FROM task_diagnostic_refs WHERE task_type=%s AND task_id=%s",
        (task_type, task_id),
    ).fetchone()
    if ref is None:
        raise HTTPException(404, detail={"code": "TASK_REFERENCE_NOT_FOUND"})
    return CustomerTaskReference(short_ref=str(ref[0]))
