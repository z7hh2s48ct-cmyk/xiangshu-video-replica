"""持久化首页排期及发布意图版本，旧后台任务不得覆盖新运营决定。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261001T1800_viral_homepage_schedule"
down_revision = "20261001T1645_viral_search_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    for name in ("homepage_starts_at", "homepage_ends_at", "homepage_featured_at"):
        op.add_column("viral_videos", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column(
        "viral_videos",
        sa.Column("homepage_intent_version", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    for name in (
        "homepage_intent_version",
        "homepage_featured_at",
        "homepage_ends_at",
        "homepage_starts_at",
    ):
        op.drop_column("viral_videos", name)
