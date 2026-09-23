from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.analysis import APILIO_DEFAULT_BASE_URL, APILIO_GEMINI_MODEL
from app.auth import AuthenticatedUser, CurrentUser, Database
from app.bootstrap import is_customer_production
from app.character_contracts import CharacterPersona, CharacterVersion, PersonIdentity
from app.character_identity import (
    ApilioSourceImageInspector,
    CompletedSourceImage,
    CreatedIdentityUploadIntent,
    FakeSourceImageInspector,
    SourceImageInspector,
    archive_character_version,
    complete_authorization_upload,
    complete_source_upload,
    create_character_persona,
    create_character_version,
    create_identity_upload_intent,
    create_person_identity,
    delete_character_persona,
    get_character_persona,
    get_character_version,
    get_person_identity,
    list_character_personas,
    list_character_versions,
    list_person_identities,
    update_character_persona,
    update_person_identity,
)
from app.customer_fence import BusinessReadConn
from app.media_routes import get_media_storage
from app.permissions import require_role
from app.settings import SettingsRepository, SettingsUnavailableError
from app.storage import StorageAdapter

router = APIRouter(prefix="/api", tags=["character-identities"])


class PersonIdentityCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    owner_user_id: str | None = None
    authorization_scope: list[str] = Field(min_length=1)
    authorization_expires_at: datetime | None = None


class PersonIdentityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    owner_user_id: str | None = None
    authorization_scope: list[str] | None = Field(default=None, min_length=1)
    authorization_expires_at: datetime | None = None
    authorization_status: Literal["REVOKED"] | None = None
    status: Literal["ARCHIVED"] | None = None


class IdentityUploadIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(gt=0)


class CompleteIdentityUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)


class CharacterPersonaCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    occupation: str | None = None
    scene_description: str | None = None
    appearance_constraints_json: dict[str, object] = Field(default_factory=dict)
    costume_description: str | None = None
    default_background: str | None = None
    positive_prompt: str | None = None
    negative_prompt: str | None = None
    usage_scope_json: list[str] = Field(default_factory=list)


class CharacterPersonaUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    occupation: str | None = None
    scene_description: str | None = None
    appearance_constraints_json: dict[str, object] | None = None
    costume_description: str | None = None
    default_background: str | None = None
    positive_prompt: str | None = None
    negative_prompt: str | None = None
    usage_scope_json: list[str] | None = None


class CharacterVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    generation_params_json: dict[str, object] = Field(default_factory=dict)


def get_character_storage(conn: BusinessReadConn) -> StorageAdapter:
    return get_media_storage(conn)


def get_source_image_inspector(conn: Database) -> SourceImageInspector:
    if os.environ.get("VIDEO_REPLICA_FAKE_SOURCE_IMAGE_INSPECTOR") == "1":
        if is_customer_production():
            raise HTTPException(
                status_code=503,
                detail={"code": "FAKE_SOURCE_IMAGE_INSPECTOR_FORBIDDEN"},
            )
        return FakeSourceImageInspector()
    has_saved_config = (
        conn.execute("SELECT 1 FROM provider_settings WHERE provider = 'apilio'").fetchone()
        is not None
    )
    try:
        config = SettingsRepository(conn).load_provider_config("apilio")
    except SettingsUnavailableError as exc:
        code = (
            "APILIO_SETTINGS_UNAVAILABLE"
            if has_saved_config
            else "SOURCE_IMAGE_INSPECTOR_NOT_CONFIGURED"
        )
        raise HTTPException(status_code=503, detail={"code": code}) from exc
    api_key = config.get("analysis_api_key") or config.get("api_key")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={"code": "SOURCE_IMAGE_INSPECTOR_NOT_CONFIGURED"},
        )
    return ApilioSourceImageInspector(
        api_key=api_key,
        base_url=APILIO_DEFAULT_BASE_URL,
        model=APILIO_GEMINI_MODEL,
    )


