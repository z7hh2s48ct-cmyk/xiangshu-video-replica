"""内容采集来源与实际客户读取从本迁移开始记录，不回填猜测历史。"""

from alembic import op

revision = "20261001T1840_viral_content_observations"
down_revision = "20261001T1800_viral_homepage_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE viral_content_measurement_state (
        id INTEGER PRIMARY KEY CHECK(id=1),started_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    op.execute("INSERT INTO viral_content_measurement_state(id) VALUES(1)")
    op.execute("""CREATE TABLE viral_content_sources (
        batch_id TEXT NOT NULL,source_kind TEXT NOT NULL,platform TEXT NOT NULL,
        video_id TEXT NOT NULL,keyword TEXT NOT NULL,
        collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        prepared_at TIMESTAMPTZ,homepage_at TIMESTAMPTZ,
        detail_at TIMESTAMPTZ,copy_at TIMESTAMPTZ,
        PRIMARY KEY(batch_id,platform,video_id,keyword))""")
    op.execute(
        "CREATE INDEX viral_content_sources_video_idx ON viral_content_sources(platform,video_id)"
    )
    op.execute(
        "CREATE INDEX viral_content_sources_cohort_idx ON viral_content_sources(collected_at)"
    )
    op.execute("""CREATE TABLE viral_content_usage_events (
        id TEXT PRIMARY KEY,platform TEXT NOT NULL,video_id TEXT NOT NULL,user_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('detail','copy')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    op.execute(
        "CREATE INDEX viral_content_usage_video_idx ON "
        "viral_content_usage_events(platform,video_id,created_at)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE viral_content_usage_events")
    op.execute("DROP TABLE viral_content_sources")
    op.execute("DROP TABLE viral_content_measurement_state")
