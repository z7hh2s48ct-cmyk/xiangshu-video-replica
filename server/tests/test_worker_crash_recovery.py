"""T26 — worker crash recovery and provider-submission uncertainty (PG lane).

The fixture database is migrated with ``alembic upgrade head`` (the T25
precedent) so every query runs against the real revision-041 schema. The
tests drive the actual business functions on the PostgreSQL lane the way the
PG worker loop does: every logical operation inside its own fenced
``pg_transaction`` (T25).

Test matrix (T26 ADR §6 / BILL-03):
- the two-phase claim: a worker dying after the claim transaction leaves the
  task durably SUBMITTING (never silently re-submitted); expiry recovery moves
  it to SUBMISSION_UNCERTAIN and releases the slot;
- an expired lease is taken over defensively with a worker-owned audit row
  (actor_user_id NULL) and the per-user slot released, for both the
  SUBMISSION_UNCERTAIN branch and the archive-retry branch;
- concurrent expiry sweepers: only the winner's UPDATE…RETURNING audits and
  releases (no double audit, no release of a replacement task's slot);
- the live uncertain transition releases the slot (P1-2), and reconciling an
  already-released task never releases the replacement's slot (P1-5);
- reconciliation on the PG lane settles exactly one terminal billing row
  (never a second), and a cancelled provider release is equally one-shot;
- reconciliation refuses to guess when the provider task id is unknown
  (SUBMISSION_REQUIRES_MANUAL_CONFIRMATION keeps the charge human-verified);
- BILL-03: dangling RESERVE rows of terminal tasks are detected;
- the production worker loop (run_pg_worker_once) claims, processes and
  drains with the two-phase shape and a max_tasks bound;
- the FIFO double-claim race is closed by FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

# Set HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t26-worker-reliability-tests-minimum-48-bytes-long-12",
)

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import require_pg_or_explicit_skip

from app.auth import CurrentUser
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.generation import (
    GenerationTaskRetryRequest,
    H3QueryResult,
    MetasoH3Provider,
    ReconcileReservation,
    acquire_generation_continuation_lease,
    acquire_generation_task_lease,
    mark_expired_active_leases_needing_attention,
    mark_task_submission_uncertain,
    reconcile_submission_uncertain_task,
    reschedule_generation_poll,
    retry_generation_task,
)
from app.generation_worker import _run_pg_generation_step, run_pg_worker_once
from app.internal_billing import (
    find_dangling_billing_reservations,
    reserve_internal_billing,
)
from app.storage import FakeStorageAdapter

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T26_DB_NAME = "t26_worker_reliability_test"

_FAIR_TABLES = (
    "user_queue_cursors, generation_task_operations, external_call_logs, "
    "assets, wallet_transactions, recharge_orders, internal_access_tokens, "
    "wallets, generation_tasks, generation_batches, audit_logs, "
    "runtime_settings, users"
)

_REQUEST_SNAPSHOT = '{"output_duration_seconds": 10, "resolution": "768P"}'
_PROMPT_SNAPSHOT = '{"prompt_text": "a cat", "first_frame_uri": "cos://bucket/ff.jpg"}'


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t26_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T26_DB_NAME}"


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fair_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T26_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T26_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t26_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t26_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T26_DB_NAME}" WITH (FORCE)')


def _seed(
    dsn: str,
    *,
    user_ids: list[str],
    tasks_per_user: int,
    task_status: str = "PENDING",
    wallet_credits: int = 1000,
) -> None:
    """TRUNCATE and re-seed users / batches / tasks / cursors / wallets."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_FAIR_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        # Explicit retail configuration for these positive-reservation scenarios.
        pg.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES "
            "('video_768p',true,1),('video_2k',true,1) ON CONFLICT(service) "
            "DO UPDATE SET enabled=true,unit_credits=1"
        )
        pg.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('proj-owner', 'proj_owner', 'Project Owner', 'user')"
        )
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES "
            "('proj-1', 'Crash Recovery Project', 'proj-owner')"
        )
        pg.execute(
            "INSERT INTO versions (id, project_id, kind, version_number, payload_json) "
            "VALUES ('pv-1', 'proj-1', 'video', 1, '{}')"
        )
        for user_id in user_ids:
            pg.execute(
                "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'user')",
                (user_id, user_id, user_id),
            )
            pg.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES (%s, %s, 0)",
                (user_id, wallet_credits),
            )
            batch_id = f"batch-{user_id}"
            pg.execute(
                "INSERT INTO generation_batches ("
                " id, project_id, created_by_user_id, idempotency_key, "
                " request_hash, request_snapshot_json, status"
                ") VALUES (%s, 'proj-1', %s, %s, %s, %s, 'QUEUED')",
                (batch_id, user_id, f"ik-{user_id}", f"rh-{user_id}", _REQUEST_SNAPSHOT),
            )
            for task_index in range(tasks_per_user):
                pg.execute(
                    "INSERT INTO generation_tasks ("
                    " id, batch_id, generation_mode, provider, model, "
                    " status, archive_status, quality_status, "
                    " prompt_version_id, prompt_snapshot_json, next_poll_at"
                    ") VALUES (%s, %s, 'I2V', 'metaso', 'h3', %s, "
                    "'PENDING', 'PENDING', 'pv-1', %s, NULL)",
                    (
                        f"task-{user_id}-{task_index}",
                        batch_id,
                        task_status,
                        _PROMPT_SNAPSHOT,
                    ),
                )
            pg.execute(
                "INSERT INTO user_queue_cursors "
                "(user_id, last_dispatched_at, running_tasks_count) "
                "VALUES (%s, now(), 0)",
                (user_id,),
            )


