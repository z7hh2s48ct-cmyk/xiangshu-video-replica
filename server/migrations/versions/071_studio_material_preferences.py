"""C9 素材库：为现有资产与直出任务保存用户侧名称和隐藏偏好。

Revision ID: 071_studio_material_preferences
Revises: 070_viral_video_library

物理文件仍以 assets 为唯一真源，H3 DIRECT 结果仍以 generation_tasks 为
真源。本表只保存用户侧展示偏好，不授予访问权限，也不删除原业务记录。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "071_studio_material_preferences"
down_revision = "070_viral_video_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_material_preferences",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("title_override", sa.Text()),
        sa.Column("group_override", sa.Text()),
        sa.Column("hidden", sa.Integer(), nullable=False, server_default="0"),
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
        sa.PrimaryKeyConstraint("user_id", "source_type", "source_id"),
        sa.CheckConstraint(
            "source_type IN ('asset', 'generation')",
            name="ck_studio_material_preferences_source_type",
        ),
        sa.CheckConstraint(
            "hidden IN (0, 1)",
            name="ck_studio_material_preferences_hidden",
        ),
    )
    op.create_index(
        "idx_assets_creator_created",
        "assets",
        ["created_by_user_id", "created_at", "id"],
    )
    op.create_index(
        "idx_assets_project_created",
        "assets",
        ["project_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_assets_project_created", table_name="assets")
    op.drop_index("idx_assets_creator_created", table_name="assets")
    op.drop_table("studio_material_preferences")
