"""The fair-queue rollout switch on the production control plane (M4/M5
review M2 follow-up, PR #68 Codex P1).

The internal ``PATCH /api/admin/settings/runtime`` route authenticates with
the internal Bearer lane (``SettingsAdmin`` → ``AuthenticatedUser``), which
customer-production operators can never present: they authenticate through
per-operator admin sessions (the admin cookie + CSRF flow of
``admin_auth_routes``). Without this router, a normally authenticated
administrator would answer 401 on the switch and the audited enable path
would not exist in production — leaving ad-hoc SQL as the only option,
exactly what M2 set out to close.

Contract mirrors the other admin routes (T12 precedent): ``AdminReader``
reads, ``AdminWriter`` writes (admin role, CSRF header enforced by
``get_admin_actor`` on write methods), every write lands an audit_logs row
with the real actor, and the SQLite lane is not applicable — admin sessions
themselves require the PostgreSQL runtime and fail closed elsewhere.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import (
    AdminWriteContract,
    http_error,
    require_write_contract,
    write_with_idempotency,
)
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.settings import DEFAULT_BILLING_SETTINGS, DEFAULT_RUNTIME_SETTINGS
from app.sql_pagination import PAGE_CLAUSE
from app.viral_keywords import ViralKeywordConfig

router = APIRouter(prefix="/api/control", tags=["admin-runtime"])

RUNTIME_SETTINGS_SERVICE_UNAVAILABLE = "RUNTIME_SETTINGS_SERVICE_UNAVAILABLE"
RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE = (
    "Runtime settings writes require the PostgreSQL runtime."
)
ViralRefreshStatus = Literal["not_configured", "configured_only", "refreshing", "ok", "error"]


class QueueModeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fair_queue_enabled: bool


class QueueModeUpdateRequest(AdminWriteContract):
    """The production switch follows the shared admin write contract (PR #85
    review P2): idempotency key header, ``confirm: true`` and a non-blank
    operator reason, so audit rows name the operator's reason and ambiguous
    retries replay the snapshotted outcome instead of duplicating audits."""

    model_config = ConfigDict(extra="forbid")

    fair_queue_enabled: bool


class ViralPlatformStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"]
    cached_videos: int
    last_fetched_at: str | None
    refresh_status: ViralRefreshStatus
    last_refresh_error: str | None


class ViralRuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_enabled: bool
    import_enabled: bool
    pending_imports: int
    running_imports: int
    failed_imports: int
    pending_refreshes: int
    running_refreshes: int
    failed_refreshes: int
    source_configured: bool
    platforms: list[ViralPlatformStatus]
    keywords: list[ViralKeywordConfig] = Field(default_factory=list)
    per_keyword_limit: int = 10
    next_collection_at: str | None = None
    collection_interval_days: int = 7


class ViralRuntimeUpdateRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    collection_enabled: bool
    import_enabled: bool
    keywords: list[ViralKeywordConfig] | None = Field(default=None, max_length=20)
    per_keyword_limit: int | None = Field(default=None, ge=1, le=50)
    collection_interval_days: Literal[1, 7] | None = None

    @model_validator(mode="after")
    def unique_keywords(self) -> ViralRuntimeUpdateRequest:
        if self.keywords is not None:
            identities = [(item.platform, item.keyword) for item in self.keywords]
            if len(identities) != len(set(identities)):
                raise ValueError("同平台关键词不能重复")
        return self


class ViralAvailabilityUpdateRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    status: Literal["AVAILABLE", "HIDDEN", "UNAVAILABLE"]


class ViralAvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"]
    video_id: str
    status: Literal["AVAILABLE", "HIDDEN", "UNAVAILABLE"]


def _viral_runtime_response(conn: psycopg.Connection) -> ViralRuntimeResponse:
    controls = conn.execute(
        "SELECT collection_enabled, import_enabled, keywords_json, per_keyword_limit, "
        "next_collection_at, collection_interval_days FROM viral_runtime_controls WHERE id = 1"
    ).fetchone()
    counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT status, count(*) FROM viral_import_tasks GROUP BY status"
        ).fetchall()
    }
    refresh_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT status, count(*) FROM viral_refresh_tasks GROUP BY status"
        ).fetchall()
    }
    source_configured = (
        conn.execute("SELECT 1 FROM provider_settings WHERE provider = 'tikhub'").fetchone()
        is not None
    )
    platforms: list[ViralPlatformStatus] = []
    for platform in ("douyin", "wechat_channels"):
        cached = conn.execute(
            "SELECT count(*) FROM viral_videos WHERE platform = %s", (platform,)
        ).fetchone()
        fetched = conn.execute(
            "SELECT max(fetched_at) FROM viral_fetch_state WHERE platform = %s",
            (platform,),
        ).fetchone()
        refresh = conn.execute(
            """
            SELECT status, error_message_redacted FROM viral_refresh_tasks
            WHERE platform = %s ORDER BY updated_at DESC, id DESC LIMIT 1
            """,
            (platform,),
        ).fetchone()
        last_fetched_at = (
            str(fetched[0]) if fetched is not None and fetched[0] is not None else None
        )
        refresh_status: ViralRefreshStatus
        if refresh is not None and str(refresh[0]) in {"PENDING", "RUNNING"}:
            refresh_status = "refreshing"
        elif refresh is not None and str(refresh[0]) == "FAILED":
            refresh_status = "error"
        elif last_fetched_at is not None:
            refresh_status = "ok"
        elif source_configured:
            refresh_status = "configured_only"
        else:
            refresh_status = "not_configured"
        platforms.append(
            ViralPlatformStatus(
                platform=platform,
                cached_videos=int(cached[0]) if cached is not None else 0,
                last_fetched_at=last_fetched_at,
                refresh_status=refresh_status,
                last_refresh_error=(
                    str(refresh[1]) if refresh is not None and refresh[1] is not None else None
                ),
            )
        )
    return ViralRuntimeResponse(
        collection_enabled=bool(controls[0]) if controls is not None else False,
        import_enabled=bool(controls[1]) if controls is not None else False,
        pending_imports=counts.get("PENDING", 0),
        running_imports=counts.get("RUNNING", 0),
        failed_imports=counts.get("FAILED", 0),
        pending_refreshes=refresh_counts.get("PENDING", 0),
        running_refreshes=refresh_counts.get("RUNNING", 0),
        failed_refreshes=refresh_counts.get("FAILED", 0),
        source_configured=source_configured,
        platforms=platforms,
        keywords=[ViralKeywordConfig.model_validate(item) for item in json.loads(controls[2])]
        if controls
        else [],
        per_keyword_limit=int(controls[3]) if controls else 10,
        next_collection_at=str(controls[4]) if controls and controls[4] else None,
        collection_interval_days=int(controls[5]) if controls else 7,
    )


@router.get("/settings/viral", response_model=ViralRuntimeResponse)
def read_viral_runtime(_actor: AdminReader) -> ViralRuntimeResponse:
    with pg_transaction() as conn:
        return _viral_runtime_response(conn)


@router.patch("/settings/viral", response_model=ViralRuntimeResponse)
def update_viral_runtime(
    payload: ViralRuntimeUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        # 单例行必须用 upsert：原先「先 UPDATE 再看 rowcount 补 INSERT」在并发
        # 首写下两个请求都会走 INSERT，撞 id 主键 → 500（2026-09-12 评审 P3
        # 记载的 "runtime upsert 非原子"）。ON CONFLICT DO UPDATE 由 PG 在一条
        # 语句里保证原子，不再有窗口。
        conn.execute(
            """
            INSERT INTO viral_runtime_controls (
                id, collection_enabled, import_enabled, updated_by_user_id
            ) VALUES (1, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                collection_enabled = EXCLUDED.collection_enabled,
                import_enabled = EXCLUDED.import_enabled,
                updated_by_user_id = EXCLUDED.updated_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(payload.collection_enabled), int(payload.import_enabled), actor.user_id),
        )
        if payload.keywords is not None:
            conn.execute(
                "UPDATE viral_runtime_controls SET keywords_json=%s WHERE id=1",
                (json.dumps([item.model_dump() for item in payload.keywords], ensure_ascii=False),),
            )
        if payload.per_keyword_limit is not None:
            conn.execute(
                "UPDATE viral_runtime_controls SET per_keyword_limit=%s WHERE id=1",
                (payload.per_keyword_limit,),
            )
        if payload.collection_interval_days is not None:
            conn.execute(
                """UPDATE viral_runtime_controls SET
                    next_collection_at=CASE WHEN collection_interval_days!=%s
                        AND next_collection_at IS NOT NULL
                        THEN CURRENT_TIMESTAMP + (%s * interval '1 day')
                        ELSE next_collection_at END,
                    collection_interval_days=%s WHERE id=1""",
                (payload.collection_interval_days,) * 3,
            )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, %s, 'viral_runtime.update', 'viral_runtime_controls', '1', %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "collection_enabled": payload.collection_enabled,
                        "import_enabled": payload.import_enabled,
                        "keywords": [item.model_dump() for item in payload.keywords]
                        if payload.keywords is not None
                        else None,
                        "per_keyword_limit": payload.per_keyword_limit,
                        "collection_interval_days": payload.collection_interval_days,
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return _viral_runtime_response(conn).model_dump()

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
        unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
    )


