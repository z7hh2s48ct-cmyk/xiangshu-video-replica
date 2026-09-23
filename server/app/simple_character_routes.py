"""Routes for the simple character upload flow (方案 A: 极简人物库)."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Annotated
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app import content_store
from app.async_compat import reject_legacy_sync_operation
from app.auth import AuthenticatedUser, Database
from app.character_asset_review import cleanup_publication_objects
from app.character_contracts import PersonIdentity, RequiredCharacterViewType
from app.character_identity import character_error, read_identity_row, required_text
from app.character_identity_routes import get_character_storage
from app.customer_fence import BusinessDbDep
from app.first_frame_routes import get_image_provider
from app.first_frames import ImageProvider
from app.image_tasks import (
    enqueue_character_sheet_task,
    latest_base_character_sheet_task,
    latest_scene_look_task,
    load_image_task,
    require_character_sheet_task_access,
)
from app.media_routes import storage_for_asset
from app.permissions import require_not_auditor, require_project_access, write_audit
from app.simple_character import (
    SIMPLE_UPLOAD_ALLOWED_TYPES,
    SIMPLE_UPLOAD_MAX_BYTES,
    PreparedSimpleCharacterPublication,
    cleanup_deleted_character_objects,
    create_simple_character,
    delete_simple_character_identity,
    list_simple_library_page,
    list_simple_scene_looks_page,
    prepare_simple_character_generation,
    regenerate_simple_character_contact_sheet,
    rename_simple_character_identity,
    store_simple_character_publication,
    update_simple_character_profile,
    validate_simple_character_source,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    StoragePermissionError,
    storage_object_ref_from_uri,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simple-characters", tags=["simple characters"])

InjectedImageProvider = Annotated[ImageProvider, Depends(get_image_provider)]


def _best_effort_delete_task_input(storage: StorageAdapter, storage_uri: str) -> None:
    try:
        content_store.delete_object_outside_content_namespace(
            storage, storage_object_ref_from_uri(storage_uri).key
        )
    except (OSError, StorageBackendUnavailable, StoragePermissionError, ValueError):
        logger.warning("unable to clean temporary character task input", exc_info=True)


class SimpleUploadIntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generate_url: str
    method: str
    max_size_bytes: int
    allowed_content_types: list[str]
    required_form_fields: list[str]
    task_status_url_template: str


class SimpleCharacterViewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_type: RequiredCharacterViewType
    asset_id: str


class SimpleCharacterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    persona_id: str
    character_version_id: str
    publication_hash: str
    contact_sheet_asset_id: str
    generation_source: str
    views: list[SimpleCharacterViewResponse]


class SimpleLibraryEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    persona_id: str | None
    version_number: int | None
    display_name: str
    role: str
    service_scope: str
    target_audience: str
    expression_style: str
    audience_needs: str = ""
    factual_background: str = ""
    sample_script: str = ""
    forbidden_claims: str = ""
    owner_user_id: str | None
    status: str
    contact_sheet_asset_id: str | None
    generation_source: str | None
    scene_look_count: int
    views: list[SimpleCharacterViewResponse]


class SimpleLibraryPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SimpleLibraryEntryResponse]
    next_cursor: str | None
    total: int


class SimpleCharacterRegenerationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    persona_id: str
    character_version_id: str
    previous_version_id: str
    version_number: int
    publication_hash: str
    contact_sheet_asset_id: str
    generation_source: str
    views: list[SimpleCharacterViewResponse]


class SimpleSceneLookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    persona_id: str
    character_version_id: str
    scene_name: str
    scene_description: str
    costume_description: str
    contact_sheet_asset_id: str
    generation_source: str
    views: list[SimpleCharacterViewResponse]
    published_at: str | None = None


class SimpleSceneLookPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SimpleSceneLookResponse]
    total: int
    limit: int
    offset: int


class SimpleSceneLookCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_name: str = Field(min_length=1, max_length=80)
    scene_description: str = Field(min_length=1, max_length=600)
    costume_description: str = Field(min_length=1, max_length=600)
    idempotency_key: str = Field(min_length=8, max_length=128)


class IdentityRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str


class SimpleCharacterProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(max_length=120)
    role: str = Field(max_length=160)
    service_scope: str = Field(max_length=600)
    target_audience: str = Field(max_length=600)
    expression_style: str = Field(max_length=600)
    audience_needs: str | None = Field(default=None, max_length=600)
    factual_background: str | None = Field(default=None, max_length=2000)
    sample_script: str | None = Field(default=None, max_length=2000)
    forbidden_claims: str | None = Field(default=None, max_length=600)


class CharacterSheetTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str | None
    identity_id: str | None
    operation: str
    display_name: str
    status: str
    attempt: int
    result_identity_id: str | None
    result_version_id: str | None
    result: dict[str, object] | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


def require_async_global_character_route() -> None:
    reject_legacy_sync_operation(
        replacement="/api/simple-characters/tasks/generate",
    )


def require_async_project_character_route(project_id: str) -> None:
    reject_legacy_sync_operation(
        replacement=f"/api/simple-characters/tasks/{project_id}/generate",
    )


def require_async_character_regeneration_route(identity_id: str) -> None:
    reject_legacy_sync_operation(
        replacement=(
            f"/api/simple-characters/identities/{identity_id}/regenerate-contact-sheet-task"
        ),
    )


@router.post("/upload-intent", response_model=SimpleUploadIntentResponse)
def create_simple_upload_intent(
    actor: AuthenticatedUser,
) -> SimpleUploadIntentResponse:
    """Describe how the client should upload a simple character source image.

    The simple flow uploads the image directly with the asynchronous task
    endpoint as multipart form data, so the intent echoes the enqueue contract
    and limits instead of issuing a presigned URL.
    """
    return SimpleUploadIntentResponse(
        generate_url="/api/simple-characters/tasks/generate",
        method="POST (multipart/form-data)",
        max_size_bytes=SIMPLE_UPLOAD_MAX_BYTES,
        allowed_content_types=sorted(SIMPLE_UPLOAD_ALLOWED_TYPES),
        required_form_fields=[
            "file",
            "display_name",
            "idempotency_key",
            "image_consent_version",
            "image_consent_accepted",
        ],
        task_status_url_template="/api/simple-characters/task-status/{task_id}",
    )


@router.post(
    "/generate",
    response_model=SimpleCharacterResponse,
    status_code=201,
    dependencies=[Depends(require_async_global_character_route)],
)
async def generate_global_simple_character(
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    provider: InjectedImageProvider,
    db: BusinessDbDep,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> SimpleCharacterResponse:
    with db.write() as (conn, actor):
        """Global one-click character creation (人物库精简流程，无项目上下文).

        Mirrors the project-scoped endpoint but skips ``require_project_access``:
        the character library page has no project context, and the creator's
        identity ownership is recorded for later renames.
        """
        require_not_auditor(
            conn,
            actor=actor,
            action="simple_character.create",
            entity_type="character_version",
            entity_id="collection",
        )
    (
        content,
        content_type,
        effective_persona_name,
        prepared,
    ) = await _prepare_simple_character_upload(
        file=file,
        display_name=display_name,
        persona_name=persona_name,
        provider=provider,
        actor=actor,
        storage=storage,
    )
    try:
        with db.write() as (conn, actor):
            return await _run_simple_character_creation(
                conn=conn,
                actor=actor,
                storage=storage,
                content=content,
                content_type=content_type,
                display_name=display_name,
                persona_name=effective_persona_name,
                project_id=None,
                prepared_publication=prepared,
            )
    except Exception:
        cleanup_publication_objects(storage, list(prepared.object_keys))
        raise


@router.post(
    "/tasks/generate",
    response_model=CharacterSheetTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_global_simple_character(
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    db: BusinessDbDep,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
    image_consent_version: Annotated[str, Form()] = "",
    image_consent_accepted: Annotated[bool, Form()] = False,
) -> CharacterSheetTaskResponse:
    return await _enqueue_simple_character_upload(
        storage=storage,
        db=db,
        file=file,
        display_name=display_name,
        idempotency_key=idempotency_key,
        persona_name=persona_name,
        project_id=None,
        image_consent_version=image_consent_version,
        image_consent_accepted=image_consent_accepted,
    )


@router.post(
    "/tasks/{project_id}/generate",
    response_model=CharacterSheetTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_project_simple_character(
    project_id: str,
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    db: BusinessDbDep,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
    image_consent_version: Annotated[str, Form()] = "",
    image_consent_accepted: Annotated[bool, Form()] = False,
) -> CharacterSheetTaskResponse:
    return await _enqueue_simple_character_upload(
        storage=storage,
        db=db,
        file=file,
        display_name=display_name,
        idempotency_key=idempotency_key,
        persona_name=persona_name,
        project_id=project_id,
        image_consent_version=image_consent_version,
        image_consent_accepted=image_consent_accepted,
    )


@router.get("/library", response_model=SimpleLibraryPageResponse)
def read_simple_library(
    conn: Database,
    actor: AuthenticatedUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 12,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    query: Annotated[str, Query(max_length=100)] = "",
) -> SimpleLibraryPageResponse:
    """List characters with their contact sheet and five-view asset ids."""
    page = list_simple_library_page(
        conn,
        actor=actor,
        limit=limit,
        cursor=cursor,
        query=query,
    )
    items = [
        SimpleLibraryEntryResponse(
            identity_id=entry.identity_id,
            persona_id=entry.persona_id,
            version_number=entry.version_number,
            display_name=entry.display_name,
            role=entry.role,
            service_scope=entry.service_scope,
            target_audience=entry.target_audience,
            expression_style=entry.expression_style,
            audience_needs=entry.audience_needs,
            factual_background=entry.factual_background,
            sample_script=entry.sample_script,
            forbidden_claims=entry.forbidden_claims,
            owner_user_id=entry.owner_user_id,
            status=entry.status,
            contact_sheet_asset_id=entry.contact_sheet_asset_id,
            generation_source=entry.generation_source,
            scene_look_count=entry.scene_look_count,
            views=[
                SimpleCharacterViewResponse(
                    view_type=view.view_type,
                    asset_id=view.asset_id,
                )
                for view in entry.views
            ],
        )
        for entry in page.items
    ]
    return SimpleLibraryPageResponse(
        items=items,
        next_cursor=page.next_cursor,
        total=page.total,
    )


@router.get(
    "/identities/{identity_id}/scene-looks",
    response_model=SimpleSceneLookPageResponse,
)
def read_scene_looks(
    identity_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 12,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SimpleSceneLookPageResponse:
    page = list_simple_scene_looks_page(
        conn,
        actor=actor,
        identity_id=identity_id,
        limit=limit,
        offset=offset,
    )
    items = [
        SimpleSceneLookResponse(
            identity_id=look.identity_id,
            persona_id=look.persona_id,
            character_version_id=look.character_version_id,
            scene_name=look.scene_name,
            scene_description=look.scene_description,
            costume_description=look.costume_description,
            contact_sheet_asset_id=look.contact_sheet_asset_id,
            generation_source=look.generation_source,
            views=[
                SimpleCharacterViewResponse(
                    view_type=view.view_type,
                    asset_id=view.asset_id,
                )
                for view in look.views
            ],
            published_at=look.published_at,
        )
        for look in page.items
    ]
    return SimpleSceneLookPageResponse(
        items=items,
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/identities/{identity_id}/scene-looks/tasks/generate",
    response_model=CharacterSheetTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_scene_look(
    identity_id: str,
    payload: SimpleSceneLookCreateRequest,
    db: BusinessDbDep,
) -> CharacterSheetTaskResponse:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="simple_character.scene_look.create",
            entity_type="character_version",
            entity_id=identity_id,
        )
        identity = read_identity_row(conn, identity_id)
        if actor.role != "admin" and str(identity["owner_user_id"]) != actor.id:
            raise character_error(
                404,
                "PERSON_IDENTITY_NOT_FOUND",
                "人物身份不存在或不可用。",
            )
        if str(identity["status"]) == "ARCHIVED":
            raise character_error(
                409,
                "IDENTITY_ARCHIVED",
                "已归档人物身份不能新增场景造型。",
            )
        scene_name = required_text(
            payload.scene_name,
            "SCENE_LOOK_NAME_REQUIRED",
            "场景名称不能为空。",
        )
        scene_description = required_text(
            payload.scene_description,
            "SCENE_LOOK_DESCRIPTION_REQUIRED",
            "场景描述不能为空。",
        )
        costume_description = required_text(
            payload.costume_description,
            "SCENE_LOOK_COSTUME_REQUIRED",
            "服装描述不能为空。",
        )
        source_asset = conn.execute(
            "SELECT storage_uri, content_type, sha256, size_bytes FROM assets WHERE id = %s",
            (str(identity["source_asset_id"]),),
        ).fetchone()
        if source_asset is None:
            raise character_error(
                409,
                "SIMPLE_CHARACTER_SOURCE_MISSING",
                "人物缺少原始授权照片。",
            )
        row = enqueue_character_sheet_task(
            conn,
            actor=actor,
            operation="SCENE",
            project_id=None,
            identity_id=identity_id,
            display_name=str(identity["display_name"]),
            persona_name=scene_name,
            source_storage_uri=str(source_asset["storage_uri"]),
            source_content_type=str(source_asset["content_type"]),
            source_sha256=str(source_asset["sha256"]),
            source_size_bytes=int(source_asset["size_bytes"]),
            idempotency_key=payload.idempotency_key,
            scene_description=scene_description,
            costume_description=costume_description,
        )
        return character_sheet_task_response(row)


@router.get(
    "/identities/{identity_id}/scene-looks/tasks/active-or-latest",
    response_model=CharacterSheetTaskResponse | None,
)
def read_latest_scene_look_task(
    identity_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterSheetTaskResponse | None:
    identity = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(identity["owner_user_id"]) != actor.id:
        raise character_error(404, "PERSON_IDENTITY_NOT_FOUND", "人物身份不存在或不可用。")
    row = latest_scene_look_task(conn, actor=actor, identity_id=identity_id)
    return None if row is None else character_sheet_task_response(row)


@router.patch("/identities/{identity_id}/name")
def rename_identity(
    identity_id: str,
    request: IdentityRenameRequest,
    db: BusinessDbDep,
) -> PersonIdentity:
    with db.write() as (conn, actor):
        """Rename a character identity (owner or admin only)."""
        return rename_simple_character_identity(
            conn,
            actor=actor,
            identity_id=identity_id,
            display_name=request.display_name,
        )


@router.patch(
    "/identities/{identity_id}/profile",
    response_model=SimpleLibraryEntryResponse,
)
def update_identity_profile(
    identity_id: str,
    request: SimpleCharacterProfileRequest,
    db: BusinessDbDep,
) -> SimpleLibraryEntryResponse:
    with db.write() as (conn, actor):
        entry = update_simple_character_profile(
            conn,
            actor=actor,
            identity_id=identity_id,
            display_name=request.display_name,
            role=request.role,
            service_scope=request.service_scope,
            target_audience=request.target_audience,
            expression_style=request.expression_style,
            audience_needs=request.audience_needs,
            factual_background=request.factual_background,
            sample_script=request.sample_script,
            forbidden_claims=request.forbidden_claims,
        )
        return SimpleLibraryEntryResponse(
            identity_id=entry.identity_id,
            persona_id=entry.persona_id,
            version_number=entry.version_number,
            display_name=entry.display_name,
            role=entry.role,
            service_scope=entry.service_scope,
            target_audience=entry.target_audience,
            expression_style=entry.expression_style,
            audience_needs=entry.audience_needs,
            factual_background=entry.factual_background,
            sample_script=entry.sample_script,
            forbidden_claims=entry.forbidden_claims,
            owner_user_id=entry.owner_user_id,
            status=entry.status,
            contact_sheet_asset_id=entry.contact_sheet_asset_id,
            generation_source=entry.generation_source,
            scene_look_count=entry.scene_look_count,
            views=[
                SimpleCharacterViewResponse(
                    view_type=view.view_type,
                    asset_id=view.asset_id,
                )
                for view in entry.views
            ],
        )


@router.post(
    "/identities/{identity_id}/regenerate-contact-sheet",
    response_model=SimpleCharacterRegenerationResponse,
    status_code=201,
    dependencies=[Depends(require_async_character_regeneration_route)],
)
def regenerate_contact_sheet(
    identity_id: str,
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    provider: InjectedImageProvider,
    db: BusinessDbDep,
) -> SimpleCharacterRegenerationResponse:
    with db.write() as (conn, actor):
        """Re-run the five-view contact sheet from the original source photo.

        Reuses the identity's stored authorization photo with the same
        identity-preserve prompt, publishes the result as the next character
        version, and keeps the previous published version untouched so projects
        already bound to it continue to work.
        """
        require_not_auditor(
            conn,
            actor=actor,
            action="simple_character.regenerate",
            entity_type="character_version",
            entity_id=identity_id,
        )
        try:
            result = regenerate_simple_character_contact_sheet(
                conn,
                actor=actor,
                identity_id=identity_id,
                storage=storage,
                image_provider=provider,
            )
        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.exception("Simple character regeneration failed unexpectedly")
            raise character_error(
                500,
                "SIMPLE_CHARACTER_REGENERATION_FAILED",
                "重新生成五视图失败，请稍后重试。",
            ) from exc
        return SimpleCharacterRegenerationResponse(
            identity_id=result.identity_id,
            persona_id=result.persona_id,
            character_version_id=result.character_version_id,
            previous_version_id=result.previous_version_id,
            version_number=result.version_number,
            publication_hash=result.publication_hash,
            contact_sheet_asset_id=result.contact_sheet_asset_id,
            generation_source=result.generation_source,
            views=[
                SimpleCharacterViewResponse(
                    view_type=view.view_type,
                    asset_id=view.asset_id,
                )
                for view in result.views
            ],
        )


@router.post(
    "/identities/{identity_id}/regenerate-contact-sheet-task",
    response_model=CharacterSheetTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_regenerate_contact_sheet(
    identity_id: str,
    idempotency_key: Annotated[str, Form()],
    db: BusinessDbDep,
) -> CharacterSheetTaskResponse:
    if len(idempotency_key.strip()) < 8:
        raise character_error(422, "IDEMPOTENCY_KEY_REQUIRED", "请重新提交生成请求。")
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="simple_character.regenerate",
            entity_type="character_version",
            entity_id=identity_id,
        )
        identity = read_identity_row(conn, identity_id)
        if actor.role != "admin" and str(identity["owner_user_id"]) != actor.id:
            raise character_error(
                404,
                "PERSON_IDENTITY_NOT_FOUND",
                "人物身份不存在或不可用。",
            )
        source_asset = conn.execute(
            "SELECT storage_uri, content_type, sha256, size_bytes FROM assets WHERE id = %s",
            (str(identity["source_asset_id"]),),
        ).fetchone()
        if source_asset is None:
            raise character_error(409, "SIMPLE_CHARACTER_SOURCE_MISSING", "人物缺少原始授权照片。")
        row = enqueue_character_sheet_task(
            conn,
            actor=actor,
            operation="REGENERATE",
            project_id=None,
            identity_id=identity_id,
            display_name=str(identity["display_name"]),
            persona_name=str(identity["display_name"]),
            source_storage_uri=str(source_asset["storage_uri"]),
            source_content_type=str(source_asset["content_type"]),
            source_sha256=str(source_asset["sha256"]),
            source_size_bytes=int(source_asset["size_bytes"]),
            idempotency_key=idempotency_key.strip(),
        )
        return character_sheet_task_response(row)


@router.get("/task-status/{task_id}", response_model=CharacterSheetTaskResponse)
def read_character_sheet_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterSheetTaskResponse:
    row = load_image_task(conn, table="character_sheet_tasks", task_id=task_id)
    require_character_sheet_task_access(actor=actor, row=row)
    return character_sheet_task_response(row)


@router.get("/tasks/active-or-latest", response_model=CharacterSheetTaskResponse | None)
def read_latest_character_sheet_task(
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterSheetTaskResponse | None:
    row = latest_base_character_sheet_task(conn, owner_id=actor.id)
    return None if row is None else character_sheet_task_response(row)


@router.delete("/identities/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_identity(
    identity_id: str,
    db: BusinessDbDep,
) -> Response:
    cleanup_plan = None
    delete_actor = None
    with db.write() as (conn, actor):
        """Delete a character identity with all derived assets (owner or admin)."""
        cleanup_plan = delete_simple_character_identity(
            conn,
            actor=actor,
            identity_id=identity_id,
            storage_for_uri=storage_for_asset,
        )
        delete_actor = actor

    assert cleanup_plan is not None and delete_actor is not None
    cleanup_result = cleanup_deleted_character_objects(cleanup_plan)
    try:
        with db.write() as (conn, _actor):
            write_audit(
                conn,
                actor=delete_actor,
                action="simple_character.delete.storage_cleanup",
                entity_type="person_identity",
                entity_id=identity_id,
                metadata={
                    "planned_count": len(cleanup_plan.targets),
                    "deleted_count": cleanup_result.deleted_count,
                    "failed_count": cleanup_result.failed_count,
                },
            )
    except Exception:  # noqa: BLE001 - primary database deletion already committed
        logger.warning(
            "unable to audit character storage cleanup identity=%s",
            identity_id,
            exc_info=True,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{project_id}/generate",
    response_model=SimpleCharacterResponse,
    status_code=201,
    dependencies=[Depends(require_async_project_character_route)],
)
async def generate_simple_character(
    project_id: str,
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    provider: InjectedImageProvider,
    db: BusinessDbDep,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> SimpleCharacterResponse:
    with db.write() as (conn, actor):
        """Upload one authorization image and publish a five-view character.

        The image is stored as both the authorization proof and the source asset,
        a single five-view contact sheet plus the five standard views are
        generated, auto-approved, and the resulting character version is
        published so it immediately appears in the project's available character
        version list.
        """
        require_not_auditor(
            conn,
            actor=actor,
            action="simple_character.create",
            entity_type="character_version",
            entity_id="collection",
        )
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="simple_character.create",
        )
    (
        content,
        content_type,
        effective_persona_name,
        prepared,
    ) = await _prepare_simple_character_upload(
        file=file,
        display_name=display_name,
        persona_name=persona_name,
        provider=provider,
        actor=actor,
        storage=storage,
    )
    try:
        with db.write() as (conn, actor):
            return await _run_simple_character_creation(
                conn=conn,
                actor=actor,
                storage=storage,
                content=content,
                content_type=content_type,
                display_name=display_name,
                persona_name=effective_persona_name,
                project_id=project_id,
                prepared_publication=prepared,
            )
    except Exception:
        cleanup_publication_objects(storage, list(prepared.object_keys))
        raise


async def _prepare_simple_character_upload(
    *,
    file: UploadFile,
    display_name: str,
    persona_name: str,
    provider: ImageProvider,
    actor: AuthenticatedUser,
    storage: StorageAdapter,
) -> tuple[bytes, str, str, PreparedSimpleCharacterPublication]:
    """Render and archive every character object with no session row locked."""

    if file.size is not None and file.size > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_TOO_LARGE",
            "人物授权图片超过 10MB 限制。",
        )
    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    effective_persona_name = persona_name.strip() or display_name.strip()
    generation = await run_in_threadpool(
        prepare_simple_character_generation,
        source_content=content,
        source_content_type=content_type,
        display_name=display_name,
        image_provider=provider,
    )
    prepared = await run_in_threadpool(
        store_simple_character_publication,
        actor=actor,
        storage=storage,
        source_content=content,
        source_content_type=content_type,
        display_name=display_name,
        generation=generation,
    )
    return content, content_type, effective_persona_name, prepared


async def _run_simple_character_creation(
    *,
    conn: Database,
    actor: AuthenticatedUser,
    storage: StorageAdapter,
    content: bytes,
    content_type: str,
    display_name: str,
    persona_name: str,
    project_id: str | None,
    prepared_publication: PreparedSimpleCharacterPublication,
) -> SimpleCharacterResponse:
    """Shared body of the global and project-scoped generate endpoints."""
    try:
        # Provider, image and object-storage work is already complete. Keep
        # the short database publication off the FastAPI event loop as well.
        result = await run_in_threadpool(
            create_simple_character,
            conn,
            actor=actor,
            project_id=project_id,
            storage=storage,
            source_content=content,
            source_content_type=content_type,
            display_name=display_name,
            persona_name=persona_name,
            prepared_publication=prepared_publication,
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Simple character generation failed unexpectedly")
        raise character_error(
            500,
            "SIMPLE_CHARACTER_GENERATION_FAILED",
            "一键生成人物失败，请稍后重试。",
        ) from exc
    return SimpleCharacterResponse(
        identity_id=result.identity_id,
        persona_id=result.persona_id,
        character_version_id=result.character_version_id,
        publication_hash=result.publication_hash,
        contact_sheet_asset_id=result.contact_sheet_asset_id,
        generation_source=result.generation_source,
        views=[
            SimpleCharacterViewResponse(
                view_type=view.view_type,
                asset_id=view.asset_id,
            )
            for view in result.views
        ],
    )


async def _enqueue_simple_character_upload(
    *,
    storage: StorageAdapter,
    db: BusinessDbDep,
    file: UploadFile,
    display_name: str,
    idempotency_key: str,
    persona_name: str,
    project_id: str | None,
    image_consent_version: str,
    image_consent_accepted: bool,
) -> CharacterSheetTaskResponse:
    if not image_consent_accepted or image_consent_version != "2026-09-14-v1":
        raise character_error(
            422, "IMAGE_AUTHORIZATION_REQUIRED", "请先阅读并确认人物图像使用授权。"
        )
    clean_name = display_name.strip()
    clean_key = idempotency_key.strip()
    if not clean_name:
        raise character_error(422, "SIMPLE_CHARACTER_NAME_REQUIRED", "人物名称不能为空。")
    if len(clean_key) < 8:
        raise character_error(422, "IDEMPOTENCY_KEY_REQUIRED", "请重新提交生成请求。")

    # Reject unauthorized callers before reading or persisting their upload.
    # Authorization is checked again in the enqueue transaction below to
    # protect the interval between this preflight and the database write.
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="simple_character.task.create",
            entity_type="character_sheet_task",
            entity_id=project_id or "collection",
        )
        if project_id is not None:
            require_project_access(
                conn,
                actor=actor,
                project_id=project_id,
                action="simple_character.task.create",
            )

    if file.size is not None and file.size > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422, "SIMPLE_CHARACTER_IMAGE_TOO_LARGE", "人物授权图片超过 10MB 限制。"
        )
    content = await file.read()
    if len(content) > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422, "SIMPLE_CHARACTER_IMAGE_TOO_LARGE", "人物授权图片超过 10MB 限制。"
        )
    content_type = (file.content_type or "application/octet-stream").split(";", 1)[0].lower()
    await run_in_threadpool(
        validate_simple_character_source,
        content,
        content_type,
        display_name,
    )
    content_sha256 = hashlib.sha256(content).hexdigest()
    task_input_id = str(uuid4())
    extension = SIMPLE_UPLOAD_ALLOWED_TYPES[content_type]
    input_key = f"users/task-inputs/{task_input_id}{extension}"
    stored = await run_in_threadpool(
        storage.put_object,
        input_key,
        content,
        content_type=content_type,
    )
    try:
        with db.write() as (conn, actor):
            require_not_auditor(
                conn,
                actor=actor,
                action="simple_character.task.create",
                entity_type="character_sheet_task",
                entity_id=task_input_id,
            )
            if project_id is not None:
                require_project_access(
                    conn,
                    actor=actor,
                    project_id=project_id,
                    action="simple_character.task.create",
                )
            row = enqueue_character_sheet_task(
                conn,
                actor=actor,
                operation="CREATE",
                project_id=project_id,
                identity_id=None,
                display_name=clean_name,
                persona_name=persona_name.strip() or clean_name,
                source_storage_uri=stored.uri,
                source_content_type=content_type,
                source_sha256=content_sha256,
                source_size_bytes=len(content),
                idempotency_key=clean_key,
                image_consent_version=image_consent_version,
            )
        if str(row["source_storage_uri"]) != stored.uri:
            _best_effort_delete_task_input(storage, stored.uri)
        return character_sheet_task_response(row)
    except Exception:
        _best_effort_delete_task_input(storage, stored.uri)
        raise


def character_sheet_task_response(row: sqlite3.Row) -> CharacterSheetTaskResponse:
    task = row
    request_payload = json.loads(str(task["request_json"]))
    result_payload = None if task["result_json"] is None else json.loads(str(task["result_json"]))
    if isinstance(result_payload, dict):
        result_payload = {
            key: value
            for key, value in result_payload.items()
            if key not in {"provider", "model", "execution"}
        }
    return CharacterSheetTaskResponse(
        id=str(task["id"]),
        project_id=(None if task["project_id"] is None else str(task["project_id"])),
        identity_id=(None if task["identity_id"] is None else str(task["identity_id"])),
        operation=str(task["operation"]),
        display_name=str(request_payload.get("display_name") or "人物"),
        status=str(task["status"]),
        attempt=int(task["attempt"]),
        result_identity_id=(
            None if task["result_identity_id"] is None else str(task["result_identity_id"])
        ),
        result_version_id=(
            None if task["result_version_id"] is None else str(task["result_version_id"])
        ),
        result=result_payload,
        error_code=(None if task["error_code"] is None else str(task["error_code"])),
        error_message=(
            None if task["error_message_redacted"] is None else str(task["error_message_redacted"])
        ),
        retryable=bool(task["retryable"]),
        created_at=str(task["created_at"]),
        updated_at=str(task["updated_at"]),
        started_at=(None if task["started_at"] is None else str(task["started_at"])),
        completed_at=(None if task["completed_at"] is None else str(task["completed_at"])),
    )
