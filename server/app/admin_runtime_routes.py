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
from datetime import UTC, datetime, time
from typing import Annotated, Literal
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_dates import SHANGHAI, utc_timestamp_sql
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
    # 采集质量规则与月度预算（方案 P1 内容模块·采集设置）。
    quality_min_likes: int | None = None
    quality_duration_min_ms: int | None = None
    quality_duration_max_ms: int | None = None
    quality_exclude_words: list[str] = Field(default_factory=list)
    monthly_budget_fen: int | None = None
    month_spend_fen: int = 0
    next_collection_at: str | None = None
    collection_interval_days: int = 7


class ViralRuntimeUpdateRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    collection_enabled: bool
    import_enabled: bool
    keywords: list[ViralKeywordConfig] | None = Field(default=None, max_length=20)
    per_keyword_limit: int | None = Field(default=None, ge=1, le=50)
    collection_interval_days: Literal[1, 7] | None = None
    quality_min_likes: int | None = Field(default=None, ge=0, le=10_000_000)
    quality_duration_min_ms: int | None = Field(default=None, ge=0, le=24 * 3600 * 1000)
    quality_duration_max_ms: int | None = Field(default=None, ge=0, le=24 * 3600 * 1000)
    quality_exclude_words: list[str] | None = Field(default=None, max_length=50)
    monthly_budget_fen: int | None = Field(default=None, ge=1, le=10_000_000)

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
        "next_collection_at, collection_interval_days, quality_min_likes, "
        "quality_duration_min_ms, quality_duration_max_ms, quality_exclude_words_json, "
        "monthly_budget_fen FROM viral_runtime_controls WHERE id = 1"
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
        quality_min_likes=int(controls[6]) if controls and controls[6] is not None else None,
        quality_duration_min_ms=int(controls[7]) if controls and controls[7] is not None else None,
        quality_duration_max_ms=int(controls[8]) if controls and controls[8] is not None else None,
        quality_exclude_words=[str(word) for word in json.loads(controls[9] or "[]")]
        if controls
        else [],
        monthly_budget_fen=int(controls[10]) if controls and controls[10] is not None else None,
        # 本月已用：上海挂钟月首之后的平台侧采集成本（用户为空的平台请求行）。
        month_spend_fen=(lambda _row: int(_row[0]) if _row is not None else 0)(
            conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(a.cost_fen, 0)), 0)
                FROM billing_attempts a
                JOIN billing_operations o ON o.id = a.operation_id
                WHERE o.user_id IS NULL AND o.collection_batch_id IS NOT NULL
                  AND a.completed_at::timestamp AT TIME ZONE 'UTC'
                      >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai')
                         AT TIME ZONE 'Asia/Shanghai'
                """
            ).fetchone()
        ),
    )


@router.get("/settings/viral", response_model=ViralRuntimeResponse)
def read_viral_runtime(_actor: AdminReader) -> ViralRuntimeResponse:
    with pg_transaction() as conn:
        return _viral_runtime_response(conn)


# ---------------------------------------------------------------------------
# 采集关键词的单条增删（方案 P1 内容模块 C-12）：整表覆盖会让两个管理员
# 的并发编辑互相吞掉；「实时搜索 → 加入采集关键词」也改为走这里的原子
# 单条写，不再先读后写。
# ---------------------------------------------------------------------------


class ViralKeywordAddRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"]
    category: str = Field(min_length=1, max_length=32)
    keyword: str = Field(min_length=1, max_length=80)


class ViralKeywordDeleteRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"]
    keyword: str = Field(min_length=1, max_length=80)


def _read_keyword_rows(conn: psycopg.Connection) -> list[dict[str, str]]:
    row = conn.execute(
        "SELECT keywords_json FROM viral_runtime_controls WHERE id=1 FOR UPDATE"
    ).fetchone()
    raw = json.loads(row[0]) if row and row[0] else []
    return [item for item in raw if isinstance(item, dict)]


@router.post("/viral/keywords", status_code=200)
def add_viral_keyword(
    payload: ViralKeywordAddRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        rows = _read_keyword_rows(conn)
        if any(
            item.get("platform") == payload.platform and item.get("keyword") == payload.keyword
            for item in rows
        ):
            return {"added": False, "total": len(rows), "request_id": request_id}
        if len(rows) >= 20:
            raise http_error(409, "VIRAL_KEYWORD_LIMIT", "采集关键词最多 20 个，请先删除不需要的。")
        rows.append(
            {
                "platform": payload.platform,
                "category": payload.category.strip(),
                "keyword": payload.keyword.strip(),
            }
        )
        conn.execute(
            "UPDATE viral_runtime_controls SET keywords_json=%s, "
            "updated_by_user_id=%s, updated_at=CURRENT_TIMESTAMP WHERE id=1",
            (json.dumps(rows, ensure_ascii=False), actor.user_id),
        )
        conn.execute(
            """INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json)
            VALUES(%s,%s,'viral_runtime.keyword_add','viral_keyword',%s,%s)""",
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{payload.platform}:{payload.keyword.strip()}",
                json.dumps(
                    {
                        "category": payload.category.strip(),
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return {"added": True, "total": len(rows), "request_id": request_id}

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


@router.post("/viral/keywords/delete", status_code=200)
def delete_viral_keyword(
    payload: ViralKeywordDeleteRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        rows = _read_keyword_rows(conn)
        remaining = [
            item
            for item in rows
            if not (
                item.get("platform") == payload.platform and item.get("keyword") == payload.keyword
            )
        ]
        if len(remaining) == len(rows):
            return {"deleted": False, "total": len(rows), "request_id": request_id}
        conn.execute(
            "UPDATE viral_runtime_controls SET keywords_json=%s, "
            "updated_by_user_id=%s, updated_at=CURRENT_TIMESTAMP WHERE id=1",
            (json.dumps(remaining, ensure_ascii=False), actor.user_id),
        )
        conn.execute(
            """INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json)
            VALUES(%s,%s,'viral_runtime.keyword_delete','viral_keyword',%s,%s)""",
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{payload.platform}:{payload.keyword.strip()}",
                json.dumps(
                    {"reason": payload.reason.strip(), "request_id": request_id},
                    ensure_ascii=False,
                ),
            ),
        )
        return {"deleted": True, "total": len(remaining), "request_id": request_id}

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
        quality_sets: list[str] = []
        quality_params: list[object] = []
        if payload.quality_min_likes is not None:
            quality_sets.append("quality_min_likes=%s")
            quality_params.append(payload.quality_min_likes)
        if payload.quality_duration_min_ms is not None:
            quality_sets.append("quality_duration_min_ms=%s")
            quality_params.append(payload.quality_duration_min_ms)
        if payload.quality_duration_max_ms is not None:
            quality_sets.append("quality_duration_max_ms=%s")
            quality_params.append(payload.quality_duration_max_ms)
        if payload.quality_exclude_words is not None:
            quality_sets.append("quality_exclude_words_json=%s")
            quality_params.append(
                json.dumps(
                    [word.strip() for word in payload.quality_exclude_words if word.strip()],
                    ensure_ascii=False,
                )
            )
        if payload.monthly_budget_fen is not None:
            quality_sets.append("monthly_budget_fen=%s")
            quality_params.append(payload.monthly_budget_fen)
        if quality_sets:
            quality_params.append(1)
            conn.execute(
                "UPDATE viral_runtime_controls SET " + ", ".join(quality_sets) + " WHERE id=%s",
                tuple(quality_params),
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
    # prepare（方案 P1 内容模块 C-2）：只排队准备素材，不改首页状态；
    # feature 遇到未准备的视频同样自动排队，就绪后由后台自动上首页。
    action: Literal["feature", "unfeature", "delete", "pin", "unpin", "prepare"]


# 视频库与管理端搜索共用的行投影：viral_videos 左联云端媒体准备与单条转存
# 任务，给出前端「转存 / 上首页」按钮需要的全部状态，以及列表要直接上屏的
# 头像 / 认证 / 发布时间 / 点赞显示值 / 话题标签（运营不必逐行展开才看到）。
_COLLECTED_VIRAL_ROW_SELECT = """
    SELECT v.platform,v.video_id,v.category,v.title,v.author,v.author_avatar,
        v.verified,v.duration_ms,
        v.likes,v.comments,v.shares,v.collects,v.published_at,v.created_at,
        v.published_display,v.like_display,v.tags_json,
        v.homepage_featured,v.collection_published,v.homepage_rank,v.cover_key,
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

