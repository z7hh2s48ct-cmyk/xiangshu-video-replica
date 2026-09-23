"""Add durable oral-task lifecycle and wallet references.

Revision ID: 073_oral_durable_billing
Revises: 072_oral_clone_consent
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "073_oral_durable_billing"
down_revision = "072_oral_clone_consent"
branch_labels = None
depends_on = None


# 形状沿用 057_second_based_billing 的按秒放宽语义：
# RESERVE 为 -reserved_delta（秒数不再固定 1），SETTLE/RELEASE 同步放宽。
_WALLET_SHAPE_WITH_ORAL = """
(type = 'CHARGE' AND available_delta > 0 AND reserved_delta = 0
 AND recharge_order_id IS NOT NULL AND task_id IS NULL AND oral_task_id IS NULL
 AND billing_round IS NULL) OR
(type = 'RESERVE' AND available_delta = -reserved_delta AND reserved_delta >= 1
 AND recharge_order_id IS NULL AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL))
 AND billing_round IS NOT NULL) OR
(type = 'SETTLE' AND available_delta = 0 AND reserved_delta <= -1
 AND recharge_order_id IS NULL AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL))
 AND billing_round IS NOT NULL) OR
(type = 'RELEASE' AND available_delta = -reserved_delta AND available_delta >= 1
 AND reserved_delta <= -1
 AND recharge_order_id IS NULL AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL))
 AND billing_round IS NOT NULL)
"""

_WALLET_SHAPE_LEGACY = """
(type = 'CHARGE' AND available_delta > 0 AND reserved_delta = 0
 AND recharge_order_id IS NOT NULL AND task_id IS NULL AND billing_round IS NULL) OR
(type = 'RESERVE' AND available_delta = -reserved_delta AND reserved_delta >= 1
 AND recharge_order_id IS NULL AND task_id IS NOT NULL AND billing_round IS NOT NULL) OR
(type = 'SETTLE' AND available_delta = 0 AND reserved_delta <= -1
 AND recharge_order_id IS NULL AND task_id IS NOT NULL AND billing_round IS NOT NULL) OR
(type = 'RELEASE' AND available_delta = -reserved_delta AND available_delta >= 1
 AND reserved_delta <= -1
 AND recharge_order_id IS NULL AND task_id IS NOT NULL AND billing_round IS NOT NULL)
