"""CW-030 — per-worker-class PG claim / lease / recovery matrix.

Every formally supported task class is verified on the real PostgreSQL lane
through the exact claim/complete/fail functions the production PG worker loop
(``run_pg_worker_once``) uses, in fenced ``pg_transaction`` blocks:

- 独立任务 (independent ``generation_batches`` riding the shared generation
  machinery): 2-worker double claim, expired lease → SUBMISSION_UNCERTAIN +
  slot release, late duplicate uncertainty cannot disturb a replacement task,
  worker settle with exactly-once billing, crash after claim leaves a durable
  RUNNING that never blind-resubmits;
- 首帧/联系表 (the shared ``image_tasks`` state machine): double claim,
  expired lease quarantined to ``IMAGE_TASK_LEASE_EXPIRED`` or resumed from a
  recoverable checkpoint, stale-lease failure is a no-op, and the
  submission fence splits SUBMISSION_UNCERTAIN from retryable FAILED;
- 人物图 (``character_generation_tasks``): SKIP LOCKED double-claim guard,
  expired lease with attempts left is recoverable, exhausted attempts fail
  closed (this class has no SUBMISSION_UNCERTAIN semantics — registered as
  the CW-002 known limitation);
- 源帧 (``source_frame_tasks``): double claim is exclusive, an expired task
  fails closed to manual recovery (never auto-retried), and the degraded
  worker round processes source frames locally when quality settings are
  missing;
- 文案改写 (``script_rewrite_tasks``): exclusivity, expired submission
  quarantined without recalling the provider, a 504 after submission lands in
  SUBMISSION_UNCERTAIN, a stale-lease completion cannot overwrite recovered
  state, and the worker settles on PG;
- ASR (``script_from_audio_tasks``): exclusivity, expired pre-submission
  leases reset to PENDING while expired in-flight submissions quarantine,
  provider failure is terminal without blind resubmit, and the durable audio
  receipt survives a retry;
- 混合队列/两设备: mixed H3 + independent rounds rotate within the frozen
  per-user ``running<=1`` slot and the ``_FAIR_QUEUE_MAX_ROUNDS`` bound, and a
  second device cannot grow the same user's running slot from 1 to 2.

Per CW-002's signed scope the fair queue covers H3/独立/口播; 图片/文案/ASR
have no fairness rotation and this suite registers (not fakes) that limit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any, cast

# Set HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-cw030-worker-matrix-tests-minimum-48-bytes-long-1",
)

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)
from psycopg.rows import dict_row

from app.character_image_generation import acquire_character_generation_task
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.first_frames import (
    ImageProviderFailed,
    RetryableImageProviderFailed,
    first_frame_error,
)
from app.generation import (
    acquire_generation_task_lease,
    mark_expired_active_leases_needing_attention,
    mark_task_submission_uncertain,
)
from app.generation_worker import (
    _cleanup_audio_objects,
    _pg_audio_connection,
    run_pg_worker_once,
    run_pg_worker_round,
)
from app.image_tasks import (
    acquire_character_sheet_task,
    acquire_first_frame_task,
    fail_image_task,
    record_image_task_provider,
    save_first_frame_provider_submission,
)
from app.script_from_audio import (
    acquire_script_from_audio_task,
    fail_script_from_audio_task,
    prepare_script_from_audio_task,
)
from app.script_rewrite import (
    _script_rewrite_request_hash,
    acquire_script_rewrite_task,
    complete_script_rewrite_task,
    fail_script_rewrite_task,
    mark_script_rewrite_submission_started,
    prepare_script_rewrite_task,
)
from app.source_frames import ExtractedSourceFrame, acquire_source_frame_task
from app.storage import FakeStorageAdapter

CW030_DB_NAME = "cw030_worker_matrix_test"

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

_MATRIX_CLEANUP_ORDER = (
    "viral_script_cache",
    # Reset only this isolated fixture, including new immutable billing descendants.
    "wallet_transactions",
    "generation_task_operations",
    "external_call_logs",
    "operation_cost_records",
    "generation_tasks",
    "generation_batches",
    "script_rewrite_tasks",
    "script_from_audio_tasks",
    "source_frame_tasks",
    "first_frame_tasks",
    "character_sheet_tasks",
    "character_generation_tasks",
    "character_assets",
    "character_versions",
    "character_personas",
    "person_identities",
    "user_queue_cursors",
    "audit_logs",
    "assets",
    "versions",
    "projects",
    "internal_access_tokens",
    "recharge_orders",
    "wallets",
    "users",
    "runtime_settings",
    "billing_tariffs",
)

_REQUEST_SNAPSHOT = '{"output_duration_seconds": 10, "resolution": "768P"}'
_PROMPT_SNAPSHOT = '{"prompt_text": "a cat", "first_frame_uri": "cos://bucket/ff.jpg"}'

_SHA_A = "a" * 64
_FIRST_FRAME_CHECKPOINT_JSON = (
    '{"execution": {"provider": "fake-image", "model": "fake-image-model"},'
    ' "checkpoint": {"schema_version": 1, "candidates": ['
    '{"index": 0, "storage_uri": "fake://generation-results/candidate-0.png",'
    ' "sha256": "' + _SHA_A + '", "size_bytes": 9, "content_type": "image/png",'
    ' "quality": {"passed": true}}]}}'
)


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module")
def matrix_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(CW030_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW030_DB_NAME)


@pytest.fixture()
def pg_state(matrix_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, matrix_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_FAKE_H3_OUTCOME", raising=False)
    yield matrix_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# Raw-seed helpers (worker-identical rows, no route machinery)
# ---------------------------------------------------------------------------


def _exec(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(sql, params)


def _rows(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as pg:
        found = pg.execute(sql, params).fetchall()
        return [dict(row) for row in found]


def _one(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(sql, params).fetchone()
        return None if row is None else row[0]


def _truncate(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute("TRUNCATE " + ",".join(_MATRIX_CLEANUP_ORDER) + " CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")


def _seed_base(dsn: str, *, fair_queue: bool = True) -> None:
    _truncate(dsn)
    _exec(
        dsn,
        "INSERT INTO runtime_settings ("
        " id, max_generation_count_per_batch, max_concurrent_h3_tasks,"
        " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen,"
        " fair_queue_enabled"
        ") VALUES (1, 4, 100, 1000, 10000, 1000, %s)",
        (fair_queue,),
    )
    _exec(
        dsn,
        "INSERT INTO users (id, username, display_name, role) VALUES"
        " ('u1', 'u1', 'User One', 'employee'),"
        " ('u2', 'u2', 'User Two', 'user'),"
        " ('u3', 'u3', 'User Three', 'user')",
    )
    _exec(
        dsn,
        "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES"
        " ('u1', 1000, 0), ('u2', 1000, 0), ('u3', 1000, 0)",
    )
    _exec(
        dsn,
        "INSERT INTO projects (id, name, owner_user_id) VALUES ('proj-1', 'CW030 Matrix', 'u1')",
    )
    _exec(
        dsn,
        "INSERT INTO versions (id, project_id, kind, version_number, payload_json) VALUES"
        " ('pv-1', 'proj-1', 'video', 1, '{}')",
    )
    _exec(
        dsn,
        "INSERT INTO person_identities (id, owner_user_id, display_name,"
        " authorization_status) VALUES ('identity-owned', 'u1', '张工', 'AUTHORIZED')",
    )


def _seed_generation_task(
    dsn: str,
    *,
    task_id: str,
    user_id: str,
    batch_id: str | None = None,
    creation_kind: str = "replica",
    project_id: str | None = "proj-1",
    provider: str = "fake_h3",
    status: str = "PENDING",
) -> str:
    batch = batch_id or f"batch-{task_id}"
    _exec(
        dsn,
        "INSERT INTO generation_batches ("
        " id, project_id, created_by_user_id, idempotency_key,"
        " request_hash, request_snapshot_json, status, creation_kind"
        ") VALUES (%s, %s, %s, %s, %s, %s, 'QUEUED', %s)",
        (
            batch,
            project_id,
            user_id,
            f"ik-{batch}",
            f"rh-{batch}",
            _REQUEST_SNAPSHOT,
            creation_kind,
        ),
    )
    _exec(
        dsn,
        "INSERT INTO generation_tasks ("
        " id, batch_id, generation_mode, provider, model,"
        " status, archive_status, quality_status,"
        " prompt_version_id, prompt_snapshot_json, next_poll_at"
        ") VALUES (%s, %s, 'I2V', %s, 'h3', %s, 'PENDING', 'PENDING', 'pv-1', %s, NULL)",
        (task_id, batch, provider, status, _PROMPT_SNAPSHOT),
    )
    _exec(
        dsn,
        "INSERT INTO user_queue_cursors (user_id, last_dispatched_at, running_tasks_count)"
        " VALUES (%s, now(), 0) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )
    return task_id


def _acquire(dsn: str, worker_id: str) -> Any:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return acquire_generation_task_lease(conn, worker_id=worker_id)


def _cursor_count(dsn: str, user_id: str) -> int:
    value = _one(
        dsn,
        "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id = %s",
        (user_id,),
    )
    return -1 if value is None else int(value)


def _task_status(dsn: str, table: str, task_id: str) -> str | None:
    return cast(str | None, _one(dsn, f"SELECT status FROM {table} WHERE id = %s", (task_id,)))


def _run_worker(worker_id: str, *, max_tasks: int | None = 1) -> int:
    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
    return run_pg_worker_once(worker_id=worker_id, storage=storage, max_tasks=max_tasks)


# ---------------------------------------------------------------------------
# A. 独立任务 — shared generation machinery, independent-kind seeds
# ---------------------------------------------------------------------------


def test_independent_double_claim_is_exclusive(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_generation_task(
        pg_state, task_id="ind-1", user_id="u1", creation_kind="independent", project_id=None
    )
    first = _acquire(pg_state, "worker-a")
    assert first is not None
    assert str(first["id"]) == "ind-1"
    second = _acquire(pg_state, "worker-b")
    assert second is None, "the same independent task must never be claimed twice"
    assert _cursor_count(pg_state, "u1") == 1


def test_independent_expired_lease_marks_uncertain_and_releases_slot(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_generation_task(
        pg_state, task_id="ind-1", user_id="u1", creation_kind="independent", project_id=None
    )
    lease = _acquire(pg_state, "worker-a")
    assert lease is not None
    _exec(
        pg_state,
        "UPDATE generation_tasks SET locked_until = now() - interval '5 minutes' WHERE id = %s",
        ("ind-1",),
    )
    with pg_transaction() as raw:
        mark_expired_active_leases_needing_attention(BusinessConnection.postgres(raw))
    assert _cursor_count(pg_state, "u1") == 0
    assert _task_status(pg_state, "generation_tasks", "ind-1") == "SUBMISSION_UNCERTAIN"
    replacement = _acquire(pg_state, "worker-b")
    assert replacement is None, "an uncertain task must not be silently resubmitted"


def test_independent_late_uncertainty_cannot_disturb_replacement(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_generation_task(
        pg_state, task_id="ind-1", user_id="u1", creation_kind="independent", project_id=None
    )
    lease = _acquire(pg_state, "worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_task_submission_uncertain(
            conn,
            task_id="ind-1",
            message="POST timed out",
            provider_task_id="prov-ind-1",
        )
    assert _cursor_count(pg_state, "u1") == 0
    # A replacement task takes the freed slot; a late duplicate uncertainty
    # signal for the old task must not touch the replacement's slot.
    _seed_generation_task(
        pg_state, task_id="ind-2", user_id="u1", creation_kind="independent", project_id=None
    )
    replacement = _acquire(pg_state, "worker-b")
    assert replacement is not None
    assert _cursor_count(pg_state, "u1") == 1
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_task_submission_uncertain(
            conn,
            task_id="ind-1",
            message="duplicate late signal",
            provider_task_id="prov-ind-1",
        )
    assert _cursor_count(pg_state, "u1") == 1, "late duplicate uncertainty must not double-count"
    assert _task_status(pg_state, "generation_tasks", "ind-2") == "SUBMITTING"


def test_independent_worker_settles_exactly_once_on_pg(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_generation_task(
        pg_state, task_id="ind-1", user_id="u1", creation_kind="independent", project_id=None
    )
    # The creation route commits the RESERVE (billing_round 1) before the
    # task is claimable; the worker owns the SETTLE side of the round.
    _exec(
        pg_state,
        "UPDATE wallets SET available_credits = 999, reserved_credits = 1 WHERE user_id = 'u1'",
    )
    _exec(
        pg_state,
        "INSERT INTO wallet_transactions ("
        " id, user_id, type, available_delta, reserved_delta, task_id,"
        " billing_round, idempotency_key"
        ") VALUES ('wt-ind-1-r1', 'u1', 'RESERVE', -1, 1, 'ind-1', 1, 'reserve:ind-1:1')",
    )
    processed = _run_worker("cw030-independent-worker")
    final = _rows(
        pg_state,
        "SELECT status, error_code, error_message_redacted FROM generation_tasks"
        " WHERE id = 'ind-1'",
    )[0]
    assert processed == 1, final
    assert final["status"] == "SUCCEEDED", final
    ledger = _rows(
        pg_state,
        "SELECT type, billing_round FROM wallet_transactions WHERE task_id = 'ind-1'"
        " ORDER BY billing_round, type",
    )
    assert [(str(r["type"]), int(r["billing_round"])) for r in ledger] == [
        ("RESERVE", 1),
        ("SETTLE", 1),
    ], ledger
    assert _one(pg_state, "SELECT reserved_credits FROM wallets WHERE user_id = 'u1'") == 0


def test_independent_crash_after_claim_never_blind_resubmits(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_generation_task(
        pg_state, task_id="ind-1", user_id="u1", creation_kind="independent", project_id=None
    )
    lease = _acquire(pg_state, "worker-a")
    assert lease is not None
    # The claim transaction committed the durable SUBMITTING lease; a crash
    # here must never silently re-fire the paid provider from a fresh worker.
    assert _task_status(pg_state, "generation_tasks", "ind-1") == "SUBMITTING"
    _exec(
        pg_state,
        "UPDATE generation_tasks SET locked_until = now() - interval '5 minutes' WHERE id = %s",
        ("ind-1",),
    )
    processed = _run_worker("restart-worker")
    assert processed == 0, "the restart must not blind-resubmit the uncertain task"
    assert _task_status(pg_state, "generation_tasks", "ind-1") == "SUBMISSION_UNCERTAIN"


# ---------------------------------------------------------------------------
# B/C. 首帧 + 联系表 — the shared image_tasks state machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shot",
    [
        {"subject": "主讲人", "person_count": 3},
        {"subject": "主讲人"},
    ],
)
def test_first_frame_generation_gate_accepts_multi_person_and_legacy_analysis(
    pg_state: str, shot: dict[str, Any]
) -> None:
    from app.first_frames import require_readable_video_analysis

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO versions (id,project_id,kind,version_number,payload_json) "
        "VALUES ('analysis-1','proj-1','analysis',1,%s)",
        (json.dumps({"analysis": {"shots": [shot]}}),),
    )

    with pg_transaction() as raw:
        require_readable_video_analysis(
            BusinessConnection.postgres(raw),
            project_id="proj-1",
        )


def _seed_image_task(
    dsn: str,
    *,
    table: str,
    task_id: str,
    user_id: str = "u1",
    status: str = "PENDING",
    result_json: str | None = None,
    attempt: int = 0,
) -> None:
    locked_by = user_id if status == "RUNNING" else None
    if table == "first_frame_tasks":
        _exec(
            dsn,
            "INSERT INTO first_frame_tasks ("
            " id, created_by_user_id, project_id, idempotency_key, request_hash,"
            " request_json, result_json, status, attempt, locked_by"
            ") VALUES (%s, %s, 'proj-1', %s, %s, '{}', %s, %s, %s, %s)",
            (
                task_id,
                user_id,
                f"ik-{task_id}",
                f"rh-{task_id}",
                result_json,
                status,
                attempt,
                locked_by,
            ),
        )
    else:
        _exec(
            dsn,
            "INSERT INTO character_sheet_tasks ("
            " id, created_by_user_id, identity_id, idempotency_key, request_hash,"
            " request_json, result_json, status, attempt, locked_by, operation,"
            " source_storage_uri, source_content_type, source_sha256, source_size_bytes"
            ") VALUES (%s, %s, 'identity-owned', %s, %s, '{}', %s, %s, %s, %s, 'CREATE',"
            " 'fake://sf-uploads/identity-owned.png', 'image/png', '" + "f" * 64 + "', 9)",
            (
                task_id,
                user_id,
                f"ik-{task_id}",
                f"rh-{task_id}",
                result_json,
                status,
                attempt,
                locked_by,
            ),
        )


def _expire_running_lease(dsn: str, table: str, task_id: str) -> None:
    _exec(
        dsn,
        f"UPDATE {table} SET locked_until = now() - interval '5 minutes' WHERE id = %s",
        (task_id,),
    )


def test_first_frame_double_claim_is_exclusive(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-1")
    with pg_transaction() as raw:
        first = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert first is not None
    assert first.id == "ff-1"
    with pg_transaction() as raw:
        second = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-b")
    assert second is None
    assert _task_status(pg_state, "first_frame_tasks", "ff-1") == "RUNNING"


def test_first_frame_expired_without_checkpoint_is_quarantined(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_image_task(
        pg_state,
        table="first_frame_tasks",
        task_id="ff-1",
        status="RUNNING",
        attempt=3,
    )
    _expire_running_lease(pg_state, "first_frame_tasks", "ff-1")
    # attempt>=3: no checkpoint resume, no retry — quarantine to UNCERTAIN.
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is None
    row = _rows(
        pg_state,
        "SELECT status, error_code FROM first_frame_tasks WHERE id = 'ff-1'",
    )[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert row["error_code"] == "IMAGE_TASK_LEASE_EXPIRED"


def test_first_frame_expired_with_checkpoint_resumes(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_image_task(
        pg_state,
        table="first_frame_tasks",
        task_id="ff-1",
        status="RUNNING",
        attempt=1,
        result_json=_FIRST_FRAME_CHECKPOINT_JSON,
    )
    _expire_running_lease(pg_state, "first_frame_tasks", "ff-1")
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    # The resume marker is transient (the claim clears error_code); the
    # resume is proven by attempt 1->2 under the new owner — a
    # non-recoverable task would have been quarantined at attempt 1.
    row = _rows(
        pg_state,
        "SELECT status, attempt, locked_by FROM first_frame_tasks WHERE id = 'ff-1'",
    )[0]
    assert row["status"] == "RUNNING"
    assert row["locked_by"] == "worker-a"
    assert int(row["attempt"]) == 2
    assert lease is not None and lease.id == "ff-1"


def test_first_frame_stale_lease_failure_is_a_noop(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-1")
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is not None
    # The lease is lost (quarantined underneath the stale worker).
    _exec(
        pg_state,
        "UPDATE first_frame_tasks SET status = 'SUBMISSION_UNCERTAIN',"
        " error_code = 'IMAGE_TASK_LEASE_EXPIRED', locked_by = NULL,"
        " locked_until = NULL WHERE id = 'ff-1'",
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        fail_image_task(
            conn,
            table="first_frame_tasks",
            lease=lease,
            cause=RuntimeError("late provider failure from the stale worker"),
            submission_started=True,
        )
    row = _rows(pg_state, "SELECT status, error_code FROM first_frame_tasks WHERE id = 'ff-1'")[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN", (
        "the stale worker's late failure must not overwrite the recovered state"
    )
    assert row["error_code"] == "IMAGE_TASK_LEASE_EXPIRED"


@pytest.mark.parametrize("action", ["renew", "provider", "checkpoint", "prepare"])
def test_first_frame_stale_attempt_cannot_change_checkpoint_or_lease(pg_state: str, action: str):
    from app.image_tasks import (
        _require_owned_task,
        record_image_task_provider,
        renew_image_task_lease,
        save_first_frame_task_checkpoint,
    )

    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="late-image")
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="reused-id")
    assert lease is not None
    _exec(pg_state, "UPDATE first_frame_tasks SET attempt=attempt+1 WHERE id='late-image'")
    with pytest.raises(RuntimeError, match="lease was lost"):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            if action == "renew":
                renew_image_task_lease(conn, table="first_frame_tasks", lease=lease)
            elif action == "provider":
                record_image_task_provider(
                    conn, table="first_frame_tasks", lease=lease, provider="old", model="old"
                )
            elif action == "checkpoint":
                save_first_frame_task_checkpoint(conn, lease=lease, candidates=[])
            else:
                _require_owned_task(conn, "first_frame_tasks", lease)


def test_first_frame_submission_fence_splits_uncertain_and_retryable(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-1")
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        fail_image_task(
            conn,
            table="first_frame_tasks",
            lease=lease,
            cause=RuntimeError("provider transport dropped after submission"),
            submission_started=True,
        )
    row = _rows(pg_state, "SELECT status, retryable FROM first_frame_tasks WHERE id = 'ff-1'")[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert int(row["retryable"]) == 0

    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-2")
    with pg_transaction() as raw:
        lease2 = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease2 is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        fail_image_task(
            conn,
            table="first_frame_tasks",
            lease=lease2,
            cause=RuntimeError("storage unavailable before any provider call"),
            submission_started=False,
        )
    row2 = _rows(pg_state, "SELECT status, retryable FROM first_frame_tasks WHERE id = 'ff-2'")[0]
    assert row2["status"] == "FAILED"
    assert int(row2["retryable"]) == 1


def test_character_sheet_double_claim_and_expiry(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_image_task(pg_state, table="character_sheet_tasks", task_id="cs-1")
    with pg_transaction() as raw:
        first = acquire_character_sheet_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert first is not None
    with pg_transaction() as raw:
        second = acquire_character_sheet_task(
            BusinessConnection.postgres(raw), worker_id="worker-b"
        )
    assert second is None
    # Expire the RUNNING lease; the next acquire quarantines it (no checkpoint
    # resume exists for contact sheets).
    _expire_running_lease(pg_state, "character_sheet_tasks", "cs-1")
    with pg_transaction() as raw:
        third = acquire_character_sheet_task(BusinessConnection.postgres(raw), worker_id="worker-b")
    assert third is None
    row = _rows(
        pg_state,
        "SELECT status, error_code FROM character_sheet_tasks WHERE id = 'cs-1'",
    )[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert row["error_code"] == "IMAGE_TASK_LEASE_EXPIRED"


# ---------------------------------------------------------------------------
# D. 人物图 — character_generation_tasks (no SUBMISSION_UNCERTAIN semantics:
# registered CW-002 limitation; failure closes through attempt exhaustion)
# ---------------------------------------------------------------------------


def _seed_character_domain(dsn: str) -> None:
    _exec(
        dsn,
        "INSERT INTO person_identities (id, owner_user_id, display_name,"
        " authorization_status) VALUES ('pi-1', 'u1', '张工', 'AUTHORIZED')",
    )
    _exec(
        dsn,
        "INSERT INTO character_personas (id, identity_id, name) VALUES ('cp-1', 'pi-1', '张工')",
    )
    _exec(
        dsn,
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes, content_type,"
        " created_by_user_id"
        ") VALUES ('char-src', NULL, 'material_image',"
        " 'fake://generation-results/char-src.png', '" + "c" * 64 + "', 9, 'image/png', 'u1')"
        " ON CONFLICT (id) DO NOTHING",
    )
    _exec(
        dsn,
        "INSERT INTO character_versions ("
        " id, persona_id, version_number, status, source_asset_id, source_sha256,"
        " persona_snapshot_json, provider, model, generation_params_json,"
        " template_version, template_hash, required_view_types_json, created_by"
        ") VALUES ('cv-1', 'cp-1', 1, 'PUBLISHED', 'char-src', '"
        + "d" * 64
        + "', '{}', 'fake-image', 'fake-image-model', '{}', 'v1', '"
        + "e" * 64
        + "', '[]', 'u1')",
    )


def _seed_character_task(
    dsn: str,
    *,
    task_id: str,
    status: str = "PENDING",
    attempt: int = 0,
    max_attempts: int = 3,
) -> None:
    _exec(
        dsn,
        "INSERT INTO character_generation_tasks ("
        " id, character_version_id, view_type, provider, model, idempotency_key,"
        " request_hash, candidate_number, status, attempt, max_attempts, locked_by,"
        " created_by"
        ") VALUES (%s, 'cv-1', 'FRONT_FACE', 'fake-image', 'fake-image-model', %s, %s, 1,"
        " %s, %s, %s, %s, 'u1')",
        (
            task_id,
            f"ik-{task_id}",
            f"rh-{task_id}",
            status,
            attempt,
            max_attempts,
            "worker-a" if status == "RUNNING" else None,
        ),
    )


def test_character_generation_skip_locked_double_claim(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_character_domain(pg_state)
    _seed_character_task(pg_state, task_id="cg-1")
    with pg_transaction() as raw:
        first = acquire_character_generation_task(
            BusinessConnection.postgres(raw), worker_id="worker-a"
        )
    assert first is not None
    assert str(first["id"]) == "cg-1"
    with pg_transaction() as raw:
        second = acquire_character_generation_task(
            BusinessConnection.postgres(raw), worker_id="worker-b"
        )
    assert second is None


def test_character_generation_expired_lease_with_attempts_left_recovers(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_character_domain(pg_state)
    _seed_character_task(pg_state, task_id="cg-1", status="RUNNING", attempt=1)
    _expire_running_lease(pg_state, "character_generation_tasks", "cg-1")
    with pg_transaction() as raw:
        reacquired = acquire_character_generation_task(
            BusinessConnection.postgres(raw), worker_id="worker-b"
        )
    assert reacquired is not None
    row = _rows(
        pg_state,
        "SELECT status, attempt, locked_by FROM character_generation_tasks WHERE id = 'cg-1'",
    )[0]
    assert row["status"] == "RUNNING"
    assert row["locked_by"] == "worker-b"
    assert int(row["attempt"]) == 2


def test_character_generation_exhausted_attempts_fail_closed(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_character_domain(pg_state)
    _seed_character_task(pg_state, task_id="cg-1", status="RUNNING", attempt=3, max_attempts=3)
    _expire_running_lease(pg_state, "character_generation_tasks", "cg-1")
    with pg_transaction() as raw:
        lease = acquire_character_generation_task(
            BusinessConnection.postgres(raw), worker_id="worker-b"
        )
    assert lease is None
    row = _rows(
        pg_state,
        "SELECT status, error_code FROM character_generation_tasks WHERE id = 'cg-1'",
    )[0]
    assert row["status"] == "FAILED"
    assert row["error_code"] == "CHARACTER_LEASE_EXPIRED"


# ---------------------------------------------------------------------------
# E. 源帧 — exclusivity + manual recovery + degraded worker round
# ---------------------------------------------------------------------------


def _seed_source_frame_task(
    dsn: str,
    *,
    task_id: str,
    status: str = "PENDING",
    timestamps: str = "[1.0]",
) -> None:
    _exec(
        dsn,
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes, content_type,"
        " created_by_user_id"
        ") VALUES ('asset-ref', 'proj-1', 'reference_video',"
        " 'fake://sf-uploads/reference.mp4', '" + "b" * 64 + "', 16, 'video/mp4', 'u1')"
        " ON CONFLICT (id) DO NOTHING",
    )
    _exec(
        dsn,
        "INSERT INTO source_frame_tasks ("
        " id, project_id, asset_id, created_by_user_id, idempotency_key,"
        " request_hash, request_json, status"
        ") VALUES (%s, 'proj-1', 'asset-ref', 'u1', %s, %s, %s, %s)",
        (
            task_id,
            f"ik-{task_id}",
            f"rh-{task_id}",
            '{"timestamps_seconds": ' + timestamps + "}",
            status,
        ),
    )


class _FakeSourceFrameExtractor:
    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        timestamps_seconds: tuple[float, ...],
    ) -> list[ExtractedSourceFrame]:
        assert content == b"reference-video"
        return [
            ExtractedSourceFrame(
                timestamp_seconds=timestamp,
                image=f"frame-{timestamp}".encode(),
                technical_score=round(timestamp / 12, 3),
            )
            for timestamp in timestamps_seconds
        ]


def test_source_frame_double_claim_is_exclusive(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-1")
    with pg_transaction() as raw:
        first = acquire_source_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert first is not None
    with pg_transaction() as raw:
        second = acquire_source_frame_task(BusinessConnection.postgres(raw), worker_id="worker-b")
    assert second is None


def test_source_frame_expired_fails_closed_to_manual_recovery(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-1", status="RUNNING")
    _exec(
        pg_state,
        "UPDATE source_frame_tasks SET locked_by = 'worker-a',"
        " locked_until = now() - interval '5 minutes' WHERE id = 'sf-1'",
    )
    with pg_transaction() as raw:
        reacquired = acquire_source_frame_task(
            BusinessConnection.postgres(raw), worker_id="worker-b"
        )
    assert reacquired is None
    row = _rows(
        pg_state, "SELECT status, error_code, retryable FROM source_frame_tasks WHERE id = 'sf-1'"
    )[0]
    assert row["status"] == "FAILED"
    assert row["error_code"] == "SOURCE_FRAME_TASK_RECOVERY_REQUIRED"
    assert int(row["retryable"]) == 1, "manual restart stays possible; auto-retry stays off"


def test_pg_worker_round_processes_source_frames_with_missing_quality_settings(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-1")
    storage = FakeStorageAdapter(provider="fake", bucket="sf-uploads")
    storage.put_object("reference.mp4", b"reference-video", content_type="video/mp4")
    monkeypatch.setattr(
        "app.generation_worker.get_first_frame_quality_inspector",
        lambda _conn: (_ for _ in ()).throw(
            HTTPException(
                status_code=503,
                detail={"code": "FIRST_FRAME_QUALITY_SETTINGS_UNAVAILABLE"},
            )
        ),
    )
    monkeypatch.setattr(
        "app.generation_worker.get_media_storage",
        lambda _conn: storage,
    )
    monkeypatch.setattr(
        "app.generation_worker.FFmpegSourceFrameExtractor",
        lambda: _FakeSourceFrameExtractor(),
    )
    processed = run_pg_worker_round(worker_id="sf-fallback-worker", max_tasks=1)
    final = _rows(
        pg_state,
        "SELECT status, error_code, error_message_redacted FROM source_frame_tasks"
        " WHERE id = 'sf-1'",
    )[0]
    assert processed == 1, final
    assert final["status"] == "SUCCEEDED", final


# ---------------------------------------------------------------------------
# F. 文案改写 — script_rewrite_tasks on the PG lane
# ---------------------------------------------------------------------------


_REWRITE_TEXT = "原始口播稿。"
_REWRITE_HASH = _script_rewrite_request_hash({"text": _REWRITE_TEXT})


def _seed_script_rewrite_task(
    dsn: str, *, task_id: str, user_id: str = "u1", status: str = "PENDING"
) -> None:
    _exec(
        dsn,
        "INSERT INTO script_rewrite_tasks ("
        " id, project_id, created_by_user_id, idempotency_key, request_hash,"
        " request_json, status"
        ") VALUES (%s, 'proj-1', %s, %s, %s, %s, %s)",
        (
            task_id,
            user_id,
            f"ik-{task_id}",
            _REWRITE_HASH,
            json.dumps({"text": _REWRITE_TEXT}, ensure_ascii=False, separators=(",", ":")),
            status,
        ),
    )


def _rewrite_lease(dsn: str, worker_id: str) -> Any:
    with pg_transaction() as raw:
        return acquire_script_rewrite_task(BusinessConnection.postgres(raw), worker_id=worker_id)


def _configure_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    """prepare resolves DeepSeek settings through SettingsRepository; the
    matrix stubs that seam so the paid HTTP boundary (_request_deepseek)
    stays the only faked layer."""

    class _StubSettingsRepository:
        def __init__(self, conn: object) -> None:
            self._conn = conn

        def load_provider_config(self, provider: str) -> dict[str, str]:
            assert provider == "deepseek"
            return {"api_key": "test-key", "model": "deepseek-chat"}

    import app.script_rewrite as script_rewrite

    monkeypatch.setattr(script_rewrite, "SettingsRepository", _StubSettingsRepository)


@pytest.mark.parametrize("service", ["first_frame", "rewrite"])
def test_stale_failure_cannot_release_current_attempt_budget(pg_state: str, service: str) -> None:
    from app.usage_billing import accept_operation

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES(%s,true,7)",
        (service,),
    )
    if service == "first_frame":
        _seed_image_task(pg_state, table="first_frame_tasks", task_id="late")
        table = "first_frame_tasks"
        with pg_transaction() as raw:
            lease = acquire_first_frame_task(
                BusinessConnection.postgres(raw), worker_id="same-worker"
            )
    else:
        _seed_script_rewrite_task(pg_state, task_id="late")
        table = "script_rewrite_tasks"
        lease = _rewrite_lease(pg_state, "same-worker")
    assert lease is not None
    with pg_transaction() as raw:
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="u1",
            service=service,
            source_id="late",
            units=1,
        )
    # A restarted worker may reuse its name: attempt is the fencing token.
    _exec(pg_state, f"UPDATE {table} SET attempt=attempt+1 WHERE id='late'")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        if service == "first_frame":
            fail_image_task(
                conn,
                table="first_frame_tasks",
                lease=lease,
                cause=ValueError("late"),
                submission_started=False,
            )
        else:
            fail_script_rewrite_task(
                conn, lease=lease, cause=ValueError("late"), submission_started=False
            )
    assert _one(pg_state, "SELECT reserved_credits FROM wallets WHERE user_id='u1'") == 7
    assert _task_status(pg_state, table, "late") == "RUNNING"


def test_uncertain_checkpoint_recovery_releases_old_budget(pg_state: str) -> None:
    from app.usage_billing import accept_operation, finish_source, reconcile_operations

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES('first_frame',true,7)",
    )
    _seed_image_task(
        pg_state,
        table="first_frame_tasks",
        task_id="old",
        status="SUBMISSION_UNCERTAIN",
        result_json=_FIRST_FRAME_CHECKPOINT_JSON,
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        accept_operation(conn, user_id="u1", service="first_frame", source_id="old", units=1)
        assert reconcile_operations(conn) == 1
        accept_operation(conn, user_id="u1", service="first_frame", source_id="new", units=1)
        finish_source(conn, "new", units=1, succeeded=True)
    assert _one(pg_state, "SELECT reserved_credits FROM wallets WHERE user_id='u1'") == 0
    assert _one(pg_state, "SELECT sum(charged_credits) FROM billing_operations") == 7


def test_pg_first_frame_quality_transport_records_parent_cost(pg_state: str, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.first_frames import ApilioFirstFrameQualityInspector
    from app.usage_billing import accept_operation

    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="quality-cost")
    _exec(
        pg_state,
        "INSERT INTO billing_tariffs(service,unit_cost_fen) VALUES('quality_inspection',0.125)",
    )
    with pg_transaction() as raw:
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="u1",
            service="first_frame",
            source_id="quality-cost",
            units=1,
        )

    class Transport:
        def post(self, *args, **kwargs):
            return b'{"choices":[{"message":{"content":"{}"}}]}', {}

    inspector = ApilioFirstFrameQualityInspector(api_key="test-key", transport=Transport())
    monkeypatch.setattr(
        "app.generation_worker.prepare_first_frame_task",
        lambda *args, **kwargs: SimpleNamespace(
            provider=SimpleNamespace(provider_name="fake"),
            plan=SimpleNamespace(model="fake"),
            provider_submission=None,
        ),
    )
    monkeypatch.setattr(
        "app.generation_worker.record_image_task_provider", lambda *args, **kwargs: None
    )

    def execute(*args, **kwargs):
        inspector._chat_json([])
        raise ValueError("publication unavailable")

    monkeypatch.setattr("app.generation_worker.run_first_frame_task_outside_transaction", execute)
    assert _run_worker("quality-worker") == 1
    assert (
        _one(pg_state, "SELECT cost_fen FROM billing_attempts WHERE service='quality_inspection'")
        == 0.125
    )
    assert (
        _one(
            pg_state,
            "SELECT source_id FROM billing_operations WHERE id=(SELECT operation_id "
            "FROM billing_attempts WHERE service='quality_inspection')",
        )
        == "quality-cost"
    )


def test_analysis_publication_is_atomic_and_repeat_completion_is_noop(pg_state, monkeypatch):
    from app import analysis_routes
    from app.analysis import FakeGemini, analyze_video

    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="atomic-source")
    _exec(
        pg_state,
        "INSERT INTO analysis_tasks(id,project_id,asset_id,created_by_user_id,duration_seconds,"
        "status,locked_by,attempt) VALUES('atomic-analysis','proj-1','asset-ref','u1',4,"
        "'RUNNING','atomic-worker',1)",
    )
    work = analysis_routes.AnalysisTaskWork(
        lease=analysis_routes.AnalysisTaskLease(
            id="atomic-analysis",
            project_id="proj-1",
            asset_id="asset-ref",
            created_by_user_id="u1",
            duration_seconds=4,
            worker_id="atomic-worker",
        ),
        provider=FakeGemini(),
        video_uri="fake",
        asset_uri="fake://sf-uploads/reference.mp4",
    )
    result = analyze_video(video_uri="fake", video_duration_seconds=4, provider=FakeGemini())
    original_audit = analysis_routes.write_audit

    def unavailable_audit(*args, **kwargs):
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(analysis_routes, "write_audit", unavailable_audit)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        with pg_transaction() as raw:
            analysis_routes.complete_analysis_task(
                BusinessConnection.postgres(raw),
                work=work,
                result=result,
            )
    assert (
        _one(pg_state, "SELECT COUNT(*) FROM versions WHERE kind IN ('analysis', 'shot_card')") == 0
    )
    assert _one(pg_state, "SELECT status FROM analysis_tasks") == "RUNNING"
    monkeypatch.setattr(analysis_routes, "write_audit", original_audit)
    for _ in range(2):
        with pg_transaction() as raw:
            analysis_routes.complete_analysis_task(
                BusinessConnection.postgres(raw),
                work=work,
                result=result,
            )
    assert _one(pg_state, "SELECT status FROM analysis_tasks") == "SUCCEEDED"
    assert _one(pg_state, "SELECT COUNT(*) FROM versions WHERE kind='analysis'") == 1
    assert _one(pg_state, "SELECT COUNT(*) FROM versions WHERE kind='shot_card'") == 1


def test_pg_analysis_keeps_known_call_cost_when_repair_fails(pg_state: str) -> None:
    from app.analysis import ProviderResponse
    from app.usage_billing import accept_operation

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO "
        "assets(id,project_id,kind,storage_uri,sha256,size_bytes,content_type,created_by_user_id) VALUES('analysis-input','proj-1','reference_video','fake://bucket/ref.mp4',%s,8,'video/mp4','u1')",
        ("a" * 64,),
    )
    _exec(
        pg_state,
        "INSERT INTO "
        "analysis_tasks(id,project_id,asset_id,created_by_user_id,duration_seconds) "
        "VALUES('analysis-failed','proj-1','analysis-input','u1',5)",
    )
    _exec(
        pg_state,
        "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
        "VALUES('analysis',true,7,2.5)",
    )
    with pg_transaction() as raw:
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="u1",
            service="analysis",
            source_id="analysis-failed",
            units=1,
        )

    class Provider:
        requires_https_video_url = False

        def analyze(self, **kwargs):
            return ProviderResponse(text="invalid", raw={})

        def repair_json(self, **kwargs):
            raise ValueError("repair failed")

    assert (
        run_pg_worker_once(
            worker_id="analysis-worker",
            storage=FakeStorageAdapter(provider="fake", bucket="bucket"),
            analysis_provider=Provider(),
            max_tasks=1,
        )
        == 1
    )
    assert _one(pg_state, "SELECT cost_fen FROM billing_attempts WHERE service='analysis'") == 2.5
    assert _one(pg_state, "SELECT charged_credits FROM billing_operations") == 0
    assert _one(pg_state, "SELECT reserved_credits FROM wallets WHERE user_id='u1'") == 0


def test_script_rewrite_claim_is_exclusive(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_script_rewrite_task(pg_state, task_id="sr-1")
    first = _rewrite_lease(pg_state, "worker-a")
    assert first is not None
    assert _rewrite_lease(pg_state, "worker-b") is None


def test_script_rewrite_expired_submission_quarantines_without_recall(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_base(pg_state)
    _configure_deepseek(monkeypatch)
    _seed_script_rewrite_task(pg_state, task_id="sr-1")
    lease = _rewrite_lease(pg_state, "crashed-worker")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        prepare_script_rewrite_task(conn, lease=lease)
        mark_script_rewrite_submission_started(conn, lease=lease)
    # Expire the lease from outside the fenced transaction (an in-block
    # UPDATE would self-deadlock on the row the transaction just marked).
    _expire_running_lease(pg_state, "script_rewrite_tasks", "sr-1")
    # The replacement worker must NOT pick the uncertain task back up (the
    # provider call already left; a resubmit would double-charge DeepSeek).
    assert _rewrite_lease(pg_state, "replacement-worker") is None
    assert _task_status(pg_state, "script_rewrite_tasks", "sr-1") == "SUBMISSION_UNCERTAIN"


def test_script_rewrite_network_timeout_lands_uncertain_not_retried(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_base(pg_state)
    _configure_deepseek(monkeypatch)
    _seed_script_rewrite_task(pg_state, task_id="sr-1")
    lease = _rewrite_lease(pg_state, "timeout-worker")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        prepare_script_rewrite_task(conn, lease=lease)
        mark_script_rewrite_submission_started(conn, lease=lease)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        fail_script_rewrite_task(
            conn,
            lease=lease,
            cause=HTTPException(
                504,
                detail={
                    "code": "DEEPSEEK_NETWORK_FAILED",
                    "message": "连接 AI 改写服务失败，请检查网络后重试。",
                },
            ),
            submission_started=True,
        )
    assert _task_status(pg_state, "script_rewrite_tasks", "sr-1") == "SUBMISSION_UNCERTAIN"
    assert _rewrite_lease(pg_state, "second-worker") is None


def test_script_rewrite_stale_completion_cannot_overwrite_recovered_state(
    pg_state: str,
) -> None:
    from app.script_rewrite import ScriptRewriteResult

    _seed_base(pg_state)
    _seed_script_rewrite_task(pg_state, task_id="sr-1")
    stale_lease = _rewrite_lease(pg_state, "worker-a")
    assert stale_lease is not None
    # The lease is lost underneath the stale worker (quarantined).
    _exec(
        pg_state,
        "UPDATE script_rewrite_tasks SET status = 'SUBMISSION_UNCERTAIN', locked_by = NULL,"
        " locked_until = NULL WHERE id = 'sr-1'",
    )
    with pytest.raises(RuntimeError, match="lease was lost"):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            complete_script_rewrite_task(
                conn,
                lease=stale_lease,
                result=ScriptRewriteResult(
                    rewritten_text="迟到结果",
                    provider="deepseek",
                    model="deepseek-chat",
                ),
            )
    row = _rows(
        pg_state,
        "SELECT status, result_json FROM script_rewrite_tasks WHERE id = 'sr-1'",
    )[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert row["result_json"] is None, "the late result must not overwrite recovered state"


def test_script_rewrite_worker_settles_on_pg(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.script_rewrite as script_rewrite

    _seed_base(pg_state)
    _configure_deepseek(monkeypatch)
    _seed_script_rewrite_task(pg_state, task_id="sr-1")
    monkeypatch.setattr(
        script_rewrite,
        "_request_deepseek",
        lambda **_kwargs: "这是改写后的口播稿。",
    )
    processed = _run_worker("cw030-rewrite-worker")
    assert processed == 1
    row = _rows(
        pg_state,
        "SELECT status, result_json FROM script_rewrite_tasks WHERE id = 'sr-1'",
    )[0]
    assert row["status"] == "SUCCEEDED"
    assert "这是改写后的口播稿。" in str(row["result_json"])


def test_truncated_rewrite_refunds_customer_but_preserves_confirmed_cost(pg_state, monkeypatch):
    from types import SimpleNamespace

    from app.usage_billing import accept_operation

    _seed_base(pg_state)
    _configure_deepseek(monkeypatch)
    _seed_script_rewrite_task(pg_state, task_id="sr-truncated")
    _exec(
        pg_state,
        "INSERT INTO wallets(user_id,available_credits) VALUES('u1',100) ON CONFLICT(user_id) "
        "DO UPDATE SET available_credits=100",
    )
    _exec(
        pg_state,
        "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
        "VALUES('rewrite',true,5,2)",
    )
    with pg_transaction() as raw:
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="u1",
            service="rewrite",
            source_id="sr-truncated",
            units=1,
        )
    monkeypatch.setattr(
        "curl_cffi.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            content=b'{"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}',
        ),
    )
    assert _run_worker("truncated-cost") == 1
    assert _task_status(pg_state, "script_rewrite_tasks", "sr-truncated") == "FAILED"
    assert _one(pg_state, "SELECT available_credits FROM wallets WHERE user_id='u1'") == 100
    assert _one(pg_state, "SELECT cost_fen FROM billing_attempts WHERE service='rewrite'") == 2


def test_empty_asr_text_refunds_customer_and_preserves_actual_duration_cost(pg_state):
    from test_asr_provider import StubTransport, make_config

    from app.asr import AsrProviderError, DashScopeFunAsr
    from app.usage_billing import accept_operation, begin_source_attempt

    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="asr-cost-asset")
    _seed_audio_task(pg_state, task_id="asr-cost")
    lease = _audio_lease(pg_state, "asr-cost-worker")
    _exec(
        pg_state,
        "INSERT INTO wallets(user_id,available_credits) VALUES('u1',100) ON CONFLICT(user_id) "
        "DO UPDATE SET available_credits=100",
    )
    _exec(
        pg_state,
        "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
        "VALUES('asr',true,2,0.25)",
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        accept_operation(conn, user_id="u1", service="asr", source_id="asr-cost", units=20)
        begin_source_attempt(conn, "asr-cost")
    provider = DashScopeFunAsr(
        make_config(),
        transport=StubTransport([(200, b'{"output":{"text":""},"usage":{"duration":12.5}}')]),
    )
    with pytest.raises(AsrProviderError) as failure:
        provider.transcribe("https://media.example/asr", duration_sec=20)
    with pg_transaction() as raw:
        fail_script_from_audio_task(
            BusinessConnection.postgres(raw),
            lease=lease,
            cause=failure.value,
            submission_started=True,
        )
    assert _one(pg_state, "SELECT available_credits FROM wallets WHERE user_id='u1'") == 100
    assert _one(pg_state, "SELECT cost_fen FROM billing_attempts WHERE service='asr'") == 3.125
    assert _task_status(pg_state, "script_from_audio_tasks", "asr-cost") == "FAILED"


# ---------------------------------------------------------------------------
# G. ASR — script_from_audio_tasks on the PG lane
# ---------------------------------------------------------------------------


def _seed_audio_task(
    dsn: str,
    *,
    task_id: str,
    status: str = "PENDING",
    audio_object_key: str | None = None,
) -> None:
    _exec(
        dsn,
        "INSERT INTO script_from_audio_tasks ("
        " id, project_id, source_asset_id, created_by_user_id, idempotency_key,"
        " request_hash, request_json, audio_object_key, status"
        ") VALUES (%s, 'proj-1', 'asset-ref', 'u1', %s, %s, '{}', %s, %s)"
        " ON CONFLICT (id) DO NOTHING",
        (task_id, f"ik-{task_id}", f"rh-{task_id}", audio_object_key, status),
    )


def _audio_lease(dsn: str, worker_id: str) -> Any:
    with pg_transaction() as raw:
        return acquire_script_from_audio_task(BusinessConnection.postgres(raw), worker_id=worker_id)


def _seed_viral_copy(
    dsn: str, user: str, *, platform: str = "douyin", imported: bool = True
) -> None:
    _exec(
        dsn,
        "INSERT INTO projects(id,name,owner_user_id) VALUES (%s,'copy',%s)",
        (f"copy-{user}", user),
    )
    _exec(
        dsn,
        "INSERT INTO assets(id,project_id,kind,storage_uri,sha256,size_bytes,"
        "content_type,created_by_user_id,metadata_json) VALUES (%s,%s,'reference_video',"
        "'fake://copies/source.mp4',%s,16,'video/mp4',%s,%s)",
        (
            f"copy-asset-{user}",
            f"copy-{user}",
            "b" * 64,
            user,
            json.dumps({"duration_seconds": 12, "platform": platform, "video_id": "123"}),
        ),
    )
    if imported:
        _exec(
            dsn,
            "INSERT INTO viral_import_tasks(id,owner_user_id,project_id,source_asset_id,"
            "platform,video_id,purpose,idempotency_key,request_hash,request_json,status) "
            "VALUES (%s,%s,%s,%s,%s,'123','copy',%s,'hash','{}','SUCCEEDED')",
            (f"import-{user}", user, f"copy-{user}", f"copy-asset-{user}", platform, user),
        )


def _enqueue_copy(dsn: str, user: str, key: str) -> Any:
    from app.auth import CurrentUser
    from app.script_from_audio import enqueue_script_from_audio_task

    with psycopg.connect(dsn) as raw:
        return enqueue_script_from_audio_task(
            BusinessConnection.postgres(raw),
            actor=CurrentUser(user, user, user, "employee"),
            project_id=f"copy-{user}",
            source_asset_id=f"copy-asset-{user}",
            idempotency_key=key,
        )


def _copy_fixture(dsn: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    from app import script_from_audio as pipeline
    from app.asr import TranscriptResult

    _seed_base(dsn)
    _exec(dsn, "DELETE FROM billing_tariffs WHERE service='asr'")
    _exec(
        dsn,
        "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
        "VALUES ('asr',true,2,1)",
    )
    calls: list[str] = []

    class Provider:
        name = "fake-copy-asr"

        def transcribe(self, url: str, *, duration_sec: float | None = None) -> TranscriptResult:
            calls.append(url)
            return TranscriptResult("服务器共享的原视频文案", 12, "zh")

    monkeypatch.setattr(pipeline, "_configured_asr", lambda conn: Provider())
    monkeypatch.setattr(pipeline, "resolve_media_binary", lambda name: name)
    monkeypatch.setattr(
        pipeline, "extract_audio", lambda binary, source, dest: dest.write_bytes(b"audio")
    )
    monkeypatch.setattr(pipeline, "probe_duration_seconds", lambda binary, path: 12)
    storage = FakeStorageAdapter(provider="fake", bucket="copies")
    storage.put_object("source.mp4", b"video", content_type="video/mp4")
    return storage, calls


def _run_copy(dsn: str, storage: Any, worker: str = "copy-worker") -> Any:
    from app.generation_worker import _run_audio_lease

    lease = _audio_lease(dsn, worker)
    assert lease is not None
    _run_audio_lease(lease, storage=storage, connection=_pg_audio_connection)
    return lease


def test_viral_copy_cache_reuses_result_but_bills_each_request(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    first = _enqueue_copy(pg_state, "u1", "first")
    _run_copy(pg_state, storage)
    assert len(calls) == 1
    second = _enqueue_copy(pg_state, "u2", "second")
    assert second["status"] == "SUCCEEDED"
    assert json.loads(second["result_json"])["text"] == "服务器共享的原视频文案"
    assert second["id"] != first["id"]
    assert _enqueue_copy(pg_state, "u2", "second")["id"] == second["id"]
    _exec(pg_state, "UPDATE billing_tariffs SET unit_credits=3 WHERE service='asr'")
    third = _enqueue_copy(pg_state, "u2", "third")
    assert third["status"] == "SUCCEEDED"
    assert len(calls) == 1
    assert _one(pg_state, "SELECT count(*) FROM billing_attempts WHERE service='asr'") == 1
    assert sorted(
        r["charged_credits"]
        for r in _rows(
            pg_state, "SELECT charged_credits FROM billing_operations WHERE service='asr'"
        )
    ) == [24, 24, 36]
    assert _one(pg_state, "SELECT sum(reserved_credits) FROM wallets") == 0


def test_viral_copy_cache_waiters_do_not_submit_twice(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    _enqueue_copy(pg_state, "u1", "first")
    producer = _audio_lease(pg_state, "producer")
    assert producer is not None
    follower = _enqueue_copy(pg_state, "u2", "second")
    _run_copy(pg_state, storage, "follower")
    assert calls == []
    assert _task_status(pg_state, "script_from_audio_tasks", follower["id"]) == "PENDING"
    assert "请稍候" in _one(
        pg_state,
        "SELECT error_message_redacted FROM script_from_audio_tasks WHERE id=%s",
        (follower["id"],),
    )
    from app.generation_worker import _run_audio_lease

    _run_audio_lease(producer, storage=storage, connection=_pg_audio_connection)
    _exec(
        pg_state,
        "UPDATE script_from_audio_tasks SET next_attempt_at=NULL WHERE id=%s",
        (follower["id"],),
    )
    _run_copy(pg_state, storage)
    assert len(calls) == 1
    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM billing_operations WHERE service='asr' AND state='SUCCEEDED'",
        )
        == 2
    )


def test_viral_copy_cache_hit_needs_no_provider_or_media(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _enqueue_copy(pg_state, "u1", "first")
    _run_copy(pg_state, storage)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Cache hit must not access provider/media")

    monkeypatch.setattr("app.script_from_audio._configured_asr", unavailable)
    monkeypatch.setattr(storage, "get_object", unavailable)
    monkeypatch.setattr("app.script_from_audio.resolve_media_binary", unavailable)
    assert _enqueue_copy(pg_state, "u1", "again")["status"] == "SUCCEEDED"
    assert len(calls) == 1


@pytest.mark.parametrize("platform,imported", [("wechat_channels", True), ("douyin", False)])
def test_viral_copy_cache_platform_and_trusted_import_isolation(
    pg_state: str, monkeypatch: pytest.MonkeyPatch, platform: str, imported: bool
) -> None:
    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2", platform=platform, imported=imported)
    _enqueue_copy(pg_state, "u1", "first")
    _run_copy(pg_state, storage)
    assert _enqueue_copy(pg_state, "u2", "second")["status"] == "PENDING"
    _run_copy(pg_state, storage)
    assert len(calls) == 2


def test_script_from_audio_claim_is_exclusive(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-asset")  # ensures asset-ref exists
    _seed_audio_task(pg_state, task_id="sfa-1")
    first = _audio_lease(pg_state, "worker-a")
    assert first is not None
    assert _audio_lease(pg_state, "worker-b") is None


@pytest.mark.parametrize("legacy", [False, True])
def test_viral_copy_cache_uncertain_never_resubmits(
    pg_state: str, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    from fastapi import HTTPException

    from app.asr import AsrSubmissionUncertain

    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    first = _enqueue_copy(pg_state, "u1", "first")
    lease = _audio_lease(pg_state, "producer")
    assert lease is not None
    with pg_transaction() as raw:
        from app.script_from_audio import mark_script_from_audio_submission_started

        conn = BusinessConnection.postgres(raw)
        mark_script_from_audio_submission_started(conn, lease=lease)
        fail_script_from_audio_task(
            conn, lease=lease, cause=AsrSubmissionUncertain("unknown"), submission_started=True
        )
    if legacy:
        _exec(pg_state, "DELETE FROM viral_script_cache")
        _exec(
            pg_state,
            "UPDATE script_from_audio_tasks SET request_json=%s WHERE id=%s",
            (json.dumps({"source_asset_id": "copy-asset-u1"}), first["id"]),
        )
    with pytest.raises(HTTPException) as error:
        _enqueue_copy(pg_state, "u2", "second")
    assert error.value.detail["code"] == "SCRIPT_FROM_AUDIO_CACHE_UNCERTAIN"
    assert calls == []
    assert _one(pg_state, "SELECT available_credits FROM wallets WHERE user_id='u2'") == 1000


def test_viral_copy_cache_adopts_legacy_running_then_success(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    first = _enqueue_copy(pg_state, "u1", "first")
    producer = _audio_lease(pg_state, "producer")
    assert producer is not None
    _exec(pg_state, "DELETE FROM viral_script_cache")
    _exec(
        pg_state,
        "UPDATE script_from_audio_tasks SET request_json=%s WHERE id=%s",
        (json.dumps({"source_asset_id": "copy-asset-u1"}), first["id"]),
    )
    follower = _enqueue_copy(pg_state, "u2", "second")
    _run_copy(pg_state, storage, "follower")
    assert calls == []
    from app.generation_worker import _run_audio_lease

    _run_audio_lease(producer, storage=storage, connection=_pg_audio_connection)
    _exec(
        pg_state,
        "UPDATE script_from_audio_tasks SET next_attempt_at=NULL WHERE id=%s",
        (follower["id"],),
    )
    _run_copy(pg_state, storage)
    assert len(calls) == 1
    assert _task_status(pg_state, "script_from_audio_tasks", follower["id"]) == "SUCCEEDED"


def test_viral_copy_cache_adopts_history_and_survives_project_deletion(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    _enqueue_copy(pg_state, "u1", "first")
    _run_copy(pg_state, storage)
    _exec(pg_state, "DELETE FROM viral_script_cache")
    assert _enqueue_copy(pg_state, "u2", "second")["status"] == "SUCCEEDED"
    _exec(pg_state, "DELETE FROM projects WHERE id='copy-u1'")
    assert _enqueue_copy(pg_state, "u2", "third")["status"] == "SUCCEEDED"
    assert len(calls) == 1


def test_viral_copy_cache_deleted_inflight_producer_stays_blocked(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    _, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    _enqueue_copy(pg_state, "u1", "first")
    _exec(pg_state, "DELETE FROM projects WHERE id='copy-u1'")
    with pytest.raises(HTTPException) as error:
        _enqueue_copy(pg_state, "u2", "second")
    assert error.value.detail["code"] == "SCRIPT_FROM_AUDIO_CACHE_UNCERTAIN"
    assert calls == []


def test_viral_copy_cache_insufficient_balance_and_wrong_owner_cannot_read(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from app.auth import CurrentUser
    from app.script_from_audio import enqueue_script_from_audio_task

    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    _enqueue_copy(pg_state, "u1", "first")
    _run_copy(pg_state, storage)
    _exec(pg_state, "UPDATE wallets SET available_credits=0 WHERE user_id='u2'")
    with pytest.raises(HTTPException) as error:
        _enqueue_copy(pg_state, "u2", "second")
    assert error.value.status_code == 402
    assert (
        _one(pg_state, "SELECT count(*) FROM script_from_audio_tasks WHERE project_id='copy-u2'")
        == 0
    )
    with pytest.raises(HTTPException):
        with psycopg.connect(pg_state) as raw:
            enqueue_script_from_audio_task(
                BusinessConnection.postgres(raw),
                actor=CurrentUser("u2", "u2", "u2", "employee"),
                project_id="copy-u1",
                source_asset_id="copy-asset-u1",
                idempotency_key="unauthorized",
            )
    assert len(calls) == 1


def test_viral_copy_cache_new_key_while_active_is_not_silently_free(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    first = _enqueue_copy(pg_state, "u1", "first")
    assert _enqueue_copy(pg_state, "u1", "first")["id"] == first["id"]
    with pytest.raises(HTTPException) as error:
        _enqueue_copy(pg_state, "u1", "different")
    assert error.value.status_code == 409
    assert _one(pg_state, "SELECT count(*) FROM billing_operations WHERE service='asr'") == 1


@pytest.mark.parametrize("same_request", [True, False])
def test_viral_copy_cache_simultaneous_enqueues_have_one_producer(
    pg_state: str, monkeypatch: pytest.MonkeyPatch, same_request: bool
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app import script_from_audio as pipeline

    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    barrier = Barrier(2)
    original_lock = pipeline._lock_script_cache

    def simultaneous(conn: BusinessConnection, source: tuple[str, str]) -> Any:
        barrier.wait(timeout=10)
        return original_lock(conn, source)

    monkeypatch.setattr(pipeline, "_lock_script_cache", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_enqueue_copy, pg_state, "u1", "first")
        b = pool.submit(_enqueue_copy, pg_state, "u1" if same_request else "u2", "first")
        rows = [a.result(timeout=15), b.result(timeout=15)]
    monkeypatch.setattr(pipeline, "_lock_script_cache", original_lock)
    assert len({row["id"] for row in rows}) == (1 if same_request else 2)
    assert _one(pg_state, "SELECT count(*) FROM viral_script_cache") == 1
    _run_copy(pg_state, storage)
    if not same_request:
        _run_copy(pg_state, storage)
    assert len(calls) == 1
    assert _one(pg_state, "SELECT count(*) FROM billing_operations WHERE service='asr'") == (
        1 if same_request else 2
    )
    assert _one(
        pg_state,
        "SELECT count(*) FROM audit_logs WHERE action='project.script_from_audio_enqueued'",
    ) == (1 if same_request else 2)


def test_viral_copy_cache_presubmission_failure_allows_waiter_to_recover(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    _enqueue_copy(pg_state, "u1", "first")
    lease = _audio_lease(pg_state, "producer")
    assert lease is not None
    _enqueue_copy(pg_state, "u2", "second")
    with pg_transaction() as raw:
        fail_script_from_audio_task(
            BusinessConnection.postgres(raw),
            lease=lease,
            cause=RuntimeError("media unavailable before ASR"),
            submission_started=False,
        )
    _run_copy(pg_state, storage)
    assert len(calls) == 1
    assert _one(pg_state, "SELECT available_credits FROM wallets WHERE user_id='u1'") == 1000
    assert _one(pg_state, "SELECT available_credits FROM wallets WHERE user_id='u2'") == 976


@pytest.mark.parametrize("text,duration", [("", 12), ("text", None), ("text", float("nan"))])
def test_viral_copy_cache_invalid_result_is_not_cached_or_charged(
    pg_state: str, monkeypatch: pytest.MonkeyPatch, text: str, duration: float | None
) -> None:
    from app.asr import TranscriptResult

    storage, _ = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _enqueue_copy(pg_state, "u1", "first")

    class InvalidProvider:
        name = "fake-invalid-asr"

        def transcribe(self, url: str, *, duration_sec: float | None = None) -> TranscriptResult:
            return TranscriptResult(text, duration, "zh")

    monkeypatch.setattr("app.script_from_audio._configured_asr", lambda conn: InvalidProvider())
    _run_copy(pg_state, storage)
    assert _one(pg_state, "SELECT result_json FROM viral_script_cache") is None
    assert _one(pg_state, "SELECT available_credits FROM wallets WHERE user_id='u1'") == 1000
    assert _one(pg_state, "SELECT reserved_credits FROM wallets WHERE user_id='u1'") == 0


def test_viral_copy_cache_legacy_completion_during_lookup_does_not_resubmit(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import script_from_audio as pipeline
    from app.generation_worker import _run_audio_lease

    storage, calls = _copy_fixture(pg_state, monkeypatch)
    _seed_viral_copy(pg_state, "u1")
    _seed_viral_copy(pg_state, "u2")
    first = _enqueue_copy(pg_state, "u1", "first")
    producer = _audio_lease(pg_state, "producer")
    assert producer is not None
    _exec(pg_state, "DELETE FROM viral_script_cache")
    _exec(
        pg_state,
        "UPDATE script_from_audio_tasks SET request_json=%s WHERE id=%s",
        (json.dumps({"source_asset_id": "copy-asset-u1"}), first["id"]),
    )
    original = pipeline._historical_transcripts

    def finish_between_reads(
        conn: BusinessConnection, source: tuple[str, str], *, exclude: str | None = None
    ) -> Any:
        rows = original(conn, source, exclude=exclude)
        _run_audio_lease(producer, storage=storage, connection=_pg_audio_connection)
        return rows

    monkeypatch.setattr(pipeline, "_historical_transcripts", finish_between_reads)
    assert _enqueue_copy(pg_state, "u2", "second")["status"] == "SUCCEEDED"
    assert len(calls) == 1


def test_viral_copy_cache_failed_legacy_leader_does_not_hide_other_uncertain_task(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    _, calls = _copy_fixture(pg_state, monkeypatch)
    for user in ("u1", "u2", "u3"):
        _seed_viral_copy(pg_state, user)
    first = _enqueue_copy(pg_state, "u1", "first")
    second = _enqueue_copy(pg_state, "u2", "second")
    for user, task in (("u1", first), ("u2", second)):
        _exec(
            pg_state,
            "UPDATE script_from_audio_tasks SET request_json=%s WHERE id=%s",
            (json.dumps({"source_asset_id": f"copy-asset-{user}"}), task["id"]),
        )
    _exec(
        pg_state, "UPDATE script_from_audio_tasks SET status='FAILED' WHERE id=%s", (first["id"],)
    )
    _exec(
        pg_state,
        "UPDATE script_from_audio_tasks SET status='SUBMISSION_UNCERTAIN',"
        "provider_started_at=now()::text WHERE id=%s",
        (second["id"],),
    )
    with pytest.raises(HTTPException) as error:
        _enqueue_copy(pg_state, "u3", "third")
    assert error.value.detail["code"] == "SCRIPT_FROM_AUDIO_CACHE_UNCERTAIN"
    assert _one(pg_state, "SELECT available_credits FROM wallets WHERE user_id='u3'") == 1000
    assert calls == []


@pytest.mark.parametrize("existing_key", [None, "legacy/input.m4a"])
def test_asr_audio_uses_project_storage_and_preserves_existing_receipt(
    pg_state: str, monkeypatch: pytest.MonkeyPatch, existing_key: str | None
) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-asset")
    _seed_audio_task(pg_state, task_id="sfa-storage", audio_object_key=existing_key)
    _exec(
        pg_state,
        "UPDATE script_from_audio_tasks SET request_json=%s WHERE id='sfa-storage'",
        (json.dumps({"source_asset_id": "asset-ref"}),),
    )
    monkeypatch.setattr("app.script_from_audio.resolve_media_binary", lambda name: name)
    monkeypatch.setattr("app.script_from_audio._configured_asr", lambda conn: object())
    lease = _audio_lease(pg_state, "worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        work = prepare_script_from_audio_task(
            BusinessConnection.postgres(raw),
            lease=lease,
            storage=FakeStorageAdapter(provider="cos", bucket="bucket"),
        )
    assert work.audio_object_key == (existing_key or "projects/proj-1/asr/sfa-storage.m4a")


def test_asr_cleanup_failure_backs_off_and_keeps_the_object_receipt(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-asset")
    _seed_audio_task(
        pg_state,
        task_id="sfa-cleanup",
        status="FAILED",
        audio_object_key="projects/proj-1/asr/cleanup.m4a",
    )
    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
    calls: list[str] = []

    def failed_delete(key: str, *, actor_id: str | None = None) -> None:
        calls.append(key)
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(storage, "delete_object", failed_delete)
    _cleanup_audio_objects(_pg_audio_connection, storage)
    _cleanup_audio_objects(_pg_audio_connection, storage)
    assert calls == ["projects/proj-1/asr/cleanup.m4a"]
    row = _rows(
        pg_state,
        "SELECT audio_object_key,next_attempt_at FROM script_from_audio_tasks "
        "WHERE id='sfa-cleanup'",
    )[0]
    assert row["audio_object_key"] == calls[0]
    assert row["next_attempt_at"] is not None
    _exec(
        pg_state, "UPDATE script_from_audio_tasks SET next_attempt_at=NULL WHERE id='sfa-cleanup'"
    )
    monkeypatch.setattr(storage, "delete_object", lambda key, **kwargs: calls.append(key))
    _cleanup_audio_objects(_pg_audio_connection, storage)
    assert len(calls) == 2
    assert (
        _rows(
            pg_state, "SELECT audio_object_key FROM script_from_audio_tasks WHERE id='sfa-cleanup'"
        )[0]["audio_object_key"]
        is None
    )


def test_script_from_audio_expired_presubmission_lease_resets_to_pending(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-asset")
    _seed_audio_task(pg_state, task_id="sfa-1", status="RUNNING")
    _exec(
        pg_state,
        "UPDATE script_from_audio_tasks SET locked_by = 'worker-a',"
        " locked_until = now() - interval '5 minutes' WHERE id = 'sfa-1'",
    )
    # No provider submission happened: the lease resets to PENDING and the
    # task is safely re-claimable (the audio receipt stays durable).
    reacquired = _audio_lease(pg_state, "worker-b")
    assert reacquired is not None
    row = _rows(
        pg_state,
        "SELECT status, locked_by, attempt FROM script_from_audio_tasks WHERE id = 'sfa-1'",
    )[0]
    assert row["status"] == "RUNNING"
    assert row["locked_by"] == "worker-b"
    assert int(row["attempt"]) == 1


def test_script_from_audio_expired_inflight_submission_quarantines(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-asset")
    _seed_audio_task(pg_state, task_id="sfa-1", status="RUNNING")
    _exec(
        pg_state,
        "UPDATE script_from_audio_tasks SET locked_by = 'worker-a',"
        " provider_started_at = now()::text, provider_task_id = NULL,"
        " locked_until = now() - interval '5 minutes' WHERE id = 'sfa-1'",
    )
    assert _audio_lease(pg_state, "worker-b") is None
    row = _rows(
        pg_state,
        "SELECT status, error_code, retryable FROM script_from_audio_tasks WHERE id = 'sfa-1'",
    )[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert row["error_code"] == "SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN"
    assert int(row["retryable"]) == 0


def test_script_from_audio_provider_failure_is_terminal_without_resubmit(
    pg_state: str,
) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-asset")
    _seed_audio_task(pg_state, task_id="sfa-1")
    lease = _audio_lease(pg_state, "worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        fail_script_from_audio_task(
            conn,
            lease=lease,
            cause=RuntimeError("ASR provider timeout after 30s"),
            submission_started=True,
        )
    row = _rows(
        pg_state,
        "SELECT status, retryable FROM script_from_audio_tasks WHERE id = 'sfa-1'",
    )[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert int(row["retryable"]) == 0
    assert _audio_lease(pg_state, "worker-b") is None, "never blind-resubmit a paid ASR call"


def test_script_from_audio_receipt_survives_retry(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_source_frame_task(pg_state, task_id="sf-asset")
    _seed_audio_task(pg_state, task_id="sfa-1", audio_object_key="audio/sfa-1.opus")
    lease = _audio_lease(pg_state, "worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        fail_script_from_audio_task(
            conn,
            lease=lease,
            cause=RuntimeError("transcription worker crashed after upload"),
            submission_started=False,
        )
    row = _rows(
        pg_state,
        "SELECT audio_object_key FROM script_from_audio_tasks WHERE id = 'sfa-1'",
    )[0]
    assert row["audio_object_key"] == "audio/sfa-1.opus", (
        "the durable audio receipt must survive a worker retry"
    )


# ---------------------------------------------------------------------------
# H. 混合队列与两设备
# ---------------------------------------------------------------------------


def test_mixed_h3_and_independent_rotation_within_frozen_bounds(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_generation_task(pg_state, task_id="h3-u1", user_id="u1")
    _seed_generation_task(
        pg_state, task_id="ind-u2", user_id="u2", creation_kind="independent", project_id=None
    )
    _seed_generation_task(pg_state, task_id="h3-u3", user_id="u3")
    served: set[str] = set()
    rounds = 0
    while rounds < 8 and len(served) < 3:
        rounds += 1
        _run_worker(f"mixed-worker-{rounds}", max_tasks=3)
        served = {
            str(row["id"])
            for row in _rows(pg_state, "SELECT id FROM generation_tasks WHERE status = 'SUCCEEDED'")
        }
    assert len(served) == 3, f"every user must be served within the 8-round bound: {served}"
    assert rounds <= 8
    for user in ("u1", "u2", "u3"):
        assert _cursor_count(pg_state, user) == 0


def test_second_device_cannot_grow_same_user_running_slot(pg_state: str) -> None:
    _seed_base(pg_state)
    # Device A submits one task; a worker claims it (running slot = 1).
    _seed_generation_task(pg_state, task_id="dev-a-task", user_id="u1")
    first = _acquire(pg_state, "worker-device-a")
    assert first is not None
    assert _cursor_count(pg_state, "u1") == 1
    # Device B (same user, second device/session) submits another task.
    _seed_generation_task(pg_state, task_id="dev-b-task", user_id="u1")
    blocked = _acquire(pg_state, "worker-device-b")
    assert blocked is None, "the second device must not grow running_tasks_count past 1"
    assert (
        _one(pg_state, "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id = 'u1'")
        == 1
    )


def test_first_frame_quality_lease_is_short_and_generation_can_extend_it(pg_state):
    from app.image_tasks import renew_image_task_lease

    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="quality-lease")
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    with pg_transaction() as raw:
        renew_image_task_lease(
            BusinessConnection.postgres(raw),
            table="first_frame_tasks",
            lease=lease,
            lease_minutes=3,
        )
    seconds = float(
        _one(
            pg_state,
            "SELECT extract(epoch FROM locked_until::timestamptz-now()) "
            "FROM first_frame_tasks WHERE id='quality-lease'",
        )
    )
    assert 0 < seconds <= 180
    with pg_transaction() as raw:
        renew_image_task_lease(
            BusinessConnection.postgres(raw), table="first_frame_tasks", lease=lease
        )
    seconds = float(
        _one(
            pg_state,
            "SELECT extract(epoch FROM locked_until::timestamptz-now()) "
            "FROM first_frame_tasks WHERE id='quality-lease'",
        )
    )
    assert seconds > 1700


def test_compiled_video_prompt_uses_confirmed_image_instead_of_source_appearance():
    from app.generation import compile_prompt_text

    prompt = compile_prompt_text(
        script_payload={"full_text": "先把建房预算规划好。"},
        shot_payload={
            "shots": [
                {
                    "shot_id": "shot-1",
                    "start_time": 0,
                    "end_time": 4,
                    "shot_type": "中景",
                    "composition": "人物居中",
                    "camera_motion": "缓慢拉远",
                    "subject": "穿浅色条纹衬衫的男子",
                    "action": "右手抬起做手势，左手拿图纸",
                    "scene": "建筑工地，背景有农田和房屋",
                    "transition": "无",
                }
            ],
        },
        source_duration_seconds=4,
        duration_seconds=4,
        resolution="768P",
    )
    assert "穿浅色条纹衬衫" not in prompt
    assert "主体：已确认首帧中的人物" in prompt
    for preserved in [
        "人物居中",
        "缓慢拉远",
        "右手抬起做手势，左手拿图纸",
        "建筑工地，背景有农田和房屋",
        "先把建房预算规划好。",
    ]:
        assert preserved in prompt


@pytest.mark.parametrize("manual_review", [False, True])
def test_first_frame_human_review_records_user_without_fake_qc_pass(
    pg_state, monkeypatch, manual_review
):
    from app import first_frames
    from app.auth import CurrentUser

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO assets (id,project_id,kind,storage_uri,sha256,size_bytes,"
        "content_type,created_by_user_id) VALUES ('manual-frame','proj-1','first_frame',"
        "'cos://qa/manual.png','hash',20,'image/png','u1')",
    )
    payload = {"candidates": [{"asset_id": "manual-frame", "quality": None}]}
    if manual_review:
        payload["review_mode"] = "HUMAN_CONFIRMATION"
    from app.analysis import insert_version

    with pg_transaction() as raw:
        candidate = insert_version(
            BusinessConnection.postgres(raw),
            project_id="proj-1",
            asset_id="manual-frame",
            kind="first_frame_candidates",
            created_by_user_id="u1",
            payload=payload,
        )
    monkeypatch.setattr(
        first_frames,
        "current_first_frame_candidates",
        lambda *args, **kwargs: candidate,
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        actor = CurrentUser(id="u1", username="u1", display_name="User One", role="employee")
        row = first_frames.confirm_first_frame(
            conn, project_id="proj-1", first_frame_asset_id="manual-frame", actor=actor
        )
        stored = json.loads(row["payload_json"])
        assert stored["review_mode"] == "HUMAN_CONFIRMATION"
        assert stored["reviewed_by_user_id"] == "u1"
        assert "quality_override" not in stored
        from app.generation import confirmed_first_frame_sources

        sources = confirmed_first_frame_sources(
            conn, project_id="proj-1", first_frame_asset_id="manual-frame"
        )
        assert sources["first_frame_selection_version_id"] == str(row["id"])
        for unverified_reviewer in (None, "u2"):
            insert_version(
                conn,
                project_id="proj-1",
                asset_id="manual-frame",
                kind="first_frame_selection",
                created_by_user_id="u1",
                payload={**stored, "reviewed_by_user_id": unverified_reviewer},
            )
            with pytest.raises(HTTPException) as exc:
                confirmed_first_frame_sources(
                    conn, project_id="proj-1", first_frame_asset_id="manual-frame"
                )
            assert exc.value.detail["code"] == "FIRST_FRAME_QUALITY_NOT_VERIFIED"
    assert _rows(
        pg_state, "SELECT available_credits,reserved_credits FROM wallets WHERE user_id='u1'"
    )[0] == {"available_credits": 1000, "reserved_credits": 0}


def _seed_first_frame_asset(dsn: str, asset_id: str) -> None:
    _exec(
        dsn,
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, created_by_user_id)"
        " VALUES (%s, 'proj-1', 'first_frame', %s, %s, 20, 'image/png', 'u1')",
        (asset_id, f"cos://qa/{asset_id}.png", asset_id + "f" * 60),
    )


def test_first_frame_confirm_accepts_explicit_history_candidates_version(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """置换首帧历史版本放开：显式带上候选版本 id 时，确认不再要求
    “必须是最新候选集”；selection 记录该历史版本，生成侧（来源绑定与 H3
    门禁）跟随 selection 指向的版本读取，且历史确认跳过 b5 输入新鲜度检查
    （基于旧输入的警示由前端承担）。"""
    from app import first_frames
    from app.analysis import insert_version
    from app.auth import CurrentUser
    from app.generation import confirmed_first_frame_sources, require_confirmed_first_frame

    _seed_base(pg_state)
    _seed_first_frame_asset(pg_state, "ff-hist")
    _seed_first_frame_asset(pg_state, "ff-new")
    actor = CurrentUser(id="u1", username="u1", display_name="User One", role="employee")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        history = insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-hist",
            kind="first_frame_candidates",
            created_by_user_id="u1",
            payload={
                "schema_version": "b5.first-frame.v1",
                "candidates": [{"asset_id": "ff-hist", "quality": None}],
            },
        )
        insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-new",
            kind="first_frame_candidates",
            created_by_user_id="u1",
            payload={
                "schema_version": "b5.first-frame.v1",
                "candidates": [{"asset_id": "ff-new", "quality": None}],
            },
        )
        row = first_frames.confirm_first_frame(
            conn,
            project_id="proj-1",
            first_frame_asset_id="ff-hist",
            actor=actor,
            first_frame_candidates_version_id=str(history["id"]),
        )
        stored = json.loads(str(row["payload_json"]))
        assert stored["first_frame_candidates_version_id"] == str(history["id"])
        assert stored["first_frame_asset_id"] == "ff-hist"

        sources = confirmed_first_frame_sources(
            conn, project_id="proj-1", first_frame_asset_id="ff-hist"
        )
        assert sources["first_frame_candidates_version_id"] == str(history["id"])
        assert sources["first_frame_selection_version_id"] == str(row["id"])

        def _stale(*args: Any, **kwargs: Any) -> None:
            raise first_frames.stale_first_frame_inputs()

        monkeypatch.setattr(first_frames, "current_first_frame_candidates", _stale)
        # 历史确认不因“当前候选集已过期”被拦：b5 新鲜度检查只作用于指向
        # 最新候选版本的确认。
        require_confirmed_first_frame(conn, project_id="proj-1", first_frame_asset_id="ff-hist")


def test_first_frame_generation_gate_keeps_b5_freshness_for_latest_selection(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """selection 指向最新候选版本时，b5 输入新鲜度检查保持原判：源画面或
    人物已变化（最新候选集过期）→ H3 门禁必须 409，不能沉默放行。"""
    from app import first_frames
    from app.analysis import insert_version

    _seed_base(pg_state)
    _seed_first_frame_asset(pg_state, "ff-now")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        candidates = insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-now",
            kind="first_frame_candidates",
            created_by_user_id="u1",
            payload={
                "schema_version": "b5.first-frame.v1",
                "candidates": [{"asset_id": "ff-now", "quality": None}],
            },
        )
        insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-now",
            kind="first_frame_selection",
            created_by_user_id="u1",
            payload={
                "first_frame_candidates_version_id": str(candidates["id"]),
                "first_frame_asset_id": "ff-now",
                "review_mode": "HUMAN_CONFIRMATION",
                "reviewed_by_user_id": "u1",
            },
        )

        def _stale(*args: Any, **kwargs: Any) -> None:
            raise first_frames.stale_first_frame_inputs()

        monkeypatch.setattr(first_frames, "current_first_frame_candidates", _stale)
        from app.generation import require_confirmed_first_frame

        with pytest.raises(HTTPException) as exc:
            require_confirmed_first_frame(conn, project_id="proj-1", first_frame_asset_id="ff-now")
        assert exc.value.detail["code"] == "FIRST_FRAME_CONFIRMATION_REQUIRED"


def test_first_frame_confirm_default_path_still_accepts_latest_set_only(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不带版本的确认保持原契约：只在最新候选集里找 asset，历史图仍报
    422（客户端必须显式带上目标版本 id 才能确认历史图）。"""
    from app import first_frames
    from app.analysis import insert_version
    from app.auth import CurrentUser

    _seed_base(pg_state)
    _seed_first_frame_asset(pg_state, "ff-hist")
    _seed_first_frame_asset(pg_state, "ff-new")
    actor = CurrentUser(id="u1", username="u1", display_name="User One", role="employee")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-hist",
            kind="first_frame_candidates",
            created_by_user_id="u1",
            payload={"candidates": [{"asset_id": "ff-hist", "quality": None}]},
        )
        latest = insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-new",
            kind="first_frame_candidates",
            created_by_user_id="u1",
            payload={"candidates": [{"asset_id": "ff-new", "quality": None}]},
        )
        monkeypatch.setattr(
            first_frames, "current_first_frame_candidates", lambda *args, **kwargs: latest
        )
        with pytest.raises(HTTPException) as exc:
            first_frames.confirm_first_frame(
                conn, project_id="proj-1", first_frame_asset_id="ff-hist", actor=actor
            )
        assert exc.value.detail["code"] == "FIRST_FRAME_CANDIDATE_NOT_FOUND"


