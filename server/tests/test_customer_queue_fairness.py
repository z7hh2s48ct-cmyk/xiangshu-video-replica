"""T25 — user fair queue tests (per-user concurrency 1, round-robin rotation).

The fixture database is migrated with ``alembic upgrade head`` (the T23/T34
precedent) so every query runs against the real revision-041 schema where
``user_queue_cursors`` and ``runtime_settings.fair_queue_enabled`` exist.
The tests drive the actual business functions on the PostgreSQL lane
(``acquire_generation_task_lease``, ``release_user_queue_slot_for_task``,
``mark_task_provider_failed``, ``mark_expired_active_leases_needing_attention``,
``ensure_user_queue_cursor``, ``cleanup_idle_queue_cursors``), each call
inside its own fenced transaction exactly like the PG worker loop (T25).

Test matrix (revised T24 ADR, §5):
- migration safety: the fair-queue switch is FALSE by default → legacy global
  FIFO keeps working (no cursor table dependency);
- single user: per-user concurrency 1 (second acquire sees the user busy);
- round-robin: idle users served least-recently-dispatched first;
- concurrent workers: SKIP LOCKED on the cursor row → no double claim;
- terminal states release the slot (SUCCEEDED / FAILED / expired lease);
- double release is idempotent (GREATEST guard);
- Pattern D cold start: ensure_user_queue_cursor makes a new user schedulable;
- Pattern E: idle-cursor cleanup keeps the table bounded;
- migration seed: a legacy running count above 1 clamps to 1 (LEAST).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

# Set HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t25-fair-queue-tests-minimum-48-bytes-long-1234567890",
)

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip

from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.generation import (
    acquire_generation_task_lease,
    cleanup_idle_queue_cursors,
    ensure_user_queue_cursor,
    mark_expired_active_leases_needing_attention,
    mark_task_provider_failed,
    release_user_queue_slot_for_task,
)

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T25_DB_NAME = "t25_fair_queue_test"

_FAIR_TABLES = (
    "user_queue_cursors, generation_task_operations, external_call_logs, "
    "assets, wallet_transactions, generation_tasks, generation_batches, "
    "audit_logs, runtime_settings, users"
)

_REQUEST_SNAPSHOT = '{"output_duration_seconds": 3, "resolution": "480p"}'
_PROMPT_SNAPSHOT = '{"prompt_text": "a cat", "first_frame_uri": "cos://bucket/ff.jpg"}'


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t25_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T25_DB_NAME}"


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fair_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T25_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T25_DB_NAME}"')
        # 门禁护栏：本文件任意残留的持锁事务必须在秒级快速失败，而不是把
        # 整个全量 pytest 挂死到人工 terminate（2026-09-07 梳理实测教训）。
        conn.execute(f"ALTER DATABASE \"{T25_DB_NAME}\" SET lock_timeout = '10s'")
        conn.execute(
            f"ALTER DATABASE \"{T25_DB_NAME}\" SET idle_in_transaction_session_timeout = '30s'"
        )
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t25_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t25_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T25_DB_NAME}" WITH (FORCE)')


def _seed(
    dsn: str,
    *,
    user_ids: list[str],
    tasks_per_user: int,
    task_status: str = "PENDING",
    cursor_users: list[str] | None = None,
    cursor_age_hours: dict[str, int] | None = None,
    fair_queue_enabled: bool = True,
) -> None:
    """TRUNCATE and re-seed users / batches / tasks / cursors.

    Cursor rows are inserted explicitly (the migration seed precedent) with
    ``last_dispatched_at`` spread by ``cursor_age_hours`` so round-robin order
    is deterministic: the oldest dispatcher is served first.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {_FAIR_TABLES} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 4, 100, 1000, 10000, 1000, %s)",
            (fair_queue_enabled,),
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('proj-owner', 'proj_owner', 'Project Owner', 'user')"
        )
        conn.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES "
            "('proj-1', 'Fair Queue Project', 'proj-owner')"
        )
        conn.execute(
            "INSERT INTO versions (id, project_id, kind, version_number, payload_json) "
            "VALUES ('pv-1', 'proj-1', 'video', 1, '{}')"
        )
        cursor_users = list(cursor_users) if cursor_users is not None else list(user_ids)
        for index, user_id in enumerate(user_ids):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'user')",
                (user_id, user_id, user_id),
            )
            batch_id = f"batch-{user_id}"
            conn.execute(
                "INSERT INTO generation_batches ("
                " id, project_id, created_by_user_id, idempotency_key, "
                " request_hash, request_snapshot_json, status"
                ") VALUES (%s, 'proj-1', %s, %s, %s, %s, 'QUEUED')",
                (batch_id, user_id, f"ik-{user_id}", f"rh-{user_id}", _REQUEST_SNAPSHOT),
            )
            for task_index in range(tasks_per_user):
                conn.execute(
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
            if user_id in cursor_users:
                age_hours = (cursor_age_hours or {}).get(user_id, 0)
                conn.execute(
                    "INSERT INTO user_queue_cursors "
                    "(user_id, last_dispatched_at, running_tasks_count) "
                    "VALUES (%s, now() - make_interval(hours => %s), 0)",
                    (user_id, age_hours),
                )


def _acquire(dsn: str, worker_id: str) -> dict[str, object] | None:
    """Acquire a lease the way the PG worker loop does: one fenced transaction."""
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        lease = acquire_generation_task_lease(conn, worker_id=worker_id)
        if lease is not None:
            return dict(lease)
        return None


def _complete_and_release(dsn: str, lease: dict[str, object]) -> None:
    """Terminal state + slot release in one fenced transaction."""
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE generation_tasks SET status = 'SUCCEEDED', "
            "archive_status = 'ARCHIVED', locked_by = NULL, locked_until = NULL, "
            "completed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (str(lease["id"]),),
        )
        release_user_queue_slot_for_task(conn, task_id=str(lease["id"]))


