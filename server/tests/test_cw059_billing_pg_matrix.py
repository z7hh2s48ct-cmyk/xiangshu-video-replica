"""CW-059 — billing-domain PostgreSQL constraint & concurrency matrix (TEST-PG).

V3 收敛清单 CW-059 remaining work: ``补齐账务与全部任务测试的真实 PG 覆盖 …
补 … 与实际锁约束（FOR UPDATE SKIP LOCKED / advisory lock / 约束 / 不确定提交
恢复）；旧测试 100% 映射``.  This module closes the **账务/支付/钱包** half of
that mandate on a dedicated real-PostgreSQL database (``cw059_billing_test``,
port-isolated at 5437), migrated to alembic head so every billing constraint
from ``022_internal_billing`` / ``025_postgres_runtime_compatibility`` /
``057_second_based_billing`` / ``063_wallet_ledger_sequence`` is present.

What this file deliberately does NOT re-do (DoD「剔除重复开发」), with the suite
that already proves it on real PostgreSQL:

* wallet RESERVE/SETTLE/RELEASE **logic**, seconds-based billing, idempotency,
  dangling-reservation sweep, SAVEPOINT isolation and the concurrent
  wallet-row-lock overspend guard → ``test_wallet_billing_service.py`` (CW-010,
  incl. ``test_concurrent_task_reservations_cannot_overspend``).
* ZPay **payment-callback** matrix (paid/duplicate/out-of-order/forged/
  cross-user/closed-order/price-snapshot) and the task-result finalization
  matrix → ``test_cw029_billing_pg_matrix.py`` (CW-029).
* ``/api/wallet`` **owner-scoping** on the converged customer-session PG lane →
  ``test_cw026_converged_auth.py::test_customer_session_reads_resolve_to_session_owner_only``.
* recharge_orders **provider/pricing/amount CHECK shapes** and the
  terminal-round partial-unique index → ``test_postgres_migrations.py``
  (``test_pg_billing_provider_shapes_accepted_and_rejected``,
  ``test_pg_wallet_terminal_round_row_level``).
* ledger_sequence **INSERT** "database assigned" refusal →
  ``test_cw056_supported_head_matrix.py``.

The genuine gap this matrix fills is the **raw database constraint enforcement**
that ``test_internal_billing.py`` still asserts only through the legacy SQLite
lane (``sqlite3.IntegrityError``): the wallet non-negative CHECKs, the
recharge_orders merchant/provider-trade-no uniqueness, the wallet_transactions
idempotency / CHARGE-per-order / RESERVE-per-round uniqueness, the ledger shape
CHECK, and the ledger_sequence UPDATE-immutability trigger — each proven here
against real PostgreSQL (``psycopg.errors.CheckViolation`` 23514 /
``UniqueViolation`` 23505 / ``RaiseException`` P0001) and, for the two globally
unique keys, under **real multi-connection concurrency** so the guarantee is
shown to hold across independent sessions rather than one serial connection.

Each test maps 1:1 to a legacy SQLite assertion it replaces; the mapping is
recorded in ``docs/evidence/CW059-EVIDENCE.md`` §3.1.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

CW059_BILLING_DB_NAME = "cw059_billing_test"

# Truncated (FK bypassed) then re-seeded per test so every case starts from an
# identical, known-valid billing scene.
_TRUNCATE_TABLES = (
    "wallet_transactions, recharge_orders, wallets, generation_tasks, "
    "generation_batches, projects, runtime_settings, users"
)

# A legal head-schema order (zpay + INTERNAL + PENDING, no trade number) — the
# same baseline shape test_postgres_migrations._T08_BASELINE_ORDER uses.
_ORDER_BASELINE: dict[str, object] = {
    "provider": "zpay",
    "provider_trade_no": None,
    "channel": "alipay",
    "status": "PENDING",
    "pricing_scope": "INTERNAL",
    "base_unit_price_fen_snapshot": 1000,
    "charged_unit_price_fen_snapshot": 1000,
    "min_recharge_fen_snapshot": 10000,
    "recharge_step_fen_snapshot": 1000,
    "amount_fen": 10000,
    "credits": 10,
    "paid_at": None,
}


# ---------------------------------------------------------------------------
# Fixtures — CW-007 kit helpers only (no hand-rolled DSN concatenation).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def billing_dsn() -> Iterator[str]:
    """A dedicated migrated PG database for the billing constraint matrix.

    ``require_pg_or_explicit_skip`` hard-gates (never a silent skip counting as
    evidence); ``create/drop_test_database`` are allowlist-guarded so cleanup
    can only ever touch ``cw059_billing_test``.
    """
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW059_BILLING_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW059_BILLING_DB_NAME)


@pytest.fixture()
def seeded(billing_dsn: str) -> Iterator[str]:
    """TRUNCATE + re-seed the single-user billing scene; yield its DSN."""
    _reset_and_seed(billing_dsn)
    yield billing_dsn


def _insert_order(
    conn: psycopg.Connection,
    *,
    order_id: str,
    merchant_order_no: str,
    user_id: str = "user_1",
    **overrides: object,
) -> None:
    row: dict[str, object] = {
        "id": order_id,
        "user_id": user_id,
        "merchant_order_no": merchant_order_no,
        **_ORDER_BASELINE,
        **overrides,
    }
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    conn.execute(
        f"INSERT INTO recharge_orders ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def _insert_tx(
    conn: psycopg.Connection,
    *,
    tx_id: str,
    tx_type: str,
    available_delta: int,
    reserved_delta: int,
    idempotency_key: str,
    recharge_order_id: str | None = None,
    task_id: str | None = None,
    billing_round: int | None = None,
    user_id: str = "user_1",
) -> None:
    # ledger_sequence is intentionally omitted: migration 063 installs a BEFORE
    # INSERT trigger that assigns it from a sequence under a wallets FOR UPDATE
    # row lock; supplying it explicitly is itself rejected ("database assigned").
    conn.execute(
        """
        INSERT INTO wallet_transactions (
            id, user_id, type, available_delta, reserved_delta,
            recharge_order_id, task_id, billing_round, idempotency_key
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tx_id,
            user_id,
            tx_type,
            available_delta,
            reserved_delta,
            recharge_order_id,
            task_id,
            billing_round,
            idempotency_key,
        ),
    )


