"""Persist actual keyword execution, including successful zero-result runs."""

from alembic import op

revision = "20261001T2115_viral_keyword_runs"
down_revision = "20261001T1840_viral_content_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE viral_keyword_runs (
        batch_id TEXT NOT NULL,platform TEXT NOT NULL,keyword TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED')),
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ,
        attempt_count INTEGER NOT NULL DEFAULT 1,video_count INTEGER,
        PRIMARY KEY(batch_id,platform,keyword))""")
    op.execute("CREATE INDEX viral_keyword_runs_time_idx ON viral_keyword_runs(started_at)")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE viral_keyword_runs")
