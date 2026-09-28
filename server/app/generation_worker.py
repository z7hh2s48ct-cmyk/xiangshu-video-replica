from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any

from fastapi import HTTPException

from app import content_store
from app.analysis import VideoAnalysisProvider
from app.analysis_routes import (
    acquire_analysis_task,
    complete_analysis_task,
    fail_analysis_task,
    perform_analysis_task,
    prepare_analysis_task,
)
from app.billing_meter import billing_context
from app.character_image_generation import (
    CharacterImageProvider,
    acquire_character_generation_task,
    run_next_character_generation_task,
)
from app.db_pg import (
    DatabaseMode,
    check_pg_ready,
    close_pg_pool,
    pg_transaction,
    resolve_database_config,
    validate_customer_production,
)
from app.db_portable import BusinessConnection
from app.first_frame_routes import get_first_frame_quality_inspector, get_image_provider
from app.first_frames import (
    FirstFrameQualityInspector,
    ImageInput,
    ImageProvider,
    bounded_source_frame_quality_inspector,
)
from app.generation import (
    GenerationTaskSupersededError,
    H3Provider,
    H3ProviderFailed,
    H3ProviderSettingsUnavailable,
    MetasoH3Provider,
    ProviderRequestContractError,
    SubmissionUncertain,
    acquire_generation_continuation_lease,
    acquire_generation_reconcile_operation,
    acquire_generation_task_lease,
    complete_generation_reconcile_operation,
    fail_generation_reconcile_operation,
    finalize_generation_direct_result,
    h3_provider_for_task,
    mark_generation_task_archiving,
    mark_generation_task_running,
    mark_task_first_frame_url_sign_failed,
    mark_task_provider_failed,
    mark_task_provider_request_contract_failed,
    mark_task_provider_settings_unavailable,
    mark_task_submission_uncertain,
    perform_generation_reconcile_operation,
    prepare_generation_reconcile_operation,
    prepare_generation_submission,
    reschedule_generation_poll,
    run_next_generation_task,
)
from app.hifly import HiflyClient, HiflySettingsUnavailable, hifly_client_from_settings
from app.image_tasks import (
    acquire_character_sheet_task,
    acquire_first_frame_task,
    complete_character_sheet_task,
    complete_first_frame_task,
    fail_image_task,
    log_image_task_failure,
    perform_character_sheet_task,
    prepare_character_sheet_task,
    prepare_first_frame_task,
    rate_limit_backoff_seconds,
    record_image_task_provider,
    renew_image_task_lease,
    run_first_frame_task_outside_transaction,
    save_first_frame_provider_submission,
    save_first_frame_task_checkpoint,
)
from app.media_routes import get_media_storage
from app.operation_costs import begin_operation_cost, complete_operation_cost
from app.oral_worker import (
    OralLeaseLostError,
    OralWorkKind,
    OralWorkResult,
    claim_oral_work,
    discard_uncommitted_oral_asset,
    finalize_oral_work,
    perform_oral_work,
    prepare_oral_work,
)
from app.script_from_audio import (
    ScriptFromAudioTaskLease,
    acquire_script_from_audio_task,
    checkpoint_script_from_audio_task,
    complete_script_from_audio_task,
    fail_script_from_audio_task,
    mark_script_from_audio_submission_started,
    perform_script_from_audio_task,
    prepare_script_from_audio_task,
)
from app.script_rewrite import (
    acquire_script_rewrite_task,
    complete_script_rewrite_task,
    fail_script_rewrite_task,
    mark_script_rewrite_submission_started,
    perform_script_rewrite_task,
    prepare_script_rewrite_task,
)
from app.simple_character import SIMPLE_CONTACT_SHEET_MODEL
from app.source_frames import (
    FFmpegSourceFrameExtractor,
    SourceFrameExtractor,
    SourceFrameQualityInspector,
    SourceFrameTaskLease,
    acquire_source_frame_task,
    complete_source_frame_task,
    delete_created_source_frames,
    fail_source_frame_task,
    perform_source_frame_extraction,
    prepare_source_frame_task,
    record_source_frame_quality_started,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    StoragePermissionError,
)
from app.viral_import import (
    acquire_viral_import_task,
    complete_viral_import_task,
    discard_viral_import_outcome,
    fail_viral_import_task,
    perform_viral_import_task,
    prepare_viral_import_task,
)
from app.viral_refresh import (
    ViralRefreshLease,
    acquire_viral_refresh_task,
    complete_viral_refresh_task,
    fail_viral_refresh_task,
)
from app.worker_identity import new_worker_instance_id

logger = logging.getLogger(__name__)


@contextmanager
def _sqlite_audio_connection(conn: BusinessConnection) -> Iterator[BusinessConnection]:
    yield conn


@contextmanager
def _pg_audio_connection() -> Iterator[BusinessConnection]:
    with pg_transaction() as raw_conn:
        yield BusinessConnection.postgres(raw_conn)


def _cleanup_audio_objects(
    connection: Callable[[], AbstractContextManager[BusinessConnection]],
    storage: StorageAdapter,
) -> None:
    # Leave uncertain submissions' input available until the signed URL expires.
    cutoff = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with connection() as conn:
        rows = conn.execute(
            "SELECT id,audio_object_key FROM script_from_audio_tasks "
            "WHERE audio_object_key IS NOT NULL AND (status IN ('SUCCEEDED','FAILED') "
            "OR (status='SUBMISSION_UNCERTAIN' AND updated_at<=%s)) "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=%s) "
            "ORDER BY updated_at,id LIMIT 10",
            (cutoff, now),
        ).fetchall()
    for row in rows:
        try:
            with connection() as conn:
                # The gate re-reads references at delete time. A refusal means the
                # bytes belong to a surviving reference (registry or another asset),
                # in which case only this task's now-stale column is cleared.
                content_store.delete_object_if_unreferenced(
                    conn,
                    storage,
                    str(row["audio_object_key"]),
                    actor_id="script-from-audio-cleanup",
                )
                conn.execute(
                    "UPDATE script_from_audio_tasks SET audio_object_key=NULL "
                    "WHERE id=%s AND audio_object_key=%s "
                    "AND status IN ('SUCCEEDED','FAILED','SUBMISSION_UNCERTAIN')",
                    (row["id"], row["audio_object_key"]),
                )
                conn.commit()
        except Exception as exc:
            # 权限/网络故障保持清理凭据，但不要每个空闲轮次重复访问对象存储。
            retry_at = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            with connection() as conn:
                conn.execute(
                    "UPDATE script_from_audio_tasks SET next_attempt_at=%s "
                    "WHERE id=%s AND audio_object_key=%s "
                    "AND status IN ('SUCCEEDED','FAILED','SUBMISSION_UNCERTAIN')",
                    (retry_at, row["id"], row["audio_object_key"]),
                )
                conn.commit()
            logger.warning("ASR cleanup deferred for task %s: %s", row["id"], type(exc).__name__)


def _run_audio_lease(
    lease: ScriptFromAudioTaskLease,
    *,
    storage: StorageAdapter,
    connection: Callable[[], AbstractContextManager[BusinessConnection]],
) -> None:
    submission_started = False
    work = None

    def before_provider_call() -> None:
        nonlocal submission_started
        with connection() as conn:
            mark_script_from_audio_submission_started(conn, lease=lease)
        submission_started = True

    def checkpoint(provider_task_id: str | None = None) -> None:
        with connection() as conn:
            checkpoint_script_from_audio_task(conn, lease=lease, provider_task_id=provider_task_id)

    try:
        with connection() as conn:
            work = prepare_script_from_audio_task(conn, lease=lease, storage=storage)
        submission_started = work.provider_task_id is not None
        result = perform_script_from_audio_task(
            work,
            before_provider_call=before_provider_call,
            on_submitted=checkpoint,
            heartbeat=checkpoint,
        )
        with connection() as conn:
            complete_script_from_audio_task(
                conn,
                lease=lease,
                result=result,
                audio_deleted=work.audio_deleted,
            )
    except Exception as exc:
        with connection() as conn:
            fail_script_from_audio_task(
                conn,
                lease=lease,
                cause=exc,
                submission_started=submission_started,
                audio_deleted=work.audio_deleted if work is not None else False,
            )