# 列表状态分段的服务端口径：与前端 archiveLabel 同一套判定，避免两处漂移。
_COLLECTED_STATUS_FILTERS = {
    "ready": (
        "EXISTS (SELECT 1 FROM viral_media_preparations m2"
        " WHERE m2.platform=v.platform AND m2.video_id=v.video_id"
        " AND m2.media_kind='video' AND m2.status='SUCCEEDED' AND m2.storage_uri IS NOT NULL)"
    ),
    "pending": (
        "NOT EXISTS (SELECT 1 FROM viral_media_preparations m2"
        " WHERE m2.platform=v.platform AND m2.video_id=v.video_id"
        " AND m2.media_kind='video' AND m2.status='SUCCEEDED' AND m2.storage_uri IS NOT NULL)"
        " AND NOT EXISTS (SELECT 1 FROM viral_media_preparations m2"
        " WHERE m2.platform=v.platform AND m2.video_id=v.video_id"
        " AND m2.media_kind='video' AND m2.status='FAILED')"
        " AND NOT EXISTS (SELECT 1 FROM viral_refresh_tasks r2"
        " WHERE r2.platform=v.platform AND r2.sort='latest'"
        " AND r2.collection_config_json::jsonb->>'kind'='single_archive'"
        " AND r2.collection_config_json::jsonb->>'video_id'=v.video_id"
        " AND r2.status='FAILED')"
    ),
    "failed": (
        "EXISTS (SELECT 1 FROM viral_media_preparations m2"
        " WHERE m2.platform=v.platform AND m2.video_id=v.video_id"
        " AND m2.media_kind='video' AND m2.status='FAILED')"
        " OR EXISTS (SELECT 1 FROM viral_refresh_tasks r2"
        " WHERE r2.platform=v.platform AND r2.sort='latest'"
        " AND r2.collection_config_json::jsonb->>'kind'='single_archive'"
        " AND r2.collection_config_json::jsonb->>'video_id'=v.video_id"
        " AND r2.status='FAILED')"
    ),
    "featured": "v.homepage_featured=1",
}


