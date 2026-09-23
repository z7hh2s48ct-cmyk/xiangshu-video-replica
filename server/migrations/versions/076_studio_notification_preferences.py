"""076_studio_notification_preferences — C10b 用户通知偏好存储.

个人中心的「通知偏好」开关此前只弹 toast（接口未接通）。本表按用户存
一份 JSON 偏好：当前唯一键 enabled，未来扩展新偏好键不需要再改表。未
写入过的用户不落行，按服务端默认（开启）返回。

Revision ID: 076_studio_notification_preferences
Revises: 075_independent_creation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "076_studio_notification_preferences"
down_revision = "075_independent_creation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_notification_preferences",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prefs_json", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("studio_notification_preferences")
