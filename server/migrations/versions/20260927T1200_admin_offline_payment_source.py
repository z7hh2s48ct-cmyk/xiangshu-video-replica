"""管理员代客开通套餐：来源单类型增加 OFFLINE_PAYMENT（线下收款）.

需求（2026-09-27 用户确认）：客户线下已付款（对公转账等），管理员在后台选一个充值
套餐直接为其开通——积分与折扣权益按套餐冻结快照落账，与客户自助 ZPay 购买同形。

既有来源单枚举里没有「线下收款」：借用 ``CS_TICKET`` / ``COMPENSATION_APPROVAL``
会让对账时无法区分「真实收了钱的开通」与「客服补偿」，借用 ``FREE_GRANT`` 更会把
有收入的订单误标成赠送。故只追加一个枚举值，不动其他任何约束：

- 套餐单的金额/积分由 ``ck_recharge_orders_credit_calculation`` 第三支（套餐快照）
  约束，``20260922T1200`` 已覆盖 ``provider='admin_adjustment'``，本迁移无需改它；
- 金额下限 ``ck_recharge_orders_amount_minimum`` 对套餐单继续生效（路由先行 422）。

PG-only（客户泳道本就 PG-only）。downgrade：已有 OFFLINE_PAYMENT 审计行时拒绝回退
（append-only 审计事实不得被新 CHECK 拒之门外），空表对称回退。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260927T1200_admin_offline_payment_source"
down_revision = "20260925T1400_api_metadata_pending_write"
branch_labels = None
depends_on = None

_PREVIOUS_TYPES = (
    "'CS_TICKET', 'REFUND_APPROVAL', 'COMPENSATION_APPROVAL', "
    "'LEDGER_CORRECTION', 'FREE_GRANT', 'CREDIT_COMPENSATION'"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_constraint("ck_admin_adjustments_source_type", "admin_adjustments", type_="check")
    op.create_check_constraint(
        "ck_admin_adjustments_source_type",
        "admin_adjustments",
        f"source_document_type IN ({_PREVIOUS_TYPES}, 'OFFLINE_PAYMENT')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    has_rows = bind.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM admin_adjustments "
            "WHERE source_document_type = 'OFFLINE_PAYMENT')"
        )
    ).scalar()
    if has_rows:
        raise RuntimeError(
            "cannot downgrade 20260927T1200_admin_offline_payment_source: OFFLINE_PAYMENT "
            "adjustments are append-only audit rows and must survive the rollback."
        )
    op.drop_constraint("ck_admin_adjustments_source_type", "admin_adjustments", type_="check")
    op.create_check_constraint(
        "ck_admin_adjustments_source_type",
        "admin_adjustments",
        f"source_document_type IN ({_PREVIOUS_TYPES})",
    )