def source_frame_semantic_inspector(
    conn: BusinessConnection,
    *,
    override: SourceFrameQualityInspector | None,
    shared_inspector: FirstFrameQualityInspector | None,
) -> SourceFrameQualityInspector | None:
    if override is not None:
        return override
    if shared_inspector is not None:
        return bounded_source_frame_quality_inspector(shared_inspector)
    try:
        return bounded_source_frame_quality_inspector(get_first_frame_quality_inspector(conn))
    except HTTPException as exc:
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        logger.warning(
            "source-frame semantic scoring unavailable: code=%s",
            detail.get("code", "UNKNOWN"),
        )
        return None


def _record_pg_source_frame_quality_started(
    lease: SourceFrameTaskLease,
    inspector: SourceFrameQualityInspector,
) -> None:
    with pg_transaction() as raw_conn:
        record_source_frame_quality_started(
            BusinessConnection.postgres(raw_conn),
            lease=lease,
            provider=getattr(inspector, "provider_name", None),
            model=getattr(inspector, "model", None),
        )


def _run_source_frame_once(
    conn: BusinessConnection,
    *,
    worker_id: str,
    storage: StorageAdapter,
    extractor: SourceFrameExtractor | None,
    quality_inspector: SourceFrameQualityInspector | None,
    shared_inspector: FirstFrameQualityInspector | None,
) -> bool:
    lease = acquire_source_frame_task(conn, worker_id=worker_id)
    if lease is None:
        return False
    plan = None
    stored = None
    try:
        plan = prepare_source_frame_task(conn, lease=lease)
        semantic_inspector = source_frame_semantic_inspector(
            conn,
            override=quality_inspector,
            shared_inspector=shared_inspector,
        )

        def mark_quality_started() -> None:
            record_source_frame_quality_started(
                conn,
                lease=lease,
                provider=getattr(semantic_inspector, "provider_name", None),
                model=getattr(semantic_inspector, "model", None),
            )

        stored = perform_source_frame_extraction(
            plan,
            storage=storage,
            extractor=extractor or FFmpegSourceFrameExtractor(),
            quality_inspector=semantic_inspector,
            before_quality_call=(mark_quality_started if semantic_inspector is not None else None),
        )
        complete_source_frame_task(
            conn,
            lease=lease,
            plan=plan,
            stored=stored,
        )
    except Exception as exc:
        if plan is not None and stored is not None:
            delete_created_source_frames(
                storage,
                stored.created_assets,
                actor_id=plan.actor.id,
            )
        fail_source_frame_task(conn, lease=lease, cause=exc)
    return True


def _run_pg_source_frame_once(
    *,
    worker_id: str,
    storage: StorageAdapter,
    extractor: SourceFrameExtractor | None,
    quality_inspector: SourceFrameQualityInspector | None,
    shared_inspector: FirstFrameQualityInspector | None,
) -> bool:
    with pg_transaction() as raw_conn:
        lease = acquire_source_frame_task(
            BusinessConnection.postgres(raw_conn),
            worker_id=worker_id,
        )
    if lease is None:
        return False
    plan = None
    stored = None
    try:
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            plan = prepare_source_frame_task(conn, lease=lease)
            from app.usage_billing import accept_operation

            accept_operation(
                conn,
                user_id=lease.created_by_user_id,
                service="quality_inspection",
                source_id=lease.id,
                units=1,
            )
            semantic_inspector = source_frame_semantic_inspector(
                conn,
                override=quality_inspector,
                shared_inspector=shared_inspector,
            )
        with billing_context(lease.id):
            stored = perform_source_frame_extraction(
                plan,
                storage=storage,
                extractor=extractor or FFmpegSourceFrameExtractor(),
                quality_inspector=semantic_inspector,
                before_quality_call=(
                    lambda: _record_pg_source_frame_quality_started(lease, semantic_inspector)
                )
                if semantic_inspector is not None
                else None,
            )
        with pg_transaction() as raw_conn:
            from app.usage_billing import finish_source

            finish_source(BusinessConnection.postgres(raw_conn), lease.id, units=1, succeeded=True)
            complete_source_frame_task(
                BusinessConnection.postgres(raw_conn),
                lease=lease,
                plan=plan,
                stored=stored,
            )
    except Exception as exc:
        if plan is not None and stored is not None:
            delete_created_source_frames(
                storage,
                stored.created_assets,
                actor_id=plan.actor.id,
            )
        with pg_transaction() as raw_conn:
            fail_source_frame_task(
                BusinessConnection.postgres(raw_conn),
                lease=lease,
                cause=exc,
            )
    return True


def _is_quality_settings_failure(exc: HTTPException) -> bool:
    detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
    return str(detail.get("code", "")).startswith("FIRST_FRAME_QUALITY_SETTINGS_")


def _sleep_rate_limit_backoff(exc: Exception, lease: Any) -> None:
    """429 错峰退避：必须在数据库事务外 sleep，避免占着连接等限流窗口。

    full jitter 退避同时形成背压，压住同 worker 后续任务的提交节奏；
    非 429 失败返回 0，不增加任何延迟。
    """
    backoff = rate_limit_backoff_seconds(exc, attempt=int(lease.attempt))
    if backoff > 0:
        time.sleep(backoff)


def _oral_settings_failure(kind: OralWorkKind) -> OralWorkResult:
    if kind.endswith("submit"):
        return OralWorkResult("failed", message="数字人服务未配置，任务未提交")
    if kind == "task_archive":
        return OralWorkResult("failed", message="数字人服务暂不可用，成片归档失败")
    return OralWorkResult("waiting", message="数字人服务暂不可用")


def _run_sqlite_oral_step(
    conn: BusinessConnection,
    *,
    worker_id: str,
    storage: StorageAdapter,
    vendor_override: HiflyClient | None,
) -> bool:
    lease = claim_oral_work(conn, worker_id=worker_id)
    if lease is None:
        return False
    prepared = prepare_oral_work(conn, lease)
    try:
        vendor = vendor_override or hifly_client_from_settings(conn)
    except HiflySettingsUnavailable:
        result = _oral_settings_failure(prepared.kind)
    else:
        result = perform_oral_work(prepared, vendor=vendor, storage=storage)
    try:
        finalize_oral_work(conn, lease=prepared, result=result)
    except OralLeaseLostError:
        discard_uncommitted_oral_asset(
            storage,
            result=result,
            actor_id=str(prepared.row.get("owner_user_id") or "") or None,
        )
        logger.warning("oral worker finalize discarded after lease loss: kind=%s", prepared.kind)
    return True


def _fail_viral_refresh_if_current(
    conn: BusinessConnection, *, lease: ViralRefreshLease, cause: Exception
) -> None:
    try:
        fail_viral_refresh_task(conn, lease=lease, cause=cause)
    except HTTPException as lease_error:
        detail: dict[str, object] = (
            lease_error.detail if isinstance(lease_error.detail, dict) else {}
        )
        if detail.get("code") != "VIRAL_REFRESH_LEASE_LOST":
            raise
        conn.rollback()
        logger.warning("viral refresh failure ignored after lease loss: task=%s", lease.id)


def _run_pg_viral_refresh(lease: ViralRefreshLease, storage: StorageAdapter) -> None:
    from app.viral_collection import run_viral_collection

    try:
        run_viral_collection(lease, storage)
        with pg_transaction() as raw_conn:
            complete_viral_refresh_task(BusinessConnection.postgres(raw_conn), lease=lease)
    except Exception as exc:
        with pg_transaction() as raw_conn:
            _fail_viral_refresh_if_current(
                BusinessConnection.postgres(raw_conn), lease=lease, cause=exc
            )


