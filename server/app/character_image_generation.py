from __future__ import annotations

import hashlib
import logging
import sqlite3
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import uuid4

from fastapi import HTTPException

from app import content_store
from app.auth import CurrentUser
from app.bootstrap import is_customer_production
from app.character_asset_quality import inspect_fake_character_asset
from app.character_contracts import (
    CharacterAsset,
    CharacterGenerationTask,
    RequiredCharacterViewType,
)
from app.character_identity import (
    REQUIRED_CHARACTER_VIEW_TYPES,
    character_error,
    character_not_found,
    decode_object,
    decode_string_list,
    effective_identity_is_active,
    encode_json,
    generated_character_asset_key,
    get_character_version,
    parse_datetime,
    parse_optional_datetime,
    read_identity_row,
    read_persona_row,
    read_version_row,
    require_character_admin,
    require_identity_active,
)
from app.db_portable import BusinessConnection
from app.operation_costs import begin_operation_cost, complete_operation_cost
from app.permissions import require_role, write_audit
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

logger = logging.getLogger(__name__)

FAKE_CHARACTER_PROVIDER = "fake_character"
FAKE_CHARACTER_MODEL = "fake-character-v1"
CHARACTER_GENERATION_SCHEMA_VERSION = "character-generation.v1"
CHARACTER_GENERATION_LEASE_SECONDS = 120
MAX_CHARACTER_GENERATION_ATTEMPTS = 3
CHARACTER_RETRY_BASE_SECONDS = 1
GENERATABLE_CHARACTER_VERSION_STATUSES = {"DRAFT", "GENERATING", "FAILED", "REVIEWING"}


@dataclass(frozen=True)
class CharacterImageRequest:
    task_id: str
    character_version_id: str
    view_type: RequiredCharacterViewType
    candidate_number: int
    attempt: int
    model: str
    source_sha256: str
    source_content: bytes
    persona_snapshot: dict[str, object]
    provider_parameters: dict[str, object]
    template_version: str | None
    template_hash: str | None


@dataclass(frozen=True)
class CharacterImageResult:
    content: bytes
    content_type: str
    provider_task_id: str
    cost_amount: float


class CharacterImageProvider(Protocol):
    provider_name: str

    def generate_view(self, request: CharacterImageRequest) -> CharacterImageResult: ...


class CharacterImageProviderFailed(RuntimeError):
    def __init__(
        self,
        code: str,
        message_redacted: str,
        *,
        retriable: bool,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message_redacted)
        self.code = code
        self.message_redacted = message_redacted
        self.retriable = retriable
        self.http_status = http_status


class CharacterGenerationLeaseLost(RuntimeError):
    pass


class FakeCharacterImageProvider:
    provider_name = FAKE_CHARACTER_PROVIDER

    def generate_view(self, request: CharacterImageRequest) -> CharacterImageResult:
        failure = fake_failure_for_request(request)
        if failure is not None:
            raise failure
        seed_payload = {
            "candidate_number": request.candidate_number,
            "character_version_id": request.character_version_id,
            "model": request.model,
            "persona_snapshot": request.persona_snapshot,
            "provider_parameters": request.provider_parameters,
            "source_sha256": request.source_sha256,
            "template_hash": request.template_hash,
            "template_version": request.template_version,
            "view_type": request.view_type,
        }
        seed = hashlib.sha256(encode_json(seed_payload).encode()).digest()
        return CharacterImageResult(
            content=deterministic_png(seed),
            content_type="image/png",
            provider_task_id=f"fake-character-{hashlib.sha256(seed).hexdigest()[:20]}",
            cost_amount=0.0,
        )


def fake_failure_for_request(
    request: CharacterImageRequest,
) -> CharacterImageProviderFailed | None:
    raw_behaviors = request.provider_parameters.get("fake_behavior_by_view")
    if not isinstance(raw_behaviors, dict):
        return None
    raw_behavior = raw_behaviors.get(request.view_type)
    if not isinstance(raw_behavior, dict):
        return None
    failure_type = str(raw_behavior.get("type", ""))
    try:
        fail_attempts = max(0, int(raw_behavior.get("fail_attempts", 0)))
    except (TypeError, ValueError):
        fail_attempts = 0
    if request.attempt > fail_attempts:
        return None
    failures = {
        "timeout": CharacterImageProviderFailed(
            "CHARACTER_PROVIDER_TIMEOUT",
            "character image provider timed out",
            retriable=True,
        ),
        "rate_limit": CharacterImageProviderFailed(
            "CHARACTER_PROVIDER_RATE_LIMITED",
            "character image provider rate limited the request",
            retriable=True,
            http_status=429,
        ),
        "server_error": CharacterImageProviderFailed(
            "CHARACTER_PROVIDER_UNAVAILABLE",
            "character image provider returned a server error",
            retriable=True,
            http_status=503,
        ),
        "invalid_response": CharacterImageProviderFailed(
            "CHARACTER_PROVIDER_INVALID_RESPONSE",
            "character image provider returned an invalid response",
            retriable=False,
        ),
    }
    return failures.get(failure_type)


