from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from _thread import LockType
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, cast
from urllib.parse import quote, urlencode
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app import content_store
from app.auth import (
    AuthenticatedUser,
    CurrentUser,
    Database,
    authenticate_request,
    identity_source,
)
from app.bootstrap import is_customer_production
from app.customer_fence import BusinessDbDep
from app.db_pg import DATABASE_URL_ENV, pg_transaction
from app.db_portable import BusinessConnection
from app.material_thumbs import THUMBNAIL_URL_EXPIRES_IN, thumbnail_key_for
from app.media import storage_key_from_uri
from app.media_routes import (
    api_base_url,
    signed_asset_session_epoch,
    storage_for_asset,
    validate_signed_asset_grant,
)
from app.permissions import (
    AuditedSecurityDenial,
    persist_security_denial,
    require_asset_access,
    require_not_auditor,
    require_project_access,
    require_role,
    write_audit,
)
from app.settings import SettingsRepository, SettingsUnavailableError, settings_encryption_key
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    cloud_storage_config_from_settings,
    create_storage_adapter,
    local_download_signature,
    local_storage_root,
)

router = APIRouter(prefix="/api", tags=["rbac"])
DOWNLOAD_URL_EXPIRES_IN = timedelta(minutes=15)
CHARACTER_CACHE_KINDS = frozenset(
    {
        "character_contact_sheet",
        "character_generated_image",
        "character_approved_image",
    }
)
CHARACTER_CACHE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
CHARACTER_CACHE_CONTENT_TYPES = {
    suffix: content_type for content_type, suffix in CHARACTER_CACHE_SUFFIXES.items()
}
CHARACTER_CACHE_NAME = re.compile(r"^[0-9a-f]{64}\.(?:jpg|png|webp)$")
CHARACTER_CACHE_LOCKS: dict[str, LockType] = {}
CHARACTER_CACHE_LOCKS_GUARD = Lock()
logger = logging.getLogger(__name__)


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class RenameProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class ProjectResponse(BaseModel):
    id: str
    owner_user_id: str
    name: str
    status: str
    reference_asset_id: str | None
    reference_upload_status: str
    analysis_status: str
    analysis_task_id: str | None = None
    analysis_error_message: str | None = None
    analysis_retryable: bool = False


class AssetResponse(BaseModel):
    id: str
    project_id: str | None
    kind: str
    sha256: str
    size_bytes: int
    content_type: str | None


class DownloadUrlResponse(BaseModel):
    url: str


@dataclass(frozen=True)
class CustomerCharacterCachePlan:
    asset_id: str
    cache_name: str
    content_type: str
    source_object_key: str
    expected_sha256: str
    source_storage: StorageAdapter
    shared_storage: StorageAdapter


def _character_cache_root() -> Path:
    # CW-031：本目录是可重建的本地临时处理中间文件缓存（原子落盘 + sha256 校验），
    # 不是跨进程持久真源——跨进程真源始终是 COS（见 _customer_character_cache_storage
    # 的 COS 强制）。整目录删除后业务必须能从 COS 完整恢复（不变量由
    # test_storage_cross_instance.py 钉住）；历史 local 持久资产的搬迁归 CW-037。
    home = os.environ.get("VIDEO_REPLICA_HOME", "").strip()
    if home:
        return (Path(home) / "storage-cache" / "character-images").resolve()
    return (local_storage_root() / ".cache" / "character-images").resolve()


def _character_cache_identity(row: sqlite3.Row) -> tuple[str, str]:
    kind = str(row["kind"])
    content_type = str(row["content_type"] or "").split(";", 1)[0].strip().lower()
    if kind not in CHARACTER_CACHE_KINDS or content_type not in CHARACTER_CACHE_SUFFIXES:
        raise HTTPException(
            status_code=409,
            detail={"code": "CHARACTER_CACHE_UNSUPPORTED"},
        )
    source_version = str(row["sha256"] or row["storage_uri"])
    digest = hashlib.sha256(f"{row['id']}:{source_version}".encode()).hexdigest()
    return f"{digest}{CHARACTER_CACHE_SUFFIXES[content_type]}", content_type


def _character_cache_path(cache_name: str) -> Path:
    _character_cache_object_key(cache_name)
    return _character_cache_root() / cache_name


def _character_cache_object_key(cache_name: str) -> str:
    if CHARACTER_CACHE_NAME.fullmatch(cache_name) is None:
        raise HTTPException(status_code=404, detail={"code": "CHARACTER_CACHE_NOT_FOUND"})
    return f"projects/character-cache/{cache_name}"


def _character_cache_lock(cache_name: str) -> LockType:
    with CHARACTER_CACHE_LOCKS_GUARD:
        return CHARACTER_CACHE_LOCKS.setdefault(cache_name, Lock())


def _customer_character_cache_storage(conn: BusinessConnection) -> StorageAdapter:
    try:
        repo = SettingsRepository(conn)
        runtime = repo.read_runtime_settings()
        if runtime.get("active_storage_provider") != "cos":
            raise StorageBackendUnavailable("customer character cache requires COS")
        config = repo.load_provider_config("cos")
        return create_storage_adapter(cloud_storage_config_from_settings("cos", config))
    except (SettingsUnavailableError, ValueError) as exc:
        raise StorageBackendUnavailable("customer character cache storage unavailable") from exc


