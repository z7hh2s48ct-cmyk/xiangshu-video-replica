from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "008_provider_result_url"
down_revision = "007_local_storage_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Retain the provider result URL when archiving fails, so a Worker can
    re-download and re-archive the paid H3 result instead of losing it forever."""
    op.add_column(
        "generation_tasks",
        sa.Column("provider_result_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_column("provider_result_url")
