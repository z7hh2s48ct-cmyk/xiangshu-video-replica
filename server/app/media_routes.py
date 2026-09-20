from __future__ import annotations

import hmac
import json
import logging
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.auth import AuthenticatedUser, CurrentUser, Database, authenticate_user
from app.customer_fence import BusinessDbDep, BusinessReadConn
from app.db_pg import DATABASE_URL_ENV, pg_transaction
from app.db_portable import BusinessConnection
from app.material_thumbs import (
    THUMBNAIL_SUFFIX,
    THUMBNAIL_URL_EXPIRES_IN,
    ensure_thumbnail_object,
    thumbnail_key_for,
)
from app.media import (
    MAX_UPLOAD_BYTES,
    FFprobeVideoProbe,
    VideoMetadata,
    VideoProbe,
    create_upload_intent,
    persist_upload_completion,
    prepare_upload_completion,
    probe_upload_completion,
    storage_key_from_uri,
)
from app.permissions import (
    AuditedSecurityDenial,
    persist_security_denial,
    remap_security_denial,
    require_asset_access,
    require_not_auditor,
    require_project_access,
    require_role,
)
from app.settings import SettingsRepository, SettingsUnavailableError, settings_encryption_key
from app.storage import (
    LocalStorageAdapter,
    StorageAdapter,
    StorageBackendUnavailable,
    cloud_storage_config_from_settings,
    create_local_storage_from_environment,
    create_storage_adapter,
    local_download_signature,
    local_storage_root,
    require_storage_match,
    storage_object_ref_from_uri,
)

router = APIRouter(prefix="/api/assets", tags=["media"])
LOCAL_API_BASE_URL = "http://127.0.0.1:8000"
PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
LOCAL_API_BASE_URL_ENV = "VIDEO_REPLICA_LOCAL_API_BASE_URL"
MAX_OBJECT_DOWNLOAD_BYTES = 512 * 1024 * 1024
OBJECT_DOWNLOAD_CHUNK_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)


def api_base_url() -> str:
    """Use the public HTTPS origin on a server and localhost for desktop fallback."""
    from app.bootstrap import is_customer_production

    configured = os.environ.get(PUBLIC_BASE_URL_ENV, "").strip()
    if not configured:
        local_configured = os.environ.get(LOCAL_API_BASE_URL_ENV, "").strip()
        if not local_configured:
            return LOCAL_API_BASE_URL
        parsed_local = urlsplit(local_configured)
        if (
            is_customer_production()
            or parsed_local.scheme != "http"
            or parsed_local.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_local.username is not None
            or parsed_local.password is not None
            or parsed_local.path not in {"", "/"}
            or parsed_local.query
            or parsed_local.fragment
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "LOCAL_API_BASE_URL_INVALID",
                    "message": (
                        "VIDEO_REPLICA_LOCAL_API_BASE_URL must be a loopback HTTP "
                        "origin outside customer production."
                    ),
                },
            )
        return f"http://{parsed_local.netloc}"

    parsed = urlsplit(configured)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PUBLIC_BASE_URL_INVALID",
                "message": "PUBLIC_BASE_URL must be an HTTPS origin without a path.",
            },
        )
    return f"https://{parsed.netloc}"


class UploadIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    purpose: Literal["replica", "script"] = "replica"


class UploadIntentResponse(BaseModel):
    asset_id: str
    project_id: str
    storage_key: str | None = None
    method: str | None
    url: str | None
    headers: dict[str, str]
    expires_at: str | None
    upload_required: bool


class CompleteUploadResponse(BaseModel):
    asset_id: str
    project_id: str
    status: str
    storage_uri: str | None = None
    sha256: str
    size_bytes: int
    content_type: str
    metadata: VideoMetadata
    analysis_task_id: str | None
    analysis_task_status: str | None


