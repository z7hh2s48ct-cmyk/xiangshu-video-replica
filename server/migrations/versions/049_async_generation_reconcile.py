"""Move H3 reconciliation into the durable worker.

Revision ID: 049_async_generation_reconcile
Revises: 048_async_script_rewrite_tasks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "049_async_generation_reconcile"
down_revision = "048_async_script_rewrite_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_task_operations",
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "generation_task_operations",
        sa.Column("locked_by", sa.Text()),
    )
    op.add_column(
        "generation_task_operations",
        sa.Column("locked_until", sa.Text()),
    )
    op.add_column(
        "generation_task_operations",
        sa.Column("started_at", sa.Text()),
    )
    op.add_column(
        "generation_task_operations",
        sa.Column("completed_at", sa.Text()),
    )
    op.add_column(
        "generation_task_operations",
        sa.Column("error_code", sa.Text()),
    )
    op.add_column(
        "generation_task_operations",
        sa.Column("error_message_redacted", sa.Text()),
    )
    op.add_column(
        "generation_task_operations",
        sa.Column("retryable", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "idx_generation_task_operations_reconcile_claim",
        "generation_task_operations",
        ["action", "result_status", "locked_until", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_generation_task_operations_reconcile_claim",
        table_name="generation_task_operations",
    )
    for column in (
        "retryable",
        "error_message_redacted",
        "error_code",
        "completed_at",
        "started_at",
        "locked_until",
        "locked_by",
        "attempt",
    ):
        op.drop_column("generation_task_operations", column)
