"""Keep execution outcomes independently from the reusable refresh queue."""

from alembic import op

revision = "20261001T2300_viral_collection_records"
down_revision = "20261001T2240_viral_collection_clock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""ALTER TABLE viral_collection_batches
      ADD COLUMN run_status text CHECK(run_status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
      ADD COLUMN started_at timestamptz,
      ADD COLUMN completed_at timestamptz,
      ADD COLUMN failed_video_count integer CHECK(failed_video_count>=0),
      ADD COLUMN failure_reason text;
      ALTER TABLE viral_content_sources ADD COLUMN is_new boolean;""")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""ALTER TABLE viral_content_sources DROP COLUMN is_new;
      ALTER TABLE viral_collection_batches DROP COLUMN failure_reason,
      DROP COLUMN failed_video_count,DROP COLUMN completed_at,DROP COLUMN started_at,
      DROP COLUMN run_status;""")