@router.patch(
    "/viral/videos/{platform}/{video_id:path}/availability",
    response_model=ViralAvailabilityResponse,
)
def update_viral_video_availability(
    platform: Literal["douyin", "wechat_channels"],
    video_id: str,
    payload: ViralAvailabilityUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        video = conn.execute(
            "SELECT 1 FROM viral_videos WHERE platform = %s AND video_id = %s",
            (platform, video_id),
        ).fetchone()
        if video is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "Viral video was not found.")
        conn.execute(
            """
            INSERT INTO viral_video_visibility (
                platform, video_id, status, reason, updated_by_user_id, updated_at
            ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (platform, video_id) DO UPDATE SET
                status = excluded.status,
                reason = excluded.reason,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (platform, video_id, payload.status, payload.reason.strip(), actor.user_id),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, %s, 'viral_video.availability_update', 'viral_video', %s, %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{platform}:{video_id}",
                json.dumps(
                    {
                        "platform": platform,
                        "video_id": video_id,
                        "status": payload.status,
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return ViralAvailabilityResponse(
            platform=platform, video_id=video_id, status=payload.status
        ).model_dump()

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
        unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
    )


class ViralCurationRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    action: Literal["feature", "unfeature", "delete"]


@router.get("/viral/videos")
def read_collected_viral_videos(
    _actor: AdminReader,
    platform: Literal["douyin", "wechat_channels"] | None = None,
    query: Annotated[str, Query(max_length=100)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
) -> dict[str, object]:
    filters = "v.deleted_at IS NULL AND v.platform IN ('douyin','wechat_channels')"
    params: list[object] = []
    if platform:
        filters += " AND v.platform=%s"
        params.append(platform)
    if query.strip():
        filters += " AND (v.title ILIKE %s OR v.author ILIKE %s OR v.video_id ILIKE %s)"
        params.extend([f"%{query.strip()}%"] * 3)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        total = conn.execute(
            f"SELECT count(*) FROM viral_videos v WHERE {filters}", tuple(params)
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT v.platform,v.video_id,v.category,v.title,v.author,v.duration_ms,
                v.likes,v.comments,v.shares,v.collects,v.published_at,v.created_at,
                v.homepage_featured,v.collection_published,v.cover_key,
                v.native_json::jsonb->>'_statistics_checked_at' AS statistics_checked_at,
                v.native_json::jsonb->>'_statistics_retry_at' AS statistics_retry_at,
                (COALESCE(v.cover_url,'') != '') AS cover_required,
                COALESCE(m.status,'NOT_STARTED') AS media_status,m.storage_uri,
                r.status AS archive_status,r.error_message_redacted AS archive_error
            FROM viral_videos v LEFT JOIN viral_media_preparations m
                ON m.platform=v.platform AND m.video_id=v.video_id AND m.media_kind='video'
            LEFT JOIN viral_refresh_tasks r ON r.platform=v.platform AND r.sort='latest'
                AND r.collection_config_json::jsonb->>'kind'='single_archive'
                AND r.collection_config_json::jsonb->>'video_id'=v.video_id
            WHERE {filters} ORDER BY v.created_at DESC,v.platform,v.video_id {PAGE_CLAUSE}""",
            (*params, limit, offset),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["homepage_featured"] = bool(item["homepage_featured"])
        item["collection_published"] = bool(item["collection_published"])
        items.append(item)
    return {"items": items, "total": int(total), "offset": offset, "limit": limit}


@router.get("/viral/discoveries")
def read_viral_search_discoveries(
    request: Request,
    _actor: AdminReader,
) -> dict[str, object]:
    """搜索发现每日汇总（运营视图）：按关键词×平台的热度分组.

    一行 = 一个 (keyword, platform) 分组；`users`/`videos` 为去重覆盖数。
    明细（谁在什么时候搜到什么）由客户侧 `GET /api/viral/search/discoveries`
    与内容池 `GET /api/control/viral/videos` 交叉查看。
    """
    search_date_raw = request.query_params.get("date", "")
    resolved_date = search_date_raw.strip()
    if not resolved_date:
        raise HTTPException(
            status_code=422,
            detail={"code": "VIRAL_SEARCH_DATE_REQUIRED", "message": "日期参数必填。"},
        )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved_date):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_DATE_INVALID",
                "message": "日期格式应为 YYYY-MM-DD。",
            },
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        totals = conn.execute(
            "SELECT count(*),count(DISTINCT user_id),"
            "count(DISTINCT platform || '/' || video_id) "
            "FROM viral_search_discoveries WHERE search_date=%s",
            (resolved_date,),
        ).fetchone()
        rows = conn.execute(
            "SELECT keyword,platform,count(*) AS discoveries,"
            "count(DISTINCT user_id) AS users,count(DISTINCT video_id) AS videos "
            "FROM viral_search_discoveries WHERE search_date=%s "
            "GROUP BY keyword,platform "
            "ORDER BY discoveries DESC,keyword,platform LIMIT 100",
            (resolved_date,),
        ).fetchall()
    return {
        "date": resolved_date,
        "total": int(totals[0]),
        "users": int(totals[1]),
        "videos": int(totals[2]),
        "keywords": [dict(row) for row in rows],
    }


