"""子账号月度额度配置表（CUSTOMER-CENTER-V2 Phase 3a / audit-40 Phase 3 前半）.

Revision ID: 20260921T1200_sub_account_quotas
Revises: 20260922T1200_recharge_packages

母账号给指定子账号设置「每月最多消耗多少积分」：

- 行存在 = 该子账号受限；无行 = 不限——与「存量子账号默认不限」的现状兼容，
  不做任何回填即保持原行为。清除限额 = 删除行（PUT quota null）。
- ``monthly_credits`` 以积分计（与钱包/流水同单位，1 元 = points_per_yuan 积分）。
- 当月已用量不落库，按 ``wallet_transactions``（actor_user_id + 当月 +
  RESERVE/RELEASE 白名单）实时聚合，见 ``app/sub_account_quota.py``：额度是
  「限制」不是「账本」，账本唯一来源仍是钱包流水，避免双写漂移。
- ``ON DELETE CASCADE``：子账号被真删（无历史）时额度行随行清理；有历史而降级
  停用的子账号额度行保留（停用期间不消费，重新启用后限额继续有效）。
- 「额度行只挂在子账号上」是应用层不变量：行级作用域由
  ``customer_sub_account_routes._lock_owned_sub`` 强制（与
  ``ck_users_account_parent_shape`` 的「层级 ≤1 由应用层强制」同款边界）。
- downgrade 有额度行时 fail-closed：删表 = 全部机构内控限额静默消失（子账号
  可无限制消费），与 20260919T1200/T1500 的防御式回退一致。

Runtime is PG-only（与链上相邻迁移同款守卫）；离线归档工具仍遍历本链。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260921T1200_sub_account_quotas"
# 手册 §3：未合并分支重挂——#187 合并 main 时 recharge_packages（#188）已在
# main 链尾之上，本迁移重挂到它之后以维持 alembic 单头（重挂后已重跑 --record）。
down_revision = "20260922T1200_recharge_packages"
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
        "sub_account_quotas",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("monthly_credits", sa.BigInteger(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "monthly_credits >= 0",
            name="ck_sub_account_quotas_monthly_credits",
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 有一个额度行就拒绝：删表会使全部机构内控限额静默消失（fail-closed）。
    quota_count = op.get_bind().execute(sa.text("SELECT count(*) FROM sub_account_quotas")).scalar()
    if quota_count:
        raise RuntimeError(
            "cannot downgrade: sub-account quotas exist; dropping the limit "
            "table would silently remove every organisation's spend cap "
            "(delete the quota rows first if that is really intended)"
        )
    op.drop_table("sub_account_quotas")
