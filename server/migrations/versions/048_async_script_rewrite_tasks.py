"""Keep AI script rewrites alive after page navigation.

Revision ID: 048_async_script_rewrite_tasks
Revises: 047_async_source_frame_tasks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "048_async_script_rewrite_tasks"
down_revision = "047_async_source_frame_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "script_rewrite_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
        sa.Column("provider_started_at", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column("retryable", sa.Integer(), nullable=False, server_default="0"),
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
        sa.Column("started_at", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','SUBMISSION_UNCERTAIN')",
            name="ck_script_rewrite_tasks_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_script_rewrite_tasks_attempt"),
        sa.CheckConstraint(
            "retryable IN (0, 1)",
            name="ck_script_rewrite_tasks_retryable",
        ),
    )
    op.create_index(
        "idx_script_rewrite_tasks_claim",
        "script_rewrite_tasks",
        ["status", "locked_until", "created_at", "id"],
    )
    op.create_index(
        "idx_script_rewrite_tasks_project_created",
        "script_rewrite_tasks",
        ["project_id", "created_at", "id"],
    )
    op.create_index(
        "uq_script_rewrite_tasks_idempotency",
        "script_rewrite_tasks",
        ["project_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_script_rewrite_tasks_active_project",
        "script_rewrite_tasks",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING','RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_script_rewrite_tasks_active_project",
        table_name="script_rewrite_tasks",
    )
    op.drop_index(
        "uq_script_rewrite_tasks_idempotency",
        table_name="script_rewrite_tasks",
    )
    op.drop_index(
        "idx_script_rewrite_tasks_project_created",
        table_name="script_rewrite_tasks",
    )
    op.drop_index(
        "idx_script_rewrite_tasks_claim",
        table_name="script_rewrite_tasks",
    )
    op.drop_table("script_rewrite_tasks")