@router.post("/viral/videos/{platform}/{video_id:path}/archive", status_code=202)
def archive_collected_viral_video(
    platform: Literal["douyin", "wechat_channels"],
    video_id: str,
    payload: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        # Serialize against the scheduler; reuse the durable latest slot without
        # replacing a running collection or introducing an in-process job.
        control = conn.execute(
            "SELECT collection_enabled FROM viral_runtime_controls WHERE id=1 FOR UPDATE"
        ).fetchone()
        if control is None or not control[0]:
            raise http_error(409, "VIRAL_COLLECTION_PAUSED", "后台采集已暂停，请先开启采集服务。")
        video = conn.execute(
            "SELECT 1 FROM viral_videos WHERE platform=%s AND video_id=%s "
            "AND deleted_at IS NULL FOR UPDATE",
            (platform, video_id),
        ).fetchone()
        if video is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频不存在或已删除。")
        active = conn.execute(
            "SELECT id,collection_config_json FROM viral_refresh_tasks "
            "WHERE platform=%s AND status IN ('PENDING','RUNNING') FOR UPDATE",
            (platform,),
        ).fetchall()
        for task in active:
            config = json.loads(task[1])
            if config.get("kind") == "single_archive" and config.get("video_id") == video_id:
                return {"task_id": task[0], "queued": True}
        if active:
            raise http_error(409, "VIRAL_ARCHIVE_BUSY", "该平台已有后台任务，请完成后再转存。")
        queued = conn.execute(
            """INSERT INTO viral_refresh_tasks(id,platform,sort,collection_config_json)
            VALUES(%s,%s,'latest',%s) ON CONFLICT(platform,sort) DO UPDATE SET
                status='PENDING',collection_config_json=excluded.collection_config_json,
                checkpoint_json='{}',retry_count=0,locked_by=NULL,locked_until=NULL,
                error_code=NULL,error_message_redacted=NULL,retryable=0,
                started_at=NULL,completed_at=NULL,updated_at=CURRENT_TIMESTAMP RETURNING id""",
            (
                str(uuid.uuid4()),
                platform,
                json.dumps({"kind": "single_archive", "video_id": video_id}),
            ),
        ).fetchone()
        if queued is None:
            raise RuntimeError("viral archive task enqueue did not persist")
        task_id = str(queued[0])
        conn.execute(
            """INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json)
            VALUES(%s,%s,'viral_video.archive','viral_video',%s,%s)""",
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{platform}:{video_id}",
                json.dumps(
                    {"request_id": request_id, "reason": payload.reason.strip(), "task_id": task_id}
                ),
            ),
        )
        return {"task_id": task_id, "queued": True}

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=202,
        unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
        unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
    )


