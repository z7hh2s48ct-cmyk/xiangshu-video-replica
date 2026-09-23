"""Persist paid viral-link resolution receipts across retries and restarts.

Revision ID: 080_viral_link_resolution_receipts
Revises: 079_viral_favorite_page_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "080_viral_link_resolution_receipts"
down_revision = "079_viral_favorite_page_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "viral_link_resolution_receipts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
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
            name="uq_viral_link_receipts_owner_idempotency",
        ),
        sa.CheckConstraint(
            "purpose IN ('copy', 'replica')",
            name="ck_viral_link_receipts_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('PREPARED', 'REQUEST_SENT', 'SUCCEEDED', 'FAILED_SAFE', 'UNCERTAIN')",
            name="ck_viral_link_receipts_status",
        ),
    )
    op.create_index(
        "idx_viral_link_receipts_owner_status",
        "viral_link_resolution_receipts",
        ["owner_user_id", "status", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_viral_link_receipts_owner_status",
        table_name="viral_link_resolution_receipts",
    )
    op.drop_table("viral_link_resolution_receipts")
