"""Studio workspace real counters (C6/C10a: 平台侧真实统计).

The V1.4 studio shell shows metric cards (今日成片 / 队列 / 待处理) that had
no data source — they rendered fixtures in review mode and "—" in production.
This module exposes one aggregate over the caller's visible generation and oral
tasks. Employees/customers see only their own records, hidden generation batches
stay hidden, and superseded generation outputs never count. Nothing here invents numbers —
published/external platform metrics (播放/互动) remain absent until their
capability lands (C5 发布 / C6 外部数据源).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser, CurrentUser, Database
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection
from app.permissions import require_not_auditor
from app.studio_search import SearchKind, StudioSearchResponse, search_studio

router = APIRouter(prefix="/api")


@router.get("/studio/search", response_model=StudioSearchResponse)
def read_studio_search(
    conn: Database,
    actor: AuthenticatedUser,
    q: Annotated[str, Query(min_length=1, max_length=100)],
    kind: SearchKind = "video",
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 12,
) -> StudioSearchResponse:
    return search_studio(conn, actor=actor, query=q, kind=kind, page=page, page_size=page_size)


_BEIJING_TZ = timezone(timedelta(hours=8))

# CURRENT_TIMESTAMP writes UTC on both dialects; timestamp casts below also
# normalize ISO-8601 values written with ``T`` or an explicit offset.
_CUTOFF_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_cutoff_for_beijing_day(now: datetime | None = None) -> str:
    """UTC text cutoff for the start of the current Beijing calendar day."""
    moment = now or datetime.now(tz=UTC)
    beijing_midnight = moment.astimezone(_BEIJING_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return beijing_midnight.astimezone(UTC).strftime(_CUTOFF_FORMAT)


class StudioStatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    today_completed: int
    running: int
    queued: int
    needs_attention: int
    total_completed: int


def studio_task_stats(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    now: datetime | None = None,
) -> StudioStatsResponse:
    cutoff = utc_cutoff_for_beijing_day(now)
    clauses = [
        "task.superseded_by_task_id IS NULL",
        "NOT EXISTS ("
        "SELECT 1 FROM customer_batch_visibility AS visibility "
        "WHERE visibility.user_id = %s AND visibility.batch_id = task.batch_id)",
    ]
    # Placeholder order mirrors the SQL text: the SELECT's cutoff comes first,
    # then the visibility clause, then the optional owner clause.
    parameters: list[object] = [cutoff, actor.id]
    if actor.role in {"employee", "customer"}:
        clauses.append(
            "(project.owner_user_id = %s "
            "OR (batch.project_id IS NULL AND batch.created_by_user_id = %s))"
        )
        parameters.extend([actor.id, actor.id])

    generation_row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN task.status = 'SUCCEEDED'
                AND task.updated_at::timestamptz >= %s::timestamptz
                THEN 1 ELSE 0 END), 0) AS today_completed,
            COALESCE(SUM(CASE WHEN task.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
                THEN 1 ELSE 0 END), 0) AS running,
            COALESCE(SUM(CASE WHEN task.status IN ('PENDING', 'QUEUED')
                THEN 1 ELSE 0 END), 0) AS queued,
            COALESCE(SUM(CASE WHEN task.status = 'FAILED'
                OR task.status = 'SUBMISSION_UNCERTAIN'
                OR task.archive_status = 'ARCHIVE_FAILED'
                THEN 1 ELSE 0 END), 0) AS needs_attention,
            COALESCE(SUM(CASE WHEN task.status = 'SUCCEEDED'
                THEN 1 ELSE 0 END), 0) AS total_completed
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        LEFT JOIN projects AS project ON project.id = batch.project_id
        WHERE {" AND ".join(clauses)}
        """,
        tuple(parameters),
    ).fetchone()
    oral_clauses: list[str] = []
    oral_parameters: list[object] = [cutoff]
    if actor.role in {"employee", "customer"}:
        oral_clauses.append("owner_user_id = %s")
        oral_parameters.append(actor.id)
    oral_where = f"WHERE {' AND '.join(oral_clauses)}" if oral_clauses else ""
    oral_row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN status = 'SUCCEEDED'
                AND updated_at::timestamptz >= %s::timestamptz
                THEN 1 ELSE 0 END), 0) AS today_completed,
            COALESCE(SUM(CASE WHEN status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
                THEN 1 ELSE 0 END), 0) AS running,
            COALESCE(SUM(CASE WHEN status = 'QUEUED'
                THEN 1 ELSE 0 END), 0) AS queued,
            COALESCE(SUM(CASE WHEN status IN (
                'FAILED', 'SUBMISSION_UNCERTAIN', 'ARCHIVE_FAILED'
            ) THEN 1 ELSE 0 END), 0) AS needs_attention,
            COALESCE(SUM(CASE WHEN status = 'SUCCEEDED'
                THEN 1 ELSE 0 END), 0) AS total_completed
        FROM oral_tasks
        {oral_where}
        """,
        tuple(oral_parameters),
    ).fetchone()

    def combined(field: str) -> int:
        return int(generation_row[field]) + int(oral_row[field])

    return StudioStatsResponse(
        today_completed=combined("today_completed"),
        running=combined("running"),
        queued=combined("queued"),
        needs_attention=combined("needs_attention"),
        total_completed=combined("total_completed"),
    )


@router.get("/studio/stats", response_model=StudioStatsResponse)
def read_studio_stats(conn: Database, actor: AuthenticatedUser) -> StudioStatsResponse:
    return studio_task_stats(conn, actor=actor)


# ---------------------------------------------------------------------------
# C6 数据看板：窗口内成片聚合（每日桶 / 任务类型分布 / 最近成片）
# ---------------------------------------------------------------------------


class StudioAnalyticsDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str
    completed: int
    failed: int


class StudioAnalyticsKindCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    completed: int


class StudioAnalyticsWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_kind: str
    batch_id: str | None
    project_id: str | None
    title: str
    creation_kind: str
    completed_at: str
    # 按秒计费（057+）实际消耗：最新一轮 RESERVE 扣减的秒数；无计费记录为 null。
    cost_credits: int | None


class StudioAnalyticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    range_days: int
    generated_at: str
    today_completed: int
    range_completed: int
    total_completed: int
    today_generation_batches: int
    range_generation_batches: int
    total_generation_batches: int
    range_generation_outputs: int
    range_oral_outputs: int
    daily: list[StudioAnalyticsDay]
    kind_breakdown: list[StudioAnalyticsKindCount]
    recent_works: list[StudioAnalyticsWorkItem]


_RECENT_WORKS_CAP = 20


def _utc_moment(value: str) -> datetime:
    """UTC 文本时间戳（SQLite 文本 / PG 文本或 datetime 序列化）→ UTC 时刻。"""
    moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def studio_analytics(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    days: int = 7,
    now: datetime | None = None,
) -> StudioAnalyticsResponse:
    """窗口内成片产出项聚合，并单列普通生成批次去重计数。

    普通生成和口播都以任务 ``updated_at`` 作为终态时刻，按北京日界分桶；
    普通生成的 ``batch_id`` 另行去重，避免把一个批次的多个成片混成批次数。
    播放/互动等外部平台数据不在其中——不伪造。
    """
    days = max(1, min(int(days), 90))
    moment = now or datetime.now(tz=UTC)
    stats = studio_task_stats(conn, actor=actor, now=moment)
    start_day = moment.astimezone(_BEIJING_TZ).date() - timedelta(days=days - 1)
    range_start = (
        datetime(start_day.year, start_day.month, start_day.day, tzinfo=_BEIJING_TZ)
        .astimezone(UTC)
        .strftime(_CUTOFF_FORMAT)
    )

    clauses = [
        "task.superseded_by_task_id IS NULL",
        "NOT EXISTS ("
        "SELECT 1 FROM customer_batch_visibility AS visibility "
        "WHERE visibility.user_id = %s AND visibility.batch_id = task.batch_id)",
    ]
    # 占位符顺序随 SQL 文本：可见性 → 归属（可选）→ 窗口起点。
    parameters: list[object] = [actor.id]
    if actor.role in {"employee", "customer"}:
        clauses.append(
            "(project.owner_user_id = %s "
            "OR (batch.project_id IS NULL AND batch.created_by_user_id = %s))"
        )
        parameters.extend([actor.id, actor.id])
    clauses.append("task.updated_at::timestamptz >= %s::timestamptz")
    parameters.append(range_start)

    generation_rows = conn.execute(
        f"""
        SELECT task.id, task.batch_id, task.status, task.updated_at,
               task.archive_status, batch.creation_kind, batch.project_id,
               COALESCE(project.name, batch.display_name, '') AS project_name,
               (
                   SELECT -wt.available_delta
                   FROM wallet_transactions AS wt
                   WHERE wt.task_id = task.id AND wt.type = 'RESERVE'
                   ORDER BY wt.billing_round DESC
                   LIMIT 1
               ) AS cost_credits
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        LEFT JOIN projects AS project ON project.id = batch.project_id
        WHERE {" AND ".join(clauses)}
        """,
        tuple(parameters),
    ).fetchall()

    oral_clauses = ["task.updated_at::timestamptz >= %s::timestamptz"]
    oral_parameters: list[object] = [range_start]
    if actor.role in {"employee", "customer"}:
        oral_clauses.append("task.owner_user_id = %s")
        oral_parameters.append(actor.id)
    oral_rows = conn.execute(
        f"""
        SELECT task.id, task.status, task.updated_at, task.title,
               (
                   SELECT -wt.available_delta
                   FROM wallet_transactions AS wt
                   WHERE wt.oral_task_id = task.id AND wt.type = 'RESERVE'
                   ORDER BY wt.billing_round DESC
                   LIMIT 1
               ) AS cost_credits
        FROM oral_tasks AS task
        WHERE {" AND ".join(oral_clauses)}
        """,
        tuple(oral_parameters),
    ).fetchall()

    total_batch_clauses = [
        "task.status = 'SUCCEEDED'",
        "task.superseded_by_task_id IS NULL",
        "NOT EXISTS ("
        "SELECT 1 FROM customer_batch_visibility AS visibility "
        "WHERE visibility.user_id = %s AND visibility.batch_id = task.batch_id)",
    ]
    total_batch_parameters: list[object] = [actor.id]
    if actor.role in {"employee", "customer"}:
        total_batch_clauses.append(
            "(project.owner_user_id = %s "
            "OR (batch.project_id IS NULL AND batch.created_by_user_id = %s))"
        )
        total_batch_parameters.extend([actor.id, actor.id])
    total_batch_row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT task.batch_id) AS completed_batches
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        LEFT JOIN projects AS project ON project.id = batch.project_id
        WHERE {" AND ".join(total_batch_clauses)}
        """,
        tuple(total_batch_parameters),
    ).fetchone()

    range_generation_outputs = 0
    range_oral_outputs = 0
    today_generation_batches: set[str] = set()
    range_generation_batches: set[str] = set()
    completed_by_day: dict[str, int] = {}
    failed_by_day: dict[str, int] = {}
    completed_by_kind: dict[str, int] = {}
    works: list[tuple[datetime, str, StudioAnalyticsWorkItem]] = []
    for row in generation_rows:
        status = str(row["status"])
        completed_at = _utc_moment(str(row["updated_at"]))
        day_label = completed_at.astimezone(_BEIJING_TZ).date().isoformat()
        if status in {"FAILED", "SUBMISSION_UNCERTAIN"} or str(row["archive_status"]) == (
            "ARCHIVE_FAILED"
        ):
            failed_by_day[day_label] = failed_by_day.get(day_label, 0) + 1
        if status in {"FAILED", "SUBMISSION_UNCERTAIN"}:
            continue
        if status != "SUCCEEDED":
            continue
        range_generation_outputs += 1
        batch_id = str(row["batch_id"])
        range_generation_batches.add(batch_id)
        if day_label == moment.astimezone(_BEIJING_TZ).date().isoformat():
            today_generation_batches.add(batch_id)
        completed_by_day[day_label] = completed_by_day.get(day_label, 0) + 1
        kind = str(row["creation_kind"] or "replica")
        completed_by_kind[kind] = completed_by_kind.get(kind, 0) + 1
        cost_raw = row["cost_credits"]
        works.append(
            (
                completed_at,
                str(row["id"]),
                StudioAnalyticsWorkItem(
                    task_id=str(row["id"]),
                    task_kind="generation",
                    batch_id=batch_id,
                    project_id=(None if row["project_id"] is None else str(row["project_id"])),
                    title=str(row["project_name"] or ""),
                    creation_kind=kind,
                    completed_at=str(row["updated_at"]),
                    cost_credits=int(cost_raw) if cost_raw is not None else None,
                ),
            )
        )
    for row in oral_rows:
        status = str(row["status"])
        completed_at = _utc_moment(str(row["updated_at"]))
        day_label = completed_at.astimezone(_BEIJING_TZ).date().isoformat()
        if status in {"FAILED", "SUBMISSION_UNCERTAIN", "ARCHIVE_FAILED"}:
            failed_by_day[day_label] = failed_by_day.get(day_label, 0) + 1
            continue
        if status != "SUCCEEDED":
            continue
        range_oral_outputs += 1
        completed_by_day[day_label] = completed_by_day.get(day_label, 0) + 1
        completed_by_kind["oral"] = completed_by_kind.get("oral", 0) + 1
        cost_raw = row["cost_credits"]
        works.append(
            (
                completed_at,
                str(row["id"]),
                StudioAnalyticsWorkItem(
                    task_id=str(row["id"]),
                    task_kind="oral",
                    batch_id=None,
                    project_id=None,
                    title=str(row["title"] or ""),
                    creation_kind="oral",
                    completed_at=str(row["updated_at"]),
                    cost_credits=int(cost_raw) if cost_raw is not None else None,
                ),
            )
        )
    # 最近成片：完成时刻倒序，同一时刻按任务编号稳定排序，截断到看板表格容量。
    works.sort(key=lambda item: (-item[0].timestamp(), item[1]))
    daily = [
        StudioAnalyticsDay(
            day=(start_day + timedelta(days=offset)).isoformat(),
            completed=completed_by_day.get((start_day + timedelta(days=offset)).isoformat(), 0),
            failed=failed_by_day.get((start_day + timedelta(days=offset)).isoformat(), 0),
        )
        for offset in range(days)
    ]
    kind_breakdown = [
        StudioAnalyticsKindCount(kind=kind, completed=count)
        for kind, count in sorted(completed_by_kind.items(), key=lambda item: (-item[1], item[0]))
    ]
    range_completed = range_generation_outputs + range_oral_outputs
    return StudioAnalyticsResponse(
        range_days=days,
        generated_at=moment.isoformat(),
        today_completed=stats.today_completed,
        range_completed=range_completed,
        total_completed=stats.total_completed,
        today_generation_batches=len(today_generation_batches),
        range_generation_batches=len(range_generation_batches),
        total_generation_batches=(
            int(total_batch_row["completed_batches"]) if total_batch_row is not None else 0
        ),
        range_generation_outputs=range_generation_outputs,
        range_oral_outputs=range_oral_outputs,
        daily=daily,
        kind_breakdown=kind_breakdown,
        recent_works=[item for _, _, item in works[:_RECENT_WORKS_CAP]],
    )


