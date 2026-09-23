from __future__ import annotations

from alembic import op

revision = "064_hifly_provider"
down_revision = "063_wallet_ledger_sequence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Widen the provider whitelist so admins can store the HiFly (飞影) API
    # token used by the digital-human oral-broadcast feature (C1).
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay', 'hifly')",
        )


def downgrade() -> None:
    op.execute("DELETE FROM provider_settings WHERE provider = 'hifly'")
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay')",
        )
