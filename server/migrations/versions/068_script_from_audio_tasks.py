"""C3/工作台音频提取文案：script-from-audio 异步任务。

Revision ID: 068_script_from_audio_tasks
Revises: 067_studio_drafts

Uploads arrive as raw reference videos without any analysis, so the
generation-gated script version table cannot hold their transcripts. The
task result JSON carries the ASR transcript instead; the copy workshop
consumes it and the explicit "确认终稿" publish path stays the only writer
of project script versions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "068_script_from_audio_tasks"
down_revision = "067_studio_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "script_from_audio_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_asset_id", sa.Text(), nullable=False),
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
        sa.Column("audio_object_key", sa.Text()),
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
            name="ck_script_from_audio_tasks_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_script_from_audio_tasks_attempt"),
        sa.CheckConstraint(
            "retryable IN (0, 1)",
            name="ck_script_from_audio_tasks_retryable",
        ),
    )
    op.create_index(
        "idx_script_from_audio_tasks_claim",
        "script_from_audio_tasks",
        ["status", "locked_until", "created_at", "id"],
    )
    op.create_index(
        "uq_script_from_audio_tasks_idempotency",
        "script_from_audio_tasks",
        ["project_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "idx_script_from_audio_tasks_project_created",
        "script_from_audio_tasks",
        ["project_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("script_from_audio_tasks")