def _load_customer_character_cache_storage() -> StorageAdapter:
    with pg_transaction() as raw_conn:
        return _customer_character_cache_storage(BusinessConnection.postgres(raw_conn))


def _validate_character_cache_grant(
    *,
    user_id: str,
    asset_id: str,
    session_epoch: str,
) -> None:
    """Use a short DB scope so a later COS read never holds a PG connection."""
    if os.environ.get(DATABASE_URL_ENV, "").strip():
        try:
            with pg_transaction() as raw_conn:
                validate_signed_asset_grant(
                    BusinessConnection.postgres(raw_conn),
                    user_id=user_id,
                    asset_id=asset_id,
                    session_epoch=session_epoch,
                )
        except AuditedSecurityDenial as exc:
            persist_security_denial(exc)
            raise
        return
    raise HTTPException(status_code=503, detail={"code": "DATABASE_NOT_CONFIGURED"})


def _read_verified_character_cache_source(
    *,
    source_storage: StorageAdapter,
    source_object_key: str,
    expected_sha256: str,
    asset_id: str,
) -> bytes:
    try:
        content = source_storage.get_object(source_object_key)
    except (KeyError, OSError, StorageBackendUnavailable) as exc:
        logger.error(
            "character cache source read failed for asset %s: %s",
            asset_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        ) from exc

    if expected_sha256 and not hmac.compare_digest(
        hashlib.sha256(content).hexdigest(),
        expected_sha256,
    ):
        logger.error("character cache source hash mismatch for asset %s", asset_id)
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        )
    return content


def _prepare_customer_character_cache(
    conn: BusinessConnection,
    row: sqlite3.Row,
) -> CustomerCharacterCachePlan:
    cache_name, content_type = _character_cache_identity(row)
    storage_uri = str(row["storage_uri"])
    try:
        shared_storage = _customer_character_cache_storage(conn)
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        ) from exc
    return CustomerCharacterCachePlan(
        asset_id=str(row["id"]),
        cache_name=cache_name,
        content_type=content_type,
        source_object_key=storage_key_from_uri(storage_uri),
        expected_sha256=str(row["sha256"] or "").lower(),
        source_storage=storage_for_asset(conn, storage_uri),
        shared_storage=shared_storage,
    )


def _populate_customer_character_cache(
    plan: CustomerCharacterCachePlan,
) -> tuple[str, str]:
    cache_key = _character_cache_object_key(plan.cache_name)
    with _character_cache_lock(plan.cache_name):
        try:
            if plan.shared_storage.head_object(cache_key) is not None:
                return plan.cache_name, plan.content_type
        except StorageBackendUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
            ) from exc
        content = _read_verified_character_cache_source(
            source_storage=plan.source_storage,
            source_object_key=plan.source_object_key,
            expected_sha256=plan.expected_sha256,
            asset_id=plan.asset_id,
        )
        try:
            # The deterministic key makes concurrent writes from separate
            # API replicas idempotent; the shared COS object is the cache.
            plan.shared_storage.put_object(
                cache_key,
                content,
                content_type=plan.content_type,
            )
        except StorageBackendUnavailable as exc:
            logger.error(
                "shared character cache write failed for asset %s: %s",
                plan.asset_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
            ) from exc
    return plan.cache_name, plan.content_type


def _populate_character_cache(
    conn: BusinessConnection,
    row: sqlite3.Row,
) -> tuple[str, str]:
    cache_name, content_type = _character_cache_identity(row)
    try:
        cache_path = _character_cache_path(cache_name)
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        ) from exc
    with _character_cache_lock(cache_name):
        if cache_path.is_file():
            return cache_name, content_type

        storage_uri = str(row["storage_uri"])
        content = _read_verified_character_cache_source(
            source_storage=storage_for_asset(conn, storage_uri),
            source_object_key=storage_key_from_uri(storage_uri),
            expected_sha256=str(row["sha256"] or "").lower(),
            asset_id=str(row["id"]),
        )

        # Keep the temporary basename short. Appending a full UUID to the
        # already hash-sized cache name exceeds the legacy Windows MAX_PATH
        # limit when the application home is nested deeply.
        temporary_path = cache_path.with_name(f".cache-{uuid4().hex[:12]}.tmp")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_bytes(content)
            temporary_path.replace(cache_path)
        except OSError as exc:
            logger.error(
                "character cache write failed for asset %s at %s via %s: %s",
                row["id"],
                cache_path,
                temporary_path,
                type(exc).__name__,
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
            ) from exc
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "character cache temporary cleanup failed for asset %s: %s",
                    row["id"],
                    type(exc).__name__,
                )
    return cache_name, content_type


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    actor_user_id: str | None
    action: str
    entity_type: str
    entity_id: str
    metadata_json: str
    created_at: str


