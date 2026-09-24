"""工作台"提取文案"链路：上传视频 → 抽音轨 → ASR 转写（异步任务）。

任务编排使用 PENDING→RUNNING→终态、租约与提交不确定门禁。首次执行：
存储取原视频 → ffmpeg 抽音轨 → 登记并上传临时对象 → ASR 转写。异步回执
即刻入库，恢复时继续查询原任务；成功或明确失败后删除临时对象，清理失败
由 Worker 复删。无回执的不确定任务保留音频至签名 URL 过期。转写全文放在
任务 result_json——原始上传
没有镜头卡版本，无法写生成门禁管制的 ``/projects/{id}/scripts``，文案
工坊从任务结果取文本回填草稿，终稿发布仍走唯一的显式脚本版本路径。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import content_store
from app.asr import (
    AsrProvider,
    AsrProviderError,
    AsrSubmissionUncertain,
    AsrTaskPending,
    DashScopeFunAsr,
    TranscriptResult,
    get_asr_provider,
)
from app.db_portable import BusinessConnection
from app.media_tools import (
    MediaToolFailed,
    MediaToolUnavailable,
    extract_audio,
    probe_duration_seconds,
    resolve_media_binary,
)
from app.permissions import require_not_auditor, require_project_access
from app.storage import StorageAdapter

logger = logging.getLogger("app.script_from_audio")

SCRIPT_FROM_AUDIO_TASK_LEASE_MINUTES = 20
SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES = 2_000_000_000
_TRANSCRIPT_LOOKUP_CHUNK = 100
_DOWNLOAD_INTENT_EXPIRES = timedelta(minutes=30)


class ScriptFromAudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_asset_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ScriptFromAudioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    duration_sec: float | None = None
    language: str | None = None


class ScriptFromAudioTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    source_asset_id: str
    status: str
    attempt: int
    result: ScriptFromAudioResult | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


def script_from_audio_task_response(row: sqlite3.Row) -> ScriptFromAudioTaskResponse:
    result_json = row["result_json"]
    result: ScriptFromAudioResult | None = None
    if result_json is not None:
        payload = json.loads(str(result_json))
        result = ScriptFromAudioResult(
            text=str(payload.get("text", "")),
            duration_sec=(
                None if payload.get("duration_sec") is None else float(payload["duration_sec"])
            ),
            language=(None if payload.get("language") is None else str(payload["language"])),
        )
    return ScriptFromAudioTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        source_asset_id=str(row["source_asset_id"]),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        result=result,
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=(
            None if row["error_message_redacted"] is None else str(row["error_message_redacted"])
        ),
        retryable=bool(row["retryable"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        completed_at=(None if row["completed_at"] is None else str(row["completed_at"])),
    )


@dataclass(frozen=True)
class ScriptFromAudioTaskLease:
    id: str
    project_id: str
    created_by_user_id: str
    worker_id: str
    attempt: int


@dataclass
class PreparedScriptFromAudio:
    task_id: str
    project_id: str
    asset_id: str
    object_key: str
    storage: StorageAdapter
    asr: AsrProvider | None
    ffmpeg_path: str
    ffprobe_path: str | None
    audio_object_key: str
    provider_task_id: str | None = None
    audio_deleted: bool = False
    cached_result: TranscriptResult | None = None


class ScriptCachePending(Exception):
    """Another durable task owns this video's one upstream transcription."""


def _viral_copy_upload(asset: sqlite3.Row) -> bool:
    try:
        metadata = json.loads(str(asset["metadata_json"] or "{}"))
    except (ValueError, TypeError):
        return False
    return metadata.get("viral_copy_upload") is True


def _viral_source(conn: BusinessConnection, asset: sqlite3.Row) -> tuple[str, str] | None:
    # Only server-verified imports can participate in cross-user reuse. User
    # supplied asset metadata is not proof of a public video's identity.
    row = conn.execute(
        "SELECT platform,video_id FROM viral_import_tasks WHERE source_asset_id=%s "
        "AND project_id=%s AND status='SUCCEEDED' ORDER BY created_at,id LIMIT 1",
        (asset["id"], asset["project_id"]),
    ).fetchone()
    if row is None:
        return None
    if asset["kind"] == "reference_video":
        return str(row["platform"]), str(row["video_id"])
    # 客户端本地抽音轨上传（决策 #15）：身份仍由服务端在内容池里按
    # platform+videoId 校验后写进 viral_import_tasks，音轨取自客户端缓存的
    # **整支视频**。解析器下发的独立音轨可能是背景音乐，所以只有本端点写下的
    # 标记能放行其余 reference_audio——标记写在资产上，而该资产由服务端创建，
    # 客户端无法自证。
    if asset["kind"] == "reference_audio" and _viral_copy_upload(asset):
        return str(row["platform"]), str(row["video_id"])
    return None


