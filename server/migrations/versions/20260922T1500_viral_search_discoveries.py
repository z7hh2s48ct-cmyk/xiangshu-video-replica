"""爆款视频搜索发现记录表（P1：搜索驱动 + 本地缓存）.

Revision ID: 20260922T1500_viral_search_discoveries
Revises: 20260923T0000_open_h3_extended_modes

「用户每天搜索到了哪些视频」是运营侧每日汇总与客户侧"我的发现"的共同事实来源：

- 一行 = 一个客户在某搜索词下命中的一条视频的"发现事实"。
- ``UNIQUE(user_id, keyword, platform, video_id, search_date)``：同一天同一词
  重复命中同一视频 → upsert 刷新 ``searched_at``（幂等重放不产生重复行）。
- ``search_date`` 为上海时区 YYYY-MM-DD 文本（"每天"维度取自业务口径，见
  ``app/admin_dates.SHANGHAI``）；``searched_at`` 存 UTC ISO 时间戳。
- 不建 FK：删用户不回收发现记录（运营侧历史汇总保留；与 viral_videos 主表的
  "无 FK 全文本列"风格一致）。
- downgrade 有行时 fail-closed：删表会使全部发现记录静默消失。

Runtime is PG-only（与链上相邻迁移同款守卫）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260922T1500_viral_search_discoveries"
down_revision = "20260923T0000_open_h3_extended_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "viral_search_discoveries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("search_date", sa.Text(), nullable=False),
        sa.Column("searched_at", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "keyword",
            "platform",
            "video_id",
            "search_date",
            name="uq_viral_search_discoveries_identity",
        ),
    )
    op.create_index(
        "ix_viral_search_discoveries_user_date",
        "viral_search_discoveries",
        ["user_id", "search_date"],
    )
    op.create_index(
        "ix_viral_search_discoveries_date",
        "viral_search_discoveries",
        ["search_date"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    row_count = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM viral_search_discoveries")).scalar()
    )
    if row_count:
        raise RuntimeError(
            "cannot downgrade: viral search discoveries exist; dropping the "
            "table would silently remove every discovery record "
            "(delete the rows first if that is really intended)"
        )
    op.drop_index("ix_viral_search_discoveries_date", table_name="viral_search_discoveries")
    op.drop_index("ix_viral_search_discoveries_user_date", table_name="viral_search_discoveries")
    op.drop_table("viral_search_discoveries")
