"""CW-010: wallet RESERVE/SETTLE/RELEASE recovery baseline on real PostgreSQL.

Migrated off the ``tmp_path`` SQLite lane onto a dedicated TEST-PG database with
an *independent multi-connection* baseline: every logical write block runs in its
own :func:`pg_transaction` (a separate pooled connection), so per-block
commit/rollback isolation and the RESERVE/SETTLE/RELEASE accounting deltas are
asserted against real PostgreSQL — never SQLite, never a mock-persisted store,
never a missing-PG skip (the CW-007 ``require_pg_or_explicit_skip`` hard gate
fails closed when the fixture is unreachable).

Migration finding (SQLite → PostgreSQL ledger ordering): the SQLite lane stored
``wallet_transactions.created_at`` at coarse precision, so rows written in the same
instant tied and fell back to ``type`` alphabetical ordering — which placed RELEASE
before RESERVE even though a RESERVE always happens first. PostgreSQL keeps that
same TEXT ``created_at`` (a per-transaction constant), so ordering by it plus
``type`` would encode the identical backwards artifact. Every ledger assertion here
instead orders by ``ledger_sequence``: migration 063 installs a BEFORE INSERT
trigger that assigns ``nextval('wallet_ledger_sequence_seq')`` under a
``SELECT 1 FROM wallets WHERE user_id = NEW.user_id FOR UPDATE`` row lock, giving an
immutable database-assigned causal order per user — the same key production
``control_routes`` and ``test_customer_chain_e2e`` already reconstruct balances
with. Same-transaction RESERVE→RELEASE and cross-transaction sweeps therefore both
read in true causal order, so ``[RESERVE, RELEASE]`` is asserted everywhere rather
than the alphabetical ``[RELEASE, RESERVE]`` tiebreak artifact.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator

# Set the audit HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-cw010-wallet-billing-tests-minimum-48-bytes-long-12",
)

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

import app.internal_billing as internal_billing
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.internal_billing import (
    BillingInvariantError,
    InsufficientCreditsError,
    finalize_internal_billing,
    reconcile_dangling_billing_reservations,
    reserve_internal_billing,
)

CW010_WALLET_DB_NAME = "cw010_wallet_billing_test"

_WALLET_TABLES = (
    "wallet_transactions, assets, generation_tasks, generation_batches, "
    "projects, wallets, runtime_settings, users"
)


@pytest.fixture(scope="module")
def wallet_dsn() -> Iterator[str]:
    """A dedicated migrated PG database for the wallet billing baseline.

    Created through the CW-007 kit helpers so ``assert_safe_test_database``
    guards the ``DROP ... WITH (FORCE)`` against the allowlisted name
    (registered in ``pg_test_kit.RECORDED_TEST_DATABASES``) and the kit resolves
    the admin DSN — no hand-rolled DSN concatenation lives in this file.
    """
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW010_WALLET_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW010_WALLET_DB_NAME)


@pytest.fixture()
def wallet_db(wallet_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the app PG pool at the wallet database for one test."""
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, wallet_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield wallet_dsn
    close_pg_pool()


