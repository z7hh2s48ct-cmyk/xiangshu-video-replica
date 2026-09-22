"""Fail-first PostgreSQL tests for the sub-account database foundation.

Covers the two frozen migrations that lay the 母账号 / 子账号 groundwork:

* ``20260919T1200_sub_accounts`` — ``users.parent_user_id`` (self-FK,
  ON DELETE CASCADE) + ``users.account_type`` (MASTER/SUB/SUB_ADMIN) with three
  shape CHECKs and a fail-closed downgrade guard.
* ``20260919T1300_wallet_actor`` — ``wallet_transactions.actor_user_id``
  (FK→users.id, ON DELETE SET NULL) + the existing-row backfill
  (``actor_user_id = user_id``).

Every invariant proven here is enforced by PostgreSQL constraints, never by
application code, matching the dev-doc rule that single-row shape lives in the
database while cross-row rules (parent must be MASTER, hierarchy depth = 1) are
deferred to the Phase 2 service layer. Mirrors ``test_activation_code_schema.py``:
raw admin CREATE/DROP of a per-test scratch database, module gate via
``require_pg_or_explicit_skip``.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

USERS_TABLE = "users"
WALLET_TX_TABLE = "wallet_transactions"

# 重挂后链尾：#188 recharge_packages 之后是 Phase 3a sub_account_quotas、
# Phase 3b sub_account_permissions，再叠加 BILLING-OBS-20260922 三个 analysis 迁移。
_HEAD_REVISION = "20260923T1200_admin_refund_adjustment"
# 1200 adds the sub-account columns; 1300 adds the wallet actor column;
# 1500 + the 20260921T0000 merge revision sit on top of 1300, with the
# BILLING-OBS revisions (failure diagnostic, request id, attempt history)
# trailing the merge.
# Downgrading below 1200 must fail-closed while sub-accounts still exist.
_PRE_ACTOR_REVISION = "20260919T1200_sub_accounts"
_PRIOR_REVISION = "20260919T1000_browser_account_probe"
# 20260920T0000_merge_parallel_heads 之后 browser_account_probe 与
# oral_soft_delete 是同一深度的并行叶子，降级到其一版本表仍保留两行。
_PRIOR_SIBLING_REVISION = "20260919T1000_oral_soft_delete"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip(_pg_dsn())


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _drop_database(db_name: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def _create_database(db_name: str) -> str:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db_name}"')
    return _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"


def _alembic_config(dsn: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    return config


def _upgrade(dsn: str, target: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(dsn), target)


def _downgrade(dsn: str, target: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(dsn), target)


@pytest.fixture()
def head_dsn() -> str:
    """Fresh database at head with one MASTER account ('m1') seeded.

    'm1' is a valid parent for sub-account inserts; each test owns the database
    and drops it on teardown, so tests never share rows.
    """
    dsn = _create_database("t_sub_account_schema")
    _upgrade(dsn, "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES ('m1', 'm1', 'Master 1')"
        )
    try:
        yield dsn
    finally:
        _drop_database("t_sub_account_schema")


@pytest.fixture()
def pre_actor_dsn() -> str:
    """Fresh database upgraded only to 1200 (before wallet_transactions.actor_user_id)."""
    dsn = _create_database("t_sub_account_pre_actor")
    _upgrade(dsn, _PRE_ACTOR_REVISION)
    try:
        yield dsn
    finally:
        _drop_database("t_sub_account_pre_actor")


def _insert_sub(
    conn: psycopg.Connection, sub_id: str, parent_id: str, account_type: str = "SUB"
) -> None:
    conn.execute(
        "INSERT INTO users (id, username, display_name, parent_user_id, account_type) "
        "VALUES (%s, %s, %s, %s, %s)",
        (sub_id, sub_id, sub_id, parent_id, account_type),
    )


def _column(conn: psycopg.Connection, table: str, name: str):  # type: ignore[no-untyped-def]
    """(data_type, is_nullable, column_default) for one column, or None if absent."""
    return conn.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, name),
    ).fetchone()


def _seed_billing_chain(conn: psycopg.Connection, *, owner_id: str, seq: str) -> str:
    """Seed projects→generation_batches→generation_tasks→wallets for ``owner_id``.

    ``wallet_transactions.task_id`` is FK→``generation_tasks.id`` (022), so a
    transaction row needs this chain to exist first. Returns the task id.
    """
    project_id, batch_id, task_id = f"p-{seq}", f"b-{seq}", f"t-{seq}"
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        (project_id, owner_id, project_id),
    )
    conn.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json) "
        "VALUES (%s, %s, %s, %s, %s, '{}')",
        (batch_id, project_id, owner_id, f"k-{seq}", f"h-{seq}"),
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, provider, model) "
        "VALUES (%s, %s, 'apilio', 'h3')",
        (task_id, batch_id),
    )
    conn.execute(
        "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, 9, 1)",
        (owner_id,),
    )
    return task_id


# ---------------------------------------------------------------------------
# Shape: the three new columns with the frozen types / nullability
# ---------------------------------------------------------------------------


def test_sub_account_columns_and_types(head_dsn: str) -> None:
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _HEAD_REVISION

        parent = _column(conn, USERS_TABLE, "parent_user_id")
        assert parent is not None, "users.parent_user_id missing at head"
        assert parent[0] == "text"
        assert parent[1] == "YES"  # nullable: NULL == 母账号 (top-level)

        account_type = _column(conn, USERS_TABLE, "account_type")
        assert account_type is not None, "users.account_type missing at head"
        assert account_type[0] == "text"
        assert account_type[1] == "NO"  # NOT NULL
        assert account_type[2] is not None and "MASTER" in account_type[2]

        actor = _column(conn, WALLET_TX_TABLE, "actor_user_id")
        assert actor is not None, "wallet_transactions.actor_user_id missing at head"
        assert actor[0] == "text"
        assert actor[1] == "YES"  # nullable: NULL == actor was a deleted sub-account


# ---------------------------------------------------------------------------
# 20260919T1200: account-shape CHECKs + parent self-FK
# ---------------------------------------------------------------------------


def test_account_shape_constraints_enforced(head_dsn: str) -> None:
    """Legal account shapes insert; the three CHECKs + parent FK reject the rest."""
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        # Legal: MASTER by default (parent NULL), SUB and SUB_ADMIN under m1.
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES ('m2', 'm2', 'Master 2')"
        )
        _insert_sub(conn, "s1", "m1", "SUB")
        _insert_sub(conn, "s2", "m1", "SUB_ADMIN")
        assert (
            conn.execute("SELECT count(*) FROM users WHERE parent_user_id = 'm1'").fetchone()[0]
            == 2
        )

        # ck_users_account_parent_shape: a SUB requires a parent.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO users (id, username, display_name, account_type) "
                "VALUES ('bad1', 'bad1', 'B', 'SUB')"
            )
        # ck_users_account_parent_shape: MASTER (default) forbids a parent.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO users (id, username, display_name, parent_user_id) "
                "VALUES ('bad2', 'bad2', 'B', 'm1')"
            )
        # ck_users_account_type: enum only.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO users (id, username, display_name, parent_user_id, account_type) "
                "VALUES ('bad3', 'bad3', 'B', 'm1', 'NOPE')"
            )
        # ck_users_no_self_parent: a row cannot be its own parent.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO users (id, username, display_name, parent_user_id, account_type) "
                "VALUES ('bad4', 'bad4', 'B', 'bad4', 'SUB')"
            )
        # parent_user_id FK: must reference an existing user.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO users (id, username, display_name, parent_user_id, account_type) "
                "VALUES ('bad5', 'bad5', 'B', 'ghost', 'SUB')"
            )


def test_master_delete_cascades_to_sub_accounts(head_dsn: str) -> None:
    """ON DELETE CASCADE: deleting a 母账号 removes its sub-accounts."""
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        _insert_sub(conn, "s1", "m1", "SUB")
        _insert_sub(conn, "s2", "m1", "SUB_ADMIN")
        conn.execute("DELETE FROM users WHERE id = 'm1'")
        remaining = conn.execute(
            "SELECT id FROM users WHERE id IN ('m1', 's1', 's2') ORDER BY id"
        ).fetchall()
        assert remaining == [], f"master delete must cascade to sub-accounts, got {remaining}"


# ---------------------------------------------------------------------------
# 20260919T1300: wallet actor FK / SET NULL / backfill
# ---------------------------------------------------------------------------


def test_actor_delete_sets_null_and_preserves_history(head_dsn: str) -> None:
    """ON DELETE SET NULL: removing the acting sub keeps the 母账号 transaction."""
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        _insert_sub(conn, "s1", "m1", "SUB")
        task_id = _seed_billing_chain(conn, owner_id="m1", seq="an")

        # actor_user_id FK→users.id: a nonexistent actor is rejected.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
                "reserved_delta, task_id, billing_round, idempotency_key, actor_user_id) "
                "VALUES ('tx0', 'm1', 'RESERVE', -1, 1, %s, 1, 'idem-an0', 'ghost')",
                (task_id,),
            )

        # The sub-account 's1' acts on the master 'm1' wallet.
        conn.execute(
            "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
            "reserved_delta, task_id, billing_round, idempotency_key, actor_user_id) "
            "VALUES ('tx1', 'm1', 'RESERVE', -1, 1, %s, 1, 'idem-an1', 's1')",
            (task_id,),
        )
        conn.execute("DELETE FROM users WHERE id = 's1'")

        row = conn.execute(
            "SELECT user_id, actor_user_id FROM wallet_transactions WHERE id = 'tx1'"
        ).fetchone()
        assert row is not None, "the transaction must survive the actor's deletion"
        assert row[0] == "m1", "wallet ownership (母账号) is unchanged"
        assert row[1] is None, "actor_user_id is SET NULL, not cascaded away"


def test_actor_backfill_on_staged_upgrade(pre_actor_dsn: str) -> None:
    """Existing wallet rows get actor_user_id = user_id when 1300 applies.

    A fresh-DB upgrade backfills zero rows and proves nothing, so we stage:
    seed a transaction at 1200 (before the column exists), then upgrade to head
    and assert the pre-existing row was backfilled and no NULL actor remains.
    """
    with psycopg.connect(pre_actor_dsn, autocommit=True) as conn:
        assert _column(conn, WALLET_TX_TABLE, "actor_user_id") is None, (
            "actor column must be absent at 1200"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES ('owner1', 'owner1', 'O1')"
        )
        task_id = _seed_billing_chain(conn, owner_id="owner1", seq="bf")
        # RESERVE shape (057 ck_wallet_transactions_shape): available -1 /
        # reserved +1 / task_id + billing_round set / recharge_order_id NULL.
        conn.execute(
            "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
            "reserved_delta, task_id, billing_round, idempotency_key) "
            "VALUES ('tx1', 'owner1', 'RESERVE', -1, 1, %s, 1, 'idem-bf')",
            (task_id,),
        )

    _upgrade(pre_actor_dsn, "head")

    with psycopg.connect(pre_actor_dsn) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _HEAD_REVISION
        user_id, actor = conn.execute(
            "SELECT user_id, actor_user_id FROM wallet_transactions WHERE id = 'tx1'"
        ).fetchone()
        assert actor == user_id == "owner1", (
            f"backfill must set actor_user_id = user_id, got user_id={user_id!r} actor={actor!r}"
        )
        nulls = conn.execute(
            "SELECT count(*) FROM wallet_transactions WHERE actor_user_id IS NULL"
        ).fetchone()[0]
        assert nulls == 0, f"no pre-existing row may keep a NULL actor, got {nulls}"


# ---------------------------------------------------------------------------
# 20260919T1200: fail-closed downgrade guard
# ---------------------------------------------------------------------------


def test_downgrade_guard_refuses_while_sub_accounts_exist(head_dsn: str) -> None:
    """Sub-accounts present -> RuntimeError and the whole chain rolls back to head."""
    with psycopg.connect(head_dsn, autocommit=True) as conn:
        _insert_sub(conn, "s1", "m1", "SUB")

    with pytest.raises(RuntimeError, match="sub-accounts exist"):
        _downgrade(head_dsn, _PRIOR_REVISION)

    with psycopg.connect(head_dsn, autocommit=True) as conn:
        # The downgrade runs in one transaction, so the guard rolls it all back.
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _HEAD_REVISION, "failed downgrade must leave the chain at head"
        assert _column(conn, USERS_TABLE, "parent_user_id") is not None

        # Remove the sub-accounts; the same downgrade now succeeds.
        conn.execute("DELETE FROM users WHERE parent_user_id IS NOT NULL")

    _downgrade(head_dsn, _PRIOR_REVISION)

    with psycopg.connect(head_dsn) as conn:
        versions = {row[0] for row in conn.execute("SELECT version_num FROM alembic_version")}
        assert versions == {_PRIOR_REVISION, _PRIOR_SIBLING_REVISION}
        assert _column(conn, USERS_TABLE, "parent_user_id") is None
        assert _column(conn, USERS_TABLE, "account_type") is None
        assert _column(conn, WALLET_TX_TABLE, "actor_user_id") is None
