"""记录来源统计是否覆盖批次全程；历史数据不猜测回填。"""

from alembic import op

revision = "20261002T0050_collection_stats_version"
down_revision = "20261002T0030_realtime_legacy_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    # 默认NULL覆盖旧在途批次，即使它续跑成功也不把缺来源记录误报为零。
    op.execute(
        "ALTER TABLE viral_collection_batches ADD COLUMN stats_version integer "
        "CHECK(stats_version IS NULL OR stats_version=1)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE viral_collection_batches DROP COLUMN stats_version")