@router.get("/auth/me", response_model=UserResponse)
def read_me(
    conn: Database,
    dev_user_id: Annotated[str | None, Header(alias="X-Dev-User-Id")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> UserResponse:
    try:
        actor = authenticate_request(
            conn,
            authorization=authorization,
            dev_user_id=dev_user_id,
        )
    except HTTPException as exc:
        write_login_failure(
            conn,
            error=exc,
            identity_source_name=identity_source(dev_user_id, authorization),
        )
        raise
    write_audit(
        conn,
        actor=actor,
        action="auth.login_success",
        entity_type="user",
        entity_id=actor.id,
    )
    return UserResponse(
        id=actor.id,
        username=actor.username,
        display_name=actor.display_name,
        role=actor.role,
    )


def write_login_failure(
    conn: BusinessConnection,
    *,
    error: HTTPException,
    identity_source_name: str,
) -> None:
    code = "AUTH_UNKNOWN"
    if isinstance(error.detail, dict) and isinstance(error.detail.get("code"), str):
        code = str(error.detail["code"])
    conn.execute(
        """
        INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, metadata_json)
        VALUES (%s, NULL, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            "auth.login_failure",
            "auth",
            "current_user",
            json.dumps(
                {"code": code, "identity_source": identity_source_name},
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )
    conn.commit()


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(
    conn: Database,
    actor: AuthenticatedUser,
) -> list[ProjectResponse]:
    if actor.role in {"admin", "auditor"}:
        rows = conn.execute(
            """
            SELECT
                projects.id,
                projects.owner_user_id,
                projects.name,
                projects.status,
                reference_assets.id AS reference_asset_id,
                CASE
                    WHEN reference_assets.id IS NULL THEN 'NOT_STARTED'
                    WHEN reference_assets.sha256 = '' OR reference_assets.size_bytes = 0
                        THEN 'UPLOAD_PENDING'
                    ELSE 'READY'
                END AS reference_upload_status,
                CASE
                    WHEN reference_assets.id IS NULL
                        OR reference_assets.sha256 = ''
                        OR reference_assets.size_bytes = 0
                        THEN 'NOT_READY'
                    WHEN EXISTS (
                        SELECT 1 FROM versions
                        WHERE versions.project_id = projects.id
                            AND versions.kind = 'analysis'
                            AND versions.asset_id = reference_assets.id
                    ) THEN 'READY'
                    WHEN latest_analysis_task.status IN ('PENDING', 'RUNNING')
                        THEN 'PENDING'
                    WHEN latest_analysis_task.status = 'FAILED' THEN 'FAILED'
                    ELSE 'NOT_READY'
                END AS analysis_status,
                latest_analysis_task.id AS analysis_task_id,
                latest_analysis_task.error_message_redacted AS analysis_error_message,
                COALESCE(latest_analysis_task.retryable, 0) AS analysis_retryable
            FROM projects
            LEFT JOIN assets AS reference_assets ON reference_assets.id = (
                SELECT assets.id
                FROM assets
                WHERE assets.project_id = projects.id
                    AND (
                        assets.kind = 'reference_video'
                        OR (
                            assets.kind = 'video'
                            AND assets.storage_uri LIKE (
                                '%%/projects/' || projects.id || '/uploads/%%'
                            )
                        )
                    )
                ORDER BY assets.created_at DESC, assets.id DESC
                LIMIT 1
            )
            LEFT JOIN analysis_tasks AS latest_analysis_task
                ON latest_analysis_task.id = (
                    SELECT analysis_tasks.id
                    FROM analysis_tasks
                    WHERE analysis_tasks.project_id = projects.id
                      AND analysis_tasks.asset_id = reference_assets.id
                    ORDER BY analysis_tasks.created_at DESC, analysis_tasks.id DESC
                    LIMIT 1
                )
            ORDER BY projects.created_at DESC, projects.id DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
                projects.id,
                projects.owner_user_id,
                projects.name,
                projects.status,
                reference_assets.id AS reference_asset_id,
                CASE
                    WHEN reference_assets.id IS NULL THEN 'NOT_STARTED'
                    WHEN reference_assets.sha256 = '' OR reference_assets.size_bytes = 0
                        THEN 'UPLOAD_PENDING'
                    ELSE 'READY'
                END AS reference_upload_status,
                CASE
                    WHEN reference_assets.id IS NULL
                        OR reference_assets.sha256 = ''
                        OR reference_assets.size_bytes = 0
                        THEN 'NOT_READY'
                    WHEN EXISTS (
                        SELECT 1 FROM versions
                        WHERE versions.project_id = projects.id
                            AND versions.kind = 'analysis'
                            AND versions.asset_id = reference_assets.id
                    ) THEN 'READY'
                    WHEN latest_analysis_task.status IN ('PENDING', 'RUNNING')
                        THEN 'PENDING'
                    WHEN latest_analysis_task.status = 'FAILED' THEN 'FAILED'
                    ELSE 'NOT_READY'
                END AS analysis_status,
                latest_analysis_task.id AS analysis_task_id,
                latest_analysis_task.error_message_redacted AS analysis_error_message,
                COALESCE(latest_analysis_task.retryable, 0) AS analysis_retryable
            FROM projects
            LEFT JOIN assets AS reference_assets ON reference_assets.id = (
                SELECT assets.id
                FROM assets
                WHERE assets.project_id = projects.id
                    AND (
                        assets.kind = 'reference_video'
                        OR (
                            assets.kind = 'video'
                            AND assets.storage_uri LIKE (
                                '%%/projects/' || projects.id || '/uploads/%%'
                            )
                        )
                    )
                ORDER BY assets.created_at DESC, assets.id DESC
                LIMIT 1
            )
            LEFT JOIN analysis_tasks AS latest_analysis_task
                ON latest_analysis_task.id = (
                    SELECT analysis_tasks.id
                    FROM analysis_tasks
                    WHERE analysis_tasks.project_id = projects.id
                      AND analysis_tasks.asset_id = reference_assets.id
                    ORDER BY analysis_tasks.created_at DESC, analysis_tasks.id DESC
                    LIMIT 1
                )
            WHERE projects.owner_user_id = %s
            ORDER BY projects.created_at DESC, projects.id DESC
            """,
            (actor.id,),
        ).fetchall()
    return [project_response(row) for row in rows]


@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    payload: CreateProjectRequest,
    db: BusinessDbDep,
) -> ProjectResponse:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="project.create",
            entity_type="project",
            entity_id="new",
        )
        name = payload.name.strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "PROJECT_NAME_REQUIRED", "message": "Project name is required."},
            )

        project_id = str(uuid4())
        with conn:
            conn.execute(
                """
                INSERT INTO projects (id, owner_user_id, name)
                VALUES (%s, %s, %s)
                """,
                (project_id, actor.id, name),
            )
        write_audit(
            conn,
            actor=actor,
            action="project.create",
            entity_type="project",
            entity_id=project_id,
            metadata={"name": name},
        )
        return ProjectResponse(
            id=project_id,
            owner_user_id=actor.id,
            name=name,
            status="ACTIVE",
            reference_asset_id=None,
            reference_upload_status="NOT_STARTED",
            analysis_status="NOT_READY",
        )


@router.patch("/projects/{project_id}/name", response_model=ProjectResponse)
def rename_project(
    project_id: str,
    payload: RenameProjectRequest,
    db: BusinessDbDep,
) -> ProjectResponse:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="project.rename",
            entity_type="project",
            entity_id=project_id,
        )
        current_row = require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="project.rename",
        )

        name = payload.name.strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "PROJECT_NAME_REQUIRED", "message": "Project name is required."},
            )
        with conn:
            conn.execute(
                """
                UPDATE projects
                SET name = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (name, project_id),
            )
        write_audit(
            conn,
            actor=actor,
            action="project.rename",
            entity_type="project",
            entity_id=project_id,
            metadata={"from_name": str(current_row["name"]), "to_name": name},
        )
        row = project_detail_row(conn, project_id)
        if row is None:
            raise RuntimeError("project disappeared after rename")
        return project_response(row)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> Response:
    require_not_auditor(
        conn,
        actor=actor,
        action="project.delete",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="project.delete")

    # Paid provider calls may still be in flight for leased tasks; deleting the
    # project underneath them would lose their write-back, so require the
    # operator to wait until they settle (succeed, fail, or supersede).
    has_active_tasks = conn.execute(
        """
        SELECT 1 FROM (
            SELECT generation_batches.project_id
            FROM generation_tasks
            JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
            WHERE generation_batches.project_id = %s
              AND generation_tasks.status IN
                  ('PENDING', 'SUBMITTING', 'QUEUED', 'RUNNING', 'ARCHIVING')
            UNION ALL
            SELECT analysis_tasks.project_id
            FROM analysis_tasks
            WHERE analysis_tasks.project_id = %s
              AND analysis_tasks.status IN ('PENDING', 'RUNNING')
        ) AS active_project_tasks
        LIMIT 1
        """,
        (project_id, project_id),
    ).fetchone()
    if has_active_tasks:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROJECT_DELETE_HAS_ACTIVE_TASKS",
                "message": "项目存在进行中的生成任务，请等待任务结束或失败后再删除。",
            },
        )

    assets = conn.execute(
        """
        SELECT id, storage_uri, sha256, size_bytes, content_object_id
        FROM assets
        WHERE project_id = %s
        """,
        (project_id,),
    ).fetchall()
    versions_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM versions WHERE project_id = %s",
            (project_id,),
        ).fetchone()[0]
    )

    # Object bytes are removed *only* by the reclaim sweeper in app.content_store,
    # which re-reads the reference count under a lock before touching storage.
    # This route's job is therefore narrower than it used to be: drop this
    # project's references, and delete bytes directly only for legacy rows the
    # registry cannot speak for.
    #
    # The registry branch is what fixes a real leak: an asset whose project_id is
    # NULL (material-library and character uploads are exactly that) is a live
    # reference, but `project_id <> %s` evaluates NULL against a value and never
    # matched it, so the old check reported "not shared" and deleted an object
    # another asset was still using. Counting cannot fail that way.
    registry_assets = 0
    legacy_deletable_uris: list[str] = []
    shared_storage_object_count = 0
    for asset in assets:
        if asset["content_object_id"] is not None:
            registry_assets += 1
            continue
        uri = str(asset["storage_uri"])
        if content_store.object_referenced_by_another_asset(
            conn,
            storage_uri=uri,
            excluding_asset_ids=[str(asset["id"])],
        ):
            shared_storage_object_count += 1
            continue
        legacy_deletable_uris.append(uri)

    released_to_zero = 0
    with conn:
        # Released before the cascade removes the rows, in the same transaction,
        # so a failed project delete cannot leave the count already decremented.
        released_to_zero = content_store.release_assets_content_objects(
            conn,
            asset_ids=[str(asset["id"]) for asset in assets],
        )
        # character_reference_selections references versions with ON DELETE
        # RESTRICT, so it must be cleared before the cascade removes versions.
        conn.execute(
            "DELETE FROM character_reference_selections WHERE project_id = %s",
            (project_id,),
        )
        conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))

    # Best-effort object cleanup: the operator is discarding the whole project,
    # so an unavailable backend (e.g. cloud credentials removed) must not block
    # the delete. Failures are counted and surfaced through the audit log.
    # Runs after the commit so no storage I/O happens inside a write transaction.
    storage_cleanup_failed_count = 0
    for uri in dict.fromkeys(legacy_deletable_uris):
        try:
            storage = storage_for_asset(conn, uri)
            # Re-checked here rather than trusted from the planning loop above: the
            # commit between the two is a window in which another asset can start
            # pointing at the same object, and this read is what closes it.
            content_store.delete_object_if_unreferenced(
                conn,
                storage,
                storage_key_from_uri(uri),
                actor_id=actor.id,
            )
        except (HTTPException, StorageBackendUnavailable, OSError, ValueError):
            storage_cleanup_failed_count += 1

    write_audit(
        conn,
        actor=actor,
        action="project.delete",
        entity_type="project",
        entity_id=project_id,
        metadata={
            "deleted_asset_count": len(assets),
            "deleted_versions_count": versions_count,
            "storage_cleanup_failed_count": storage_cleanup_failed_count,
            "shared_storage_object_count": shared_storage_object_count,
            "content_object_assets": registry_assets,
            "content_objects_released_to_zero": released_to_zero,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def read_project(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ProjectResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="project.read")
    row = project_detail_row(conn, project_id)
    if row is None:
        raise RuntimeError("project disappeared after access check")
    return project_response(row)


def project_detail_row(
    conn: BusinessConnection,
    project_id: str,
) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        conn.execute(
            """
        SELECT
            projects.id,
            projects.owner_user_id,
            projects.name,
            projects.status,
            reference_assets.id AS reference_asset_id,
            CASE
                WHEN reference_assets.id IS NULL THEN 'NOT_STARTED'
                WHEN reference_assets.sha256 = '' OR reference_assets.size_bytes = 0
                    THEN 'UPLOAD_PENDING'
                ELSE 'READY'
            END AS reference_upload_status,
            CASE
                WHEN reference_assets.id IS NULL
                    OR reference_assets.sha256 = ''
                    OR reference_assets.size_bytes = 0
                    THEN 'NOT_READY'
                WHEN EXISTS (
                    SELECT 1 FROM versions
                    WHERE versions.project_id = projects.id
                        AND versions.kind = 'analysis'
                        AND versions.asset_id = reference_assets.id
                ) THEN 'READY'
                WHEN latest_analysis_task.status IN ('PENDING', 'RUNNING')
                    THEN 'PENDING'
                WHEN latest_analysis_task.status = 'FAILED' THEN 'FAILED'
                ELSE 'NOT_READY'
            END AS analysis_status,
            latest_analysis_task.id AS analysis_task_id,
            latest_analysis_task.error_message_redacted AS analysis_error_message,
            COALESCE(latest_analysis_task.retryable, 0) AS analysis_retryable
        FROM projects
        LEFT JOIN assets AS reference_assets ON reference_assets.id = (
            SELECT assets.id
            FROM assets
            WHERE assets.project_id = projects.id
                AND (
                    assets.kind = 'reference_video'
                    OR (
                        assets.kind = 'video'
                        AND assets.storage_uri LIKE '%%/projects/' || projects.id || '/uploads/%%'
                    )
                )
            ORDER BY assets.created_at DESC, assets.id DESC
            LIMIT 1
        )
        LEFT JOIN analysis_tasks AS latest_analysis_task
            ON latest_analysis_task.id = (
                SELECT analysis_tasks.id
                FROM analysis_tasks
                WHERE analysis_tasks.project_id = projects.id
                  AND analysis_tasks.asset_id = reference_assets.id
                ORDER BY analysis_tasks.created_at DESC, analysis_tasks.id DESC
                LIMIT 1
            )
        WHERE projects.id = %s
        """,
            (project_id,),
        ).fetchone(),
    )


@router.get("/assets/{asset_id}", response_model=AssetResponse)
def read_asset(
    asset_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> AssetResponse:
    row = require_asset_access(conn, actor=actor, asset_id=asset_id, action="asset.read")
    if actor.role == "auditor":
        write_audit(
            conn,
            actor=actor,
            action="auditor.asset_metadata.read",
            entity_type="asset",
            entity_id=asset_id,
            metadata={"kind": str(row["kind"]), "project_id": row["project_id"]},
        )
    return asset_response(row)


@router.post("/assets/{asset_id}/download-url", response_model=DownloadUrlResponse)
def create_download_url(
    asset_id: str,
    db: BusinessDbDep,
) -> DownloadUrlResponse:
    with db.write() as (conn, actor):
        return _create_download_grant(conn, actor=actor, asset_id=asset_id)


def _create_download_grant(
    conn: BusinessConnection, *, actor: CurrentUser, asset_id: str
) -> DownloadUrlResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="asset.download_url.create",
        entity_type="asset",
        entity_id=asset_id,
    )
    _row, url = _grant_download_for_asset(conn, actor=actor, asset_id=asset_id)
    return DownloadUrlResponse(url=url)


def _grant_download_for_asset(
    conn: BusinessConnection, *, actor: CurrentUser, asset_id: str
) -> tuple[sqlite3.Row, str]:
    """单资产授权主体：属主校验 → 完整性校验 → 逐资产审计 → 签名 URL。

    单资产端点与批量端点（MATERIAL-PERF-A）共用，保证两条通道的授权、
    审计口径与签名格式完全一致。
    """
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="asset.download_url.create",
    )
    if int(row["size_bytes"]) <= 0 or not str(row["sha256"] or "").strip():
        raise HTTPException(
            409,
            detail={
                "code": "ASSET_UPLOAD_NOT_COMPLETE",
                "message": "素材尚未上传完成，请重新上传。",
            },
        )
    write_audit(
        conn,
        actor=actor,
        action="asset.download_url.create",
        entity_type="asset",
        entity_id=asset_id,
        metadata={"project_id": str(row["project_id"])},
    )
    try:
        storage_for_asset(conn, str(row["storage_uri"]))
        object_key = storage_key_from_uri(str(row["storage_uri"]))
        secret = settings_encryption_key()
        expires_at = str(int(time.time()) + int(DOWNLOAD_URL_EXPIRES_IN.total_seconds()))
        session_epoch = signed_asset_session_epoch(conn, actor)
        query = {
            "expires": expires_at,
            "user_id": actor.id,
            "asset_id": asset_id,
            "session_epoch": session_epoch,
        }
        signature = local_download_signature(
            object_key,
            expires_at,
            user_id=actor.id,
            asset_id=asset_id,
            session_epoch=session_epoch,
            secret=secret,
        )
        query["sig"] = signature
        # 下发绝对地址：桌面端页面 origin 是 tauri://，客户云版前端可与 API 分域名
        # 部署，站内相对地址会打到客户端自身而不是后端，img/video 只剩空预览框。
        # 与 viral_routes 的自有封面路由同口径，客户端零拼接。
        url = (
            f"{api_base_url()}/api/assets/signed-objects/"
            f"{quote(object_key, safe='/')}?{urlencode(query)}"
        )
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        ) from exc
    return row, url


class DownloadUrlsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ids: Annotated[list[str], Field(min_length=1, max_length=100)]


class DownloadUrlItem(BaseModel):
    asset_id: str
    url: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None
    error_code: str | None = None
    # MATERIAL-THUMBS-B / MATERIAL-UX-02：带缩略图键的视频/图片资产额外签出的
    # 7 天缩略图 URL。
    thumbnail_url: str | None = None


class DownloadUrlsResponse(BaseModel):
    items: list[DownloadUrlItem]


@router.post("/assets/download-urls", response_model=DownloadUrlsResponse)
def create_download_urls(
    request: DownloadUrlsRequest,
    db: BusinessDbDep,
) -> DownloadUrlsResponse:
    """批量签发素材预览授权（MATERIAL-PERF-A P0-2）。

    素材库网格此前对每个瓦片各发一次单资产授权（N+1 写连接 + 审计往返）；
    本端点在一个写事务内逐资产复用与单资产端点完全相同的授权逻辑。他属/
    缺失/未完成上传按条返回 ``error_code``（属主掩蔽与单端点同形），不拖垮
    整批；审计仍逐资产落行，口径不因批量而变稀。
    """
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="asset.download_url.create",
            entity_type="asset",
            entity_id="batch",
        )
        items: list[DownloadUrlItem] = []
        seen: set[str] = set()
        for asset_id in request.asset_ids:
            if asset_id in seen:
                continue
            seen.add(asset_id)
            try:
                row, url = _grant_download_for_asset(conn, actor=actor, asset_id=asset_id)
            except HTTPException as exc:
                detail: dict[Any, Any] = exc.detail if isinstance(exc.detail, dict) else {}
                code = detail.get("code")
                items.append(
                    DownloadUrlItem(
                        asset_id=asset_id,
                        error_code=str(code) if code else f"HTTP_{exc.status_code}",
                    )
                )
                continue
            thumbnail_url = _signed_thumbnail_url(conn, actor=actor, row=row, asset_id=asset_id)
            items.append(
                DownloadUrlItem(
                    asset_id=asset_id,
                    url=url,
                    sha256=str(row["sha256"]),
                    size_bytes=int(row["size_bytes"]),
                    content_type=(
                        str(row["content_type"]) if row["content_type"] is not None else None
                    ),
                    thumbnail_url=thumbnail_url,
                )
            )
    return DownloadUrlsResponse(items=items)