@router.get("/studio/analytics", response_model=StudioAnalyticsResponse)
def read_studio_analytics(
    conn: Database,
    actor: AuthenticatedUser,
    days: int = 7,
) -> StudioAnalyticsResponse:
    return studio_analytics(conn, actor=actor, days=days)


# ---------------------------------------------------------------------------
# C10b 通知偏好：按用户一行 JSON 偏好（迁移 076），当前唯一键 enabled
# ---------------------------------------------------------------------------


class StudioNotificationPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


def _read_notification_preferences(
    conn: BusinessConnection,
    user_id: str,
) -> StudioNotificationPreferences:
    row = conn.execute(
        "SELECT prefs_json FROM studio_notification_preferences WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    if row is None:
        return StudioNotificationPreferences()
    try:
        payload = json.loads(str(row["prefs_json"]))
    except json.JSONDecodeError:
        return StudioNotificationPreferences()
    if not isinstance(payload, dict):
        return StudioNotificationPreferences()
    return StudioNotificationPreferences(enabled=bool(payload.get("enabled", True)))


@router.get(
    "/studio/notification-preferences",
    response_model=StudioNotificationPreferences,
)
def read_studio_notification_preferences(
    conn: Database,
    actor: AuthenticatedUser,
) -> StudioNotificationPreferences:
    return _read_notification_preferences(conn, actor.id)


@router.put(
    "/studio/notification-preferences",
    response_model=StudioNotificationPreferences,
)
def update_studio_notification_preferences(
    conn: Database,
    actor: AuthenticatedUser,
    payload: StudioNotificationPreferences,
) -> StudioNotificationPreferences:
    conn.execute(
        """
        INSERT INTO studio_notification_preferences (user_id, prefs_json)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE
        SET prefs_json = (studio_notification_preferences.prefs_json::jsonb
            || excluded.prefs_json::jsonb)::text, updated_at = CURRENT_TIMESTAMP
        """,
        (actor.id, payload.model_dump_json()),
    )
    conn.commit()
    return payload


class StudioNotificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    task_kind: str
    title: str
    status: str
    occurred_at: str
    unread: bool


class StudioNotificationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[StudioNotificationItem]
    unread_count: int
    enabled: bool


def studio_notifications(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
) -> StudioNotificationsResponse:
    preferences = _read_notification_preferences(conn, actor.id)
    if not preferences.enabled:
        return StudioNotificationsResponse(items=[], unread_count=0, enabled=False)
    row = conn.execute(
        "SELECT prefs_json FROM studio_notification_preferences WHERE user_id = %s",
        (actor.id,),
    ).fetchone()
    read_before = "1970-01-01T00:00:00+00:00"
    if row:
        try:
            payload = json.loads(str(row["prefs_json"]))
            timestamp = payload.get("read_before") if isinstance(payload, dict) else None
            if isinstance(timestamp, str) and datetime.fromisoformat(timestamp).tzinfo:
                read_before = timestamp
        except (ValueError, TypeError):
            pass
    # Events derive from durable task states; no polling request writes fake events.
    # Archive changes get a new event time, while hidden/superseded results stay absent.
    rows = conn.execute(
        """
        WITH events AS (
          SELECT 'generation:' || task.id AS id, batch.id AS task_id,
            'generation_batch' AS task_kind,
            COALESCE(NULLIF(batch.display_name, ''), project.name, '视频生成') AS title,
            CASE WHEN task.archive_status = 'ARCHIVE_FAILED' THEN 'ARCHIVE_FAILED'
              ELSE task.status END AS status, task.updated_at::timestamptz AS occurred_at
          FROM generation_tasks AS task
          JOIN generation_batches AS batch ON batch.id = task.batch_id
          LEFT JOIN projects AS project ON project.id = batch.project_id
          WHERE (project.owner_user_id = %s OR
              (batch.project_id IS NULL AND batch.created_by_user_id = %s))
            AND task.superseded_by_task_id IS NULL
            AND task.status IN ('SUCCEEDED', 'FAILED', 'SUBMISSION_UNCERTAIN')
            AND NOT EXISTS (SELECT 1 FROM customer_batch_visibility AS visibility
              WHERE visibility.user_id = %s AND visibility.batch_id = batch.id)
          UNION ALL
          SELECT 'oral:' || oral.id, oral.id, 'oral_task', '数字人口播',
            oral.status, oral.updated_at::timestamptz
          FROM oral_tasks AS oral
          WHERE oral.owner_user_id = %s
            AND oral.status IN ('SUCCEEDED', 'FAILED', 'SUBMISSION_UNCERTAIN', 'ARCHIVE_FAILED')
        )
        SELECT *, occurred_at > %s::timestamptz AS unread,
          COUNT(*) FILTER (WHERE occurred_at > %s::timestamptz) OVER () AS unread_count
        FROM events ORDER BY occurred_at DESC, id DESC LIMIT 50
        """,
        (actor.id, actor.id, actor.id, actor.id, read_before, read_before),
    ).fetchall()
    return StudioNotificationsResponse(
        items=[
            StudioNotificationItem(
                id=str(row["id"]),
                task_id=str(row["task_id"]),
                task_kind=str(row["task_kind"]),
                title=str(row["title"]),
                status=str(row["status"]),
                occurred_at=row["occurred_at"].isoformat(),
                unread=bool(row["unread"]),
            )
            for row in rows
        ],
        unread_count=int(rows[0]["unread_count"]) if rows else 0,
        enabled=True,
    )


@router.get("/studio/notifications", response_model=StudioNotificationsResponse)
def read_studio_notifications(
    conn: Database, actor: AuthenticatedUser
) -> StudioNotificationsResponse:
    return studio_notifications(conn, actor=actor)


class StudioNotificationReadResponse(BaseModel):
    read_before: str


@router.post("/studio/notifications/read", response_model=StudioNotificationReadResponse)
def mark_studio_notifications_read(db: BusinessDbDep) -> StudioNotificationReadResponse:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="studio.notifications_read",
            entity_type="studio_notification_preferences",
            entity_id=actor.id,
        )
        row = conn.execute("SELECT clock_timestamp() AS cutoff").fetchone()
        cutoff = row["cutoff"].isoformat()
        conn.execute(
            """INSERT INTO studio_notification_preferences (user_id, prefs_json)
            VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE
            SET prefs_json = (studio_notification_preferences.prefs_json::jsonb
                || excluded.prefs_json::jsonb)::text, updated_at = CURRENT_TIMESTAMP""",
            (actor.id, json.dumps({"read_before": cutoff})),
        )
    return StudioNotificationReadResponse(read_before=cutoff)


