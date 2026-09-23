from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth import AuthenticatedUser, Database
from app.character_contracts import ProjectCharacterVersionOption
from app.characters import (
    CharacterData,
    choose_project_main_character,
    create_character,
    delete_character,
    get_character,
    get_project_main_character,
    list_characters,
    update_character,
)
from app.customer_fence import BusinessDbDep
from app.permissions import require_not_auditor, require_project_access, require_role
from app.project_character_selection import (
    choose_project_character_version,
    list_available_project_character_versions,
)

router = APIRouter(prefix="/api", tags=["characters"])


class CharacterCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    reference_asset_ids: list[str] = Field(default_factory=list)
    authorization_project_ids: list[str] = Field(default_factory=list)
    authorization_expires_at: datetime | None = None
    is_active: bool = True


class CharacterUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    reference_asset_ids: list[str] | None = None
    authorization_project_ids: list[str] | None = None
    authorization_expires_at: datetime | None = None
    clear_authorization_expires_at: bool = False
    is_active: bool | None = None


class CharacterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    reference_asset_ids: list[str]
    authorization_project_ids: list[str]
    authorization_expires_at: str | None
    is_active: bool
    created_by_user_id: str | None
    created_at: str
    updated_at: str


class ProjectMainCharacterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str | None = Field(default=None, min_length=1)
    character_version_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def require_one_selection_source(self) -> ProjectMainCharacterRequest:
        if (self.character_id is None) == (self.character_version_id is None):
            raise ValueError("Provide exactly one character selection source.")
        return self


class ProjectMainCharacterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    character_id: str | None
    character_version_id: str | None
    version_id: str
    version_number: int
    character_snapshot: dict[str, object]


@router.get(
    "/projects/{project_id}/main-character",
    response_model=ProjectMainCharacterResponse | None,
)
def read_project_main_character_route(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ProjectMainCharacterResponse | None:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.main_character.read",
    )
    try:
        character = get_project_main_character(conn, project_id=project_id)
    except HTTPException as exc:
        if (
            exc.status_code == 404
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "MAIN_CHARACTER_NOT_FOUND"
        ):
            return None
        raise
    return ProjectMainCharacterResponse.model_validate(character)


@router.get("/characters", response_model=list[CharacterResponse])
def read_characters(
    conn: Database,
    actor: AuthenticatedUser,
    project_id: str | None = Query(default=None),
) -> list[CharacterResponse]:
    if actor.role in {"employee", "customer"} and project_id is not None:
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="character.read",
        )
    characters = list_characters(conn, actor=actor, project_id=project_id)
    return [character_response(character) for character in characters]


@router.get(
    "/projects/{project_id}/character-versions/available",
    response_model=list[ProjectCharacterVersionOption],
)
def read_available_project_character_versions(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[ProjectCharacterVersionOption]:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.main_character.options.read",
    )
    return list_available_project_character_versions(conn, actor=actor, project_id=project_id)


@router.post("/characters", response_model=CharacterResponse, status_code=status.HTTP_201_CREATED)
def create_character_route(
    payload: CharacterCreateRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterResponse:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action="character.create",
        entity_type="character",
        entity_id="collection",
    )
    character = create_character(
        conn,
        actor=actor,
        name=payload.name,
        reference_asset_ids=payload.reference_asset_ids,
        authorization_project_ids=payload.authorization_project_ids,
        authorization_expires_at=payload.authorization_expires_at,
        is_active=payload.is_active,
    )
    return character_response(character)


@router.get("/characters/{character_id}", response_model=CharacterResponse)
def read_character_route(
    character_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    project_id: str | None = Query(default=None),
) -> CharacterResponse:
    if actor.role in {"employee", "customer"} and project_id is not None:
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="character.read",
        )
    character = get_character(conn, character_id=character_id, actor=actor, project_id=project_id)
    return character_response(character)


@router.patch("/characters/{character_id}", response_model=CharacterResponse)
def update_character_route(
    character_id: str,
    payload: CharacterUpdateRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterResponse:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action="character.update",
        entity_type="character",
        entity_id=character_id,
    )
    character = update_character(
        conn,
        actor=actor,
        character_id=character_id,
        name=payload.name,
        reference_asset_ids=payload.reference_asset_ids,
        authorization_project_ids=payload.authorization_project_ids,
        authorization_expires_at=payload.authorization_expires_at,
        clear_authorization_expires_at=payload.clear_authorization_expires_at,
        is_active=payload.is_active,
    )
    return character_response(character)


@router.delete("/characters/{character_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_character_route(
    character_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> Response:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action="character.delete",
        entity_type="character",
        entity_id=character_id,
    )
    delete_character(conn, actor=actor, character_id=character_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/projects/{project_id}/main-character",
    response_model=ProjectMainCharacterResponse,
)
def choose_main_character_route(
    project_id: str,
    payload: ProjectMainCharacterRequest,
    db: BusinessDbDep,
) -> ProjectMainCharacterResponse:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="project.main_character.choose",
            entity_type="project",
            entity_id=project_id,
        )
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="project.main_character.choose",
        )
        if payload.character_version_id is not None:
            result = choose_project_character_version(
                conn,
                actor=actor,
                project_id=project_id,
                character_version_id=payload.character_version_id,
            )
        else:
            assert payload.character_id is not None
            result = choose_project_main_character(
                conn,
                actor=actor,
                project_id=project_id,
                character_id=payload.character_id,
            )
        return ProjectMainCharacterResponse.model_validate(result)


def character_response(character: CharacterData) -> CharacterResponse:
    return CharacterResponse(
        id=character.id,
        name=character.name,
        reference_asset_ids=character.reference_asset_ids,
        authorization_project_ids=character.authorization_project_ids,
        authorization_expires_at=character.authorization_expires_at,
        is_active=character.is_active,
        created_by_user_id=character.created_by_user_id,
        created_at=character.created_at,
        updated_at=character.updated_at,
    )
