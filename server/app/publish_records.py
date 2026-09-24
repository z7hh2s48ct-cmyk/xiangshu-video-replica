"""Publish records (phase 2): queue a stored video to one platform account.

A record is created ``queued`` with an optional ``scheduled_at``; the publish
worker claims it once ``COALESCE(scheduled_at, created_at)`` has passed, moves
it to ``publishing`` under a fenced lease, delivers through the platform
adapters and finalizes it ``published`` / ``failed`` (or re-queues a transient
failure). Only ``queued`` rows can be cancelled; ``failed`` rows can be
retried. Result-collection columns (``stats`` …) are written by the follow-up
sync task and only exposed read-only here.

Accounts are the encrypted browser-login rows (``publish_browser_accounts``);
the legacy cookie-paste ``publish_accounts`` table is not a delivery source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.materials import require_material
from app.permissions import require_asset_access, require_not_auditor
from app.publish import PublishLeaseLostError
from app.publishers.base import PublishResult

PLATFORMS: tuple[str, ...] = ("douyin", "wechat_channels", "xiaohongshu")
# 小红书没有协议发布器，本期只能扫码登录；记录表预留 platform 值。
DELIVERABLE_PLATFORMS: frozenset[str] = frozenset({"douyin", "wechat_channels"})

RecordStatus = Literal["queued", "publishing", "published", "failed", "cancelled"]
STATUSES: tuple[str, ...] = ("queued", "publishing", "published", "failed", "cancelled")

MAX_TITLE_LENGTH = 300
MAX_DESCRIPTION_LENGTH = 5000
MAX_TAGS = 20
MAX_TAG_LENGTH = 40
MAX_LIST_LIMIT = 100
SCHEDULE_MIN_LEAD = timedelta(minutes=2)
SCHEDULE_MAX_LEAD = timedelta(days=30)

PUBLISH_LEASE_SECONDS = 900
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=5)

QUARANTINE_MESSAGE = "发布中断，已重新排队"
ACCOUNT_GONE_MESSAGE = "发布账号已解绑或登录态失效，请重新连接后重试"


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class PublishRecordCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    video_material_id: str = Field(min_length=1, max_length=128)
    cover_material_id: str | None = Field(default=None, max_length=128)
    title: str = Field(default="", max_length=MAX_TITLE_LENGTH)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LENGTH)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    scheduled_at: datetime | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class PublishRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    platform: str
    account_id: str | None
    account_username: str | None
    video_asset_id: str
    cover_asset_id: str | None
    title: str
    description: str
    tags: list[str]
    scheduled_at: str | None
    status: str
    delivery_mode: str | None
    platform_item_id: str | None
    platform_short_url: str | None
    platform_status: str | None
    stats: dict[str, Any] | None
    stats_synced_at: str | None
    sync_requested: bool
    error_message: str | None
    published_at: str | None
    attempt_count: int
    created_at: str
    updated_at: str


class PublishRecordListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[PublishRecordResponse]


class PublishSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    published_total: int
    queued_total: int
    failed_total: int
    play_total: int
    like_total: int


class PublishRecordActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: PublishRecordResponse


class PublishRecordDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment: datetime = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        text: str = moment.astimezone(UTC).isoformat()
        return text
    return str(value)


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _json_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return dict(value) if isinstance(value, dict) else None


_RECORD_SELECT = """
    SELECT r.*, a.username AS account_username
    FROM publish_records r
    LEFT JOIN publish_browser_accounts a ON a.id = r.account_id
