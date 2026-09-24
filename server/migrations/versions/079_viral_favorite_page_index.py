"""让收藏分页索引匹配稳定排序方向。

Revision ID: 079_viral_favorite_page_index
Revises: 078_viral_module_launch
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "079_viral_favorite_page_index"
down_revision = "078_viral_module_launch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "idx_viral_video_favorites_user_created",
        table_name="viral_video_favorites",
    )
    op.create_index(
        "idx_viral_video_favorites_user_page",
        "viral_video_favorites",
        ["user_id", sa.text("created_at DESC"), "platform", "video_id"],
    )
    op.create_index(
        "idx_viral_video_favorites_user_platform_page",
        "viral_video_favorites",
        ["user_id", "platform", sa.text("created_at DESC"), "video_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_viral_video_favorites_user_platform_page",
        table_name="viral_video_favorites",
    )
    op.drop_index(
        "idx_viral_video_favorites_user_page",
        table_name="viral_video_favorites",
    )
    op.create_index(
        "idx_viral_video_favorites_user_created",
        "viral_video_favorites",
        ["user_id", "created_at", "platform", "video_id"],
    )