def _cached_transcript(raw: object) -> TranscriptResult | None:
    if raw is None:
        return None
    try:
        value = json.loads(str(raw))
        text = value["text"]
        duration = float(value["duration_sec"])
        if (
            not isinstance(text, str)
            or not text.strip()
            or not math.isfinite(duration)
            or duration <= 0
        ):
            return None
        return TranscriptResult(text, duration, value.get("language"))
    except (ValueError, TypeError, KeyError):
        return None


@dataclass(frozen=True)
class CachedTranscriptHit:
    """共享文案缓存命中：转写结果 + 最近写入时间（GET /api/viral/search/copy 的 updatedAt）."""

    result: TranscriptResult
    updated_at: str


def _iso_updated_at(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def cached_transcript(
    conn: BusinessConnection, *, platform: str, video_id: str
) -> CachedTranscriptHit | None:
    """只读查询共享文案缓存；未命中或内容非法返回 None. 不加锁、不转写、不计费."""
    row = conn.execute(
        "SELECT result_json, updated_at FROM viral_script_cache WHERE platform=%s AND video_id=%s",
        (platform, video_id),
    ).fetchone()
    if row is None:
        return None
    result = _cached_transcript(row["result_json"])
    if result is None:
        return None
    return CachedTranscriptHit(result=result, updated_at=_iso_updated_at(row["updated_at"]))


def cached_transcripts(
    conn: BusinessConnection, refs: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], CachedTranscriptHit]:
    """批量只读查询（搜索结果回填 hasCopy）；去重后分块 OR 查询，未命中不入结果."""
    unique = list(dict.fromkeys(refs))
    hits: dict[tuple[str, str], CachedTranscriptHit] = {}
    for start in range(0, len(unique), _TRANSCRIPT_LOOKUP_CHUNK):
        chunk = unique[start : start + _TRANSCRIPT_LOOKUP_CHUNK]
        clause = " OR ".join(["(platform=%s AND video_id=%s)"] * len(chunk))
        params: list[str] = []
        for platform, video_id in chunk:
            params.extend((platform, video_id))
        rows = conn.execute(
            f"SELECT platform, video_id, result_json, updated_at FROM viral_script_cache "
            f"WHERE {clause}",  # noqa: S608 - 占位符模板拼接，值仍走参数化
            tuple(params),
        ).fetchall()
        for row in rows:
            result = _cached_transcript(row["result_json"])
            if result is None:
                continue
            hits[(str(row["platform"]), str(row["video_id"]))] = CachedTranscriptHit(
                result=result, updated_at=_iso_updated_at(row["updated_at"])
            )
    return hits


def _historical_transcripts(
    conn: BusinessConnection, source: tuple[str, str], *, exclude: str | None = None
) -> list[sqlite3.Row]:
    # One snapshot for successes and in-flight work: a legacy worker completing
    # between two separate queries must not become invisible to both queries.
    return cast(
        list[sqlite3.Row],
        conn.execute(
            "SELECT t.id,t.status,t.provider_started_at,t.result_json "
            "FROM script_from_audio_tasks t "
            "JOIN viral_import_tasks i ON i.source_asset_id=t.source_asset_id "
            "AND i.project_id=t.project_id JOIN assets a ON a.id=t.source_asset_id "
            "WHERE i.platform=%s AND i.video_id=%s AND i.status='SUCCEEDED' "
            "AND a.kind='reference_video' AND (t.status IN "
            "('PENDING','RUNNING','SUBMISSION_UNCERTAIN','SUCCEEDED') "
            "OR t.provider_started_at IS NOT NULL) "
            "AND (%s::text IS NULL OR (t.id<>%s AND NOT (t.request_json::jsonb ? 'viral_source'))) "
            "ORDER BY CASE WHEN t.provider_started_at IS NOT NULL "
            "OR t.status='SUBMISSION_UNCERTAIN' THEN 0 ELSE 1 END,t.created_at,t.id",
            (*source, exclude, exclude),
        ).fetchall(),
    )


