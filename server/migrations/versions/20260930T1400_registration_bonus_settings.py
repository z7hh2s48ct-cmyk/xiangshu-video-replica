"""注册赠送积分设置：管理端可配置新注册主账号的一次性赠送积分数。

- ``registration_bonus_settings``：单行（id=1）配置表——注册成功时自动发放的
  积分数，``0`` 表示关闭赠送。用单行表而不是塞进 ``runtime_settings``：注册
  赠送是 PG-only 的客户域资金策略，与双 lane 共享的运行参数分开，读写都能
  一眼看全（与 ``alert_settings`` 同一模式）。
- 只影响此后新注册的主账号；子账号由主账号自建且没有独立钱包，天然不在
  发放范围内，已注册账号不补发——这些口径由注册路径的调用点保证，表里
  不需要额外字段。

PG-only（业务库唯一真源）。downgrade 删表：配置本身可重建，发放出去的
流水与订单是账本事实，由各自的表承载，不受影响。

Revision ID: 20260930T1400_registration_bonus_settings
Revises: 20260930T1100_alert_notify_dedup
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260930T1400_registration_bonus_settings"
down_revision = "20260930T1100_alert_notify_dedup"
branch_labels = None
depends_on = None

_REGISTRATION_BONUS_SETTINGS = "registration_bonus_settings"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        _REGISTRATION_BONUS_SETTINGS,
        sa.Column("id", sa.Integer(), primary_key=True),
        # 默认 0 = 关闭：升级瞬间注册行为不变，管理员在设置页改了才发放，
        # 与 alert_settings「默认值保持既有口径」同一原则。
        sa.Column(
            "bonus_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint("id = 1", name="ck_registration_bonus_settings_singleton"),
        # 上界对齐 wallets.available_credits 的 int4：发放端还有一层 WHERE
        # 护栏防溢出，这里先把不可能落库的值挡在写入前。
        sa.CheckConstraint(
            "bonus_credits >= 0 AND bonus_credits <= 2147483647",
            name="ck_registration_bonus_settings_credits_range",
        ),
    )
    op.execute(f"INSERT INTO {_REGISTRATION_BONUS_SETTINGS} (id) VALUES (1)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table(_REGISTRATION_BONUS_SETTINGS)
