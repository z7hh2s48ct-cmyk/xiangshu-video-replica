"""C4 爆款视频参考库：搜索结果落库复用（按 platform+video_id 去重）.

Revision ID: 070_viral_video_library
Revises: 069_provider_whitelist_widen

两张表：
- ``viral_videos``：两个平台的爆款视频条目库。(platform, video_id) 唯一，
  跨分类/跨排序去重；内容以 upsert 方式刷新互动数据，未再出现的条目保留
  在库中作为长期参考。``tags_json``/``native_json`` 按仓库惯例以 JSON 文本
  双方言存储；``cover_key`` 指向主存储里落地的长期封面副本。
- ``viral_fetch_state``：记录 (platform, sort) 最近一次上游拉取时间，
  作为计费护栏的回源判据（TTL 内只读库，不请求上游）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "070_viral_video_library"
down_revision = "069_provider_whitelist_widen"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column[str]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "viral_videos",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False, server_default=""),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False, server_default=""),
        sa.Column("author_avatar", sa.Text()),
        sa.Column("verified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cover_url", sa.Text()),
        sa.Column("cover_key", sa.Text()),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("likes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments", sa.Integer()),
        sa.Column("shares", sa.Integer()),
        sa.Column("collects", sa.Integer()),
        sa.Column("published_at", sa.Integer()),
        sa.Column("published_display", sa.Text()),
        sa.Column("like_display", sa.Text()),
        sa.Column("tags_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("play_url", sa.Text()),
        sa.Column("audio_url", sa.Text()),
        sa.Column("native_json", sa.Text(), nullable=False, server_default="{}"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("platform", "video_id"),
        sa.CheckConstraint("verified IN (0, 1)", name="ck_viral_videos_verified"),
    )
    op.create_index(
        "ix_viral_videos_platform_likes",
        "viral_videos",
        ["platform", "likes"],
    )
    op.create_index(
        "ix_viral_videos_platform_published",
        "viral_videos",
        ["platform", "published_at"],
    )
    op.create_table(
        "viral_fetch_state",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("sort", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("platform", "sort"),
    )


def downgrade() -> None:
    op.drop_table("viral_fetch_state")
    op.drop_index("ix_viral_videos_platform_published", "viral_videos")
    op.drop_index("ix_viral_videos_platform_likes", "viral_videos")
    op.drop_table("viral_videos")