def get_media_storage(conn: BusinessReadConn) -> StorageAdapter:
    """业务主存储：源参考视频（拆解需要 HTTPS URL）、人物图片、多视角
    图与首帧。

    CW-031：正式服务（``active_storage_provider="cos"`` 或客户生产）只允许
    云端存储，缺配置/配置不完整一律 503，绝不回退本地持久盘。本函数是 API
    与 Worker 共同的存储入口（``generation_worker``/``oral``/``rbac_routes``/
    ``viral_routes``/``analysis_routes`` 等消费），一道门禁即关闭全部正式服务
    的本地新写。内部 P0 单机车道（``active_storage_provider="local"`` 且非
    客户生产，见 ``gate1_bootstrap.py``）保留本地适配器——那是桌面单机场景的
    既定架构，不是正式服务。读路径 ``storage_for_asset`` 按已持久化 URI 解析、
    不经过这里，历史 ``local://`` 资产的只读追溯不受影响（搬迁归 CW-037）。
    """
    # 局部 import：bootstrap 间接依赖路由模块，顶层引入有循环依赖风险（CW-031 §8）。
    from app.bootstrap import is_customer_production

    try:
        repo = SettingsRepository(conn)
        runtime = repo.read_runtime_settings()
        provider = str(runtime.get("active_storage_provider") or "")
        customer_production = is_customer_production()
    except (SettingsUnavailableError, StorageBackendUnavailable, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        ) from exc

    if provider == "cos" or customer_production:
        # 正式服务：COS 是唯一合法的持久存储后端，缺配置 = 明确失败，不回退。
        # 客户生产即使 active_storage_provider 被改成 local 也走本分支（兜底闸门）。
        try:
            config = repo.load_provider_config("cos")
        except (SettingsUnavailableError, StorageBackendUnavailable, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
            ) from exc
        if not config:
            logger.error(
                "Formal service storage is unconfigured (provider=%s)", provider or "<unset>"
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "STORAGE_PROVIDER_FORBIDDEN"},
            )
        try:
            return create_storage_adapter(cloud_storage_config_from_settings("cos", config))
        except (StorageBackendUnavailable, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
            ) from exc

    if provider == "local":
        # 内部 P0 单机车道（桌面场景）。走到这里说明 customer_production 为假。
        logger.warning(
            "Local persistent storage is only valid on the internal P0 single-machine lane"
        )
        try:
            return create_local_storage_from_environment()
        except StorageBackendUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
            ) from exc

    raise HTTPException(
        status_code=503,
        detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
    )


def storage_for_asset(conn: BusinessConnection, storage_uri: str) -> StorageAdapter:
    """Resolve the adapter named by a persisted asset URI, including its bucket."""
    reference = storage_object_ref_from_uri(storage_uri)
    if reference.provider == "local":
        # CW-031：历史 local URI 只作为迁移期可追溯输入（只读），新写已被
        # get_media_storage 的闸门禁止。客户生产不该再有本地持久资产——若有，
        # 说明是 CW-037 未搬迁完的遗留，必须显式失败而不是静默服务。
        from app.bootstrap import is_customer_production

        if is_customer_production():
            logger.error("Legacy local asset URI rejected on the customer production lane")
            raise HTTPException(
                status_code=503,
                detail={"code": "STORAGE_PROVIDER_FORBIDDEN"},
            )
        logger.warning(
            "Serving a legacy local asset URI (provider=%s); migration to COS is owed by CW-037",
            reference.provider,
        )
        local_storage = LocalStorageAdapter(root=local_storage_root(), bucket=reference.bucket)
        require_storage_match(local_storage, reference)
        return local_storage
    if reference.provider != "cos":
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        )
    try:
        config = SettingsRepository(conn).load_provider_config(reference.provider)
    except SettingsUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        ) from exc
    if config.get("bucket") != reference.bucket:
        raise HTTPException(
            status_code=409,
            detail={"code": "STORAGE_BUCKET_MISMATCH"},
        )
    try:
        cloud_storage = create_storage_adapter(
            cloud_storage_config_from_settings(reference.provider, config)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        ) from exc
    require_storage_match(cloud_storage, reference)
    return cloud_storage