def test_first_frame_selection_latest_returns_history_confirmation(pg_state: str) -> None:
    """selection/latest 不再对“指向历史候选版本”的已确认首帧整体 409：
    读取端直接返回记录本身（过期与否交给生成门禁与前端警示）。"""
    from app.analysis import insert_version
    from app.auth import CurrentUser
    from app.first_frame_routes import read_latest_first_frame_selection

    _seed_base(pg_state)
    _seed_first_frame_asset(pg_state, "ff-hist")
    _seed_first_frame_asset(pg_state, "ff-new")
    actor = CurrentUser(id="u1", username="u1", display_name="User One", role="employee")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        history = insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-hist",
            kind="first_frame_candidates",
            created_by_user_id="u1",
            payload={"candidates": [{"asset_id": "ff-hist", "quality": None}]},
        )
        insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-new",
            kind="first_frame_candidates",
            created_by_user_id="u1",
            payload={"candidates": [{"asset_id": "ff-new", "quality": None}]},
        )
        selection = insert_version(
            conn,
            project_id="proj-1",
            asset_id="ff-hist",
            kind="first_frame_selection",
            created_by_user_id="u1",
            payload={
                "first_frame_candidates_version_id": str(history["id"]),
                "first_frame_asset_id": "ff-hist",
                "review_mode": "HUMAN_CONFIRMATION",
                "reviewed_by_user_id": "u1",
            },
        )
        response = read_latest_first_frame_selection("proj-1", conn=conn, actor=actor)
    assert response is not None
    assert response.id == str(selection["id"])
    assert response.payload["first_frame_candidates_version_id"] == str(history["id"])