def _reset_and_seed(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {_TRUNCATE_TABLES} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('user_1', 'user_1', 'User One', 'customer')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('user_1', 100, 0)"
        )
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES ('proj_1', 'user_1', 'CW-059')"
        )
        conn.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, "
            " request_hash, request_snapshot_json"
            ") VALUES ('batch_1', 'proj_1', 'user_1', 'batch-key', 'hash', '{}')"
        )
        conn.execute(
            "INSERT INTO generation_tasks "
            "(id, batch_id, generation_mode, provider, model, status) "
            "VALUES ('task_1', 'batch_1', 'I2V', 'fake_h3', 'MiniMax-H3', 'PENDING')"
        )
        _insert_order(conn, order_id="order_1", merchant_order_no="CW059-ORD-1")
        _insert_order(conn, order_id="order_2", merchant_order_no="CW059-ORD-2")


def _count(dsn: str, sql: str, params: tuple[object, ...] = ()) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Group A — raw DB constraint enforcement on PostgreSQL (23514 / 23505).
# Mirrors test_internal_billing.py's three SQLite-only constraint tests.
# ---------------------------------------------------------------------------


def test_pg_wallets_reject_negative_available_and_reserved(seeded: str) -> None:
    """Maps test_internal_billing.py::test_wallets_reject_negative_balances.

    SQLite asserted ``sqlite3.IntegrityError``; PostgreSQL must enforce the
    ``ck_wallets_available_nonnegative`` / ``ck_wallets_reserved_nonnegative``
    CHECK constraints with a real ``CheckViolation`` (SQLSTATE 23514), and the
    rejected UPDATE must leave the stored balance untouched.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as available:
            conn.execute("UPDATE wallets SET available_credits = -1 WHERE user_id = 'user_1'")
        assert available.value.diag.table_name == "wallets"
        assert available.value.diag.constraint_name == "ck_wallets_available_nonnegative"

        with pytest.raises(psycopg.errors.CheckViolation) as reserved:
            conn.execute("UPDATE wallets SET reserved_credits = -1 WHERE user_id = 'user_1'")
        assert reserved.value.diag.table_name == "wallets"
        assert reserved.value.diag.constraint_name == "ck_wallets_reserved_nonnegative"

        row = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'user_1'"
        ).fetchone()
    assert row is not None and (int(row[0]), int(row[1])) == (100, 0)


def test_pg_recharge_orders_reject_duplicate_merchant_order_no(seeded: str) -> None:
    """Maps the merchant_order_no arm of test_internal_billing.py::
    test_recharge_orders_reject_duplicate_merchant_and_provider_numbers.

    ``order_1`` already holds ``CW059-ORD-1``; a second order presenting the
    same merchant order number must be refused by the column UNIQUE constraint.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation) as dup:
            _insert_order(conn, order_id="order_dup", merchant_order_no="CW059-ORD-1")
        assert dup.value.diag.table_name == "recharge_orders"
        assert "merchant_order_no" in (dup.value.diag.message_primary or "")
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM recharge_orders WHERE merchant_order_no = %s",
            ("CW059-ORD-1",),
        )
        == 1
    )