def get_video_probe() -> VideoProbe:
    return FFprobeVideoProbe()


MediaStorage = Annotated[StorageAdapter, Depends(get_media_storage)]
InjectedVideoProbe = Annotated[VideoProbe, Depends(get_video_probe)]


def signed_asset_session_epoch(conn: BusinessConnection, actor: CurrentUser) -> str:
    """Bind customer grants to the live session epoch; internal grants use zero."""
    if actor.role != "customer":
        return "0"
    from app.customer_auth import CustomerSessionContext

    if isinstance(conn.ctx, CustomerSessionContext):
        return f"{conn.ctx.session_id}:{conn.ctx.session_epoch}"
    row = conn.execute(
        "SELECT session_id, session_epoch FROM customer_session_state WHERE user_id = %s "
        "AND lease_until::timestamptz > clock_timestamp() ORDER BY session_id LIMIT 2",
        (actor.id,),
    ).fetchall()
    if len(row) != 1:
        raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
    return f"{row[0]['session_id']}:{row[0]['session_epoch']}"


def validate_signed_asset_grant(
    conn: BusinessConnection,
    *,
    user_id: str,
    asset_id: str,
    session_epoch: str,
    expected_object_key: str | None = None,
) -> Any:
    """Revalidate identity, authorization, asset existence and session state."""
    try:
        actor = authenticate_user(conn, user_id)
        asset = require_asset_access(
            conn,
            actor=actor,
            asset_id=asset_id,
            action="asset.signed_download.read",
        )
    except HTTPException as exc:
        raise remap_security_denial(
            exc,
            status_code=403,
            detail={"code": "SIGNED_ASSET_GRANT_FORBIDDEN"},
        ) from exc
    if expected_object_key is not None:
        try:
            current_key = storage_key_from_uri(str(asset["storage_uri"]))
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "SIGNED_ASSET_GRANT_FORBIDDEN"},
            ) from exc
        # 素材缩略图由原对象键确定性派生（``<key>.thumb.jpg``），授权侧签发的
        # 就是派生键。这里只比对原键会让每个缩略图请求必然 403，并连带挡死
        # ``get_signed_object`` 里「历史素材首次读取时现场补齐」那段逻辑。
        # 仍然是精确比对：只认本资产的原键与本资产的派生键，两者都不匹配就拒。
        requested = expected_object_key.encode("utf-8")
        matches_original = hmac.compare_digest(current_key.encode("utf-8"), requested)
        matches_thumbnail = hmac.compare_digest(
            thumbnail_key_for(current_key).encode("utf-8"), requested
        )
        if not (matches_original or matches_thumbnail):
            raise HTTPException(
                status_code=403,
                detail={"code": "SIGNED_ASSET_GRANT_FORBIDDEN"},
            )
    if actor.role != "customer":
        if session_epoch != "0":
            raise HTTPException(
                status_code=403,
                detail={"code": "SIGNED_ASSET_GRANT_FORBIDDEN"},
            )
        return asset
    grant_session_id, separator, epoch = session_epoch.partition(":")
    if not separator or not epoch.isdigit():
        raise HTTPException(
            status_code=403,
            detail={"code": "SIGNED_ASSET_GRANT_FORBIDDEN"},
        )
    state = conn.execute(
        """
        SELECT css.session_epoch, css.lease_until, ac.status AS code_status,
               css.activation_code_id,
               device.status AS device_status
        FROM customer_session_state AS css
        LEFT JOIN activation_codes AS ac ON ac.id = css.activation_code_id
        JOIN customer_devices AS device ON device.id = css.device_id
        WHERE css.user_id = %s AND css.session_id = %s
        """,
        (user_id, grant_session_id),
    ).fetchone()
    if state is None:
        raise HTTPException(status_code=403, detail={"code": "SIGNED_ASSET_GRANT_FORBIDDEN"})
    lease_until = datetime.fromisoformat(str(state["lease_until"]))
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=UTC)
    if (
        int(state["session_epoch"]) != int(epoch)
        or (state["activation_code_id"] is not None and str(state["code_status"]) != "ACTIVE")
        or str(state["device_status"]) != "BOUND"
        or lease_until <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=403, detail={"code": "SIGNED_ASSET_GRANT_FORBIDDEN"})
    return asset