def test_first_frame_async_receipt_is_fenced_and_resumes_original_task(pg_state):
    import json

    from app.image_tasks import (
        _first_frame_checkpoint_candidates,
        _parse_first_frame_submission,
        record_image_task_provider,
        save_first_frame_provider_submission,
    )

    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-1")
    # Production requests are SHA256-bound; the raw fixture uses a placeholder.
    _exec(pg_state, "UPDATE first_frame_tasks SET request_hash=%s WHERE id='ff-1'", (_SHA_A,))
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        lease = acquire_first_frame_task(conn, worker_id="worker-a")
        record_image_task_provider(
            conn, table="first_frame_tasks", lease=lease, provider="apilio", model="gpt-image-2"
        )
        receipt = {
            "schema_version": 1,
            "task_id": "vendor-1",
            "account_fingerprint": _SHA_A,
            "model": "gpt-image-2",
            "output_count": 1,
        }
        save_first_frame_provider_submission(
            conn, lease=lease, submission=receipt, cost_record_id="cost-original"
        )
    row = _rows(pg_state, "SELECT result_json FROM first_frame_tasks WHERE id='ff-1'")[0]
    assert _first_frame_checkpoint_candidates(row) == []
    assert _parse_first_frame_submission(row["result_json"])["cost_record_id"] == "cost-original"
    _expire_running_lease(pg_state, "first_frame_tasks", "ff-1")
    with pg_transaction() as raw:
        resumed = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-b")
    assert resumed.attempt == 2
    with pg_transaction() as raw:
        with pytest.raises((RuntimeError, HTTPException)):
            save_first_frame_provider_submission(
                BusinessConnection.postgres(raw),
                lease=lease,
                submission={**receipt, "task_id": "must-not-overwrite"},
            )
    row = _rows(pg_state, "SELECT result_json FROM first_frame_tasks WHERE id='ff-1'")[0]
    assert json.loads(row["result_json"])["provider_submission"]["task_id"] == "vendor-1"


