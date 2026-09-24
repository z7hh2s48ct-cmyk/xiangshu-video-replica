"""059_operation_cost_records — freeze rates and record actual provider usage.

The rate table is mutable configuration.  This migration adds immutable rate
snapshots to generation tasks and an append-only operation ledger for video,
analysis, and image provider calls.  Missing usage remains explicitly UNKNOWN;
it is never replaced by the requested amount.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "059_operation_cost_records"
down_revision = "058_daily_external_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column("cost_rate_subject_snapshot", sa.Text(), nullable=True),
    )
    op.add_column(
        "generation_tasks",
        sa.Column("cost_unit_price_fen_snapshot", sa.Integer(), nullable=True),
    )
    op.add_column(
        "generation_tasks",
        sa.Column("external_unit_price_fen_snapshot", sa.Integer(), nullable=True),
    )
    op.add_column(
        "generation_tasks",
        sa.Column("actual_output_seconds", sa.Numeric(12, 3), nullable=True),
    )
    op.add_column(
        "generation_tasks",
        sa.Column("cost_status", sa.Text(), nullable=True),
    )
    if op.get_bind().dialect.name != "postgresql":
        return

    op.create_table(
        "operation_cost_records",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("generation_task_id", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("usage_amount", sa.Numeric(14, 3), nullable=True),
        sa.Column("unit_price_fen", sa.Integer(), nullable=False),
        sa.Column("cost_fen", sa.Numeric(16, 3), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["generation_task_id"], ["generation_tasks.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_type", "source_id", "subject", name="uq_operation_cost_source_subject"
        ),
        sa.CheckConstraint("unit IN ('second', 'image', 'call')", name="ck_operation_cost_unit"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACTUAL', 'UNKNOWN')", name="ck_operation_cost_status"
        ),
        sa.CheckConstraint("unit_price_fen >= 0", name="ck_operation_cost_rate_non_negative"),
        sa.CheckConstraint(
            "usage_amount IS NULL OR usage_amount >= 0",
            name="ck_operation_cost_usage_non_negative",
        ),
        sa.CheckConstraint(
            "cost_fen IS NULL OR cost_fen >= 0", name="ck_operation_cost_value_non_negative"
        ),
    )
    op.create_index(
        "idx_operation_cost_records_occurred", "operation_cost_records", ["occurred_at"]
    )
    op.create_index("idx_operation_cost_records_subject", "operation_cost_records", ["subject"])
    op.create_index(
        "idx_operation_cost_records_generation_task",
        "operation_cost_records",
        ["generation_task_id"],
    )
    op.create_index(
        "idx_wallet_transactions_type_created_at",
        "wallet_transactions",
        ["type", "created_at"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("idx_wallet_transactions_type_created_at", table_name="wallet_transactions")
        op.drop_index(
            "idx_operation_cost_records_generation_task", table_name="operation_cost_records"
        )
        op.drop_index("idx_operation_cost_records_subject", table_name="operation_cost_records")
        op.drop_index("idx_operation_cost_records_occurred", table_name="operation_cost_records")
        op.drop_table("operation_cost_records")
    op.drop_column("generation_tasks", "cost_status")
    op.drop_column("generation_tasks", "actual_output_seconds")
    op.drop_column("generation_tasks", "external_unit_price_fen_snapshot")
    op.drop_column("generation_tasks", "cost_unit_price_fen_snapshot")
    op.drop_column("generation_tasks", "cost_rate_subject_snapshot")
