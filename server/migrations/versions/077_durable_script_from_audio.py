"""Keep ASR receipts, retry times and project-level task exclusion durable."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "077_durable_script_from_audio"
down_revision = "076_studio_notification_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("script_from_audio_tasks") as batch:
        batch.add_column(sa.Column("provider_task_id", sa.Text()))
        batch.add_column(sa.Column("next_attempt_at", sa.Text()))
    # Preserve every legacy row; ambiguous duplicates must never be resubmitted.
    op.execute(
        """
        UPDATE script_from_audio_tasks
        SET status='SUBMISSION_UNCERTAIN', locked_by=NULL, locked_until=NULL,
            retryable=0, error_code='SCRIPT_FROM_AUDIO_LEGACY_DUPLICATE',
            error_message_redacted='存在重复提取任务，请先核查已有任务结果。',
            completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY project_id
                    ORDER BY CASE WHEN provider_started_at IS NOT NULL THEN 0 ELSE 1 END,
                             created_at, id
                ) AS position
                FROM script_from_audio_tasks WHERE status IN ('PENDING','RUNNING')
            ) AS duplicates WHERE position > 1
        )
        """
    )
    op.create_index(
        "uq_script_from_audio_tasks_active_project",
        "script_from_audio_tasks",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING','RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )


def downgrade() -> None:
    # Operational safety decisions stay terminal when the schema rolls back.
    op.drop_index("uq_script_from_audio_tasks_active_project", table_name="script_from_audio_tasks")
    with op.batch_alter_table("script_from_audio_tasks") as batch:
        batch.drop_column("next_attempt_at")
        batch.drop_column("provider_task_id")
