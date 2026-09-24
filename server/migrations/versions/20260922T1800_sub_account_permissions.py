"""子账号功能权限配置表（CUSTOMER-CENTER-V2 Phase 3b / audit-40 权限矩阵）.

Revision ID: 20260922T1800_sub_account_permissions
Revises: 20260921T1200_sub_account_quotas

母账号给指定子账号限定「12 类业务 + 2 项系统权限」的可使用范围：

- 行存在 = 该子账号受限；无行 = 全允许——与「存量子账号默认不受限」的现状兼容，
  不做任何回填即保持原行为；恢复全允许 = 删除行（PUT permissions）。
- ``allowed_businesses`` 存业务键数组的 TEXT-JSON（维持仓库约定：JSON 全存 TEXT，
  仅当需要 PG 操作符/索引时用 JSONB，见 089 与 cw056 §617；本列只做整列读取）。
- ``allow_api_keys`` / ``allow_publish_accounts``：Token 创建与发布账号（导入/扫码）开关。
  「设为管理员」（users.account_type = SUB_ADMIN）是 users 列的状态，不在本表——
  权限行只表达「业务/系统开关」，角色表达「管理能力」。
- ``ON DELETE CASCADE``：子账号被真删（无历史）时权限行随行清理；有历史而降级停用
  的子账号权限行保留（停用期间不消费，重新启用后权限继续有效）。
- 「权限行只挂在子账号上」是应用层不变量（同 ``sub_account_quotas`` 的边界）；
  母账号行无意义——读侧以 ``users.parent_user_id IS NULL`` 折叠为全允许。
- downgrade 有权限行时 fail-closed：删表 = 全部机构内控权限静默消失
  （受限子账号恢复全允许），与 20260921T1200 的防御式回退一致。

Runtime is PG-only（与链上相邻迁移同款守卫）；离线归档工具仍遍历本链。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260922T1800_sub_account_permissions"
down_revision = "20260921T1200_sub_account_quotas"
branch_labels = None
depends_on = None


def _created_at() -> sa.Column[str]:
    return sa.Column(
        "created_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _updated_at() -> sa.Column[str]:
    return sa.Column(
        "updated_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "sub_account_permissions",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("allowed_businesses", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("allow_api_keys", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "allow_publish_accounts",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        _created_at(),
        _updated_at(),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 有一个权限行就拒绝：删表会使全部机构内控权限静默消失（fail-closed）。
    permission_count = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM sub_account_permissions")).scalar()
    )
    if permission_count:
        raise RuntimeError(
            "cannot downgrade: sub-account permission rows exist; dropping the "
            "permission table would silently restore unrestricted access for "
            "every restricted sub-account (delete the permission rows first if "
            "that is really intended)"
        )
    op.drop_table("sub_account_permissions")