"""

# 063_wallet_ledger_sequence attached these triggers to wallet_transactions.
# SQLite's batch_alter_table rebuild drops them with the old table, so they
# must be restored after every rebuild in both directions.
_SQLITE_LEDGER_TRIGGERS = (
    (
        "trg_wallet_transactions_reject_explicit_ledger_sequence",
        """
        BEFORE INSERT ON wallet_transactions
        WHEN NEW.ledger_sequence IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'ledger_sequence is database assigned');
        END
        """,
    ),
    (
        "trg_wallet_transactions_reject_ledger_sequence_update",
        """
        BEFORE UPDATE OF ledger_sequence ON wallet_transactions
        WHEN OLD.ledger_sequence IS NOT NULL
             AND NEW.ledger_sequence IS NOT OLD.ledger_sequence
        BEGIN
            SELECT RAISE(ABORT, 'ledger_sequence is immutable');
        END
        """,
    ),
    (
        "trg_wallet_transactions_assign_ledger_sequence",
        """
        AFTER INSERT ON wallet_transactions
        WHEN NEW.ledger_sequence IS NULL
        BEGIN
            UPDATE wallet_transactions
            SET ledger_sequence = (
                SELECT COALESCE(MAX(ledger_sequence), 0) + 1
                FROM wallet_transactions
                WHERE id <> NEW.id
            )
            WHERE id = NEW.id;
        END
        """,
    ),
)


def _restore_sqlite_ledger_triggers() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for name, body in _SQLITE_LEDGER_TRIGGERS:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
        op.execute(sa.text(f"CREATE TRIGGER {name}{body}"))


def upgrade() -> None:
    bind = op.get_bind()
    active_legacy_tasks = bind.execute(
        sa.text("SELECT COUNT(*) FROM oral_tasks WHERE status IN ('QUEUED', 'RUNNING')")
    ).scalar_one()
    if int(active_legacy_tasks) > 0:
        raise RuntimeError(
            "cannot upgrade 065 while active legacy oral tasks have no wallet "
            "reservation; finish or cancel them before upgrading"
        )

    for table in ("oral_avatars", "oral_voices"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("lease_owner", sa.Text()))
            batch_op.add_column(sa.Column("lease_expires_at", sa.Text()))
            batch_op.add_column(sa.Column("next_attempt_at", sa.Text()))
            batch_op.add_column(
                sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
            )
            batch_op.drop_constraint(f"ck_{table}_submission_state", type_="check")
            batch_op.create_check_constraint(
                f"ck_{table}_submission_state",
                "submission_state IN ('LOCAL_PENDING', 'SUBMITTING', 'SUBMITTED', "
                "'SUBMISSION_UNKNOWN', 'FAILED')",
            )
            batch_op.create_check_constraint(f"ck_{table}_attempt_count", "attempt_count >= 0")
        op.create_index(
            f"idx_{table}_durable_claim",
            table,
            ["submission_state", "next_attempt_at", "lease_expires_at", "created_at"],
        )

    with op.batch_alter_table("wallet_transactions") as batch_op:
        batch_op.add_column(sa.Column("oral_task_id", sa.Text()))
        batch_op.create_foreign_key(
            "fk_wallet_transactions_oral_task_id",
            "oral_tasks",
            ["oral_task_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.drop_constraint("ck_wallet_transactions_shape", type_="check")
        batch_op.create_check_constraint("ck_wallet_transactions_shape", _WALLET_SHAPE_WITH_ORAL)
    _restore_sqlite_ledger_triggers()
    op.create_index(
        "uq_wallet_transactions_oral_reserve_round",
        "wallet_transactions",
        ["oral_task_id", "billing_round"],
        unique=True,
        sqlite_where=sa.text("type = 'RESERVE'"),
        postgresql_where=sa.text("type = 'RESERVE'"),
    )
    op.create_index(
        "uq_wallet_transactions_oral_terminal_round",
        "wallet_transactions",
        ["oral_task_id", "billing_round"],
        unique=True,
        sqlite_where=sa.text("type IN ('SETTLE', 'RELEASE')"),
        postgresql_where=sa.text("type IN ('SETTLE', 'RELEASE')"),
    )

    with op.batch_alter_table("oral_tasks") as batch_op:
        batch_op.add_column(sa.Column("billing_round", sa.Integer()))
        batch_op.add_column(
            sa.Column(
                "provider_charge_state",
                sa.Text(),
                nullable=False,
                server_default="NOT_SUBMITTED",
            )
        )
        batch_op.add_column(sa.Column("provider_result_url", sa.Text()))
        batch_op.add_column(sa.Column("lease_owner", sa.Text()))
        batch_op.add_column(sa.Column("lease_expires_at", sa.Text()))
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("queue_slot_acquired", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("next_attempt_at", sa.Text()))
        batch_op.drop_constraint("ck_oral_tasks_status", type_="check")
        batch_op.drop_constraint("ck_oral_tasks_submission_state", type_="check")
        batch_op.create_check_constraint(
            "ck_oral_tasks_status",
            "status IN ('QUEUED', 'SUBMITTING', 'SUBMISSION_UNCERTAIN', 'RUNNING', "
            "'ARCHIVING', 'ARCHIVE_FAILED', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
        )
        batch_op.create_check_constraint(
            "ck_oral_tasks_submission_state",
            "submission_state IN ('LOCAL_PENDING', 'SUBMITTING', 'SUBMITTED', "
            "'SUBMISSION_UNKNOWN', 'FAILED')",
        )
        batch_op.create_check_constraint("ck_oral_tasks_attempt_count", "attempt_count >= 0")
        batch_op.create_check_constraint(
            "ck_oral_tasks_queue_slot_acquired",
            "queue_slot_acquired IN (0, 1)",
        )
        batch_op.create_check_constraint(
            "ck_oral_tasks_billing_round",
            "billing_round IS NULL OR billing_round > 0",
        )
        batch_op.create_check_constraint(
            "ck_oral_tasks_provider_charge_state",
            "provider_charge_state IN ('NOT_SUBMITTED', 'NOT_CHARGED', 'CHARGED', 'UNKNOWN')",
        )
    op.execute(
        """
        UPDATE oral_tasks
        SET provider_charge_state = CASE
            WHEN status IN ('RUNNING', 'SUCCEEDED') THEN 'CHARGED'
            WHEN status = 'FAILED' THEN 'UNKNOWN'
            ELSE 'NOT_SUBMITTED'
        END
        """
    )
    op.create_index(
        "idx_oral_tasks_durable_claim",
        "oral_tasks",
        ["status", "next_attempt_at", "lease_expires_at", "created_at"],
    )
    op.create_table(
        "oral_billing_reconciliation_operations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "oral_task_id",
            sa.Text(),
            sa.ForeignKey("oral_tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("evidence_asset_id", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.Text(), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("applied_at", sa.Text()),
        sa.CheckConstraint(
            "resolution IN ('SETTLE', 'RELEASE')",
            name="ck_oral_billing_reconcile_resolution",
        ),
    )
    op.create_index(
        "idx_oral_billing_reconcile_task",
        "oral_billing_reconciliation_operations",
        ["oral_task_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    reconcile_rows = bind.execute(
        sa.text("SELECT COUNT(*) FROM oral_billing_reconciliation_operations")
    ).scalar_one()
    if int(reconcile_rows) > 0:
        raise RuntimeError(
            "cannot downgrade 065 while oral billing reconciliation audit records exist"
        )
    oral_rows = bind.execute(
        sa.text("SELECT COUNT(*) FROM wallet_transactions WHERE oral_task_id IS NOT NULL")
    ).scalar_one()
    if int(oral_rows) > 0:
        raise RuntimeError(
            "cannot downgrade 065 while oral wallet transactions exist; "
            "reconcile or export them first"
        )
    unsafe_tasks = bind.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM oral_tasks
            WHERE status NOT IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')
               OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL
               OR attempt_count <> 0 OR next_attempt_at IS NOT NULL
               OR queue_slot_acquired <> 0
               OR provider_result_url IS NOT NULL OR billing_round IS NOT NULL
            """
        )
    ).scalar_one()
    unsafe_clones = 0
    for table in ("oral_avatars", "oral_voices"):
        unsafe_clones += int(
            bind.execute(
                sa.text(
                    f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE submission_state = 'SUBMITTING'
                       OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL
                       OR attempt_count <> 0 OR next_attempt_at IS NOT NULL
                    """  # noqa: S608 - fixed migration table names
                )
            ).scalar_one()
        )
    if int(unsafe_tasks) > 0 or unsafe_clones > 0:
        raise RuntimeError("cannot downgrade 065 while durable oral worker recovery state exists")

    op.drop_index(
        "idx_oral_billing_reconcile_task",
        table_name="oral_billing_reconciliation_operations",
    )
    op.drop_table("oral_billing_reconciliation_operations")
    op.drop_index("idx_oral_tasks_durable_claim", table_name="oral_tasks")
    with op.batch_alter_table("oral_tasks") as batch_op:
        batch_op.drop_constraint("ck_oral_tasks_provider_charge_state", type_="check")
        batch_op.drop_constraint("ck_oral_tasks_billing_round", type_="check")
        batch_op.drop_constraint("ck_oral_tasks_queue_slot_acquired", type_="check")
        batch_op.drop_constraint("ck_oral_tasks_attempt_count", type_="check")
        batch_op.drop_constraint("ck_oral_tasks_submission_state", type_="check")
        batch_op.drop_constraint("ck_oral_tasks_status", type_="check")
        batch_op.create_check_constraint(
            "ck_oral_tasks_status",
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
        )
        batch_op.create_check_constraint(
            "ck_oral_tasks_submission_state",
            "submission_state IN ('LOCAL_PENDING', 'SUBMITTED', 'SUBMISSION_UNKNOWN', 'FAILED')",
        )
        batch_op.drop_column("next_attempt_at")
        batch_op.drop_column("queue_slot_acquired")
        batch_op.drop_column("attempt_count")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("provider_result_url")
        batch_op.drop_column("provider_charge_state")
        batch_op.drop_column("billing_round")

    op.drop_index("uq_wallet_transactions_oral_terminal_round", table_name="wallet_transactions")
    op.drop_index("uq_wallet_transactions_oral_reserve_round", table_name="wallet_transactions")
    with op.batch_alter_table("wallet_transactions") as batch_op:
        batch_op.drop_constraint("ck_wallet_transactions_shape", type_="check")
        batch_op.drop_constraint("fk_wallet_transactions_oral_task_id", type_="foreignkey")
        batch_op.drop_column("oral_task_id")
        batch_op.create_check_constraint("ck_wallet_transactions_shape", _WALLET_SHAPE_LEGACY)
    _restore_sqlite_ledger_triggers()

    for table in ("oral_voices", "oral_avatars"):
        op.drop_index(f"idx_{table}_durable_claim", table_name=table)
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(f"ck_{table}_attempt_count", type_="check")
            batch_op.drop_constraint(f"ck_{table}_submission_state", type_="check")
            batch_op.create_check_constraint(
                f"ck_{table}_submission_state",
                "submission_state IN ('LOCAL_PENDING', 'SUBMITTED', "
                "'SUBMISSION_UNKNOWN', 'FAILED')",
            )
            batch_op.drop_column("attempt_count")
            batch_op.drop_column("next_attempt_at")
            batch_op.drop_column("lease_expires_at")
            batch_op.drop_column("lease_owner")