def _collected_video_payload(row: dict[str, object]) -> dict[str, object]:
    """行 → 响应：只保留视频库行字段，布尔列还原为 bool，时间戳转 ISO.

    明细视图会把分组列与视频列混在一行里，这里按白名单挑列，避免把
    ``discoveries``/``total`` 这类分组字段漏进 video 对象；ISO 化是因为
    幂等快照层用 ``json.dumps`` 落响应，datetime 不能直接序列化。
    ``tags_json`` 是库内原始形态，不进响应：解析成数组后由 ``tags`` 承载。
    """
    item = {key: row[key] for key in _COLLECTED_VIDEO_COLUMNS if key in row}
    item["homepage_featured"] = bool(item["homepage_featured"])
    item["collection_published"] = bool(item["collection_published"])
    item["verified"] = bool(item.get("verified"))
    item["tags"] = _collected_tags(item.pop("tags_json", None))
    # 封面只下发站内代理地址（源站签名链接会过期，且代理路由不受 Referer 限制）；
    # 未归档封面副本时留空，前端渲染占位而不是加载一个必坏的外链。
    item["cover_url"] = (
        f"/api/viral/covers/{item['platform']}/{quote(str(item['video_id']), safe='')}"
        if item.get("cover_key")
        else None
    )
    created_at = item.get("created_at")
    if created_at is not None and hasattr(created_at, "isoformat"):
        item["created_at"] = created_at.isoformat()
    return item


def _collected_tags(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(tag) for tag in parsed] if isinstance(parsed, list) else []


# 行投影里响应侧的列名（与 _COLLECTED_VIRAL_ROW_SELECT 的别名一一对应）。
_COLLECTED_VIDEO_COLUMNS = (
    "platform",
    "video_id",
    "category",
    "title",
    "author",
    "author_avatar",
    "verified",
    "duration_ms",
    "likes",
    "comments",
    "shares",
    "collects",
    "published_at",
    "created_at",
    "published_display",
    "like_display",
    "tags_json",
    "homepage_featured",
    "collection_published",
    "homepage_rank",
    "cover_key",
    "statistics_checked_at",
    "statistics_retry_at",
    "cover_required",
    "media_status",
    "storage_uri",
    "archive_status",
    "archive_error",
    "availability",
    "usage_detail_count",
    "usage_copy_count",
    "usage_favorite_count",
)


# 客户使用计数（方案 P1 内容模块 C-1）：
# 详情/文案是按次扣费的台账行，source_id 形如
# `viral-detail:{付费账号}:{platform}:{video_id}`（见 viral_routes._detail_source_id），
# 用 `LIKE '%:' || platform || ':' || video_id` 命中该视频的全部付费账号；
# 收藏是独立表。三者都以「平台 + 视频」维度聚合成每条视频的客户使用量。
_USAGE_DETAIL_SQL = (
    "(SELECT count(*) FROM billing_operations o WHERE o.service='viral_detail' "
    "AND o.state='SUCCEEDED' AND o.user_id IS NOT NULL "
    "AND o.source_id LIKE '%%:' || v.platform || ':' || v.video_id)"
)
_USAGE_COPY_SQL = (
    "(SELECT count(*) FROM billing_operations o WHERE o.service='viral_copy' "
    "AND o.state='SUCCEEDED' AND o.user_id IS NOT NULL "
    "AND o.source_id LIKE '%%:' || v.platform || ':' || v.video_id)"
)
_USAGE_FAVORITE_SQL = (
    "(SELECT count(*) FROM viral_video_favorites f "
    "WHERE f.platform = v.platform AND f.video_id = v.video_id)"
)
_USAGE_TOTAL_SQL = f"{_USAGE_DETAIL_SQL} + {_USAGE_COPY_SQL} + {_USAGE_FAVORITE_SQL}"

