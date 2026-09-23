"""Audited negative adjustment (反向调账) — ledger type ``REFUND`` (B1).

背景：``docs/客户版部署与灰度手册.md`` §10 第 2 步（BILL-04 冻结前的人工单例流程）
要求管理员经 T23 审计调账通道做**反向调账**，实际退付款项在 ZPay 后台人工办理。
在该迁移之前，账本只允许正向 ``CHARGE``：类型枚举没有退款态，形状 CHECK 把
``CHARGE`` 的 ``available_delta`` 钉死为 ``> 0``，所以 SOP 第 2 步在代码里执行不了。

本迁移做四件事（只追加分支，不改既有五类的形状）：

1. ``ck_wallet_transactions_type`` 增加 ``'REFUND'``；
2. ``ck_wallet_transactions_shape`` 增加 ``REFUND`` 形状分支，照抄既有 ``CONVERSION``
   分支的形状：``available_delta < 0 AND reserved_delta = 0`` 且不挂订单 / 任务 /
   口播任务 / 计费操作 / 计费轮次。既有形状文本**从库上读回后原样保留**——不是
   重打一遍——所以「不改既有五类」是结构性成立的，不依赖人工誊抄的准确性；
3. ``admin_adjustments.recharge_order_id`` 放开 NOT NULL：D4 拍板「退款不产生充值单」，
   而 039 建表时该列是 NOT NULL + UNIQUE 的。反向调账没有也不该有充值单
   （``ck_recharge_orders_status`` 无退款态，且 ``reconcile_customer_billing`` 要求
   每张 PAID 单恰好对应一条同额 ``CHARGE``，用负 credits 造单必然对不平），
   所以审计行必须能脱离订单存在；UNIQUE 保留（PG 下 NULL 不参与唯一性），
   「一张调账单一条审计行」的不变量对正向调账零变化；
4. 新增 ``ck_admin_adjustments_order_required``：只有可反向调账的来源单类型
   （``REFUND_APPROVAL`` / ``LEDGER_CORRECTION``）允许没有充值单，其余类型仍必须
   挂单——防止将来某条路径造出「无单的正向补偿」这种半事实。

**已拍板的设计边界（D2，行业惯例：退款只覆盖未消耗部分）**：钱包下限仍是 026 的
``ck_wallets_available_nonnegative``（``available_credits >= 0``），本迁移**不放开**
也不加透支分支。因此退款额度上限就是客户**当前可用余额**；已被消耗的额度对应已交付
服务，账本层面不予退回（争议走线下 / 拒付通道，不在本任务范围）。路由层对「退款额超过
当前可用余额」返回明确的 400 业务错误码，既不 500，也不静默截断成最大可退额
（静默截断会让运营误以为已全额退款）。

Revision ID: 20260923T1200_admin_refund_adjustment
Revises: 20260922T2200_material_preference_tags
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260923T1200_admin_refund_adjustment"
down_revision = "20260922T2200_material_preference_tags"
branch_labels = None
depends_on = None

# 反向调账允许的来源单类型（与路由 REVERSAL_SOURCE_DOCUMENT_TYPES 同源）。
# CSV 形式便于直接嵌进 CHECK 表达式；库侧一致性由
# test_admin_refund_adjustment_pg.py 的「集合相等」断言看住。
_REVERSAL_SOURCE_DOCUMENT_TYPES_SQL = "'REFUND_APPROVAL', 'LEDGER_CORRECTION'"

# REFUND 形状分支：照抄既有 CONVERSION 分支（available_delta <> 0 → 这里收紧为 < 0，
# 正向补偿继续走既有 CHARGE）。``billing_operation_id IS NULL`` 与 CONVERSION 分支
# 保持一致：手工调账不属于任何计费操作。
_REFUND_BRANCH = (
    "(type = 'REFUND' AND available_delta < 0 AND reserved_delta = 0 "
    "AND recharge_order_id IS NULL AND task_id IS NULL AND oral_task_id IS NULL "
    "AND billing_operation_id IS NULL AND billing_round IS NULL)"
)

# 本迁移落库前的形状文本（= 20260913T1100_itemized_billing 建的版本，**逐字**
# 由 ``pg_get_constraintdef`` 在 postgres:16-alpine 上读回）。降级时用它原样还原，
# 保证「降级后既有四类形状不变」。往返保真由
# test_admin_refund_adjustment_pg.py::test_downgrade_restores_pre_refund_shape 断言。
_PRE_REFUND_SHAPE = (
    "(((type = 'CHARGE'::text) AND (available_delta > 0) AND (reserved_delta = 0) "
    "AND (recharge_order_id IS NOT NULL) AND (task_id IS NULL) AND (oral_task_id IS NULL) "
    "AND (billing_operation_id IS NULL) AND (billing_round IS NULL)) OR "
    "((type = 'CONVERSION'::text) AND (available_delta <> 0) AND (reserved_delta = 0) "
    "AND (recharge_order_id IS NULL) AND (task_id IS NULL) AND (oral_task_id IS NULL) "
    "AND (billing_operation_id IS NULL) AND (billing_round IS NULL)) OR "
    "((recharge_order_id IS NULL) AND (billing_round IS NOT NULL) AND "
    "(((billing_operation_id IS NULL) AND (num_nonnulls(task_id, oral_task_id) = 1)) OR "
    "((billing_operation_id IS NOT NULL) AND (num_nonnulls(task_id, oral_task_id) <= 1))) AND "
    "(((type = 'RESERVE'::text) AND (available_delta = (- reserved_delta)) AND "
    "(reserved_delta >= 1)) OR ((type = 'SETTLE'::text) AND (available_delta = 0) AND "
    "(reserved_delta <= '-1'::integer)) OR ((type = 'RELEASE'::text) AND "
    "(available_delta = (- reserved_delta)) AND (available_delta >= 1) AND "
    "(reserved_delta <= '-1'::integer)))))"
)

_PRE_REFUND_TYPE = "type IN ('CHARGE', 'RESERVE', 'SETTLE', 'RELEASE', 'CONVERSION')"


def _live_shape_expression(bind: sa.engine.Connection) -> str:
    """读回库上 ``ck_wallet_transactions_shape`` 的表达式（去掉 ``CHECK `` 前缀）。

    这样升级只**追加**一个 OR 分支：既有五类的形状文本一个字符都没被重写，
    「只加约束分支，不改既有形状」这条红线由机制保证而不是靠评审肉眼比对。
    """
    row = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_wallet_transactions_shape' "
            "AND conrelid = 'wallet_transactions'::regclass"
        )
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "ck_wallet_transactions_shape is missing from wallet_transactions; "
            "the migration chain is not at 20260913T1100 or later."
        )
    definition = str(row[0]).strip()
    if not definition.startswith("CHECK "):
        raise RuntimeError(
            f"unexpected constraint definition for ck_wallet_transactions_shape: {definition!r}"
        )
    return definition[len("CHECK ") :].strip()


def upgrade() -> None:
    # SQLite 内部 P0 泳道没有资金账本（022+ 的既有先例），客户生产才是真源。
    if op.get_bind().dialect.name != "postgresql":
        return

    op.drop_constraint("ck_wallet_transactions_type", "wallet_transactions", type_="check")
    op.create_check_constraint(
        "ck_wallet_transactions_type",
        "wallet_transactions",
        "type IN ('CHARGE', 'RESERVE', 'SETTLE', 'RELEASE', 'CONVERSION', 'REFUND')",
    )

    existing_shape = _live_shape_expression(op.get_bind())
    op.drop_constraint("ck_wallet_transactions_shape", "wallet_transactions", type_="check")
    op.execute(
        "ALTER TABLE wallet_transactions ADD CONSTRAINT ck_wallet_transactions_shape "
        f"CHECK ({existing_shape} OR {_REFUND_BRANCH})"
    )

    # D4：反向调账不建充值单，审计行必须能脱离订单存在。
    op.execute("ALTER TABLE admin_adjustments ALTER COLUMN recharge_order_id DROP NOT NULL")
    op.create_check_constraint(
        "ck_admin_adjustments_order_required",
        "admin_adjustments",
        f"source_document_type IN ({_REVERSAL_SOURCE_DOCUMENT_TYPES_SQL}) "
        "OR recharge_order_id IS NOT NULL",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # 资金事实不能靠 schema 回滚丢弃（20260913T0630 / 20260913T1100 同款守卫）。
    refused = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM wallet_transactions WHERE type = 'REFUND') "
                "OR EXISTS(SELECT 1 FROM admin_adjustments WHERE recharge_order_id IS NULL)"
            )
        )
        .scalar()
    )
    if refused:
        raise RuntimeError(
            "Refund ledger rows and order-less adjustment audit rows must survive rollback. "
            "Keep revision 20260923T1200_admin_refund_adjustment, export the refund facts, "
            "or wait for the refund SOP to be retired before downgrading."
        )

    op.drop_constraint("ck_admin_adjustments_order_required", "admin_adjustments", type_="check")
    op.execute("ALTER TABLE admin_adjustments ALTER COLUMN recharge_order_id SET NOT NULL")

    op.drop_constraint("ck_wallet_transactions_shape", "wallet_transactions", type_="check")
    op.create_check_constraint(
        "ck_wallet_transactions_shape",
        "wallet_transactions",
        _PRE_REFUND_SHAPE,
    )
    op.drop_constraint("ck_wallet_transactions_type", "wallet_transactions", type_="check")
    op.create_check_constraint(
        "ck_wallet_transactions_type",
        "wallet_transactions",
        _PRE_REFUND_TYPE,
    )