"""


def record_response(row: Any) -> PublishRecordResponse:
    data = dict(row)
    return PublishRecordResponse(
        id=str(data["id"]),
        platform=str(data["platform"]),
        account_id=None if data["account_id"] is None else str(data["account_id"]),
        account_username=(
            None if data.get("account_username") is None else str(data["account_username"])
        ),
        video_asset_id=str(data["video_asset_id"]),
        cover_asset_id=None if data["cover_asset_id"] is None else str(data["cover_asset_id"]),
        title=str(data["title"]),
        description=str(data["description"]),
        tags=_json_list(data["tags"]),
        scheduled_at=_iso(data["scheduled_at"]),
        status=str(data["status"]),
        delivery_mode=None if data["delivery_mode"] is None else str(data["delivery_mode"]),
        platform_item_id=(
            None if data["platform_item_id"] is None else str(data["platform_item_id"])
        ),
        platform_short_url=(
            None if data["platform_short_url"] is None else str(data["platform_short_url"])
        ),
        platform_status=None if data["platform_status"] is None else str(data["platform_status"]),
        stats=_json_dict(data["stats"]),
        stats_synced_at=_iso(data["stats_synced_at"]),
        sync_requested=bool(data["sync_requested"]),
        error_message=None if data["error_message"] is None else str(data["error_message"]),
        published_at=_iso(data["published_at"]),
        attempt_count=int(data["attempt_count"]),
        created_at=_iso(data["created_at"]) or "",
        updated_at=_iso(data["updated_at"]) or "",
    )


def _require_owned_record(conn: BusinessConnection, *, actor_id: str, record_id: str) -> Any:
    row = conn.execute(
        _RECORD_SELECT + " WHERE r.id = %s AND r.user_id = %s",
        (record_id, actor_id),
    ).fetchone()
    if row is None:
        raise _error(404, "PUBLISH_RECORD_NOT_FOUND", "发布记录不存在或无权访问。")
    return row


def _normalize_tags(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    for tag in tags:
        text = tag.strip().lstrip("#").strip()
        if not text:
            continue
        if len(text) > MAX_TAG_LENGTH:
            raise _error(422, "PUBLISH_TAG_TOO_LONG", f"单个标签不能超过 {MAX_TAG_LENGTH} 字。")
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _normalize_schedule(scheduled_at: datetime | None, *, now: datetime) -> datetime | None:
    if scheduled_at is None:
        return None
    if scheduled_at.tzinfo is None:
        raise _error(422, "PUBLISH_SCHEDULE_TIMEZONE_REQUIRED", "定时时间必须携带时区。")
    moment = scheduled_at.astimezone(UTC)
    if moment < now + SCHEDULE_MIN_LEAD:
        raise _error(422, "PUBLISH_SCHEDULE_TOO_SOON", "定时发布至少需要提前 2 分钟。")
    if moment > now + SCHEDULE_MAX_LEAD:
        raise _error(422, "PUBLISH_SCHEDULE_TOO_FAR", "定时发布最多只能提前 30 天。")
    return moment


def _resolve_stored_asset(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_id: str,
    expected_media: Literal["video", "image"],
) -> str:
    item = require_material(conn, actor=actor, material_id=material_id)
    if item.media_type != expected_media:
        label = "视频" if expected_media == "video" else "封面图片"
        raise _error(422, "PUBLISH_MATERIAL_TYPE_MISMATCH", f"所选素材不是{label}。")
    if item.asset_id is None or item.delivery != "stored":
        raise _error(
            422,
            "PUBLISH_MATERIAL_NOT_STORED",
            "该成片尚未保存到素材库，请先保存后再发布。",
        )
    asset = require_asset_access(
        conn, actor=actor, asset_id=item.asset_id, action="publish.record.create"
    )
    if int(asset["size_bytes"] or 0) <= 0 or not str(asset["sha256"] or ""):
        raise _error(409, "ASSET_UPLOAD_NOT_COMPLETE", "素材尚未上传完成。")
    return str(asset["id"])


# ---------------------------------------------------------------------------
# API domain
# ---------------------------------------------------------------------------


def create_record(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    request: PublishRecordCreateRequest,
    now: datetime | None = None,
) -> PublishRecordResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="publish.record.create",
        entity_type="publish_record",
        entity_id="new",
    )
    moment = now or _now()
    account = conn.execute(
        "SELECT id, platform, status FROM publish_browser_accounts WHERE id = %s AND user_id = %s",
        (request.account_id, actor.id),
    ).fetchone()
    if account is None:
        raise _error(404, "PUBLISH_ACCOUNT_NOT_FOUND", "发布账号不存在或无权访问。")
    platform = str(account["platform"])
    if platform not in DELIVERABLE_PLATFORMS:
        raise _error(
            422,
            "PUBLISH_PLATFORM_NOT_READY",
            "该平台的自动发布即将上线，请先前往官方页面发布。",
        )
    if str(account["status"]) != "connected":
        raise _error(409, "PUBLISH_ACCOUNT_INVALID", "发布账号登录态已失效，请重新扫码。")

    video_asset_id = _resolve_stored_asset(
        conn, actor=actor, material_id=request.video_material_id, expected_media="video"
    )
    cover_asset_id = (
        _resolve_stored_asset(
            conn, actor=actor, material_id=request.cover_material_id, expected_media="image"
        )
        if request.cover_material_id
        else None
    )
    scheduled_at = _normalize_schedule(request.scheduled_at, now=moment)
    tags = _normalize_tags(request.tags)

    duplicate = conn.execute(
        """
        SELECT id FROM publish_records
        WHERE user_id = %s AND account_id = %s AND video_asset_id = %s
          AND status IN ('queued', 'publishing')
        """,
        (actor.id, request.account_id, video_asset_id),
    ).fetchone()
    if duplicate is not None:
        raise _error(409, "PUBLISH_RECORD_DUPLICATE", "该视频已在此账号的发布队列中。")

    record_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO publish_records (
            id, user_id, account_id, platform, video_asset_id, cover_asset_id,
            title, description, tags, options, scheduled_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        """,
        (
            record_id,
            actor.id,
            request.account_id,
            platform,
            video_asset_id,
            cover_asset_id,
            request.title.strip(),
            request.description.strip(),
            json.dumps(tags, ensure_ascii=False),
            json.dumps(request.options, ensure_ascii=False),
            scheduled_at,
        ),
    )
    row = _require_owned_record(conn, actor_id=actor.id, record_id=record_id)
    conn.commit()
    return record_response(row)


