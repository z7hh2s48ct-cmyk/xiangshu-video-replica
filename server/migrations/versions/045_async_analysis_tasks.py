"""Run reference-video analysis through the persistent worker queue.

Revision ID: 045_async_analysis_tasks
Revises: 044_customer_unit_prices

The API transaction only validates and enqueues work.  Provider I/O happens in
the worker, outside customer-session fencing transactions, and failures remain
queryable so the desktop can offer an explicit retry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "045_async_analysis_tasks"
down_revision = "044_customer_unit_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_tasks",
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
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
        sa.Column(
            "result_version_id",
            sa.Text(),
            sa.ForeignKey("versions.id", ondelete="SET NULL"),
        ),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column("failure_phase", sa.Text()),
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
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_analysis_tasks_status",
        ),
        sa.CheckConstraint("duration_seconds > 0", name="ck_analysis_tasks_duration"),
        sa.CheckConstraint("attempt >= 0", name="ck_analysis_tasks_attempt"),
        sa.CheckConstraint("retryable IN (0, 1)", name="ck_analysis_tasks_retryable"),
    )
    op.create_index(
        "idx_analysis_tasks_claim",
        "analysis_tasks",
        ["status", "locked_until", "created_at", "id"],
    )
    op.create_index(
        "idx_analysis_tasks_project_asset_created",
        "analysis_tasks",
        ["project_id", "asset_id", "created_at", "id"],
    )
    op.create_index(
        "uq_analysis_tasks_active_asset",
        "analysis_tasks",
        ["project_id", "asset_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING', 'RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_analysis_tasks_active_asset", table_name="analysis_tasks")
    op.drop_index("idx_analysis_tasks_project_asset_created", table_name="analysis_tasks")
    op.drop_index("idx_analysis_tasks_claim", table_name="analysis_tasks")
    op.drop_table("analysis_tasks")
