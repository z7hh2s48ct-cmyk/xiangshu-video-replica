"""Run source-frame extraction through the durable worker.

Revision ID: 047_async_source_frame_tasks
Revises: 046_async_image_tasks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "047_async_source_frame_tasks"
down_revision = "046_async_image_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_frame_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
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
        sa.Column(
            "result_version_id",
            sa.Text(),
            sa.ForeignKey("versions.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
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
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')",
            name="ck_source_frame_tasks_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_source_frame_tasks_attempt"),
        sa.CheckConstraint(
            "retryable IN (0, 1)",
            name="ck_source_frame_tasks_retryable",
        ),
    )
    op.create_index(
        "idx_source_frame_tasks_claim",
        "source_frame_tasks",
        ["status", "locked_until", "created_at", "id"],
    )
    op.create_index(
        "idx_source_frame_tasks_project_created",
        "source_frame_tasks",
        ["project_id", "created_at", "id"],
    )
    op.create_index(
        "idx_source_frame_tasks_result_version",
        "source_frame_tasks",
        ["result_version_id"],
    )
    op.create_index(
        "uq_source_frame_tasks_idempotency",
        "source_frame_tasks",
        ["project_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_source_frame_tasks_active_asset",
        "source_frame_tasks",
        ["project_id", "asset_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING','RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_source_frame_tasks_active_asset", table_name="source_frame_tasks")
    op.drop_index("uq_source_frame_tasks_idempotency", table_name="source_frame_tasks")
    op.drop_index("idx_source_frame_tasks_result_version", table_name="source_frame_tasks")
    op.drop_index("idx_source_frame_tasks_project_created", table_name="source_frame_tasks")
    op.drop_index("idx_source_frame_tasks_claim", table_name="source_frame_tasks")
    op.drop_table("source_frame_tasks")