def list_records(
    conn: BusinessConnection,
    *,
    actor_id: str,
    status: str | None = None,
    platform: str | None = None,
    limit: int = 50,
) -> PublishRecordListResponse:
    if status is not None and status not in STATUSES:
        raise _error(422, "PUBLISH_STATUS_UNSUPPORTED", "不支持的发布状态筛选。")
    if platform is not None and platform not in PLATFORMS:
        raise _error(422, "PUBLISH_PLATFORM_UNSUPPORTED", "不支持的发布平台。")
    bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
    clauses = ["r.user_id = %s"]
    params: list[object] = [actor_id]
    if status is not None:
        clauses.append("r.status = %s")
        params.append(status)
    if platform is not None:
        clauses.append("r.platform = %s")
        params.append(platform)
    params.append(bounded)
    rows = conn.execute(
        _RECORD_SELECT
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY r.created_at DESC, r.id LIMIT %s",
        tuple(params),
    ).fetchall()
    return PublishRecordListResponse(records=[record_response(row) for row in rows])


def get_record(conn: BusinessConnection, *, actor_id: str, record_id: str) -> PublishRecordResponse:
    return record_response(_require_owned_record(conn, actor_id=actor_id, record_id=record_id))


def _transition(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    record_id: str,
    action: str,
    from_statuses: tuple[str, ...],
    assignments: str,
    params: tuple[object, ...],
    conflict_message: str,
) -> PublishRecordResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action=action,
        entity_type="publish_record",
        entity_id=record_id,
    )
    _require_owned_record(conn, actor_id=actor.id, record_id=record_id)
    placeholders = ", ".join("%s" for _ in from_statuses)
    cursor = conn.execute(
        f"""
        UPDATE publish_records
        SET {assignments}, updated_at = clock_timestamp()
        WHERE id = %s AND user_id = %s AND status IN ({placeholders})
        """,  # noqa: S608 - assignments are fixed internal literals
        (*params, record_id, actor.id, *from_statuses),
    )
    if cursor.rowcount != 1:
        raise _error(409, "PUBLISH_RECORD_STATE_CONFLICT", conflict_message)
    row = _require_owned_record(conn, actor_id=actor.id, record_id=record_id)
    conn.commit()
    return record_response(row)


def cancel_record(
    conn: BusinessConnection, *, actor: CurrentUser, record_id: str
) -> PublishRecordResponse:
    return _transition(
        conn,
        actor=actor,
        record_id=record_id,
        action="publish.record.cancel",
        from_statuses=("queued",),
        assignments="status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL",
        params=(),
        conflict_message="只有排队中的发布可以取消。",
    )


def retry_record(
    conn: BusinessConnection, *, actor: CurrentUser, record_id: str
) -> PublishRecordResponse:
    return _transition(
        conn,
        actor=actor,
        record_id=record_id,
        action="publish.record.retry",
        from_statuses=("failed",),
        assignments=(
            "status = 'queued', attempt_count = 0, error_message = NULL, "
            "scheduled_at = NULL, lease_owner = NULL, lease_expires_at = NULL"
        ),
        params=(),
        conflict_message="只有失败的发布可以重试。",
    )