def _cursor_count(dsn: str, user_id: str) -> int:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute(
            "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        return int(row[0]) if row is not None else -1


@pytest.fixture()
def fair_state(fair_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, fair_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield fair_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# Migration safety / switch behavior
# ---------------------------------------------------------------------------


def test_fair_queue_disabled_keeps_global_fifo(fair_state: str) -> None:
    """Switch FALSE: the legacy global FIFO works with no cursor rows at all
    (a user with no cursor must still be served — the old behavior)."""
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=1,
        fair_queue_enabled=False,
        cursor_users=[],
    )
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    assert lease["created_by_user_id"] == "u1"
    assert _cursor_count(fair_state, "u1") == -1  # no cursor row was touched


def test_migration_seed_clamps_running_count(fair_state: str) -> None:
    """The 030 seed clamps a legacy per-user running count above 1 with LEAST
    (migration must never fail on data that already violated the invariant)."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1)
    with psycopg.connect(fair_state, autocommit=True) as conn:
        conn.execute("UPDATE generation_tasks SET status = 'RUNNING' WHERE batch_id = 'batch-u1'")
        conn.execute(
            "INSERT INTO generation_tasks ("
            " id, batch_id, generation_mode, provider, model, status, "
            " archive_status, quality_status, prompt_version_id, "
            " prompt_snapshot_json, next_poll_at"
            ") VALUES ('task-u1-extra', 'batch-u1', 'I2V', 'metaso', 'h3', "
            "'RUNNING', 'PENDING', 'PENDING', 'pv-1', %s, NULL)",
            (_PROMPT_SNAPSHOT,),
        )
        conn.execute(
            # The exact seed statement from migration 030 (SELECT part plus
            # the ON CONFLICT upsert, which the seed uses to refresh cursors):
            # running count 2 → LEAST(..., 1) → 1.
            "INSERT INTO user_queue_cursors ("
            " user_id, last_dispatched_at, running_tasks_count"
            ") SELECT %s, COALESCE(MAX(t.created_at::timestamptz), now()), "
            "LEAST(SUM(CASE WHEN t.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING') "
            "THEN 1 ELSE 0 END), 1) "
            "FROM generation_batches b "
            "LEFT JOIN generation_tasks t ON t.batch_id = b.id "
            "WHERE b.created_by_user_id = %s "
            "GROUP BY b.created_by_user_id "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "last_dispatched_at = EXCLUDED.last_dispatched_at, "
            "running_tasks_count = EXCLUDED.running_tasks_count",
            ("u1", "u1"),
        )
    assert _cursor_count(fair_state, "u1") == 1


# ---------------------------------------------------------------------------
# Fair-queue core
# ---------------------------------------------------------------------------


def test_single_user_concurrency_one(fair_state: str) -> None:
    """Per-user concurrency 1: a second acquire sees the user busy and no
    other idle user, so it returns None; after completion the slot frees."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2)
    lease1 = _acquire(fair_state, "w1")
    assert lease1 is not None
    assert lease1["created_by_user_id"] == "u1"
    assert _cursor_count(fair_state, "u1") == 1
    assert _acquire(fair_state, "w2") is None  # the only user is busy
    _complete_and_release(fair_state, lease1)
    assert _cursor_count(fair_state, "u1") == 0
    lease2 = _acquire(fair_state, "w3")
    assert lease2 is not None
    assert str(lease2["id"]) == "task-u1-1"


def test_round_robin_rotates_across_users(fair_state: str) -> None:
    """Idle users are served least-recently-dispatched first; after each
    completion the rotation moves to the next user."""
    _seed(
        fair_state,
        user_ids=["u1", "u2", "u3", "u4"],
        tasks_per_user=1,
        cursor_age_hours={"u1": 40, "u2": 30, "u3": 20, "u4": 10},
    )
    seen: list[str] = []
    for index in range(4):
        lease = _acquire(fair_state, f"w{index}")
        assert lease is not None
        seen.append(str(lease["created_by_user_id"]))
        _complete_and_release(fair_state, lease)
    assert seen == ["u1", "u2", "u3", "u4"]


def test_rotation_skips_user_whose_task_is_locked(fair_state: str) -> None:
    """P3: a user whose only eligible task is locked by another worker must
    not stall the rotation — the acquirer advances past them (dispatcher
    timestamp bumped so they rejoin at the tail) and serves the next idle
    user instead of returning None. The lock holder reproduces the window
    where worker 1 has claimed the task row but not yet committed."""
    _seed(
        fair_state,
        user_ids=["u1", "u2"],
        tasks_per_user=1,
        cursor_age_hours={"u1": 10, "u2": 1},
    )
    holder = psycopg.connect(fair_state, autocommit=False)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT id FROM generation_tasks WHERE id = 'task-u1-0' FOR UPDATE")
            assert cur.fetchone() is not None
            lease = _acquire(fair_state, "w2")
    finally:
        holder.rollback()
        holder.close()
    assert lease is not None
    assert str(lease["id"]) == "task-u2-0"  # rotation advanced past u1
    # Once the holder lets go, u1's task is schedulable again.
    lease2 = _acquire(fair_state, "w3")
    assert lease2 is not None
    assert str(lease2["id"]) == "task-u1-0"


def test_concurrent_workers_do_not_double_claim(fair_state: str) -> None:
    """Two workers acquiring at the same time never claim the same user's
    task: the shared capacity row serialises the claims, SKIP LOCKED on the
    cursor row rotates users.

    A claim transaction must never be parked open across a barrier: commit
    authority stays with the fenced block, so a worker waiting inside its
    uncommitted claim transaction holds the capacity row lock and deadlocks
    the second worker (2026-09-07 review, defect A)."""
    _seed(fair_state, user_ids=["u1", "u2"], tasks_per_user=1)
    start = threading.Barrier(2)
    leases: dict[str, dict[str, object] | None] = {}

    def worker(name: str) -> None:
        start.wait()  # both workers primed to acquire concurrently
        # 容量行锁（lock_shared_generation_capacity）随外层事务提交才释放，
        # 因此两个获取必须在各自事务提交后才会合——在事务内等待对方会构造
        # 出自死锁（先提交者持锁，后到者在 runtime FOR UPDATE 上排队）。
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            start.wait()  # both workers race into their claim transactions
            leases[name] = acquire_generation_task_lease(conn, worker_id=name)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)

    acquired = [lease for lease in leases.values() if lease is not None]
    assert len(acquired) == 2  # two idle users, two workers
    task_ids = {str(lease["id"]) for lease in acquired}
    assert len(task_ids) == 2  # no double claim
    users = {str(lease["created_by_user_id"]) for lease in acquired}
    assert users == {"u1", "u2"}


