from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010_add_asset_metadata"
down_revision = "009_idempotency_project_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist asset-level metadata (e.g. ffprobe duration) so analysis and
    other flows can verify client-supplied values against the measured ones."""
    with op.batch_alter_table("assets") as batch_op:
        batch_op.add_column(
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("assets") as batch_op:
        batch_op.drop_column("metadata_json")