def get_character_admin(
    conn: Database,
    actor: AuthenticatedUser,
) -> CurrentUser:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action="character.manage",
        entity_type="character_domain",
        entity_id="collection",
    )
    return actor


CharacterStorage = Annotated[StorageAdapter, Depends(get_character_storage)]
CharacterAdmin = Annotated[CurrentUser, Depends(get_character_admin)]
InjectedSourceImageInspector = Annotated[
    SourceImageInspector,
    Depends(get_source_image_inspector),
]


def local_upload_intent(
    intent: CreatedIdentityUploadIntent,
    *,
    storage: StorageAdapter,
) -> CreatedIdentityUploadIntent:
    if storage.provider != "local":
        return intent
    return intent.model_copy(
        update={"url": f"/api/assets/local-objects/{quote(intent.storage_key, safe='/')}"}
    )


@router.get("/person-identities", response_model=list[PersonIdentity])
def read_person_identities(
    conn: Database,
    actor: AuthenticatedUser,
) -> list[PersonIdentity]:
    return list_person_identities(conn, actor=actor)


@router.post(
    "/person-identities",
    response_model=PersonIdentity,
    status_code=status.HTTP_201_CREATED,
)
def create_person_identity_route(
    payload: PersonIdentityCreateRequest,
    conn: Database,
    actor: CharacterAdmin,
) -> PersonIdentity:
    return create_person_identity(
        conn,
        actor=actor,
        display_name=payload.display_name,
        owner_user_id=payload.owner_user_id,
        authorization_scope=payload.authorization_scope,
        authorization_expires_at=payload.authorization_expires_at,
    )


