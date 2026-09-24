from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006_active_storage_provider"
down_revision = "005_remove_provider_result_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "active_storage_provider",
                sa.Text(),
                nullable=False,
                server_default="cos",
            )
        )
        batch_op.create_check_constraint(
            "ck_runtime_settings_active_storage_provider",
            "active_storage_provider IN ('cos', 'oss')",
        )


def downgrade() -> None:
    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.drop_constraint("ck_runtime_settings_active_storage_provider", type_="check")
        batch_op.drop_column("active_storage_provider")
