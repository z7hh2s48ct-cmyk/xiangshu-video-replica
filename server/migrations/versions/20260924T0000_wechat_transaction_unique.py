"""wechat_native 交易号的数据库唯一兜底（部分唯一索引）。

背景：``confirm_recharge_payment`` 对「交易号已绑定其他订单」只有应用层的
先查后写预检查（``zpay_payments`` 的 ``bound_order`` 查询），该检查在并发下
会失效——READ COMMITTED 里两个事务各自 ``FOR UPDATE`` 锁住自己的订单行，
互相看不见对方未提交的交易号，随后两条 UPDATE 落在不同行互不冲突，
同一个 ``transaction_id`` 可以同时绑到两张订单、入账两份资金。

zpay 泳道自 022 起就有数据库兜底（``uq_recharge_orders_provider_trade_no``
部分唯一索引），而 083 给 wechat_native 引入的 ``transaction_id`` 列只有
NULL/NOT NULL 耦合的 CHECK，没有唯一索引——本迁移补齐同一不变量。

谓词形状照抄 022：083 的 ``ck_recharge_orders_wechat_transaction_id`` 保证
非 NULL 的 ``transaction_id`` 只存在于已结算（PAID）的 wechat_native 行上，
所以「对非 NULL 建唯一」恰好覆盖全部已绑定行，且不受 provider 枚举未来
放宽的影响。

升级前置检查：若存量数据已经踩中过该竞态（同一交易号绑多单），唯一索引
会建不起来——此时必须**失败并指认重复键**，由人工对账后处置，绝不静默
去重（资金流水不允许被迁移悄悄改写）。

Revision ID: 20260924T0000_wechat_transaction_unique
Revises: 20260923T1800_re_add_h3_extended_modes_rollout_compat
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924T0000_wechat_transaction_unique"
down_revision = "20260923T1800_re_add_h3_extended_modes_rollout_compat"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_recharge_orders_wechat_transaction_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    duplicates = bind.execute(
        sa.text(
            "SELECT transaction_id, COUNT(*) AS bound_orders "
            "FROM recharge_orders WHERE transaction_id IS NOT NULL "
            "GROUP BY transaction_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if duplicates:
        listed = ", ".join(repr(str(row[0])) for row in duplicates)
        raise RuntimeError(
            "cannot upgrade 20260924T0000_wechat_transaction_unique: "
            "recharge_orders already holds duplicate transaction_id values, "
            f"which the new unique index cannot index: {listed}. Resolve the "
            "duplicates through the manual reconciliation runbook first — "
            "this migration never deletes or rewrites ledger rows to pass."
        )

    op.create_index(
        _INDEX_NAME,
        "recharge_orders",
        ["transaction_id"],
        unique=True,
        postgresql_where=sa.text("transaction_id IS NOT NULL"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index(_INDEX_NAME, table_name="recharge_orders")
