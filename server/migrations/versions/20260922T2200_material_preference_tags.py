"""MATERIAL-UX-05 标签底座：素材偏好表增加标签列.

Revision ID: 20260922T2200_material_preference_tags
Revises: 20260922T1500_viral_search_discoveries

``studio_material_preferences`` 新增 ``tags_json``（JSONB，NOT NULL，默认 '[]'）：

- 标签与 title/group_override 同层：用户侧展示偏好，不改变资产真源，也不授予访问权限。
- 列类型走 TEXT-JSON（仓库约定默认）：SQLite 历史迁移 lane 必须能执行本迁移并保持
  与 PG 的 schema 逐列一致（sqlite_to_postgres reconcile 按列比对 fail-closed）；
  包含过滤与聚合在查询侧按需 ``::jsonb`` 转型，操作符能力不受影响。
- 默认 '[]' 使存量偏好行零回填即满足 NOT NULL；新行由应用层写入规整后的数组。
- downgrade 直接删列：标签是用户侧增强数据，回退丢弃可接受（与列语义对称）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260922T2200_material_preference_tags"
down_revision = "20260922T1500_viral_search_discoveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "studio_material_preferences",
        sa.Column("tags_json", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_column("studio_material_preferences", "tags_json")