def test_failed_task_releases_slot(fair_state: str) -> None:
    """A FAILED task (provider error) frees the slot: the user's next task
    becomes schedulable instead of starving the user (ADR Pattern C)."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2)
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_task_provider_failed(
            conn,
            lease=lease,
            provider_task_id="pt-1",
        )
    assert _cursor_count(fair_state, "u1") == 0
    lease2 = _acquire(fair_state, "w2")
    assert lease2 is not None
    assert str(lease2["id"]) == "task-u1-1"


def test_expired_lease_releases_slot(fair_state: str) -> None:
    """A lease that expired while the worker was gone (defensive path) marks
    the task SUBMISSION_UNCERTAIN and frees the slot; the task is never
    automatically resubmitted (do not re-pay the provider)."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2)
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE generation_tasks SET locked_until = now() - interval '5 minutes' WHERE id = %s",
            (str(lease["id"]),),
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_expired_active_leases_needing_attention(conn)
    assert _cursor_count(fair_state, "u1") == 0
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        status = conn.execute(
            "SELECT status FROM generation_tasks WHERE id = %s", (str(lease["id"]),)
        ).fetchone()
        assert str(status[0]) == "SUBMISSION_UNCERTAIN"
    lease2 = _acquire(fair_state, "w2")
    assert lease2 is not None
    assert str(lease2["id"]) == "task-u1-1"