def _signed_thumbnail_url(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    row: sqlite3.Row,
    asset_id: str,
) -> str | None:
    """MATERIAL-THUMBS-B / MATERIAL-UX-02：为带缩略图键的视频/图片签出 7 天缩略图对象 URL。

    属主校验已在 ``_grant_download_for_asset`` 完成；此处只读元数据派生键，
    签名走与原对象完全相同的通道。音频与其他类型一律 None。
    """
    if row["content_type"] is None or not str(row["content_type"]).startswith(("video/", "image/")):
        return None
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except (TypeError, ValueError):
        return None
    thumbnail_key = metadata.get("thumbnail_key") if isinstance(metadata, dict) else None
    try:
        storage = storage_for_asset(conn, str(row["storage_uri"]))
        if not isinstance(thumbnail_key, str) or not thumbnail_key:
            # 历史素材（抽帧写入点上线前入库，或当初抽帧失败）没有记键。派生键
            # 由原对象键确定性推导，这里照签，首次加载时由 signed-objects 现场
            # 补齐——历史素材因此不需要回填脚本跑批。对象此刻还不存在，不能走
            # 直连（COS 预签名对缺失对象只会 404，不会触发派生）。
            thumbnail_key = thumbnail_key_for(storage_key_from_uri(str(row["storage_uri"])))
        elif storage.provider == "cos":
            # 缩略图直连对象存储：派生小图不再经应用服务器逐字节转发（网格一页
            # 24 张瓦片过去就是 24 次穿透 worker，且代理响应是 no-store，翻页
            # 必然全量重拉）。字节搬运交给对象存储，应用只负责签名。
            #
            # 代价：这条地址不受 session_epoch 即时吊销约束，在
            # THUMBNAIL_URL_EXPIRES_IN 内持续有效。缩略图是 480px 派生物
            # （视频首帧或图片单帧缩放），按低敏感度接受该窗口；原视频与人物
            # 原图仍走可吊销的代理通道（MATERIAL-UX-02：图片缩略图与视频
            # 缩略图同属派生小图，一并直连，原图不变）。
            return storage.create_download_intent(
                thumbnail_key, expires_in=THUMBNAIL_URL_EXPIRES_IN, can_read=True
            ).url
        secret = settings_encryption_key()
        expires_at = str(int(time.time()) + int(THUMBNAIL_URL_EXPIRES_IN.total_seconds()))
        session_epoch = signed_asset_session_epoch(conn, actor)
        signature = local_download_signature(
            thumbnail_key,
            expires_at,
            user_id=actor.id,
            asset_id=asset_id,
            session_epoch=session_epoch,
            secret=secret,
        )
        query = urlencode(
            {
                "expires": expires_at,
                "user_id": actor.id,
                "asset_id": asset_id,
                "session_epoch": session_epoch,
                "sig": signature,
            }
        )
        return (
            f"{api_base_url()}/api/assets/signed-objects/{quote(thumbnail_key, safe='/')}?{query}"
        )
    except StorageBackendUnavailable:
        return None


