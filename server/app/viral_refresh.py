"""Durable, lease-fenced refresh queue for the shared viral-video cache."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.db_portable import BusinessConnection
from app.viral_collection_failures import FAILURES, classify_collection_failure

logger = logging.getLogger(__name__)
VIRAL_REFRESH_LEASE_MINUTES = 10


@dataclass(frozen=True)
class ViralRefreshLease:
    id: str
    worker_id: str
    platform: str
    sort: str
    attempt: int


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _lease_lost() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "VIRAL_REFRESH_LEASE_LOST", "message": "刷新任务租约已失效。"},
    )


def enqueue_viral_refresh_task(conn: BusinessConnection, *, platform: str, sort: str) -> Any:
    task_id = str(uuid4())
    retry_cutoff = _time_text(datetime.now(UTC) - timedelta(minutes=1))
    conn.execute(
        """
        INSERT INTO viral_refresh_tasks (id, platform, sort, status)
        VALUES (%s, %s, %s, 'PENDING')
        ON CONFLICT (platform, sort) DO UPDATE SET
            status = CASE
                WHEN viral_refresh_tasks.status = 'RUNNING' THEN 'RUNNING'
                ELSE 'PENDING'
            END,
            locked_by = CASE
                WHEN viral_refresh_tasks.status = 'RUNNING' THEN viral_refresh_tasks.locked_by
                ELSE NULL
            END,
            locked_until = CASE
                WHEN viral_refresh_tasks.status = 'RUNNING' THEN viral_refresh_tasks.locked_until
                ELSE NULL
            END,
            error_code = CASE
                WHEN viral_refresh_tasks.status = 'RUNNING' THEN viral_refresh_tasks.error_code
                ELSE NULL
            END,
            error_message_redacted = CASE
                WHEN viral_refresh_tasks.status = 'RUNNING'
                    THEN viral_refresh_tasks.error_message_redacted
                ELSE NULL
            END,
            retryable = CASE
                WHEN viral_refresh_tasks.status = 'RUNNING' THEN viral_refresh_tasks.retryable
                ELSE 0
            END,
            completed_at = CASE
                WHEN viral_refresh_tasks.status = 'RUNNING' THEN viral_refresh_tasks.completed_at
                ELSE NULL
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE viral_refresh_tasks.status NOT IN ('PENDING', 'RUNNING')
            AND (viral_refresh_tasks.status != 'FAILED'
                OR viral_refresh_tasks.updated_at <= %s)
        """,
        (task_id, platform, sort, retry_cutoff),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM viral_refresh_tasks WHERE platform = %s AND sort = %s",
        (platform, sort),
    ).fetchone()
    if row is None:
        raise RuntimeError("viral refresh task enqueue did not persist")
    return row


def viral_refresh_status(
    conn: BusinessConnection, *, platform: str, sort: str
) -> tuple[bool, str | None]:
    row = conn.execute(
        """
        SELECT status FROM viral_refresh_tasks
        WHERE platform = %s AND sort = %s AND status IN ('PENDING', 'RUNNING')
        """,
        (platform, sort),
    ).fetchone()
    if row is not None:
        return True, None
    failed = conn.execute(
        """
        SELECT error_message_redacted FROM viral_refresh_tasks
        WHERE platform = %s AND sort = %s AND status = 'FAILED'
        """,
        (platform, sort),
    ).fetchone()
    return False, str(failed["error_message_redacted"]) if failed else None


def acquire_viral_refresh_task(
    conn: BusinessConnection, *, worker_id: str
) -> ViralRefreshLease | None:
    enabled = conn.execute(
        "SELECT collection_enabled FROM viral_runtime_controls WHERE id=1"
    ).fetchone()
    if enabled is None or not enabled[0]:
        return None
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=VIRAL_REFRESH_LEASE_MINUTES))
    conn.execute(
        """
        UPDATE viral_refresh_tasks SET status = 'PENDING', locked_by = NULL,
            locked_until = NULL, error_code = NULL, error_message_redacted = NULL,
            retryable = 0, updated_at = %s
        WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    row = conn.execute(
        """
        UPDATE viral_refresh_tasks SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s, started_at = COALESCE(started_at, %s),
            updated_at = %s, error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM viral_refresh_tasks WHERE status = 'PENDING'
            ORDER BY updated_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING' RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE viral_collection_batches SET run_status='RUNNING',"
            "started_at=COALESCE(started_at,clock_timestamp()),completed_at=NULL,failure_reason=NULL,"
            "failure_code=NULL "
            "WHERE id=%s",
            (str(json.loads(row["collection_config_json"]).get("billing_batch_id") or ""),),
        )
    conn.commit()
    if row is None:
        return None
    return ViralRefreshLease(
        id=str(row["id"]),
        worker_id=worker_id,
        platform=str(row["platform"]),
        sort=str(row["sort"]),
        attempt=int(row["attempt"]),
    )


def _require_lease(conn: BusinessConnection, lease: ViralRefreshLease) -> None:
    now = _time_text(datetime.now(UTC))
    row = conn.execute(
        """
        SELECT 1 FROM viral_refresh_tasks
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        FOR UPDATE
        """,
        (lease.id, lease.worker_id, lease.attempt, now),
    ).fetchone()
    if row is None:
        raise _lease_lost()


def complete_viral_refresh_task(conn: BusinessConnection, *, lease: ViralRefreshLease) -> None:
    _require_lease(conn, lease)
    now = _time_text(datetime.now(UTC))
    updated = conn.execute(
        """
        UPDATE viral_refresh_tasks SET status = 'SUCCEEDED', locked_by = NULL,
            locked_until = NULL, retryable = 0, error_code = NULL,
            error_message_redacted = NULL, completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (now, now, lease.id, lease.worker_id, lease.attempt, now),
    )
    if updated.rowcount != 1:
        raise _lease_lost()
    conn.execute(
        "UPDATE viral_collection_batches SET run_status='SUCCEEDED',completed_at=clock_timestamp(),"
        "failed_video_count=0,failure_reason=NULL WHERE id=(SELECT collection_config_json::jsonb"
        "->>'billing_batch_id' FROM viral_refresh_tasks WHERE id=%s)",
        (lease.id,),
    )
    conn.commit()


def fail_viral_refresh_task(
    conn: BusinessConnection, *, lease: ViralRefreshLease, cause: Exception
) -> None:
    logger.warning("viral refresh task %s failed: %s", lease.id, type(cause).__name__)
    code = classify_collection_failure(cause)
    now = _time_text(datetime.now(UTC))
    updated = conn.execute(
        """
        UPDATE viral_refresh_tasks SET status = 'FAILED', locked_by = NULL,
            locked_until = NULL, error_code = 'VIRAL_REFRESH_FAILED',
            error_message_redacted = '爆款视频刷新失败，请稍后重试。',
            retryable = 1, completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (now, now, lease.id, lease.worker_id, lease.attempt, now),
    )
    if updated.rowcount != 1:
        raise _lease_lost()
    conn.execute(
        "UPDATE viral_collection_batches SET run_status='FAILED',completed_at=clock_timestamp(),"
        "failure_reason=%s,failure_code=%s "
        "WHERE id=(SELECT collection_config_json::jsonb->>'billing_batch_id' "
        "FROM viral_refresh_tasks WHERE id=%s)",
        (FAILURES[code][0], code, lease.id),
    )
    conn.commit()
