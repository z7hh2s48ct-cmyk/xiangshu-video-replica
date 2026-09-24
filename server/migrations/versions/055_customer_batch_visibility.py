"""Hide a customer's list entry without deleting generation or billing history."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "055_customer_batch_visibility"
down_revision = "054_admin_free_grant_adjustments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_batch_visibility",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column(
            "hidden_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.PrimaryKeyConstraint("user_id", "batch_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["generation_batches.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    # Removing preferences restores visibility; all generation and ledger rows survive.
    op.drop_table("customer_batch_visibility")