@router.post("/assets/{asset_id}/cached-url", response_model=DownloadUrlResponse)
def create_cached_character_url(
    asset_id: str,
    db: BusinessDbDep,
) -> DownloadUrlResponse:
    if is_customer_production():
        # Authorize the asset and load both encrypted storage configurations in
        # the fenced PG transaction, then release the pooled connection before
        # any COS HEAD/GET/PUT network operation.
        with db.write() as (conn, actor):
            require_not_auditor(
                conn,
                actor=actor,
                action="asset.character_cache.read",
                entity_type="asset",
                entity_id=asset_id,
            )
            row = require_asset_access(
                conn,
                actor=actor,
                asset_id=asset_id,
                action="asset.character_cache.read",
            )
            plan = _prepare_customer_character_cache(conn, row)
            grant_user_id = actor.id
            grant_session_epoch = signed_asset_session_epoch(conn, actor)
        cache_name, _ = _populate_customer_character_cache(plan)
    else:
        with db.write() as (conn, actor):
            require_not_auditor(
                conn,
                actor=actor,
                action="asset.character_cache.read",
                entity_type="asset",
                entity_id=asset_id,
            )
            row = require_asset_access(
                conn,
                actor=actor,
                asset_id=asset_id,
                action="asset.character_cache.read",
            )
            cache_name, _ = _populate_character_cache(conn, row)
            grant_user_id = actor.id
            grant_session_epoch = signed_asset_session_epoch(conn, actor)
    expires_at = str(int(time.time()) + int(DOWNLOAD_URL_EXPIRES_IN.total_seconds()))
    signed_key = f"character-cache/{cache_name}"
    signature = local_download_signature(
        signed_key,
        expires_at,
        user_id=grant_user_id,
        asset_id=asset_id,
        session_epoch=grant_session_epoch,
        secret=settings_encryption_key(),
    )
    query = urlencode(
        {
            "expires": expires_at,
            "user_id": grant_user_id,
            "asset_id": asset_id,
            "session_epoch": grant_session_epoch,
            "sig": signature,
        }
    )
    return DownloadUrlResponse(
        url=f"{api_base_url()}/api/assets/character-cache/{cache_name}?{query}"
    )


