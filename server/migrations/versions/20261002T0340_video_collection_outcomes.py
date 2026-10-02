"""Preserve per-video results on each independent collection record."""

from alembic import op

revision = "20261002T0340_video_collection_outcomes"
down_revision = "20261002T0220_adjustment_balance_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE viral_collection_batches ADD COLUMN video_outcomes_json "
        "text NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(video_outcomes_json::jsonb)='object')"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE viral_collection_batches DROP COLUMN video_outcomes_json")