def test_pg_recharge_orders_reject_duplicate_provider_trade_no(seeded: str) -> None:
    """Maps the provider_trade_no arm of the same legacy test.

    Two PAID ZPay orders may not share one globally-unique provider trade
    number: the partial UNIQUE index ``uq_recharge_orders_provider_trade_no``
    (WHERE provider_trade_no IS NOT NULL) refuses the second.
    """
    paid = {
        "status": "PAID",
        "pricing_scope": "CUSTOMER_STANDARD",
        "charged_unit_price_fen_snapshot": 1500,
        "amount_fen": 15000,
        "credits": 10,
        "paid_at": "2026-09-11T00:00:00+00:00",
    }
    with psycopg.connect(seeded, autocommit=True) as conn:
        _insert_order(
            conn,
            order_id="paid_1",
            merchant_order_no="CW059-PAID-1",
            provider_trade_no="ZPAY-TRADE-DUP",
            **paid,
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as dup:
            _insert_order(
                conn,
                order_id="paid_2",
                merchant_order_no="CW059-PAID-2",
                provider_trade_no="ZPAY-TRADE-DUP",
                **paid,
            )
        assert dup.value.diag.table_name == "recharge_orders"
        assert dup.value.diag.constraint_name == "uq_recharge_orders_provider_trade_no"
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM recharge_orders WHERE provider_trade_no = %s",
            ("ZPAY-TRADE-DUP",),
        )
        == 1
    )


def test_pg_recharge_orders_reject_blank_provider_trade_no(seeded: str) -> None:
    """Maps the empty-string arm (``order_5``) of the same legacy test.

    Migration 026 reshaped 022's ``..._not_blank`` CHECK into the stricter
    provider/status/trade-no coupling ``ck_recharge_orders_provider_trade_no``:
    a PENDING (non-PAID) zpay order must carry ``provider_trade_no IS NULL``.
    A blank ``''`` therefore violates the coupling (it is not NULL), so an empty
    string can never squat the globally-unique trade-number space — PostgreSQL
    refuses it with a real ``CheckViolation``.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as blank:
            _insert_order(
                conn,
                order_id="order_blank",
                merchant_order_no="CW059-BLANK",
                provider_trade_no="",
            )
        assert blank.value.diag.table_name == "recharge_orders"
        assert blank.value.diag.constraint_name == "ck_recharge_orders_provider_trade_no"


def test_pg_wallet_transactions_reject_duplicate_idempotency_key(seeded: str) -> None:
    """Maps the idempotency arm of test_internal_billing.py::
    test_wallet_transactions_reject_duplicate_charge_reserve_and_terminal.

    ``idempotency_key`` is globally UNIQUE across the ledger; a second row
    reusing it (here on a *different* order, so only the idempotency key — not
    the CHARGE-per-order index — can fire) is refused.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        _insert_tx(
            conn,
            tx_id="tx_charge_1",
            tx_type="CHARGE",
            available_delta=10,
            reserved_delta=0,
            recharge_order_id="order_1",
            idempotency_key="charge:order_1",
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as dup:
            _insert_tx(
                conn,
                tx_id="tx_charge_2",
                tx_type="CHARGE",
                available_delta=10,
                reserved_delta=0,
                recharge_order_id="order_2",
                idempotency_key="charge:order_1",
            )
        assert dup.value.diag.table_name == "wallet_transactions"
        assert "idempotency_key" in (dup.value.diag.message_primary or "")
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM wallet_transactions WHERE idempotency_key = %s",
            ("charge:order_1",),
        )
        == 1
    )


