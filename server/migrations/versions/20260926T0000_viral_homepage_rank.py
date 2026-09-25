"""爆款首页策展排序：viral_videos 增加 homepage_rank。

「置顶」= 取当前已展示视频的最小 rank-1，「取消置顶/取消首页展示」=
置 NULL，「展示到首页」= 追加当前最大 rank+1。客户端 featured 列表按
``homepage_rank ASC NULLS LAST`` 后接既有默认序，运营可控制置顶顺序而
不必一次重排全部内容；列可空、无回填，downgrade 直接删列即回滚。

已知取舍：分页游标的 data_version 不含 rank 变更，置顶操作瞬间旧游标
可能重复/漏过一行，重新翻页即恢复；featured 池为运营精选的小集合，
不值得为此引入额外的版本指纹。

Revision ID: 20260926T0000_viral_homepage_rank
Revises: 20260924T0200_customer_oral_task_visibility
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260926T0000_viral_homepage_rank"
down_revision = "20260924T0200_customer_oral_task_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "viral_videos",
        sa.Column("homepage_rank", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("viral_videos", "homepage_rank")