def _validate_signed_object_request(
    conn: BusinessConnection,
    *,
    object_key: str,
    request: Request,
) -> Any:
    expires_at = request.query_params.get("expires")
    signature = request.query_params.get("sig")
    user_id = request.query_params.get("user_id")
    asset_id = request.query_params.get("asset_id")
    session_epoch = request.query_params.get("session_epoch")
    secret = settings_encryption_key()
    if expires_at is not None and len(expires_at) > 20:
        raise HTTPException(status_code=400, detail={"code": "INVALID_EXPIRES"})
    if (
        not expires_at
        or not signature
        or not user_id
        or not asset_id
        or session_epoch is None
        or not secret
        or not expires_at.isdigit()
        or int(expires_at) < int(time.time())
        or not hmac.compare_digest(
            signature,
            local_download_signature(
                object_key,
                expires_at,
                user_id=user_id,
                asset_id=asset_id,
                session_epoch=session_epoch,
                secret=secret,
            ),
        )
    ):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_DOWNLOAD_FORBIDDEN"})
    return validate_signed_asset_grant(
        conn,
        user_id=user_id,
        asset_id=asset_id,
        session_epoch=session_epoch,
        expected_object_key=object_key,
    )


def _signed_object_response(
    conn: BusinessConnection,
    *,
    object_key: str,
    request: Request,
    storage: StorageAdapter,
    asset: Any | None = None,
) -> Response:
    if asset is None:
        asset = _validate_signed_object_request(conn, object_key=object_key, request=request)
    reference = storage_object_ref_from_uri(str(asset["storage_uri"]))
    require_storage_match(storage, reference)
    return _read_stored_object(
        storage, object_key=object_key, range_header=request.headers.get("range")
    )


