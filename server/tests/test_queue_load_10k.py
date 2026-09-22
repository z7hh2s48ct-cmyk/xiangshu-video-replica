"""T27 — 10k-task queue load, hot-index and connection-pool evidence (PG lane).

The fixture database is migrated with ``alembic upgrade head`` (the T25
precedent) so every query runs against the real revision-041 schema. The
tests drive the actual business functions on the PostgreSQL lane the way the
PG worker loop does: every logical operation inside its own fenced
``pg_transaction`` (T25).

Evidence collected (the task gates of T27):
- EXPLAIN ANALYZE on the three hot paths at 10,000 rows (rotation scan,
  per-user acquire subquery, release ownership lookup) — index scans, no
  Seq Scan on the 10k-row tables;
- four concurrent workers drain the full 10k queue: claim sets are disjoint
  and cover every task exactly once (no double claim), and the database
  reports zero deadlocks;
- full-drain wall-clock throughput;
- pool saturation: with all pool connections held, the next request queues
  and completes as soon as one frees (no crash, bounded wait).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Set HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t27-queue-load-tests-minimum-48-bytes-long-1234567890",
)

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip

from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.generation import acquire_generation_task_lease, release_user_queue_slot_for_task

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T27_DB_NAME = "t27_queue_load_test"

_N_USERS = 200
_TASKS_PER_USER = 50
_TOTAL_TASKS = _N_USERS * _TASKS_PER_USER  # 10,000

_TABLES = (
    "user_queue_cursors, generation_task_operations, external_call_logs, "
    "assets, wallet_transactions, recharge_orders, internal_access_tokens, "
    "wallets, generation_tasks, generation_batches, audit_logs, "
    "runtime_settings, users"
)

_REQUEST_SNAPSHOT = '{"output_duration_seconds": 3, "resolution": "480p"}'
_PROMPT_SNAPSHOT = '{"prompt_text": "a cat", "first_frame_uri": "cos://bucket/ff.jpg"}'


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t27_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T27_DB_NAME}"


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def t27_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T27_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T27_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t27_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t27_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T27_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def t27_state(t27_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, t27_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield t27_dsn
    close_pg_pool()


def _seed_10k(dsn: str) -> None:
    """TRUNCATE and insert 10,000 PENDING tasks across 200 users (50 each).

    本函数同时把三张被测表的 autovacuum 关掉。原因是一起真实事故：计划断言
    （``test_explain_hot_paths_use_indexes_at_10k`` 等）依赖规划器此刻所见的
    统计；autoanalyze 若**恰好**在灌数与 EXPLAIN 之间跑完，规划器就会改用
    基于真实行数的计划，断言随之翻车（PR #208 的 CI 上真实红过）。
    夹具库每次 run 都新建，统计状态本就由建库/迁移序列唯一确定；关掉
    autovacuum 只是把这份确定性固定下来，不改任何断言、也不改数据形状。
    """
    users = [f"u{i:03d}" for i in range(1, _N_USERS + 1)]
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("ALTER TABLE user_queue_cursors SET (autovacuum_enabled = false)")
        pg.execute("ALTER TABLE generation_tasks SET (autovacuum_enabled = false)")
        pg.execute("ALTER TABLE generation_batches SET (autovacuum_enabled = false)")
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        pg.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 100, 1000, 1000, 10000, 1000, true)"
        )
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('proj-owner', 'proj_owner', 'Project Owner', 'user')"
        )
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES "
            "('proj-1', 'Load Test Project', 'proj-owner')"
        )
        pg.execute(
            "INSERT INTO versions (id, project_id, kind, version_number, payload_json) "
            "VALUES ('pv-1', 'proj-1', 'video', 1, '{}')"
        )
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'user')",
                [(user_id, user_id, user_id) for user_id in users],
            )
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO generation_batches ("
                " id, project_id, created_by_user_id, idempotency_key, "
                " request_hash, request_snapshot_json, status"
                ") VALUES (%s, 'proj-1', %s, %s, %s, %s, 'QUEUED')",
                [
                    (
                        f"batch-{user_id}",
                        user_id,
                        f"ik-{user_id}",
                        f"rh-{user_id}",
                        _REQUEST_SNAPSHOT,
                    )
                    for user_id in users
                ],
            )
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO generation_tasks ("
                " id, batch_id, generation_mode, provider, model, "
                " status, archive_status, quality_status, "
                " prompt_version_id, prompt_snapshot_json, next_poll_at"
                ") VALUES (%s, %s, 'I2V', 'metaso', 'h3', 'PENDING', "
                "'PENDING', 'PENDING', 'pv-1', %s, NULL)",
                [
                    (
                        f"task-{user_id}-{index}",
                        f"batch-{user_id}",
                        _PROMPT_SNAPSHOT,
                    )
                    for user_id in users
                    for index in range(_TASKS_PER_USER)
                ],
            )
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO user_queue_cursors "
                "(user_id, last_dispatched_at, running_tasks_count) "
                "VALUES (%s, now(), 0)",
                [(user_id,) for user_id in users],
            )


def _fetch_scalar(raw: psycopg.Connection, sql: str) -> int:
    """Execute ``sql`` and return the first column of the first row as int."""
    row = raw.execute(sql).fetchone()
    if row is None:
        raise AssertionError(f"query returned no rows: {sql}")
    return int(row[0])


def _explain(dsn: str, sql: str) -> str:
    with pg_transaction() as raw:
        # Default row factory is tuple_row: EXPLAIN text rows come back as
        # plain tuples (setting row_factory to None would break row creation).
        rows = raw.execute(sql).fetchall()
        return "\n".join(str(row[0]) for row in rows)


# ---------------------------------------------------------------------------
# Evidence 1: hot-path EXPLAIN ANALYZE at 10,000 rows
# ---------------------------------------------------------------------------


def test_explain_hot_paths_use_indexes_at_10k(t27_state: str) -> None:
    """The three hot queries of the fair queue stay index-backed with 10,000
    PENDING tasks: the rotation partial index serves the idle-user scan, the
    per-user acquire plans through the batches unique index plus bitmap scans
    on both task indexes (idx_generation_tasks_active_attention for the batch,
    idx_generation_tasks_lease for the status), and the release lookup is a
    plain key lookup. No Seq Scan on the 10k-row tables."""
    _seed_10k(t27_state)

    rotation = _explain(
        t27_state,
        """
        EXPLAIN (ANALYZE, COSTS OFF)
        SELECT user_id
        FROM user_queue_cursors
        WHERE running_tasks_count = 0
        ORDER BY last_dispatched_at ASC
        LIMIT 1
        """,
    )
    acquire_subquery = _explain(
        t27_state,
        """
        EXPLAIN (ANALYZE, COSTS OFF)
        SELECT t.id
        FROM generation_tasks t
        WHERE
            (t.status IN ('PENDING', 'QUEUED')
             OR (
                 t.status = 'SUCCEEDED'
                 AND t.archive_status = 'ARCHIVE_FAILED'
                 AND t.provider_result_url IS NOT NULL
                 AND t.provider_result_url != ''
             ))
            AND (t.locked_until IS NULL OR t.locked_until::timestamptz <= now())
            AND (t.next_poll_at IS NULL OR t.next_poll_at::timestamptz <= now())
            AND EXISTS (
                SELECT 1
                FROM generation_batches b
                WHERE b.id = t.batch_id AND b.created_by_user_id = 'u001'
            )
        ORDER BY t.created_at, t.id
        LIMIT 1
        """,
    )
    release_lookup = _explain(
        t27_state,
        """
        EXPLAIN (ANALYZE, COSTS OFF)
        SELECT b.created_by_user_id
        FROM generation_tasks t
        JOIN generation_batches b ON b.id = t.batch_id
        WHERE t.id = 'task-u001-0'
        """,
    )

    print(f"--- rotation scan ---\n{rotation}")
    print(f"--- acquire subquery ---\n{acquire_subquery}")
    print(f"--- release lookup ---\n{release_lookup}")

    assert "Index Scan using idx_user_queue_cursors_rotation" in rotation
    assert "Seq Scan" not in rotation
    assert "Index Scan using uq_generation_batches_user_project_key" in acquire_subquery
    assert "Bitmap Index Scan on idx_generation_tasks_active_attention" in acquire_subquery
    assert "Bitmap Index Scan on idx_generation_tasks_lease" in acquire_subquery
    assert "Seq Scan on generation_tasks" not in acquire_subquery
    assert "Seq Scan on generation_batches" not in acquire_subquery
    assert "Index Scan using generation_tasks_pkey" in release_lookup
    assert "Seq Scan" not in release_lookup


# ---------------------------------------------------------------------------
# Evidence 2: four workers drain 10k with no double claim
# ---------------------------------------------------------------------------


def _drain_worker(
    dsn: str,
    worker_id: str,
    collected: list[tuple[str, str]],
    lock: threading.Lock,
) -> int:
    processed = 0
    while True:
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            lease = acquire_generation_task_lease(conn, worker_id=worker_id)
            if lease is None:
                return processed
            task_id = str(lease["id"])
        # Simulated paid processing, then terminal write + slot release in a
        # second fenced transaction (the run_next_generation_task shape).
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            conn.execute(
                "UPDATE generation_tasks SET status = 'SUCCEEDED', "
                "archive_status = 'ARCHIVED', locked_by = NULL, "
                "locked_until = NULL, completed_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (task_id,),
            )
            release_user_queue_slot_for_task(conn, task_id=task_id)
        with lock:
            collected.append((worker_id, task_id))
        processed += 1


def test_four_workers_drain_10k_without_double_claim(t27_state: str) -> None:
    """Four workers consume the full 10,000-task queue; every task is claimed
    by exactly one worker (disjoint, complete coverage), the database ends
    with zero deadlocks and the drain throughput is recorded."""
    _seed_10k(t27_state)
    collected: list[tuple[str, str]] = []
    lock = threading.Lock()
    start = time.monotonic()
    threads = [
        threading.Thread(target=_drain_worker, args=(t27_state, f"w{i}", collected, lock))
        for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    elapsed = time.monotonic() - start
    assert not any(thread.is_alive() for thread in threads)

    assert len(collected) == _TOTAL_TASKS
    seen: dict[str, list[str]] = {}
    for worker_id, task_id in collected:
        seen.setdefault(task_id, []).append(worker_id)
    assert len(seen) == _TOTAL_TASKS  # every task claimed
    assert all(len(claimers) == 1 for claimers in seen.values())  # exactly once

    with pg_transaction() as raw:
        deadlocks = _fetch_scalar(
            raw,
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()",
        )
        pending_locks = _fetch_scalar(raw, "SELECT COUNT(*) FROM pg_locks WHERE NOT granted")
        terminal = _fetch_scalar(
            raw,
            "SELECT COUNT(*) FROM generation_tasks "
            "WHERE status = 'SUCCEEDED' AND archive_status = 'ARCHIVED'",
        )
        stuck_cursors = _fetch_scalar(
            raw,
            "SELECT COUNT(*) FROM user_queue_cursors WHERE running_tasks_count > 0",
        )

    throughput = _TOTAL_TASKS / elapsed
    print(f"--- drain: {_TOTAL_TASKS} tasks in {elapsed:.1f}s = {throughput:.0f} tasks/s")
    print(
        f"--- deadlocks={deadlocks} pending_locks={pending_locks} "
        f"terminal={terminal} stuck_cursors={stuck_cursors}"
    )

    assert int(deadlocks) == 0
    assert int(pending_locks) == 0
    assert int(terminal) == _TOTAL_TASKS
    assert int(stuck_cursors) == 0


# ---------------------------------------------------------------------------
# Evidence 3: pool saturation queues and recovers
# ---------------------------------------------------------------------------


def test_pool_saturation_queues_within_timeout(t27_state: str) -> None:
    """With every pool connection held, the next request waits in the queue
    and completes as soon as one connection frees (no crash, bounded wait;
    the default pool timeout is 30 s)."""
    # Hold the context managers too: a bare Connection loses the generator,
    # whose GC would close it and return the connection to the pool, so the
    # pool would never actually saturate.
    held: list[tuple[Any, Any]] = []
    try:
        for _ in range(8):  # DEFAULT_POOL_MAX
            cm = pg_transaction()
            held.append((cm, cm.__enter__()))
        results: dict[str, float] = {}
        error: list[BaseException] = []

        def ninth() -> None:
            try:
                start = time.monotonic()
                with pg_transaction() as raw:
                    raw.execute("SELECT 1")
                results["wait"] = time.monotonic() - start
            except BaseException as exc:  # pragma: no cover - failure path
                error.append(exc)

        waiter = threading.Thread(target=ninth)
        waiter.start()
        time.sleep(0.5)
        assert waiter.is_alive()  # the 9th request is queued, pool exhausted
        held[0][0].__exit__(None, None, None)  # free one connection
        held.pop(0)
        waiter.join(timeout=15)
        assert not waiter.is_alive()
        assert not error
        assert results["wait"] < 15
        print(f"--- pool wait after freeing one connection: {results['wait'] * 1000:.0f} ms")
    finally:
        for cm, _conn in held:
            cm.__exit__(None, None, None)