def request_sync(
    conn: BusinessConnection, *, actor: CurrentUser, record_id: str
) -> PublishRecordResponse:
    return _transition(
        conn,
        actor=actor,
        record_id=record_id,
        action="publish.record.sync",
        from_statuses=("published",),
        assignments="sync_requested = 1",
        params=(),
        conflict_message="只有已发布的作品可以同步数据。",
    )


def delete_record(conn: BusinessConnection, *, actor: CurrentUser, record_id: str) -> None:
    require_not_auditor(
        conn,
        actor=actor,
        action="publish.record.delete",
        entity_type="publish_record",
        entity_id=record_id,
    )
    _require_owned_record(conn, actor_id=actor.id, record_id=record_id)
    cursor = conn.execute(
        """
        DELETE FROM publish_records
        WHERE id = %s AND user_id = %s AND status IN ('published', 'failed', 'cancelled')
        """,
        (record_id, actor.id),
    )
    if cursor.rowcount != 1:
        raise _error(409, "PUBLISH_RECORD_STATE_CONFLICT", "排队或发布中的记录不能删除，请先取消。")
    conn.commit()


def summary(conn: BusinessConnection, *, actor_id: str) -> PublishSummaryResponse:
    row = conn.execute(
        """
        SELECT
            count(*) FILTER (WHERE status = 'published') AS published_total,
            count(*) FILTER (WHERE status IN ('queued', 'publishing')) AS queued_total,
            count(*) FILTER (WHERE status = 'failed') AS failed_total,
            COALESCE(sum((stats ->> 'play_count')::bigint)
                     FILTER (WHERE status = 'published'
                             AND jsonb_typeof(stats -> 'play_count') = 'number'), 0)
                AS play_total,
            COALESCE(sum((stats ->> 'like_count')::bigint)
                     FILTER (WHERE status = 'published'
                             AND jsonb_typeof(stats -> 'like_count') = 'number'), 0)
                AS like_total
        FROM publish_records WHERE user_id = %s
        """,
        (actor_id,),
    ).fetchone()
    return PublishSummaryResponse(
        published_total=int(row["published_total"]),
        queued_total=int(row["queued_total"]),
        failed_total=int(row["failed_total"]),
        play_total=int(row["play_total"]),
        like_total=int(row["like_total"]),
    )


# ---------------------------------------------------------------------------
# Worker claims / finalizers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishWork:
    record_id: str
    worker_id: str
    lease_token: str
    attempt_count: int
    row: dict[str, Any]


def _quarantine_expired_publishes(conn: BusinessConnection) -> None:
    """Re-queue publishes whose worker died mid-delivery.

    Separate from ``publish._quarantine_expired_verifies`` on purpose: the two
    tables have independent lifecycles and test A12 pins the verify statement.
    """
    conn.execute(
        """
        UPDATE publish_records
        SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
            error_message = %s, updated_at = clock_timestamp()
        WHERE status = 'publishing' AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= clock_timestamp()
        """,
        (QUARANTINE_MESSAGE,),
    )


def _fail_orphaned_queued(conn: BusinessConnection) -> None:
    """Queued rows whose account vanished or went invalid can never deliver."""
    conn.execute(
        """
        UPDATE publish_records r
        SET status = 'failed', error_message = %s, lease_owner = NULL,
            lease_expires_at = NULL, updated_at = clock_timestamp()
        WHERE r.status = 'queued'
          AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= clock_timestamp())
          AND (
            r.account_id IS NULL
            OR NOT EXISTS (
                SELECT 1 FROM publish_browser_accounts a
                WHERE a.id = r.account_id AND a.user_id = r.user_id AND a.status = 'connected'
            )
          )
        """,
        (ACCOUNT_GONE_MESSAGE,),
    )


_PUBLISH_CANDIDATE_SQL = """
    SELECT r.*, a.storage_state_enc, a.platform_user_id, a.username AS account_username,
           v.storage_uri AS video_uri, v.content_type AS video_content_type,
           c.storage_uri AS cover_uri, c.content_type AS cover_content_type
    FROM publish_records r
    JOIN publish_browser_accounts a
      ON a.id = r.account_id AND a.user_id = r.user_id AND a.status = 'connected'
    JOIN assets v ON v.id = r.video_asset_id
    LEFT JOIN assets c ON c.id = r.cover_asset_id
    WHERE r.status = 'queued'
      AND COALESCE(r.scheduled_at, r.created_at) <= clock_timestamp()
      AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= clock_timestamp())
      AND NOT EXISTS (
        SELECT 1 FROM publish_records p
        WHERE p.account_id = r.account_id AND p.status = 'publishing'
          AND p.lease_expires_at > clock_timestamp()
      )
    ORDER BY COALESCE(r.scheduled_at, r.created_at), r.id
    LIMIT 1
    FOR UPDATE OF r SKIP LOCKED
"""