def _lock_script_cache(conn: BusinessConnection, source: tuple[str, str]) -> sqlite3.Row:
    inserted = conn.execute(
        "INSERT INTO viral_script_cache(platform,video_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        source,
    )
    cache = cast(
        sqlite3.Row,
        conn.execute(
            "SELECT * FROM viral_script_cache WHERE platform=%s AND video_id=%s FOR UPDATE",
            source,
        ).fetchone(),
    )
    producer = conn.execute(
        "SELECT status,provider_started_at FROM script_from_audio_tasks WHERE id=%s",
        (cache["producer_task_id"],),
    ).fetchone()
    if cache["result_json"] is None and (
        inserted.rowcount == 1
        or (
            producer is not None
            and producer["status"] == "FAILED"
            and producer["provider_started_at"] is None
        )
    ):
        previous = _historical_transcripts(
            conn, source, exclude=None if inserted.rowcount == 1 else str(cache["producer_task_id"])
        )
        chosen = next(
            (
                old
                for old in previous
                if old["status"] == "SUCCEEDED"
                and _cached_transcript(old["result_json"]) is not None
            ),
            None,
        )
        if chosen is None:
            chosen = next((old for old in previous if old["status"] != "SUCCEEDED"), None)
        if chosen is not None:
            cache = cast(
                sqlite3.Row,
                conn.execute(
                    "UPDATE viral_script_cache SET producer_task_id=%s,result_json=%s,"
                    "updated_at=now() "
                    "WHERE platform=%s AND video_id=%s RETURNING *",
                    (
                        chosen["id"],
                        chosen["result_json"] if chosen["status"] == "SUCCEEDED" else None,
                        *source,
                    ),
                ).fetchone(),
            )
    if cache["result_json"] is None and cache["producer_task_id"] is not None:
        previous_result = conn.execute(
            "SELECT result_json FROM script_from_audio_tasks WHERE id=%s AND status='SUCCEEDED'",
            (cache["producer_task_id"],),
        ).fetchone()
        if previous_result is not None and _cached_transcript(previous_result["result_json"]):
            cache = cast(
                sqlite3.Row,
                conn.execute(
                    "UPDATE viral_script_cache SET result_json=%s,updated_at=now() "
                    "WHERE platform=%s AND video_id=%s RETURNING *",
                    (previous_result["result_json"], *source),
                ).fetchone(),
            )
    return cache


def _cache_producer_state(conn: BusinessConnection, cache: sqlite3.Row) -> str:
    if cache["producer_task_id"] is None:
        return "AVAILABLE"
    producer = conn.execute(
        "SELECT status,provider_started_at FROM script_from_audio_tasks WHERE id=%s",
        (cache["producer_task_id"],),
    ).fetchone()
    # A missing producer may have been deleted while a paid request was in
    # flight. Its disappearance must never authorize another submission.
    if producer is None:
        return "UNCERTAIN"
    if producer["status"] == "FAILED" and producer["provider_started_at"] is None:
        if any(
            old["status"] != "SUCCEEDED" or _cached_transcript(old["result_json"])
            for old in _historical_transcripts(
                conn,
                (str(cache["platform"]), str(cache["video_id"])),
                exclude=str(cache["producer_task_id"]),
            )
        ):
            # Rebind on the next locked lookup if the producer failed after the
            # lookup above. Never overlook another pre-upgrade in-flight task.
            return "WAITING"
        return "AVAILABLE"
    if producer["status"] in {"PENDING", "RUNNING"}:
        return "WAITING"
    return "UNCERTAIN"


def _cache_uncertain() -> Exception:
    return script_from_audio_error(
        409,
        "SCRIPT_FROM_AUDIO_CACHE_UNCERTAIN",
        "该视频上次转写结果尚未确认，请联系管理员核实后再提取。",
    )


