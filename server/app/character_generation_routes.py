from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.character_asset_review import (
    list_character_asset_reviews,
    publish_character_version,
    review_character_asset,
)
from app.character_contracts import (
    CharacterAsset,
    CharacterAssetReview,
    CharacterAssetReviewDecision,
    CharacterGenerationTask,
    CharacterVersion,
    RequiredCharacterViewType,
)
from app.character_identity_routes import CharacterAdmin, CharacterStorage
from app.character_image_generation import (
    create_character_generation_tasks,
    list_character_assets,
    list_character_generation_tasks,
    regenerate_character_asset,
)

router = APIRouter(prefix="/api", tags=["character-generation"])


class CharacterGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    view_types: list[RequiredCharacterViewType] | None = Field(default=None, min_length=1)
    candidates_per_view: int = Field(default=1, ge=1, le=4)


class CharacterRegenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)


class CharacterAssetReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: CharacterAssetReviewDecision
    issue_codes: list[str] = Field(default_factory=list, max_length=16)
    comment: str | None = Field(default=None, max_length=1000)


class CharacterVersionPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_asset_ids: dict[RequiredCharacterViewType, str] = Field(min_length=1)


@router.post(
    "/character-versions/{version_id}/generate-assets",
    response_model=list[CharacterGenerationTask],
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_character_assets(
    version_id: str,
    payload: CharacterGenerationRequest,
    conn: Database,
    actor: CharacterAdmin,
) -> list[CharacterGenerationTask]:
    return create_character_generation_tasks(
        conn,
        actor=actor,
        version_id=version_id,
        idempotency_key=payload.idempotency_key,
        view_types=payload.view_types,
        candidates_per_view=payload.candidates_per_view,
    )


@router.get(
    "/character-versions/{version_id}/generation-tasks",
    response_model=list[CharacterGenerationTask],
)
def read_character_generation_tasks(
    version_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[CharacterGenerationTask]:
    return list_character_generation_tasks(
        conn,
        actor=actor,
        version_id=version_id,
    )


@router.get(
    "/character-versions/{version_id}/assets",
    response_model=list[CharacterAsset],
)
def read_character_assets(
    version_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[CharacterAsset]:
    return list_character_assets(
        conn,
        actor=actor,
        version_id=version_id,
    )


@router.post(
    "/character-assets/{character_asset_id}/review",
    response_model=CharacterAssetReview,
    status_code=status.HTTP_201_CREATED,
)
def create_character_asset_review(
    character_asset_id: str,
    payload: CharacterAssetReviewRequest,
    conn: Database,
    actor: CharacterAdmin,
) -> CharacterAssetReview:
    return review_character_asset(
        conn,
        actor=actor,
        character_asset_id=character_asset_id,
        decision=payload.decision,
        issue_codes=payload.issue_codes,
        comment=payload.comment,
    )


@router.get(
    "/character-assets/{character_asset_id}/reviews",
    response_model=list[CharacterAssetReview],
)
def read_character_asset_reviews(
    character_asset_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[CharacterAssetReview]:
    return list_character_asset_reviews(
        conn,
        actor=actor,
        character_asset_id=character_asset_id,
    )


@router.post(
    "/character-versions/{version_id}/publish",
    response_model=CharacterVersion,
)
def publish_character_version_route(
    version_id: str,
    payload: CharacterVersionPublishRequest,
    conn: Database,
    actor: CharacterAdmin,
    storage: CharacterStorage,
) -> CharacterVersion:
    return publish_character_version(
        conn,
        actor=actor,
        version_id=version_id,
        selected_asset_ids=payload.selected_asset_ids,
        storage=storage,
    )


@router.post(
    "/character-assets/{character_asset_id}/regenerate",
    response_model=list[CharacterGenerationTask],
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_character_asset_route(
    character_asset_id: str,
    payload: CharacterRegenerationRequest,
    conn: Database,
    actor: CharacterAdmin,
) -> list[CharacterGenerationTask]:
    return regenerate_character_asset(
        conn,
        actor=actor,
        character_asset_id=character_asset_id,
        idempotency_key=payload.idempotency_key,
    )