def _acquire(dsn: str, worker_id: str) -> dict[str, object] | None:
    """Acquire a lease the way the PG worker loop does: one fenced transaction."""
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        lease = acquire_generation_task_lease(conn, worker_id=worker_id)
        if lease is not None:
            return dict(lease)
        return None


def _cursor_count(dsn: str, user_id: str) -> int:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute(
            "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        return int(row[0]) if row is not None else -1


def _billing_rows(dsn: str, task_id: str) -> list[tuple[str, int]]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            "SELECT type, billing_round FROM wallet_transactions "
            "WHERE task_id = %s "
            "ORDER BY billing_round, CASE WHEN type = 'RESERVE' THEN 0 ELSE 1 END, type",
            (task_id,),
        ).fetchall()
        return [(str(row["type"]), int(row["billing_round"])) for row in rows]


def _audit_actions(dsn: str) -> list[tuple[str, str | None, str]]:
    """(action, actor_user_id, entity_id) audit rows, oldest first."""
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            "SELECT action, actor_user_id, entity_id FROM audit_logs ORDER BY created_at, id"
        ).fetchall()
        return [(str(row["action"]), row["actor_user_id"], str(row["entity_id"])) for row in rows]


def _seed_reserved(
    dsn: str, *, task_id: str, available_credits: int, reserved_credits: int
) -> None:
    """Post-crash state: a committed RESERVE with the wallet moved (T26)."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "UPDATE wallets SET available_credits = %s, reserved_credits = %s WHERE user_id = 'u1'",
            (available_credits, reserved_credits),
        )
        pg.execute(
            "INSERT INTO wallet_transactions ("
            " id, user_id, type, available_delta, reserved_delta, "
            " task_id, billing_round, idempotency_key"
            ") VALUES (%s, 'u1', 'RESERVE', -1, 1, %s, 1, %s)",
            (f"wt-{task_id}-r1", task_id, f"reserve:{task_id}:1"),
        )


@pytest.fixture()
def fair_state(fair_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, fair_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield fair_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# Crash inside the fenced transaction
# ---------------------------------------------------------------------------


def test_crash_after_claim_leaves_durable_submitting(fair_state: str) -> None:
    """A worker dying after the claim transaction but before the work
    transaction: the SUBMITTING lease, the wallet untouched and the slot stay
    committed; the task is never silently re-claimed, and expiry recovery
    moves it to SUBMISSION_UNCERTAIN (a manual reconciliation gate).

    This is the two-phase production shape (M5 review P1-1): the old single
    fenced transaction rolled the whole round back on a crash, returning the
    task to PENDING and letting the next worker re-send the paid POST.
    """
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2, wallet_credits=1000)
    # Phase 1 — durable claim: committed in its own fenced transaction.
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    assert str(lease["id"]) == "task-u1-0"
    # Phase 2 crashes before anything is written: the work transaction rolls
    # back, but the claim survives.
    with pytest.raises(RuntimeError, match="simulated crash"):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            reserve_internal_billing(conn, user_id="u1", task_id=str(lease["id"]), billing_round=1)
            raise RuntimeError("simulated crash")

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, locked_by, locked_until FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u1'"
        ).fetchone()
    assert task["status"] == "SUBMITTING"  # the claim is durable
    assert task["locked_by"] == "w1"
    assert task["locked_until"] is not None
    assert int(wallet["available_credits"]) == 1000
    assert int(wallet["reserved_credits"]) == 0
    assert _billing_rows(fair_state, "task-u1-0") == []
    assert _cursor_count(fair_state, "u1") == 1  # the slot is held by the claim

    # The expired claim is taken over defensively: SUBMISSION_UNCERTAIN +
    # slot released — the next worker claims the user's NEXT task, never a
    # re-send of the same one.
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE generation_tasks SET locked_until = now() - interval '5 minutes' WHERE id = %s",
            (str(lease["id"]),),
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_expired_active_leases_needing_attention(conn)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, error_code FROM generation_tasks WHERE id = %s",
            (str(lease["id"]),),
        ).fetchone()
    assert task["status"] == "SUBMISSION_UNCERTAIN"
    assert task["error_code"] == "LEASE_EXPIRED_NEEDS_ATTENTION"
    assert _cursor_count(fair_state, "u1") == 0
    lease2 = _acquire(fair_state, "w2")
    assert lease2 is not None
    assert str(lease2["id"]) == "task-u1-1"  # the next task, not a re-send


# ---------------------------------------------------------------------------
# Expired lease takeover (audited, slot released)
# ---------------------------------------------------------------------------


def test_expired_lease_audited_and_slot_released(fair_state: str) -> None:
    """A worker that vanished mid-call: SUBMISSION_UNCERTAIN, worker-owned
    audit row, per-user slot freed for the user's next task."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2, wallet_credits=1000)
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    assert _cursor_count(fair_state, "u1") == 1
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE generation_tasks SET locked_until = now() - interval '5 minutes' WHERE id = %s",
            (str(lease["id"]),),
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_expired_active_leases_needing_attention(conn)

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, error_code, locked_by, locked_until "
            "FROM generation_tasks WHERE id = %s",
            (str(lease["id"]),),
        ).fetchone()
    assert task["status"] == "SUBMISSION_UNCERTAIN"
    assert task["error_code"] == "LEASE_EXPIRED_NEEDS_ATTENTION"
    assert task["locked_by"] is None
    assert task["locked_until"] is None
    assert _audit_actions(fair_state) == [
        ("generation_task.lease_expired_uncertain", None, str(lease["id"]))
    ]
    assert _cursor_count(fair_state, "u1") == 0
    lease2 = _acquire(fair_state, "w2")
    assert lease2 is not None  # the user's next task is schedulable
    assert str(lease2["id"]) == "task-u1-1"


