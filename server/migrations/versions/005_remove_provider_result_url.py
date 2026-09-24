from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005_remove_provider_result_url"
down_revision = "004_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_column("result_url")


def downgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(sa.Column("result_url", sa.Text()))