def _seed_source_frame_asset(dsn: str, asset_id: str = "source-asset") -> None:
    _exec(
        dsn,
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, created_by_user_id)"
        " VALUES (%s, 'proj-1', 'source_frame', 'fake://cos/source.png', %s, 9,"
        " 'image/png', 'u1')",
        (asset_id, _SHA_A),
    )


def _single_shot_work(fingerprint: str, *, main_character_version_id: str = "char-v1"):
    from types import SimpleNamespace

    character_inputs = SimpleNamespace(
        main_character_version_id=main_character_version_id,
        character_reference_selection_id=None,
        character_version_id=main_character_version_id,
        reference_asset_ids=["scene"],
        reference_asset_roles=["scene_image"],
        character_snapshot={},
        character_name="张工",
    )
    appearance = SimpleNamespace(
        fingerprint=fingerprint,
        source_timestamp_seconds=0,
        appearance_source="SCENE_LOOK",
        as_payload=lambda: {"fingerprint": fingerprint},
    )
    return SimpleNamespace(
        project_id="proj-1",
        actor=SimpleNamespace(id="u1"),
        model="gpt-image-2",
        effective_prompt="template",
        source_frame_selection_version_id="sel-1",
        source_frame_asset_id="source-asset",
        character_inputs=character_inputs,
        project_appearance=appearance,
        aspect_ratio="9:16",
        replace_scene=False,
        quantity=1,
    )