@router.post("/viral/videos/wechat_channels/{video_id:path}/statistics")
def refresh_collected_wechat_statistics(
    video_id: str,
    payload: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    from app.viral_statistics import refresh_viral_statistics
    from app.viral_store import STATISTICS_CHECKED_AT_KEY, STATISTICS_RETRY_AT_KEY
    from app.viral_tikhub import viral_source_client_from_settings

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        # Serialize same-record refreshes across API instances without holding
        # its row lock during provider I/O (the collector also updates this row).
        raw.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"viral-statistics:{video_id}",),
        )
        if (
            raw.execute(
                "SELECT 1 FROM viral_videos WHERE platform='wechat_channels' "
                "AND video_id=%s AND deleted_at IS NULL",
                (video_id,),
            ).fetchone()
            is None
        ):
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频不存在或已删除。")
        conn = BusinessConnection.postgres(raw)
        videos = refresh_viral_statistics(
            conn,
            viral_source_client_from_settings(conn),
            [video_id],
        )
        if not videos:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频已删除。")
        video = videos[0]
        checked = video.native.get(STATISTICS_CHECKED_AT_KEY)
        retry = video.native.get(STATISTICS_RETRY_AT_KEY)
        status = (
            "failure"
            if retry
            else (
                "complete"
                if all(v is not None for v in (video.comments, video.shares, video.collects))
                else "partial"
            )
        )
        result: dict[str, object] = {
            "video_id": video_id,
            "likes": video.likes,
            "comments": video.comments,
            "shares": video.shares,
            "collects": video.collects,
            "statistics_checked_at": checked,
            "statistics_retry_at": retry,
            "statistics_status": status,
        }
        raw.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_video.statistics','viral_video',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"wechat_channels:{video_id}",
                json.dumps({**result, "reason": payload.reason.strip(), "request_id": request_id}),
            ),
        )
        return result

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="VIRAL_STATISTICS_UNAVAILABLE",
        unavailable_message="互动数据暂时无法获取，请稍后重试。",
    )


