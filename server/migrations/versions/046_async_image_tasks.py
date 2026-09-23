"""Persist first-frame and simple-character image generation work.

Revision ID: 046_async_image_tasks
Revises: 045_async_analysis_tasks

The API stores an immutable request fingerprint and returns immediately.  Slow
provider calls are claimed by the generation worker, and an expired RUNNING
lease is quarantined instead of being submitted a second time automatically.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "046_async_image_tasks"
down_revision = "045_async_analysis_tasks"
branch_labels = None
depends_on = None


def _common_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("created_by_user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column("retryable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("started_at", sa.Text()),
        sa.Column("completed_at", sa.Text()),
    ]


def _common_constraints(prefix: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','SUBMISSION_UNCERTAIN')",
            name=f"ck_{prefix}_status",
        ),
        sa.CheckConstraint("attempt >= 0", name=f"ck_{prefix}_attempt"),
        sa.CheckConstraint("retryable IN (0, 1)", name=f"ck_{prefix}_retryable"),
    ]


def upgrade() -> None:
    op.create_table(
        "first_frame_tasks",
        *_common_columns(),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "result_version_id",
            sa.Text(),
            sa.ForeignKey("versions.id", ondelete="SET NULL"),
        ),
        *_common_constraints("first_frame_tasks"),
    )
    op.create_index(
        "idx_first_frame_tasks_claim",
        "first_frame_tasks",
        ["status", "locked_until", "created_at", "id"],
    )
    op.create_index(
        "idx_first_frame_tasks_project_created",
        "first_frame_tasks",
        ["project_id", "created_at", "id"],
    )
    op.create_index(
        "idx_first_frame_tasks_created_by",
        "first_frame_tasks",
        ["created_by_user_id"],
    )
    op.create_index(
        "idx_first_frame_tasks_result_version",
        "first_frame_tasks",
        ["result_version_id"],
    )
    op.create_index(
        "uq_first_frame_tasks_idempotency",
        "first_frame_tasks",
        ["project_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_first_frame_tasks_active_request",
        "first_frame_tasks",
        ["project_id", "request_hash"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING','RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )

    op.create_table(
        "character_sheet_tasks",
        *_common_columns(),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "identity_id",
            sa.Text(),
            sa.ForeignKey("person_identities.id", ondelete="CASCADE"),
        ),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("source_storage_uri", sa.Text(), nullable=False),
        sa.Column("source_content_type", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("source_size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "result_identity_id",
            sa.Text(),
            sa.ForeignKey("person_identities.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "result_version_id",
            sa.Text(),
            sa.ForeignKey("character_versions.id", ondelete="SET NULL"),
        ),
        *_common_constraints("character_sheet_tasks"),
        sa.CheckConstraint(
            "operation IN ('CREATE','REGENERATE')",
            name="ck_character_sheet_tasks_operation",
        ),
        sa.CheckConstraint("source_size_bytes > 0", name="ck_character_sheet_tasks_size"),
    )
    op.create_index(
        "idx_character_sheet_tasks_claim",
        "character_sheet_tasks",
        ["status", "locked_until", "created_at", "id"],
    )
    op.create_index(
        "idx_character_sheet_tasks_owner_created",
        "character_sheet_tasks",
        ["created_by_user_id", "created_at", "id"],
    )
    op.create_index(
        "idx_character_sheet_tasks_project",
        "character_sheet_tasks",
        ["project_id"],
    )
    op.create_index(
        "idx_character_sheet_tasks_identity",
        "character_sheet_tasks",
        ["identity_id"],
    )
    op.create_index(
        "idx_character_sheet_tasks_result_identity",
        "character_sheet_tasks",
        ["result_identity_id"],
    )
    op.create_index(
        "idx_character_sheet_tasks_result_version",
        "character_sheet_tasks",
        ["result_version_id"],
    )
    op.create_index(
        "uq_character_sheet_tasks_idempotency",
        "character_sheet_tasks",
        ["created_by_user_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_character_sheet_tasks_active_request",
        "character_sheet_tasks",
        ["created_by_user_id", "request_hash"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING','RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_character_sheet_tasks_active_request", table_name="character_sheet_tasks")
    op.drop_index("uq_character_sheet_tasks_idempotency", table_name="character_sheet_tasks")
    op.drop_index("idx_character_sheet_tasks_result_version", table_name="character_sheet_tasks")
    op.drop_index("idx_character_sheet_tasks_result_identity", table_name="character_sheet_tasks")
    op.drop_index("idx_character_sheet_tasks_identity", table_name="character_sheet_tasks")
    op.drop_index("idx_character_sheet_tasks_project", table_name="character_sheet_tasks")
    op.drop_index("idx_character_sheet_tasks_owner_created", table_name="character_sheet_tasks")
    op.drop_index("idx_character_sheet_tasks_claim", table_name="character_sheet_tasks")
    op.drop_table("character_sheet_tasks")
    op.drop_index("uq_first_frame_tasks_active_request", table_name="first_frame_tasks")
    op.drop_index("uq_first_frame_tasks_idempotency", table_name="first_frame_tasks")
    op.drop_index("idx_first_frame_tasks_result_version", table_name="first_frame_tasks")
    op.drop_index("idx_first_frame_tasks_created_by", table_name="first_frame_tasks")
    op.drop_index("idx_first_frame_tasks_project_created", table_name="first_frame_tasks")
    op.drop_index("idx_first_frame_tasks_claim", table_name="first_frame_tasks")
    op.drop_table("first_frame_tasks")
