"""Sub-account hierarchy on the ``users`` table (母账号 / 子账号体系 MVP).

Revision ID: 20260919T1200_sub_accounts
Revises: 20260919T1000_browser_account_probe

激活码方案废弃后，客户身份改由注册登录生成的 ``users`` 行 + session token 承载，
钱包（``wallets``）以 ``user_id`` 为主键。本迁移为「母账号下挂子账号、子账号消费
统一走母账号钱包」的体系打地基：

- ``users.parent_user_id``：自引用外键，NULL 表示母账号（顶层），非 NULL 表示挂在
  某个母账号下的子账号。ON DELETE CASCADE——母账号注销时其子账号一并删除（子账号
  脱离母账号无独立钱包/无意义，见 audit Rev.2 风险清单「母账号注销级联」）。
- ``users.account_type``：MASTER（母账号）/ SUB（子账号）/ SUB_ADMIN（子账号管理员，
  可代母账号管理其他子账号）。存量行默认 MASTER，与 parent_user_id IS NULL 自洽，
  无需数据回填。

约束（形状 CHECK）保证 account_type 与 parent_user_id 一致，并禁止自引用为父。
「父必须是 MASTER」「层级深度 = 1（不允许子账号再挂子账号）」是跨行不变量，
无法用单行 CHECK 表达，由应用层（Phase 2 的子账号 CRUD 服务）强制。

Runtime is PG-only（与 20260919T1000 同款守卫）；离线归档工具仍遍历本链。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260919T1200_sub_accounts"
down_revision = "20260919T1000_browser_account_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        ALTER TABLE users
            ADD COLUMN parent_user_id text
                REFERENCES users(id) ON DELETE CASCADE,
            ADD COLUMN account_type text NOT NULL DEFAULT 'MASTER';
        ALTER TABLE users
            ADD CONSTRAINT ck_users_account_type
                CHECK (account_type IN ('MASTER', 'SUB', 'SUB_ADMIN'));
        ALTER TABLE users
            ADD CONSTRAINT ck_users_account_parent_shape
                CHECK (
                    (account_type = 'MASTER' AND parent_user_id IS NULL)
                    OR (account_type IN ('SUB', 'SUB_ADMIN') AND parent_user_id IS NOT NULL)
                );
        ALTER TABLE users
            ADD CONSTRAINT ck_users_no_self_parent
                CHECK (parent_user_id IS NULL OR parent_user_id <> id);
        CREATE INDEX idx_users_parent_user
            ON users (parent_user_id)
            WHERE parent_user_id IS NOT NULL;
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 拒绝在已产生子账号时回退：删除 parent_user_id 会静默丢失母子归属关系，
    # 与仓库既有防御性 downgrade（如 20260912T2200）一致——有业务数据即 fail-closed。
    sub_count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM users WHERE parent_user_id IS NOT NULL"))
        .scalar()
    )
    if sub_count:
        raise RuntimeError(
            "cannot downgrade: sub-accounts exist; re-parent or delete them first "
            "(dropping parent_user_id would silently orphan the hierarchy)"
        )
    op.execute("""
        DROP INDEX IF EXISTS idx_users_parent_user;
        ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_no_self_parent;
        ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_account_parent_shape;
        ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_account_type;
        ALTER TABLE users DROP COLUMN account_type;
        ALTER TABLE users DROP COLUMN parent_user_id;
    """)