@router.get("/viral/videos/{platform}/{video_id:path}/preview")
def preview_collected_viral_video(
    platform: Literal["douyin", "wechat_channels"], video_id: str, _actor: AdminReader
) -> dict[str, str]:
    from app.media_routes import get_media_storage
    from app.viral_media import ViralMediaPipeline
    from app.viral_routes import _browser_playable_url
    from app.viral_store import get_viral_video
    from app.viral_tikhub import ViralSourceError

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        video = get_viral_video(conn, platform=platform, video_id=video_id)
        if video is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频不存在或已删除。")
        storage = get_media_storage(conn)
    try:
        result = ViralMediaPipeline(
            client=None, storage=storage, shared=True, cached_only=True
        ).fetch(video, prefer="video")
    except ViralSourceError as exc:
        raise http_error(409, "VIRAL_MEDIA_NOT_READY", "云端素材尚未准备完成。") from exc
    # Recheck after storage I/O so a concurrent deletion cannot return a fresh URL.
    with pg_transaction() as raw:
        if (
            get_viral_video(BusinessConnection.postgres(raw), platform=platform, video_id=video_id)
            is None
        ):
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频已删除。")
    return {"url": _browser_playable_url(result.url, _actor.user_id)}


def _prepare_feature_cover(platform: str, video_id: str) -> tuple[str, str] | None:
    from app.media_routes import get_media_storage
    from app.viral_media import CoverEnricher, UrlFetcher
    from app.viral_store import get_viral_video

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        video = get_viral_video(conn, platform=platform, video_id=video_id)
        if video is None or video.cover_key or not video.cover_url:
            return None
        ready = conn.execute(
            "SELECT 1 FROM viral_media_preparations WHERE platform=%s AND video_id=%s "
            "AND media_kind='video' AND status='SUCCEEDED' AND storage_uri IS NOT NULL",
            (platform, video_id),
        ).fetchone()
        if ready is None:
            return None
        storage = get_media_storage(conn)
    # Only the existing public cover is fetched, with a bounded download and
    # deterministic cache key. No provider collection request or long PG lock.
    cover = CoverEnricher(storage=storage, fetcher=UrlFetcher(max_bytes=10 * 1024 * 1024)).enrich(
        video
    )
    return (video.cover_url, cover.cover_key) if cover.cover_key else None