def seed_task(dsn: str, *, available_credits: int = 2) -> None:
    """TRUNCATE and re-seed the single-user wallet scene on real PostgreSQL."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_WALLET_TABLES} CASCADE")
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
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            ("user_1", "user_1", "User One", "employee"),
        )
        pg.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            ("project_1", "user_1", "Project One"),
        )
        pg.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, "
            " request_hash, request_snapshot_json"
            ") VALUES ('batch_1', 'project_1', 'user_1', 'batch-key', 'hash', '{}')"
        )
        pg.execute(
            "INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, model, status) "
            "VALUES ('task_1', 'batch_1', 'I2V', 'fake_h3', 'MiniMax-H3', 'PENDING')"
        )
        pg.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, %s, 0)",
            ("user_1", available_credits),
        )


def wallet_state() -> tuple[int, int]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'user_1'"
        ).fetchone()
    assert row is not None
    return int(row["available_credits"]), int(row["reserved_credits"])


def transaction_types() -> list[tuple[str, int]]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            "SELECT type, billing_round FROM wallet_transactions "
            "WHERE task_id = 'task_1' ORDER BY ledger_sequence"
        ).fetchall()
    return [(str(row["type"]), int(row["billing_round"])) for row in rows]


def _deltas() -> list[tuple[str, int, int]]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            "SELECT type, available_delta, reserved_delta FROM wallet_transactions "
            "WHERE task_id = 'task_1' ORDER BY ledger_sequence"
        ).fetchall()
    return [
        (str(row["type"]), int(row["available_delta"]), int(row["reserved_delta"])) for row in rows
    ]


def test_reserve_and_finalize_support_multiple_seconds_per_round(wallet_db: str) -> None:
    """W11 按秒计费：预留/结算/释放按提交档位秒数记账（不再固定 1）。"""
    seed_task(wallet_db, available_credits=20)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
            seconds=15,
        )
    assert wallet_state() == (5, 15)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE generation_tasks "
            "SET status = 'SUCCEEDED', actual_output_seconds = 15, archive_status = 'DIRECT', "
            "    provider_result_url = 'https://cdn.example/video.mp4' "
            "WHERE id = 'task_1'"
        )
        result = finalize_internal_billing(conn, task_id="task_1", outcome="success")
    assert result.seconds == 15
    assert wallet_state() == (5, 0)
    assert _deltas() == [("RESERVE", -15, 15), ("SETTLE", 0, -15)]


def test_release_returns_all_reserved_seconds(wallet_db: str) -> None:
    seed_task(wallet_db, available_credits=20)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
            seconds=15,
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task_1'")
        result = finalize_internal_billing(conn, task_id="task_1", outcome="failed")
    assert result.seconds == 15
    assert wallet_state() == (20, 0)


def test_reserve_rejects_zero_or_negative_seconds(wallet_db: str) -> None:
    seed_task(wallet_db, available_credits=20)
    with pytest.raises(BillingInvariantError):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
                seconds=0,
            )


def test_missing_video_duration_stays_pending_until_verified_evidence(wallet_db: str) -> None:
    from decimal import Decimal

    from app.billing_evidence import record_evidence

    seed_task(wallet_db, available_credits=20)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn, user_id="user_1", task_id="task_1", billing_round=1, seconds=15
        )
        conn.execute(
            "UPDATE generation_tasks SET status='SUCCEEDED',archive_status='DIRECT',"
            "provider_result_url='https://example.test/result.mp4' WHERE id='task_1'"
        )
        assert (
            finalize_internal_billing(conn, task_id="task_1", outcome="success").transaction_type
            is None
        )
        operation = str(
            conn.execute("SELECT id FROM billing_operations WHERE source_id='task_1'").fetchone()[0]
        )
        record_evidence(
            conn,
            operation_id=operation,
            actor_id="user_1",
            reference="media-probe-1",
            reason="verified output duration",
            units=Decimal(12),
        )
    assert wallet_state() == (8, 0)
    assert transaction_types() == [("RESERVE", 1), ("SETTLE", 1), ("RELEASE", 1)]


def test_reserve_moves_one_credit_and_is_idempotent(wallet_db: str) -> None:
    seed_task(wallet_db)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        first_round = reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
        replay_round = reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
    assert first_round == replay_round == 1
    assert wallet_state() == (1, 1)
    assert transaction_types() == [("RESERVE", 1)]


def test_reserve_rejects_insufficient_credits_without_partial_write(wallet_db: str) -> None:
    seed_task(wallet_db, available_credits=0)
    with pytest.raises(InsufficientCreditsError):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
            )
    assert wallet_state() == (0, 0)
    assert transaction_types() == []


def test_reserve_rejects_a_new_round_while_the_previous_round_is_active(wallet_db: str) -> None:
    seed_task(wallet_db)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
    with pytest.raises(BillingInvariantError):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=2,
            )
    assert wallet_state() == (1, 1)
    assert transaction_types() == [("RESERVE", 1)]


def test_finalize_success_settles_only_an_archived_result(wallet_db: str) -> None:
    seed_task(wallet_db)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
    with pytest.raises(BillingInvariantError):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            finalize_internal_billing(conn, task_id="task_1", outcome="success")
    assert wallet_state() == (1, 1)
    assert transaction_types() == [("RESERVE", 1)]

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes, content_type, "
            " created_by_user_id"
            ") VALUES ('result_1', 'project_1', 'video', 'cos://bucket/result.mp4', "
            " 'sha', 12, 'video/mp4', 'user_1')"
        )
        conn.execute(
            "UPDATE generation_tasks "
            "SET status = 'SUCCEEDED', actual_output_seconds = 1, archive_status = "
            "'ARCHIVED', result_asset_id = 'result_1' "
            "WHERE id = 'task_1'"
        )
        first = finalize_internal_billing(conn, task_id="task_1", outcome="success")
        replay = finalize_internal_billing(conn, task_id="task_1", outcome="success")
    assert first.transaction_type == replay.transaction_type == "SETTLE"
    assert first.billing_round == replay.billing_round == 1
    assert wallet_state() == (1, 0)
    assert transaction_types() == [("RESERVE", 1), ("SETTLE", 1)]


def test_real_provider_result_must_be_archived_in_cos(wallet_db: str) -> None:
    seed_task(wallet_db)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
        conn.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes, content_type, "
            " created_by_user_id"
            ") VALUES ('result_local', 'project_1', 'video', 'local://results/task.mp4', "
            " 'sha', 12, 'video/mp4', 'user_1')"
        )
        conn.execute(
            "UPDATE generation_tasks "
            "SET provider = 'metaso', status = 'SUCCEEDED', archive_status = 'ARCHIVED', "
            "    result_asset_id = 'result_local' "
            "WHERE id = 'task_1'"
        )
    with pytest.raises(BillingInvariantError):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            finalize_internal_billing(conn, task_id="task_1", outcome="success")
    assert wallet_state() == (1, 1)
    assert transaction_types() == [("RESERVE", 1)]


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_finalize_failure_or_cancellation_releases_credit_once(
    wallet_db: str,
    outcome: str,
) -> None:
    seed_task(wallet_db, available_credits=1)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
        conn.execute(
            "UPDATE generation_tasks SET status = %s WHERE id = 'task_1'",
            ("FAILED" if outcome == "failed" else "CANCELLED",),
        )
        first = finalize_internal_billing(conn, task_id="task_1", outcome=outcome)
        replay = finalize_internal_billing(conn, task_id="task_1", outcome=outcome)
    assert first.transaction_type == replay.transaction_type == "RELEASE"
    assert wallet_state() == (1, 0)
    # 同事务内先 RESERVE 后 RELEASE；ledger_sequence（迁移 063 触发器在 wallets 行锁下
    # nextval 赋值）给出权威因果序，不再依赖 TEXT created_at + type 字母序 tiebreak。
    assert transaction_types() == [("RESERVE", 1), ("RELEASE", 1)]


def test_dangling_reservation_sweep_releases_terminal_failure_once(wallet_db: str) -> None:
    seed_task(wallet_db, available_credits=1)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
        conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task_1'")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        first = reconcile_dangling_billing_reservations(conn)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        replay = reconcile_dangling_billing_reservations(conn)
    assert first.scanned == first.released == 1
    assert first.settled == first.failed == 0
    assert replay.scanned == replay.released == replay.settled == replay.failed == 0
    assert wallet_state() == (1, 0)
    # RESERVE（事务 A）先于清扫的 RELEASE（事务 B）；ledger_sequence 权威因果序与
    # 跨事务时间序一致（旧 SQLite lane 靠 type 字母序 tiebreak，此处不再依赖）。
    assert transaction_types() == [("RESERVE", 1), ("RELEASE", 1)]


def test_dangling_reservation_sweep_settles_only_archived_success(wallet_db: str) -> None:
    seed_task(wallet_db)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
        conn.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes, content_type, "
            " created_by_user_id"
            ") VALUES ('result_1', 'project_1', 'video', 'cos://bucket/result.mp4', "
            " 'sha', 12, 'video/mp4', 'user_1')"
        )
        conn.execute(
            "UPDATE generation_tasks "
            "SET status = 'SUCCEEDED', actual_output_seconds = 1, archive_status = "
            "'ARCHIVED', result_asset_id = 'result_1' "
            "WHERE id = 'task_1'"
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        result = reconcile_dangling_billing_reservations(conn)
    assert result.scanned == result.settled == 1
    assert result.released == result.failed == 0
    assert wallet_state() == (1, 0)
    assert transaction_types() == [("RESERVE", 1), ("SETTLE", 1)]


def test_dangling_reservation_sweep_isolates_poisoned_candidate(
    wallet_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_task(wallet_db, available_credits=2)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "INSERT INTO generation_tasks ("
            " id, batch_id, generation_mode, provider, model, status"
            ") VALUES ('task_2', 'batch_1', 'I2V', 'fake_h3', 'MiniMax-H3', 'PENDING')"
        )
        reserve_internal_billing(conn, user_id="user_1", task_id="task_1", billing_round=1)
        reserve_internal_billing(conn, user_id="user_1", task_id="task_2", billing_round=1)
        conn.execute(
            "UPDATE generation_tasks SET status = 'FAILED' WHERE id IN ('task_1', 'task_2')"
        )

    original_finalize = internal_billing.finalize_internal_billing

    def poison_first_candidate(
        business_conn: BusinessConnection,
        *,
        task_id: str,
        outcome: internal_billing.BillingOutcome,
    ):
        result = original_finalize(
            business_conn,
            task_id=task_id,
            outcome=outcome,
        )
        if task_id == "task_1":
            raise BillingInvariantError("poisoned candidate")
        return result

    monkeypatch.setattr(internal_billing, "finalize_internal_billing", poison_first_candidate)
    # reconcile isolates each candidate in a SAVEPOINT, so the poisoned task_1
    # rolls back to its savepoint while task_2 still releases (real PG semantics).
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        result = reconcile_dangling_billing_reservations(conn)

    assert result.scanned == 2
    assert result.released == result.failed == 1
    assert result.settled == 0
    assert wallet_state() == (1, 1)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        terminal_rows = conn.execute(
            "SELECT task_id FROM wallet_transactions WHERE type = 'RELEASE' ORDER BY task_id"
        ).fetchall()
    assert [str(row[0]) for row in terminal_rows] == ["task_2"]


def test_released_task_can_reserve_a_new_billing_round(wallet_db: str) -> None:
    seed_task(wallet_db, available_credits=1)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
            billing_round=1,
        )
        conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task_1'")
        finalize_internal_billing(conn, task_id="task_1", outcome="failed")
        second_round = reserve_internal_billing(
            conn,
            user_id="user_1",
            task_id="task_1",
        )
    assert second_round == 2
    assert wallet_state() == (0, 1)
    # 同事务内因果序：预留(round1) → 释放(round1) → 新预留(round2)；ledger_sequence 权威给出。
    assert transaction_types() == [
        ("RESERVE", 1),
        ("RELEASE", 1),
        ("RESERVE", 2),
    ]


def test_historical_unbilled_task_is_a_noop(wallet_db: str) -> None:
    seed_task(wallet_db)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task_1'")
        result = finalize_internal_billing(conn, task_id="task_1", outcome="failed")
    assert result.transaction_type is None
    assert result.billing_round is None
    assert wallet_state() == (2, 0)
    assert transaction_types() == []


def test_concurrent_task_reservations_cannot_overspend(wallet_db: str) -> None:
    seed_task(wallet_db, available_credits=1)
    with psycopg.connect(wallet_db, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO generation_tasks "
            "(id, batch_id, generation_mode, provider, model, status) "
            "VALUES ('task_2', 'batch_1', 'I2V', 'fake_h3', 'MiniMax-H3', 'PENDING')"
        )

    barrier = threading.Barrier(2)
    results: list[str] = []
    result_lock = threading.Lock()

    def reserve(task_id: str) -> None:
        # Each thread reserves on its own pooled PG connection; the wallet row
        # lock serializes the two claims so exactly one wins the single credit.
        barrier.wait()
        try:
            with pg_transaction() as raw:
                conn = BusinessConnection.postgres(raw)
                reserve_internal_billing(
                    conn,
                    user_id="user_1",
                    task_id=task_id,
                    billing_round=1,
                )
            outcome = "reserved"
        except InsufficientCreditsError:
            outcome = "insufficient"
        with result_lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=reserve, args=("task_1",)),
        threading.Thread(target=reserve, args=("task_2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["insufficient", "reserved"]
    assert wallet_state() == (0, 1)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        reserve_rows = conn.execute(
            "SELECT COUNT(*) FROM wallet_transactions WHERE type = 'RESERVE'"
        ).fetchone()
    assert reserve_rows is not None
    assert int(reserve_rows[0]) == 1
