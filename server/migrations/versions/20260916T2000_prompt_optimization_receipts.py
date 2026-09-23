"""Durable H3 prompt tasks and frozen analysis context (unpublished migration).

Revision ID: 20260916T2000_prompt_optimization_receipts
Revises: 20260916T1400_content_objects
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260916T2000_prompt_optimization_receipts"
down_revision = "20260916T1400_content_objects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_optimization_receipts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("provider_started_at", sa.Text()),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=False),
        sa.Column("lease_expires_at", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text()),
        sa.Column("error_status", sa.Integer()),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column(
            "created_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("completed_at", sa.Text()),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_prompt_optimization_receipts_owner_idempotency",
        ),
        sa.CheckConstraint(
            "mode IN ('T2VA', 'I2VA', 'FL2VA', 'L2VA', 'Ref2VA')",
            name="ck_prompt_optimization_receipts_mode",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'NEEDS_INPUT', "
            "'FAILED', 'SUBMISSION_UNCERTAIN')",
            name="ck_prompt_optimization_receipts_status",
        ),
    )
    op.add_column("analysis_tasks", sa.Column("generation_context_json", sa.Text(), nullable=True))
    op.create_index(
        "idx_prompt_optimization_receipts_owner_status",
        "prompt_optimization_receipts",
        ["owner_user_id", "status", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_column("analysis_tasks", "generation_context_json")
    op.drop_index(
        "idx_prompt_optimization_receipts_owner_status",
        table_name="prompt_optimization_receipts",
    )
    op.drop_table("prompt_optimization_receipts")
