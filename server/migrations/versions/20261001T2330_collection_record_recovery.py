"""Snapshot related queue identity and fence interrupted realtime search records."""

from alembic import op

revision = "20261001T2330_collection_record_recovery"
down_revision = "20261001T2300_viral_collection_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""ALTER TABLE viral_collection_batches
      ADD COLUMN failure_code text,
      ADD COLUMN related_task_id text,
      ADD COLUMN lease_expires_at timestamptz;
      UPDATE viral_collection_batches SET lease_expires_at=started_at+interval '10 minutes'
      WHERE run_status='RUNNING' AND started_at IS NOT NULL
        AND config_json::jsonb->>'trigger_kind'='realtime';
      ALTER TABLE viral_keyword_runs ADD COLUMN failure_code text;
      CREATE TABLE viral_platform_probes(
        platform text PRIMARY KEY CHECK(platform IN ('douyin','wechat_channels')),
        state text NOT NULL CHECK(state IN ('available','unavailable')),
        checked_at timestamptz NOT NULL,
        failure_code text);""")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""DROP TABLE viral_platform_probes;
      ALTER TABLE viral_keyword_runs DROP COLUMN failure_code;
      ALTER TABLE viral_collection_batches DROP COLUMN lease_expires_at,
      DROP COLUMN related_task_id,DROP COLUMN failure_code;""")
