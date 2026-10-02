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
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.admin_auth_routes import AdminReader, AdminWriter, SuperAdminReader, SuperAdminWriter
from app.admin_dates import SHANGHAI, utc_timestamp_sql
from app.admin_viral_collection_records import (
    fail_admin_search_record,
    start_admin_search_record,
)
from app.admin_viral_collection_records import (
    router as collection_records_router,
)
from app.admin_viral_costs import require_cost_snapshot
from app.admin_viral_costs import router as operation_cost_router
from app.admin_viral_demand import enrich_demand
from app.admin_viral_keywords import router as keyword_router
from app.admin_viral_metrics import usage_count_sql, video_business_metrics
from app.admin_viral_platform_probe import router as platform_probe_router
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
from app.viral_collection_billing import freeze_collection_billing
from app.viral_collection_budget import collection_budget
from app.viral_collection_schedule import collection_estimate, next_collection_time
from app.viral_content_observations import record_content_source, record_content_stage
from app.viral_content_state import (
    archive_task_match_sql,
    archive_task_status_sql,
    content_state_sql,
    customer_visible_sql,
)
from app.viral_homepage import live_homepage_sql, pending_homepage_sql
from app.viral_keywords import ViralKeywordConfig

router = APIRouter(prefix="/api/control", tags=["admin-runtime"])

router.include_router(keyword_router)
router.include_router(operation_cost_router)
router.include_router(collection_records_router)
router.include_router(platform_probe_router)

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
    month_spend_fen: float = 0
    month_unknown_cost_count: int = 0
    month_pending_cost_count: int = 0
    month_total_cost_fen: float | None = None
    budget_status: Literal["unlimited", "normal", "warning", "exhausted"] = "unlimited"
    budget_usage_percent: float | None = None
    budget_period_start: str | None = None
    budget_period_end: str | None = None
    budget_scope: str = ""
    next_collection_at: str | None = None
    collection_interval_days: int = 7
    collection_time: str | None = None
    last_collection_at: str | None = None


class ViralRuntimeUpdateRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    collection_enabled: bool
    import_enabled: bool
    keywords: list[ViralKeywordConfig] | None = Field(default=None, max_length=50)
    expected_keywords: list[ViralKeywordConfig] | None = Field(default=None, max_length=50)
    per_keyword_limit: int | None = Field(default=None, ge=1, le=50)
    collection_interval_days: Literal[1, 7] | None = None
    collection_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
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
        "monthly_budget_fen,collection_time FROM viral_runtime_controls WHERE id = 1"
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
        collection_time=controls[11].strftime("%H:%M") if controls and controls[11] else None,
        last_collection_at=(lambda row: row[0].isoformat() if row and row[0] else None)(
            conn.execute("SELECT max(started_at) FROM viral_keyword_runs").fetchone()
        ),
        quality_min_likes=int(controls[6]) if controls and controls[6] is not None else None,
        quality_duration_min_ms=int(controls[7]) if controls and controls[7] is not None else None,
        quality_duration_max_ms=int(controls[8]) if controls and controls[8] is not None else None,
        quality_exclude_words=[str(word) for word in json.loads(controls[9] or "[]")]
        if controls
        else [],
        monthly_budget_fen=int(controls[10]) if controls and controls[10] is not None else None,
        **collection_budget(BusinessConnection.postgres(conn)),
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
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

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


def _viral_settings_audit_snapshot(conn: psycopg.Connection) -> dict[str, object]:
    row = (
        BusinessConnection.postgres(conn)
        .execute(
            "SELECT collection_enabled,import_enabled,keywords_json,per_keyword_limit,"
            "collection_interval_days,collection_time,quality_min_likes,quality_duration_min_ms,"
            "quality_duration_max_ms,quality_exclude_words_json,monthly_budget_fen "
            "FROM viral_runtime_controls WHERE id=1 FOR UPDATE"
        )
        .fetchone()
    )
    if row is None:
        return {}
    snapshot = dict(row)
    snapshot["keywords"] = json.loads(str(snapshot.pop("keywords_json")))
    snapshot["quality_exclude_words"] = json.loads(str(snapshot.pop("quality_exclude_words_json")))
    clock = snapshot["collection_time"]
    snapshot["collection_time"] = str(clock) if clock is not None else None
    return snapshot


