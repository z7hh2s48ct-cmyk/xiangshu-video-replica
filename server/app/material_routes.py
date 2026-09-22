"""HTTP contract for the Studio material library."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.materials import (
    MaterialBulkRequest,
    MaterialBulkResult,
    MaterialGroupsResponse,
    MaterialItem,
    MaterialMediaType,
    MaterialPage,
    MaterialResolveResponse,
    MaterialSource,
    MaterialUpdateRequest,
    MaterialUploadIntentRequest,
    MaterialUploadIntentResponse,
    attach_video_thumbnail,
    bulk_update_materials,
    create_material_upload_intent,
    hide_material,
    list_material_groups,
    list_materials,
    persist_material_upload,
    prepare_material_upload,
    probe_material_upload,
    resolve_materials,
    update_material,
)
from app.media_routes import MediaStorage
from app.storage import StorageBackendUnavailable

router = APIRouter(prefix="/api/studio/materials", tags=["studio-materials"])


class MaterialResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_ids: list[str] = Field(min_length=1, max_length=100)


@router.get("", response_model=MaterialPage)
def read_materials(
    conn: Database,
    actor: AuthenticatedUser,
    media_type: MaterialMediaType | None = None,
    source: MaterialSource | None = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    group: Annotated[str | None, Query(max_length=80)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 24,
) -> MaterialPage:
    return list_materials(
        conn,
        actor=actor,
        media_type=media_type,
        source=source,
        query=q.strip() if q and q.strip() else None,
        page=page,
        page_size=page_size,
        group=group.strip() if group is not None else None,
    )


@router.get("/groups", response_model=MaterialGroupsResponse)
def read_material_groups(
    conn: Database,
    actor: AuthenticatedUser,
) -> MaterialGroupsResponse:
    return list_material_groups(conn, actor=actor)


@router.post("/resolve", response_model=MaterialResolveResponse)
def read_materials_by_id(
    request: MaterialResolveRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> MaterialResolveResponse:
    return resolve_materials(conn, actor=actor, material_ids=request.material_ids)


@router.post("/upload-intent", response_model=MaterialUploadIntentResponse)
def create_upload_intent(
    request: MaterialUploadIntentRequest,
    db: BusinessDbDep,
    storage: MediaStorage,
) -> MaterialUploadIntentResponse | JSONResponse:
    with db.write() as (conn, actor):
        intent = create_material_upload_intent(
            conn,
            actor=actor,
            storage=storage,
            request=request,
        )
        is_customer = actor.role == "customer"
    # A deduplicated intent carries no transfer: handing the client a local
    # upload URL would invite it to re-upload bytes that already exist.
    if storage.provider == "local" and intent.upload_required:
        intent = intent.model_copy(
            update={"url": f"/api/studio/materials/uploads/{intent.asset_id}/content"}
        )
    if is_customer:
        return JSONResponse(content=intent.model_dump(mode="json", exclude={"storage_key"}))
    return intent


@router.put("/uploads/{asset_id}/content", status_code=204)
async def put_local_material(
    asset_id: str,
    request: Request,
    conn: Database,
    actor: AuthenticatedUser,
    storage: MediaStorage,
) -> Response:
    if storage.provider != "local":
        raise HTTPException(status_code=404, detail={"code": "LOCAL_UPLOAD_UNAVAILABLE"})
    prepared = prepare_material_upload(conn, actor=actor, asset_id=asset_id, pending_only=True)
    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > prepared.requested_size_bytes
    ):
        raise HTTPException(status_code=413, detail={"code": "PAYLOAD_TOO_LARGE"})
    content_type = request.headers.get("content-type", "application/octet-stream")
    if content_type != prepared.content_type:
        raise HTTPException(status_code=415, detail={"code": "CONTENT_TYPE_MISMATCH"})
    content_buffer = bytearray()
    async for chunk in request.stream():
        if len(content_buffer) + len(chunk) > prepared.requested_size_bytes:
            raise HTTPException(status_code=413, detail={"code": "PAYLOAD_TOO_LARGE"})
        content_buffer.extend(chunk)
    content = bytes(content_buffer)
    if len(content) != prepared.requested_size_bytes:
        raise HTTPException(status_code=409, detail={"code": "UPLOAD_SIZE_MISMATCH"})
    try:
        storage.put_object(prepared.storage_key, content, content_type=content_type)
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        ) from exc
    return Response(status_code=204)


@router.post("/uploads/{asset_id}/complete", response_model=MaterialItem)
def complete_upload(
    asset_id: str,
    db: BusinessDbDep,
    storage: MediaStorage,
) -> MaterialItem:
    with db.write() as (conn, actor):
        prepared = prepare_material_upload(conn, actor=actor, asset_id=asset_id)
    try:
        probed = probe_material_upload(prepared, storage=storage)
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        ) from exc
    with db.write() as (conn, actor):
        item = persist_material_upload(conn, actor=actor, probed=probed, storage=storage)
    # MATERIAL-THUMBS-B：缩略图落存储与记键在写事务之外（dedup 后的最终对象键
    # 以持久化结果为准）；失败只损失缩略图，不影响上传结果。
    if probed.thumbnail_jpeg is not None:
        try:
            with db.write() as (conn, actor):
                attach_video_thumbnail(
                    conn,
                    asset_id=str(item.asset_id),
                    thumbnail_jpeg=probed.thumbnail_jpeg,
                    storage=storage,
                )
        except StorageBackendUnavailable:
            pass
    return item


@router.patch("/bulk", response_model=MaterialBulkResult)
def patch_materials_bulk(
    request: MaterialBulkRequest,
    db: BusinessDbDep,
) -> MaterialBulkResult:
    with db.write() as (conn, actor):
        return bulk_update_materials(conn, actor=actor, request=request)


@router.patch("/{material_id}", response_model=MaterialItem)
def patch_material(
    material_id: str,
    request: MaterialUpdateRequest,
    db: BusinessDbDep,
) -> MaterialItem:
    with db.write() as (conn, actor):
        return update_material(
            conn,
            actor=actor,
            material_id=material_id,
            request=request,
        )


@router.delete("/{material_id}", status_code=204)
def remove_material(material_id: str, db: BusinessDbDep) -> Response:
    with db.write() as (conn, actor):
        hide_material(conn, actor=actor, material_id=material_id)
    return Response(status_code=204)