class _ObjectStreamingResponse(StreamingResponse):
    def __init__(
        self, source: Iterator[bytes], *, resource: Iterator[bytes] | None = None, **kwargs: Any
    ) -> None:
        self._source = source
        self._resource = resource
        super().__init__(source, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette's threadpool wrapper need not close the underlying
            # synchronous generator when sending fails or the client disconnects.
            try:
                close = getattr(self._source, "close", None)
                if close is not None:
                    with CancelScope(shield=True):
                        await run_in_threadpool(close)
            finally:
                resource_close = getattr(self._resource, "close", None)
                if resource_close is not None:
                    with CancelScope(shield=True):
                        await run_in_threadpool(resource_close)


def _object_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if value is None:
        return 0, size - 1, False
    try:
        if size <= 0 or len(value) > 100 or not value.startswith("bytes=") or "," in value:
            raise ValueError
        first, separator, last = value[6:].partition("-")
        if not separator or (first and not first.isascii()) or (last and not last.isascii()):
            raise ValueError
        if first:
            if not first.isdecimal() or (last and not last.isdecimal()):
                raise ValueError
            start, end = int(first), min(int(last), size - 1) if last else size - 1
        else:
            if not last.isdecimal() or int(last) <= 0:
                raise ValueError
            start, end = max(0, size - int(last)), size - 1
        if start < 0 or start >= size or end < start:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(
            416,
            detail={"code": "OBJECT_RANGE_INVALID"},
            headers={"Content-Range": f"bytes */{size}"},
        ) from exc
    return start, end, True


def _bounded_object_chunks(source: Iterator[bytes], expected: int) -> Iterator[bytes]:
    consumed = 0
    try:
        for chunk in source:
            if len(chunk) > OBJECT_DOWNLOAD_CHUNK_BYTES or consumed + len(chunk) > expected:
                raise StorageBackendUnavailable("object exceeded the declared download length")
            consumed += len(chunk)
            yield chunk
        if consumed != expected:
            raise StorageBackendUnavailable("object ended before the declared download length")
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()


def _prefetched_object_chunks(first: bytes, source: Iterator[bytes]) -> Iterator[bytes]:
    try:
        yield first
        yield from source
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()


def _read_stored_object(
    storage: StorageAdapter,
    *,
    object_key: str,
    range_header: str | None = None,
    cache_control: str | None = None,
) -> Response:
    try:
        stored = storage.head_object(object_key)
        if stored is None:
            raise HTTPException(status_code=404, detail={"code": "OBJECT_NOT_FOUND"})
        if stored.size < 0 or stored.size > MAX_OBJECT_DOWNLOAD_BYTES:
            raise HTTPException(413, detail={"code": "OBJECT_TOO_LARGE"})
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        ) from exc
    start, end, partial = _object_range(range_header, stored.size)
    length = end - start + 1
    filename = quote(Path(object_key).name, safe="")
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": cache_control or "private, no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{stored.size}"
    if length == 0:
        return Response(content=b"", media_type=stored.content_type, headers=headers)
    source = _bounded_object_chunks(
        storage.iter_object(
            object_key, start=start, end=end, chunk_size=OBJECT_DOWNLOAD_CHUNK_BYTES
        ),
        length,
    )
    try:
        first = next(source)
    except StorageBackendUnavailable as exc:
        raise HTTPException(503, detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"}) from exc
    return _ObjectStreamingResponse(
        _prefetched_object_chunks(first, source),
        resource=source,
        status_code=206 if partial else 200,
        media_type=stored.content_type,
        headers=headers,
    )


def _prepare_signed_object_read(
    *,
    object_key: str,
    request: Request,
) -> StorageAdapter:
    """Authorize and resolve storage in a short DB scope before cloud I/O."""
    if os.environ.get(DATABASE_URL_ENV, "").strip():
        try:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                asset = _validate_signed_object_request(
                    conn,
                    object_key=object_key,
                    request=request,
                )
                return storage_for_asset(conn, str(asset["storage_uri"]))
        except AuditedSecurityDenial as exc:
            persist_security_denial(exc)
            raise
    raise HTTPException(status_code=503, detail={"code": "DATABASE_NOT_CONFIGURED"})


@router.post(
    "/upload-intent",
    response_model=UploadIntentResponse,
)
def create_asset_upload_intent(
    payload: UploadIntentRequest,
    db: BusinessDbDep,
    storage: MediaStorage,
) -> UploadIntentResponse | JSONResponse:
    with db.write() as (conn, actor):
        intent = create_upload_intent(
            conn,
            actor=actor,
            storage=storage,
            project_id=payload.project_id,
            filename=payload.filename,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            sha256=payload.sha256,
            purpose=payload.purpose,
        )
        is_customer = actor.role == "customer"
    upload_url = intent.url
    if intent.upload_required and storage.provider == "local":
        # Let the client resolve API-managed uploads through its configured
        # proxy/base, instead of leaking the server's internal host and port.
        upload_url = f"/api/assets/local-objects/{quote(intent.storage_key, safe='/')}"
    result = UploadIntentResponse(
        asset_id=intent.asset_id,
        project_id=intent.project_id,
        storage_key=intent.storage_key,
        method=intent.method,
        url=upload_url,
        headers=intent.headers,
        expires_at=intent.expires_at,
        upload_required=intent.upload_required,
    )
    if is_customer:
        return JSONResponse(content=result.model_dump(mode="json", exclude={"storage_key"}))
    return result


@router.post(
    "/{asset_id}/complete",
    response_model=CompleteUploadResponse,
)
def complete_asset_upload(
    asset_id: str,
    db: BusinessDbDep,
    storage: MediaStorage,
    probe: InjectedVideoProbe,
) -> CompleteUploadResponse | JSONResponse:
    with db.write() as (conn, actor):
        prepared = prepare_upload_completion(conn, actor=actor, asset_id=asset_id)
        is_customer = actor.role == "customer"
    try:
        probed = probe_upload_completion(prepared, storage=storage, probe=probe)
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "STORAGE_PROVIDER_UNAVAILABLE",
                "message": "素材库暂时无法完成视频校验，请稍后重试。本次未启动 AI 拆解。",
            },
        ) from exc
    with db.write() as (conn, actor):
        completed = persist_upload_completion(conn, actor=actor, probed=probed)
    result = CompleteUploadResponse(
        asset_id=completed.asset_id,
        project_id=completed.project_id,
        status=completed.status,
        storage_uri=completed.storage_uri,
        sha256=completed.sha256,
        size_bytes=completed.size_bytes,
        content_type=completed.content_type,
        metadata=completed.metadata,
        analysis_task_id=completed.analysis_task_id,
        analysis_task_status=completed.analysis_task_status,
    )
    if is_customer:
        return JSONResponse(content=result.model_dump(mode="json", exclude={"storage_uri"}))
    return result