# ---------------------------------------------------------------------------
# C2 独立创作：跨项目「我的提示词」只读聚合
# ---------------------------------------------------------------------------


class SavedPromptListItem(BaseModel):
    generation_context: dict[str, Any] | None = None
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    name: str
    prompt_text: str
    created_at: str


class SavedPromptListPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SavedPromptListItem]


@router.get("/studio/saved-prompts", response_model=SavedPromptListPage)
def read_user_saved_prompts(
    conn: Database,
    actor: AuthenticatedUser,
    limit: int = 50,
) -> SavedPromptListPage:
    """跨项目聚合作者本人的已保存提示词（versions kind='saved_prompt'）。

    独立创作页的「导入提示词」数据源：只读、仅作者本人、按时间倒序。
    存储仍复用项目域的 versions 底座（迁移 061），零新表。
    """
    if limit < 1 or limit > 100:
        limit = 50
    rows = conn.execute(
        """
        SELECT id, project_id, payload_json, created_at
        FROM versions
        WHERE kind = 'saved_prompt' AND author_user_id = %s
        ORDER BY created_at DESC, version_number DESC
        LIMIT %s
        """,
        (actor.id, limit),
    ).fetchall()
    items: list[SavedPromptListItem] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        items.append(
            SavedPromptListItem(
                id=str(row["id"]),
                project_id=str(row["project_id"]),
                name=str(payload.get("name") or "未命名提示词"),
                prompt_text=str(payload.get("prompt_text") or ""),
                generation_context=payload.get("generation_context"),
                created_at=str(row["created_at"]),
            )
        )
    return SavedPromptListPage(items=items)