@router.get("/person-identities/{identity_id}", response_model=PersonIdentity)
def read_person_identity(
    identity_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> PersonIdentity:
    return get_person_identity(conn, actor=actor, identity_id=identity_id)


@router.patch("/person-identities/{identity_id}", response_model=PersonIdentity)
def update_person_identity_route(
    identity_id: str,
    payload: PersonIdentityUpdateRequest,
    conn: Database,
    actor: CharacterAdmin,
) -> PersonIdentity:
    return update_person_identity(
        conn,
        actor=actor,
        identity_id=identity_id,
        updates=payload.model_dump(exclude_unset=True),
    )


def create_upload_intent_response(
    *,
    identity_id: str,
    purpose: Literal["authorization", "source"],
    payload: IdentityUploadIntentRequest,
    conn: Database,
    actor: AuthenticatedUser,
    storage: StorageAdapter,
) -> CreatedIdentityUploadIntent:
    intent = create_identity_upload_intent(
        conn,
        actor=actor,
        storage=storage,
        identity_id=identity_id,
        purpose=purpose,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
    )
    return local_upload_intent(intent, storage=storage)


@router.post(
    "/person-identities/{identity_id}/authorization-upload-intent",
    response_model=CreatedIdentityUploadIntent,
)
def create_authorization_upload_intent(
    identity_id: str,
    payload: IdentityUploadIntentRequest,
    conn: Database,
    actor: CharacterAdmin,
    storage: CharacterStorage,
) -> CreatedIdentityUploadIntent:
    return create_upload_intent_response(
        identity_id=identity_id,
        purpose="authorization",
        payload=payload,
        conn=conn,
        actor=actor,
        storage=storage,
    )


@router.post(
    "/person-identities/{identity_id}/authorization-upload-complete",
    response_model=PersonIdentity,
)
def complete_authorization_upload_route(
    identity_id: str,
    payload: CompleteIdentityUploadRequest,
    conn: Database,
    actor: CharacterAdmin,
    storage: CharacterStorage,
) -> PersonIdentity:
    return complete_authorization_upload(
        conn,
        actor=actor,
        storage=storage,
        identity_id=identity_id,
        asset_id=payload.asset_id,
    )


@router.post(
    "/person-identities/{identity_id}/source-upload-intent",
    response_model=CreatedIdentityUploadIntent,
)
def create_source_upload_intent(
    identity_id: str,
    payload: IdentityUploadIntentRequest,
    conn: Database,
    actor: CharacterAdmin,
    storage: CharacterStorage,
) -> CreatedIdentityUploadIntent:
    return create_upload_intent_response(
        identity_id=identity_id,
        purpose="source",
        payload=payload,
        conn=conn,
        actor=actor,
        storage=storage,
    )


@router.post(
    "/person-identities/{identity_id}/source-upload-complete",
    response_model=CompletedSourceImage,
)
def complete_source_upload_route(
    identity_id: str,
    payload: CompleteIdentityUploadRequest,
    conn: Database,
    actor: CharacterAdmin,
    storage: CharacterStorage,
    inspector: InjectedSourceImageInspector,
) -> CompletedSourceImage:
    return complete_source_upload(
        conn,
        actor=actor,
        storage=storage,
        inspector=inspector,
        identity_id=identity_id,
        asset_id=payload.asset_id,
    )


@router.get(
    "/person-identities/{identity_id}/personas",
    response_model=list[CharacterPersona],
)
def read_character_personas(
    identity_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[CharacterPersona]:
    return list_character_personas(conn, actor=actor, identity_id=identity_id)


@router.post(
    "/person-identities/{identity_id}/personas",
    response_model=CharacterPersona,
    status_code=status.HTTP_201_CREATED,
)
def create_character_persona_route(
    identity_id: str,
    payload: CharacterPersonaCreateRequest,
    conn: Database,
    actor: CharacterAdmin,
) -> CharacterPersona:
    return create_character_persona(
        conn,
        actor=actor,
        identity_id=identity_id,
        values=payload.model_dump(),
    )


@router.get("/character-personas/{persona_id}", response_model=CharacterPersona)
def read_character_persona(
    persona_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterPersona:
    return get_character_persona(conn, actor=actor, persona_id=persona_id)


@router.patch("/character-personas/{persona_id}", response_model=CharacterPersona)
def update_character_persona_route(
    persona_id: str,
    payload: CharacterPersonaUpdateRequest,
    conn: Database,
    actor: CharacterAdmin,
) -> CharacterPersona:
    return update_character_persona(
        conn,
        actor=actor,
        persona_id=persona_id,
        updates=payload.model_dump(exclude_unset=True),
    )


@router.delete(
    "/character-personas/{persona_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_character_persona_route(
    persona_id: str,
    conn: Database,
    actor: CharacterAdmin,
) -> Response:
    delete_character_persona(conn, actor=actor, persona_id=persona_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/character-personas/{persona_id}/versions",
    response_model=list[CharacterVersion],
)
def read_character_versions(
    persona_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[CharacterVersion]:
    return list_character_versions(conn, actor=actor, persona_id=persona_id)


@router.post(
    "/character-personas/{persona_id}/versions",
    response_model=CharacterVersion,
    status_code=status.HTTP_201_CREATED,
)
def create_character_version_route(
    persona_id: str,
    payload: CharacterVersionCreateRequest,
    conn: Database,
    actor: CharacterAdmin,
) -> CharacterVersion:
    return create_character_version(
        conn,
        actor=actor,
        persona_id=persona_id,
        provider=payload.provider,
        model=payload.model,
        generation_params_json=payload.generation_params_json,
    )


@router.get("/character-versions/{version_id}", response_model=CharacterVersion)
def read_character_version(
    version_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterVersion:
    return get_character_version(conn, actor=actor, version_id=version_id)


@router.post(
    "/character-versions/{version_id}/archive",
    response_model=CharacterVersion,
)
def archive_character_version_route(
    version_id: str,
    conn: Database,
    actor: CharacterAdmin,
) -> CharacterVersion:
    return archive_character_version(conn, actor=actor, version_id=version_id)
