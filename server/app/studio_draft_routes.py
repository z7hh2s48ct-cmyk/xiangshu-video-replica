"""C7 studio draft routes: cloud persistence for the V1.4 copy workshop."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.studio_drafts import (
    DraftKind,
    SavedScriptListResponse,
    SavedScriptRequest,
    SavedScriptResponse,
    StudioDraftResponse,
    StudioDraftUpsertRequest,
    delete_saved_script,
    delete_studio_draft,
    list_saved_scripts,
    load_saved_script,
    load_studio_draft,
    save_saved_script,
    save_studio_draft,
)

router = APIRouter(prefix="/api/studio")


class StudioDraftDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool


class SavedScriptDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool


@router.get("/drafts/{draft_kind}", response_model=StudioDraftResponse)
def read_studio_draft(
    draft_kind: DraftKind,
    conn: Database,
    actor: AuthenticatedUser,
) -> StudioDraftResponse:
    return load_studio_draft(conn, actor_id=actor.id, kind=draft_kind)


@router.put("/drafts/{draft_kind}", response_model=StudioDraftResponse)
def upsert_studio_draft(
    draft_kind: DraftKind,
    request: StudioDraftUpsertRequest,
    db: BusinessDbDep,
) -> StudioDraftResponse:
    with db.write() as (conn, actor):
        return save_studio_draft(conn, actor=actor, kind=draft_kind, request=request)


@router.delete("/drafts/{draft_kind}", response_model=StudioDraftDeleteResponse)
def remove_studio_draft(
    draft_kind: DraftKind,
    db: BusinessDbDep,
) -> StudioDraftDeleteResponse:
    with db.write() as (conn, actor):
        delete_studio_draft(conn, actor=actor, kind=draft_kind)
    return StudioDraftDeleteResponse(deleted=True)


@router.get("/saved-scripts", response_model=SavedScriptListResponse)
def read_saved_scripts(
    conn: Database,
    actor: AuthenticatedUser,
) -> SavedScriptListResponse:
    return list_saved_scripts(conn, actor_id=actor.id)


@router.get("/saved-scripts/{script_id}", response_model=SavedScriptResponse)
def read_user_saved_script(
    script_id: str, conn: Database, actor: AuthenticatedUser
) -> SavedScriptResponse:
    return load_saved_script(conn, actor_id=actor.id, script_id=script_id)


@router.post("/saved-scripts", response_model=SavedScriptResponse)
def create_saved_script(
    request: SavedScriptRequest,
    db: BusinessDbDep,
) -> SavedScriptResponse:
    with db.write() as (conn, actor):
        return save_saved_script(conn, actor=actor, request=request)


@router.delete("/saved-scripts/{script_id}", response_model=SavedScriptDeleteResponse)
def remove_saved_script(
    script_id: str,
    db: BusinessDbDep,
) -> SavedScriptDeleteResponse:
    with db.write() as (conn, actor):
        delete_saved_script(conn, actor=actor, script_id=script_id)
    return SavedScriptDeleteResponse(deleted=True)