def script_from_audio_error(status_code: int, code: str, message: str) -> Exception:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def enqueue_script_from_audio_task(
    conn: BusinessConnection,
    *,
    actor: object,
    project_id: str,
    source_asset_id: str,
    idempotency_key: str,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,  # type: ignore[arg-type]
        action="project.script_from_audio",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,  # type: ignore[arg-type]
        project_id=project_id,
        action="project.script_from_audio",
    )
    asset = conn.execute(
        "SELECT * FROM assets WHERE id = %s AND project_id = %s",
        (source_asset_id, project_id),
    ).fetchone()
    if asset is None:
        raise script_from_audio_error(
            404,
            "SCRIPT_FROM_AUDIO_SOURCE_ASSET_MISSING",
            "来源视频不存在或已删除，请重新上传。",
        )
    _validate_source(asset)
    viral_source = _viral_source(conn, asset)
    request_payload: dict[str, object] = {"source_asset_id": source_asset_id}
    request_hash = hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    replay = conn.execute(
        """
        SELECT * FROM script_from_audio_tasks
        WHERE project_id = %s AND idempotency_key = %s
        """,
        (project_id, idempotency_key),
    ).fetchone()
    if replay is not None:
        if str(replay["request_hash"]) != request_hash:
            raise script_from_audio_error(
                409,
                "SCRIPT_FROM_AUDIO_IDEMPOTENCY_CONFLICT",
                "提取内容已经变化，请重新提交。",
            )
        return cast(sqlite3.Row, replay)
    active = conn.execute(
        """
        SELECT * FROM script_from_audio_tasks
        WHERE project_id = %s AND status IN ('PENDING','RUNNING')
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if active is not None:
        if str(active["request_hash"]) != request_hash or viral_source is not None:
            raise script_from_audio_error(
                409,
                "SCRIPT_FROM_AUDIO_ALREADY_RUNNING",
                "该项目已有文案提取任务在进行，请等待完成。",
            )
        return cast(sqlite3.Row, active)

    cache = _lock_script_cache(conn, viral_source) if viral_source else None
    cached = _cached_transcript(cache["result_json"]) if cache is not None else None
    if cache is not None:
        if cached is None and _cache_producer_state(conn, cache) == "UNCERTAIN":
            raise _cache_uncertain()
        request_payload["viral_source"] = [cache["platform"], cache["video_id"]]
    if cached is None and (cache is None or _cache_producer_state(conn, cache) == "AVAILABLE"):
        _configured_asr(conn)

    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO script_from_audio_tasks (
            id, project_id, source_asset_id, created_by_user_id,
            idempotency_key, request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT DO NOTHING
        """,
        (
            task_id,
            project_id,
            source_asset_id,
            actor.id,  # type: ignore[attr-defined]
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM script_from_audio_tasks WHERE project_id=%s AND idempotency_key=%s",
            (project_id, idempotency_key),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT * FROM script_from_audio_tasks
            WHERE project_id = %s AND status IN ('PENDING','RUNNING')
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    if row is None:
        raise script_from_audio_error(
            409,
            "SCRIPT_FROM_AUDIO_ENQUEUE_CONFLICT",
            "提取任务状态已经变化，请重试。",
        )
    if str(row["request_hash"]) != request_hash:
        raise script_from_audio_error(
            409, "SCRIPT_FROM_AUDIO_ENQUEUE_CONFLICT", "该项目已有不同来源的提取任务，请等待完成。"
        )
    if viral_source is not None and str(row["idempotency_key"]) != idempotency_key:
        raise script_from_audio_error(
            409, "SCRIPT_FROM_AUDIO_ALREADY_RUNNING", "该项目已有文案提取任务在进行，请等待完成。"
        )
    if str(row["id"]) != task_id:
        return cast(sqlite3.Row, row)
    from app.permissions import write_audit
    from app.usage_billing import accept_operation

    metadata = json.loads(str(asset["metadata_json"] or "{}"))
    duration = (
        cast(float, cached.duration_sec)
        if cached
        else (metadata.get("duration_seconds") or metadata.get("duration_sec") or 0)
    )
    accept_operation(
        conn,
        user_id=actor.id,  # type: ignore[attr-defined]
        service="asr",
        source_id=str(row["id"]),
        units=duration,
    )
    if cache is not None and viral_source is not None:
        if cached is not None:
            from app.usage_billing import finish_source

            conn.execute(
                "UPDATE script_from_audio_tasks SET status='SUCCEEDED',result_json=%s,"
                "completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                (cache["result_json"], row["id"]),
            )
            finish_source(conn, str(row["id"]), units=duration, succeeded=True)
            row = conn.execute(
                "SELECT * FROM script_from_audio_tasks WHERE id=%s", (row["id"],)
            ).fetchone()
        elif _cache_producer_state(conn, cache) == "AVAILABLE":
            conn.execute(
                "UPDATE viral_script_cache SET producer_task_id=%s,updated_at=now() "
                "WHERE platform=%s AND video_id=%s",
                (row["id"], *viral_source),
            )
    write_audit(
        conn,
        actor=actor,  # type: ignore[arg-type]
        action="project.script_from_audio_enqueued",
        entity_type="script_from_audio_task",
        entity_id=str(row["id"]),
        metadata={
            "project_id": project_id,
            "request_hash": request_hash,
            "cache_hit": cached is not None,
        },
    )
    return cast(sqlite3.Row, row)


def acquire_script_from_audio_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> ScriptFromAudioTaskLease | None:
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(
        datetime.now(UTC) + timedelta(minutes=SCRIPT_FROM_AUDIO_TASK_LEASE_MINUTES)
    )
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'PENDING', locked_by = NULL, locked_until = NULL,
            error_code = NULL, error_message_redacted = NULL, retryable = 0,
            updated_at = %s
        WHERE status = 'RUNNING' AND (provider_started_at IS NULL OR provider_task_id IS NOT NULL)
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'SUBMISSION_UNCERTAIN', locked_by = NULL, locked_until = NULL,
            error_code = 'SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN',
            error_message_redacted = %s, retryable = 0,
            completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND provider_started_at IS NOT NULL AND provider_task_id IS NULL
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (
            "语音转写请求可能已经送达服务商，请人工确认后再决定是否重试。",
            now,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM script_from_audio_tasks
            WHERE status = 'PENDING' AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return ScriptFromAudioTaskLease(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
    )


def _require_leased_task(conn: BusinessConnection, lease: ScriptFromAudioTaskLease) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id = %s AND status = 'RUNNING'",
        (lease.id,),
    ).fetchone()
    if (
        row is None
        or str(row["locked_by"]) != lease.worker_id
        or int(row["attempt"]) != lease.attempt
        or str(row["locked_until"] or "") <= _time_text(datetime.now(UTC))
    ):
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    return cast(sqlite3.Row, row)


def _configured_asr(conn: BusinessConnection) -> AsrProvider:
    try:
        return get_asr_provider(conn)
    except (AsrProviderError, ValueError) as exc:
        logger.warning("ASR configuration unavailable: %s", type(exc).__name__)
        raise script_from_audio_error(
            503,
            "SCRIPT_FROM_AUDIO_SERVICE_UNAVAILABLE",
            "语音转写服务暂不可用，请联系管理员检查配置。",
        ) from exc


def _validate_source(asset: sqlite3.Row) -> None:
    if not str(asset["content_type"] or "").lower().startswith(("audio/", "video/")):
        raise script_from_audio_error(
            422, "SCRIPT_FROM_AUDIO_SOURCE_TYPE_INVALID", "请选择有效的视频或音频文件。"
        )
    if int(asset["size_bytes"] or 0) > SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES:
        raise script_from_audio_error(
            413, "SCRIPT_FROM_AUDIO_SOURCE_TOO_LARGE", "来源文件过大，请压缩后重新上传。"
        )


def prepare_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    storage: StorageAdapter,
) -> PreparedScriptFromAudio:
    row = _require_leased_task(conn, lease)
    payload = json.loads(str(row["request_json"]))
    asset_id = str(payload.get("source_asset_id", ""))
    source = payload.get("viral_source")
    if source is not None:
        cache = _lock_script_cache(conn, (str(source[0]), str(source[1])))
        cached = _cached_transcript(cache["result_json"])
        if cached is not None:
            conn.commit()
            return PreparedScriptFromAudio(
                task_id=lease.id,
                project_id=lease.project_id,
                asset_id=asset_id,
                object_key="",
                storage=storage,
                asr=None,
                ffmpeg_path="",
                ffprobe_path=None,
                audio_object_key="",
                cached_result=cached,
            )
        if cache["producer_task_id"] != lease.id:
            state = _cache_producer_state(conn, cache)
            if state == "WAITING":
                raise ScriptCachePending()
            if state != "AVAILABLE":
                raise _cache_uncertain()
            conn.execute(
                "UPDATE viral_script_cache SET producer_task_id=%s,updated_at=now() "
                "WHERE platform=%s AND video_id=%s",
                (lease.id, *source),
            )
        conn.commit()
    if row["provider_task_id"] is not None:
        return PreparedScriptFromAudio(
            task_id=lease.id,
            project_id=lease.project_id,
            asset_id=asset_id,
            object_key="",
            storage=storage,
            asr=_configured_asr(conn),
            ffmpeg_path="",
            ffprobe_path=None,
            audio_object_key=str(row["audio_object_key"] or ""),
            provider_task_id=str(row["provider_task_id"]),
        )
    asset = conn.execute(
        "SELECT * FROM assets WHERE id = %s AND project_id = %s",
        (asset_id, lease.project_id),
    ).fetchone()
    if asset is None:
        raise script_from_audio_error(
            404,
            "SCRIPT_FROM_AUDIO_SOURCE_ASSET_MISSING",
            "来源视频不存在或已删除，请重新上传。",
        )
    _validate_source(asset)
    storage_uri = str(asset["storage_uri"])
    object_key = _object_key_from_uri(storage_uri)
    try:
        ffmpeg_path = resolve_media_binary("ffmpeg")
        ffprobe_path = resolve_media_binary("ffprobe")
    except MediaToolUnavailable as exc:
        raise script_from_audio_error(
            503,
            "SCRIPT_FROM_AUDIO_MEDIA_TOOL_MISSING",
            str(exc),
        ) from exc
    audio_key = str(row["audio_object_key"] or f"projects/{lease.project_id}/asr/{lease.id}.m4a")
    conn.execute(
        "UPDATE script_from_audio_tasks SET audio_object_key=%s WHERE id=%s "
        "AND status='RUNNING' AND locked_by=%s AND attempt=%s",
        (audio_key, lease.id, lease.worker_id, lease.attempt),
    )
    conn.commit()
    return PreparedScriptFromAudio(
        task_id=lease.id,
        project_id=lease.project_id,
        asset_id=asset_id,
        object_key=object_key,
        storage=storage,
        asr=_configured_asr(conn),
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        audio_object_key=audio_key,
        provider_task_id=None if row["provider_task_id"] is None else str(row["provider_task_id"]),
    )


def mark_script_from_audio_submission_started(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
) -> None:
    updated = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET provider_started_at = %s, updated_at = %s
        WHERE id = %s AND status='RUNNING' AND locked_by=%s AND attempt=%s
          AND locked_until > %s
        """,
        (
            _time_text(datetime.now(UTC)),
            _time_text(datetime.now(UTC)),
            lease.id,
            lease.worker_id,
            lease.attempt,
            _time_text(datetime.now(UTC)),
        ),
    )
    if updated.rowcount != 1:
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    from app.usage_billing import begin_source_attempt

    begin_source_attempt(conn, lease.id)
    conn.commit()


def checkpoint_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    provider_task_id: str | None = None,
) -> None:
    now = datetime.now(UTC)
    updated = conn.execute(
        "UPDATE script_from_audio_tasks SET provider_task_id=COALESCE(%s,provider_task_id), "
        "locked_until=%s, updated_at=%s WHERE id=%s AND status='RUNNING' "
        "AND locked_by=%s AND attempt=%s AND locked_until>%s",
        (
            provider_task_id,
            _time_text(now + timedelta(minutes=SCRIPT_FROM_AUDIO_TASK_LEASE_MINUTES)),
            _time_text(now),
            lease.id,
            lease.worker_id,
            lease.attempt,
            _time_text(now),
        ),
    )
    if updated.rowcount != 1:
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    conn.commit()


def perform_script_from_audio_task(
    work: PreparedScriptFromAudio,
    *,
    before_provider_call: Callable[[], None] | None = None,
    on_submitted: Callable[[str], None] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> TranscriptResult:
    """Known receipts resume without another upload/POST; pending work retains its audio."""
    if work.cached_result is not None:
        return work.cached_result
    if work.asr is None:
        raise AsrProviderError("转写服务暂不可用。")
    keep_audio = False
    try:
        if heartbeat is not None:
            heartbeat()
        if work.provider_task_id is not None:
            if not isinstance(work.asr, DashScopeFunAsr):
                raise AsrProviderError("当前转写配置无法恢复已有任务，请检查配置。")
            return work.asr.resume(work.provider_task_id, heartbeat=heartbeat)
        metadata = work.storage.head_object(work.object_key)
        if metadata is not None and metadata.size > SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES:
            raise script_from_audio_error(
                413, "SCRIPT_FROM_AUDIO_SOURCE_TOO_LARGE", "来源文件过大，请压缩后重新上传。"
            )
        video_bytes = work.storage.get_object(work.object_key)
        if len(video_bytes) > SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES:
            raise script_from_audio_error(
                413, "SCRIPT_FROM_AUDIO_SOURCE_TOO_LARGE", "来源文件过大，请压缩后重新上传。"
            )
        with tempfile.TemporaryDirectory(prefix="script-from-audio-") as tmp_dir:
            video_path = Path(tmp_dir) / "source-video"
            audio_path = Path(tmp_dir) / "extracted-audio.m4a"
            video_path.write_bytes(video_bytes)
            try:
                extract_audio(work.ffmpeg_path, video_path, audio_path)
                audio_bytes = audio_path.read_bytes()
            except MediaToolFailed as exc:
                raise AsrProviderError(f"音轨抽取失败：{exc}") from exc
            duration = (
                probe_duration_seconds(work.ffprobe_path, audio_path) if work.ffprobe_path else None
            )
            if heartbeat is not None:
                heartbeat()
            work.storage.put_object(work.audio_object_key, audio_bytes, content_type="audio/mp4")
            intent = work.storage.create_download_intent(
                work.audio_object_key,
                expires_in=_DOWNLOAD_INTENT_EXPIRES,
                can_read=True,
            )
            audio_input = intent.url
            if isinstance(work.asr, DashScopeFunAsr):
                audio_input = work.asr.prepare_audio_input(
                    audio_input, audio_bytes=audio_bytes, duration_sec=duration
                )
            if before_provider_call is not None:
                before_provider_call()
            if isinstance(work.asr, DashScopeFunAsr):
                return work.asr.transcribe(
                    audio_input,
                    duration_sec=duration,
                    on_submitted=on_submitted,
                    heartbeat=heartbeat,
                )
            return work.asr.transcribe(intent.url, duration_sec=duration)
    except (AsrTaskPending, AsrSubmissionUncertain):
        keep_audio = True
        raise
    finally:
        if not keep_audio and work.audio_object_key:
            try:
                # A superseded worker must not delete the current attempt's input.
                if heartbeat is not None:
                    heartbeat()
                content_store.delete_object_outside_content_namespace(
                    work.storage,
                    work.audio_object_key,
                    actor_id="script-from-audio-worker",
                )
                work.audio_deleted = True
            except Exception:
                logger.warning("temporary ASR audio cleanup deferred for task %s", work.task_id)


def complete_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    result: TranscriptResult,
    audio_deleted: bool = False,
) -> None:
    now = _time_text(datetime.now(UTC))
    result_payload = {
        "text": result.text,
        "duration_sec": result.duration_sec,
        "language": result.language,
    }
    row = _require_leased_task(conn, lease)
    source = json.loads(str(row["request_json"])).get("viral_source")
    result_json = json.dumps(result_payload, ensure_ascii=False, sort_keys=True)
    if source is not None and _cached_transcript(result_json) is None:
        raise AsrProviderError("转写结果缺少有效文案或时长，请联系管理员核实。")
    updated = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'SUCCEEDED', result_json = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0,
            completed_at = %s, updated_at = %s, locked_by = NULL,
            locked_until = NULL, provider_started_at = NULL,
            audio_object_key = CASE WHEN %s=1 THEN NULL ELSE audio_object_key END
        WHERE id = %s AND status='RUNNING' AND locked_by=%s AND attempt=%s AND locked_until>%s
        """,
        (
            result_json,
            now,
            now,
            int(audio_deleted),
            lease.id,
            lease.worker_id,
            lease.attempt,
            now,
        ),
    )
    if updated.rowcount != 1:
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    if source is not None:
        conn.execute(
            "UPDATE viral_script_cache SET result_json=%s,updated_at=now() "
            "WHERE platform=%s AND video_id=%s AND producer_task_id=%s AND result_json IS NULL",
            (result_json, *source, lease.id),
        )
    from app.usage_billing import complete_source_attempt, finish_source

    complete_source_attempt(conn, lease.id, usage=result.duration_sec)
    if result.duration_sec is not None:
        finish_source(conn, lease.id, units=result.duration_sec, succeeded=True)
    else:
        # 供应商没回时长的成功结果按预留预算结算：文案已经交付，绝不能
        # 把计费单留在 PENDING（交付未扣费，且对账器按「SUCCEEDED 且有
        # 时长」过滤，永远兜不到这一行）。
        budget = conn.execute(
            "SELECT budget_units FROM billing_operations WHERE source_id=%s AND service='asr' "
            "ORDER BY billing_round DESC LIMIT 1",
            (lease.id,),
        ).fetchone()
        if budget is None or budget[0] is None:
            raise script_from_audio_error(
                503,
                "SCRIPT_FROM_AUDIO_BILLING_MISSING",
                "转写计费单缺失，请联系管理员核实。",
            )
        finish_source(conn, lease.id, units=budget[0], succeeded=True)
    conn.commit()