@router.get("/assets/character-cache/{cache_name}")
def read_cached_character_asset(cache_name: str, request: Request) -> Response:
    expires_at = request.query_params.get("expires")
    signature = request.query_params.get("sig")
    user_id = request.query_params.get("user_id")
    asset_id = request.query_params.get("asset_id")
    session_epoch = request.query_params.get("session_epoch")
    signed_key = f"character-cache/{cache_name}"
    if (
        not expires_at
        or not signature
        or not user_id
        or not asset_id
        or session_epoch is None
        or not expires_at.isdigit()
        or len(expires_at) > 20
        or int(expires_at) < int(time.time())
        or not hmac.compare_digest(
            signature,
            local_download_signature(
                signed_key,
                expires_at,
                user_id=user_id,
                asset_id=asset_id,
                session_epoch=session_epoch,
                secret=settings_encryption_key(),
            ),
        )
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "CHARACTER_CACHE_FORBIDDEN"},
        )
    _validate_character_cache_grant(
        user_id=user_id,
        asset_id=asset_id,
        session_epoch=session_epoch,
    )
    if is_customer_production():
        cache_key = _character_cache_object_key(cache_name)
        try:
            # Load encrypted settings inside a short PG transaction, then
            # release the connection before the shared COS network read.
            shared_storage = _load_customer_character_cache_storage()
            content = shared_storage.get_object(cache_key)
        except (KeyError, OSError, StorageBackendUnavailable) as exc:
            logger.error(
                "shared character cache read failed for file %s: %s",
                cache_name,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
            ) from exc
        return Response(
            content=content,
            media_type=CHARACTER_CACHE_CONTENT_TYPES[Path(cache_name).suffix],
            headers={
                "Cache-Control": "private, max-age=900",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="{cache_name}"',
            },
        )
    try:
        cache_path = _character_cache_path(cache_name)
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        ) from exc
    if not cache_path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"code": "CHARACTER_CACHE_NOT_FOUND"},
        )
    try:
        content = cache_path.read_bytes()
    except OSError as exc:
        logger.error(
            "character cache read failed for file %s: %s",
            cache_name,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        ) from exc
    return Response(
        content=content,
        media_type=CHARACTER_CACHE_CONTENT_TYPES[cache_path.suffix],
        headers={
            "Cache-Control": "private, max-age=900",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{cache_name}"',
        },
    )


