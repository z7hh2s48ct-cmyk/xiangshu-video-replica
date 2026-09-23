from __future__ import annotations

from alembic import op

revision = "007_local_storage_provider"
down_revision = "006_active_storage_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Allow `local` storage provider so a machine without cloud credentials
    (e.g. macOS dev box) can run the full local flow."""
    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.drop_constraint("ck_runtime_settings_active_storage_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_runtime_settings_active_storage_provider",
            "active_storage_provider IN ('cos', 'oss', 'local')",
        )


def downgrade() -> None:
    # SQLite batch_alter_table rebuilds the table with the narrowed CHECK, so any
    # row still holding 'local' would fail the downgrade. Reset them to 'cos' first.
    op.execute(
        "UPDATE runtime_settings SET active_storage_provider = 'cos' "
        "WHERE active_storage_provider = 'local'"
    )
    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.drop_constraint("ck_runtime_settings_active_storage_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_runtime_settings_active_storage_provider",
            "active_storage_provider IN ('cos', 'oss')",
        )