def test_expired_archive_retry_lease_audited_and_reset(fair_state: str) -> None:
    """Archive retries never start a paid call: an expired retry lease just
    resets to SUCCEEDED/ARCHIVE_FAILED for another attempt, audited."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks "
            "SET status = 'ARCHIVING', archive_status = 'ARCHIVE_FAILED', "
            "    provider_result_url = 'cos://bucket/result.mp4', "
            "    locked_by = 'w1', locked_until = now() - interval '5 minutes' "
            "WHERE id = 'task-u1-0'"
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_expired_active_leases_needing_attention(conn)

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, archive_status, next_poll_at, locked_by "
            "FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
    assert task["status"] == "SUCCEEDED"
    assert task["archive_status"] == "ARCHIVE_FAILED"
    assert task["next_poll_at"] is not None  # retry backoff scheduled
    assert task["locked_by"] is None
    assert _audit_actions(fair_state) == [
        ("generation_task.lease_expired_archive_retry", None, "task-u1-0")
    ]
    assert _cursor_count(fair_state, "u1") == 0  # GREATEST guard: no-op is safe


@pytest.mark.parametrize("claim_after_read", [False, True])
def test_archive_retry_cannot_clear_active_recovery_lease(
    fair_state: str, monkeypatch: pytest.MonkeyPatch, claim_after_read: bool
) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1)
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status='SUCCEEDED', archive_status='ARCHIVE_FAILED', "
            "provider_task_id='saved-paid-result', "
            "provider_result_url='https://example.com/result.mp4'"
        )
    lease = None if claim_after_read else _acquire(fair_state, "archive-recovery")
    with pytest.raises(HTTPException) as rejected:
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            execute = conn.execute

            def claim_before_retry_update(sql: str, params: Any = ()) -> Any:
                nonlocal lease
                if claim_after_read and "retry_requested_by_user_id = %s" in sql:
                    lease = _acquire(fair_state, "archive-recovery")
                    assert lease is not None
                return execute(sql, params)

            monkeypatch.setattr(conn, "execute", claim_before_retry_update)
            retry_generation_task(
                conn,
                task_id="task-u1-0",
                actor=CurrentUser("u1", "u1", "User One", "employee"),
                request=GenerationTaskRetryRequest(
                    idempotency_key="active-archive-retry", retry_reason="Retry saved result"
                ),
            )
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "TASK_RETRY_NOT_ALLOWED"
    assert lease is not None
    with psycopg.connect(fair_state, autocommit=True) as pg:
        task = pg.execute(
            "SELECT status, locked_by, locked_until, archive_retry_count "
            "FROM generation_tasks WHERE id='task-u1-0'"
        ).fetchone()
        assert task is not None
        assert task[0:2] == ("SUBMITTING", "archive-recovery")
        assert str(task[2]) == str(lease["locked_until"])
        assert task[3] == 0
        assert pg.execute("SELECT count(*) FROM generation_task_operations").fetchone() == (0,)
        assert pg.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id='u1'"
        ).fetchone() == (999, 1)
    assert _cursor_count(fair_state, "u1") == 1
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1)]
    assert _audit_actions(fair_state) == []

    def reject_provider(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("archive recovery must never contact a provider")

    monkeypatch.setattr("app.generation_worker.h3_provider_for_task", reject_provider)
    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
    _run_pg_generation_step(
        lease=cast(dict[str, Any], lease), storage=storage, first_frame_storage=storage
    )
    assert _cursor_count(fair_state, "u1") == 0
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("SETTLE", 1)]


def test_archive_retry_replay_preserves_claim_and_safe_retry_still_completes(
    fair_state: str,
) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1)
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status='SUCCEEDED', archive_status='ARCHIVE_FAILED', "
            "provider_task_id='saved-paid-result', "
            "provider_result_url='https://example.com/result.mp4', "
            "next_poll_at=now() + interval '1 hour'"
        )
    actor = CurrentUser("u1", "u1", "User One", "employee")
    request = GenerationTaskRetryRequest(
        idempotency_key="safe-archive-retry", retry_reason="Retry saved result"
    )
    with pg_transaction() as raw:
        retry_generation_task(
            BusinessConnection.postgres(raw), task_id="task-u1-0", actor=actor, request=request
        )
    lease = _acquire(fair_state, "archive-recovery")
    assert lease is not None
    with pg_transaction() as raw:
        # A replay of the already accepted request returns the current result;
        # it must not execute the scheduling write a second time.
        retry_generation_task(
            BusinessConnection.postgres(raw), task_id="task-u1-0", actor=actor, request=request
        )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        task = pg.execute(
            "SELECT status, locked_by, locked_until FROM generation_tasks WHERE id='task-u1-0'"
        ).fetchone()
        assert task is not None
        assert task[0:2] == ("SUBMITTING", "archive-recovery")
        assert str(task[2]) == str(lease["locked_until"])
        assert pg.execute("SELECT count(*) FROM generation_task_operations").fetchone() == (1,)
    assert _audit_actions(fair_state) == [
        ("generation_task.archive_retry_queued", "u1", "task-u1-0")
    ]
    assert _cursor_count(fair_state, "u1") == 1
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1)]
    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
    _run_pg_generation_step(
        lease=cast(dict[str, Any], lease), storage=storage, first_frame_storage=storage
    )
    assert _cursor_count(fair_state, "u1") == 0
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("SETTLE", 1)]


def test_archive_retry_cannot_reopen_concurrently_completed_result(
    fair_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1)
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status='SUCCEEDED', archive_status='ARCHIVE_FAILED', "
            "provider_task_id='saved-paid-result', "
            "provider_result_url='https://example.com/result.mp4'"
        )
    with pytest.raises(HTTPException) as rejected:
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            execute = conn.execute

            def finish_before_retry_update(sql: str, params: Any = ()) -> Any:
                if "retry_requested_by_user_id = %s" in sql:
                    lease = _acquire(fair_state, "archive-recovery")
                    assert lease is not None
                    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
                    _run_pg_generation_step(
                        lease=cast(dict[str, Any], lease),
                        storage=storage,
                        first_frame_storage=storage,
                    )
                return execute(sql, params)

            monkeypatch.setattr(conn, "execute", finish_before_retry_update)
            retry_generation_task(
                conn,
                task_id="task-u1-0",
                actor=CurrentUser("u1", "u1", "User One", "employee"),
                request=GenerationTaskRetryRequest(
                    idempotency_key="late-archive-retry", retry_reason="Retry saved result"
                ),
            )
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "TASK_RETRY_NOT_ALLOWED"
    with psycopg.connect(fair_state, autocommit=True) as pg:
        assert pg.execute(
            "SELECT status, archive_status, locked_by, locked_until, retry_requested_at "
            "FROM generation_tasks WHERE id='task-u1-0'"
        ).fetchone() == ("SUCCEEDED", "DIRECT", None, None, None)
        assert pg.execute("SELECT count(*) FROM generation_task_operations").fetchone() == (0,)
    assert _cursor_count(fair_state, "u1") == 0
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("SETTLE", 1)]
    assert _audit_actions(fair_state) == []


# ---------------------------------------------------------------------------
# Reconciliation on the PG lane (terminal billing exactly once)
# ---------------------------------------------------------------------------


class _ReconcileSucceededProvider(MetasoH3Provider):
    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        return {
            "id": provider_task_id,
            "status": "succeeded",
            "content": {"url": "https://example.com/results/ok.mp4"},
        }

    def download_result(self, url: str) -> bytes:
        return b"reconciled-mp4-bytes"


class _ReconcileCancelledProvider(MetasoH3Provider):
    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        return {"id": provider_task_id, "status": "cancelled"}


def _reconcile(fair_state: str, task_id: str, provider: MetasoH3Provider) -> object:
    """Reconcile the way the API route does: create the RECONCILE operation
    row, then hand its reservation to the business function (M5 review P1-5
    — the reservation marks the reconciliation path, which must NOT release
    the concurrency slot of the user's replacement task)."""
    operation_id = f"op-{uuid4()}"
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO generation_task_operations ("
            " id, task_id, actor_user_id, action, idempotency_key, "
            " request_hash, result_task_id, result_status"
            ") VALUES (%s, %s, 'proj-owner', 'RECONCILE', %s, %s, %s, 'PENDING')",
            (operation_id, task_id, f"ik-{operation_id}", f"rh-{operation_id}", task_id),
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return reconcile_submission_uncertain_task(
            conn,
            task_id=task_id,
            batch_id="batch-u1",
            project_id="proj-1",
            created_by_user_id="u1",
            storage_factory=lambda: FakeStorageAdapter(provider="cos", bucket="generation-results"),
            provider=provider,
            reconcile_reservation=ReconcileReservation(
                id=operation_id,
                actor=CurrentUser(
                    id="proj-owner",
                    username="proj_owner",
                    display_name="Project Owner",
                    role="employee",
                ),
                idempotency_key=f"ik-{operation_id}",
            ),
        )


def test_reconcile_success_settles_exactly_once(
    fair_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciliation preserves the direct result URL and settles the reserved
    credit exactly once; a repeated reconcile never settles twice."""
    # The SSRF guard is covered by its own tests; here the fake provider's
    # example.com result URL must not trigger a real DNS/address check. Patch
    # only the module-level guard, never the socket module (psycopg relies on
    # the real one for the PG pool).
    monkeypatch.setattr("app.generation._require_public_https_host", lambda _host: None)
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=1,
        task_status="SUBMISSION_UNCERTAIN",
        wallet_credits=1000,
    )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute("UPDATE generation_tasks SET provider_task_id = 'pt-1' WHERE id = 'task-u1-0'")
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)

    result = _reconcile(fair_state, "task-u1-0", _ReconcileSucceededProvider(api_key="test-key"))
    assert result is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, archive_status, result_asset_id, provider_result_url "
            "FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u1'"
        ).fetchone()
    assert task["status"] == "SUCCEEDED"
    assert task["archive_status"] == "DIRECT"
    assert task["result_asset_id"] is None
    assert task["provider_result_url"] == "https://example.com/results/ok.mp4"
    assert int(wallet["available_credits"]) == 999
    assert int(wallet["reserved_credits"]) == 0
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("SETTLE", 1)]

    # A re-marked uncertain task reconciled again must not settle twice.
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status = 'SUBMISSION_UNCERTAIN', "
            "error_code = 'SUBMISSION_UNCERTAIN', result_asset_id = NULL "
            "WHERE id = 'task-u1-0'"
        )
    _reconcile(fair_state, "task-u1-0", _ReconcileSucceededProvider(api_key="test-key"))
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("SETTLE", 1)]


def test_reconcile_cancelled_releases_exactly_once(fair_state: str) -> None:
    """A provider-reported cancellation releases the reserved credit exactly
    once; the wallet is restored to its pre-reservation balance."""
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=1,
        task_status="SUBMISSION_UNCERTAIN",
        wallet_credits=1000,
    )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute("UPDATE generation_tasks SET provider_task_id = 'pt-1' WHERE id = 'task-u1-0'")
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)

    result = _reconcile(fair_state, "task-u1-0", _ReconcileCancelledProvider(api_key="test-key"))
    assert result is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute("SELECT status FROM generation_tasks WHERE id = 'task-u1-0'").fetchone()
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u1'"
        ).fetchone()
    assert task["status"] == "FAILED"
    assert int(wallet["available_credits"]) == 1000
    assert int(wallet["reserved_credits"]) == 0
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("RELEASE", 1)]


def test_reconcile_refuses_without_provider_task_id(fair_state: str) -> None:
    """No provider task id: refuse to guess, keep the charge reserved and the
    wallet untouched until a human confirms (SUBMISSION_REQUIRES_MANUAL_*)."""
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=1,
        task_status="SUBMISSION_UNCERTAIN",
        wallet_credits=1000,
    )
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)

    with pytest.raises(HTTPException) as exc_info:
        _reconcile(fair_state, "task-u1-0", _ReconcileCancelledProvider(api_key="test-key"))
    assert exc_info.value.status_code == 409
    detail = cast(dict[str, object], exc_info.value.detail)
    assert detail["code"] == "SUBMISSION_REQUIRES_MANUAL_CONFIRMATION"
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1)]
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u1'"
        ).fetchone()
    assert int(wallet["available_credits"]) == 999
    assert int(wallet["reserved_credits"]) == 1


# ---------------------------------------------------------------------------
# M5 review regressions: slot ownership, sweeper races, FIFO claim race
# ---------------------------------------------------------------------------


def test_live_submission_uncertain_releases_slot(fair_state: str) -> None:
    """P1-2: a provider POST timeout on the LIVE path (not the expiry sweeper)
    moves the task to SUBMISSION_UNCERTAIN and releases the per-user slot with
    the transition — the task is no longer runnable and has no lease left for
    the sweeper to find, so the slot must not stay pinned at 1."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2, wallet_credits=1000)
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    assert str(lease["id"]) == "task-u1-0"
    assert _cursor_count(fair_state, "u1") == 1
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_task_submission_uncertain(
            conn,
            task_id=str(lease["id"]),
            message="POST timed out",
            provider_task_id="pt-1",
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, locked_by, locked_until, provider_task_id "
            "FROM generation_tasks WHERE id = %s",
            (str(lease["id"]),),
        ).fetchone()
    assert task["status"] == "SUBMISSION_UNCERTAIN"
    assert task["provider_task_id"] == "pt-1"  # the observed id survives
    assert task["locked_by"] is None
    assert _cursor_count(fair_state, "u1") == 0  # released with the transition
    lease2 = _acquire(fair_state, "w2")
    assert lease2 is not None
    assert str(lease2["id"]) == "task-u1-1"  # the user's next task runs


def test_uncertain_then_reconcile_failed_keeps_slot_free(fair_state: str) -> None:
    """P1-2 follow-on: after the live uncertain transition released the slot,
    the later reconcile-failed terminalization must not re-enter the slot
    accounting — the cursor stays 0 and the user's next task is claimable."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2, wallet_credits=1000)
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    assert str(lease["id"]) == "task-u1-0"
    assert _cursor_count(fair_state, "u1") == 1
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_task_submission_uncertain(
            conn, task_id=str(lease["id"]), message="POST timed out", provider_task_id="pt-1"
        )
    assert _cursor_count(fair_state, "u1") == 0
    _seed_reserved(fair_state, task_id=str(lease["id"]), available_credits=999, reserved_credits=1)
    result = _reconcile(
        fair_state, str(lease["id"]), _ReconcileCancelledProvider(api_key="test-key")
    )
    assert result is not None
    assert _cursor_count(fair_state, "u1") == 0
    lease2 = _acquire(fair_state, "w2")
    assert lease2 is not None
    assert str(lease2["id"]) == "task-u1-1"


def test_reconcile_does_not_release_replacement_slot(
    fair_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-5: the expired takeover already released the slot, which the user's
    replacement task now holds. Reconciling the old task as succeeded must not
    release again — that would zero the replacement's slot and let a third
    task run concurrently."""
    monkeypatch.setattr("app.generation._require_public_https_host", lambda _host: None)
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2, wallet_credits=1000)
    # X expires mid-call → SUBMISSION_UNCERTAIN + slot released.
    lease_x = _acquire(fair_state, "w1")
    assert lease_x is not None
    assert str(lease_x["id"]) == "task-u1-0"
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE generation_tasks SET locked_until = now() - interval '5 minutes' WHERE id = %s",
            (str(lease_x["id"]),),
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_expired_active_leases_needing_attention(conn)
    assert _cursor_count(fair_state, "u1") == 0
    # Y is claimed for the same user; the slot is now Y's.
    lease_y = _acquire(fair_state, "w2")
    assert lease_y is not None
    assert str(lease_y["id"]) == "task-u1-1"
    assert _cursor_count(fair_state, "u1") == 1
    # X is reconciled as succeeded: archive + terminal billing row, and Y's
    # slot must stay at 1 (the pre-fix unconditional release dropped it to 0).
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET provider_task_id = 'pt-x' WHERE id = %s",
            (str(lease_x["id"]),),
        )
    _seed_reserved(
        fair_state,
        task_id=str(lease_x["id"]),
        available_credits=999,
        reserved_credits=1,
    )
    result = _reconcile(
        fair_state, str(lease_x["id"]), _ReconcileSucceededProvider(api_key="test-key")
    )
    assert result is not None
    assert _cursor_count(fair_state, "u1") == 1  # Y's slot untouched


def test_concurrent_expiry_sweepers_audit_once(fair_state: str) -> None:
    """P2-1: two sweepers racing over the same expired lease — only the
    winner's UPDATE…RETURNING produces an audit row and a release; the loser
    sees zero rows and must not re-audit or release the slot a replacement
    task may hold."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE generation_tasks SET locked_until = now() - interval '5 minutes' WHERE id = %s",
            (str(lease["id"]),),
        )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def sweep() -> None:
        try:
            barrier.wait(timeout=10)
            with pg_transaction() as raw:
                conn = BusinessConnection.postgres(raw)
                mark_expired_active_leases_needing_attention(conn)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=sweep), threading.Thread(target=sweep)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert _audit_actions(fair_state) == [
        ("generation_task.lease_expired_uncertain", None, str(lease["id"]))
    ]
    assert _cursor_count(fair_state, "u1") == 0


def test_fifo_double_claim_race_skips_locked_head(fair_state: str) -> None:
    """P1-6: two workers racing for the FIFO head — the second must skip the
    row the first holds (FOR UPDATE SKIP LOCKED in the subquery) instead of
    blocking and then re-matching it after the winner commits, which double
    claimed the paid task. The race window is reproduced deterministically:
    worker 1 holds the head row locked inside an open transaction while
    worker 2 runs the real acquire path."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute("UPDATE runtime_settings SET fair_queue_enabled = false WHERE id = 1")
    holder_lock_until = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    holder = psycopg.connect(fair_state, autocommit=False)
    try:
        with holder.cursor() as cur:
            cur.execute(
                """
                UPDATE generation_tasks
                SET status = 'SUBMITTING', locked_by = 'w1', locked_until = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id FROM generation_tasks
                    WHERE status IN ('PENDING', 'QUEUED')
                      AND (locked_until IS NULL OR locked_until::timestamptz <= now())
                    ORDER BY created_at, id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id
                """,
                (holder_lock_until,),
            )
            held = cur.fetchone()
        assert held is not None and held[0] == "task-u1-0"
        # Worker 2 races on a separate connection: with SKIP LOCKED it picks
        # nothing (the only task is locked). Without it the subquery would
        # match task-u1-0 and the UPDATE would block until the holder rolls
        # back, then re-claim the same task — the double-claim bug.
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            lease = acquire_generation_task_lease(conn, worker_id="w2")
        assert lease is None
    finally:
        holder.rollback()
        holder.close()


def test_pg_worker_once_loop_claims_processes_and_drains(fair_state: str) -> None:
    """The production worker loop: claim commits in its own fenced
    transaction, work in a second; max_tasks bounds a round; an empty round
    exits. Tasks use the fake_h3 provider so the full paid-call path runs.
    (P3 — the loop previously had zero direct tests; this is also the shape
    the T27 drain worker now mirrors exactly.)"""
    _seed(fair_state, user_ids=["u1", "u2"], tasks_per_user=2, wallet_credits=1000)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute("UPDATE generation_tasks SET provider = 'fake_h3'")
    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
    for _ in range(4):
        processed = run_pg_worker_once(worker_id="w-loop", storage=storage, max_tasks=1)
        assert processed == 1
    # Drained: an empty round exits with zero.
    assert run_pg_worker_once(worker_id="w-loop", storage=storage) == 0
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            "SELECT status, archive_status, result_asset_id, provider_task_id, provider_result_url "
            "FROM generation_tasks ORDER BY id"
        ).fetchall()
    assert len(rows) == 4
    for row in rows:
        assert row["status"] == "SUCCEEDED"
        assert row["archive_status"] == "DIRECT"
        assert row["result_asset_id"] is None
        assert row["provider_task_id"]
        assert row["provider_result_url"] == f"fake://h3-results/{row['provider_task_id']}.mp4"
    assert _cursor_count(fair_state, "u1") == 0
    assert _cursor_count(fair_state, "u2") == 0