def _candidate(asset_id: str) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "storage_key": f"projects/proj-1/first-frames/{asset_id}.png",
        "storage_uri": f"fake://cos/projects/proj-1/first-frames/{asset_id}.png",
        "sha256": _SHA_A,
        "size_bytes": 9,
        "content_type": "image/png",
        "quality": None,
    }


def _latest_first_frame_payload(dsn: str) -> dict[str, Any]:
    row = _rows(
        dsn,
        "SELECT payload_json FROM versions WHERE project_id='proj-1'"
        " AND kind='first_frame_candidates' ORDER BY version_number DESC LIMIT 1",
    )[0]
    return cast(dict[str, Any], json.loads(str(row["payload_json"])))


def test_first_frame_single_shot_regen_accumulates_into_latest_candidates(pg_state, monkeypatch):
    """单张产品流：每次付费任务交付 1 张；同输入绑定的再次生成把新候选
    追加进最新候选版本（确认与 H3 围栏只看最新版本，历史图必须仍可选）。"""
    from types import SimpleNamespace

    from app import first_frames as ff
    from app.first_frames import (
        StoredFirstFrameCandidates,
        complete_first_frame_generation,
    )

    _seed_base(pg_state)
    _seed_source_frame_asset(pg_state)
    work = _single_shot_work(_SHA_A)
    provider = SimpleNamespace(provider_name="apilio")
    monkeypatch.setattr(ff, "require_current_first_frame_inputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ff, "resolve_project_appearance_spec", lambda *args, **kwargs: work.project_appearance
    )
    monkeypatch.setattr(
        ff, "apply_selected_scene_look", lambda *args, **kwargs: work.project_appearance
    )

    def complete(asset_id: str, *, override_work=None):
        with pg_transaction() as raw:
            return complete_first_frame_generation(
                BusinessConnection.postgres(raw),
                work=override_work or work,
                provider=provider,
                stored=StoredFirstFrameCandidates(
                    candidates=[_candidate(asset_id)], created_assets=[]
                ),
            )

    complete("ff-a1")
    payload = _latest_first_frame_payload(pg_state)
    assert [c["asset_id"] for c in payload["candidates"]] == ["ff-a1"]

    complete("ff-a2")
    payload = _latest_first_frame_payload(pg_state)
    assert [c["asset_id"] for c in payload["candidates"]] == ["ff-a1", "ff-a2"]

    # 输入绑定变化（换人物版本）→ 另起新池，不混入旧输入的候选。
    stale_binding_work = _single_shot_work(_SHA_A, main_character_version_id="char-v2")
    complete("ff-b1", override_work=stale_binding_work)
    payload = _latest_first_frame_payload(pg_state)
    assert [c["asset_id"] for c in payload["candidates"]] == ["ff-b1"]


