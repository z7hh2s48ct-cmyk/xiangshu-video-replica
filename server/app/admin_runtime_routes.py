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


@router.post("/viral/collect", status_code=202)
def collect_viral_now(
    payload: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """立即采集：置 ``next_collection_at`` 为当前并直接入队一次关键词采集.

    与定时采集共用 ``enqueue_due_viral_collections``：关键词、平台、每词上限与
    共享账单批次都在同一事务内建立；若已有在队/在制的采集任务则本次不重复入队
    （与定时调度一致，避免并发采集互踩）。
    """
    from app.viral_collection import enqueue_due_viral_collections

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        control = conn.execute(
            "SELECT collection_enabled, keywords_json FROM viral_runtime_controls WHERE id = 1"
        ).fetchone()
        if control is None or not bool(control[0]):
            raise http_error(409, "VIRAL_COLLECTION_PAUSED", "后台采集已暂停，请先开启采集服务。")
        if not json.loads(str(control[1] or "[]")):
            raise http_error(409, "VIRAL_COLLECTION_KEYWORDS_REQUIRED", "请先配置采集关键词。")
        # 置 next_collection_at 为当前，让 enqueue_due_viral_collections 判为「到期」立即入队。
        conn.execute(
            "UPDATE viral_runtime_controls SET next_collection_at = CURRENT_TIMESTAMP WHERE id = 1"
        )
        enqueue_due_viral_collections(BusinessConnection.postgres(conn))
        conn.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_collection.collect_now','viral_runtime_controls','1',%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps({"request_id": request_id, "reason": payload.reason.strip()}),
            ),
        )
        return {"queued": True}

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


class ViralCurationRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    action: Literal["feature", "unfeature", "delete"]


# 视频库与管理端搜索共用的行投影：viral_videos 左联云端媒体准备与单条转存
# 任务，给出前端「转存 / 上首页」按钮需要的全部状态。
_COLLECTED_VIRAL_ROW_SELECT = """
    SELECT v.platform,v.video_id,v.category,v.title,v.author,v.duration_ms,
        v.likes,v.comments,v.shares,v.collects,v.published_at,v.created_at,
        v.homepage_featured,v.collection_published,v.cover_key,
        v.native_json::jsonb->>'_statistics_checked_at' AS statistics_checked_at,
        v.native_json::jsonb->>'_statistics_retry_at' AS statistics_retry_at,
        (COALESCE(v.cover_url,'') != '') AS cover_required,
        COALESCE(m.status,'NOT_STARTED') AS media_status,m.storage_uri,
        r.status AS archive_status,r.error_message_redacted AS archive_error,
        COALESCE(vis.status,'AVAILABLE') AS availability
    FROM viral_videos v LEFT JOIN viral_media_preparations m
        ON m.platform=v.platform AND m.video_id=v.video_id AND m.media_kind='video'
    LEFT JOIN viral_refresh_tasks r ON r.platform=v.platform AND r.sort='latest'
        AND r.collection_config_json::jsonb->>'kind'='single_archive'
        AND r.collection_config_json::jsonb->>'video_id'=v.video_id
    LEFT JOIN viral_video_visibility vis
        ON vis.platform=v.platform AND vis.video_id=v.video_id
"""


def _collected_video_payload(row: dict[str, object]) -> dict[str, object]:
    """行 → 响应：只保留视频库行字段，布尔列还原为 bool，时间戳转 ISO.

    明细视图会把分组列与视频列混在一行里，这里按白名单挑列，避免把
    ``discoveries``/``total`` 这类分组字段漏进 video 对象；ISO 化是因为
    幂等快照层用 ``json.dumps`` 落响应，datetime 不能直接序列化。
    """
    item = {key: row[key] for key in _COLLECTED_VIDEO_COLUMNS if key in row}
    item["homepage_featured"] = bool(item["homepage_featured"])
    item["collection_published"] = bool(item["collection_published"])
    created_at = item.get("created_at")
    if created_at is not None and hasattr(created_at, "isoformat"):
        item["created_at"] = created_at.isoformat()
    return item


# 行投影里响应侧的列名（与 _COLLECTED_VIRAL_ROW_SELECT 的别名一一对应）。
_COLLECTED_VIDEO_COLUMNS = (
    "platform",
    "video_id",
    "category",
    "title",
    "author",
    "duration_ms",
    "likes",
    "comments",
    "shares",
    "collects",
    "published_at",
    "created_at",
    "homepage_featured",
    "collection_published",
    "cover_key",
    "statistics_checked_at",
    "statistics_retry_at",
    "cover_required",
    "media_status",
    "storage_uri",
    "archive_status",
    "archive_error",
    "availability",
)


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
            f"{_COLLECTED_VIRAL_ROW_SELECT} WHERE {filters} "
            f"ORDER BY v.created_at DESC,v.platform,v.video_id {PAGE_CLAUSE}",
            (*params, limit, offset),
        ).fetchall()
    return {
        "items": [_collected_video_payload(dict(row)) for row in rows],
        "total": int(total),
        "offset": offset,
        "limit": limit,
    }


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


