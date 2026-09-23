from __future__ import annotations

from alembic import op

revision = "020_deepseek_provider"
down_revision = "019_character_simple_upload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Widen the provider whitelist so admins can store the DeepSeek API key
    # used by the "AI 改写" (二创口播稿) feature.
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos', 'deepseek')",
        )


def downgrade() -> None:
    op.execute("DELETE FROM provider_settings WHERE provider = 'deepseek'")
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos')",
        )