class _StepwiseMetasoProvider(MetasoH3Provider):
    def __init__(self) -> None:
        super().__init__(api_key="test-key")
        self.submit_count = 0
        self.query_count = 0

    def submit_image_to_video(self, request: dict[str, Any]) -> str:
        self.submit_count += 1
        provider_task_id = "provider-stepwise-1"
        if self.task_created_observer is not None:
            self.task_created_observer(provider_task_id)
        return provider_task_id

    def query_image_to_video(self, provider_task_id: str) -> H3QueryResult:
        self.query_count += 1
        if self.query_count == 1:
            return H3QueryResult(status="RUNNING")
        return H3QueryResult(
            status="SUCCEEDED",
            result_url="https://example.com/result.mp4",
        )

    def download_result(self, url: str) -> bytes:
        raise AssertionError("direct video delivery must not download generated media")


def test_pg_worker_persists_provider_id_and_resumes_without_resubmit(
    fair_state: str,
) -> None:
    """A paid task crosses separate worker rounds as RUNNING/ARCHIVING.

    The provider id is committed in the submission observer before the first
    round returns. Later rounds query/check the same task, preserve its direct
    result URL and never call submit a second time.
    """

    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
    provider = _StepwiseMetasoProvider()

    assert (
        run_pg_worker_once(
            worker_id="w-step",
            storage=storage,
            generation_provider=provider,
            max_tasks=1,
        )
        == 1
    )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        row = pg.execute(
            "SELECT status, provider_task_id FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
        assert row == ("RUNNING", "provider-stepwise-1")
        pg.execute("UPDATE generation_tasks SET next_poll_at = now() WHERE id = 'task-u1-0'")

    assert (
        run_pg_worker_once(
            worker_id="w-step",
            storage=storage,
            generation_provider=provider,
            max_tasks=1,
        )
        == 1
    )
    assert provider.submit_count == 1
    assert provider.query_count == 1
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute("UPDATE generation_tasks SET next_poll_at = now() WHERE id = 'task-u1-0'")

    assert (
        run_pg_worker_once(
            worker_id="w-step",
            storage=storage,
            generation_provider=provider,
            max_tasks=1,
        )
        == 1
    )
    assert provider.submit_count == 1
    with psycopg.connect(fair_state, autocommit=True) as pg:
        row = pg.execute(
            "SELECT status, provider_result_url FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
        assert row == ("SUCCEEDED", "https://example.com/result.mp4")

    assert (
        run_pg_worker_once(
            worker_id="w-step",
            storage=storage,
            generation_provider=provider,
            max_tasks=1,
        )
        == 0
    )
    assert provider.submit_count == 1
    with psycopg.connect(fair_state, autocommit=True) as pg:
        row = pg.execute(
            "SELECT status, archive_status, result_asset_id, provider_result_url "
            "FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
    assert row is not None
    assert row[0] == "SUCCEEDED"
    assert row[1] == "DIRECT"
    assert row[2] is None
    assert row[3] == "https://example.com/result.mp4"
    with psycopg.connect(fair_state, autocommit=True) as pg:
        assert pg.execute(
            "SELECT quality_status FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone() == ("NOT_REQUIRED",)


@pytest.mark.parametrize(
    ("status", "archive_status"), [("ARCHIVING", "PENDING"), ("SUCCEEDED", "ARCHIVE_FAILED")]
)
def test_pg_saved_video_result_finishes_without_provider_or_quality_credentials(
    fair_state: str, monkeypatch: pytest.MonkeyPatch, status: str, archive_status: str
) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status = %s, archive_status = %s, "
            "provider = 'metaso', provider_task_id = 'saved-paid-result', "
            "provider_result_url = 'https://example.com/result.mp4', next_poll_at = now()",
            (status, archive_status),
        )

    def reject_processing(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("saved video URLs require neither a provider nor media processing")

    monkeypatch.setattr("app.generation_worker.h3_provider_for_task", reject_processing)
    monkeypatch.setattr("app.generation.final_generation_quality", reject_processing)
    monkeypatch.setattr("app.generation.h3_audio_quality", reject_processing)
    assert (
        run_pg_worker_once(
            worker_id="saved-result",
            storage=FakeStorageAdapter(provider="cos", bucket="bucket"),
            max_tasks=1,
        )
        == 1
    )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        assert pg.execute(
            "SELECT status, archive_status, quality_status, provider_result_url, result_asset_id "
            "FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone() == (
            "SUCCEEDED",
            "DIRECT",
            "NOT_REQUIRED",
            "https://example.com/result.mp4",
            None,
        )
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("SETTLE", 1)]


def test_expired_running_lease_resumes_instead_of_becoming_uncertain(
    fair_state: str,
) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status = 'RUNNING', "
            "provider_task_id = 'provider-durable', locked_by = 'dead-worker', "
            "locked_until = now() - interval '5 minutes' WHERE id = 'task-u1-0'"
        )
        pg.execute("UPDATE user_queue_cursors SET running_tasks_count = 1 WHERE user_id = 'u1'")
    with pg_transaction() as raw:
        mark_expired_active_leases_needing_attention(BusinessConnection.postgres(raw))
    with psycopg.connect(fair_state, autocommit=True) as pg:
        row = pg.execute(
            "SELECT status, locked_by, next_poll_at FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
    assert row is not None
    assert row[0] == "RUNNING"
    assert row[1] is None
    assert row[2] is not None
    assert _cursor_count(fair_state, "u1") == 1
    assert _audit_actions(fair_state) == [
        ("generation_task.lease_expired_resumed", None, "task-u1-0")
    ]


@pytest.mark.parametrize("poll_timed_out", [False, True])
@pytest.mark.parametrize("reuse_worker_id", [False, True])
def test_late_poll_cannot_clear_replacement_lease_or_release_slot(
    fair_state: str, poll_timed_out: bool, reuse_worker_id: bool
) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status='RUNNING', provider_task_id='durable-poll-id', "
            "submitted_at=now() - (%s * interval '1 hour'), next_poll_at=now() "
            "WHERE id='task-u1-0'",
            (3 if poll_timed_out else 1,),
        )
        pg.execute("UPDATE user_queue_cursors SET running_tasks_count=1 WHERE user_id='u1'")
    with pg_transaction() as raw:
        old_lease = acquire_generation_continuation_lease(
            BusinessConnection.postgres(raw), worker_id="poll-old"
        )
    assert old_lease is not None
    # Simulate a stalled query whose lease expires, then the normal recovery
    # path claims it in another committed transaction without resubmission.
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET locked_until=now() - interval '1 minute' "
            "WHERE id='task-u1-0'"
        )
    with pg_transaction() as raw:
        new_lease = acquire_generation_continuation_lease(
            BusinessConnection.postgres(raw),
            worker_id="poll-old" if reuse_worker_id else "poll-new",
        )
    assert new_lease is not None
    assert new_lease["provider_task_id"] == old_lease["provider_task_id"]
    state_sql = (
        "SELECT status,locked_by,locked_until,next_poll_at,error_code,provider_task_id "
        "FROM generation_tasks WHERE id='task-u1-0'"
    )
    with psycopg.connect(fair_state) as pg:
        claimed_state = pg.execute(state_sql).fetchone()
    audit_before = _audit_actions(fair_state)
    for _ in range(2):
        with pg_transaction() as raw:
            reschedule_generation_poll(BusinessConnection.postgres(raw), lease=old_lease)
    with psycopg.connect(fair_state) as pg:
        assert pg.execute(state_sql).fetchone() == claimed_state
        assert pg.execute(
            "SELECT available_credits,reserved_credits FROM wallets WHERE user_id='u1'"
        ).fetchone() == (999, 1)
        assert pg.execute("SELECT COUNT(*) FROM external_call_logs").fetchone() == (0,)
    assert _cursor_count(fair_state, "u1") == 1
    assert _audit_actions(fair_state) == audit_before
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1)]
    # Only the replacement lease may reschedule or declare the polling timeout.
    with pg_transaction() as raw:
        reschedule_generation_poll(BusinessConnection.postgres(raw), lease=new_lease)
    with psycopg.connect(fair_state) as pg:
        final = pg.execute(state_sql).fetchone()
    assert final is not None and final[1:3] == (None, None)
    assert final[0] == ("SUBMISSION_UNCERTAIN" if poll_timed_out else "RUNNING")
    assert _cursor_count(fair_state, "u1") == (0 if poll_timed_out else 1)
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1)]