@router.patch("/settings/viral", response_model=ViralRuntimeResponse)
def update_viral_runtime(
    payload: ViralRuntimeUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        before = _viral_settings_audit_snapshot(conn)
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
            current_keywords = _read_keyword_rows(conn)
            if payload.expected_keywords is None:
                raise HTTPException(
                    409,
                    detail={
                        "code": "VIRAL_KEYWORDS_VERSION_REQUIRED",
                        "message": "请刷新采集设置后重新编辑关键词。",
                    },
                )
            if [
                ViralKeywordConfig.model_validate(item).model_dump() for item in current_keywords
            ] != [item.model_dump() for item in payload.expected_keywords]:
                raise HTTPException(
                    409,
                    detail={
                        "code": "VIRAL_KEYWORDS_CONFLICT",
                        "message": "其他管理员已更新关键词。当前草稿未保存，请刷新后合并修改。",
                    },
                )
            existing = {(item["platform"], item["keyword"]): item for item in current_keywords}
            replacements = []
            for item in payload.keywords:
                merged = item.model_dump()
                previous = existing.get((item.platform, item.keyword), {})
                # Legacy editors omit these fields; retain independently saved settings.
                for field in ("enabled", "limit"):
                    if field not in item.model_fields_set and field in previous:
                        merged[field] = previous[field]
                replacements.append(merged)
            conn.execute(
                "UPDATE viral_runtime_controls SET keywords_json=%s WHERE id=1",
                (json.dumps(replacements, ensure_ascii=False),),
            )
        if payload.per_keyword_limit is not None:
            conn.execute(
                "UPDATE viral_runtime_controls SET per_keyword_limit=%s WHERE id=1",
                (payload.per_keyword_limit,),
            )
        quality_sets: list[str] = []
        quality_params: list[object] = []
        current = conn.execute(
            "SELECT quality_duration_min_ms, quality_duration_max_ms "
            "FROM viral_runtime_controls WHERE id=1 FOR UPDATE"
        ).fetchone()
        assert current is not None
        duration_min = (
            payload.quality_duration_min_ms
            if "quality_duration_min_ms" in payload.model_fields_set
            else current[0]
        )
        duration_max = (
            payload.quality_duration_max_ms
            if "quality_duration_max_ms" in payload.model_fields_set
            else current[1]
        )
        if duration_min is not None and duration_max is not None and duration_min > duration_max:
            raise HTTPException(422, detail="最短时长不能大于最长时长。")
        # PATCH 必须区分省略和显式 null，否则已配置的限制无法恢复为不限。
        if "quality_min_likes" in payload.model_fields_set:
            quality_sets.append("quality_min_likes=%s")
            quality_params.append(payload.quality_min_likes)
        if "quality_duration_min_ms" in payload.model_fields_set:
            quality_sets.append("quality_duration_min_ms=%s")
            quality_params.append(payload.quality_duration_min_ms)
        if "quality_duration_max_ms" in payload.model_fields_set:
            quality_sets.append("quality_duration_max_ms=%s")
            quality_params.append(payload.quality_duration_max_ms)
        if "quality_exclude_words" in payload.model_fields_set:
            quality_sets.append("quality_exclude_words_json=%s")
            quality_params.append(
                json.dumps(
                    [
                        word.strip()
                        for word in (payload.quality_exclude_words or [])
                        if word.strip()
                    ],
                    ensure_ascii=False,
                )
            )
        if "monthly_budget_fen" in payload.model_fields_set:
            quality_sets.append("monthly_budget_fen=%s")
            quality_params.append(payload.monthly_budget_fen)
        if quality_sets:
            quality_params.append(1)
            conn.execute(
                "UPDATE viral_runtime_controls SET " + ", ".join(quality_sets) + " WHERE id=%s",
                tuple(quality_params),
            )
        if (
            payload.collection_interval_days is not None
            or "collection_time" in payload.model_fields_set
        ):
            schedule = conn.execute(
                "SELECT collection_interval_days,collection_time FROM viral_runtime_controls "
                "WHERE id=1 FOR UPDATE"
            ).fetchone()
            assert schedule is not None
            interval = payload.collection_interval_days or int(schedule[0])
            clock = (
                (time.fromisoformat(payload.collection_time) if payload.collection_time else None)
                if "collection_time" in payload.model_fields_set
                else schedule[1]
            )
            if clock is not None:
                conn.execute(
                    "UPDATE viral_runtime_controls SET "
                    "collection_interval_days=%s,collection_time=%s,"
                    "next_collection_at=CASE WHEN collection_interval_days!=%s "
                    "OR collection_time IS DISTINCT FROM %s THEN %s ELSE next_collection_at END "
                    "WHERE id=1",
                    (
                        interval,
                        clock,
                        interval,
                        clock,
                        next_collection_time(datetime.now(UTC), interval, clock),
                    ),
                )
            else:
                conn.execute("UPDATE viral_runtime_controls SET collection_time=NULL WHERE id=1")
        if payload.collection_interval_days is not None and clock is None:
            conn.execute(
                """UPDATE viral_runtime_controls SET
                    next_collection_at=CASE WHEN collection_interval_days!=%s
                        AND next_collection_at IS NOT NULL
                        THEN CURRENT_TIMESTAMP + (%s * interval '1 day')
                        ELSE next_collection_at END,
                    collection_interval_days=%s WHERE id=1""",
                (payload.collection_interval_days,) * 3,
            )
        after = _viral_settings_audit_snapshot(conn)
        changes = {
            field: {"before": before.get(field), "after": value}
            for field, value in after.items()
            if before.get(field) != value
        }
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
                        "collection_time": payload.collection_time,
                        "before": before,
                        "after": after,
                        "changes": changes,
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
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
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
        if payload.status != "AVAILABLE":
            conn.execute(
                "UPDATE viral_videos SET homepage_featured=0,homepage_rank=NULL,"
                "homepage_intent_version=homepage_intent_version+1 "
                "WHERE platform=%s AND video_id=%s",
                (platform, video_id),
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


@router.get("/viral/collection/estimate")
def estimate_viral_collection(_actor: AdminReader) -> dict[str, object]:
    with pg_transaction() as raw:
        return collection_estimate(BusinessConnection.postgres(raw))


class ViralCollectRequest(AdminWriteContract):
    expected_estimate_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


@router.post("/viral/collect", status_code=202)
def collect_viral_now(
    payload: ViralCollectRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """立即入队一次关键词采集，保留下一次定时执行计划.

    与定时采集共用 ``enqueue_due_viral_collections``：关键词、平台、每词上限与
    共享账单批次都在同一事务内建立。手动触发不受月度预算门槛拦截（运营知情下的
    动作）；若已有在队/在制的采集任务则如实回 409，而不是回「已入队」却什么都没排
    ——事务随之回滚，不改下一次定时执行计划。
    """
    from app.viral_collection import enqueue_due_viral_collections

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        control = conn.execute(
            "SELECT collection_enabled, keywords_json FROM viral_runtime_controls "
            "WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if control is None or not bool(control[0]):
            raise http_error(409, "VIRAL_COLLECTION_PAUSED", "后台采集已暂停，请先开启采集服务。")
        if not any(
            ViralKeywordConfig.model_validate(item).enabled
            for item in json.loads(str(control[1] or "[]"))
        ):
            raise http_error(
                409, "VIRAL_COLLECTION_KEYWORDS_REQUIRED", "请先启用至少一个采集关键词。"
            )
        frozen = freeze_collection_billing(BusinessConnection.postgres(conn))
        estimate = collection_estimate(BusinessConnection.postgres(conn), frozen_billing=frozen)
        if (
            payload.expected_estimate_snapshot is not None
            and payload.expected_estimate_snapshot != estimate["snapshot"]
        ):
            raise http_error(
                409,
                "VIRAL_ESTIMATE_CHANGED",
                "关键词、规则、预算、单价或收费客户已改变，请重新预估后确认。",
            )
        if not enqueue_due_viral_collections(
            BusinessConnection.postgres(conn), manual=True, frozen_billing=frozen
        ):
            raise http_error(
                409,
                "VIRAL_COLLECTION_BUSY",
                "已有采集任务在队列或运行中，请等它完成后再立即采集。",
            )
        conn.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_collection.collect_now','viral_runtime_controls','1',%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "request_id": request_id,
                        "reason": payload.reason.strip(),
                        "estimate": estimate,
                    }
                ),
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
    expected_cost_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def routine_reason(self) -> ViralCurationRequest:
        if not self.reason.strip() and self.action in {"feature", "prepare", "pin", "unpin"}:
            self.reason = {
                "feature": "上首页",
                "prepare": "准备素材",
                "pin": "调整顺序",
                "unpin": "调整顺序",
            }[self.action]
        return self


# 视频库与管理端搜索共用的行投影：viral_videos 左联云端媒体准备与单条转存
# 任务，给出前端「转存 / 上首页」按钮需要的全部状态，以及列表要直接上屏的
# 头像 / 认证 / 发布时间 / 点赞显示值 / 话题标签（运营不必逐行展开才看到）。
_COLLECTED_VIRAL_ROW_SELECT = f"""
    SELECT v.platform,v.video_id,v.category,v.title,v.author,v.author_avatar,
        v.verified,v.duration_ms,
        v.likes,v.comments,v.shares,v.collects,v.published_at,v.created_at,
        v.published_display,v.like_display,v.tags_json,
        v.homepage_featured,v.collection_published,v.homepage_rank,v.cover_key,
        v.homepage_starts_at,v.homepage_ends_at,v.homepage_featured_at,
        {live_homepage_sql("v")} AS homepage_live,
        {pending_homepage_sql()} AS homepage_pending,
        {content_state_sql()} AS content_state,
        {customer_visible_sql()} AS customer_visible,
        v.native_json::jsonb->>'_statistics_checked_at' AS statistics_checked_at,
        v.native_json::jsonb->>'_statistics_retry_at' AS statistics_retry_at,
        (COALESCE(v.cover_url,'') != '') AS cover_required,
        COALESCE(m.status,'NOT_STARTED') AS media_status,m.storage_uri,
        r.status AS archive_status,r.error_message_redacted AS archive_error,
        COALESCE(vis.status,'AVAILABLE') AS availability
    FROM viral_videos v LEFT JOIN viral_media_preparations m
        ON m.platform=v.platform AND m.video_id=v.video_id AND m.media_kind='video'
    LEFT JOIN LATERAL (
        SELECT {archive_task_status_sql("rt", "v.video_id")} AS status,
            CASE WHEN {archive_task_status_sql("rt", "v.video_id")}='FAILED'
                THEN error_message_redacted END AS error_message_redacted
        FROM viral_refresh_tasks rt WHERE rt.platform=v.platform AND rt.sort='latest'
          AND {archive_task_match_sql("rt", "v")}
        ORDER BY rt.created_at DESC,rt.id DESC LIMIT 1
    ) r ON TRUE
    LEFT JOIN viral_video_visibility vis
        ON vis.platform=v.platform AND vis.video_id=v.video_id
"""

# 列表状态分段的服务端口径：与前端 archiveLabel 同一套判定，避免两处漂移。
_COLLECTED_STATUS_FILTERS = {
    "ready": f"({content_state_sql()}) IN ('ready','featured')",
    "pending": f"({content_state_sql()})='pending_prepare'",
    "failed": f"({content_state_sql()})='prepare_failed'",
    "featured": f"({content_state_sql()})='featured'",
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
    "homepage_pending",
    "content_state",
    "customer_visible",
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
    "homepage_starts_at",
    "homepage_ends_at",
    "homepage_featured_at",
    "homepage_live",
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


# 列表、详情和排序使用同一钱包主体口径，子账号授权不会被漏掉。
_USAGE_DETAIL_SQL = usage_count_sql("viral_detail")
_USAGE_COPY_SQL = usage_count_sql("viral_copy")
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
    content_state: Literal[
        "pending_prepare", "prepare_failed", "ready", "featured", "removed", "blocked"
    ]
    | None = None,
    category: Annotated[str, Query(max_length=50)] = "",
    source_keyword: Annotated[str, Query(max_length=100)] = "",
    published_from: date | None = None,
    published_to: date | None = None,
    customer_visible: bool | None = None,
    has_usage: bool | None = None,
    sort: Literal["created", "likes", "published", "usage"] = "created",
    query: Annotated[str, Query(max_length=100)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
) -> dict[str, object]:
    filters = "v.deleted_at IS NULL AND v.platform IN ('douyin','wechat_channels')"
    params: list[object] = []
    if published_from and published_to and published_to < published_from:
        raise http_error(422, "VIRAL_PUBLISH_RANGE_INVALID", "结束日期不能早于开始日期。")
    if content_state:
        filters += f" AND ({content_state_sql()})=%s"
        params.append(content_state)
    if source_keyword.strip():
        filters += " AND (EXISTS (SELECT 1 FROM viral_search_discoveries cs_source WHERE "
        filters += "cs_source.platform=v.platform AND cs_source.video_id=v.video_id "
        filters += (
            "AND cs_source.keyword=%s) OR EXISTS (SELECT 1 FROM viral_content_sources cs_origin "
        )
        filters += "WHERE cs_origin.platform=v.platform AND cs_origin.video_id=v.video_id "
        filters += "AND cs_origin.keyword=%s))"
        params.extend([source_keyword.strip()] * 2)
    if published_from:
        filters += " AND v.published_at >= %s"
        params.append(int(datetime.combine(published_from, time.min, SHANGHAI).timestamp()))
    if published_to:
        filters += " AND v.published_at < %s"
        params.append(
            int(datetime.combine(published_to + timedelta(days=1), time.min, SHANGHAI).timestamp())
        )
    if customer_visible is not None:
        filters += f" AND ({customer_visible_sql()})=%s"
        params.append(customer_visible)
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


def _discovery_window(request: Request) -> tuple[str, str]:
    first = (request.query_params.get("from") or request.query_params.get("date") or "").strip()
    last = (request.query_params.get("to") or first).strip()
    if not first:
        raise HTTPException(
            422, detail={"code": "VIRAL_SEARCH_DATE_REQUIRED", "message": "日期参数必填。"}
        )
    try:
        if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (first, last)):
            raise ValueError
        span = (datetime.strptime(last, "%Y-%m-%d") - datetime.strptime(first, "%Y-%m-%d")).days
    except ValueError:
        raise HTTPException(
            422,
            detail={"code": "VIRAL_SEARCH_DATE_INVALID", "message": "请填写有效日期 YYYY-MM-DD。"},
        ) from None
    if span < 0:
        raise HTTPException(
            422,
            detail={
                "code": "VIRAL_SEARCH_DATE_RANGE_INVALID",
                "message": "结束日期不能早于开始日期。",
            },
        )
    if span > 92:
        raise HTTPException(
            422, detail={"code": "VIRAL_SEARCH_DATE_RANGE_TOO_LONG", "message": "区间最多 92 天。"}
        )
    return first, last


@router.get("/viral/discoveries")
def read_viral_search_discoveries(request: Request, _actor: AdminReader) -> dict[str, object]:
    first, last = _discovery_window(request)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            """
            WITH events AS (
                SELECT keyword,platform,count(*) AS searches,count(DISTINCT user_id) AS users,
                       count(*) FILTER(WHERE video_ids_json::jsonb='[]'::jsonb) AS zero_results
                FROM viral_search_events WHERE search_date BETWEEN %s AND %s
                GROUP BY keyword,platform
            ), hits AS (
                SELECT keyword,platform,count(*) AS discoveries,count(DISTINCT user_id) AS users,
                       count(DISTINCT video_id) AS videos FROM viral_search_discoveries
                WHERE search_date BETWEEN %s AND %s GROUP BY keyword,platform
            )
            SELECT COALESCE(e.keyword,h.keyword) AS keyword,
                   COALESCE(e.platform,h.platform) AS platform,
                   COALESCE(h.discoveries,0) AS discoveries,
                   COALESCE(e.searches,0) AS recorded_searches,
                   CASE WHEN e.searches IS NULL THEN NULL ELSE e.searches END AS searches,
                   COALESCE(e.zero_results,0) AS zero_results,
                   COALESCE(e.users,h.users,0) AS users,COALESCE(h.videos,0) AS videos
            FROM events e FULL JOIN hits h ON e.keyword=h.keyword AND e.platform=h.platform
            ORDER BY COALESCE(e.searches,0) DESC,COALESCE(h.discoveries,0) DESC,keyword,platform
            """,
            (first, last, first, last),
        ).fetchall()
        totals = conn.execute(
            "SELECT count(*),count(DISTINCT user_id) FROM viral_search_events "
            "WHERE search_date BETWEEN %s AND %s",
            (first, last),
        ).fetchone()
        coverage = conn.execute(
            "SELECT count(*),count(DISTINCT user_id),count(DISTINCT platform || '/' || video_id) "
            "FROM viral_search_discoveries WHERE search_date BETWEEN %s AND %s",
            (first, last),
        ).fetchone()
        started = conn.execute(
            "SELECT started_at FROM viral_search_metrics_state WHERE id=1"
        ).fetchone()
        demand, categories = enrich_demand(conn, rows, first, last, started[0] if started else None)
    return {
        "date": first if first == last else f"{first}~{last}",
        "from": first,
        "to": last,
        "total": int(totals[0]),
        "users": int(totals[1]),
        "videos": int(coverage[2]),
        "hitRecords": int(coverage[0]),
        "coveredUsers": int(coverage[1]),
        "measurementStartedAt": str(started[0]) if started else None,
        "countingRule": (
            "成功交付的真实搜索页次；包含零结果；同一计费轮次重放不重复计数。"
            "历史命中记录不能还原搜索次数。"
        ),
        "keywords": demand,
        "categories": categories,
        "inventoryRule": (
            "当前客户端可用视频去重，关联来自实际采集或搜索命中；"
            "排除未就绪、隐藏、屏蔽、删除及已过展示窗口的视频。"
        ),
        "priorityRule": (
            "按已记录搜索次数÷（可用库存+1）降序，再按搜索次数排序；"
            "用于备货优先级，不代表缺货条数。"
        ),
    }


@router.get("/viral/discoveries/detail")
def read_viral_discovery_details(
    request: Request,
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
    first, last = _discovery_window(request)
    resolved_date = first if first == last else f"{first}~{last}"
    filters = "d.search_date BETWEEN %s AND %s"
    params: list[object] = [first, last]
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
        SELECT g.keyword,g.platform AS discovery_platform,g.video_id AS discovery_video_id,
               g.discoveries,g.users,g.last_searched_at,g.total,video.*
        FROM grouped g
        LEFT JOIN LATERAL (
            {_COLLECTED_VIRAL_ROW_SELECT}
            WHERE v.platform=g.platform AND v.video_id=g.video_id AND v.deleted_at IS NULL
        ) video ON TRUE
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
                "platform": str(mapping["discovery_platform"]),
                "videoId": str(mapping["discovery_video_id"]),
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
        "from": first,
        "to": last,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
    }


@router.get("/viral/discoveries/customers")
def read_viral_discovery_customers(
    request: Request,
    _actor: AdminReader,
    keyword: Annotated[str, Query(min_length=1, max_length=100)],
    platform: Literal["douyin", "wechat_channels"],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, object]:
    first, last = _discovery_window(request)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            f"""WITH events AS (
                SELECT user_id,count(*) AS searches,
                  count(*) FILTER(WHERE video_ids_json::jsonb='[]'::jsonb) AS zero_results,
                  max(searched_at) AS last_at FROM viral_search_events
                WHERE search_date BETWEEN %s AND %s AND keyword=%s AND platform=%s
                GROUP BY user_id
            ), hits AS (
                SELECT user_id,count(DISTINCT video_id) AS videos,max(searched_at) AS last_at
                FROM viral_search_discoveries WHERE search_date BETWEEN %s AND %s
                  AND keyword=%s AND platform=%s GROUP BY user_id
            ) SELECT COALESCE(e.user_id,h.user_id) AS user_id,u.username,u.display_name,
                COALESCE(u.parent_user_id,u.id) AS customer_user_id,
                CASE WHEN e.user_id IS NULL THEN NULL ELSE e.searches END AS searches,
                CASE WHEN e.user_id IS NULL THEN NULL ELSE e.zero_results END AS zero_results,
                COALESCE(h.videos,0) AS videos,GREATEST(e.last_at,h.last_at) AS last_searched_at,
                count(*) OVER() AS total
            FROM events e FULL JOIN hits h ON h.user_id=e.user_id
            LEFT JOIN users u ON u.id=COALESCE(e.user_id,h.user_id)
            WHERE e.user_id IS NOT NULL OR NOT EXISTS(SELECT 1 FROM events)
            ORDER BY COALESCE(e.searches,0) DESC,last_searched_at DESC,COALESCE(e.user_id,h.user_id)
            {PAGE_CLAUSE}""",
            (
                first,
                last,
                keyword.strip(),
                platform,
                first,
                last,
                keyword.strip(),
                platform,
                limit,
                offset,
            ),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item.pop("total")
        item["last_searched_at"] = str(item["last_searched_at"])
        items.append(item)
    return {
        "from": first,
        "to": last,
        "items": items,
        "total": int(rows[0]["total"]) if rows else 0,
        "offset": offset,
        "limit": limit,
        "countingRule": (
            "有搜索次数时只列已记录真实搜索账号，包含零结果；仅历史命中时列历史覆盖账号。"
            "子账号显示所属主客户，历史命中不能还原搜索次数。"
        ),
    }


class ViralAdminSearchRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    expected_cost_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

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

    batch_id: str | None = None

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        require_cost_snapshot(conn, payload.expected_cost_snapshot)
        nonlocal batch_id
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
        batch_id = start_admin_search_record(payload.platform, keyword)
        try:
            client = viral_source_client_from_settings(BusinessConnection.postgres(conn))
        except ViralSourceUnavailable as exc:
            fail_admin_search_record(
                batch_id, "数据服务尚未配置或配置无效，请核对服务配置。", "CONFIGURATION"
            )
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
                billing_batch_id=batch_id,
            )
        except ViralSourceError as exc:
            from app.viral_collection_failures import FAILURES, classify_collection_failure

            code = classify_collection_failure(exc)
            fail_admin_search_record(batch_id, FAILURES[code][0], code)
            raise http_error(503, "VIRAL_ADMIN_SEARCH_UPSTREAM_FAILED", str(exc)) from exc
        except TimeoutError as exc:
            fail_admin_search_record(
                batch_id, "数据服务响应超时，请稍后重试并核对供应商费用。", "TIMEOUT"
            )
            raise http_error(
                503, "VIRAL_ADMIN_SEARCH_UPSTREAM_TIMEOUT", "搜索服务响应超时，请稍后重试。"
            ) from exc
        storage = get_media_storage(BusinessConnection.postgres(conn))
        try:
            enriched = archive_search_covers_bounded(storage, page.items)
        except TimeoutError:
            # 封面归档超时不放弃整页结果：未归档条目保留源站链接兜底。
            enriched = page.items
        inserted = upsert_viral_videos(BusinessConnection.postgres(conn), enriched, commit=False)
        for video in enriched:
            record_content_source(
                BusinessConnection.postgres(conn),
                batch_id=batch_id,
                source_kind="admin_search",
                platform=video.platform,
                video_id=video.video_id,
                keyword=keyword,
                is_new=(video.platform, video.video_id) in inserted,
            )
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
        completed = conn.execute(
            "UPDATE viral_collection_batches SET run_status='SUCCEEDED',"
            "completed_at=clock_timestamp(),"
            "failed_video_count=0 WHERE id=%s AND run_status='RUNNING' "
            "AND lease_expires_at>clock_timestamp()",
            (batch_id,),
        )
        if completed.rowcount != 1:
            from app.viral_collection_failures import FAILURES

            fail_admin_search_record(batch_id, FAILURES["INTERRUPTED"][0], "INTERRUPTED")
            raise http_error(
                409, "VIRAL_SEARCH_RECORD_EXPIRED", "搜索记录已中断，请核对费用后重新搜索。"
            )
        return {
            "items": items,
            "cursor": page.cursor,
            "hasMore": page.has_more,
            "keyword": keyword,
            "platform": payload.platform,
            "timeRange": payload.time_range,
        }

    try:
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
    except Exception:
        if batch_id is not None:
            fail_admin_search_record(
                batch_id,
                "搜索结果未完成入库，请核对采集记录、存储配置与费用后重试。",
                "RESULT_WRITE",
            )
        raise


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
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
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
    for task in active:
        task_config = json.loads(task[1])
        if (
            task_config.get("kind") == "archive_batch"
            and task_config.get("video_ids") == video_ids
            and bool(task_config.get("feature_after")) == bool(feature_after)
        ):
            if feature_after:
                expected = task_config.get("feature_after", {}).get("versions", {})
                actual = conn.execute(
                    "SELECT video_id,homepage_intent_version FROM viral_videos "
                    "WHERE platform=%s AND video_id=ANY(%s)",
                    (platform, video_ids),
                ).fetchall()
                if any(
                    int(version) != int(expected.get(str(ident), 0)) for ident, version in actual
                ):
                    raise http_error(
                        409, "CONFLICT", "已有发布任务被后续决定取消，请等待结束后重试。"
                    )
            return str(task[0])
    if active:
        raise http_error(409, "VIRAL_ARCHIVE_BUSY", "该平台已有后台任务，请完成后再准备素材。")
    if feature_after:
        versions = {}
        for video_id in video_ids:
            version = conn.execute(
                "UPDATE viral_videos SET homepage_intent_version=homepage_intent_version+1 "
                "WHERE platform=%s AND video_id=%s RETURNING homepage_intent_version",
                (platform, video_id),
            ).fetchone()
            if version is None:
                raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频已删除。")
            versions[video_id] = int(version[0])
        config["feature_after"] = {**feature_after, "versions": versions}
    config_json = json.dumps(config, ensure_ascii=False)
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


