"""Allow operators to select an explicit Shanghai collection execution time."""

from alembic import op

revision = "20261001T2240_viral_collection_clock"
down_revision = "20261001T2115_viral_keyword_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE viral_runtime_controls ADD COLUMN collection_time TIME")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE viral_runtime_controls DROP COLUMN collection_time")
