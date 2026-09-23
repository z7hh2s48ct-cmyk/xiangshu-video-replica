from __future__ import annotations

from alembic import op

revision = "023_zpay_provider"
down_revision = "022_internal_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay')",
        )


def downgrade() -> None:
    op.execute("DELETE FROM provider_settings WHERE provider = 'zpay'")
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos', 'deepseek')",
        )