@router.patch("/viral/videos/{platform}/{video_id:path}/curation")
def curate_collected_viral_video(
    platform: Literal["douyin", "wechat_channels"],
    video_id: str,
    payload: ViralCurationRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    require_write_contract(request, payload)
    prepared_cover = (
        _prepare_feature_cover(platform, video_id) if payload.action == "feature" else None
    )

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT cover_url,cover_key FROM viral_videos WHERE platform=%s AND video_id=%s "
            "AND deleted_at IS NULL FOR UPDATE",
            (platform, video_id),
        ).fetchone()
        if row is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频不存在或已删除。")
        if payload.action == "feature":
            ready = conn.execute(
                "SELECT 1 FROM viral_media_preparations WHERE platform=%s AND video_id=%s "
                "AND media_kind='video' AND status='SUCCEEDED' AND storage_uri IS NOT NULL",
                (platform, video_id),
            ).fetchone()
            if ready is None:
                raise http_error(
                    409, "VIRAL_MEDIA_NOT_READY", "视频和封面归档完成后才能展示到首页。"
                )
            if row[0] and not row[1]:
                if prepared_cover is None or prepared_cover[0] != row[0]:
                    raise http_error(
                        409, "VIRAL_COVER_NOT_READY", "视频已归档，但封面暂时无法获取，请稍后重试。"
                    )
                conn.execute(
                    "UPDATE viral_videos SET cover_key=%s WHERE platform=%s AND video_id=%s",
                    (prepared_cover[1], platform, video_id),
                )
            hidden = conn.execute(
                "SELECT 1 FROM viral_video_visibility WHERE platform=%s AND video_id=%s "
                "AND status!='AVAILABLE'",
                (platform, video_id),
            ).fetchone()
            if hidden:
                raise http_error(
                    409, "VIRAL_VIDEO_UNAVAILABLE", "该视频已下架或隐藏，请先恢复可用状态。"
                )
        conn.execute(
            """UPDATE viral_videos SET homepage_featured=%s,
                collection_published=CASE WHEN %s THEN 1 ELSE collection_published END,
                deleted_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE deleted_at END
            WHERE platform=%s AND video_id=%s""",
            (
                int(payload.action == "feature"),
                payload.action == "feature",
                payload.action == "delete",
                platform,
                video_id,
            ),
        )
        conn.execute(
            """INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json)
            VALUES(%s,%s,'viral_video.curation','viral_video',%s,%s)""",
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{platform}:{video_id}",
                json.dumps(
                    {
                        "action": payload.action,
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return {
            "platform": platform,
            "video_id": video_id,
            "homepage_featured": payload.action == "feature",
            "deleted": payload.action == "delete",
        }

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
        unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
    )


@router.get("/settings/queue-mode", response_model=QueueModeResponse)
def read_queue_mode(_actor: AdminReader) -> QueueModeResponse:
    """The current queue mode for the admin UI (revised ADR §4)."""
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
    return QueueModeResponse(fair_queue_enabled=bool(row[0]) if row is not None else False)


@router.patch("/settings/queue-mode", response_model=QueueModeResponse)
def update_queue_mode(
    payload: QueueModeUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Flip the fair-queue rollout switch as an audited, idempotent admin
    write behind the shared write contract (PR #85 review P2).

    Uses the runtime_settings row's queue-mode column directly (not the
    internal ``save_runtime_settings`` limits upsert): the switch-only write
    must not require restating unrelated runtime limits, and the limits
    themselves stay on the internal settings lane. When the settings row does
    not exist yet (a fresh database whose limits were never configured), the
    documented defaults seed it so the switch always lands on a real row.
    """

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        # 同 viral 那处的理由：单例行「先 UPDATE 再补 INSERT」在并发首写下会
        # 双双 INSERT 撞主键 500，改成一条 upsert。首次落库仍按文档化的默认值
        # 播种（这一段 INSERT 的列清单就是默认值来源），已存在则只翻转开关，
        # 不覆盖其它运行参数。
        conn.execute(
            """
            INSERT INTO runtime_settings (
                id, max_generation_count_per_batch, max_concurrent_h3_tasks,
                internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen,
                active_storage_provider, fair_queue_enabled, updated_by_user_id,
                created_at, updated_at
            ) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
                fair_queue_enabled = EXCLUDED.fair_queue_enabled,
                updated_by_user_id = EXCLUDED.updated_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                DEFAULT_RUNTIME_SETTINGS["max_generation_count_per_batch"],
                DEFAULT_RUNTIME_SETTINGS["max_concurrent_h3_tasks"],
                DEFAULT_BILLING_SETTINGS["internal_base_unit_price_fen"],
                DEFAULT_BILLING_SETTINGS["min_recharge_fen"],
                DEFAULT_BILLING_SETTINGS["recharge_step_fen"],
                DEFAULT_RUNTIME_SETTINGS["active_storage_provider"],
                payload.fair_queue_enabled,
                actor.user_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, %s, 'runtime_settings.update', 'runtime_settings', '1', %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "fair_queue_enabled": payload.fair_queue_enabled,
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                        "setting": "queue_mode",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        row = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
        return QueueModeResponse(
            fair_queue_enabled=bool(row[0]) if row is not None else False
        ).model_dump()

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
        unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
    )