def test_first_frame_candidate_pool_is_capped_to_newest_six(pg_state, monkeypatch):
    from types import SimpleNamespace

    from app import first_frames as ff
    from app.first_frames import (
        MAX_FIRST_FRAME_CANDIDATE_POOL,
        StoredFirstFrameCandidates,
        complete_first_frame_generation,
    )

    _seed_base(pg_state)
    _seed_source_frame_asset(pg_state)
    work = _single_shot_work(_SHA_A)
    provider = SimpleNamespace(provider_name="apilio")
    monkeypatch.setattr(ff, "require_current_first_frame_inputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ff, "resolve_project_appearance_spec", lambda *args, **kwargs: work.project_appearance
    )
    monkeypatch.setattr(
        ff, "apply_selected_scene_look", lambda *args, **kwargs: work.project_appearance
    )

    for index in range(1, MAX_FIRST_FRAME_CANDIDATE_POOL + 3):
        with pg_transaction() as raw:
            complete_first_frame_generation(
                BusinessConnection.postgres(raw),
                work=work,
                provider=provider,
                stored=StoredFirstFrameCandidates(
                    candidates=[_candidate(f"ff-c{index}")], created_assets=[]
                ),
            )
    payload = _latest_first_frame_payload(pg_state)
    candidates = [c["asset_id"] for c in payload["candidates"]]
    assert len(candidates) == MAX_FIRST_FRAME_CANDIDATE_POOL
    assert candidates == [f"ff-c{index}" for index in range(3, MAX_FIRST_FRAME_CANDIDATE_POOL + 3)]


@pytest.mark.parametrize("replace_scene", [False, True])
def test_first_frame_ratio_is_frozen_in_request_and_idempotency(
    pg_state, monkeypatch, replace_scene
):
    import json
    from types import SimpleNamespace

    from app.first_frames import ApilioImageProvider
    from app.image_tasks import (
        enqueue_first_frame_task,
        load_image_task_actor,
        prepare_first_frame_task,
    )

    _seed_base(pg_state)
    observed = []

    def plan(*args, **kwargs):
        observed.append((kwargs.get("aspect_ratio"), kwargs.get("replace_scene")))
        return SimpleNamespace(
            model="gpt-image-2",
            quantity=1,
            source_frame_selection_version_id="source-selection",
            source_frame_asset_id="source-asset",
            character_inputs=SimpleNamespace(reference_asset_ids=["scene"]),
            project_appearance=SimpleNamespace(
                fingerprint=_SHA_A,
                source_analysis_version_id="analysis",
                appearance_source="SCENE_LOOK",
            ),
        )

    monkeypatch.setattr("app.image_tasks.prepare_first_frame_generation", plan)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        actor = load_image_task_actor(conn, "u1")
        kwargs = dict(
            actor=actor,
            project_id="proj-1",
            model="gpt-image-2",
            prompt=None,
            quantity=1,
            character_version_id=None,
            character_reference_selection_id=None,
            idempotency_key="ratio-key-1",
            replace_scene=replace_scene,
        )
        first = enqueue_first_frame_task(conn, **kwargs, aspect_ratio="9:16")
        assert json.loads(first["request_json"])["aspect_ratio"] == "9:16"
        assert enqueue_first_frame_task(conn, **kwargs, aspect_ratio="9:16")["id"] == first["id"]
    with pg_transaction() as raw:
        with pytest.raises(HTTPException) as conflict:
            enqueue_first_frame_task(
                BusinessConnection.postgres(raw), **kwargs, aspect_ratio="16:9"
            )
        assert conflict.value.status_code == 409
    with pg_transaction() as raw:
        with pytest.raises(HTTPException) as conflict:
            enqueue_first_frame_task(
                BusinessConnection.postgres(raw),
                **{**kwargs, "replace_scene": not replace_scene},
                aspect_ratio="9:16",
            )
        assert conflict.value.status_code == 409
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        lease = acquire_first_frame_task(conn, worker_id="ratio-worker")
        prepare_first_frame_task(
            conn, lease=lease, provider=ApilioImageProvider(api_key="test-key")
        )
    assert observed[-1] == ("9:16", replace_scene)


def test_first_frame_replacement_contract_change_does_not_reuse_old_checkpoint(
    pg_state, monkeypatch
):
    from types import SimpleNamespace

    from app.image_tasks import enqueue_first_frame_task, load_image_task_actor

    _seed_base(pg_state)
    current_fingerprint = {"value": "old-contract-fingerprint"}

    def plan(*args, **kwargs):
        return SimpleNamespace(
            source_frame_selection_version_id="source-selection",
            source_frame_asset_id="source-asset",
            character_inputs=SimpleNamespace(reference_asset_ids=["scene"]),
            project_appearance=SimpleNamespace(
                fingerprint=current_fingerprint["value"],
                source_analysis_version_id="analysis",
            ),
        )

    monkeypatch.setattr("app.image_tasks.prepare_first_frame_generation", plan)
    common = dict(
        project_id="proj-1",
        model="gpt-image-2",
        prompt=None,
        quantity=3,
        character_version_id=None,
        character_reference_selection_id=None,
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        actor = load_image_task_actor(conn, "u1")
        old_task = enqueue_first_frame_task(
            conn,
            actor=actor,
            idempotency_key="old-contract",
            **common,
        )
    _exec(
        pg_state,
        "UPDATE first_frame_tasks SET status='FAILED',result_json=%s WHERE id=%s",
        (_FIRST_FRAME_CHECKPOINT_JSON, str(old_task["id"])),
    )

    current_fingerprint["value"] = "primary-subject-contract-v3"
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        actor = load_image_task_actor(conn, "u1")
        new_task = enqueue_first_frame_task(
            conn,
            actor=actor,
            idempotency_key="primary-subject-contract-v3",
            **common,
        )

    assert new_task["id"] != old_task["id"]
    assert new_task["request_hash"] != old_task["request_hash"]
    assert new_task["result_json"] is None


def test_rewrite_instructions_and_extended_profile_contract():
    from app.script_rewrite import ScriptRewriteRequest, _validated_ip_profile_snapshot
    from app.simple_character_routes import SimpleCharacterProfileRequest

    request = ScriptRewriteRequest(text="来源原文", instructions="精简至 200 字")
    assert request.instructions == "精简至 200 字"
    profile = SimpleCharacterProfileRequest(
        display_name="张工",
        role="乡墅设计师",
        service_scope="乡墅设计",
        target_audience="回乡建房家庭",
        expression_style="朴实",
        audience_needs="预算与布局",
        factual_background="已确认的项目资料",
        sample_script="先规划预算。\n再考虑空间。",
        forbidden_claims="不承诺最低价",
    )
    snapshot = _validated_ip_profile_snapshot(
        {
            "identity_id": "person-1",
            "profile_version": 1,
            **profile.model_dump(),
        }
    )
    assert snapshot["sample_script"] == "先规划预算。\n再考虑空间。"


@pytest.mark.parametrize("purpose", ["rewrite", "analysis", "title", "prompt"])
def test_text_ai_purposes_use_same_deepseek_transport(monkeypatch, purpose):
    from types import SimpleNamespace

    import app.script_rewrite as rewrite

    requests = []

    def respond(url, **_kwargs):
        assert _kwargs["timeout"] == 240
        assert _kwargs["stream"] is False
        assert _kwargs["allow_redirects"] is False
        requests.append((url, _kwargs["data"]))
        return SimpleNamespace(
            status_code=200,
            content=json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": "测试输出"}}]}
            ).encode(),
        )

    monkeypatch.setattr("curl_cffi.requests.post", respond)
    result = rewrite.request_deepseek_text(
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model="deepseek-chat",
        source_text="来源文本",
        instructions="简洁表达",
        purpose=purpose,
    )
    assert result == "测试输出"
    assert requests[0][0] == "https://api.deepseek.com/chat/completions"
    body = json.loads(requests[0][1])
    assert body["model"] == "deepseek-chat"
    assert body["max_tokens"] == 8192
    assert "简洁表达" in body["messages"][-2]["content"]
    assert "来源文本" in body["messages"][-1]["content"]