class ViralOperationWriteRequest(AdminWriteContract):
    expected_cost_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


@router.post("/viral/videos/{platform}/{video_id:path}/archive", status_code=202)
def archive_collected_viral_video(
    platform: Literal["douyin", "wechat_channels"],
    video_id: str,
    payload: ViralOperationWriteRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        require_cost_snapshot(conn, payload.expected_cost_snapshot)
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
    payload: ViralOperationWriteRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    from app.viral_statistics import refresh_viral_statistics
    from app.viral_store import STATISTICS_CHECKED_AT_KEY, STATISTICS_RETRY_AT_KEY
    from app.viral_tikhub import viral_source_client_from_settings

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        require_cost_snapshot(raw, payload.expected_cost_snapshot)
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


@router.get("/viral/videos/{platform}/{video_id:path}/details")
def read_viral_video_business_details(
    platform: Literal["douyin", "wechat_channels"],
    video_id: str,
    _actor: AdminReader,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, object]:
    from urllib.parse import urlsplit

    from app.script_from_audio import cached_transcript

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute(
            _COLLECTED_VIRAL_ROW_SELECT
            + " WHERE v.platform=%s AND v.video_id=%s AND v.deleted_at IS NULL",
            (platform, video_id),
        ).fetchone()
        if row is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "视频不存在或已被屏蔽。")
        item = _collected_video_payload(dict(row))
        business = video_business_metrics(
            conn, platform=platform, video_id=video_id, offset=offset, limit=limit
        )
        item.update(
            usage_detail_count=business["detailAccounts"],
            usage_copy_count=business["copyAccounts"],
            usage_favorite_count=business["favoriteAccounts"],
        )
        media_rows = conn.execute(
            "SELECT media_kind,status,storage_uri,error_code,updated_at "
            "FROM viral_media_preparations "
            "WHERE platform=%s AND video_id=%s",
            (platform, video_id),
        ).fetchall()
        media = {
            str(r["media_kind"]): {
                "status": str(r["status"]),
                "ready": r["status"] == "SUCCEEDED" and bool(r["storage_uri"]),
                "errorCode": r["error_code"],
                "updatedAt": str(r["updated_at"]),
            }
            for r in media_rows
        }
        for kind in ("audio", "video"):
            media.setdefault(
                kind,
                {"status": "NOT_STARTED", "ready": False, "errorCode": None, "updatedAt": None},
            )
        copy = cached_transcript(conn, platform=platform, video_id=video_id)
        native_row = conn.execute(
            "SELECT native_json FROM viral_videos WHERE platform=%s AND video_id=%s",
            (platform, video_id),
        ).fetchone()
        native = json.loads(native_row[0] or "{}")
        if not isinstance(native, dict):
            native = {}
        original_url = None
        # 原始分享页与签名媒体直链分开；只展示已有的官方分享页，不拼造链接。
        candidates = [native.get(key) for key in ("share_url", "original_url", "source_url")]
        if isinstance(native.get("share_info"), dict):
            candidates.append(native["share_info"].get("share_url"))
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            try:
                parsed = urlsplit(candidate)
            except ValueError:
                continue
            if (
                parsed.scheme == "https"
                and parsed.hostname in {"www.douyin.com", "v.douyin.com", "channels.weixin.qq.com"}
                and not parsed.username
                and not parsed.password
                and not parsed.query
            ):
                original_url = candidate
                break
        words = conn.execute(
            """SELECT DISTINCT keyword FROM (
                SELECT keyword FROM viral_search_discoveries WHERE platform=%s AND video_id=%s
                UNION ALL
                SELECT t.collection_config_json::jsonb->'keywords'->
                    (CASE WHEN kv.key ~ '^[0-9]{1,9}$' THEN kv.key::int ELSE NULL END)->>'keyword'
                FROM viral_refresh_tasks t CROSS JOIN LATERAL
                    jsonb_each(COALESCE(t.checkpoint_json::jsonb->'keywords','{}'::jsonb)) kv
                WHERE t.platform=%s AND kv.key ~ '^[0-9]+$'
                  AND kv.value @> jsonb_build_array(%s::text)
            ) known WHERE keyword IS NOT NULL AND keyword<>'' ORDER BY keyword""",
            (platform, video_id, platform, video_id),
        ).fetchall()
        related = (
            conn.execute(
                _COLLECTED_VIRAL_ROW_SELECT + " WHERE v.platform=%s AND v.author=%s "
                "AND v.video_id<>%s AND v.deleted_at IS NULL ORDER BY v.created_at DESC LIMIT 8",
                (platform, item["author"], video_id),
            ).fetchall()
            if item["author"]
            else []
        )
    return {
        "video": item,
        "business": business,
        "media": media,
        "copy": {"text": copy.result.text, "updatedAt": copy.updated_at} if copy else None,
        "sourceDescription": (native.get("source_description") or None)
        if isinstance(native.get("source_description"), str)
        else None,
        "originalUrl": original_url,
        "sourceKeywords": [str(r[0]) for r in words],
        "relatedVideos": [_collected_video_payload(dict(r)) for r in related],
    }


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
        require_cost_snapshot(conn, payload.expected_cost_snapshot)
        # 所有首页写入先取同一事务锁，避免与重排的行锁顺序相反。
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
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
            hidden = conn.execute(
                "SELECT 1 FROM viral_video_visibility WHERE platform=%s AND video_id=%s "
                "AND status!='AVAILABLE'",
                (platform, video_id),
            ).fetchone()
            if hidden:
                raise http_error(409, "VIRAL_VIDEO_UNAVAILABLE", "视频已下架，请先恢复。")
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
                homepage_rank=%s,
                homepage_intent_version=homepage_intent_version+CASE WHEN %s THEN 0 ELSE 1 END,
                homepage_starts_at=CASE WHEN %s THEN homepage_starts_at ELSE NULL END,
                homepage_ends_at=CASE WHEN %s THEN homepage_ends_at ELSE NULL END,
                homepage_featured_at=CASE WHEN %s THEN homepage_featured_at
                    WHEN %s THEN now() ELSE NULL END
            WHERE platform=%s AND video_id=%s""",
            (
                featured_value,
                payload.action == "feature",
                payload.action == "delete",
                next_rank,
                payload.action in ("pin", "unpin"),
                payload.action in ("pin", "unpin"),
                payload.action in ("pin", "unpin"),
                payload.action in ("pin", "unpin"),
                payload.action == "feature",
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
        if payload.action == "feature":
            record_content_stage(
                BusinessConnection.postgres(conn),
                platform=platform,
                video_id=video_id,
                stage="prepared",
            )
            record_content_stage(
                BusinessConnection.postgres(conn),
                platform=platform,
                video_id=video_id,
                stage="homepage",
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

    action: Literal["feature", "unfeature", "delete", "prepare", "hide", "block"]
    expected_cost_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    items: list[ViralBatchTarget] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def routine_reason(self) -> ViralBatchCurationRequest:
        if not self.reason.strip() and self.action in {"feature", "prepare"}:
            self.reason = "上首页" if self.action == "feature" else "准备素材"
        return self

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
            require_cost_snapshot(conn, payload.expected_cost_snapshot)
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
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
                "count": 0,
                "queued_count": len(payload.items),
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
        require_cost_snapshot(conn, payload.expected_cost_snapshot)
        updated: list[dict[str, object]] = []
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
        # 未就绪的条目先记下，循环结束后每个平台只排一个任务：每平台同一时刻只允许
        # 一个后台任务，在循环里逐条排队，第二条就会撞上第一条刚排的任务而 409，
        # 连同已排的第一条一起回滚——同平台两条未就绪视频的批量根本做不成。
        unready_by_platform: dict[str, list[str]] = {}
        unready_entries: dict[str, list[dict[str, object]]] = {}
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
                hidden = conn.execute(
                    "SELECT 1 FROM viral_video_visibility WHERE platform=%s AND video_id=%s "
                    "AND status!='AVAILABLE'",
                    (target.platform, target.video_id),
                ).fetchone()
                if hidden:
                    raise http_error(409, "VIRAL_VIDEO_UNAVAILABLE", "视频已下架，请先恢复。")
                if ready is None:
                    # 方案 C-2：未就绪的条目转入「准备后就绪自动上首页」队列；
                    # 就绪条目照常当场展示，批量不再整批取消。
                    entry: dict[str, object] = {
                        "platform": target.platform,
                        "video_id": target.video_id,
                        "homepage_featured": False,
                        "deleted": False,
                        "queued_for_preparation": True,
                        "task_id": None,
                    }
                    updated.append(entry)
                    unready_by_platform.setdefault(target.platform, []).append(target.video_id)
                    unready_entries.setdefault(target.platform, []).append(entry)
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
                    deleted_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE deleted_at END,
                    homepage_intent_version=homepage_intent_version+1,
                    homepage_starts_at=NULL,homepage_ends_at=NULL,
                    homepage_featured_at=CASE WHEN %s THEN now() ELSE NULL END
                WHERE platform=%s AND video_id=%s""",
                (
                    int(payload.action == "feature"),
                    payload.action == "feature",
                    payload.action == "delete",
                    payload.action == "feature",
                    target.platform,
                    target.video_id,
                ),
            )
            if payload.action in ("hide", "block"):
                conn.execute(
                    "INSERT INTO "
                    "viral_video_visibility(platform,video_id,status,reason,updated_by_user"
                    "_id) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(platform,video_id) DO UPDATE SET "
                    "status=excluded.status,reason=excluded.reason,updated_by_user_id=exclu"
                    "ded.updated_by_user_id,updated_at=now()",
                    (
                        target.platform,
                        target.video_id,
                        "HIDDEN" if payload.action == "hide" else "UNAVAILABLE",
                        payload.reason,
                        actor.user_id,
                    ),
                )
            if payload.action == "feature":
                record_content_stage(
                    BusinessConnection.postgres(conn),
                    platform=target.platform,
                    video_id=target.video_id,
                    stage="prepared",
                )
                record_content_stage(
                    BusinessConnection.postgres(conn),
                    platform=target.platform,
                    video_id=target.video_id,
                    stage="homepage",
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
        for platform, video_ids in unready_by_platform.items():
            queued_task = _enqueue_archive_batch(
                conn,
                platform,
                video_ids,
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
            for entry in unready_entries[platform]:
                entry["task_id"] = queued_task
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


class ViralHomepageOrderRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["douyin", "wechat_channels"]
    video_ids: list[str]
    expected_video_ids: list[str]
    reason: str = "调整首页顺序"


class ViralHomepageScheduleRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    expected_starts_at: datetime | None
    expected_ends_at: datetime | None
    reason: str = "设置首页排期"

    @model_validator(mode="after")
    def valid_window(self) -> ViralHomepageScheduleRequest:
        for value in (self.starts_at, self.ends_at, self.expected_starts_at, self.expected_ends_at):
            if value and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("排期必须包含时区")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("下线时间必须晚于上线时间")
        return self


@router.get("/viral/homepage")
def read_viral_homepage(
    _actor: AdminReader, platform: Literal["douyin", "wechat_channels"] = "douyin"
) -> dict[str, object]:
    # 共用业务连接的列映射，避免 psycopg 默认 tuple 行与页面字段发生偏移。
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mapped = conn.execute(
            _COLLECTED_VIRAL_ROW_SELECT
            + " WHERE v.platform=%s AND v.homepage_featured=1 AND v.deleted_at IS NULL "
            "ORDER BY v.homepage_rank ASC NULLS LAST,v.likes DESC,v.published_at DESC NULLS LAST,"
            "v.video_id ASC",
            (platform,),
        ).fetchall()
        items = [_collected_video_payload(dict(r)) for r in mapped]
        preparing_rows = conn.execute(
            _COLLECTED_VIRAL_ROW_SELECT + f" WHERE v.platform=%s AND v.homepage_featured=0 "
            f"AND v.deleted_at IS NULL AND {pending_homepage_sql()} "
            "ORDER BY v.created_at,v.video_id",
            (platform,),
        ).fetchall()
        preparing = [_collected_video_payload(dict(r)) for r in preparing_rows]
        started = conn.execute(
            "SELECT started_at FROM viral_content_measurement_state WHERE id=1"
        ).fetchone()[0]
        opened = conn.execute(
            "SELECT v.video_id,count(e.id) AS requests FROM viral_videos v "
            "LEFT JOIN viral_content_usage_events e ON e.platform=v.platform AND "
            "e.video_id=v.video_id "
            "AND e.kind='detail' AND "
            "e.created_at>=GREATEST(v.homepage_featured_at,v.homepage_starts_at) "
            "AND (v.homepage_ends_at IS NULL OR e.created_at<v.homepage_ends_at) "
            "WHERE v.platform=%s AND v.homepage_featured_at IS NOT NULL "
            "AND v.video_id=ANY(%s) GROUP BY v.video_id",
            (platform, [str(item["video_id"]) for item in items]),
        ).fetchall()
        by_video = {str(row[0]): int(row[1]) for row in opened}
        for item in items:
            item["opens_after_homepage"] = (
                None
                if item.get("homepage_featured_at") is None
                else by_video.get(str(item["video_id"]), 0)
            )
        from app.viral_tikhub import is_irrelevant_viral_video

        preview = [
            item
            for item in items
            if not is_irrelevant_viral_video(str(item["title"]))
            and (not item.get("cover_required") or item.get("cover_key"))
            and item.get("homepage_live")
            and item.get("availability") == "AVAILABLE"
            and item.get("media_status") == "SUCCEEDED"
            and item.get("storage_uri")
        ]
    return {
        "platform": platform,
        "revision": _homepage_order_revision([dict(row) for row in mapped]),
        "items": items,
        "preparing": preparing,
        "measurement_started_at": str(started),
        "preview": preview[:12],
        "preview_limit": 12,
        "capacity": None,
        "counting_rule": "预览前12条；12不是首页容量。排期使用数据库时间。",
    }


@router.put("/viral/homepage/order")
def reorder_viral_homepage(
    payload: ViralHomepageOrderRequest, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    require_write_contract(request, payload)

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
        rows = conn.execute(
            "SELECT video_id FROM viral_videos WHERE platform=%s AND homepage_featured=1 "
            "AND deleted_at IS NULL ORDER BY homepage_rank ASC NULLS LAST,likes DESC,"
            "published_at DESC NULLS LAST,video_id ASC FOR UPDATE",
            (payload.platform,),
        ).fetchall()
        current = [str(row[0]) for row in rows]
        if current != payload.expected_video_ids:
            raise http_error(409, "CONFLICT", "首页内容或顺序已变化，请刷新后再调整。")
        if len(payload.video_ids) != len(set(payload.video_ids)) or set(current) != set(
            payload.video_ids
        ):
            raise http_error(422, "VIRAL_ORDER_INCOMPLETE", "请提交该平台完整且不重复的首页顺序。")
        for rank, ident in enumerate(payload.video_ids, 1):
            conn.execute(
                "UPDATE viral_videos SET homepage_rank=%s WHERE platform=%s AND video_id=%s",
                (rank, payload.platform, ident),
            )
        conn.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_homepage.reorder','viral_homepage',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                payload.platform,
                json.dumps(
                    {
                        "before": current,
                        "after": payload.video_ids,
                        "reason": payload.reason,
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return {"platform": payload.platform, "video_ids": payload.video_ids}

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


@router.patch("/viral/homepage/{platform}/{video_id:path}/schedule")
def schedule_viral_homepage(
    platform: Literal["douyin", "wechat_channels"],
    video_id: str,
    payload: ViralHomepageScheduleRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    require_write_contract(request, payload)

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
        row = conn.execute(
            "SELECT homepage_starts_at,homepage_ends_at FROM viral_videos WHERE platform=%s "
            f"AND video_id=%s AND (homepage_featured=1 OR {pending_homepage_sql('viral_videos')}) "
            "AND deleted_at IS NULL FOR UPDATE",
            (platform, video_id),
        ).fetchone()
        if row is None:
            raise http_error(409, "VIRAL_VIDEO_NOT_FEATURED", "请先将视频加入首页，再设置排期。")
        if tuple(row) != (payload.expected_starts_at, payload.expected_ends_at):
            raise http_error(409, "CONFLICT", "排期已被其他操作更新，请刷新后重新设置。")
        conn.execute(
            "UPDATE viral_videos SET homepage_starts_at=%s,homepage_ends_at=%s "
            "WHERE platform=%s AND video_id=%s",
            (payload.starts_at, payload.ends_at, platform, video_id),
        )
        conn.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_homepage.schedule','viral_video',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{platform}:{video_id}",
                json.dumps(
                    {
                        "before": [str(v) if v else None for v in row],
                        "after": [
                            payload.starts_at.isoformat() if payload.starts_at else None,
                            payload.ends_at.isoformat() if payload.ends_at else None,
                        ],
                        "reason": payload.reason,
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return {
            "platform": platform,
            "video_id": video_id,
            "starts_at": payload.starts_at.isoformat() if payload.starts_at else None,
            "ends_at": payload.ends_at.isoformat() if payload.ends_at else None,
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
                   count(*) FILTER (WHERE {live_homepage_sql("v")}) AS featured,
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


@router.get("/viral/content-overview")
def read_viral_content_overview(request: Request, _actor: AdminReader) -> dict[str, object]:
    from app.viral_content_overview import content_overview

    if request.query_params.get("from") or request.query_params.get("date"):
        first, last = _discovery_window(request)
        start, end = date.fromisoformat(first), date.fromisoformat(last)
    else:
        end = datetime.now(SHANGHAI).date()
        start = end - timedelta(days=end.weekday())
    with pg_transaction() as raw:
        return content_overview(BusinessConnection.postgres(raw), start=start, end=end)


@router.get("/settings/queue-mode", response_model=QueueModeResponse)
def read_queue_mode(_actor: SuperAdminReader) -> QueueModeResponse:
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
    actor: SuperAdminWriter,
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


@router.get("/viral/recycle")
def read_viral_recycle_bin(
    _actor: AdminReader,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
) -> dict[str, object]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            _COLLECTED_VIRAL_ROW_SELECT.replace(
                "SELECT v.platform,",
                "SELECT v.deleted_at,"
                "v.deleted_at+interval '30 days' AS restore_before,"
                "v.deleted_at>=now()-interval '30 days' AS restorable,v.platform,",
            )
            + " WHERE v.deleted_at IS NOT NULL AND v.platform IN ('douyin','wechat_channels') "
            "ORDER BY v.deleted_at DESC,v.platform,v.video_id LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
        count = conn.execute(
            "SELECT count(*) FROM viral_videos WHERE deleted_at IS NOT NULL "
            "AND platform IN ('douyin','wechat_channels')"
        ).fetchone()[0]
        items = []
        for row in rows:
            item = _collected_video_payload(dict(row))
            item.update(
                deleted_at=row["deleted_at"].isoformat(),
                restore_before=row["restore_before"].isoformat(),
                restorable=bool(row["restorable"]),
            )
            items.append(item)
    return {
        "items": items,
        "total": int(count),
        "offset": offset,
        "limit": limit,
        "rule": (
            "删除后30天内可恢复；恢复不会自动上首页。"
            "到期保留删除标记防止重新采集，永久清理需单独批准。"
        ),
    }


@router.post("/viral/videos/{platform}/{video_id:path}/restore")
def restore_viral_video(
    platform: Literal["douyin", "wechat_channels"],
    video_id: str,
    payload: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    require_write_contract(request, payload)

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
        row = conn.execute(
            "SELECT deleted_at,deleted_at>=now()-interval '30 days' FROM "
            "viral_videos WHERE platform=%s AND video_id=%s FOR UPDATE",
            (platform, video_id),
        ).fetchone()
        if row is None or row[0] is None:
            raise http_error(409, "VIRAL_VIDEO_NOT_DELETED", "视频不在回收站，请刷新后重试。")
        if not row[1]:
            raise http_error(409, "VIRAL_RESTORE_EXPIRED", "已超过30天恢复期限，不能恢复。")
        conn.execute(
            "UPDATE viral_videos SET deleted_at=NULL,homepage_featured=0,homepage_rank=NULL,"
            "homepage_starts_at=NULL,homepage_ends_at=NULL,homepage_featured_at=NULL,"
            "homepage_intent_version=homepage_intent_version+1 "
            "WHERE platform=%s AND video_id=%s",
            (platform, video_id),
        )
        conn.execute(
            "INSERT INTO viral_video_visibility"
            "(platform,video_id,status,reason,updated_by_user_id) "
            "VALUES(%s,%s,'AVAILABLE',%s,%s) ON CONFLICT(platform,video_id) "
            "DO UPDATE SET status='AVAILABLE',reason=excluded.reason,"
            "updated_by_user_id=excluded.updated_by_user_id,updated_at=now()",
            (platform, video_id, payload.reason.strip(), actor.user_id),
        )
        conn.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_video.restore','viral_video',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{platform}:{video_id}",
                json.dumps(
                    {
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                        "deleted_at": row[0].isoformat(),
                        "homepage_featured": False,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return {
            "platform": platform,
            "video_id": video_id,
            "restored": True,
            "homepage_featured": False,
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


def _homepage_order_revision(rows: Sequence[Mapping[str, object]]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(
            [[row["video_id"], row["homepage_rank"]] for row in rows],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class ViralHomepageMoveRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["douyin", "wechat_channels"]
    video_id: str = Field(min_length=1, max_length=512)
    position: int = Field(ge=1)
    expected_revision: str = Field(min_length=64, max_length=64)
    reason: str = "调整首页顺序"


@router.post("/viral/homepage/move")
def move_viral_homepage(
    payload: ViralHomepageMoveRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    require_write_contract(request, payload)

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
        rows = conn.execute(
            "SELECT video_id,homepage_rank FROM viral_videos WHERE platform=%s "
            "AND homepage_featured=1 AND deleted_at IS NULL ORDER BY homepage_rank ASC NULLS LAST,"
            "likes DESC,published_at DESC NULLS LAST,video_id ASC FOR UPDATE",
            (payload.platform,),
        ).fetchall()
        mapped = [{"video_id": r[0], "homepage_rank": r[1]} for r in rows]
        if _homepage_order_revision(mapped) != payload.expected_revision:
            raise http_error(409, "CONFLICT", "首页内容或顺序已变化，请刷新后再调整。")
        ids = [str(r[0]) for r in rows]
        if payload.video_id not in ids or payload.position > len(ids):
            raise http_error(422, "VIRAL_POSITION_INVALID", "目标视频或位置无效，请刷新后重试。")
        previous_position = ids.index(payload.video_id) + 1
        ids.insert(payload.position - 1, ids.pop(previous_position - 1))
        conn.execute(
            "UPDATE viral_videos v SET homepage_rank=desired.position "
            "FROM unnest(%s::text[]) WITH ORDINALITY AS desired(video_id,position) "
            "WHERE v.platform=%s AND v.video_id=desired.video_id",
            (ids, payload.platform),
        )
        revision = _homepage_order_revision(
            [{"video_id": ident, "homepage_rank": rank} for rank, ident in enumerate(ids, 1)]
        )
        conn.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_homepage.reorder','viral_homepage',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                payload.platform,
                json.dumps(
                    {
                        "video_id": payload.video_id,
                        "before_position": previous_position,
                        "after_position": payload.position,
                        "total": len(ids),
                        "reason": payload.reason,
                        "previous_revision": payload.expected_revision,
                        "revision": revision,
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return {
            "platform": payload.platform,
            "video_id": payload.video_id,
            "position": payload.position,
            "revision": revision,
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
