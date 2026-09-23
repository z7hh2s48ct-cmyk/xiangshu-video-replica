from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018_remove_oss_storage"
down_revision = "017_generation_task_retry_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    legacy_asset = connection.execute(
        sa.text("SELECT 1 FROM assets WHERE LOWER(storage_uri) LIKE 'oss://%' LIMIT 1")
    ).fetchone()
    if legacy_asset is not None:
        raise RuntimeError(
            "OSS-backed assets must be migrated to COS or local storage before removing OSS support"
        )

    # Purging the encrypted OSS row is intentional: keeping an unreachable
    # credential would contradict the product decision to remove this provider.
    op.execute(
        """
        UPDATE runtime_settings
        SET active_storage_provider = CASE
            WHEN EXISTS (
                SELECT 1 FROM provider_settings WHERE provider = 'cos'
            ) THEN 'cos'
            ELSE 'local'
        END
        WHERE active_storage_provider = 'oss'
        """
    )
    op.execute("DELETE FROM provider_settings WHERE provider = 'oss'")

    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.drop_constraint("ck_runtime_settings_active_storage_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_runtime_settings_active_storage_provider",
            "active_storage_provider IN ('cos', 'local')",
        )

    # The original provider constraint is unnamed. Adding a narrower named
    # constraint preserves migration history while preventing future OSS rows.
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos')",
        )


def downgrade() -> None:
    # Schema support can be restored, but deleted credentials cannot. Restore
    # from the pre-upgrade database backup before downgrading if OSS is needed.
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")

    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.drop_constraint("ck_runtime_settings_active_storage_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_runtime_settings_active_storage_provider",
            "active_storage_provider IN ('cos', 'oss', 'local')",
        )