def test_double_release_is_idempotent(fair_state: str) -> None:
    """GREATEST guard: releasing twice never drives the counter negative."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1)
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        release_user_queue_slot_for_task(conn, task_id=str(lease["id"]))
        release_user_queue_slot_for_task(conn, task_id=str(lease["id"]))
    assert _cursor_count(fair_state, "u1") == 0


# ---------------------------------------------------------------------------
# Pattern D / Pattern E
# ---------------------------------------------------------------------------


def test_new_user_gets_cursor_on_batch_creation(fair_state: str) -> None:
    """Cold start (Pattern D): a user without a cursor row is not schedulable;
    the same-transaction upsert on task creation makes them one."""
    _seed(
        fair_state,
        user_ids=["u1", "u2"],
        tasks_per_user=1,
        cursor_users=["u1"],
    )
    lease1 = _acquire(fair_state, "w1")
    assert lease1 is not None
    assert lease1["created_by_user_id"] == "u1"
    _complete_and_release(fair_state, lease1)
    assert _acquire(fair_state, "w2") is None  # u2 has no cursor row yet
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        ensure_user_queue_cursor(conn, user_id="u2")
    lease2 = _acquire(fair_state, "w3")
    assert lease2 is not None
    assert lease2["created_by_user_id"] == "u2"


def test_idle_cursor_cleanup_preserves_users_with_pending_work(fair_state: str) -> None:
    """Pattern E never removes an old cursor when its user still has work.

    Otherwise the fair worker cannot discover that pending work again, because
    it selects candidates from the cursor table.
    """
    _seed(fair_state, user_ids=["u1", "u2"], tasks_per_user=1)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE user_queue_cursors SET last_dispatched_at = now() - interval '40 days'"
        )
        conn.execute(
            "DELETE FROM generation_tasks WHERE batch_id IN ("
            "SELECT id FROM generation_batches WHERE created_by_user_id = 'u2')"
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        deleted = cleanup_idle_queue_cursors(conn, idle_days=30)
        assert deleted == 1
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute("SELECT user_id FROM user_queue_cursors ORDER BY user_id").fetchall()
        assert [str(row[0]) for row in rows] == ["u1"]


def test_runtime_settings_write_flips_fair_queue_switch(fair_state: str) -> None:
    """M4/M5 review M2: the fair-queue rollout switch flips through the
    audited SettingsRepository write path (what PATCH /api/admin/settings/
    runtime calls behind SettingsAdmin), not ad-hoc SQL. Omitting the field
    leaves the current mode alone, and the flip is visible to the queue's own
    gate and to read_fair_queue_enabled (the save deliberately does NOT echo
    the switch — PR #68 Codex P2: an echoed flag would let a stale settings
    panel round-trip it back off on an unrelated limits save)."""
    from cryptography.fernet import Fernet

    from app.generation import _fair_queue_enabled
    from app.settings import SettingsRepository

    _seed(fair_state, user_ids=["u1"], tasks_per_user=0, fair_queue_enabled=False)

    def _save(fair_queue_enabled: bool | None) -> None:
        with pg_transaction() as raw:
            # Runtime settings never touch encrypted provider config; a
            # throwaway key satisfies the constructor (this module sets no
            # VIDEO_REPLICA_SETTINGS_KEY).
            repo = SettingsRepository(
                BusinessConnection.postgres(raw), fernet=Fernet(Fernet.generate_key())
            )
            result = repo.save_runtime_settings(
                max_generation_count_per_batch=4,
                max_concurrent_h3_tasks=100,
                active_storage_provider="local",
                actor_user_id="u1",
                fair_queue_enabled=fair_queue_enabled,
            )
            # The limits response never carries the switch (no echo).
            assert "fair_queue_enabled" not in result

    def _mode() -> bool:
        from cryptography.fernet import Fernet

        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            repo = SettingsRepository(conn, fernet=Fernet(Fernet.generate_key()))
            mode = repo.read_fair_queue_enabled()
            # The queue's own gate and the repository read must agree.
            assert _fair_queue_enabled(conn) is mode
            return mode

    # Seed starts FALSE; the audited write flips it on.
    assert _mode() is False
    _save(True)
    assert _mode() is True

    # Omitting the field (None) keeps the switch where it is.
    _save(None)
    assert _mode() is True

    # And back off through the same path.
    _save(False)
    assert _mode() is False