def deterministic_png(seed: bytes, *, width: int = 1024, height: int = 1536) -> bytes:
    color = bytes((48 + seed[0] % 176, 48 + seed[1] % 176, 48 + seed[2] % 176))
    scanline = b"\x00" + color * width
    pixels = zlib.compress(scanline * height, level=9)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", pixels)
        + png_chunk(b"IEND", b"")
    )


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def create_character_generation_tasks(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    version_id: str,
    idempotency_key: str,
    view_types: list[RequiredCharacterViewType] | None,
    candidates_per_view: int,
) -> list[CharacterGenerationTask]:
    require_character_admin(
        conn,
        actor=actor,
        action="character_generation.create",
        entity_type="character_version",
        entity_id=version_id,
    )
    version = read_version_row(conn, version_id)
    selected_views = normalize_generation_views(version, view_types)
    if candidates_per_view < 1 or candidates_per_view > 4:
        raise character_error(
            422,
            "CHARACTER_CANDIDATE_COUNT_INVALID",
            "每个视角的候选数必须在 1 到 4 之间。",
        )
    clean_key = idempotency_key.strip()
    if not clean_key:
        raise character_error(422, "IDEMPOTENCY_KEY_REQUIRED", "幂等键不能为空。")
    request_hash = character_generation_request_hash(
        version=version,
        view_types=selected_views,
        candidates_per_view=candidates_per_view,
    )
    existing = find_idempotent_character_tasks(
        conn,
        version_id=version_id,
        idempotency_key=clean_key,
    )
    if existing:
        require_matching_character_request(existing, request_hash=request_hash)
        return [character_task_from_row(row) for row in existing]

    provider_name = str(version["provider"] or "")
    if provider_name == FAKE_CHARACTER_PROVIDER and is_customer_production():
        raise character_error(
            503,
            "FAKE_CHARACTER_PROVIDER_FORBIDDEN",
            "客户生产环境禁止创建模拟角色图片生成任务。",
        )
    require_version_generatable(version)
    if provider_name != FAKE_CHARACTER_PROVIDER:
        raise character_error(
            503,
            "CHARACTER_PROVIDER_NOT_CONFIGURED",
            "当前角色图片 Provider 尚未接入生成 Worker。",
        )
    source_asset_id = str(version["source_asset_id"] or "")
    if not source_asset_id or not str(version["source_sha256"] or ""):
        raise character_error(
            409,
            "CHARACTER_VERSION_SOURCE_MISSING",
            "角色版本缺少已冻结的真人源图。",
        )
    source_asset = conn.execute(
        "SELECT id FROM assets WHERE id = %s AND sha256 = %s",
        (source_asset_id, str(version["source_sha256"])),
    ).fetchone()
    if source_asset is None:
        raise character_error(
            409,
            "CHARACTER_VERSION_SOURCE_MISSING",
            "角色版本冻结的真人源图不存在或哈希不一致。",
        )
    persona = read_persona_row(conn, str(version["persona_id"]))
    identity = read_identity_row(conn, str(persona["identity_id"]))
    require_identity_active(identity)
    owner_user_id = str(identity["owner_user_id"] or identity["created_by"] or actor.id)
    task_rows: list[sqlite3.Row]
    try:
        conn.execute("BEGIN IMMEDIATE")
        latest_version = read_version_row(conn, version_id)
        latest_existing = find_idempotent_character_tasks(
            conn,
            version_id=version_id,
            idempotency_key=clean_key,
        )
        if latest_existing:
            require_matching_character_request(latest_existing, request_hash=request_hash)
            conn.commit()
            return [character_task_from_row(row) for row in latest_existing]
        require_version_generatable(latest_version)
        require_identity_active(read_identity_row(conn, str(persona["identity_id"])))
        next_candidates = {
            view_type: next_character_candidate_number(conn, version_id, view_type)
            for view_type in selected_views
        }
        for view_type in selected_views:
            for offset in range(candidates_per_view):
                candidate_number = next_candidates[view_type] + offset
                task_id = str(uuid4())
                snapshot = character_generation_snapshot(
                    version=latest_version,
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                    view_type=view_type,
                    candidate_number=candidate_number,
                )
                conn.execute(
                    """
                    INSERT INTO character_generation_tasks (
                        id, character_version_id, view_type, provider, model,
                        request_snapshot_json, status, idempotency_key, request_hash,
                        candidate_number, attempt, max_attempts, next_poll_at, created_by
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, 0, %s, CURRENT_TIMESTAMP, %s
                    )
                    """,
                    (
                        task_id,
                        version_id,
                        view_type,
                        str(latest_version["provider"]),
                        str(latest_version["model"]),
                        encode_json(snapshot),
                        clean_key,
                        request_hash,
                        candidate_number,
                        MAX_CHARACTER_GENERATION_ATTEMPTS,
                        actor.id,
                    ),
                )
                from app.usage_billing import accept_operation

                accept_operation(
                    conn, user_id=actor.id, service="character", source_id=task_id, units=1
                )
        conn.execute(
            "UPDATE character_versions SET status = 'GENERATING' WHERE id = %s",
            (version_id,),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        concurrent = find_idempotent_character_tasks(
            conn,
            version_id=version_id,
            idempotency_key=clean_key,
        )
        if concurrent:
            require_matching_character_request(concurrent, request_hash=request_hash)
            return [character_task_from_row(row) for row in concurrent]
        raise character_error(
            409,
            "CHARACTER_GENERATION_CONFLICT",
            "角色图片候选编号发生并发冲突，请重试。",
        ) from exc
    task_rows = find_idempotent_character_tasks(
        conn,
        version_id=version_id,
        idempotency_key=clean_key,
    )
    write_audit(
        conn,
        actor=actor,
        action="character_generation.create",
        entity_type="character_version",
        entity_id=version_id,
        metadata={
            "candidates_per_view": candidates_per_view,
            "idempotency_key_hash": hashlib.sha256(clean_key.encode()).hexdigest(),
            "task_count": len(task_rows),
            "view_types": selected_views,
        },
    )
    return [character_task_from_row(row) for row in task_rows]


def regenerate_character_asset(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    character_asset_id: str,
    idempotency_key: str,
) -> list[CharacterGenerationTask]:
    row = conn.execute(
        "SELECT * FROM character_assets WHERE id = %s",
        (character_asset_id,),
    ).fetchone()
    if row is None:
        raise character_not_found("CHARACTER_ASSET_NOT_FOUND", "角色候选资产不存在。")
    view_type = str(row["view_type"])
    if view_type not in REQUIRED_CHARACTER_VIEW_TYPES:
        raise character_error(
            409,
            "CHARACTER_ASSET_NOT_GENERATABLE",
            "历史导入资产不能作为标准多视角候选重生成。",
        )
    tasks = create_character_generation_tasks(
        conn,
        actor=actor,
        version_id=str(row["character_version_id"]),
        idempotency_key=idempotency_key,
        view_types=[view_type],
        candidates_per_view=1,
    )
    write_audit(
        conn,
        actor=actor,
        action="character_asset.regenerate",
        entity_type="character_asset",
        entity_id=character_asset_id,
        metadata={"generation_task_ids": [task.id for task in tasks]},
    )
    return tasks


def list_character_generation_tasks(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    version_id: str,
) -> list[CharacterGenerationTask]:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin", "auditor"},
        action="character_generation.list",
        entity_type="character_version",
        entity_id=version_id,
    )
    get_character_version(conn, actor=actor, version_id=version_id)
    rows = conn.execute(
        """
        SELECT * FROM character_generation_tasks
        WHERE character_version_id = %s
        ORDER BY created_at, view_type, candidate_number, id
        """,
        (version_id,),
    ).fetchall()
    return [character_task_from_row(row) for row in rows]