def run_worker_once(
    conn: BusinessConnection,
    *,
    worker_id: str,
    storage: StorageAdapter,
    generation_storage: StorageAdapter | None = None,
    first_frame_storage: StorageAdapter | None = None,
    character_provider: CharacterImageProvider | None = None,
    analysis_provider: VideoAnalysisProvider | None = None,
    image_provider: ImageProvider | None = None,
    first_frame_quality_inspector: FirstFrameQualityInspector | None = None,
    source_frame_extractor: SourceFrameExtractor | None = None,
    source_frame_quality_inspector: SourceFrameQualityInspector | None = None,
    video_frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
    reconcile_provider: H3Provider | None = None,
    oral_vendor: HiflyClient | None = None,
    max_tasks: int | None = None,
) -> int:
    """Process all currently eligible tasks, then return so SQLite connections stay short-lived."""
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    _cleanup_audio_objects(lambda: _sqlite_audio_connection(conn), storage)
    processed = 0
    while True:
        processed_round = False
        viral_import_lease = acquire_viral_import_task(conn, worker_id=worker_id)
        if viral_import_lease is not None:
            viral_import_work = None
            viral_import_outcome = None
            try:
                viral_import_work = prepare_viral_import_task(
                    conn, lease=viral_import_lease, storage=storage
                )
                viral_import_outcome = perform_viral_import_task(viral_import_work)
                complete_viral_import_task(
                    conn, lease=viral_import_lease, outcome=viral_import_outcome
                )
            except Exception as exc:
                if viral_import_outcome is not None:
                    discard_viral_import_outcome(
                        storage,
                        outcome=viral_import_outcome,
                        actor_id=viral_import_lease.owner_user_id,
                    )
                conn.rollback()
                fail_viral_import_task(conn, lease=viral_import_lease, cause=exc)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if _run_sqlite_oral_step(
            conn,
            worker_id=worker_id,
            storage=storage,
            vendor_override=oral_vendor,
        ):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        generation_handled = False
        try:
            generation_handled = (
                run_next_generation_task(
                    conn,
                    worker_id=worker_id,
                    provider=None,
                    storage=generation_storage or storage,
                    first_frame_storage=first_frame_storage or storage,
                    visual_quality_inspector=first_frame_quality_inspector,
                    video_frame_extractor=video_frame_extractor,
                )
                is not None
            )
        except GenerationTaskSupersededError:
            # L1 (M4M5 review): the task gained a paid replacement
            # mid-flight and the late terminal write was discarded by
            # design — the lease left with the write, so this counts as a
            # processed task, not a worker fault.
            logger.info("generation task superseded while running; late terminal write discarded")
            generation_handled = True
        if generation_handled:
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if (
            run_next_character_generation_task(
                conn,
                worker_id=worker_id,
                provider=character_provider,
                storage=storage,
            )
            is not None
        ):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        analysis_lease = acquire_analysis_task(conn, worker_id=worker_id)
        if analysis_lease is not None:
            try:
                analysis_work = prepare_analysis_task(
                    conn,
                    lease=analysis_lease,
                    storage=storage,
                    provider=analysis_provider,
                )
                with billing_context(analysis_lease.id):
                    analysis_result = perform_analysis_task(analysis_work)
                complete_analysis_task(conn, work=analysis_work, result=analysis_result)
            except Exception as exc:
                fail_analysis_task(conn, lease=analysis_lease, cause=exc)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        script_rewrite_lease = acquire_script_rewrite_task(conn, worker_id=worker_id)
        if script_rewrite_lease is not None:
            submission_started = False
            try:
                rewrite_work = prepare_script_rewrite_task(
                    conn,
                    lease=script_rewrite_lease,
                )
                mark_script_rewrite_submission_started(
                    conn,
                    lease=script_rewrite_lease,
                )
                submission_started = True
                rewrite_result = perform_script_rewrite_task(rewrite_work)
                complete_script_rewrite_task(
                    conn,
                    lease=script_rewrite_lease,
                    result=rewrite_result,
                )
            except Exception as exc:
                fail_script_rewrite_task(
                    conn,
                    lease=script_rewrite_lease,
                    cause=exc,
                    submission_started=submission_started,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        script_from_audio_lease = acquire_script_from_audio_task(conn, worker_id=worker_id)
        if script_from_audio_lease is not None:
            _run_audio_lease(
                script_from_audio_lease,
                storage=storage,
                connection=lambda: _sqlite_audio_connection(conn),
            )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        reconcile_lease = acquire_generation_reconcile_operation(
            conn,
            worker_id=worker_id,
        )
        if reconcile_lease is not None:
            reconcile_work = None
            reconcile_outcome = None
            try:
                reconcile_work = prepare_generation_reconcile_operation(
                    conn,
                    lease=reconcile_lease,
                    provider_factory=lambda active_conn, provider_name: (
                        reconcile_provider
                        or h3_provider_for_task(
                            active_conn, provider_name, task_id=reconcile_lease.task_id
                        )
                    ),
                )
                reconcile_outcome = perform_generation_reconcile_operation(
                    reconcile_work,
                    storage=generation_storage or storage,
                    first_frame_storage=first_frame_storage or storage,
                    visual_quality_inspector=first_frame_quality_inspector,
                    video_frame_extractor=video_frame_extractor,
                )
                complete_generation_reconcile_operation(
                    conn,
                    work=reconcile_work,
                    outcome=reconcile_outcome,
                )
            except Exception as exc:
                fail_generation_reconcile_operation(
                    conn,
                    lease=reconcile_lease,
                    cause=exc,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if _run_source_frame_once(
            conn,
            worker_id=worker_id,
            storage=storage,
            extractor=source_frame_extractor,
            quality_inspector=source_frame_quality_inspector,
            shared_inspector=first_frame_quality_inspector,
        ):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        first_frame_lease = acquire_first_frame_task(conn, worker_id=worker_id)
        if first_frame_lease is not None:
            submission_started = False
            stored = None
            work = None

            def mark_submission_started() -> None:
                nonlocal submission_started
                submission_started = True

            def mark_submission_completed() -> None:
                # The next step archives the paid output and writes a durable
                # checkpoint before quality inspection begins.
                return

            def renew_first_frame_lease(*, quality_phase: bool = False) -> None:
                renew_image_task_lease(
                    conn,
                    table="first_frame_tasks",
                    lease=first_frame_lease,
                    lease_minutes=3 if quality_phase else 30,
                )

            def persist_first_frame_checkpoint(candidates: list[Any]) -> None:
                save_first_frame_task_checkpoint(
                    conn,
                    lease=first_frame_lease,
                    candidates=candidates,
                )

            def persist_first_frame_submission(submission: dict[str, object]) -> None:
                save_first_frame_provider_submission(
                    conn, lease=first_frame_lease, submission=submission
                )

            try:
                prepared = prepare_first_frame_task(
                    conn,
                    lease=first_frame_lease,
                    provider=image_provider or get_image_provider(conn),
                    quality_inspector=first_frame_quality_inspector,
                    quality_inspector_factory=lambda: get_first_frame_quality_inspector(conn),
                )
                record_image_task_provider(
                    conn,
                    table="first_frame_tasks",
                    lease=first_frame_lease,
                    provider=prepared.provider.provider_name,
                    model=prepared.plan.model,
                )
                submission_started = prepared.provider_submission is not None
                with billing_context(first_frame_lease.id):
                    work, stored = run_first_frame_task_outside_transaction(
                        prepared,
                        storage=first_frame_storage or storage,
                        before_provider_call=mark_submission_started,
                        after_provider_call=mark_submission_completed,
                        heartbeat=renew_first_frame_lease,
                        quality_heartbeat=lambda: renew_first_frame_lease(quality_phase=True),
                        checkpoint_candidates=persist_first_frame_checkpoint,
                        save_provider_submission=persist_first_frame_submission,
                    )
                complete_first_frame_task(
                    conn,
                    prepared=prepared,
                    work=work,
                    stored=stored,
                )
            except Exception as exc:
                log_image_task_failure("first_frame_tasks", first_frame_lease, exc)
                if stored is not None and work is not None:
                    from app.first_frames import delete_created_first_frames

                    delete_created_first_frames(
                        first_frame_storage or storage,
                        stored.created_assets,
                        actor_id=work.actor.id,
                    )
                fail_image_task(
                    conn,
                    table="first_frame_tasks",
                    lease=first_frame_lease,
                    cause=exc,
                    submission_started=submission_started,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        character_sheet_lease = acquire_character_sheet_task(conn, worker_id=worker_id)
        if character_sheet_lease is not None:
            submission_started = False
            try:
                prepared_sheet = prepare_character_sheet_task(
                    conn,
                    lease=character_sheet_lease,
                    storage=storage,
                    provider=image_provider or get_image_provider(conn),
                )
                record_image_task_provider(
                    conn,
                    table="character_sheet_tasks",
                    lease=character_sheet_lease,
                    provider=prepared_sheet.provider.provider_name,
                    model=SIMPLE_CONTACT_SHEET_MODEL,
                )
                submission_started = True
                sheet_generation = perform_character_sheet_task(prepared_sheet)
                complete_character_sheet_task(
                    conn,
                    prepared=prepared_sheet,
                    generation=sheet_generation,
                    storage=storage,
                )
            except Exception as exc:
                log_image_task_failure("character_sheet_tasks", character_sheet_lease, exc)
                fail_image_task(
                    conn,
                    table="character_sheet_tasks",
                    lease=character_sheet_lease,
                    cause=exc,
                    submission_started=submission_started,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if not processed_round:
            break
    return processed


def _run_pg_generation_step(
    *,
    lease: dict[str, Any],
    storage: StorageAdapter,
    first_frame_storage: StorageAdapter,
    provider_override: H3Provider | None = None,
    visual_quality_inspector: FirstFrameQualityInspector | None = None,
    video_frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
) -> None:
    """Run exactly one recoverable generation state transition.

    Every database mutation is fenced by its own short ``pg_transaction``.
    Provider POST/query and input object storage calls deliberately happen
    between those transactions, so a slow vendor can never exhaust the API
    connection pool or make the desktop appear offline.
    """

    task_id = str(lease["id"])
    status = str(lease["status"])

    # Legacy archive-retry rows are claimed as SUBMITTING by the shared queue
    # acquisition SQL.  They already have a paid result URL, so move directly
    # to ARCHIVING and never issue another create call.
    if (
        status == "SUBMITTING"
        and lease.get("archive_status") == "ARCHIVE_FAILED"
        and lease.get("provider_result_url")
    ):
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            mark_generation_task_archiving(
                conn,
                lease=lease,
                result_url=str(lease["provider_result_url"]),
            )
            finalize_generation_direct_result(
                conn,
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
            )
        return
    elif status == "SUBMITTING":
        try:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                work = prepare_generation_submission(
                    conn,
                    lease=lease,
                    first_frame_storage=first_frame_storage,
                    provider=provider_override,
                )
        except H3ProviderSettingsUnavailable:
            with pg_transaction() as raw_conn:
                mark_task_provider_settings_unavailable(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                )
            return
        except ProviderRequestContractError:
            # 参数违反供应商契约（如存量 T2V+adaptive），单独归类，
            # 避免误标为首帧 URL 签名失败误导排障。
            with pg_transaction() as raw_conn:
                mark_task_provider_request_contract_failed(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                )
            return
        except (StorageBackendUnavailable, StoragePermissionError, ValueError):
            with pg_transaction() as raw_conn:
                mark_task_first_frame_url_sign_failed(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                )
            return

        with pg_transaction() as raw_conn:
            cost_conn = BusinessConnection.postgres(raw_conn)
            cost_task = cost_conn.execute(
                "SELECT cost_rate_subject_snapshot FROM generation_tasks WHERE id=%s", (task_id,)
            ).fetchone()
            if cost_task and cost_task[0]:
                begin_operation_cost(
                    cost_conn,
                    source_type="generation_task",
                    source_id=f"{task_id}:{lease['attempt']}",
                    subject=str(cost_task[0]),
                    generation_task_id=task_id,
                )
        if isinstance(work.provider, MetasoH3Provider):
            created_task_ids: list[str] = []

            def persist_created_task(provider_task_id: str) -> None:
                created_task_ids.append(provider_task_id)
                with pg_transaction() as raw_conn:
                    mark_generation_task_running(
                        BusinessConnection.postgres(raw_conn),
                        lease=lease,
                        provider_task_id=provider_task_id,
                        provider_request=work.provider_request,
                        request_hash=work.request_hash,
                    )

            work.provider.task_created_observer = persist_created_task
            try:
                work.provider.submit_image_to_video(work.provider_request)
            except SubmissionUncertain as exc:
                with pg_transaction() as raw_conn:
                    mark_task_submission_uncertain(
                        BusinessConnection.postgres(raw_conn),
                        task_id=task_id,
                        message=str(exc),
                        provider_task_id=created_task_ids[-1] if created_task_ids else None,
                    )
            except H3ProviderFailed as exc:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    if exc.provider_task_id is not None and not exc.terminal:
                        mark_task_submission_uncertain(
                            conn,
                            task_id=task_id,
                            message="Provider submission needs reconciliation.",
                            provider_task_id=exc.provider_task_id,
                        )
                    else:
                        mark_task_provider_failed(
                            conn,
                            lease=lease,
                            provider_task_id=exc.provider_task_id,
                        )
            except Exception:
                # If the provider id was observed but its persistence callback
                # failed, quarantine with that id.  A later reconciliation may
                # recover it, while an automatic paid resubmit is forbidden.
                if created_task_ids:
                    with pg_transaction() as raw_conn:
                        mark_task_submission_uncertain(
                            BusinessConnection.postgres(raw_conn),
                            task_id=task_id,
                            message="Provider task was created but persistence failed.",
                            provider_task_id=created_task_ids[-1],
                        )
                    return
                raise
            return

        # Fake/test providers remain synchronous, but all provider and storage
        # I/O is still outside PostgreSQL transactions.
        try:
            result = work.provider.create_image_to_video(work.provider_request)
        except SubmissionUncertain as exc:
            with pg_transaction() as raw_conn:
                mark_task_submission_uncertain(
                    BusinessConnection.postgres(raw_conn),
                    task_id=task_id,
                    message=str(exc),
                )
            return
        except H3ProviderFailed as exc:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                mark_task_provider_failed(
                    conn,
                    lease=lease,
                    provider_task_id=exc.provider_task_id,
                )
            return
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            mark_generation_task_running(
                conn,
                lease=lease,
                provider_task_id=result.provider_task_id,
                provider_request=work.provider_request,
                request_hash=work.request_hash,
                release_lease=False,
            )
            mark_generation_task_archiving(
                conn,
                lease=lease,
                result_url=result.result_url,
            )
            finalize_generation_direct_result(
                conn,
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
                output_seconds=result.output_seconds,
            )
        return

    if status == "RUNNING":
        provider_task_id = str(lease["provider_task_id"])
        try:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                provider = provider_override or h3_provider_for_task(
                    conn, str(lease["provider"]), task_id=str(lease["id"])
                )
        except H3ProviderSettingsUnavailable:
            with pg_transaction() as raw_conn:
                reschedule_generation_poll(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                    delay_seconds=60,
                )
            return
        try:
            query = provider.query_image_to_video(provider_task_id)
        except H3ProviderFailed as exc:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                if exc.terminal:
                    mark_task_provider_failed(
                        conn,
                        lease=lease,
                        provider_task_id=provider_task_id,
                    )
                else:
                    reschedule_generation_poll(conn, lease=lease, delay_seconds=15)
            return
        if query.status == "RUNNING":
            with pg_transaction() as raw_conn:
                reschedule_generation_poll(BusinessConnection.postgres(raw_conn), lease=lease)
            return
        if query.status in {"FAILED", "CANCELLED"}:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                mark_task_provider_failed(
                    conn,
                    lease=lease,
                    provider_task_id=provider_task_id,
                )
            return
        if query.result_url is None:
            with pg_transaction() as raw_conn:
                reschedule_generation_poll(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                    delay_seconds=15,
                )
            return
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            mark_generation_task_archiving(
                conn,
                lease=lease,
                result_url=query.result_url,
            )
            finalize_generation_direct_result(
                conn,
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
                output_seconds=query.output_seconds,
            )
        return

    if status == "ARCHIVING":
        with pg_transaction() as raw_conn:
            finalize_generation_direct_result(
                BusinessConnection.postgres(raw_conn),
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
            )


_RECONCILE_INTERVAL_ENV = "VIDEO_REPLICA_RECONCILE_INTERVAL_SECONDS"
_DEFAULT_RECONCILE_INTERVAL_SECONDS = 30.0
# 对账专用的 advisory lock 命名空间。用事务级（xact）变体，事务结束即释放，
# 不需要也不应该显式 unlock。沿用项目既有约定：hashtext('域:用途') 字符串，
# 与 payment:settings / billing:tariffs 同构。
_RECONCILE_LOCK_NAMESPACE = "billing:reconcile"

_WORKER_CONCURRENCY_ENV = "VIDEO_REPLICA_WORKER_CONCURRENCY"

_reconcile_deadline = 0.0


def _reconcile_interval_seconds() -> float:
    """对账周期秒数；0 表示每轮都尝试（等价旧行为，仅用于排障）。"""
    raw = os.environ.get(_RECONCILE_INTERVAL_ENV, "").strip()
    if not raw:
        return _DEFAULT_RECONCILE_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "invalid %s=%r, falling back to %.0fs",
            _RECONCILE_INTERVAL_ENV,
            raw,
            _DEFAULT_RECONCILE_INTERVAL_SECONDS,
        )
        return _DEFAULT_RECONCILE_INTERVAL_SECONDS


def _reconcile_due() -> bool:
    """进程内节流：到点才返回 True，并顺延下一次截止时间。"""
    global _reconcile_deadline
    now = time.monotonic()
    if now < _reconcile_deadline:
        return False
    _reconcile_deadline = now + _reconcile_interval_seconds()
    return True


def _worker_concurrency(cli_value: int | None = None) -> int:
    """单进程可同时推进的任务数：CLI 优先，其次环境变量，默认 1（串行）。"""
    if cli_value is not None:
        return max(1, cli_value)
    raw = os.environ.get(_WORKER_CONCURRENCY_ENV, "").strip()
    if not raw:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("invalid %s=%r, falling back to 1", _WORKER_CONCURRENCY_ENV, raw)
        return 1


def run_pg_worker_once(
    *,
    worker_id: str,
    storage: StorageAdapter,
    generation_storage: StorageAdapter | None = None,
    first_frame_storage: StorageAdapter | None = None,
    character_provider: CharacterImageProvider | None = None,
    analysis_provider: VideoAnalysisProvider | None = None,
    generation_provider: H3Provider | None = None,
    image_provider: ImageProvider | None = None,
    first_frame_quality_inspector: FirstFrameQualityInspector | None = None,
    source_frame_extractor: SourceFrameExtractor | None = None,
    source_frame_quality_inspector: SourceFrameQualityInspector | None = None,
    video_frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
    oral_vendor: HiflyClient | None = None,
    max_tasks: int | None = None,
) -> int:
    """Process all currently eligible tasks on the PostgreSQL lane.

    PostgreSQL generation is a durable multi-step state machine.  Claim and
    state writes use short fenced transactions; paid submit, one status poll,
    provider calls run with no database transaction open; generated media is not processed.
    Existing RUNNING/ARCHIVING work is always resumed before a new task, so a
    worker crash cannot turn a known provider task into a second paid POST.
    """
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    _cleanup_audio_objects(_pg_audio_connection, storage)
    processed = 0
    from app.usage_billing import reconcile_operations

    while True:
        # 对账是兜底维护，不是任务路径。旧实现放在循环顶部无条件执行，空转时
        # 每个 worker 每秒扫一遍 11 张表，多实例还会成倍重复。现在按周期节流，
        # 并用事务级 advisory lock 保证同一时刻全集群只有一个 worker 真正在跑；
        # 拿不到锁说明别人正在对账，本轮直接跳过。
        if _reconcile_due():
            with pg_transaction() as raw_conn:
                reconcile_conn = BusinessConnection.postgres(raw_conn)
                if reconcile_conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                    (_RECONCILE_LOCK_NAMESPACE,),
                ).fetchone()[0]:
                    reconcile_operations(reconcile_conn)
        processed_round = False
        from app.prompt_optimizer import run_prompt_task

        if run_prompt_task(_pg_audio_connection, worker_id=worker_id, storage=storage):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            viral_import_lease = acquire_viral_import_task(
                BusinessConnection.postgres(raw_conn), worker_id=worker_id
            )
        if viral_import_lease is not None:
            viral_import_work = None
            viral_import_outcome = None
            viral_import_ready_to_commit = False
            try:
                with pg_transaction() as raw_conn:
                    viral_import_work = prepare_viral_import_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=viral_import_lease,
                        storage=storage,
                    )
                viral_import_outcome = perform_viral_import_task(viral_import_work)
                with pg_transaction() as raw_conn:
                    complete_viral_import_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=viral_import_lease,
                        outcome=viral_import_outcome,
                    )
                    # A lost COMMIT acknowledgement can still mean this asset exists.
                    # Keep its immutable project copy when the transaction outcome is unknown.
                    viral_import_ready_to_commit = True
            except Exception as exc:
                if viral_import_outcome is not None and not viral_import_ready_to_commit:
                    discard_viral_import_outcome(
                        storage,
                        outcome=viral_import_outcome,
                        actor_id=viral_import_lease.owner_user_id,
                    )
                with pg_transaction() as raw_conn:
                    import_conn = BusinessConnection.postgres(raw_conn)
                    fail_viral_import_task(
                        import_conn,
                        lease=viral_import_lease,
                        cause=exc,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            oral_lease = claim_oral_work(BusinessConnection.postgres(raw_conn), worker_id=worker_id)
        if oral_lease is not None:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                prepared_oral = prepare_oral_work(conn, oral_lease)
                try:
                    resolved_oral_vendor = oral_vendor or hifly_client_from_settings(conn)
                except HiflySettingsUnavailable:
                    resolved_oral_vendor = None
            oral_attempt_id = None
            if resolved_oral_vendor is not None and prepared_oral.kind.endswith("submit"):
                from app.usage_billing import begin_source_attempt

                with pg_transaction() as raw_conn:
                    oral_attempt_id = begin_source_attempt(
                        BusinessConnection.postgres(raw_conn), prepared_oral.record_id
                    )
            if resolved_oral_vendor is None:
                oral_result = _oral_settings_failure(prepared_oral.kind)
            else:
                oral_result = perform_oral_work(
                    prepared_oral,
                    vendor=resolved_oral_vendor,
                    storage=storage,
                )
            if oral_attempt_id:
                from app.usage_billing import complete_attempt

                if prepared_oral.kind != "task_submit" and oral_result.outcome == "submitted":
                    with pg_transaction() as raw_conn:
                        complete_attempt(
                            BusinessConnection.postgres(raw_conn),
                            attempt_id=oral_attempt_id,
                            usage=1,
                        )
                elif oral_result.outcome in {"failed", "uncertain"}:
                    with pg_transaction() as raw_conn:
                        complete_attempt(
                            BusinessConnection.postgres(raw_conn),
                            attempt_id=oral_attempt_id,
                            usage=None,
                        )
            try:
                with pg_transaction() as raw_conn:
                    finalize_oral_work(
                        BusinessConnection.postgres(raw_conn),
                        lease=prepared_oral,
                        result=oral_result,
                    )
            except OralLeaseLostError:
                discard_uncommitted_oral_asset(
                    storage,
                    result=oral_result,
                    actor_id=(str(prepared_oral.row.get("owner_user_id") or "") or None),
                )
                logger.warning(
                    "oral worker finalize discarded after lease loss: kind=%s",
                    prepared_oral.kind,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            lease = acquire_generation_continuation_lease(conn, worker_id=worker_id)
            if lease is None:
                lease = acquire_generation_task_lease(conn, worker_id=worker_id)
        if lease is not None:
            try:
                with billing_context(str(lease["id"])):
                    _run_pg_generation_step(
                        lease=lease,
                        storage=generation_storage or storage,
                        first_frame_storage=first_frame_storage or storage,
                        provider_override=generation_provider,
                        visual_quality_inspector=first_frame_quality_inspector,
                        video_frame_extractor=video_frame_extractor,
                    )
            except GenerationTaskSupersededError:
                # L1 (M4M5 review): the task gained a paid replacement
                # mid-flight and the late terminal write was discarded by
                # design — the lease left with the write, so this counts
                # as a processed task, not a worker fault.
                logger.info(
                    "generation task superseded while running; late terminal write discarded"
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            char_lease = acquire_character_generation_task(conn, worker_id=worker_id)
        if char_lease is not None:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                if (
                    run_next_character_generation_task(
                        conn,
                        worker_id=worker_id,
                        provider=character_provider,
                        storage=storage,
                        lease=char_lease,
                    )
                    is not None
                ):
                    processed += 1
                    processed_round = True
                    if max_tasks is not None and processed >= max_tasks:
                        return processed
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            analysis_lease = acquire_analysis_task(conn, worker_id=worker_id)
        if analysis_lease is not None:
            analysis_cost_id = ""
            analysis_cost_usage: float | None = None

            def record_analysis_response() -> None:
                nonlocal analysis_cost_usage
                analysis_cost_usage = 1
                with pg_transaction() as raw_conn:
                    complete_operation_cost(
                        BusinessConnection.postgres(raw_conn),
                        record_id=analysis_cost_id,
                        usage_amount=1,
                    )

            try:
                # Preparation only reads settings/asset state and creates the
                # short-lived signed URL.  The paid provider call below runs
                # after this transaction has committed.
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    analysis_work = prepare_analysis_task(
                        conn,
                        lease=analysis_lease,
                        storage=storage,
                        provider=analysis_provider,
                    )
                    analysis_cost_id = begin_operation_cost(
                        conn,
                        source_type="analysis_task",
                        source_id=f"{analysis_lease.id}:{analysis_lease.attempt}",
                        subject="video_analysis_768p",
                        user_id=analysis_lease.created_by_user_id,
                        resolution="768P",
                        metadata={"resolution_basis": "default_generation_tier"},
                    )
                with billing_context(analysis_lease.id):
                    analysis_result = perform_analysis_task(
                        analysis_work, on_provider_result=record_analysis_response
                    )
                analysis_cost_usage = 1
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(
                        conn,
                        record_id=analysis_cost_id,
                        usage_amount=1,
                    )
                    complete_analysis_task(
                        conn,
                        work=analysis_work,
                        result=analysis_result,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(
                        conn, record_id=analysis_cost_id, usage_amount=analysis_cost_usage
                    )
                    fail_analysis_task(conn, lease=analysis_lease, cause=exc)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            script_rewrite_lease = acquire_script_rewrite_task(
                BusinessConnection.postgres(raw_conn),
                worker_id=worker_id,
            )
        if script_rewrite_lease is not None:
            submission_started = False
            try:
                with pg_transaction() as raw_conn:
                    rewrite_work = prepare_script_rewrite_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=script_rewrite_lease,
                    )
                with pg_transaction() as raw_conn:
                    mark_script_rewrite_submission_started(
                        BusinessConnection.postgres(raw_conn),
                        lease=script_rewrite_lease,
                    )
                submission_started = True
                rewrite_result = perform_script_rewrite_task(rewrite_work)
                with pg_transaction() as raw_conn:
                    complete_script_rewrite_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=script_rewrite_lease,
                        result=rewrite_result,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    fail_script_rewrite_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=script_rewrite_lease,
                        cause=exc,
                        submission_started=submission_started,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            audio_lease = acquire_script_from_audio_task(
                BusinessConnection.postgres(raw_conn),
                worker_id=worker_id,
            )
        if audio_lease is not None:
            _run_audio_lease(audio_lease, storage=storage, connection=_pg_audio_connection)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            reconcile_lease = acquire_generation_reconcile_operation(
                BusinessConnection.postgres(raw_conn),
                worker_id=worker_id,
            )
        if reconcile_lease is not None:
            reconcile_work = None
            reconcile_outcome = None
            try:
                with pg_transaction() as raw_conn:
                    reconcile_work = prepare_generation_reconcile_operation(
                        BusinessConnection.postgres(raw_conn),
                        lease=reconcile_lease,
                        provider_factory=lambda active_conn, provider_name: (
                            generation_provider
                            or h3_provider_for_task(
                                active_conn, provider_name, task_id=reconcile_lease.task_id
                            )
                        ),
                    )
                with billing_context(reconcile_lease.task_id):
                    reconcile_outcome = perform_generation_reconcile_operation(
                        reconcile_work,
                        storage=generation_storage or storage,
                        first_frame_storage=first_frame_storage or storage,
                        visual_quality_inspector=first_frame_quality_inspector,
                        video_frame_extractor=video_frame_extractor,
                    )
                with pg_transaction() as raw_conn:
                    complete_generation_reconcile_operation(
                        BusinessConnection.postgres(raw_conn),
                        work=reconcile_work,
                        outcome=reconcile_outcome,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    fail_generation_reconcile_operation(
                        BusinessConnection.postgres(raw_conn),
                        lease=reconcile_lease,
                        cause=exc,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if _run_pg_source_frame_once(
            worker_id=worker_id,
            storage=storage,
            extractor=source_frame_extractor,
            quality_inspector=source_frame_quality_inspector,
            shared_inspector=first_frame_quality_inspector,
        ):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            first_frame_lease = acquire_first_frame_task(
                BusinessConnection.postgres(raw_conn), worker_id=worker_id
            )
        if first_frame_lease is not None:
            submission_started = False
            stored = None
            work = None
            first_frame_cost_id = ""
            first_frame_call_number = 0
            has_provider_receipt = False

            def mark_pg_submission_started() -> None:
                nonlocal submission_started, first_frame_cost_id, first_frame_call_number
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    # A retried transport response has no proven output count.
                    if first_frame_cost_id:
                        complete_operation_cost(
                            conn, record_id=first_frame_cost_id, usage_amount=None
                        )
                    first_frame_call_number += 1
                    first_frame_cost_id = begin_operation_cost(
                        conn,
                        source_type="first_frame_task",
                        source_id=f"{first_frame_lease.id}:{first_frame_lease.attempt}:{first_frame_call_number}",
                        subject="first_frame_image",
                        user_id=first_frame_lease.created_by_user_id,
                    )
                submission_started = True

            def record_pg_generated_images(count: int) -> None:
                nonlocal first_frame_cost_id
                # Record paid output before storage/QC, which can fail or resume
                # entirely from checkpoints without another provider call.
                with pg_transaction() as raw_conn:
                    complete_operation_cost(
                        BusinessConnection.postgres(raw_conn),
                        record_id=first_frame_cost_id,
                        usage_amount=count,
                    )
                first_frame_cost_id = ""

            def renew_pg_first_frame_lease(*, quality_phase: bool = False) -> None:
                with pg_transaction() as raw_conn:
                    renew_image_task_lease(
                        BusinessConnection.postgres(raw_conn),
                        table="first_frame_tasks",
                        lease=first_frame_lease,
                        lease_minutes=3 if quality_phase else 30,
                    )

            def persist_pg_first_frame_checkpoint(candidates: list[Any]) -> None:
                with pg_transaction() as raw_conn:
                    save_first_frame_task_checkpoint(
                        BusinessConnection.postgres(raw_conn),
                        lease=first_frame_lease,
                        candidates=candidates,
                    )

            def persist_pg_first_frame_submission(submission: dict[str, object]) -> None:
                nonlocal has_provider_receipt
                with pg_transaction() as raw_conn:
                    save_first_frame_provider_submission(
                        BusinessConnection.postgres(raw_conn),
                        lease=first_frame_lease,
                        submission=submission,
                        cost_record_id=first_frame_cost_id,
                    )
                has_provider_receipt = True

            try:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    prepared = prepare_first_frame_task(
                        conn,
                        lease=first_frame_lease,
                        provider=image_provider or get_image_provider(conn),
                        quality_inspector=first_frame_quality_inspector,
                        quality_inspector_factory=lambda: get_first_frame_quality_inspector(conn),
                    )
                    record_image_task_provider(
                        conn,
                        table="first_frame_tasks",
                        lease=first_frame_lease,
                        provider=prepared.provider.provider_name,
                        model=prepared.plan.model,
                    )
                    if prepared.provider_submission is not None:
                        has_provider_receipt = submission_started = True
                        receipt_cost_id = str(prepared.provider_submission["cost_record_id"])
                        receipt_cost = conn.execute(
                            "SELECT status FROM operation_cost_records WHERE id=%s AND "
                            "source_type='first_frame_task' AND source_id LIKE %s AND user_id=%s",
                            (
                                receipt_cost_id,
                                f"{first_frame_lease.id}:%",
                                first_frame_lease.created_by_user_id,
                            ),
                        ).fetchone()
                        if receipt_cost is None:
                            raise RuntimeError("first-frame receipt cost binding is missing")
                        if receipt_cost["status"] == "PENDING":
                            first_frame_cost_id = receipt_cost_id
                with billing_context(first_frame_lease.id):
                    work, stored = run_first_frame_task_outside_transaction(
                        prepared,
                        storage=first_frame_storage or storage,
                        before_provider_call=mark_pg_submission_started,
                        on_generated_images=record_pg_generated_images,
                        heartbeat=renew_pg_first_frame_lease,
                        quality_heartbeat=lambda: renew_pg_first_frame_lease(quality_phase=True),
                        checkpoint_candidates=persist_pg_first_frame_checkpoint,
                        save_provider_submission=persist_pg_first_frame_submission,
                    )
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_first_frame_task(
                        conn,
                        prepared=prepared,
                        work=work,
                        stored=stored,
                    )
            except Exception as exc:
                log_image_task_failure("first_frame_tasks", first_frame_lease, exc)
                _sleep_rate_limit_backoff(exc, first_frame_lease)
                if stored is not None and work is not None:
                    from app.first_frames import delete_created_first_frames

                    delete_created_first_frames(
                        first_frame_storage or storage,
                        stored.created_assets,
                        actor_id=work.actor.id,
                    )
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    # The same receipt can resume after transient polling/storage failures.
                    # Keep its original cost pending until that recovery settles.
                    known_failure = isinstance(exc, HTTPException) and exc.status_code < 500
                    if not (
                        has_provider_receipt and first_frame_lease.attempt < 2 and not known_failure
                    ):
                        complete_operation_cost(
                            conn, record_id=first_frame_cost_id, usage_amount=None
                        )
                    fail_image_task(
                        conn,
                        table="first_frame_tasks",
                        lease=first_frame_lease,
                        cause=exc,
                        submission_started=submission_started,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            character_sheet_lease = acquire_character_sheet_task(
                BusinessConnection.postgres(raw_conn), worker_id=worker_id
            )
        if character_sheet_lease is not None:
            submission_started = False
            character_sheet_cost_id = ""
            character_sheet_cost_usage: int | None = None
            try:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    prepared_sheet = prepare_character_sheet_task(
                        conn,
                        lease=character_sheet_lease,
                        storage=storage,
                        provider=image_provider or get_image_provider(conn),
                    )
                    record_image_task_provider(
                        conn,
                        table="character_sheet_tasks",
                        lease=character_sheet_lease,
                        provider=prepared_sheet.provider.provider_name,
                        model=SIMPLE_CONTACT_SHEET_MODEL,
                    )
                    character_sheet_cost_id = begin_operation_cost(
                        conn,
                        source_type="character_sheet_task",
                        source_id=f"{character_sheet_lease.id}:{character_sheet_lease.attempt}",
                        subject="character_sheet_image",
                        user_id=character_sheet_lease.created_by_user_id,
                    )
                submission_started = True
                sheet_generation = perform_character_sheet_task(prepared_sheet)
                character_sheet_cost_usage = 1
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(conn, record_id=character_sheet_cost_id, usage_amount=1)
                    complete_character_sheet_task(
                        conn,
                        prepared=prepared_sheet,
                        generation=sheet_generation,
                        storage=storage,
                    )
            except Exception as exc:
                log_image_task_failure("character_sheet_tasks", character_sheet_lease, exc)
                _sleep_rate_limit_backoff(exc, character_sheet_lease)
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(
                        conn,
                        record_id=character_sheet_cost_id,
                        usage_amount=character_sheet_cost_usage,
                    )
                    fail_image_task(
                        conn,
                        table="character_sheet_tasks",
                        lease=character_sheet_lease,
                        cause=exc,
                        submission_started=submission_started,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if not processed_round:
            break
    return processed


def run_pg_collection_once(*, worker_id: str, storage: StorageAdapter) -> int:
    """Dedicated collector: never run in the customer generation worker pool.

    每轮三件事，顺序有先后：
    1. 定时采集入队——按 ``next_collection_at`` 到点自动入队（关键词采集 +
       共享账单批次），恢复 P1 前被停用的调度；
    2. 共享账单结算——把上一轮已确认的采集请求按成员扣费（供应商一次、客户
       按成员扣，见 ``viral_collection_billing.settle_collection_charges``）；
    3. 消费一个在队任务（关键词采集 / admin 单条归档 / 失败重试）。
    供应商成本仍由 ``meter_call`` 记平台单；客户侧按共享账单批次快照价扣分。
    """
    from app.viral_collection import enqueue_due_viral_collections
    from app.viral_collection_billing import settle_collection_charges

    with pg_transaction() as raw:
        enqueue_due_viral_collections(BusinessConnection.postgres(raw))
    settle_collection_charges()
    with pg_transaction() as raw:
        lease = acquire_viral_refresh_task(BusinessConnection.postgres(raw), worker_id=worker_id)
    if lease is None:
        return 0
    _run_pg_viral_refresh(lease, storage)
    return 1


# Content-addressable storage only ever *schedules* byte removal: dropping a
# reference to zero sets ``reclaim_after``, and something has to come back later
# and finish the job. Without this sweep the registry grows forever and deleted
# assets leak their bytes permanently. It runs on an interval rather than every
# round because the grace window is 24h — there is no reason to pay for the scan
# more often than that, and ``FOR UPDATE SKIP LOCKED`` already keeps concurrent
# workers from fighting over the same row.
CONTENT_RECLAIM_INTERVAL_SECONDS = 3600.0
_last_content_reclaim_at = 0.0


def reclaim_expired_content_objects_throttled(storage: StorageAdapter) -> dict[str, int]:
    """Run the reclaim sweep at most once per hour per worker process."""
    global _last_content_reclaim_at
    now = time.monotonic()
    if now - _last_content_reclaim_at < CONTENT_RECLAIM_INTERVAL_SECONDS:
        return {}
    _last_content_reclaim_at = now
    try:
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            result = content_store.reclaim_expired_content_objects(conn, storage)
    except Exception:  # noqa: BLE001 - reclaiming is maintenance, never block work
        logger.warning("content reclaim sweep failed", exc_info=True)
        return {}
    if result.get("deleted") or result.get("failed"):
        logger.info(
            "content reclaim sweep deleted=%s failed=%s skipped=%s",
            result["deleted"],
            result["failed"],
            result["skipped"],
        )
    return result


def run_pg_worker_round(
    *, worker_id: str, max_tasks: int | None = None, viral_collection: bool = False
) -> int:
    try:
        # The media-storage configuration lives in the business database;
        # read it once per round inside a short fenced transaction.
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            asset_storage = get_media_storage(conn)
        reclaim_expired_content_objects_throttled(asset_storage)
        if viral_collection:
            return run_pg_collection_once(worker_id=worker_id, storage=asset_storage)
        return run_pg_worker_once(
            worker_id=worker_id,
            storage=asset_storage,
            generation_storage=asset_storage,
            first_frame_storage=asset_storage,
            max_tasks=max_tasks,
        )
    except HTTPException as exc:
        if not _is_quality_settings_failure(exc):
            raise
        logger.warning("visual quality settings unavailable; processing source frames locally")
        with pg_transaction() as raw_conn:
            asset_storage = get_media_storage(BusinessConnection.postgres(raw_conn))
        return int(
            _run_pg_source_frame_once(
                worker_id=worker_id,
                storage=asset_storage,
                extractor=None,
                quality_inspector=None,
                shared_inspector=None,
            )
        )


def run_forever_pg(
    *,
    worker_id: str,
    idle_seconds: float,
    viral_collection: bool = False,
    concurrency: int = 1,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    """常驻循环。

    concurrency=1 与历史行为完全一致：单线程串行，每轮推进一个任务。
    concurrency>1 时用线程池并行推进：每个线程各走一整轮（领取 → 调用上游 →
    落库），线程间不共享事务，也不会重复领取同一条任务——领取路径依赖数据库
    上的 FOR UPDATE SKIP LOCKED，本就不要求单线程。

    计费与结算链路（accept_operation / finish_operation / finalize_*_billing）
    不做任何改动：其正确性来自事务、唯一键与条件更新，同样不依赖单线程前提。

    并发度须不超过 VIDEO_REPLICA_PG_POOL_MAX，否则线程会在连接池上排队等待。

    ``stop_requested``（上线评审 P1-3）：优雅停机开关。SIGTERM 后**正在进行的
    一轮照常跑完**（付费调用的提交/结算绝不在一半停下），但不再开始新一轮；
    任务若在停机中被认领，认领即提交的 SUBMITTING 保证崩溃恢复语义不变。
    """

    def _stopping() -> bool:
        return stop_requested is not None and stop_requested()

    if concurrency <= 1:
        _run_forever_serial(
            worker_id=worker_id,
            idle_seconds=idle_seconds,
            viral_collection=viral_collection,
            stop_requested=_stopping,
        )
        return

    logger.info("generation worker concurrency=%d instance=%s", concurrency, worker_id)
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="gen-worker") as pool:
        while not _stopping():
            futures = [
                pool.submit(
                    run_pg_worker_round,
                    worker_id=f"{worker_id}-{slot}",
                    viral_collection=viral_collection,
                )
                for slot in range(concurrency)
            ]
            processed = 0
            for future in as_completed(futures):
                try:
                    processed += int(future.result())
                except HTTPException as exc:
                    code = exc.detail.get("code") if isinstance(exc.detail, dict) else exc.detail
                    logger.error("generation worker configuration unavailable: %s", code)
                except Exception:
                    logger.exception("generation worker iteration failed")
            if processed == 0:
                _idle_wait(idle_seconds, _stopping)


def _idle_wait(idle_seconds: float, stopping: Callable[[], bool]) -> None:
    """有界空闲等待：停机请求最多 0.2 秒内被观察到（信号只设无锁标志）。"""
    deadline = time.monotonic() + max(0.0, idle_seconds)
    while not stopping():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 0.2))


def _run_forever_serial(
    *,
    worker_id: str,
    idle_seconds: float,
    viral_collection: bool,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    """历史单线程循环（行为保持不变，仅新增停机开关）。"""
    while stop_requested is None or not stop_requested():
        try:
            processed = run_pg_worker_round(worker_id=worker_id, viral_collection=viral_collection)
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else exc.detail
            logger.error("generation worker configuration unavailable: %s", code)
            processed = 0
        except Exception:
            logger.exception("generation worker iteration failed")
            processed = 0
        if processed == 0:
            _idle_wait(idle_seconds, lambda: stop_requested is not None and stop_requested())


@contextmanager
def _shutdown_signals() -> Iterator[Callable[[], bool]]:
    """SIGINT/SIGTERM 只置位不中断：跑完在途一轮，然后退出、不再开新一轮。

    与 publish_worker 同款（M1 评审对齐）：Event.set() 在信号打断其不可重入
    条件锁时可能死锁，所以处理器只写一个 nonlocal 布尔。
    """
    previous: dict[int, Any] = {}
    requested = False

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal requested
        requested = True

    def is_requested() -> bool:
        return requested

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        yield is_requested
    finally:
        for restored_signum, handler in previous.items():
            signal.signal(restored_signum, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local Video Replica generation tasks")
    parser.add_argument(
        "--once", action="store_true", help="process current eligible tasks then exit"
    )
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument(
        "--viral-collection",
        action="store_true",
        help="run only explicitly queued viral archive tasks (scheduled collection is disabled)",
    )
    parser.add_argument(
        "--worker-id", help="logical worker label; each startup adds a unique suffix"
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        help="with --once, stop after processing this many generation/character tasks",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help=(
            "tasks this process advances in parallel; overrides "
            f"${_WORKER_CONCURRENCY_ENV}, default 1 (serial)"
        ),
    )
    args = parser.parse_args()
    if args.concurrency is not None and args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.max_tasks is not None and not args.once:
        parser.error("--max-tasks requires --once")

    worker_id = new_worker_instance_id("generation-worker", args.worker_id)
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s worker_id={worker_id} %(message)s",
    )
    logger.info("generation worker starting instance=%s", worker_id)

    # T05: resolve the database mode first so customer production fails closed
    # before any SQLite file is touched.
    config = resolve_database_config()
    validate_customer_production(config)
    from app.bootstrap import (
        assert_customer_production_security,
        check_customer_production_runtime_dependencies,
        is_customer_production,
    )

    assert_customer_production_security()
    if config.mode is DatabaseMode.POSTGRESQL:
        # PG worker runtime (T25/T26): prove readiness (pool + server round-trip),
        # then run the fair-queue task loop. Each task runs in the two-phase
        # claim/work shape of run_pg_worker_once (M5 review P1-1): the claim
        # transaction COMMITS the SUBMITTING transition, so a crash after the
        # claim does NOT roll the lease back — the expiry sweeper moves the
        # task to SUBMISSION_UNCERTAIN (a manual reconciliation gate) and a
        # paid provider call is never silently re-fired.
        if is_customer_production():
            ready = check_customer_production_runtime_dependencies()
        else:
            ready = check_pg_ready()
        if ready is None:  # pragma: no cover - customer gate always returns PG info
            raise RuntimeError("PostgreSQL worker readiness check returned no result")
        logger.info(
            "PostgreSQL worker runtime ready (pool_max=%d, server_now=%s)",
            ready.pool_size,
            ready.server_now.isoformat(),
        )
        if args.once:
            try:
                processed = run_pg_worker_round(
                    worker_id=worker_id,
                    max_tasks=args.max_tasks,
                    viral_collection=args.viral_collection,
                )
            finally:
                close_pg_pool()
                logger.info("generation worker stopped instance=%s", worker_id)
            logger.info("PostgreSQL worker processed %s task(s)", processed)
            return
        try:
            with _shutdown_signals() as stop_requested:
                run_forever_pg(
                    worker_id=worker_id,
                    idle_seconds=args.idle_seconds,
                    viral_collection=args.viral_collection,
                    concurrency=_worker_concurrency(args.concurrency),
                    stop_requested=stop_requested,
                )
        finally:
            close_pg_pool()
            logger.info("generation worker stopped instance=%s", worker_id)
        return

    # CW-025: resolve_database_config() 全环境 fail-closed 后，SQLite 分支 unreachable。
    # CW-030 已移除 Worker 的 SQLite 业务入口（run_sqlite_worker_round/run_forever）；
    # 本 RuntimeError 是防御性断言：如果走到这里，说明 resolve_database_config() 有 bug。
    raise RuntimeError(
        "generation_worker: SQLite online path is unreachable after CW-025; "
        "this indicates a bug in resolve_database_config(). "
        "Worker SQLite business logic removal is tracked by CW-030."
    )


if __name__ == "__main__":
    main()