def claim_publish_work(
    conn: BusinessConnection,
    *,
    worker_id: str,
    lease_seconds: int = PUBLISH_LEASE_SECONDS,
) -> PublishWork | None:
    with conn:
        _quarantine_expired_publishes(conn)
        _fail_orphaned_queued(conn)
    with conn:
        row = conn.execute(_PUBLISH_CANDIDATE_SQL).fetchone()
        if row is None:
            return None
        record = dict(row)
        lease_token = uuid4().hex
        claimed = conn.execute(
            """
            UPDATE publish_records
            SET status = 'publishing', lease_owner = %s,
                lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                attempt_count = attempt_count + 1, error_message = NULL,
                updated_at = clock_timestamp()
            WHERE id = %s AND status = 'queued'
              AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp())
            RETURNING attempt_count
            """,
            (lease_token, int(lease_seconds), str(record["id"])),
        ).fetchone()
        if claimed is None:
            return None
        record["status"] = "publishing"
        record["lease_owner"] = lease_token
        record["attempt_count"] = int(claimed["attempt_count"])
        return PublishWork(
            record_id=str(record["id"]),
            worker_id=worker_id,
            lease_token=lease_token,
            attempt_count=int(claimed["attempt_count"]),
            row=record,
        )


def finalize_publish_work(
    conn: BusinessConnection,
    *,
    work: PublishWork,
    result: PublishResult,
    delivery_mode: Literal["api", "browser"] | None = None,
) -> str:
    """Fenced write-back; returns the resulting record status."""
    fence = "WHERE id = %s AND status = 'publishing' AND lease_owner = %s AND attempt_count = %s"
    fence_params: tuple[object, ...] = (work.record_id, work.lease_token, work.attempt_count)
    with conn:
        if result.status == "published":
            outcome = "published"
            cursor = conn.execute(
                f"""
                UPDATE publish_records
                SET status = 'published', delivery_mode = %s, platform_item_id = %s,
                    platform_short_url = %s, error_message = %s,
                    published_at = clock_timestamp(), lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = clock_timestamp()
                {fence}
                """,  # noqa: S608 - fence is a fixed literal
                (
                    delivery_mode,
                    result.item_id,
                    result.short_url,
                    result.message,
                    *fence_params,
                ),
            )
        elif result.account_invalid:
            outcome = "failed"
            cursor = conn.execute(
                f"""
                UPDATE publish_records
                SET status = 'failed', error_message = %s, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = clock_timestamp()
                {fence}
                """,  # noqa: S608
                (result.message or "平台拒绝了当前登录态", *fence_params),
            )
            if cursor.rowcount == 1 and work.row.get("account_id"):
                conn.execute(
                    """
                    UPDATE publish_browser_accounts
                    SET status = 'invalid', error_message = %s
                    WHERE id = %s AND user_id = %s
                    """,
                    (
                        result.message or "平台拒绝了当前登录态，请重新扫码",
                        str(work.row["account_id"]),
                        str(work.row["user_id"]),
                    ),
                )
        elif work.attempt_count < MAX_ATTEMPTS:
            outcome = "queued"
            cursor = conn.execute(
                f"""
                UPDATE publish_records
                SET status = 'queued', error_message = %s,
                    scheduled_at = clock_timestamp() + make_interval(secs => %s),
                    lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                {fence}
                """,  # noqa: S608
                (
                    f"{result.message or '发布失败'}（将自动重试）",
                    int(RETRY_DELAY.total_seconds()),
                    *fence_params,
                ),
            )
        else:
            outcome = "failed"
            cursor = conn.execute(
                f"""
                UPDATE publish_records
                SET status = 'failed', error_message = %s, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = clock_timestamp()
                {fence}
                """,  # noqa: S608
                (result.message or "发布失败", *fence_params),
            )
        if cursor.rowcount != 1:
            raise PublishLeaseLostError("publish lease was lost")
    return outcome