def fail_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    cause: Exception,
    submission_started: bool,
    audio_deleted: bool = False,
) -> None:
    logger.warning("script-from-audio task %s failed: %s", lease.id, type(cause).__name__)
    now = _time_text(datetime.now(UTC))
    row = conn.execute(
        "SELECT provider_task_id FROM script_from_audio_tasks WHERE id=%s "
        "AND status='RUNNING' AND locked_by=%s AND attempt=%s AND locked_until>%s",
        (lease.id, lease.worker_id, lease.attempt, now),
    ).fetchone()
    if row is None:
        return
    has_receipt = row["provider_task_id"] is not None
    retryable = 1 if not submission_started else 0
    next_attempt_at = None
    if isinstance(cause, ScriptCachePending):
        status = "PENDING"
        code = "SCRIPT_FROM_AUDIO_CACHE_WAITING"
        retryable = 0
        next_attempt_at = _time_text(datetime.now(UTC) + timedelta(seconds=10))
    elif (
        isinstance(cause, HTTPException)
        and isinstance(cause.detail, dict)
        and cause.detail.get("code") == "SCRIPT_FROM_AUDIO_CACHE_UNCERTAIN"
    ):
        status = "FAILED"
        code = "SCRIPT_FROM_AUDIO_CACHE_UNCERTAIN"
        retryable = 0
    elif has_receipt and (
        isinstance(cause, AsrTaskPending)
        or (isinstance(cause, HTTPException) and cause.status_code == 503)
    ):
        status = "PENDING"
        code = "SCRIPT_FROM_AUDIO_RESUMING"
        retryable = 0
        next_attempt_at = _time_text(datetime.now(UTC) + timedelta(seconds=10))
    elif isinstance(cause, (AsrTaskPending, AsrSubmissionUncertain)):
        status = "SUBMISSION_UNCERTAIN"
        code = "SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN"
        retryable = 0
    elif submission_started and isinstance(cause, AsrProviderError):
        status = "FAILED"
        code = "SCRIPT_FROM_AUDIO_PROVIDER_FAILED"
    elif submission_started:
        status = "SUBMISSION_UNCERTAIN"
        code = "SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN"
    else:
        status = "FAILED"
        code = "SCRIPT_FROM_AUDIO_PIPELINE_FAILED"
    updated = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = %s, error_code = %s,
            error_message_redacted = %s, retryable = %s,
            completed_at = %s, updated_at = %s, locked_by = NULL,
            locked_until = NULL, next_attempt_at=%s,
            audio_object_key = CASE WHEN %s=1 THEN NULL ELSE audio_object_key END
        WHERE id = %s AND status='RUNNING' AND locked_by=%s AND attempt=%s AND locked_until>%s
        """,
        (
            status,
            code,
            _redacted_message(cause),
            retryable,
            None if status == "PENDING" else now,
            now,
            next_attempt_at,
            int(audio_deleted),
            lease.id,
            lease.worker_id,
            lease.attempt,
            now,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return
    if status in {"FAILED", "SUBMISSION_UNCERTAIN"}:
        from app.usage_billing import complete_source_attempt, finish_source

        complete_source_attempt(
            conn,
            lease.id,
            usage=cause.usage_seconds if isinstance(cause, AsrProviderError) else None,
        )
        finish_source(conn, lease.id, units=0, succeeded=False)
    conn.commit()


def load_script_from_audio_task(conn: BusinessConnection, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        raise script_from_audio_error(404, "SCRIPT_FROM_AUDIO_TASK_NOT_FOUND", "提取任务不存在。")
    return cast(sqlite3.Row, row)


def latest_script_from_audio_task(
    conn: BusinessConnection, *, project_id: str
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT * FROM script_from_audio_tasks
        WHERE project_id = %s
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def _redacted_message(cause: Exception) -> str:
    if isinstance(cause, ScriptCachePending):
        return "该视频文案正在提取，完成后自动返回，请稍候。"
    if isinstance(cause, AsrProviderError):
        return str(cause)
    if isinstance(cause, HTTPException):
        detail = cause.detail
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])
    return "文案提取管线执行失败，请稍后重试。"


def _object_key_from_uri(storage_uri: str) -> str:
    """``provider://bucket/key`` → ``key``（key 内段保留原样）。"""
    marker = "://"
    if marker not in storage_uri:
        return storage_uri
    after_scheme = storage_uri.split(marker, 1)[1]
    if "/" not in after_scheme:
        return storage_uri
    return after_scheme.split("/", 1)[1]