@router.get("/viral/discoveries/detail")
def read_viral_discovery_details(
    _actor: AdminReader,
    date: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
    platform: Literal["douyin", "wechat_channels"] | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, object]:
    """用户搜索记录的逐条明细：按 (关键词×平台×视频) 分组，左联内容池.

    聚合视图（``GET /viral/discoveries``）只回答「哪个词最热」；运营要据此
    把具体视频转存/上首页时需要这一层：每行带视频的归档与首页状态，操作
    语义与视频库完全一致。内容池里已删除的视频 ``video`` 为 null，仅保留
    发现记录。日期错误码与聚合端点一致（不设 max_length，避免 FastAPI 的
    参数校验 422 抢在业务错误码之前返回）。
    """
    resolved_date = (date or "").strip()
    if not resolved_date:
        raise HTTPException(
            status_code=422,
            detail={"code": "VIRAL_SEARCH_DATE_REQUIRED", "message": "日期参数必填。"},
        )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved_date):
        raise HTTPException(
            status_code=422,
            detail={"code": "VIRAL_SEARCH_DATE_INVALID", "message": "日期格式应为 YYYY-MM-DD。"},
        )
    filters = "d.search_date = %s"
    params: list[object] = [resolved_date]
    if (keyword or "").strip():
        filters += " AND d.keyword = %s"
        params.append((keyword or "").strip())
    if platform:
        filters += " AND d.platform = %s"
        params.append(platform)
    # 分页在分组后的 CTE 内完成：count(*) OVER () 在 LIMIT 前取值，
    # 因此 total 是全量分组数而不是本页行数。
    sql = f"""
        WITH grouped AS (
            SELECT d.keyword, d.platform, d.video_id,
                   count(*) AS discoveries,
                   count(DISTINCT d.user_id) AS users,
                   max(d.searched_at) AS last_searched_at,
                   count(*) OVER () AS total
            FROM viral_search_discoveries d
            WHERE {filters}
            GROUP BY d.keyword, d.platform, d.video_id
            ORDER BY max(d.searched_at) DESC, d.keyword, d.platform, d.video_id
            LIMIT %s OFFSET %s
        )
        SELECT g.keyword, g.platform, g.video_id, g.discoveries, g.users,
               g.last_searched_at, g.total,
               v.title, v.author, v.category, v.duration_ms, v.likes, v.comments,
               v.shares, v.collects, v.published_at, v.created_at,
               v.homepage_featured, v.collection_published, v.cover_key,
               v.native_json::jsonb->>'_statistics_checked_at' AS statistics_checked_at,
               v.native_json::jsonb->>'_statistics_retry_at' AS statistics_retry_at,
               (COALESCE(v.cover_url,'') != '') AS cover_required,
               COALESCE(m.status,'NOT_STARTED') AS media_status, m.storage_uri,
               r.status AS archive_status, r.error_message_redacted AS archive_error
        FROM grouped g
        LEFT JOIN viral_videos v
            ON v.platform = g.platform AND v.video_id = g.video_id
            AND v.deleted_at IS NULL
        LEFT JOIN viral_media_preparations m
            ON m.platform = g.platform AND m.video_id = g.video_id
            AND m.media_kind = 'video'
        LEFT JOIN viral_refresh_tasks r ON r.platform = g.platform AND r.sort = 'latest'
            AND r.collection_config_json::jsonb->>'kind' = 'single_archive'
            AND r.collection_config_json::jsonb->>'video_id' = g.video_id
        ORDER BY g.last_searched_at DESC, g.keyword, g.platform, g.video_id
    """
    with pg_transaction() as raw:
        # 命名行访问（dict(row)）必须走 BusinessConnection 的行工厂，
        # 裸 psycopg 连接返回的是普通元组。
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    items: list[dict[str, object]] = []
    total = 0
    for row in rows:
        mapping = dict(row)
        total = int(mapping["total"])
        items.append(
            {
                "keyword": str(mapping["keyword"]),
                "platform": str(mapping["platform"]),
                "videoId": str(mapping["video_id"]),
                "discoveries": int(mapping["discoveries"]),
                "users": int(mapping["users"]),
                "lastSearchedAt": str(mapping["last_searched_at"]),
                # 内容池已无此视频（或从未入库）时 video 为 null。
                "video": (
                    _collected_video_payload(mapping) if mapping["title"] is not None else None
                ),
            }
        )
    return {
        "date": resolved_date,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
    }


class ViralAdminSearchRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=100)
    platform: Literal["douyin", "wechat_channels"] = "douyin"
    time_range: Literal["all", "day", "week", "half_year"] = "week"
    cursor: str | None = Field(default=None, max_length=2048)


@router.post("/viral/search")
def search_viral_videos_for_admin(
    payload: ViralAdminSearchRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """管理端实时搜索：外呼上游 → 结果并入内容池，返回视频库同构行.

    与客户侧 ``POST /api/viral/search`` 的三点区别：不预留/结算客户积分
    （供应商成本由 ``billing_meter.meter_call`` 在无计费上下文时自动落
    平台操作单）；不写 ``viral_search_discoveries``（管理员自己的搜索不
    污染用户搜索统计）；命中视频直接 upsert 进内容池，响应行与视频库
    同构，前端可以直接继续「转存到云端 / 展示到首页」。

    外呼失败走 http_error 抛出：整个事务回滚（占位行一并回滚），
    幂等键保持可用，管理员重试不冲突。
    """

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        from app.media_routes import get_media_storage
        from app.viral_search import (
            archive_search_covers_bounded,
            run_viral_search_bounded,
        )
        from app.viral_store import update_viral_cover, upsert_viral_videos
        from app.viral_tikhub import (
            ViralSourceError,
            ViralSourceUnavailable,
            viral_source_client_from_settings,
        )

        keyword = payload.keyword.strip()
        if not keyword:
            raise http_error(422, "VIRAL_SEARCH_KEYWORD_REQUIRED", "搜索关键词不能为空。")
        try:
            client = viral_source_client_from_settings(BusinessConnection.postgres(conn))
        except ViralSourceUnavailable as exc:
            raise http_error(
                503, "VIRAL_ADMIN_SEARCH_UNAVAILABLE", "爆款视频数据源暂不可用，请先配置数据源。"
            ) from exc
        try:
            # 限时外呼：服务器 DNS/出网挂起是 urllib 超时盖不住的盲区，
            # 必须有界失败，否则外呼挂在本写事务里直到被 PG 会话超时击穿。
            page = run_viral_search_bounded(
                client,
                keyword=keyword,
                platform=payload.platform,
                cursor=(payload.cursor or "").strip() or None,
                time_range=payload.time_range,
            )
        except ViralSourceError as exc:
            raise http_error(503, "VIRAL_ADMIN_SEARCH_UPSTREAM_FAILED", str(exc)) from exc
        except TimeoutError as exc:
            raise http_error(
                503, "VIRAL_ADMIN_SEARCH_UPSTREAM_TIMEOUT", "搜索服务响应超时，请稍后重试。"
            ) from exc
        storage = get_media_storage(BusinessConnection.postgres(conn))
        try:
            enriched = archive_search_covers_bounded(storage, page.items)
        except TimeoutError:
            # 封面归档超时不放弃整页结果：未归档条目保留源站链接兜底。
            enriched = page.items
        upsert_viral_videos(BusinessConnection.postgres(conn), enriched, commit=False)
        for video in enriched:
            if video.cover_key:
                update_viral_cover(
                    BusinessConnection.postgres(conn),
                    platform=video.platform,
                    video_id=video.video_id,
                    cover_key=video.cover_key,
                    commit=False,
                )
        items: list[dict[str, object]] = []
        if enriched:
            # 用内容池当前数据回显（含归档/首页状态），与视频库行同构；
            # 顺序保持搜索返回的先后。单平台搜索，两个数组取交集不会串行。
            # 命名行访问必须走 BusinessConnection 行工厂（dict(row)）。
            business_conn = BusinessConnection.postgres(conn)
            platforms = sorted({video.platform for video in enriched})
            video_ids = [video.video_id for video in enriched]
            found = business_conn.execute(
                f"{_COLLECTED_VIRAL_ROW_SELECT} "
                "WHERE v.deleted_at IS NULL AND v.platform = ANY(%s) "
                "AND v.video_id = ANY(%s)",
                (platforms, video_ids),
            ).fetchall()
            by_key = {
                (str(row["platform"]), str(row["video_id"])): _collected_video_payload(dict(row))
                for row in found
            }
            items = [
                by_key[(video.platform, video.video_id)]
                for video in enriched
                if (video.platform, video.video_id) in by_key
            ]
        conn.execute(
            """INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json)
            VALUES(%s,%s,'viral_video.admin_search','viral_video',%s,%s)""",
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{payload.platform}:{keyword}",
                json.dumps(
                    {
                        "keyword": keyword,
                        "platform": payload.platform,
                        "time_range": payload.time_range,
                        "cursor_present": bool((payload.cursor or "").strip()),
                        "results": len(items),
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return {
            "items": items,
            "cursor": page.cursor,
            "hasMore": page.has_more,
            "keyword": keyword,
            "platform": payload.platform,
            "timeRange": payload.time_range,
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
