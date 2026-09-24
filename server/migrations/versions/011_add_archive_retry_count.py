from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011_add_archive_retry_count"
down_revision = "010_add_asset_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Cap archive-retry attempts so a permanently expired provider URL does not
    keep the paid task spinning every 60s forever."""
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "archive_retry_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_column("archive_retry_count")