@router.put("/local-objects/{object_key:path}")
async def put_local_object(
    object_key: str,
    request: Request,
    conn: Database,
    actor: AuthenticatedUser,
    storage: MediaStorage,
) -> Response:
    """Receive a raw PUT body and write it to the local storage adapter.

    Only reachable when the runtime storage provider is `local`; the desktop
    client uploads the reference video here instead of a `local://` scheme URL.
    Keeps the same role/project gates as the cloud intent flow: auditors are
    read-only and only the project owner/admin may write objects.
    """
    if storage.provider != "local":
        raise HTTPException(status_code=404, detail={"code": "LOCAL_UPLOAD_UNAVAILABLE"})
    prefix = "projects/"
    expected_content_type: str | None = None
    expected_size: int | None = None
    if object_key.startswith(prefix) and "/" in object_key[len(prefix) :]:
        require_not_auditor(
            conn,
            actor=actor,
            action="asset.object.put",
            entity_type="asset",
            entity_id=object_key,
        )
        project_id = object_key[len(prefix) :].split("/", 1)[0]
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="asset.object.put",
        )
        pending = conn.execute(
            "SELECT content_type, metadata_json FROM assets WHERE project_id = %s "
            "AND storage_uri = %s AND sha256 = '' AND size_bytes = 0",
            (project_id, f"{storage.provider}://{storage.bucket}/{object_key}"),
        ).fetchone()
        if pending is None:
            raise HTTPException(409, detail={"code": "UPLOAD_INTENT_REQUIRED"})
        expected_content_type = str(pending["content_type"])
        metadata = json.loads(str(pending["metadata_json"]))
        if metadata.get("upload_status") == "EXPIRED":
            raise HTTPException(409, detail={"code": "UPLOAD_EXPIRED"})
        requested_size = metadata.get("requested_size_bytes")
        if isinstance(requested_size, int):
            expected_size = requested_size
    else:
        require_role(
            conn,
            actor=actor,
            allowed_roles={"admin"},
            action="asset.object.put",
            entity_type="asset",
            entity_id=object_key,
        )
        storage_uri = f"{storage.provider}://{storage.bucket}/{object_key}"
        pending = conn.execute(
            """
            SELECT id, kind, content_type, metadata_json
            FROM assets
            WHERE project_id IS NULL AND storage_uri = %s AND sha256 = '' AND size_bytes = 0
            """,
            (storage_uri,),
        ).fetchone()
        if pending is None:
            raise HTTPException(status_code=400, detail={"code": "INVALID_OBJECT_KEY"})
        try:
            metadata = json.loads(str(pending["metadata_json"]))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "IDENTITY_UPLOAD_INTENT_INVALID"},
            ) from exc
        if (
            str(pending["kind"]) not in {"character_authorization", "character_source_image"}
            or not isinstance(metadata, dict)
            or metadata.get("upload_status") != "PENDING"
            or metadata.get("object_key") != object_key
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "IDENTITY_UPLOAD_INTENT_INVALID"},
            )
        expected_content_type = str(pending["content_type"])
        requested_size = metadata.get("requested_size_bytes")
        if isinstance(requested_size, int):
            expected_size = requested_size
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail={"code": "PAYLOAD_TOO_LARGE"})
    content_buffer = bytearray()
    async for chunk in request.stream():
        if len(content_buffer) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail={"code": "PAYLOAD_TOO_LARGE"})
        content_buffer.extend(chunk)
    content = bytes(content_buffer)
    content_type = request.headers.get("content-type", "application/octet-stream")
    if expected_content_type is not None and content_type != expected_content_type:
        raise HTTPException(status_code=415, detail={"code": "CONTENT_TYPE_MISMATCH"})
    if expected_size is not None and len(content) != expected_size:
        raise HTTPException(status_code=409, detail={"code": "UPLOAD_SIZE_MISMATCH"})
    storage.put_object(object_key, content, content_type=content_type)
    return Response(status_code=204)