_SORT_EXPRESSIONS = {
    # 默认采集时间倒序；likes/published 按内容表现；usage 按客户使用量。
    "created": "v.created_at DESC,v.platform,v.video_id",
    "likes": "v.likes DESC,v.created_at DESC,v.platform,v.video_id",
    "published": "v.published_at DESC NULLS LAST,v.created_at DESC,v.platform,v.video_id",
    "usage": (f"{_USAGE_TOTAL_SQL} DESC,v.created_at DESC,v.platform,v.video_id"),
}


@router.get("/viral/videos")
def read_collected_viral_videos(
    _actor: AdminReader,
    platform: Literal["douyin", "wechat_channels"] | None = None,
    status: Literal["ready", "pending", "failed", "featured"] | None = None,
    category: Annotated[str, Query(max_length=50)] = "",
    has_usage: bool | None = None,
    sort: Literal["created", "likes", "published", "usage"] = "created",
    query: Annotated[str, Query(max_length=100)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
) -> dict[str, object]:
    filters = "v.deleted_at IS NULL AND v.platform IN ('douyin','wechat_channels')"
    params: list[object] = []
    if platform:
        filters += " AND v.platform=%s"
        params.append(platform)
    if status:
        # 状态分段在服务端过滤：只筛当前页会给出「这页里没有」的错觉。
        filters += f" AND ({_COLLECTED_STATUS_FILTERS[status]})"
    if category.strip():
        filters += " AND v.category=%s"
        params.append(category.strip())
    if has_usage is not None:
        # 有无客户使用（方案 P1 C-1 的筛选维度）：决定「哪些内容值得上首页」。
        filters += f" AND {_USAGE_TOTAL_SQL} {'> 0' if has_usage else '= 0'}"
    if query.strip():
        filters += " AND (v.title ILIKE %s OR v.author ILIKE %s OR v.video_id ILIKE %s)"
        params.extend([f"%{query.strip()}%"] * 3)
    order_by = _SORT_EXPRESSIONS[sort]
    # 使用计数列必须注入 SELECT 列表（FROM 之前）而不是拼在行投影末尾：
    # 后者会把子查询落成 FROM 的逗号表项，其中引用 v 需要显式 LATERAL。
    usage_select = (
        f", {_USAGE_DETAIL_SQL} AS usage_detail_count"
        f", {_USAGE_COPY_SQL} AS usage_copy_count"
        f", {_USAGE_FAVORITE_SQL} AS usage_favorite_count"
    )
    row_select = _COLLECTED_VIRAL_ROW_SELECT.replace(
        "        COALESCE(vis.status,'AVAILABLE') AS availability",
        "        COALESCE(vis.status,'AVAILABLE') AS availability" + usage_select,
        1,
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        total = conn.execute(
            f"SELECT count(*) FROM viral_videos v WHERE {filters}", tuple(params)
        ).fetchone()[0]
        rows = conn.execute(
            f"{row_select} WHERE {filters} ORDER BY {order_by} {PAGE_CLAUSE}",
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
    date_from_raw = request.query_params.get("from", "")
    date_to_raw = request.query_params.get("to", "")
    # 方案 P1 客户需求洞察：单日看趋势不够——支持 from/to 区间（缺省回落
    # 到单日 date，兼容既有调用），上限 92 天防止全表聚合。
    if date_from_raw.strip() or date_to_raw.strip():
        date_from = date_from_raw.strip()
        date_to = date_to_raw.strip() or date_from
        for value, code in ((date_from, "from"), (date_to, "to")):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "VIRAL_SEARCH_DATE_INVALID",
                        "message": f"{code} 日期格式应为 YYYY-MM-DD。",
                    },
                )
        if date_to < date_from:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VIRAL_SEARCH_DATE_RANGE_INVALID",
                    "message": "结束日期不能早于开始日期。",
                },
            )
        span_days = (
            datetime.strptime(date_to, "%Y-%m-%d") - datetime.strptime(date_from, "%Y-%m-%d")
        ).days
        if span_days > 92:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VIRAL_SEARCH_DATE_RANGE_TOO_LONG",
                    "message": "区间最多 92 天，请缩小范围。",
                },
            )
        date_filter = "search_date >= %s AND search_date <= %s"
        date_params: tuple[str, ...] = (date_from, date_to)
        label = f"{date_from}~{date_to}"
    else:
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
        date_filter = "search_date=%s"
        date_params = (resolved_date,)
        label = resolved_date
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        totals = conn.execute(
            "SELECT count(*),count(DISTINCT user_id),"
            "count(DISTINCT platform || '/' || video_id) "
            f"FROM viral_search_discoveries WHERE {date_filter}",
            date_params,
        ).fetchone()
        rows = conn.execute(
            "SELECT keyword,platform,count(*) AS discoveries,"
            "count(DISTINCT user_id) AS users,count(DISTINCT video_id) AS videos "
            f"FROM viral_search_discoveries WHERE {date_filter} "
            "GROUP BY keyword,platform "
            "ORDER BY discoveries DESC,keyword,platform LIMIT 100",
            date_params,
        ).fetchall()
    return {
        "date": label,
        "from": date_params[0],
        "to": date_params[-1],
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
               v.title, v.author, v.author_avatar, v.verified, v.category,
               v.duration_ms, v.likes, v.comments,
               v.shares, v.collects, v.published_at, v.created_at,
               v.published_display, v.like_display, v.tags_json,
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


def _enqueue_archive_batch(
    conn: psycopg.Connection,
    platform: str,
    video_ids: list[str],
    *,
    feature_after: dict[str, str] | None,
    actor_user_id: str,
    reason: str,
    request_id: str,
    audit_action: str,
) -> str:
    """排队一批同平台视频的素材准备（方案 P1 内容模块 C-2）。

    ``feature_after`` 带上首页意图（操作人 / 原因 / 请求编号）：后台逐条准备
    成功后当场上首页并按真实操作人写审计。沿用既有「每平台一个活跃任务」
    的串行约束——同平台冲突时如实 409，而不是并发互相踩存储。
    """
    control = conn.execute(
        "SELECT collection_enabled FROM viral_runtime_controls WHERE id=1 FOR UPDATE"
    ).fetchone()
    if control is None or not control[0]:
        raise http_error(409, "VIRAL_COLLECTION_PAUSED", "后台采集已暂停，请先开启采集服务。")
    for video_id in video_ids:
        exists = conn.execute(
            "SELECT 1 FROM viral_videos WHERE platform=%s AND video_id=%s AND deleted_at IS NULL",
            (platform, video_id),
        ).fetchone()
        if exists is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频不存在或已删除。")
    active = conn.execute(
        "SELECT id,collection_config_json FROM viral_refresh_tasks "
        "WHERE platform=%s AND status IN ('PENDING','RUNNING') FOR UPDATE",
        (platform,),
    ).fetchall()
    config: dict[str, object] = {"kind": "archive_batch", "video_ids": video_ids}
    if feature_after:
        config["feature_after"] = feature_after
    config_json = json.dumps(config, ensure_ascii=False)
    for task in active:
        task_config = json.loads(task[1])
        if task_config.get("kind") == "archive_batch" and task_config.get("video_ids") == video_ids:
            return str(task[0])
    if active:
        raise http_error(409, "VIRAL_ARCHIVE_BUSY", "该平台已有后台任务，请完成后再准备素材。")
    queued = conn.execute(
        """INSERT INTO viral_refresh_tasks(id,platform,sort,collection_config_json)
        VALUES(%s,%s,'latest',%s) ON CONFLICT(platform,sort) DO UPDATE SET
            status='PENDING',collection_config_json=excluded.collection_config_json,
            checkpoint_json='{}',retry_count=0,locked_by=NULL,locked_until=NULL,
            error_code=NULL,error_message_redacted=NULL,retryable=0,
            started_at=NULL,completed_at=NULL,updated_at=CURRENT_TIMESTAMP RETURNING id""",
        (str(uuid.uuid4()), platform, config_json),
    ).fetchone()
    if queued is None:
        raise RuntimeError("viral archive batch enqueue did not persist")
    task_id = str(queued[0])
    conn.execute(
        """INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json)
        VALUES(%s,%s,%s,'viral_video',%s,%s)""",
        (
            str(uuid.uuid4()),
            actor_user_id,
            audit_action,
            f"{platform}:{','.join(video_ids)}",
            json.dumps(
                {
                    "request_id": request_id,
                    "reason": reason,
                    "task_id": task_id,
                    "video_ids": video_ids,
                    "feature_after": bool(feature_after),
                },
                ensure_ascii=False,
            ),
        ),
    )
    return task_id


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
    from app.viral_store import LINK_IMPORT_CATEGORY

    prepared_cover = (
        _prepare_feature_cover(platform, video_id) if payload.action == "feature" else None
    )

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT cover_url,cover_key,homepage_featured,category FROM viral_videos "
            "WHERE platform=%s AND video_id=%s AND deleted_at IS NULL FOR UPDATE",
            (platform, video_id),
        ).fetchone()
        if row is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频不存在或已删除。")
        cover_url, cover_key, homepage_featured, category = row
        if payload.action in ("pin", "unpin") and not homepage_featured:
            raise http_error(409, "VIRAL_VIDEO_NOT_FEATURED", "只有已展示到首页的视频才能置顶。")
        if payload.action == "prepare":
            # 只排队准备素材（方案 C-2）：不改首页状态，就绪后运营再决定。
            task_id = _enqueue_archive_batch(
                conn,
                platform,
                [video_id],
                feature_after=None,
                actor_user_id=actor.user_id,
                reason=payload.reason.strip(),
                request_id=request_id,
                audit_action="viral_video.prepare",
            )
            return {
                "platform": platform,
                "video_id": video_id,
                "queued_for_preparation": True,
                "task_id": task_id,
                "homepage_featured": bool(homepage_featured),
                "homepage_rank": None,
                "deleted": False,
            }
        if payload.action == "feature":
            if str(category or "") == LINK_IMPORT_CATEGORY:
                # 链接导入是用户自己的项目素材，不进首页内容池；否则它会绕过采集
                # 侧的质量门槛直接出现在所有客户的爆款网格里。
                raise http_error(
                    409,
                    "VIRAL_LINK_IMPORT_NOT_CURATABLE",
                    "链接导入的素材不进首页，请先由后台采集同主题内容。",
                )
            ready = conn.execute(
                "SELECT 1 FROM viral_media_preparations WHERE platform=%s AND video_id=%s "
                "AND media_kind='video' AND status='SUCCEEDED' AND storage_uri IS NOT NULL",
                (platform, video_id),
            ).fetchone()
            if ready is None:
                # 方案 C-2：未准备的视频不再整批拒绝——排队准备并在就绪后
                # 由后台自动上首页（意图与操作人随任务走，审计可追溯）。
                task_id = _enqueue_archive_batch(
                    conn,
                    platform,
                    [video_id],
                    feature_after={
                        "actor_user_id": actor.user_id,
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                    },
                    actor_user_id=actor.user_id,
                    reason=payload.reason.strip(),
                    request_id=request_id,
                    audit_action="viral_video.prepare",
                )
                return {
                    "platform": platform,
                    "video_id": video_id,
                    "queued_for_preparation": True,
                    "task_id": task_id,
                    "homepage_featured": False,
                    "homepage_rank": None,
                    "deleted": False,
                }
            if cover_url and not cover_key:
                if prepared_cover is None or prepared_cover[0] != cover_url:
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
        # 置顶序（T4）：只有 pin 写入（取当前最小序-1，可叠加置顶），
        # unpin/unfeature/delete 归还 NULL=回到默认顺序。展示到首页不写序，
        # 未置顶的展示视频按既有 hot/latest 默认序排在置顶视频之后。
        next_rank: int | None = None
        if payload.action == "pin":
            # 不同视频的行锁互不冲突；固定事务锁让并发置顶按提交顺序分配唯一序号。
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
            min_rank = conn.execute(
                "SELECT COALESCE(MIN(homepage_rank),1)-1 FROM viral_videos "
                "WHERE deleted_at IS NULL AND homepage_featured=1"
            ).fetchone()
            assert min_rank is not None  # 聚合查询恒有一行
            next_rank = int(min_rank[0])
        # pin/unpin 不改变展示状态（前置守卫已确保处于展示中）。
        featured_value = (
            1
            if payload.action == "feature"
            else int(homepage_featured)
            if payload.action in ("pin", "unpin")
            else 0
        )
        conn.execute(
            """UPDATE viral_videos SET homepage_featured=%s,
                collection_published=CASE WHEN %s THEN 1 ELSE collection_published END,
                deleted_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE deleted_at END,
                homepage_rank=%s
            WHERE platform=%s AND video_id=%s""",
            (
                featured_value,
                payload.action == "feature",
                payload.action == "delete",
                next_rank,
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
            "homepage_featured": bool(featured_value),
            "homepage_rank": next_rank,
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


class ViralBatchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"]
    video_id: str = Field(min_length=1, max_length=512)


class ViralBatchCurationRequest(AdminWriteContract):
    """批量策展：一次请求对多条视频做同一动作（单事务 + 一条幂等键）.

    语义与单条完全一致（同样的归档与封面门槛），只是把 N 次点击合成一次
    审计动作；批量里任何一条不满足条件就整批取消，避免「删了一半」这种
    说不清的状态。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["feature", "unfeature", "delete", "prepare"]
    items: list[ViralBatchTarget] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_items(self) -> ViralBatchCurationRequest:
        identities = [(item.platform, item.video_id) for item in self.items]
        if len(identities) != len(set(identities)):
            raise ValueError("批量条目不能重复")
        return self


@router.post("/viral/videos/curation:batch")
def curate_collected_viral_videos_batch(
    payload: ViralBatchCurationRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    require_write_contract(request, payload)
    from app.viral_store import LINK_IMPORT_CATEGORY

    prepared_covers: dict[tuple[str, str], tuple[str, str]] = {}
    if payload.action == "feature":
        # 封面补齐全走事务外（下载 + 独立连接），事务内只做校验与写库：
        # 与单条路径同一套 CoverEnricher，但不在长事务里做网络 IO。
        for target in payload.items:
            prepared = _prepare_feature_cover(target.platform, target.video_id)
            if prepared is not None:
                prepared_covers[(target.platform, target.video_id)] = prepared
    if payload.action == "prepare":
        # 批量准备素材：同平台的未就绪视频合并为一个后台任务逐条准备
        # （沿用每平台一个活跃任务的串行约束），跨平台各排一个。
        def business_prepare(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
            by_platform: dict[str, list[str]] = {}
            for target in payload.items:
                by_platform.setdefault(target.platform, []).append(target.video_id)
            task_ids: list[str] = []
            for platform, video_ids in by_platform.items():
                task_ids.append(
                    _enqueue_archive_batch(
                        conn,
                        platform,
                        video_ids,
                        feature_after=None,
                        actor_user_id=actor.user_id,
                        reason=payload.reason.strip(),
                        request_id=request_id,
                        audit_action="viral_video.prepare",
                    )
                )
            return {
                "action": "prepare",
                "count": len(payload.items),
                "items": [
                    {
                        "platform": target.platform,
                        "video_id": target.video_id,
                        "queued_for_preparation": True,
                    }
                    for target in payload.items
                ],
                "task_ids": task_ids,
            }

        return write_with_idempotency(
            request,
            response,
            actor,
            payload,
            business_prepare,
            success_status=200,
            unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
            unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
        )

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        updated: list[dict[str, object]] = []
        for target in payload.items:
            row = conn.execute(
                "SELECT title,cover_url,cover_key,category FROM viral_videos "
                "WHERE platform=%s AND video_id=%s AND deleted_at IS NULL FOR UPDATE",
                (target.platform, target.video_id),
            ).fetchone()
            if row is None:
                raise http_error(
                    404, "VIRAL_VIDEO_NOT_FOUND", "批量中有视频已被删除，请刷新后重试。"
                )
            title = str(row[0])
            if payload.action == "feature":
                if str(row[3] or "") == LINK_IMPORT_CATEGORY:
                    raise http_error(
                        409,
                        "VIRAL_LINK_IMPORT_NOT_CURATABLE",
                        f"「{title}」是链接导入的素材，不进首页，批量操作未执行。",
                    )
                ready = conn.execute(
                    "SELECT 1 FROM viral_media_preparations WHERE platform=%s AND video_id=%s "
                    "AND media_kind='video' AND status='SUCCEEDED' AND storage_uri IS NOT NULL",
                    (target.platform, target.video_id),
                ).fetchone()
                if ready is None:
                    # 方案 C-2：未就绪的条目转入「准备后就绪自动上首页」队列；
                    # 就绪条目照常当场展示，批量不再整批取消。
                    queued_task = _enqueue_archive_batch(
                        conn,
                        target.platform,
                        [target.video_id],
                        feature_after={
                            "actor_user_id": actor.user_id,
                            "reason": payload.reason.strip(),
                            "request_id": request_id,
                        },
                        actor_user_id=actor.user_id,
                        reason=payload.reason.strip(),
                        request_id=request_id,
                        audit_action="viral_video.prepare",
                    )
                    updated.append(
                        {
                            "platform": target.platform,
                            "video_id": target.video_id,
                            "homepage_featured": False,
                            "deleted": False,
                            "queued_for_preparation": True,
                            "task_id": queued_task,
                        }
                    )
                    continue
                if row[1] and not row[2]:
                    prepared = prepared_covers.get((target.platform, target.video_id))
                    if prepared is None or prepared[0] != row[1]:
                        raise http_error(
                            409,
                            "VIRAL_COVER_NOT_READY",
                            f"「{title}」的封面暂时无法获取，批量操作未执行。",
                        )
                    conn.execute(
                        "UPDATE viral_videos SET cover_key=%s WHERE platform=%s AND video_id=%s",
                        (prepared[1], target.platform, target.video_id),
                    )
                hidden = conn.execute(
                    "SELECT 1 FROM viral_video_visibility WHERE platform=%s AND video_id=%s "
                    "AND status!='AVAILABLE'",
                    (target.platform, target.video_id),
                ).fetchone()
                if hidden:
                    raise http_error(
                        409,
                        "VIRAL_VIDEO_UNAVAILABLE",
                        f"「{title}」已下架或隐藏，批量操作未执行。",
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
                    target.platform,
                    target.video_id,
                ),
            )
            entity_id = f"{target.platform}:{target.video_id}"
            conn.execute(
                """INSERT INTO audit_logs(
                    id,actor_user_id,action,entity_type,entity_id,metadata_json
                ) VALUES(%s,%s,'viral_video.curation','viral_video',%s,%s)""",
                (
                    str(uuid.uuid4()),
                    actor.user_id,
                    entity_id,
                    json.dumps(
                        {
                            "action": payload.action,
                            "reason": payload.reason.strip(),
                            "request_id": request_id,
                            "batch": True,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            updated.append(
                {
                    "platform": target.platform,
                    "video_id": target.video_id,
                    "homepage_featured": payload.action == "feature",
                    "deleted": payload.action == "delete",
                    "queued_for_preparation": False,
                }
            )
        return {
            "action": payload.action,
            "count": sum(1 for item in updated if not item.get("queued_for_preparation")),
            "queued_count": sum(1 for item in updated if item.get("queued_for_preparation")),
            "items": updated,
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


@router.get("/viral/overview")
def read_viral_library_overview(_actor: AdminReader) -> dict[str, object]:
    """爆款视频库概览（只读）：数字卡与采集计划所需的计数与时间.

    与列表端同口径（``deleted_at IS NULL`` + 平台白名单），避免「概览说
    有 1200 条、列表翻不到」这类对不上的数。今日新增按上海日历归属。
    """
    # created_at 是文本列（双方言惯例），按上海零点换算成 UTC 下界比较。
    day_start = (
        datetime.combine(datetime.now(SHANGHAI).date(), time.min, SHANGHAI)
        .astimezone(UTC)
        .isoformat()
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        controls = conn.execute(
            "SELECT collection_enabled, keywords_json, next_collection_at, "
            "collection_interval_days FROM viral_runtime_controls WHERE id = 1"
        ).fetchone()
        base = "v.deleted_at IS NULL AND v.platform IN ('douyin','wechat_channels')"
        ready = (
            "EXISTS (SELECT 1 FROM viral_media_preparations m2"
            " WHERE m2.platform=v.platform AND m2.video_id=v.video_id"
            " AND m2.media_kind='video' AND m2.status='SUCCEEDED'"
            " AND m2.storage_uri IS NOT NULL)"
        )
        counts = conn.execute(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE {ready}) AS archive_ready,
                   count(*) FILTER (WHERE v.homepage_featured=1) AS featured,
                   count(*) FILTER (WHERE ({_COLLECTED_STATUS_FILTERS["failed"]})) AS failed,
                   count(*) FILTER (WHERE ({_COLLECTED_STATUS_FILTERS["pending"]})) AS pending,
                   count(*) FILTER (
                       WHERE {utc_timestamp_sql("v.created_at")} >= %s::timestamptz
                   ) AS added_today,
                   max(v.created_at) AS last_created_at
            FROM viral_videos v WHERE {base}
            """,
            (day_start,),
        ).fetchone()
        last_fetched = conn.execute(
            "SELECT max(fetched_at) FROM viral_fetch_state"
            " WHERE platform IN ('douyin','wechat_channels')"
        ).fetchone()
    keywords = json.loads(str(controls[1])) if controls is not None else []
    by_platform: dict[str, int] = {"douyin": 0, "wechat_channels": 0}
    for item in keywords:
        platform = str(item.get("platform", ""))
        if platform in by_platform:
            by_platform[platform] += 1
    mapping = dict(counts) if counts is not None else {}
    last_created = mapping.get("last_created_at")
    return {
        "content_total": int(mapping.get("total") or 0),
        "archive_ready": int(mapping.get("archive_ready") or 0),
        "homepage_featured": int(mapping.get("featured") or 0),
        "pending_archive": int(mapping.get("pending") or 0),
        "archive_failed": int(mapping.get("failed") or 0),
        "added_today": int(mapping.get("added_today") or 0),
        "last_created_at": str(last_created) if last_created is not None else None,
        "collection_enabled": bool(controls[0]) if controls is not None else False,
        "keyword_count": {
            "douyin": by_platform["douyin"],
            "wechat_channels": by_platform["wechat_channels"],
        },
        "next_collection_at": (
            str(controls[2]) if controls is not None and controls[2] is not None else None
        ),
        "collection_interval_days": int(controls[3]) if controls is not None else 7,
        "last_fetched_at": (
            str(last_fetched[0])
            if last_fetched is not None and last_fetched[0] is not None
            else None
        ),
    }


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
