"""Publish record routes (phase 2): queue, inspect and manage platform deliveries.

Reads ride the plain authenticated lane; every write goes through the T21
fenced ``BusinessDbDep.write()`` like the other studio routes. Responses never
carry credential material — accounts are referenced by id / username only.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.publish_records import (
    PublishRecordActionResponse,
    PublishRecordCreateRequest,
    PublishRecordDeleteResponse,
    PublishRecordListResponse,
    PublishRecordResponse,
    PublishSummaryResponse,
    cancel_record,
    create_record,
    delete_record,
    get_record,
    list_records,
    request_sync,
    retry_record,
    summary,
)

router = APIRouter(prefix="/api/studio/publish/records")


@router.get("", response_model=PublishRecordListResponse)
def read_publish_records(
    conn: Database,
    actor: AuthenticatedUser,
    status: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> PublishRecordListResponse:
    return list_records(conn, actor_id=actor.id, status=status, platform=platform, limit=limit)


@router.get("/summary", response_model=PublishSummaryResponse)
def read_publish_summary(conn: Database, actor: AuthenticatedUser) -> PublishSummaryResponse:
    return summary(conn, actor_id=actor.id)


@router.post("", response_model=PublishRecordResponse)
def create_publish_record(
    request: PublishRecordCreateRequest, db: BusinessDbDep
) -> PublishRecordResponse:
    with db.write() as (conn, actor):
        return create_record(conn, actor=actor, request=request)


@router.get("/{record_id}", response_model=PublishRecordResponse)
def read_publish_record(
    record_id: str, conn: Database, actor: AuthenticatedUser
) -> PublishRecordResponse:
    return get_record(conn, actor_id=actor.id, record_id=record_id)


@router.post("/{record_id}/cancel", response_model=PublishRecordActionResponse)
def cancel_publish_record(record_id: str, db: BusinessDbDep) -> PublishRecordActionResponse:
    with db.write() as (conn, actor):
        return PublishRecordActionResponse(
            record=cancel_record(conn, actor=actor, record_id=record_id)
        )


@router.post("/{record_id}/retry", response_model=PublishRecordActionResponse)
def retry_publish_record(record_id: str, db: BusinessDbDep) -> PublishRecordActionResponse:
    with db.write() as (conn, actor):
        return PublishRecordActionResponse(
            record=retry_record(conn, actor=actor, record_id=record_id)
        )


@router.post("/{record_id}/sync", response_model=PublishRecordActionResponse)
def sync_publish_record(record_id: str, db: BusinessDbDep) -> PublishRecordActionResponse:
    with db.write() as (conn, actor):
        return PublishRecordActionResponse(
            record=request_sync(conn, actor=actor, record_id=record_id)
        )


@router.delete("/{record_id}", response_model=PublishRecordDeleteResponse)
def delete_publish_record(record_id: str, db: BusinessDbDep) -> PublishRecordDeleteResponse:
    with db.write() as (conn, actor):
        delete_record(conn, actor=actor, record_id=record_id)
    return PublishRecordDeleteResponse(deleted=True)
