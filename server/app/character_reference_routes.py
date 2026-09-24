from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.character_contracts import CharacterReferenceRecommendation, CharacterReferenceSelection
from app.character_reference_matching import (
    create_character_reference_selection,
    get_character_reference_recommendation,
    get_latest_character_reference_selection,
)
from app.customer_fence import BusinessDbDep

router = APIRouter(prefix="/api", tags=["character-references"])


class SelectCharacterReferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_frame_selection_version_id: str = Field(min_length=1)
    character_version_id: str = Field(min_length=1)
    selected_asset_ids: list[str] | None = Field(default=None, min_length=1, max_length=4)


@router.get(
    "/projects/{project_id}/character-reference-recommendation",
    response_model=CharacterReferenceRecommendation,
)
def read_character_reference_recommendation(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterReferenceRecommendation:
    return get_character_reference_recommendation(
        conn,
        actor=actor,
        project_id=project_id,
    )


@router.post(
    "/projects/{project_id}/character-reference-selection",
    response_model=CharacterReferenceSelection,
    status_code=status.HTTP_201_CREATED,
)
def select_character_references(
    project_id: str,
    request: SelectCharacterReferencesRequest,
    db: BusinessDbDep,
) -> CharacterReferenceSelection:
    with db.write() as (conn, actor):
        return create_character_reference_selection(
            conn,
            actor=actor,
            project_id=project_id,
            expected_source_frame_version_id=request.source_frame_selection_version_id,
            expected_character_version_id=request.character_version_id,
            selected_asset_ids=request.selected_asset_ids,
        )


@router.get(
    "/projects/{project_id}/character-reference-selection/latest",
    response_model=CharacterReferenceSelection | None,
)
def read_latest_character_reference_selection(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterReferenceSelection | None:
    try:
        return get_latest_character_reference_selection(
            conn,
            actor=actor,
            project_id=project_id,
        )
    except HTTPException as exc:
        if (
            exc.status_code == 404
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "CHARACTER_REFERENCE_SELECTION_NOT_FOUND"
        ):
            return None
        raise