def test_pg_wallet_transactions_reject_duplicate_charge_per_order(seeded: str) -> None:
    """Maps the CHARGE arm of the same legacy test.

    One recharge order may be credited exactly once: the partial UNIQUE index
    ``uq_wallet_transactions_charge_order`` (WHERE type = 'CHARGE') refuses a
    second CHARGE on ``order_1`` even under a fresh idempotency key.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        _insert_tx(
            conn,
            tx_id="tx_charge_a",
            tx_type="CHARGE",
            available_delta=10,
            reserved_delta=0,
            recharge_order_id="order_1",
            idempotency_key="charge:a",
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as dup:
            _insert_tx(
                conn,
                tx_id="tx_charge_b",
                tx_type="CHARGE",
                available_delta=10,
                reserved_delta=0,
                recharge_order_id="order_1",
                idempotency_key="charge:b",
            )
        assert dup.value.diag.table_name == "wallet_transactions"
        assert dup.value.diag.constraint_name == "uq_wallet_transactions_charge_order"
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM wallet_transactions "
            "WHERE type = 'CHARGE' AND recharge_order_id = %s",
            ("order_1",),
        )
        == 1
    )


def test_pg_wallet_transactions_reject_duplicate_reserve_round(seeded: str) -> None:
    """Maps the RESERVE arm of the same legacy test.

    One (task, billing_round) may hold a single RESERVE: the partial UNIQUE
    index ``uq_wallet_transactions_reserve_round`` (WHERE type = 'RESERVE')
    refuses a second reservation on ``task_1`` round 1.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        _insert_tx(
            conn,
            tx_id="tx_reserve_1",
            tx_type="RESERVE",
            available_delta=-1,
            reserved_delta=1,
            task_id="task_1",
            billing_round=1,
            idempotency_key="reserve:task_1:1",
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as dup:
            _insert_tx(
                conn,
                tx_id="tx_reserve_2",
                tx_type="RESERVE",
                available_delta=-1,
                reserved_delta=1,
                task_id="task_1",
                billing_round=1,
                idempotency_key="reserve:task_1:1:again",
            )
        assert dup.value.diag.table_name == "wallet_transactions"
        assert dup.value.diag.constraint_name == "uq_wallet_transactions_reserve_round"
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM wallet_transactions "
            "WHERE type = 'RESERVE' AND task_id = %s AND billing_round = 1",
            ("task_1",),
        )
        == 1
    )


def test_pg_wallet_transactions_reject_duplicate_terminal_round(seeded: str) -> None:
    """Maps the terminal (SETTLE/RELEASE) arm of the same legacy test.

    Prior art ``test_postgres_migrations.py::test_pg_wallet_terminal_round_row_level``
    proves RESERVE+SETTLE coexist and a second terminal is refused at the
    migration level; this re-asserts the same ``uq_wallet_transactions_terminal_round``
    partial UNIQUE index inside the CW-059 billing scene so the charge/reserve/
    terminal triple is complete in one place.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        _insert_tx(
            conn,
            tx_id="tx_reserve",
            tx_type="RESERVE",
            available_delta=-2,
            reserved_delta=2,
            task_id="task_1",
            billing_round=1,
            idempotency_key="reserve:task_1:1",
        )
        _insert_tx(
            conn,
            tx_id="tx_settle",
            tx_type="SETTLE",
            available_delta=0,
            reserved_delta=-2,
            task_id="task_1",
            billing_round=1,
            idempotency_key="settle:task_1:1",
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as dup:
            _insert_tx(
                conn,
                tx_id="tx_release_late",
                tx_type="RELEASE",
                available_delta=2,
                reserved_delta=-2,
                task_id="task_1",
                billing_round=1,
                idempotency_key="release:task_1:1",
            )
        assert dup.value.diag.table_name == "wallet_transactions"
        assert dup.value.diag.constraint_name == "uq_wallet_transactions_terminal_round"
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM wallet_transactions "
            "WHERE type IN ('SETTLE', 'RELEASE') AND task_id = %s AND billing_round = 1",
            ("task_1",),
        )
        == 1
    )


def test_pg_wallet_transactions_reject_malformed_ledger_shape(seeded: str) -> None:
    """The 057 seconds-based ``ck_wallet_transactions_shape`` CHECK on PG.

    A RESERVE must satisfy ``available_delta = -reserved_delta`` and
    ``reserved_delta >= 1``; ``(-1, +2)`` breaks the pairing and is refused by
    PostgreSQL, not by application code (No-Go rule).
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as shape:
            _insert_tx(
                conn,
                tx_id="tx_bad_shape",
                tx_type="RESERVE",
                available_delta=-1,
                reserved_delta=2,
                task_id="task_1",
                billing_round=2,
                idempotency_key="reserve:bad:shape",
            )
        assert shape.value.diag.table_name == "wallet_transactions"
        assert shape.value.diag.constraint_name == "ck_wallet_transactions_shape"
    assert _count(seeded, "SELECT COUNT(*) FROM wallet_transactions") == 0


