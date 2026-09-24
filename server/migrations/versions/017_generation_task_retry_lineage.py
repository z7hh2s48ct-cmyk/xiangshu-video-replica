from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "017_generation_task_retry_lineage"
down_revision = "016_character_reference_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("generation_batches", sa.Column("source_batch_id", sa.Text()))
    op.add_column("generation_batches", sa.Column("source_task_id", sa.Text()))
    op.add_column("generation_batches", sa.Column("generation_reason", sa.Text()))
    op.create_index(
        "idx_generation_batches_source_batch",
        "generation_batches",
        ["source_batch_id"],
    )
    op.create_index(
        "idx_generation_batches_source_task",
        "generation_batches",
        ["source_task_id"],
    )

    for column in (
        "retry_of_task_id",
        "superseded_by_task_id",
        "superseded_at",
        "retry_reason",
        "retry_requested_by_user_id",
        "retry_requested_at",
        "billing_confirmation_status",
        "billing_confirmed_by_user_id",
        "billing_confirmed_at",
        "billing_confirmation_reason",
    ):
        op.add_column("generation_tasks", sa.Column(column, sa.Text()))

    op.create_index(
        "idx_generation_tasks_retry_of",
        "generation_tasks",
        ["retry_of_task_id"],
    )
    op.create_index(
        "idx_generation_tasks_superseded_by",
        "generation_tasks",
        ["superseded_by_task_id"],
    )
    op.create_index(
        "idx_generation_tasks_retry_requested_by",
        "generation_tasks",
        ["retry_requested_by_user_id"],
    )
    op.create_index(
        "idx_generation_tasks_billing_confirmed_by",
        "generation_tasks",
        ["billing_confirmed_by_user_id"],
    )
    op.create_index(
        "idx_generation_tasks_active_attention",
        "generation_tasks",
        ["batch_id", "superseded_by_task_id", "status", "archive_status", "quality_status"],
    )

    op.create_table(
        "generation_task_operations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("generation_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("result_task_id", sa.Text()),
        sa.Column("result_status", sa.Text(), nullable=False),
        sa.Column("response_snapshot_json", sa.Text()),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "actor_user_id",
            "task_id",
            "action",
            "idempotency_key",
            name="uq_generation_task_operations_idempotency",
        ),
    )
    op.create_index(
        "idx_generation_task_operations_task_action",
        "generation_task_operations",
        ["task_id", "action"],
    )
    op.create_index(
        "uq_generation_task_operations_pending",
        "generation_task_operations",
        ["task_id", "action"],
        unique=True,
        sqlite_where=sa.text("result_status = 'PENDING'"),
        postgresql_where=sa.text("result_status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_generation_task_operations_pending",
        table_name="generation_task_operations",
    )
    op.drop_index(
        "idx_generation_task_operations_task_action",
        table_name="generation_task_operations",
    )
    op.drop_table("generation_task_operations")

    op.drop_index("idx_generation_tasks_active_attention", table_name="generation_tasks")
    op.drop_index(
        "idx_generation_tasks_billing_confirmed_by",
        table_name="generation_tasks",
    )
    op.drop_index(
        "idx_generation_tasks_retry_requested_by",
        table_name="generation_tasks",
    )
    op.drop_index("idx_generation_tasks_superseded_by", table_name="generation_tasks")
    op.drop_index("idx_generation_tasks_retry_of", table_name="generation_tasks")
    for column in (
        "billing_confirmation_reason",
        "billing_confirmed_at",
        "billing_confirmed_by_user_id",
        "billing_confirmation_status",
        "retry_requested_at",
        "retry_requested_by_user_id",
        "retry_reason",
        "superseded_at",
        "superseded_by_task_id",
        "retry_of_task_id",
    ):
        op.drop_column("generation_tasks", column)

    op.drop_index("idx_generation_batches_source_task", table_name="generation_batches")
    op.drop_index("idx_generation_batches_source_batch", table_name="generation_batches")
    op.drop_column("generation_batches", "generation_reason")
    op.drop_column("generation_batches", "source_task_id")
    op.drop_column("generation_batches", "source_batch_id")
