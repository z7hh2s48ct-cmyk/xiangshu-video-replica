from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016_character_reference_snapshot"
down_revision = "015_character_asset_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("character_reference_selections") as batch_op:
        batch_op.add_column(
            sa.Column(
                "character_version_snapshot_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("character_reference_selections") as batch_op:
        batch_op.drop_column("character_version_snapshot_json")
