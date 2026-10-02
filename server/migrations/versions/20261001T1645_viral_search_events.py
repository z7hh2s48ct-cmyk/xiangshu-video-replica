"""记录真实成功搜索事件，包含零结果；历史命中记录不伪造回填。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261001T1645_viral_search_events"
down_revision = "20261001T1600_external_call_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "viral_search_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("search_date", sa.Text(), nullable=False),
        sa.Column("searched_at", sa.Text(), nullable=False),
        sa.Column("video_ids_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_viral_search_events_date", "viral_search_events", ["search_date"])
    op.create_table(
        "viral_search_metrics_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("INSERT INTO viral_search_metrics_state VALUES(1, clock_timestamp())")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("viral_search_metrics_state")
    op.drop_table("viral_search_events")