def list_character_assets(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    version_id: str,
) -> list[CharacterAsset]:
    version = get_character_version(conn, actor=actor, version_id=version_id)
    if actor.role == "employee":
        rows = conn.execute(
            """
            SELECT * FROM character_assets
            WHERE character_version_id = %s
              AND review_status = 'APPROVED'
              AND is_published_selection = 1
            ORDER BY view_type, candidate_number, id
            """,
            (version.id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM character_assets
            WHERE character_version_id = %s
            ORDER BY view_type, candidate_number, id
            """,
            (version.id,),
        ).fetchall()
    return [character_asset_from_row(row) for row in rows]


def acquire_character_generation_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> sqlite3.Row | None:
    locked_until = (
        datetime.now(UTC) + timedelta(seconds=CHARACTER_GENERATION_LEASE_SECONDS)
    ).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        finalize_expired_character_generation_leases(conn)
        row = conn.execute(
            """
            UPDATE character_generation_tasks
            SET status = 'RUNNING', attempt = attempt + 1,
                locked_by = %s, locked_until = %s, next_poll_at = NULL,
                started_at = COALESCE(started_at::timestamptz, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM character_generation_tasks
                WHERE (
                    status = 'PENDING'
                    OR (
                        status = 'RUNNING'
                        AND locked_until IS NOT NULL
                        AND locked_until::timestamptz <= now()
                    )
                )
                  AND attempt < max_attempts
                  AND (next_poll_at IS NULL OR next_poll_at::timestamptz <= now())
                  AND (
                      locked_until IS NULL
                      OR locked_until::timestamptz <= now()
                  )
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            (worker_id, locked_until),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cast(sqlite3.Row | None, row)


def finalize_expired_character_generation_leases(conn: BusinessConnection) -> None:
    rows = conn.execute(
        """
        SELECT * FROM character_generation_tasks
        WHERE status = 'RUNNING'
          AND attempt >= max_attempts
          AND locked_until IS NOT NULL
          AND locked_until::timestamptz <= now()
        ORDER BY created_at, id
        """
    ).fetchall()
    if not rows:
        return

    error = CharacterImageProviderFailed(
        "CHARACTER_LEASE_EXPIRED",
        "character generation lease expired",
        retriable=False,
    )
    version_ids: set[str] = set()
    for task in rows:
        conn.execute(
            """
            UPDATE character_generation_tasks
            SET status = 'FAILED', error_code = %s, error_message_redacted = %s,
                completed_at = CURRENT_TIMESTAMP, locked_by = NULL,
                locked_until = NULL, next_poll_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (error.code, error.message_redacted, str(task["id"])),
        )
        insert_character_call_log(
            conn,
            task=task,
            latency_ms=0,
            request_hash=str(task["request_hash"] or ""),
            error=error,
        )
        insert_character_worker_audit(
            conn,
            task=task,
            action="character_generation.lease_expired",
            metadata={"attempt": int(task["attempt"]), "error_code": error.code},
        )
        version_ids.add(str(task["character_version_id"]))
    for version_id in version_ids:
        update_character_version_generation_status(conn, version_id=version_id)


def run_next_character_generation_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
    storage: StorageAdapter,
    provider: CharacterImageProvider | None = None,
    lease: sqlite3.Row | None = None,
) -> CharacterGenerationTask | None:
    if lease is None:
        task = acquire_character_generation_task(conn, worker_id=worker_id)
    else:
        task = lease
    if task is None:
        return None
    started = time.monotonic()
    request_hash = str(task["request_hash"] or "")
    cost_record_id = ""
    cost_completed = False
    try:
        request = load_character_image_request(conn, task=task, storage=storage)
        selected_provider = provider or character_provider_for_name(str(task["provider"]))
        if selected_provider.provider_name != str(task["provider"]):
            raise CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_MISMATCH",
                "character image provider does not match the queued task",
                retriable=False,
            )
        cost_record_id = begin_operation_cost(
            conn,
            source_type="character_generation_task",
            source_id=f"{task['id']}:{task['attempt']}",
            subject="character_sheet_image",
            user_id=str(task["created_by"]),
            metadata={"view_type": str(task["view_type"])},
        )
        result = selected_provider.generate_view(request)
        complete_operation_cost(conn, record_id=cost_record_id, usage_amount=1)
        cost_completed = True
        validate_character_image_result(result)
        try:
            auto_quality = inspect_fake_character_asset(result.content, view_type=request.view_type)
        except ValueError as exc:
            raise CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_INVALID_RESPONSE",
                "character image provider returned an invalid response",
                retriable=False,
            ) from exc
    except CharacterImageProviderFailed as exc:
        if not cost_completed:
            complete_operation_cost(conn, record_id=cost_record_id, usage_amount=None)
        return finish_character_generation_failure(
            conn,
            task=task,
            worker_id=worker_id,
            error=exc,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_hash=request_hash,
        )
    except (KeyError, OSError, StorageBackendUnavailable, ValueError) as exc:
        if not cost_completed:
            complete_operation_cost(conn, record_id=cost_record_id, usage_amount=None)
        logger.warning("Character source asset could not be read: %s", type(exc).__name__)
        return finish_character_generation_failure(
            conn,
            task=task,
            worker_id=worker_id,
            error=CharacterImageProviderFailed(
                "CHARACTER_STORAGE_UNAVAILABLE",
                "character source or output storage is unavailable",
                retriable=True,
            ),
            latency_ms=int((time.monotonic() - started) * 1000),
            request_hash=request_hash,
        )

    try:
        require_character_generation_lease_owned(conn, task=task, worker_id=worker_id)
        require_character_generation_context_active(conn, task=task)
    except CharacterGenerationLeaseLost:
        return record_stale_character_generation_result(
            conn,
            task=task,
            worker_id=worker_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_hash=request_hash,
            provider_task_id=result.provider_task_id,
            cost_amount=result.cost_amount,
        )
    except CharacterImageProviderFailed as exc:
        return finish_character_generation_failure(
            conn,
            task=task,
            worker_id=worker_id,
            error=exc,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_hash=request_hash,
            provider_task_id=result.provider_task_id,
            cost_amount=result.cost_amount,
        )

    core_asset_id = str(uuid4())
    character_asset_id = str(uuid4())
    snapshot = decode_object(task["request_snapshot_json"])
    object_key = generated_character_asset_key(
        owner_user_id=str(snapshot["owner_user_id"]),
        persona_id=str(snapshot["persona_id"]),
        version_id=str(task["character_version_id"]),
        view_type=cast(RequiredCharacterViewType, str(task["view_type"])),
        asset_id=core_asset_id,
    )
    try:
        stored = storage.put_object(object_key, result.content, content_type=result.content_type)
    except (OSError, StorageBackendUnavailable, ValueError) as exc:
        logger.warning("Character image could not be archived: %s", type(exc).__name__)
        return finish_character_generation_failure(
            conn,
            task=task,
            worker_id=worker_id,
            error=CharacterImageProviderFailed(
                "CHARACTER_STORAGE_UNAVAILABLE",
                "character source or output storage is unavailable",
                retriable=True,
            ),
            latency_ms=int((time.monotonic() - started) * 1000),
            request_hash=request_hash,
            provider_task_id=result.provider_task_id,
            cost_amount=result.cost_amount,
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        require_character_generation_lease_owned(conn, task=task, worker_id=worker_id)
        require_character_generation_context_active(conn, task=task)
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id, metadata_json
            )
            VALUES (%s, NULL, 'character_generated_image', %s, %s, %s, %s, %s, %s)
            """,
            (
                core_asset_id,
                stored.uri,
                stored.sha256,
                stored.size,
                stored.content_type,
                task["created_by"],
                encode_json(
                    {
                        "candidate_number": int(task["candidate_number"]),
                        "character_version_id": str(task["character_version_id"]),
                        "generation_task_id": str(task["id"]),
                        "view_type": str(task["view_type"]),
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO character_assets (
                id, character_version_id, asset_id, view_type,
                candidate_number, generation_task_id, auto_quality_json,
                review_status, is_published_selection
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'NOT_REVIEWED', 0)
            """,
            (
                character_asset_id,
                str(task["character_version_id"]),
                core_asset_id,
                str(task["view_type"]),
                int(task["candidate_number"]),
                str(task["id"]),
                encode_json(auto_quality),
            ),
        )
        updated = conn.execute(
            """
            UPDATE character_generation_tasks
            SET status = 'SUCCEEDED', provider_task_id = %s, error_code = NULL,
                error_message_redacted = NULL, cost_amount = %s,
                completed_at = CURRENT_TIMESTAMP,
                locked_by = NULL, locked_until = NULL, next_poll_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'RUNNING' AND locked_by = %s AND attempt = %s
            """,
            (
                result.provider_task_id,
                result.cost_amount,
                str(task["id"]),
                worker_id,
                int(task["attempt"]),
            ),
        )
        if updated.rowcount != 1:
            raise CharacterGenerationLeaseLost
        from app.usage_billing import finish_source

        finish_source(conn, str(task["id"]), units=1, succeeded=True)
        insert_character_call_log(
            conn,
            task=task,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_hash=request_hash,
            provider_task_id=result.provider_task_id,
            response_asset_id=core_asset_id,
        )
        update_character_version_generation_status(
            conn,
            version_id=str(task["character_version_id"]),
        )
        insert_character_worker_audit(
            conn,
            task=task,
            action="character_generation.succeeded",
            metadata={"character_asset_id": character_asset_id},
        )
        conn.commit()
    except CharacterGenerationLeaseLost:
        conn.rollback()
        delete_character_object_quietly(storage, object_key)
        return record_stale_character_generation_result(
            conn,
            task=task,
            worker_id=worker_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_hash=request_hash,
            provider_task_id=result.provider_task_id,
            cost_amount=result.cost_amount,
        )
    except CharacterImageProviderFailed as exc:
        conn.rollback()
        delete_character_object_quietly(storage, object_key)
        return finish_character_generation_failure(
            conn,
            task=task,
            worker_id=worker_id,
            error=exc,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_hash=request_hash,
            provider_task_id=result.provider_task_id,
            cost_amount=result.cost_amount,
        )
    except Exception:
        conn.rollback()
        delete_character_object_quietly(storage, object_key)
        raise
    return get_character_generation_task(conn, str(task["id"]))


def load_character_image_request(
    conn: BusinessConnection,
    *,
    task: sqlite3.Row,
    storage: StorageAdapter,
) -> CharacterImageRequest:
    snapshot = decode_object(task["request_snapshot_json"])
    require_character_generation_context_active(conn, task=task)
    source_asset_id = str(snapshot.get("source_asset_id", ""))
    source_asset = conn.execute(
        "SELECT * FROM assets WHERE id = %s",
        (source_asset_id,),
    ).fetchone()
    if source_asset is None or str(source_asset["sha256"]) != str(
        snapshot.get("source_sha256", "")
    ):
        raise CharacterImageProviderFailed(
            "CHARACTER_VERSION_SOURCE_MISSING",
            "character version source asset is missing or changed",
            retriable=False,
        )
    reference = storage_object_ref_from_uri(str(source_asset["storage_uri"]))
    require_storage_match(storage, reference)
    source_content = storage.get_object(reference.key)
    if hashlib.sha256(source_content).hexdigest() != str(snapshot.get("source_sha256", "")):
        raise CharacterImageProviderFailed(
            "CHARACTER_VERSION_SOURCE_CHANGED",
            "character version source asset content changed",
            retriable=False,
        )
    return CharacterImageRequest(
        task_id=str(task["id"]),
        character_version_id=str(task["character_version_id"]),
        view_type=cast(RequiredCharacterViewType, str(task["view_type"])),
        candidate_number=int(task["candidate_number"]),
        attempt=int(task["attempt"]),
        model=str(task["model"]),
        source_sha256=str(snapshot["source_sha256"]),
        source_content=source_content,
        persona_snapshot=cast(dict[str, object], snapshot.get("persona_snapshot", {})),
        provider_parameters=cast(dict[str, object], snapshot.get("provider_parameters", {})),
        template_version=optional_snapshot_text(snapshot.get("template_version")),
        template_hash=optional_snapshot_text(snapshot.get("template_hash")),
    )


def require_character_generation_context_active(
    conn: BusinessConnection,
    *,
    task: sqlite3.Row,
) -> sqlite3.Row:
    version = read_version_row(conn, str(task["character_version_id"]))
    if str(version["status"]) not in GENERATABLE_CHARACTER_VERSION_STATUSES:
        raise CharacterImageProviderFailed(
            "CHARACTER_VERSION_NOT_GENERATABLE",
            "character version is not generatable",
            retriable=False,
        )
    persona = read_persona_row(conn, str(version["persona_id"]))
    identity = read_identity_row(conn, str(persona["identity_id"]))
    if not effective_identity_is_active(identity):
        raise CharacterImageProviderFailed(
            "IDENTITY_NOT_ACTIVE",
            "character identity is not active",
            retriable=False,
        )
    return version


def require_character_generation_lease_owned(
    conn: BusinessConnection,
    *,
    task: sqlite3.Row,
    worker_id: str,
) -> None:
    current = conn.execute(
        """
        SELECT status, locked_by, attempt
        FROM character_generation_tasks
        WHERE id = %s
        """,
        (str(task["id"]),),
    ).fetchone()
    if (
        current is None
        or str(current["status"]) != "RUNNING"
        or str(current["locked_by"]) != worker_id
        or int(current["attempt"]) != int(task["attempt"])
    ):
        raise CharacterGenerationLeaseLost


def delete_character_object_quietly(storage: StorageAdapter, object_key: str) -> None:
    try:
        content_store.delete_object_outside_content_namespace(storage, object_key)
    except Exception:
        logger.exception("Failed to clean an orphaned character image object")


def validate_character_image_result(result: CharacterImageResult) -> None:
    if result.content_type != "image/png" or not result.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CharacterImageProviderFailed(
            "CHARACTER_PROVIDER_INVALID_RESPONSE",
            "character image provider returned an invalid response",
            retriable=False,
        )
    if result.cost_amount < 0:
        raise CharacterImageProviderFailed(
            "CHARACTER_PROVIDER_INVALID_RESPONSE",
            "character image provider returned an invalid response",
            retriable=False,
        )


def finish_character_generation_failure(
    conn: BusinessConnection,
    *,
    task: sqlite3.Row,
    worker_id: str,
    error: CharacterImageProviderFailed,
    latency_ms: int,
    request_hash: str,
    provider_task_id: str | None = None,
    cost_amount: float | None = None,
) -> CharacterGenerationTask:
    attempt = int(task["attempt"])
    max_attempts = int(task["max_attempts"])
    should_retry = error.retriable and attempt < max_attempts
    status = "PENDING" if should_retry else "FAILED"
    next_poll_at = (
        datetime.now(UTC)
        + timedelta(seconds=CHARACTER_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))
        if should_retry
        else None
    )
    with conn:
        updated = conn.execute(
            """
            UPDATE character_generation_tasks
            SET status = %s, error_code = %s, error_message_redacted = %s,
                provider_task_id = COALESCE(%s, provider_task_id),
                cost_amount = COALESCE(%s, cost_amount),
                completed_at = CASE WHEN %s = 'FAILED' THEN CURRENT_TIMESTAMP ELSE NULL END,
                locked_by = NULL, locked_until = NULL, next_poll_at = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'RUNNING' AND locked_by = %s AND attempt = %s
            """,
            (
                status,
                error.code,
                error.message_redacted,
                provider_task_id,
                cost_amount,
                status,
                next_poll_at.isoformat() if next_poll_at is not None else None,
                str(task["id"]),
                worker_id,
                attempt,
            ),
        )
        insert_character_call_log(
            conn,
            task=task,
            latency_ms=latency_ms,
            request_hash=request_hash,
            provider_task_id=provider_task_id,
            error=error,
        )
        if updated.rowcount != 1:
            insert_character_worker_audit(
                conn,
                task=task,
                action="character_generation.stale_worker_ignored",
                metadata={
                    "attempt": attempt,
                    "error_code": error.code,
                    "worker_id": worker_id,
                },
            )
            return get_character_generation_task(conn, str(task["id"]))
        if not should_retry:
            from app.usage_billing import finish_source

            finish_source(conn, str(task["id"]), units=0, succeeded=False)
        update_character_version_generation_status(
            conn,
            version_id=str(task["character_version_id"]),
        )
        insert_character_worker_audit(
            conn,
            task=task,
            action=(
                "character_generation.retry_scheduled"
                if should_retry
                else "character_generation.failed"
            ),
            metadata={"attempt": attempt, "error_code": error.code},
        )
    return get_character_generation_task(conn, str(task["id"]))


def record_stale_character_generation_result(
    conn: BusinessConnection,
    *,
    task: sqlite3.Row,
    worker_id: str,
    latency_ms: int,
    request_hash: str,
    provider_task_id: str,
    cost_amount: float,
) -> CharacterGenerationTask:
    error = CharacterImageProviderFailed(
        "CHARACTER_LEASE_LOST",
        "character generation result ignored after lease ownership changed",
        retriable=False,
    )
    with conn:
        insert_character_call_log(
            conn,
            task=task,
            latency_ms=latency_ms,
            request_hash=request_hash,
            provider_task_id=provider_task_id,
            error=error,
        )
        insert_character_worker_audit(
            conn,
            task=task,
            action="character_generation.stale_worker_ignored",
            metadata={
                "attempt": int(task["attempt"]),
                "cost_amount": cost_amount,
                "error_code": error.code,
                "worker_id": worker_id,
            },
        )
    return get_character_generation_task(conn, str(task["id"]))


def insert_character_call_log(
    conn: BusinessConnection,
    *,
    task: sqlite3.Row,
    latency_ms: int,
    request_hash: str,
    provider_task_id: str | None = None,
    response_asset_id: str | None = None,
    error: CharacterImageProviderFailed | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO external_call_logs (
            id, generation_task_id, character_generation_task_id,
            provider, model, endpoint_name, provider_request_id,
            http_status, latency_ms, request_hash, response_asset_id,
            error_code, error_message_redacted
        )
        VALUES (%s, NULL, %s, %s, %s, 'character_image.generate_view', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            str(task["id"]),
            str(task["provider"]),
            str(task["model"]),
            provider_task_id,
            None if error is None else error.http_status,
            max(0, latency_ms),
            request_hash,
            response_asset_id,
            None if error is None else error.code,
            None if error is None else error.message_redacted,
        ),
    )


def insert_character_worker_audit(
    conn: BusinessConnection,
    *,
    task: sqlite3.Row,
    action: str,
    metadata: dict[str, object],
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (
            id, actor_user_id, action, entity_type, entity_id, metadata_json
        )
        VALUES (%s, %s, %s, 'character_generation_task', %s, %s)
        """,
        (
            str(uuid4()),
            task["created_by"],
            action,
            str(task["id"]),
            encode_json(metadata),
        ),
    )


def update_character_version_generation_status(
    conn: BusinessConnection,
    *,
    version_id: str,
) -> None:
    current = conn.execute(
        "SELECT status FROM character_versions WHERE id = %s",
        (version_id,),
    ).fetchone()
    if current is None or str(current["status"]) in {"PUBLISHED", "ARCHIVED"}:
        return
    counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM character_generation_tasks
            WHERE character_version_id = %s
            GROUP BY status
            """,
            (version_id,),
        ).fetchall()
    }
    if counts.get("PENDING", 0) or counts.get("RUNNING", 0):
        next_status = "GENERATING"
    elif counts.get("SUCCEEDED", 0):
        next_status = "REVIEWING"
    else:
        next_status = "FAILED"
    conn.execute(
        "UPDATE character_versions SET status = %s WHERE id = %s",
        (next_status, version_id),
    )


def get_character_generation_task(
    conn: BusinessConnection,
    task_id: str,
) -> CharacterGenerationTask:
    row = conn.execute(
        "SELECT * FROM character_generation_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        raise character_not_found("CHARACTER_GENERATION_TASK_NOT_FOUND", "角色生成任务不存在。")
    return character_task_from_row(row)


def character_provider_for_name(provider_name: str) -> CharacterImageProvider:
    if provider_name == FAKE_CHARACTER_PROVIDER:
        if is_customer_production():
            raise CharacterImageProviderFailed(
                "FAKE_CHARACTER_PROVIDER_FORBIDDEN",
                "fake character provider is forbidden in customer production",
                retriable=False,
            )
        return FakeCharacterImageProvider()
    raise CharacterImageProviderFailed(
        "CHARACTER_PROVIDER_NOT_CONFIGURED",
        "character image provider is not configured",
        retriable=False,
    )


def normalize_generation_views(
    version: sqlite3.Row,
    requested: list[RequiredCharacterViewType] | None,
) -> list[RequiredCharacterViewType]:
    required = decode_string_list(version["required_view_types_json"])
    required_set = set(required)
    if not required_set:
        raise character_error(
            409,
            "CHARACTER_VERSION_HAS_NO_STANDARD_VIEWS",
            "历史导入角色版本没有标准五视角生成契约。",
        )
    requested_set = required_set if requested is None else {str(value) for value in requested}
    if not requested_set or not requested_set <= required_set:
        raise character_error(
            422,
            "CHARACTER_VIEW_TYPE_INVALID",
            "请求的视角不在当前角色版本的必需视角集中。",
        )
    return [view_type for view_type in REQUIRED_CHARACTER_VIEW_TYPES if view_type in requested_set]


def require_version_generatable(
    version: sqlite3.Row,
) -> None:
    if str(version["status"]) not in GENERATABLE_CHARACTER_VERSION_STATUSES:
        raise character_error(
            409,
            "CHARACTER_VERSION_NOT_GENERATABLE",
            "已发布或已归档的角色版本不能再生成候选资产。",
        )


def next_character_candidate_number(
    conn: BusinessConnection,
    version_id: str,
    view_type: RequiredCharacterViewType,
) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(candidate_number), 0) + 1
        FROM (
            SELECT candidate_number FROM character_generation_tasks
            WHERE character_version_id = %s AND view_type = %s
            UNION ALL
            SELECT candidate_number FROM character_assets
            WHERE character_version_id = %s AND view_type = %s
        )
        """,
        (version_id, view_type, version_id, view_type),
    ).fetchone()
    return int(row[0])


def character_generation_snapshot(
    *,
    version: sqlite3.Row,
    owner_user_id: str,
    task_id: str,
    view_type: RequiredCharacterViewType,
    candidate_number: int,
) -> dict[str, object]:
    return {
        "candidate_number": candidate_number,
        "character_version_id": str(version["id"]),
        "model": str(version["model"]),
        "owner_user_id": owner_user_id,
        "persona_id": str(version["persona_id"]),
        "persona_snapshot": decode_object(version["persona_snapshot_json"]),
        "provider": str(version["provider"]),
        "provider_parameters": decode_object(version["generation_params_json"]),
        "schema_version": CHARACTER_GENERATION_SCHEMA_VERSION,
        "source_asset_id": str(version["source_asset_id"]),
        "source_sha256": str(version["source_sha256"]),
        "task_id": task_id,
        "template_hash": version["template_hash"],
        "template_version": version["template_version"],
        "view_type": view_type,
    }


def character_generation_request_hash(
    *,
    version: sqlite3.Row,
    view_types: list[RequiredCharacterViewType],
    candidates_per_view: int,
) -> str:
    payload = {
        "candidates_per_view": candidates_per_view,
        "character_version_id": str(version["id"]),
        "model": str(version["model"]),
        "provider": str(version["provider"]),
        "view_types": view_types,
    }
    return hashlib.sha256(encode_json(payload).encode()).hexdigest()


def find_idempotent_character_tasks(
    conn: BusinessConnection,
    *,
    version_id: str,
    idempotency_key: str,
) -> list[sqlite3.Row]:
    return cast(
        list[sqlite3.Row],
        conn.execute(
            """
            SELECT * FROM character_generation_tasks
            WHERE character_version_id = %s AND idempotency_key = %s
            ORDER BY view_type, candidate_number, id
            """,
            (version_id, idempotency_key),
        ).fetchall(),
    )


def require_matching_character_request(
    rows: list[sqlite3.Row],
    *,
    request_hash: str,
) -> None:
    if any(str(row["request_hash"]) != request_hash for row in rows):
        raise character_error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "该幂等键已用于不同的角色图片生成请求。",
        )


def character_task_from_row(row: sqlite3.Row) -> CharacterGenerationTask:
    return CharacterGenerationTask(
        id=str(row["id"]),
        character_version_id=str(row["character_version_id"]),
        view_type=cast(Any, str(row["view_type"])),
        provider=str(row["provider"]),
        model=str(row["model"]),
        request_snapshot_json=decode_object(row["request_snapshot_json"]),
        status=cast(Any, str(row["status"])),
        provider_task_id=None if row["provider_task_id"] is None else str(row["provider_task_id"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        cost_amount=None if row["cost_amount"] is None else float(row["cost_amount"]),
        started_at=parse_optional_datetime(row["started_at"]),
        completed_at=parse_optional_datetime(row["completed_at"]),
        idempotency_key=str(row["idempotency_key"]),
        request_hash=str(row["request_hash"]),
        candidate_number=int(row["candidate_number"]),
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        next_poll_at=parse_optional_datetime(row["next_poll_at"]),
        error_message_redacted=(
            None if row["error_message_redacted"] is None else str(row["error_message_redacted"])
        ),
        created_by=None if row["created_by"] is None else str(row["created_by"]),
        created_at=parse_datetime(str(row["created_at"])),
        updated_at=parse_datetime(str(row["updated_at"])),
    )


def character_asset_from_row(row: sqlite3.Row) -> CharacterAsset:
    return CharacterAsset(
        id=str(row["id"]),
        character_version_id=str(row["character_version_id"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        view_type=cast(Any, str(row["view_type"])),
        candidate_number=int(row["candidate_number"]),
        generation_task_id=(
            None if row["generation_task_id"] is None else str(row["generation_task_id"])
        ),
        auto_quality_json=decode_object(row["auto_quality_json"]),
        review_status=cast(Any, str(row["review_status"])),
        is_published_selection=bool(row["is_published_selection"]),
        created_at=parse_datetime(str(row["created_at"])),
    )


def optional_snapshot_text(value: object) -> str | None:
    return None if value is None else str(value)
