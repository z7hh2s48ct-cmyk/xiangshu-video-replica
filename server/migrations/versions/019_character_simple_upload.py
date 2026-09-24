from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019_character_simple_upload"
down_revision = "018_remove_oss_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A named CHECK constraint is deliberately avoided: SQLite batch
    # downgrade cannot drop named constraints, and any later batch rebuild
    # of this table would fail while the constraint still referenced the
    # removed column. Allowed values are enforced by the application layer.
    with op.batch_alter_table("character_versions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "generation_mode",
                sa.Text(),
                nullable=True,
            )
        )
    op.execute("UPDATE character_versions SET generation_mode = 'traditional'")


def downgrade() -> None:
    with op.batch_alter_table("character_versions") as batch_op:
        batch_op.drop_column("generation_mode")