def test_provider_poll_timeout_releases_slot_without_paid_resubmit(
    fair_state: str,
) -> None:
    """A provider task missing for two hours becomes reconcilable attention.

    The provider id is retained, the user's fair-queue slot is released and
    the worker cannot silently submit the paid task again.
    """

    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status = 'RUNNING', "
            "provider_task_id = 'provider-too-old', "
            "submitted_at = now() - interval '3 hours', "
            "started_at = now() - interval '3 hours', "
            "locked_by = 'w-timeout', locked_until = now() + interval '1 minute' "
            "WHERE id = 'task-u1-0'"
        )
        pg.execute("UPDATE user_queue_cursors SET running_tasks_count = 1 WHERE user_id = 'u1'")
        locked_until = pg.execute(
            "SELECT locked_until FROM generation_tasks WHERE id='task-u1-0'"
        ).fetchone()[0]
    lease = {
        "id": "task-u1-0",
        "batch_id": "batch-u1",
        "provider_task_id": "provider-too-old",
        "locked_by": "w-timeout",
        "locked_until": locked_until,
    }
    with pg_transaction() as raw:
        reschedule_generation_poll(
            BusinessConnection.postgres(raw),
            lease=lease,
        )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        row = pg.execute(
            "SELECT status, error_code, provider_task_id, next_poll_at, locked_by "
            "FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
    assert row == (
        "SUBMISSION_UNCERTAIN",
        "PROVIDER_POLL_TIMEOUT",
        "provider-too-old",
        None,
        None,
    )
    assert _cursor_count(fair_state, "u1") == 0
    assert _audit_actions(fair_state) == [
        ("generation_task.provider_poll_timeout", None, "task-u1-0")
    ]


# ---------------------------------------------------------------------------
# BILL-03: dangling reservation detection
# ---------------------------------------------------------------------------


def test_find_dangling_billing_reservations_detects_bill_03(fair_state: str) -> None:
    """Only terminal tasks without a terminal billing row are flagged:
    archived success / failed-cancelled with a dangling RESERVE; uncertain
    tasks and already-settled tasks stay untouched."""
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=4,
        task_status="SUBMISSION_UNCERTAIN",
        wallet_credits=1000,
    )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        # t1, t2: archived success; t3 stays SUBMISSION_UNCERTAIN; t4: failed.
        pg.execute(
            "UPDATE generation_tasks SET status = 'SUCCEEDED', archive_status = 'ARCHIVED' "
            "WHERE id IN ('task-u1-0', 'task-u1-1')"
        )
        pg.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task-u1-3'")
        for task_id in ("task-u1-0", "task-u1-1", "task-u1-2", "task-u1-3"):
            pg.execute(
                "INSERT INTO wallet_transactions ("
                " id, user_id, type, available_delta, reserved_delta, "
                " task_id, billing_round, idempotency_key"
                ") VALUES (%s, 'u1', 'RESERVE', -1, 1, %s, 1, %s)",
                (f"wt-{task_id}-r1", task_id, f"reserve:{task_id}:1"),
            )
        pg.execute(
            "INSERT INTO wallet_transactions ("
            " id, user_id, type, available_delta, reserved_delta, "
            " task_id, billing_round, idempotency_key"
            ") VALUES ('wt-task-u1-1-s1', 'u1', 'SETTLE', 0, -1, "
            "'task-u1-1', 1, 'settle:task-u1-1:1')"
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        dangling = find_dangling_billing_reservations(conn)
    assert {d.task_id for d in dangling} == {"task-u1-0", "task-u1-3"}
    assert {d.billing_round for d in dangling} == {1}
    assert {d.user_id for d in dangling} == {"u1"}
