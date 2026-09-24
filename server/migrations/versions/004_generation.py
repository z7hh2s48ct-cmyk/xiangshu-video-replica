from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_generation"
down_revision = "003_characters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(sa.Column("prompt_version_id", sa.Text()))
        batch_op.add_column(sa.Column("prompt_snapshot_json", sa.Text()))
        batch_op.add_column(sa.Column("provider_request_json", sa.Text()))
        batch_op.add_column(sa.Column("result_url", sa.Text()))
        batch_op.create_foreign_key(
            "fk_generation_tasks_prompt_version_id_versions",
            "versions",
            ["prompt_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_generation_tasks_prompt_version",
        "generation_tasks",
        ["prompt_version_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_generation_tasks_prompt_version", table_name="generation_tasks")
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_column("result_url")
        batch_op.drop_column("provider_request_json")
        batch_op.drop_column("prompt_snapshot_json")
        batch_op.drop_column("prompt_version_id")