def test_deepseek_total_deadline_stops_continuous_keep_alive(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread
    from time import monotonic

    import app.script_rewrite as rewrite

    stop = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            try:
                while not stop.wait(0.02):
                    self.wfile.write(b"\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(rewrite, "DEEPSEEK_TIMEOUT_SECONDS", 0.2)
    started = monotonic()
    try:
        with pytest.raises(HTTPException) as failure:
            rewrite.request_deepseek_text(
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key="test-key",
                model="test-model",
                source_text="原文",
            )
        assert failure.value.status_code == 504
        assert monotonic() - started < 2
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_rewrite_submission_refreshes_valid_lease_and_rejects_expired(pg_state):
    _seed_base(pg_state)
    _seed_script_rewrite_task(pg_state, task_id="sr-deadline")
    lease = _rewrite_lease(pg_state, "deadline-worker")
    assert lease is not None
    _exec(
        pg_state,
        "UPDATE script_rewrite_tasks SET locked_until=now()+interval '5 seconds' "
        "WHERE id='sr-deadline'",
    )
    with pg_transaction() as raw:
        mark_script_rewrite_submission_started(BusinessConnection.postgres(raw), lease=lease)
    assert _one(
        pg_state,
        "SELECT locked_until::timestamptz > now()+interval '250 seconds' "
        "FROM script_rewrite_tasks WHERE id='sr-deadline'",
    )
    with pytest.raises(RuntimeError, match="lease was lost"):
        with pg_transaction() as raw:
            mark_script_rewrite_submission_started(BusinessConnection.postgres(raw), lease=lease)
    _exec(
        pg_state,
        "UPDATE script_rewrite_tasks SET locked_until=now()-interval '1 second', "
        "provider_started_at=NULL "
        "WHERE id='sr-deadline'",
    )
    with pytest.raises(RuntimeError, match="lease was lost"):
        with pg_transaction() as raw:
            mark_script_rewrite_submission_started(BusinessConnection.postgres(raw), lease=lease)


def _analysis_with_deepseek(monkeypatch):
    from unittest.mock import Mock

    from app import analysis_routes

    configs = {
        "apilio": {"api_key": "vision-test-key"},
        "deepseek": {"api_key": "text-test-key", "model": "deepseek-chat"},
    }
    repository = Mock()
    repository.load_provider_config.side_effect = lambda provider: configs[provider]
    monkeypatch.setattr(analysis_routes, "SettingsRepository", lambda conn: repository)
    monkeypatch.setattr("app.script_rewrite.SettingsRepository", lambda conn: repository)
    provider = analysis_routes.get_video_analysis_provider(Mock())
    return provider, configs


def test_analysis_factory_does_not_repair_invalid_json_or_switch_visual_provider(monkeypatch):
    from unittest.mock import Mock

    from app.analysis import AnalysisProviderFailed, analyze_video

    provider, _ = _analysis_with_deepseek(monkeypatch)
    provider.transport = Mock()
    provider.transport.post.return_value = (
        b'{"choices":[{"message":{"content":"broken JSON"}}]}',
        {},
    )
    text_request = Mock(side_effect=AssertionError("Unexpected paid JSON repair"))
    monkeypatch.setattr("curl_cffi.requests.post", text_request)
    with pytest.raises(AnalysisProviderFailed, match="未自动调用付费修复"):
        analyze_video(
            video_uri="https://example.com/source.mp4", video_duration_seconds=10, provider=provider
        )
    text_request.assert_not_called()
    assert provider.transport.post.call_count == 1
    assert (
        provider.transport.post.call_args.kwargs["headers"]["Authorization"]
        == "Bearer vision-test-key"
    )
    assert "gemini" in json.loads(provider.transport.post.call_args.kwargs["body"])["model"]


def test_analysis_factory_does_not_require_unused_text_ai_configuration(monkeypatch):
    from unittest.mock import Mock

    from app import analysis_routes
    from app.analysis import ApilioGemini

    _, configs = _analysis_with_deepseek(monkeypatch)
    configs["deepseek"] = {}
    provider = analysis_routes.get_video_analysis_provider(Mock())
    assert isinstance(provider, ApilioGemini)
    assert provider.api_key == "vision-test-key"
    assert not hasattr(provider, "repair_json")


def test_rewrite_instructions_persist_and_conflicting_retry_is_rejected(pg_state, monkeypatch):
    import app.script_rewrite as rewrite
    from app.auth import CurrentUser

    _seed_base(pg_state)
    _configure_deepseek(monkeypatch)
    actor = CurrentUser(id="u1", username="u1", display_name="User One", role="employee")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = rewrite.enqueue_script_rewrite_task(
            conn,
            actor=actor,
            project_id="proj-1",
            source_text="原文内容",
            instructions="  约200字，不添加报价  ",
            idempotency_key="custom-rewrite-test",
        )
        request = rewrite.validated_script_rewrite_request(row)
        assert request.source_text == "原文内容"
        assert request.instructions == "约200字，不添加报价"
        assert request.ip_profile_snapshot is None
    with pytest.raises(HTTPException) as conflict:
        with pg_transaction() as raw:
            rewrite.enqueue_script_rewrite_task(
                BusinessConnection.postgres(raw),
                actor=actor,
                project_id="proj-1",
                source_text="原文内容",
                instructions="另一种要求",
                idempotency_key="custom-rewrite-test",
            )
    assert conflict.value.status_code == 409
    observed = []

    def generate(**kwargs):
        observed.append(kwargs)
        return "完成的二创正文"

    monkeypatch.setattr(rewrite, "_request_deepseek", generate)
    assert _run_worker("ip-custom-worker") == 1
    assert observed[0]["instructions"] == "约200字，不添加报价"
    assert observed[0]["source_text"] == "原文内容"


def test_ip_extended_profile_roundtrip_and_legacy_update_preserves_fields(pg_state):
    from app.auth import CurrentUser
    from app.script_rewrite import _load_owned_ip_profile_snapshot
    from app.simple_character import update_simple_character_profile

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO character_personas (id, identity_id, name) "
        "VALUES ('cp-owned', 'identity-owned', '张工')",
    )
    actor = CurrentUser(id="u1", username="u1", display_name="User One", role="employee")
    kwargs = dict(
        actor=actor,
        identity_id="identity-owned",
        display_name="张工",
        role="设计师",
        service_scope="乡墅",
        target_audience="建房家庭",
        expression_style="朴实",
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        entry = update_simple_character_profile(
            conn,
            **kwargs,
            audience_needs="预算规划",
            factual_background="经确认的项目资料",
            sample_script="先规划。\n再动工。",
            forbidden_claims="不保证最低价",
        )
        assert entry.sample_script == "先规划。\n再动工。"
        entry = update_simple_character_profile(conn, **kwargs)
        assert entry.forbidden_claims == "不保证最低价"
        snapshot = _load_owned_ip_profile_snapshot(conn, actor=actor, identity_id="identity-owned")
        assert snapshot["audience_needs"] == "预算规划"
        assert snapshot["sample_script"] == "先规划。\n再动工。"
    with pytest.raises(HTTPException) as denied:
        with pg_transaction() as raw:
            update_simple_character_profile(
                BusinessConnection.postgres(raw),
                **{
                    **kwargs,
                    "actor": CurrentUser(
                        id="u2", username="u2", display_name="Other", role="employee"
                    ),
                },
                factual_background="不能修改",
            )
    assert denied.value.status_code == 404


# ---------------------------------------------------------------------------
# G. FIRSTFRAME-RECONCILE — 确定性失败归类 / 任务级日志 / 管理端核对
# ---------------------------------------------------------------------------


def test_deterministic_provider_failure_lands_failed_not_uncertain(pg_state: str) -> None:
    """供应商给出确定性答复（数量不符/JSON 不可读）后重跑同一任务结果必然
    相同：这属于已知失败，用户重新生成即可，不应占用"待核对"终态卡死用户
    （2026-09-17 事故类别的兜底归类）。"""
    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-det")
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        fail_image_task(
            BusinessConnection.postgres(raw),
            table="first_frame_tasks",
            lease=lease,
            cause=ImageProviderFailed("Apilio returned an unexpected number of image outputs"),
            submission_started=True,
        )
    row = _rows(
        pg_state,
        "SELECT status, error_code, retryable FROM first_frame_tasks WHERE id = 'ff-det'",
    )[0]
    assert row["status"] == "FAILED"
    assert row["error_code"] == "IMAGE_TASK_PROVIDER_FAILED"
    assert int(row["retryable"]) == 1


def test_response_invalid_http_failure_lands_failed_not_uncertain(pg_state: str) -> None:
    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-inv")
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        fail_image_task(
            BusinessConnection.postgres(raw),
            table="first_frame_tasks",
            lease=lease,
            cause=first_frame_error(
                502,
                "FIRST_FRAME_PROVIDER_RESPONSE_INVALID",
                "The image provider did not return the requested candidates.",
            ),
            submission_started=True,
        )
    row = _rows(
        pg_state,
        "SELECT status, error_code, retryable FROM first_frame_tasks WHERE id = 'ff-inv'",
    )[0]
    assert row["status"] == "FAILED"
    assert row["error_code"] == "FIRST_FRAME_PROVIDER_RESPONSE_INVALID"
    assert int(row["retryable"]) == 1


def test_retryable_transport_failure_stays_uncertain(pg_state: str) -> None:
    """传输层超时/429/5xx 仍是"结果未知"：回执可续轮询，不能盲目判死。"""
    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-trx")
    with pg_transaction() as raw:
        lease = acquire_first_frame_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        fail_image_task(
            BusinessConnection.postgres(raw),
            table="first_frame_tasks",
            lease=lease,
            cause=RetryableImageProviderFailed("Apilio image request failed"),
            submission_started=True,
        )
    row = _rows(
        pg_state,
        "SELECT status, error_code FROM first_frame_tasks WHERE id = 'ff-trx'",
    )[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert row["error_code"] == "IMAGE_TASK_SUBMISSION_UNCERTAIN"


def test_worker_round_logs_task_scoped_failure(
    pg_state: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """worker 任务级失败必须留下带任务标识的错误日志（2026-09-17 事故归因
    当时全靠裸读代码）。"""
    import logging as _logging

    from cryptography.fernet import Fernet

    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-log")
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    storage = FakeStorageAdapter(provider="fake", bucket="sf-uploads")
    monkeypatch.setattr(
        "app.generation_worker.get_media_storage",
        lambda _conn: storage,
    )
    with caplog.at_level(_logging.ERROR, logger="app.image_tasks"):
        processed = run_pg_worker_round(worker_id="log-worker", max_tasks=1)
    row = _rows(pg_state, "SELECT status FROM first_frame_tasks WHERE id = 'ff-log'")[0]
    assert processed == 1
    assert row["status"] == "FAILED"
    assert any(
        "ff-log" in record.getMessage() and "first_frame_tasks" in record.getMessage()
        for record in caplog.records
    )


def _seed_uncertain_task_with_receipt(dsn: str, task_id: str = "ff-rec") -> None:
    _seed_base(dsn)
    _seed_image_task(dsn, table="first_frame_tasks", task_id=task_id)
    # Production receipts are SHA256-bound; align request_hash with the
    # receipt fingerprint check (the _seed_image_task placeholder is not hex).
    _exec(
        dsn,
        "UPDATE first_frame_tasks SET request_hash=%s WHERE id=%s",
        (_SHA_A, task_id),
    )
    receipt = {
        "schema_version": 1,
        "task_id": "vendor-rec-1",
        "account_fingerprint": _SHA_A,
        "model": "gpt-image-2",
        "output_count": 1,
    }
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        lease = acquire_first_frame_task(conn, worker_id="worker-a")
        record_image_task_provider(
            conn, table="first_frame_tasks", lease=lease, provider="apilio", model="gpt-image-2"
        )
        save_first_frame_provider_submission(
            conn, lease=lease, submission=receipt, cost_record_id="cost-rec-1"
        )
    _exec(
        dsn,
        "UPDATE first_frame_tasks SET status='SUBMISSION_UNCERTAIN', locked_by=NULL,"
        " locked_until=NULL WHERE id=%s",
        (task_id,),
    )


class _StubReconcileProvider:
    provider_name = "apilio"
    account_fingerprint = _SHA_A

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, int]] = []

    def poll_edit(self, task_id: str, *, output_count: int) -> list[object] | None:
        self.calls.append((task_id, output_count))
        if self.outcome == "success":
            return [object() for _ in range(output_count)]
        if self.outcome == "running":
            return None
        if self.outcome == "failure":
            raise first_frame_error(
                422, "FIRST_FRAME_PROVIDER_REJECTED", "图像服务生成失败，请调整素材后重试。"
            )
        if self.outcome == "unreadable":
            raise ImageProviderFailed("Apilio returned an unknown task status")
        raise RetryableImageProviderFailed("Apilio image request failed")


def test_first_frame_reconcile_resumes_when_provider_task_alive(pg_state: str) -> None:
    from types import SimpleNamespace

    from app.image_tasks import (
        apply_first_frame_reconcile,
        first_frame_reconcile_decision,
        prepare_first_frame_reconcile,
    )

    _seed_uncertain_task_with_receipt(pg_state)
    provider = _StubReconcileProvider("success")
    with pg_transaction() as raw:
        plan = prepare_first_frame_reconcile(BusinessConnection.postgres(raw), task_id="ff-rec")
    decision, detail_code = first_frame_reconcile_decision(plan, provider)
    assert decision == "RESUME"
    assert detail_code is None
    assert provider.calls == [("vendor-rec-1", 1)]
    actor = SimpleNamespace(id="u1", username="u1", display_name="User One", role="employee")
    with pg_transaction() as raw:
        result = apply_first_frame_reconcile(
            BusinessConnection.postgres(raw),
            task_id="ff-rec",
            decision=decision,
            detail_code=detail_code,
            actor=actor,
        )
    assert result == "RESUMED"
    row = _rows(
        pg_state,
        "SELECT status, error_code, retryable, completed_at FROM first_frame_tasks"
        " WHERE id = 'ff-rec'",
    )[0]
    assert row["status"] == "PENDING"
    assert row["error_code"] == "IMAGE_TASK_RECONCILE_RESUMED"
    assert int(row["retryable"]) == 1
    assert row["completed_at"] is None
    audit = _rows(
        pg_state,
        "SELECT action FROM audit_logs WHERE entity_id = 'ff-rec'"
        " AND action = 'first_frame_task.reconcile'",
    )
    assert len(audit) == 1


def test_first_frame_reconcile_fails_when_provider_task_failed(pg_state: str) -> None:
    from types import SimpleNamespace

    from app.image_tasks import (
        apply_first_frame_reconcile,
        first_frame_reconcile_decision,
        prepare_first_frame_reconcile,
    )

    _seed_uncertain_task_with_receipt(pg_state)
    provider = _StubReconcileProvider("failure")
    with pg_transaction() as raw:
        plan = prepare_first_frame_reconcile(BusinessConnection.postgres(raw), task_id="ff-rec")
    decision, detail_code = first_frame_reconcile_decision(plan, provider)
    assert decision == "FAIL"
    assert detail_code == "FIRST_FRAME_PROVIDER_REJECTED"
    actor = SimpleNamespace(id="u1", username="u1", display_name="User One", role="employee")
    with pg_transaction() as raw:
        result = apply_first_frame_reconcile(
            BusinessConnection.postgres(raw),
            task_id="ff-rec",
            decision=decision,
            detail_code=detail_code,
            actor=actor,
        )
    assert result == "FAILED"
    row = _rows(
        pg_state,
        "SELECT status, error_code, retryable FROM first_frame_tasks WHERE id = 'ff-rec'",
    )[0]
    assert row["status"] == "FAILED"
    assert row["error_code"] == "FIRST_FRAME_PROVIDER_REJECTED"


def test_first_frame_reconcile_running_provider_task_resumes(pg_state: str) -> None:
    from app.image_tasks import (
        first_frame_reconcile_decision,
        prepare_first_frame_reconcile,
    )

    _seed_uncertain_task_with_receipt(pg_state)
    provider = _StubReconcileProvider("running")
    with pg_transaction() as raw:
        plan = prepare_first_frame_reconcile(BusinessConnection.postgres(raw), task_id="ff-rec")
    decision, detail_code = first_frame_reconcile_decision(plan, provider)
    assert decision == "RESUME"
    assert detail_code is None


def test_first_frame_reconcile_without_receipt_fails_closed(pg_state: str) -> None:
    from types import SimpleNamespace

    from app.image_tasks import (
        apply_first_frame_reconcile,
        first_frame_reconcile_decision,
        prepare_first_frame_reconcile,
    )

    _seed_base(pg_state)
    _seed_image_task(
        pg_state, table="first_frame_tasks", task_id="ff-norc", status="SUBMISSION_UNCERTAIN"
    )
    provider = _StubReconcileProvider("success")
    with pg_transaction() as raw:
        plan = prepare_first_frame_reconcile(BusinessConnection.postgres(raw), task_id="ff-norc")
    decision, detail_code = first_frame_reconcile_decision(plan, provider)
    assert decision == "FAIL"
    assert detail_code == "IMAGE_TASK_RECONCILE_NO_RECEIPT"
    assert provider.calls == []
    actor = SimpleNamespace(id="u1", username="u1", display_name="User One", role="employee")
    with pg_transaction() as raw:
        result = apply_first_frame_reconcile(
            BusinessConnection.postgres(raw),
            task_id="ff-norc",
            decision=decision,
            detail_code=detail_code,
            actor=actor,
        )
    assert result == "FAILED"
    row = _rows(pg_state, "SELECT status FROM first_frame_tasks WHERE id = 'ff-norc'")[0]
    assert row["status"] == "FAILED"


def test_first_frame_reconcile_unreadable_provider_stays_put(pg_state: str) -> None:
    from app.image_tasks import (
        first_frame_reconcile_decision,
        prepare_first_frame_reconcile,
    )

    _seed_uncertain_task_with_receipt(pg_state)
    provider = _StubReconcileProvider("unreadable")
    with pg_transaction() as raw:
        plan = prepare_first_frame_reconcile(BusinessConnection.postgres(raw), task_id="ff-rec")
    decision, detail_code = first_frame_reconcile_decision(plan, provider)
    assert decision == "RETRY"
    assert detail_code == "IMAGE_TASK_RECONCILE_PROVIDER_INCONCLUSIVE"
    row = _rows(pg_state, "SELECT status FROM first_frame_tasks WHERE id = 'ff-rec'")[0]
    assert row["status"] == "SUBMISSION_UNCERTAIN"


def test_first_frame_reconcile_apply_fences_concurrent_change(pg_state: str) -> None:
    from types import SimpleNamespace

    from app.image_tasks import apply_first_frame_reconcile

    _seed_base(pg_state)
    _seed_image_task(
        pg_state, table="first_frame_tasks", task_id="ff-fence", status="SUBMISSION_UNCERTAIN"
    )
    actor = SimpleNamespace(id="u1", username="u1", display_name="User One", role="employee")
    with pg_transaction() as raw:
        apply_first_frame_reconcile(
            BusinessConnection.postgres(raw),
            task_id="ff-fence",
            decision="FAIL",
            detail_code="IMAGE_TASK_RECONCILE_NO_RECEIPT",
            actor=actor,
        )
    with pg_transaction() as raw:
        with pytest.raises(HTTPException) as conflict:
            apply_first_frame_reconcile(
                BusinessConnection.postgres(raw),
                task_id="ff-fence",
                decision="FAIL",
                detail_code="IMAGE_TASK_RECONCILE_NO_RECEIPT",
                actor=actor,
            )
    assert conflict.value.status_code == 409


def test_first_frame_reconcile_rejects_non_uncertain_task(pg_state: str) -> None:
    from app.image_tasks import prepare_first_frame_reconcile

    _seed_base(pg_state)
    _seed_image_task(pg_state, table="first_frame_tasks", task_id="ff-ok", status="PENDING")
    with pg_transaction() as raw:
        with pytest.raises(HTTPException) as conflict:
            prepare_first_frame_reconcile(BusinessConnection.postgres(raw), task_id="ff-ok")
    assert conflict.value.status_code == 409


# ---------------------------------------------------------------------------
# H. 文案（客户端本地抽音轨上传）— 素材登记与共享缓存绑定
#
# 「客户端本地抽音轨 → 上传 → ASR → 回填」替换的是本模块既有 viral-copy 链路的
# 输入来源：平台不再需要为文案留存原片。新增的只有「把客户端上传的音轨登记成
# 服务端认可身份的素材」这一步，登记之后任务、计费、共享缓存绑定全部走既有路径，
# 所以本段验证登记本身、它对既有 `_viral_source` 身份闸门的影响，以及登记后与
# 既有项目链路是否完全同形。
# ---------------------------------------------------------------------------

_COPY_RESULT_JSON = json.dumps(
    {"text": "服务器共享的原视频文案", "duration_sec": 12.0, "language": "zh"}
)


def _copy_video(
    *, platform: str = "douyin", video_id: str = "123", duration_ms: int = 12000
) -> Any:
    from app.viral_tikhub import ViralVideo

    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category="文案",
        title="爆款视频文案提取",
        author="作者",
        author_avatar=None,
        verified=False,
        cover_url=None,
        duration_ms=duration_ms,
        likes=1,
        comments=None,
        shares=None,
        collects=None,
        published_at=None,
        published_display=None,
        like_display=None,
    )


def _seed_copy_fixture(dsn: str) -> None:
    """基础座子 + 内容池里的一条视频 + asr 资费（预留按秒计价）."""
    from app.viral_store import upsert_viral_videos

    _seed_base(dsn)
    _exec(dsn, "DELETE FROM viral_videos WHERE platform='douyin' AND video_id='123'")
    with psycopg.connect(dsn) as raw:
        upsert_viral_videos(BusinessConnection.postgres(raw), [_copy_video()])
    _exec(dsn, "DELETE FROM billing_tariffs WHERE service='asr'")
    _exec(
        dsn,
        "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
        "VALUES ('asr',true,2,1)",
    )
    # 运行开关的种子行由迁移写入，但它带 users 外键而 _truncate 用 CASCADE，
    # 会被基础座子连带清掉；本段依赖它，所以自己重建一行。
    _exec(
        dsn,
        "INSERT INTO viral_runtime_controls (id, collection_enabled, import_enabled) "
        "VALUES (1, 1, 1) ON CONFLICT (id) DO UPDATE SET import_enabled = 1",
    )


def _handler_db(dsn: str) -> Any:
    """只喂真实 PG 写事务的 ``BusinessDb`` 替身（处理器分支用例用，不覆盖会话围栏）."""
    from contextlib import contextmanager

    from app.auth import CurrentUser

    @contextmanager
    def write() -> Any:
        with psycopg.connect(dsn) as raw:
            yield BusinessConnection.postgres(raw), CurrentUser("u1", "u1", "u1", "employee")

    class Db:
        def write(self) -> Any:
            return write()

    return Db()


async def _call_copy_handler(dsn: str, *, key: str) -> tuple[Any, Any]:
    import io

    from fastapi import Response, UploadFile

    from app.viral_import_routes import extract_viral_video_copy

    response = Response()
    payload = await extract_viral_video_copy(
        response=response,
        db=_handler_db(dsn),
        platform="douyin",
        video_id="123",
        file=UploadFile(file=io.BytesIO(b"unused"), size=6),
        idempotency_key=key,
    )
    return response, payload


def _copy_upload(*, size: int = 4096, sha256: str = "c" * 64) -> Any:
    from datetime import UTC, datetime

    from app.storage import StoredObject
    from app.viral_import import ViralCopyUpload

    return ViralCopyUpload(
        stored=StoredObject(
            provider="fake",
            bucket="copies",
            key="viral/copy/deadbeef.m4a",
            uri="fake://copies/viral/copy/deadbeef.m4a",
            size=size,
            content_type="audio/mp4",
            sha256=sha256,
            updated_at=datetime.now(UTC),
        ),
        duration_seconds=12.0,
    )


def _register_copy(
    dsn: str,
    *,
    key: str,
    user: str = "u1",
    video: Any = None,
    upload: Any = None,
) -> Any:
    from app.auth import CurrentUser
    from app.viral_import import register_viral_copy_source

    with psycopg.connect(dsn) as raw:
        return register_viral_copy_source(
            BusinessConnection.postgres(raw),
            actor=CurrentUser(user, user, user, "employee"),
            video=video or _copy_video(),
            upload=upload or _copy_upload(),
            idempotency_key=key,
        )


def _enqueue_registered_copy(dsn: str, source: Any, *, key: str, user: str = "u1") -> Any:
    from app.auth import CurrentUser
    from app.script_from_audio import enqueue_script_from_audio_task

    with psycopg.connect(dsn) as raw:
        return enqueue_script_from_audio_task(
            BusinessConnection.postgres(raw),
            actor=CurrentUser(user, user, user, "employee"),
            project_id=source.project_id,
            source_asset_id=source.asset_id,
            idempotency_key=key,
        )


def _no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """共享缓存命中不该触发任何 provider 调用；被调用即失败."""
    from app import script_from_audio as pipeline

    def call(conn: Any) -> Any:
        pytest.fail("共享文案缓存命中时不应解析 ASR provider")

    monkeypatch.setattr(pipeline, "_configured_asr", call)


def test_viral_copy_upload_registers_marked_audio_and_binds_shared_cache(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登记 → 身份可映射 → 复用租约任务 → 共享缓存生产者绑定 + 按秒预留."""
    from app import script_from_audio as pipeline
    from app.asr import TranscriptResult
    from app.script_from_audio import _viral_source

    _seed_copy_fixture(pg_state)
    source = _register_copy(pg_state, key="copy-1")
    asset = _rows(
        pg_state,
        "SELECT kind, content_type, metadata_json, storage_uri FROM assets WHERE id=%s",
        (source.asset_id,),
    )[0]
    assert asset["kind"] == "reference_audio"
    assert asset["content_type"] == "audio/mp4"
    assert asset["storage_uri"] == "fake://copies/viral/copy/deadbeef.m4a"
    metadata = json.loads(asset["metadata_json"])
    # 标记由服务端写下：改闸门只认它，不认客户端能自证的任何字段。
    assert metadata["viral_copy_upload"] is True
    assert (metadata["platform"], metadata["video_id"]) == ("douyin", "123")
    assert metadata["duration_seconds"] == 12.0
    import_task = _rows(
        pg_state,
        "SELECT status, purpose, source_asset_id FROM viral_import_tasks WHERE id=%s",
        (source.import_task_id,),
    )[0]
    assert import_task["status"] == "SUCCEEDED"
    assert import_task["purpose"] == "copy"
    assert import_task["source_asset_id"] == source.asset_id
    with psycopg.connect(pg_state) as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute("SELECT * FROM assets WHERE id=%s", (source.asset_id,)).fetchone()
        assert row is not None
        assert _viral_source(conn, row) == ("douyin", "123")

    class Provider:
        name = "fake-copy-asr"

        def transcribe(self, url: str, *, duration_sec: float | None = None) -> TranscriptResult:
            return TranscriptResult("客户端上传的音轨文案", 12, "zh")

    monkeypatch.setattr(pipeline, "_configured_asr", lambda conn: Provider())
    task = _enqueue_registered_copy(pg_state, source, key="copy-1")
    assert task["status"] == "PENDING"
    assert (
        _one(
            pg_state,
            "SELECT producer_task_id FROM viral_script_cache "
            "WHERE platform='douyin' AND video_id='123'",
        )
        == task["id"]
    )
    operations = _rows(pg_state, "SELECT budget_units FROM billing_operations WHERE service='asr'")
    assert [float(item["budget_units"]) for item in operations] == [12.0]


def test_viral_copy_upload_replay_reuses_project_asset_and_task(pg_state: str) -> None:
    """同一把幂等键重放：同一个占位项目、同一份资产、同一条任务，不重复登记."""
    from app.viral_import import ViralImportError

    _seed_copy_fixture(pg_state)
    first = _register_copy(pg_state, key="same-key")
    second = _register_copy(pg_state, key="same-key")
    assert second.project_id == first.project_id
    assert second.asset_id == first.asset_id
    assert second.import_task_id == first.import_task_id
    # proj-1 是座子项目，占位项目只应多出一个。
    assert _one(pg_state, "SELECT count(*) FROM projects") == 2
    assert _one(pg_state, "SELECT count(*) FROM assets") == 1
    assert _one(pg_state, "SELECT count(*) FROM viral_import_tasks") == 1
    with pytest.raises(ViralImportError) as conflict:
        _register_copy(pg_state, key="same-key", upload=_copy_upload(size=999))
    assert conflict.value.status_code == 409


def test_viral_source_gate_only_opens_for_server_marked_copy_audio(pg_state: str) -> None:
    """`reference_audio` 的放行只取决于服务端标记.

    解析器下发的独立音轨可能是背景音乐，所以既有闸门一律拒绝 `reference_audio`；
    客户端抽出的音轨是同一支视频的音轨，才由本端点在资产上写下标记。本用例固定住
    「除标记以外一切相同」这个前提。
    """
    from app.script_from_audio import _viral_source

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO assets(id,project_id,kind,storage_uri,sha256,size_bytes,"
        "content_type,created_by_user_id,metadata_json) VALUES ('plain-audio','proj-1',"
        "'reference_audio','fake://copies/source.m4a',%s,16,'audio/mp4','u1',%s)",
        (
            "b" * 64,
            json.dumps({"platform": "douyin", "video_id": "123", "duration_seconds": 12}),
        ),
    )
    _exec(
        pg_state,
        "INSERT INTO viral_import_tasks(id,owner_user_id,project_id,source_asset_id,"
        "platform,video_id,purpose,idempotency_key,request_hash,request_json,status) "
        "VALUES ('imp-audio','u1','proj-1','plain-audio','douyin','123','copy','ik-audio',"
        "'hash','{}','SUCCEEDED')",
    )
    with psycopg.connect(pg_state) as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute("SELECT * FROM assets WHERE id='plain-audio'").fetchone()
        assert row is not None
        assert _viral_source(conn, row) is None
    _exec(
        pg_state,
        "UPDATE assets SET metadata_json=%s WHERE id='plain-audio'",
        (
            json.dumps(
                {
                    "platform": "douyin",
                    "video_id": "123",
                    "duration_seconds": 12,
                    "viral_copy_upload": True,
                }
            ),
        ),
    )
    with psycopg.connect(pg_state) as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute("SELECT * FROM assets WHERE id='plain-audio'").fetchone()
        assert row is not None
        assert _viral_source(conn, row) == ("douyin", "123")


def test_viral_copy_upload_serves_shared_cache_without_submission(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共享缓存已就绪时，登记后的入队直接落终态，不解析 provider、不产生调用."""
    _seed_copy_fixture(pg_state)
    _exec(
        pg_state,
        "INSERT INTO viral_script_cache(platform,video_id,result_json) VALUES ('douyin','123',%s)",
        (_COPY_RESULT_JSON,),
    )
    _no_provider(monkeypatch)
    source = _register_copy(pg_state, key="cache-hit")
    task = _enqueue_registered_copy(pg_state, source, key="cache-hit")
    assert task["status"] == "SUCCEEDED"
    assert json.loads(str(task["result_json"]))["text"] == "服务器共享的原视频文案"
    assert _one(pg_state, "SELECT count(*) FROM billing_attempts") == 0


async def test_viral_copy_handler_serves_shared_cache_before_any_billing(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """处理器在计费之前查共享缓存：命中即秒回，不建任务、不预留、不落素材."""
    _seed_copy_fixture(pg_state)
    _exec(
        pg_state,
        "INSERT INTO viral_script_cache(platform,video_id,result_json,updated_at) "
        "VALUES ('douyin','123',%s,now())",
        (_COPY_RESULT_JSON,),
    )
    _no_provider(monkeypatch)
    response, payload = await _call_copy_handler(pg_state, key="cache-hit")
    assert response.status_code == 200
    assert payload.text == "服务器共享的原视频文案"
    assert payload.task_id is None
    assert _one(pg_state, "SELECT count(*) FROM script_from_audio_tasks") == 0
    assert _one(pg_state, "SELECT count(*) FROM billing_operations") == 0
    assert _one(pg_state, "SELECT count(*) FROM viral_import_tasks") == 0


async def test_viral_copy_handler_rechecks_cache_and_removes_racing_upload(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传期间其他用户写入共享文案时，本请求应免费秒回并删除未登记音轨."""
    from app.asr import TranscriptResult
    from app.script_from_audio import CachedTranscriptHit
    from app.storage import FakeStorageAdapter
    from app.viral_import_routes import _viral_copy_object_key

    _seed_copy_fixture(pg_state)
    storage = FakeStorageAdapter(provider="fake", bucket="copies")
    lookups = 0

    def racing_cache(*args: Any, **kwargs: Any) -> Any:
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return None
        return CachedTranscriptHit(
            result=TranscriptResult("竞态期间已生成的文案", 12.0, "zh"),
            updated_at="2026-09-24T12:00:00Z",
        )

    monkeypatch.setattr("app.viral_import_routes.cached_transcript", racing_cache)
    monkeypatch.setattr("app.viral_import_routes.get_media_storage", lambda conn: storage)
    monkeypatch.setattr(
        "app.viral_import_routes._inline_audio_limits", lambda conn, s: (None, None)
    )
    monkeypatch.setattr("app.viral_import_routes._probe_viral_copy_duration", lambda content: 12.0)

    import io

    from fastapi import Response, UploadFile

    from app.viral_import_routes import extract_viral_video_copy

    audio = b"\x00\x00\x00 ftypM4A " + b"\x00" * 32
    response = Response()
    payload = await extract_viral_video_copy(
        response=response,
        db=_handler_db(pg_state),
        platform="douyin",
        video_id="123",
        file=UploadFile(file=io.BytesIO(audio), size=len(audio)),
        idempotency_key="racing-cache",
    )

    assert response.status_code == 200
    assert payload.text == "竞态期间已生成的文案"
    assert storage.head_object(_viral_copy_object_key("u1", "racing-cache")) is None
    assert _one(pg_state, "SELECT count(*) FROM script_from_audio_tasks") == 0
    assert _one(pg_state, "SELECT count(*) FROM billing_operations") == 0
    assert _one(pg_state, "SELECT count(*) FROM viral_import_tasks") == 0


async def test_viral_copy_handler_removes_upload_when_registration_fails(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """音轨已写存储但数据库登记失败时，不能留下无主对象."""
    from app.storage import FakeStorageAdapter
    from app.viral_import_routes import _viral_copy_object_key

    _seed_copy_fixture(pg_state)
    storage = FakeStorageAdapter(provider="fake", bucket="copies")
    monkeypatch.setattr("app.viral_import_routes.get_media_storage", lambda conn: storage)
    monkeypatch.setattr(
        "app.viral_import_routes._inline_audio_limits", lambda conn, s: (None, None)
    )
    monkeypatch.setattr("app.viral_import_routes._probe_viral_copy_duration", lambda content: 12.0)

    def registration_failed(*args: Any, **kwargs: Any) -> Any:
        raise HTTPException(status_code=409, detail={"code": "REGISTER_FAILED"})

    monkeypatch.setattr("app.viral_import_routes.register_viral_copy_source", registration_failed)

    import io

    from fastapi import Response, UploadFile

    from app.viral_import_routes import extract_viral_video_copy

    audio = b"\x00\x00\x00 ftypM4A " + b"\x00" * 32
    with pytest.raises(HTTPException) as failed:
        await extract_viral_video_copy(
            response=Response(),
            db=_handler_db(pg_state),
            platform="douyin",
            video_id="123",
            file=UploadFile(file=io.BytesIO(audio), size=len(audio)),
            idempotency_key="registration-failed",
        )
    assert failed.value.detail["code"] == "REGISTER_FAILED"
    assert storage.head_object(_viral_copy_object_key("u1", "registration-failed")) is None
    assert _one(pg_state, "SELECT count(*) FROM viral_import_tasks") == 0


async def test_viral_copy_handler_respects_the_shared_import_pause(pg_state: str) -> None:
    """管理端「暂停导入」对新端点同样生效：对用户而言仍是同一个动作."""
    _seed_copy_fixture(pg_state)
    _exec(pg_state, "UPDATE viral_runtime_controls SET import_enabled=0 WHERE id=1")
    with pytest.raises(HTTPException) as paused:
        await _call_copy_handler(pg_state, key="paused")
    assert paused.value.status_code == 503
    assert paused.value.detail["code"] == "VIRAL_IMPORT_DISABLED"


def test_viral_copy_upload_duration_limits() -> None:
    """时长门槛：与内容池时长对齐（带容差），以及本地直传时长上限."""
    from app.viral_import_routes import _validate_viral_copy_duration

    video = _copy_video(duration_ms=12000)
    long_video = _copy_video(duration_ms=310000)
    _validate_viral_copy_duration(duration=12.0, video=video, inline_max_seconds=300.0)
    # 容差之内（±25% ±3s）仍然放行，避免平台时长抖动误伤真实提取。
    _validate_viral_copy_duration(duration=14.5, video=video, inline_max_seconds=300.0)
    with pytest.raises(HTTPException) as mismatch:
        _validate_viral_copy_duration(duration=90.0, video=video, inline_max_seconds=None)
    assert mismatch.value.detail["code"] == "VIRAL_COPY_AUDIO_DURATION_MISMATCH"
    with pytest.raises(HTTPException) as too_long:
        _validate_viral_copy_duration(duration=301.0, video=long_video, inline_max_seconds=300.0)
    assert too_long.value.detail["code"] == "VIRAL_COPY_AUDIO_TOO_LONG"
    # 云存储走签名 URL，不受 base64 直传上限约束：上限为 None 即不检查。
    _validate_viral_copy_duration(duration=301.0, video=long_video, inline_max_seconds=None)


async def test_viral_copy_upload_accepts_only_m4a_containers() -> None:
    """抽取端固定产出 M4A，provider 的 Flash 请求也写死 format=m4a，故只认 MP4 容器."""
    import io

    from fastapi import UploadFile

    from app.viral_import_routes import _read_viral_copy_audio

    m4a = b"\x00\x00\x00 ftypM4A " + b"\x00" * 32
    assert (
        await _read_viral_copy_audio(
            UploadFile(file=io.BytesIO(m4a), size=len(m4a)), inline_max_bytes=None
        )
        == m4a
    )
    with pytest.raises(HTTPException) as empty:
        await _read_viral_copy_audio(
            UploadFile(file=io.BytesIO(b""), size=0), inline_max_bytes=None
        )
    assert empty.value.detail["code"] == "VIRAL_COPY_AUDIO_EMPTY"
    with pytest.raises(HTTPException) as bad_container:
        await _read_viral_copy_audio(
            UploadFile(file=io.BytesIO(b"ID3\x04\x00\x00\x00\x00\x00\x00mp3"), size=13),
            inline_max_bytes=None,
        )
    assert bad_container.value.detail["code"] == "VIRAL_COPY_AUDIO_INVALID"
    with pytest.raises(HTTPException) as too_large:
        await _read_viral_copy_audio(
            UploadFile(file=io.BytesIO(m4a), size=len(m4a)), inline_max_bytes=16
        )
    assert too_large.value.detail["code"] == "VIRAL_COPY_AUDIO_TOO_LARGE"
    assert too_large.value.status_code == 413
