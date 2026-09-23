from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015_character_asset_publication"
down_revision = "014_character_image_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("character_versions") as batch_op:
        batch_op.add_column(sa.Column("publication_snapshot_json", sa.Text()))
        batch_op.add_column(sa.Column("publication_hash", sa.Text()))
        batch_op.create_check_constraint(
            "ck_character_versions_publication_pair",
            "(publication_snapshot_json IS NULL AND publication_hash IS NULL) "
            "OR (publication_snapshot_json IS NOT NULL AND publication_hash IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_character_versions_publication_hash",
            "publication_hash IS NULL OR length(publication_hash) = 64",
        )


def downgrade() -> None:
    with op.batch_alter_table("character_versions") as batch_op:
        batch_op.drop_constraint("ck_character_versions_publication_hash", type_="check")
        batch_op.drop_constraint("ck_character_versions_publication_pair", type_="check")
        batch_op.drop_column("publication_hash")
        batch_op.drop_column("publication_snapshot_json")