@router.get("/audit-logs", response_model=list[AuditLogResponse])
def read_audit_logs(
    conn: Database,
    actor: AuthenticatedUser,
) -> list[AuditLogResponse]:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin", "auditor"},
        action="audit_log.read",
        entity_type="audit_log",
        entity_id="collection",
    )
    rows = conn.execute(
        """
        SELECT id, actor_user_id, action, entity_type, entity_id, metadata_json, created_at
        FROM audit_logs
        ORDER BY created_at, id
        """
    ).fetchall()
    return [audit_log_response(row) for row in rows]


def asset_response(row: sqlite3.Row) -> AssetResponse:
    return AssetResponse(
        id=str(row["id"]),
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        kind=str(row["kind"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        content_type=None if row["content_type"] is None else str(row["content_type"]),
    )


def project_response(row: sqlite3.Row) -> ProjectResponse:
    reference_asset_id = (
        None
        if "reference_asset_id" not in row.keys() or row["reference_asset_id"] is None
        else str(row["reference_asset_id"])
    )
    reference_upload_status = (
        "NOT_STARTED"
        if "reference_upload_status" not in row.keys()
        else str(row["reference_upload_status"])
    )
    analysis_status = (
        "NOT_READY" if "analysis_status" not in row.keys() else str(row["analysis_status"])
    )
    analysis_task_id = (
        None
        if "analysis_task_id" not in row.keys() or row["analysis_task_id"] is None
        else str(row["analysis_task_id"])
    )
    analysis_error_message = (
        None
        if "analysis_error_message" not in row.keys() or row["analysis_error_message"] is None
        else str(row["analysis_error_message"])
    )
    analysis_retryable = (
        False if "analysis_retryable" not in row.keys() else bool(row["analysis_retryable"])
    )
    return ProjectResponse(
        id=str(row["id"]),
        owner_user_id=str(row["owner_user_id"]),
        name=str(row["name"]),
        status=str(row["status"]),
        reference_asset_id=reference_asset_id,
        reference_upload_status=reference_upload_status,
        analysis_status=analysis_status,
        analysis_task_id=analysis_task_id,
        analysis_error_message=analysis_error_message,
        analysis_retryable=analysis_retryable,
    )


def audit_log_response(row: sqlite3.Row) -> AuditLogResponse:
    return AuditLogResponse(
        id=str(row["id"]),
        actor_user_id=None if row["actor_user_id"] is None else str(row["actor_user_id"]),
        action=str(row["action"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        metadata_json=str(row["metadata_json"]),
        created_at=str(row["created_at"]),
    )
