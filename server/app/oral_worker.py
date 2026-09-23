"""Durable oral-media worker operations.

Claims and final database writes are short transactions. Provider and object
storage calls happen only in ``perform_oral_work`` between those transactions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import psycopg

from app import content_store
from app.db_portable import BusinessConnection
from app.generation import (
    ensure_user_queue_cursor,
    lock_shared_generation_capacity,
    read_runtime_limits,
    shared_generation_capacity_available,
)
from app.hifly import HiflyClient, HiflyError, HiflySubmissionUncertain
from app.internal_billing import finalize_oral_billing, release_oral_queue_slot
from app.media import storage_key_from_uri
from app.media_tools import (
    MediaToolFailed,
    MediaToolUnavailable,
    MediaValidationFailed,
    inspect_media_bytes,
    normalize_audio_to_mp3,
)
from app.storage import StorageAdapter, StoredObject

logger = logging.getLogger(__name__)

OralWorkKind = Literal[
    "avatar_submit",
    "avatar_poll",
    "voice_submit",
    "voice_poll",
    "task_submit",
    "task_poll",
    "task_archive",
]
OralOutcome = Literal["submitted", "waiting", "uncertain", "failed", "ready", "archive"]


@dataclass(frozen=True)
class OralWorkLease:
    kind: OralWorkKind
    record_id: str
    worker_id: str
    lease_token: str
    attempt_count: int
    row: dict[str, Any]


@dataclass(frozen=True)
class OralWorkResult:
    outcome: OralOutcome
    provider_task_id: str | None = None
    provider_resource_id: str | None = None
    provider_result_url: str | None = None
    duration_sec: float | None = None
    stored: StoredObject | None = None
    message: str | None = None


class OralLeaseLostError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _claim_row(
    conn: BusinessConnection,
    *,
    table: str,
    record_id: str,
    lease_token: str,
    current_state_sql: str,
    current_state_params: tuple[object, ...],
    assignments: str,
    lease_expires_at: str,
) -> int | None:
    row = conn.execute(
        f"""
        UPDATE {table}
        SET {assignments}, lease_owner = %s, lease_expires_at = %s,
            attempt_count = attempt_count + 1, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND ({current_state_sql})
          AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
        RETURNING attempt_count
        """,  # noqa: S608 - table/assignments are fixed internal literals
        (
            lease_token,
            lease_expires_at,
            record_id,
            *current_state_params,
            _iso(_now()),
        ),
    ).fetchone()
    return None if row is None else int(row["attempt_count"])


def _quarantine_expired_submissions(conn: BusinessConnection, now: str) -> None:
    for table in ("oral_avatars", "oral_voices"):
        stopped = conn.execute(
            f"""
            UPDATE {table}
            SET status = 'FAILED', submission_state = 'SUBMISSION_UNKNOWN', lease_owner = NULL,
                lease_expires_at = NULL, error_message = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE submission_state = 'SUBMITTING' AND lease_expires_at <= %s
            RETURNING id
            """,  # noqa: S608 - fixed table names
            ("供应商提交结果未知，已停止自动重试", now),
        ).fetchall()
        from app.usage_billing import finish_source

        for clone in stopped:
            finish_source(conn, str(clone["id"]), units=0, succeeded=False)
    uncertain_tasks = conn.execute(
        """
        UPDATE oral_tasks
        SET status = 'SUBMISSION_UNCERTAIN', submission_state = 'SUBMISSION_UNKNOWN',
            provider_charge_state = 'UNKNOWN',
            lease_owner = NULL, lease_expires_at = NULL,
            error_message = %s, updated_at = CURRENT_TIMESTAMP
        WHERE status = 'SUBMITTING' AND lease_expires_at <= %s
        RETURNING id
        """,
        ("供应商提交结果未知，已停止自动重试", now),
    ).fetchall()
    for row in uncertain_tasks:
        release_oral_queue_slot(conn, oral_task_id=str(row["id"]))
    interrupted_archives = conn.execute(
        """
        UPDATE oral_tasks
        SET status = 'ARCHIVE_FAILED', lease_owner = NULL, lease_expires_at = NULL,
            next_attempt_at = NULL, error_message = %s, updated_at = CURRENT_TIMESTAMP
        WHERE status = 'ARCHIVING' AND lease_expires_at <= %s
        RETURNING id
        """,
        ("成片归档中断，可安全重试归档", now),
    ).fetchall()
    for row in interrupted_archives:
        release_oral_queue_slot(conn, oral_task_id=str(row["id"]))


def _pg_oral_submit_candidate(
    conn: BusinessConnection,
    *,
    now_text: str,
) -> dict[str, Any] | None:
    """Pick an idle user's oral task under the shared render hard limits.

    This deliberately provides the safety gate only: it shares PostgreSQL's
    per-user cursor slot and the cross-type concurrency ceiling, but full
    round-robin ordering across generation and oral queues remains follow-up.
    """
    runtime = read_runtime_limits(conn)
    if not shared_generation_capacity_available(
        conn,
        max_concurrent_tasks=runtime["max_concurrent_h3_tasks"],
    ):
        return None
    conn.execute(
        """
        INSERT INTO user_queue_cursors (user_id, last_dispatched_at, running_tasks_count)
        SELECT DISTINCT owner_user_id, now(), 0
        FROM oral_tasks
        WHERE status = 'QUEUED'
        ON CONFLICT (user_id) DO NOTHING
        """
    )
    owner = conn.execute(
        """
        SELECT cursor.user_id
        FROM user_queue_cursors AS cursor
        WHERE cursor.running_tasks_count = 0
          AND NOT EXISTS (
              SELECT 1 FROM oral_tasks AS active_oral
              WHERE active_oral.owner_user_id = cursor.user_id
                AND active_oral.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
          )
          AND NOT EXISTS (
              SELECT 1
              FROM generation_tasks AS generation
              JOIN generation_batches AS batch ON batch.id = generation.batch_id
              WHERE batch.created_by_user_id = cursor.user_id
                AND generation.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
          )
          AND EXISTS (
              SELECT 1 FROM oral_tasks AS task
              WHERE task.owner_user_id = cursor.user_id
                AND task.status = 'QUEUED'
                AND (task.next_attempt_at IS NULL OR task.next_attempt_at <= %s)
                AND (task.lease_expires_at IS NULL OR task.lease_expires_at <= %s)
          )
        ORDER BY (
            SELECT MIN(task.created_at) FROM oral_tasks AS task
            WHERE task.owner_user_id = cursor.user_id AND task.status = 'QUEUED'
        ), cursor.user_id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """,
        (now_text, now_text),
    ).fetchone()
    if owner is None:
        return None
    task = conn.execute(
        """
        SELECT * FROM oral_tasks
        WHERE owner_user_id = %s AND status = 'QUEUED'
          AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
          AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
        ORDER BY created_at, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """,
        (str(owner["user_id"]), now_text, now_text),
    ).fetchone()
    return None if task is None else dict(task)


def claim_oral_work(
    conn: BusinessConnection,
    *,
    worker_id: str,
    lease_seconds: int = 120,
) -> OralWorkLease | None:
    """Claim one oral operation with CAS; expired paid submissions quarantine."""
    now = _now()
    now_text = _iso(now)
    expires = _iso(now + timedelta(seconds=lease_seconds))
    with conn:
        lock_shared_generation_capacity(conn)
        _quarantine_expired_submissions(conn, now_text)
    # BusinessConnection 的 commit() 在 PG fenced 通道上是刻意 no-op；worker
    # 拥有裸连接时必须真正提交隔离边界，否则容量行锁会跨越 claim 临界区把
    # 并发 worker 串在后面。pooled pg_transaction() 托管块内禁止显式提交，
    # 此时边界由外层调用方持有，跳过即可（锁在块结束时释放）。
    _commit_worker_boundary(conn)

    with conn:
        task = conn.execute(
            """
            SELECT * FROM oral_tasks
            WHERE status IN ('ARCHIVING', 'RUNNING')
              AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
              AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
            ORDER BY CASE status WHEN 'ARCHIVING' THEN 0 ELSE 1 END, created_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (now_text, now_text),
        ).fetchone()
        if task is None:
            if conn.is_postgres:
                task = _pg_oral_submit_candidate(conn, now_text=now_text)
            else:
                task = conn.execute(
                    """
                    SELECT * FROM oral_tasks
                    WHERE status = 'QUEUED'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                      AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
                    ORDER BY created_at, id
                    LIMIT 1
                    """,
                    (now_text, now_text),
                ).fetchone()
        if task is not None:
            row = dict(task)
            status = str(row["status"])
            kind = cast(
                OralWorkKind,
                {
                    "QUEUED": "task_submit",
                    "RUNNING": "task_poll",
                    "ARCHIVING": "task_archive",
                }[status],
            )
            assignments = (
                "status = 'SUBMITTING', submission_state = 'SUBMITTING', "
                f"queue_slot_acquired = {1 if conn.is_postgres else 0}"
                if status == "QUEUED"
                else "status = status"
            )
            lease_token = uuid4().hex
            attempt_count = _claim_row(
                conn,
                table="oral_tasks",
                record_id=str(row["id"]),
                lease_token=lease_token,
                current_state_sql="status = %s",
                current_state_params=(status,),
                assignments=assignments,
                lease_expires_at=expires,
            )
            if attempt_count is not None:
                if status == "QUEUED" and conn.is_postgres:
                    slot = conn.execute(
                        """
                        UPDATE user_queue_cursors
                        SET running_tasks_count = running_tasks_count + 1,
                            last_dispatched_at = now()
                        WHERE user_id = %s AND running_tasks_count = 0
                        """,
                        (str(row["owner_user_id"]),),
                    )
                    if slot.rowcount != 1:
                        raise RuntimeError("oral user queue slot was lost")
                row["status"] = "SUBMITTING" if status == "QUEUED" else status
                row["attempt_count"] = attempt_count
                row["lease_owner"] = lease_token
                return OralWorkLease(
                    kind=kind,
                    record_id=str(row["id"]),
                    worker_id=worker_id,
                    lease_token=lease_token,
                    attempt_count=attempt_count,
                    row=row,
                )

        for table, prefix in (("oral_avatars", "avatar"), ("oral_voices", "voice")):
            candidate = conn.execute(
                f"""
                SELECT * FROM {table}
                WHERE (
                    submission_state = 'LOCAL_PENDING'
                    OR (submission_state = 'SUBMITTED' AND status = 'RUNNING')
                )
                  AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                  AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
                ORDER BY CASE submission_state WHEN 'SUBMITTED' THEN 0 ELSE 1 END,
                         created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,  # noqa: S608 - fixed table names
                (now_text, now_text),
            ).fetchone()
            if candidate is None:
                continue
            row = dict(candidate)
            submitting = str(row["submission_state"]) == "LOCAL_PENDING"
            kind = cast(OralWorkKind, f"{prefix}_{'submit' if submitting else 'poll'}")
            lease_token = uuid4().hex
            attempt_count = _claim_row(
                conn,
                table=table,
                record_id=str(row["id"]),
                lease_token=lease_token,
                current_state_sql="submission_state = %s AND status = %s",
                current_state_params=(
                    str(row["submission_state"]),
                    str(row["status"]),
                ),
                assignments=(
                    "submission_state = 'SUBMITTING'"
                    if submitting
                    else "submission_state = submission_state"
                ),
                lease_expires_at=expires,
            )
            if attempt_count is not None:
                row["attempt_count"] = attempt_count
                row["lease_owner"] = lease_token
                return OralWorkLease(
                    kind=kind,
                    record_id=str(row["id"]),
                    worker_id=worker_id,
                    lease_token=lease_token,
                    attempt_count=attempt_count,
                    row=row,
                )
    return None


def _commit_worker_boundary(conn: BusinessConnection) -> None:
    """End the quarantine transaction before the claim critical section.

    - SQLite: wrapper commit() is the real boundary.
    - PostgreSQL, bare worker-owned connection (standalone runtimes/tests):
      the implicit transaction must be committed here, or the capacity row
      lock serializes every concurrent claim behind this one.
    - PostgreSQL, pooled ``pg_transaction()`` block: an explicit commit is
      forbidden; the caller owns the boundary, so skip and let the block end
      release the lock.
    """
    if not conn.is_postgres:
        conn.commit()
        return
    raw = conn.raw
    if not isinstance(raw, psycopg.Connection):
        return
    if raw.info.transaction_status.name != "INTRANS":
        return
    try:
        raw.commit()
    except psycopg.ProgrammingError:
        # Raised by psycopg when a Transaction context owns the connection.
        pass


def _object_bytes(storage: StorageAdapter, uri: str) -> bytes:
    return storage.get_object(storage_key_from_uri(uri))


def perform_oral_work(
    lease: OralWorkLease,
    *,
    vendor: HiflyClient,
    storage: StorageAdapter,
) -> OralWorkResult:
    """Perform provider/storage I/O with no database transaction."""
    row = lease.row
    try:
        if lease.kind in {"avatar_submit", "voice_submit", "task_submit"}:
            return _perform_submission(lease, vendor=vendor, storage=storage)
        if lease.kind == "avatar_poll":
            avatar_snapshot = vendor.avatar_task(str(row["vendor_task_id"]))
            if avatar_snapshot.status == "DONE" and avatar_snapshot.avatar_id:
                return OralWorkResult("ready", provider_resource_id=avatar_snapshot.avatar_id)
            if avatar_snapshot.status == "FAILED":
                return OralWorkResult("failed", message="分身制作未通过")
            return OralWorkResult("waiting")
        if lease.kind == "voice_poll":
            voice_snapshot = vendor.voice_task(str(row["vendor_task_id"]))
            if voice_snapshot.status == "DONE" and voice_snapshot.voice and voice_snapshot.demo_url:
                demo = vendor.download(voice_snapshot.demo_url)
                if not demo:
                    return OralWorkResult("waiting")
                inspect_media_bytes(demo, suffix=".mp3", expected_type="audio")
                stored = storage.put_object(
                    f"materials/oral/voices/{lease.record_id}/attempt-{lease.attempt_count}-"
                    f"{lease.lease_token}/demo.mp3",
                    demo,
                    content_type="audio/mpeg",
                )
                return OralWorkResult(
                    "ready", provider_resource_id=voice_snapshot.voice, stored=stored
                )
            if voice_snapshot.status == "FAILED":
                return OralWorkResult("failed", message="声音克隆未通过")
            return OralWorkResult("waiting")
        if lease.kind == "task_poll":
            video_snapshot = vendor.video_task(str(row["vendor_task_id"]))
            if video_snapshot.status == "DONE" and video_snapshot.video_url:
                return OralWorkResult(
                    "archive",
                    provider_result_url=video_snapshot.video_url,
                    duration_sec=video_snapshot.duration,
                )
            if video_snapshot.status == "FAILED":
                return OralWorkResult("failed", message="数字人服务生成失败")
            return OralWorkResult("waiting")
        if lease.kind == "task_archive":
            result_url = str(row["provider_result_url"] or "")
            if not result_url:
                return OralWorkResult("failed", message="口播成片地址缺失")
            content = vendor.download(result_url)
            verified = inspect_media_bytes(content, suffix=".mp4", expected_type="video")
            stored = storage.put_object(
                f"generation-results/oral/{lease.record_id}/attempt-{lease.attempt_count}-"
                f"{lease.lease_token}.mp4",
                content,
                content_type="video/mp4",
            )
            return OralWorkResult("ready", stored=stored, duration_sec=verified.duration_seconds)
    except MediaValidationFailed:
        logger.warning("oral media validation failed: kind=%s", lease.kind)
        return OralWorkResult("failed", message=_invalid_media_message(lease.kind))
    except (MediaToolFailed, MediaToolUnavailable):
        logger.warning("oral media validation unavailable: kind=%s", lease.kind)
        return OralWorkResult("failed", message="媒体校验服务暂不可用，请稍后重试")
    except HiflySubmissionUncertain as exc:
        return OralWorkResult("uncertain", message=str(exc)[:500])
    except HiflyError as exc:
        if lease.kind.endswith("submit"):
            return OralWorkResult("failed", message=str(exc)[:500])
        return OralWorkResult("waiting", message=str(exc)[:500])
    except Exception as exc:  # noqa: BLE001 - storage/provider boundary
        logger.warning(
            "oral worker operation failed: kind=%s error=%s",
            lease.kind,
            type(exc).__name__,
        )
        if lease.kind == "task_archive":
            return OralWorkResult("failed", message="口播成片归档失败")
        if lease.kind.endswith("submit"):
            return OralWorkResult("uncertain", message="口播任务提交结果未知")
        return OralWorkResult("waiting")
    raise AssertionError(f"unsupported oral work kind: {lease.kind}")


def _perform_submission(
    lease: OralWorkLease,
    *,
    vendor: HiflyClient,
    storage: StorageAdapter,
) -> OralWorkResult:
    row = lease.row
    if lease.kind == "avatar_submit":
        content = _object_bytes(storage, str(row["source_storage_uri"]))
        image_source = str(row["source_kind"]) == "IMAGE"
        extension = "png" if image_source else "mp4"
        inspect_media_bytes(
            content,
            suffix=f".{extension}",
            expected_type="image" if image_source else "video",
        )
        target = vendor.create_upload_url(extension)
        vendor.upload_file(target, content)
        creator = (
            vendor.create_avatar_by_image
            if str(row["source_kind"]) == "IMAGE"
            else vendor.create_avatar_by_video
        )
        task_id = creator(title=str(row["title"])[:20], file_id=target.file_id, aigc_flag=True)
        return OralWorkResult("submitted", provider_task_id=task_id)
    if lease.kind == "voice_submit":
        content = _object_bytes(storage, str(row["source_storage_uri"]))
        suffix = Path(str(row["source_storage_uri"])).suffix.lower() or ".bin"
        normalized = normalize_audio_to_mp3(
            content,
            suffix=suffix,
            min_duration_seconds=5,
            max_duration_seconds=180,
        )
        target = vendor.create_upload_url("mp3")
        vendor.upload_file(target, normalized)
        task_id = vendor.create_voice(title=str(row["title"])[:20], file_id=target.file_id)
        return OralWorkResult("submitted", provider_task_id=task_id)
    if str(row["mode"]) == "TTS":
        task_id = vendor.create_video_by_tts(
            voice=str(row["vendor_voice_id"]),
            text=str(row["script_text"]),
            avatar=str(row["vendor_avatar_id"]),
            title=str(row["title"])[:20],
            aigc_flag=True,
            subtitle=(json.loads(str(row["subtitle_json"])) if row["subtitle_json"] else None),
        )
    else:
        content = _object_bytes(storage, str(row["audio_storage_uri"]))
        inspect_media_bytes(content, suffix=".mp3", expected_type="audio")
        target = vendor.create_upload_url("mp3")
        vendor.upload_file(target, content)
        task_id = vendor.create_video_by_audio(
            avatar=str(row["vendor_avatar_id"]),
            title=str(row["title"])[:20],
            file_id=target.file_id,
            aigc_flag=True,
        )
    return OralWorkResult("submitted", provider_task_id=task_id)


def _invalid_media_message(kind: OralWorkKind) -> str:
    if kind == "voice_poll":
        return "声音克隆试听文件无效"
    if kind == "task_archive":
        return "口播成片文件无效"
    if kind == "avatar_submit":
        return "分身素材无法解码，请重新上传"
    if kind == "voice_submit":
        return "声音素材需为 5 至 180 秒的有效音频"
    return "口播音频无法解码，请重新上传"


def prepare_oral_work(conn: BusinessConnection, lease: OralWorkLease) -> OralWorkLease:
    """Load immutable provider/storage inputs before leaving the read transaction."""
    fetched = conn.execute(
        {
            "avatar_submit": """
                    SELECT clone.*, asset.storage_uri AS source_storage_uri
                    FROM oral_avatars AS clone
                    JOIN assets AS asset ON asset.id = clone.source_asset_id
                    WHERE clone.id = %s AND clone.lease_owner = %s
                      AND clone.attempt_count = %s
                """,
            "voice_submit": """
                    SELECT clone.*, asset.storage_uri AS source_storage_uri
                    FROM oral_voices AS clone
                    JOIN assets AS asset ON asset.id = clone.source_asset_id
                    WHERE clone.id = %s AND clone.lease_owner = %s
                      AND clone.attempt_count = %s
                """,
            "task_submit": """
                    SELECT task.*, avatar.vendor_avatar_id, voice.vendor_voice_id,
                           asset.storage_uri AS audio_storage_uri
                    FROM oral_tasks AS task
                    JOIN oral_avatars AS avatar ON avatar.id = task.avatar_id
                    LEFT JOIN oral_voices AS voice ON voice.id = task.voice_id
                    LEFT JOIN assets AS asset ON asset.id = task.audio_asset_id
                    WHERE task.id = %s AND task.lease_owner = %s
                      AND task.attempt_count = %s
                """,
            "avatar_poll": (
                "SELECT * FROM oral_avatars WHERE id = %s AND lease_owner = %s "
                "AND attempt_count = %s"
            ),
            "voice_poll": (
                "SELECT * FROM oral_voices WHERE id = %s AND lease_owner = %s "
                "AND attempt_count = %s"
            ),
            "task_poll": (
                "SELECT * FROM oral_tasks WHERE id = %s AND lease_owner = %s AND attempt_count = %s"
            ),
            "task_archive": (
                "SELECT * FROM oral_tasks WHERE id = %s AND lease_owner = %s AND attempt_count = %s"
            ),
        }[lease.kind],
        (lease.record_id, lease.lease_token, lease.attempt_count),
    ).fetchone()
    if fetched is None:
        raise OralLeaseLostError("oral work lease was lost before preparation")
    row = dict(fetched)
    return OralWorkLease(
        kind=lease.kind,
        record_id=lease.record_id,
        worker_id=lease.worker_id,
        lease_token=lease.lease_token,
        attempt_count=lease.attempt_count,
        row=row,
    )


def finalize_oral_work(
    conn: BusinessConnection,
    *,
    lease: OralWorkLease,
    result: OralWorkResult,
) -> None:
    """CAS one worker result and finalize wallet state in the same transaction."""
    table = (
        "oral_avatars"
        if lease.kind.startswith("avatar")
        else "oral_voices"
        if lease.kind.startswith("voice")
        else "oral_tasks"
    )
    now = _now()
    retry_at = _iso(now + timedelta(seconds=15))
    with conn:
        if table == "oral_tasks":
            lock_shared_generation_capacity(conn)
        if lease.kind.endswith("submit"):
            if result.outcome == "submitted":
                if table == "oral_tasks":
                    sql = """
                        UPDATE oral_tasks SET status = 'RUNNING', submission_state = 'SUBMITTED',
                            provider_charge_state = 'CHARGED', vendor_task_id = %s,
                            lease_owner = NULL, lease_expires_at = NULL,
                            next_attempt_at = %s, error_message = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND status = 'SUBMITTING' AND lease_owner = %s
                          AND attempt_count = %s
                    """
                else:
                    sql = f"""
                        UPDATE {table} SET status = 'RUNNING', submission_state = 'SUBMITTED',
                            vendor_task_id = %s, lease_owner = NULL, lease_expires_at = NULL,
                            next_attempt_at = %s, error_message = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND submission_state = 'SUBMITTING' AND lease_owner = %s
                          AND attempt_count = %s
                    """  # noqa: S608 - fixed table
                cursor = conn.execute(
                    sql,
                    (
                        result.provider_task_id,
                        retry_at,
                        lease.record_id,
                        lease.lease_token,
                        lease.attempt_count,
                    ),
                )
            else:
                uncertain = result.outcome == "uncertain"
                if table == "oral_tasks":
                    status = "SUBMISSION_UNCERTAIN" if uncertain else "FAILED"
                    submission = "SUBMISSION_UNKNOWN" if uncertain else "FAILED"
                    cursor = conn.execute(
                        """
                        UPDATE oral_tasks SET status = %s, submission_state = %s,
                            provider_charge_state = %s,
                            lease_owner = NULL, lease_expires_at = NULL,
                            next_attempt_at = NULL, error_message = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND status = 'SUBMITTING' AND lease_owner = %s
                          AND attempt_count = %s
                        """,
                        (
                            status,
                            submission,
                            "UNKNOWN" if uncertain else "NOT_CHARGED",
                            result.message,
                            lease.record_id,
                            lease.lease_token,
                            lease.attempt_count,
                        ),
                    )
                else:
                    submission = "SUBMISSION_UNKNOWN" if uncertain else "FAILED"
                    status = "FAILED"
                    cursor = conn.execute(
                        f"""
                        UPDATE {table} SET status = %s, submission_state = %s,
                            lease_owner = NULL, lease_expires_at = NULL,
                            next_attempt_at = NULL, error_message = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND submission_state = 'SUBMITTING' AND lease_owner = %s
                          AND attempt_count = %s
                        """,  # noqa: S608 - fixed table
                        (
                            status,
                            submission,
                            result.message,
                            lease.record_id,
                            lease.lease_token,
                            lease.attempt_count,
                        ),
                    )
                if cursor.rowcount == 1 and table == "oral_tasks":
                    if uncertain:
                        release_oral_queue_slot(conn, oral_task_id=lease.record_id)
                    else:
                        finalize_oral_billing(conn, oral_task_id=lease.record_id)
            if cursor.rowcount != 1:
                raise OralLeaseLostError("oral submission lease was lost")
            if table != "oral_tasks" and result.outcome != "submitted":
                from app.usage_billing import finish_source

                finish_source(conn, lease.record_id, units=0, succeeded=False)
            return

        if lease.kind in {"avatar_poll", "voice_poll"}:
            if result.outcome == "waiting":
                cursor = conn.execute(
                    f"""
                    UPDATE {table} SET lease_owner = NULL, lease_expires_at = NULL,
                        next_attempt_at = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'RUNNING' AND lease_owner = %s
                      AND attempt_count = %s
                    """,  # noqa: S608
                    (retry_at, lease.record_id, lease.lease_token, lease.attempt_count),
                )
            elif result.outcome == "failed":
                cursor = conn.execute(
                    f"""
                    UPDATE {table} SET status = 'FAILED', submission_state = 'FAILED',
                        lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = NULL,
                        error_message = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'RUNNING' AND lease_owner = %s
                      AND attempt_count = %s
                    """,  # noqa: S608
                    (
                        result.message,
                        lease.record_id,
                        lease.lease_token,
                        lease.attempt_count,
                    ),
                )
            elif lease.kind == "avatar_poll":
                cursor = conn.execute(
                    """
                    UPDATE oral_avatars SET status = 'READY', vendor_avatar_id = %s,
                        lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = NULL,
                        error_message = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'RUNNING' AND lease_owner = %s
                      AND attempt_count = %s
                    """,
                    (
                        result.provider_resource_id,
                        lease.record_id,
                        lease.lease_token,
                        lease.attempt_count,
                    ),
                )
            else:
                if result.stored is None:
                    raise ValueError("voice ready result requires archived demo")
                asset_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO assets (
                        id, project_id, kind, storage_uri, sha256, size_bytes,
                        content_type, created_by_user_id
                    ) VALUES (%s, NULL, 'oral_audio', %s, %s, %s, 'audio/mpeg', %s)
                    """,
                    (
                        asset_id,
                        result.stored.uri,
                        result.stored.sha256,
                        result.stored.size,
                        lease.row["owner_user_id"],
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE oral_voices SET status = 'READY', vendor_voice_id = %s,
                        demo_asset_id = %s, lease_owner = NULL, lease_expires_at = NULL,
                        next_attempt_at = NULL, error_message = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'RUNNING' AND lease_owner = %s
                      AND attempt_count = %s
                    """,
                    (
                        result.provider_resource_id,
                        asset_id,
                        lease.record_id,
                        lease.lease_token,
                        lease.attempt_count,
                    ),
                )
            if cursor.rowcount != 1:
                raise OralLeaseLostError("oral clone lease was lost")
            if result.outcome != "waiting":
                from app.usage_billing import finish_source

                finish_source(conn, lease.record_id, units=1, succeeded=result.outcome != "failed")
            return

        if lease.kind == "task_poll":
            if result.outcome == "archive":
                cursor = conn.execute(
                    """
                    UPDATE oral_tasks SET status = 'ARCHIVING', provider_result_url = %s,
                        duration_sec = %s, lease_owner = NULL, lease_expires_at = NULL,
                        next_attempt_at = %s, error_message = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'RUNNING' AND lease_owner = %s
                      AND attempt_count = %s
                    """,
                    (
                        result.provider_result_url,
                        result.duration_sec,
                        _iso(now),
                        lease.record_id,
                        lease.lease_token,
                        lease.attempt_count,
                    ),
                )
            elif result.outcome == "failed":
                cursor = conn.execute(
                    """
                    UPDATE oral_tasks SET status = 'FAILED', lease_owner = NULL,
                        lease_expires_at = NULL, next_attempt_at = NULL, error_message = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'RUNNING' AND lease_owner = %s
                      AND attempt_count = %s
                    """,
                    (
                        result.message,
                        lease.record_id,
                        lease.lease_token,
                        lease.attempt_count,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE oral_tasks SET lease_owner = NULL, lease_expires_at = NULL,
                        next_attempt_at = %s, error_message = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'RUNNING' AND lease_owner = %s
                      AND attempt_count = %s
                    """,
                    (
                        retry_at,
                        result.message,
                        lease.record_id,
                        lease.lease_token,
                        lease.attempt_count,
                    ),
                )
            if cursor.rowcount != 1:
                raise OralLeaseLostError("oral poll lease was lost")
            if result.outcome == "failed":
                finalize_oral_billing(conn, oral_task_id=lease.record_id)
            return

        if result.outcome == "ready" and result.stored is not None:
            asset_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256, size_bytes,
                    content_type, created_by_user_id
                ) VALUES (%s, NULL, 'oral_video', %s, %s, %s, 'video/mp4', %s)
                """,
                (
                    asset_id,
                    result.stored.uri,
                    result.stored.sha256,
                    result.stored.size,
                    lease.row["owner_user_id"],
                ),
            )
            cursor = conn.execute(
                """
                UPDATE oral_tasks SET status = 'SUCCEEDED', result_asset_id = %s, duration_sec = %s,
                    lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = NULL,
                    error_message = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'ARCHIVING' AND lease_owner = %s
                  AND attempt_count = %s
                """,
                (
                    asset_id,
                    result.duration_sec,
                    lease.record_id,
                    lease.lease_token,
                    lease.attempt_count,
                ),
            )
            if cursor.rowcount == 1:
                from app.usage_billing import complete_source_attempt

                complete_source_attempt(conn, lease.record_id, usage=result.duration_sec)
                finalize_oral_billing(conn, oral_task_id=lease.record_id)
        else:
            cursor = conn.execute(
                """
                UPDATE oral_tasks SET status = 'ARCHIVE_FAILED', lease_owner = NULL,
                    lease_expires_at = NULL, next_attempt_at = NULL, error_message = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'ARCHIVING' AND lease_owner = %s
                  AND attempt_count = %s
                """,
                (
                    result.message,
                    lease.record_id,
                    lease.lease_token,
                    lease.attempt_count,
                ),
            )
        if cursor.rowcount != 1:
            raise OralLeaseLostError("oral archive lease was lost")
        if result.outcome != "ready":
            release_oral_queue_slot(conn, oral_task_id=lease.record_id)


def discard_uncommitted_oral_asset(
    storage: StorageAdapter,
    *,
    result: OralWorkResult,
    actor_id: str | None,
) -> None:
    """Best-effort cleanup when a fenced finalize loses its database lease."""
    if result.stored is None:
        return
    try:
        content_store.delete_object_outside_content_namespace(
            storage, result.stored.key, actor_id=actor_id
        )
    except Exception:  # noqa: BLE001 - do not hide the original fencing failure
        logger.warning("failed to clean up uncommitted oral worker asset")


def request_oral_archive_retry(
    conn: BusinessConnection,
    *,
    task_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    with conn:
        if conn.is_postgres:
            runtime = read_runtime_limits(conn)
            if not shared_generation_capacity_available(
                conn,
                max_concurrent_tasks=runtime["max_concurrent_h3_tasks"],
            ):
                raise ValueError("shared generation capacity is full")
            ensure_user_queue_cursor(conn, user_id=owner_user_id)
            cursor_row = conn.execute(
                """
                SELECT running_tasks_count FROM user_queue_cursors
                WHERE user_id = %s
                FOR UPDATE
                """,
                (owner_user_id,),
            ).fetchone()
            active_for_owner = conn.execute(
                """
                SELECT (
                    SELECT COUNT(*) FROM oral_tasks
                    WHERE owner_user_id = %s
                      AND status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
                ) + (
                    SELECT COUNT(*) FROM generation_tasks AS task
                    JOIN generation_batches AS batch ON batch.id = task.batch_id
                    WHERE batch.created_by_user_id = %s
                      AND task.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
                )
                """,
                (owner_user_id, owner_user_id),
            ).fetchone()[0]
            if cursor_row is None or int(cursor_row["running_tasks_count"]) != 0:
                raise ValueError("owner queue slot is busy")
            if int(active_for_owner) != 0:
                raise ValueError("owner already has active generation work")
        cursor = conn.execute(
            """
            UPDATE oral_tasks SET status = 'ARCHIVING', next_attempt_at = %s,
                queue_slot_acquired = %s, error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_user_id = %s AND status = 'ARCHIVE_FAILED'
              AND provider_result_url IS NOT NULL
            """,
            (_iso(_now()), 1 if conn.is_postgres else 0, task_id, owner_user_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("only an archive-failed oral task with a result URL can retry")
        if conn.is_postgres:
            acquired = conn.execute(
                """
                UPDATE user_queue_cursors
                SET running_tasks_count = running_tasks_count + 1,
                    last_dispatched_at = now()
                WHERE user_id = %s AND running_tasks_count = 0
                """,
                (owner_user_id,),
            )
            if acquired.rowcount != 1:
                raise RuntimeError("oral archive retry queue slot was lost")
    row = conn.execute(
        "SELECT * FROM oral_tasks WHERE id = %s AND owner_user_id = %s",
        (task_id, owner_user_id),
    ).fetchone()
    return dict(row)