@router.get("/local-objects/{object_key:path}")
def get_local_object(
    object_key: str,
    request: Request,
    conn: Database,
    storage: MediaStorage,
) -> Response:
    """Serve a local-storage object through the API.

    The download URL carries a short-lived HMAC signature (issued by
    create_download_url) because an <img> tag cannot attach the dev identity
    header. The signature is bound to object, actor, asset and session epoch;
    the database grant is revalidated before every read so deletion, session
    replacement and activation revocation take effect immediately.
    """
    if storage.provider != "local":
        raise HTTPException(status_code=404, detail={"code": "LOCAL_DOWNLOAD_UNAVAILABLE"})
    return _signed_object_response(
        conn,
        object_key=object_key,
        request=request,
        storage=storage,
    )


@router.get("/signed-objects/{object_key:path}")
def get_signed_object(
    object_key: str,
    request: Request,
) -> Response:
    """Proxy a revocable signed grant for local or private cloud storage."""
    storage = _prepare_signed_object_read(object_key=object_key, request=request)
    if object_key.endswith(THUMBNAIL_SUFFIX):
        # 历史素材没有缩略图对象（抽帧原本只在上传写入点发生）。授权侧照签确定性
        # 派生键，这里首次读取时现场补齐，历史素材因此不需要回填脚本。派生失败
        # 只是这条没有缩略图，继续走下面的读取并以 404 收场，由前端降级占位。
        if not ensure_thumbnail_object(storage, object_key):
            logger.info("thumbnail derivation unavailable for %s", object_key)
    return _read_stored_object(
        storage,
        object_key=object_key,
        range_header=request.headers.get("range"),
        cache_control=_signed_object_cache_control(object_key),
    )


def _signed_object_cache_control(object_key: str) -> str | None:
    """缩略图允许浏览器缓存到签名过期为止；其余对象维持 no-store。

    缩略图键由原对象内容确定性派生（``<object_key>.thumb.jpg``），内容不会原地
    变化，重新生成必然换键，因此可以 ``immutable``。素材库一页 24 张瓦片，
    ``no-store`` 意味着每次翻页/重渲染都全量重拉，穿透应用服务器直到对象存储。

    缓存窗口与签名有效期一致：地址过期后浏览器必须回来重新授权，缓存不会延长
    任何一条授权的实际寿命。
    """
    if not object_key.endswith(THUMBNAIL_SUFFIX):
        return None
    max_age = int(THUMBNAIL_URL_EXPIRES_IN.total_seconds())
    return f"private, max-age={max_age}, immutable"