def test_pg_ledger_sequence_is_immutable_on_update(seeded: str) -> None:
    """Maps the SQLite ``immutable`` assertion in test_internal_admin.py onto PG.

    Migration 063's BEFORE UPDATE trigger raises ``ledger_sequence is immutable``
    when a stored causal order is rewritten. CW-056 already proves the INSERT
    "database assigned" refusal on PG; the UPDATE-immutability path is the
    remaining PG gap this closes.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        _insert_tx(
            conn,
            tx_id="tx_ledger",
            tx_type="CHARGE",
            available_delta=10,
            reserved_delta=0,
            recharge_order_id="order_1",
            idempotency_key="charge:ledger",
        )
        assigned = conn.execute(
            "SELECT ledger_sequence FROM wallet_transactions WHERE id = 'tx_ledger'"
        ).fetchone()
        assert assigned is not None and assigned[0] is not None
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute(
                "UPDATE wallet_transactions SET ledger_sequence = ledger_sequence + 1 "
                "WHERE id = 'tx_ledger'"
            )
        unchanged = conn.execute(
            "SELECT ledger_sequence FROM wallet_transactions WHERE id = 'tx_ledger'"
        ).fetchone()
    assert unchanged is not None and int(unchanged[0]) == int(assigned[0])


# ---------------------------------------------------------------------------
# Group B — the globally-unique billing keys hold under REAL multi-connection
# concurrency (DoD「真实 PG 多连接」+「约束」): two independent sessions race and
# exactly one commits. Complements CW-010's wallet-row-lock overspend guard
# (a CHECK/lock race) with the UNIQUE-index race.
# ---------------------------------------------------------------------------


def _race_two(
    dsn: str,
    attempt: Callable[[str], None],
) -> list[str]:
    """Run ``attempt(label)`` on two barrier-synchronised threads; return outcomes."""
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(label: str) -> None:
        barrier.wait()
        try:
            attempt(label)
            result = "inserted"
        except psycopg.errors.UniqueViolation:
            result = "duplicate"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(f"r{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_pg_concurrent_duplicate_merchant_order_no_commits_exactly_once(seeded: str) -> None:
    """Two connections race to create the same merchant order number.

    The column UNIQUE index must serialise them across independent sessions:
    exactly one row commits, the loser receives ``UniqueViolation`` — the
    duplicate-booking guarantee is not an artefact of a single connection.
    """

    def attempt(label: str) -> None:
        with psycopg.connect(seeded, autocommit=True) as conn:
            _insert_order(conn, order_id=f"race_{label}", merchant_order_no="CW059-RACE")

    outcomes = _race_two(seeded, attempt)
    assert sorted(outcomes) == ["duplicate", "inserted"], outcomes
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM recharge_orders WHERE merchant_order_no = %s",
            ("CW059-RACE",),
        )
        == 1
    )


def test_pg_concurrent_duplicate_charge_per_order_commits_exactly_once(seeded: str) -> None:
    """Two connections race to credit the same recharge order.

    Each uses a distinct idempotency key, so only the partial UNIQUE
    ``uq_wallet_transactions_charge_order`` can decide the winner: exactly one
    CHARGE lands on ``order_1`` (no double-booking), the loser is refused. The
    063 ledger trigger's ``wallets … FOR UPDATE`` row lock and the unique index
    together serialise the two independent sessions.
    """

    def attempt(label: str) -> None:
        with psycopg.connect(seeded, autocommit=True) as conn:
            _insert_tx(
                conn,
                tx_id=f"charge_race_{label}",
                tx_type="CHARGE",
                available_delta=10,
                reserved_delta=0,
                recharge_order_id="order_1",
                idempotency_key=f"charge:race:{label}",
            )

    outcomes = _race_two(seeded, attempt)
    assert sorted(outcomes) == ["duplicate", "inserted"], outcomes
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM wallet_transactions "
            "WHERE type = 'CHARGE' AND recharge_order_id = %s",
            ("order_1",),
        )
        == 1
    )
