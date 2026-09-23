"""FREE_GRANT — 运营发放免费生成条数的审计调账来源单类型.

管理员在后台为激活码对应的账号发放免费生成条数（免费 = 不产生支付金额）。
复用 T23 审计调账闭环（PAID 订单 + CHARGE 台账 + 钱包 + admin_adjustments
审计行），新增来源单类型 ``FREE_GRANT``，且该类调账的 ``amount_fen`` 允许
为 0：账面订单金额为 0 才能如实反映"未收款"，按面值计金额会虚构收入。

本迁移放宽两条 CHECK 约束（对齐 026 的"已发布 revision 只可追加"纪律，
不改写 022/039 本体）：

- ``recharge_orders``：022 的 ``ck_recharge_orders_amount``（``amount_fen > 0``）
  改为 ``amount_fen > 0 OR provider = 'admin_adjustment'``；022 的
  ``ck_recharge_orders_credit_calculation``（条数 × 单价 = 金额）追加
  ``OR (provider = 'admin_adjustment' AND amount_fen = 0)``。admin_adjustment
  订单的金额由其来源单定义（026 既有注释），审计行承担留痕责任，且免费订单
  被钉死为 0 金额（不得记任意金额）；zpay 与 activation_code 订单仍保持原约束。
- ``admin_adjustments``：039 的 ``ck_admin_adjustments_source_type`` 追加
  ``'FREE_GRANT'``。

``credits > 0`` 与来源单/原因非空约束保持不变：免费发放仍然必须 ≥ 1 条，
且必须给出可审计的来源单号与原因。

Revision ID: 054_admin_free_grant_adjustments
Revises: 053_activation_code_archive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "054_admin_free_grant_adjustments"
down_revision = "053_activation_code_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — admin adjustments are a
        # customer-production concern only (039 precedent).
        return

    # A paid order that was written before this revision cannot have a zero
    # amount (022 enforced amount_fen > 0), so relaxing to allow zero amounts
    # for admin_adjustment rows cannot strand pre-existing bad data.
    op.drop_constraint("ck_recharge_orders_amount", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_amount",
        "recharge_orders",
        "amount_fen > 0 OR provider = 'admin_adjustment'",
    )

    op.drop_constraint("ck_recharge_orders_credit_calculation", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_credit_calculation",
        "recharge_orders",
        "credits * charged_unit_price_fen_snapshot = amount_fen "
        "OR (provider = 'admin_adjustment' AND amount_fen = 0)",
    )

    op.drop_constraint("ck_admin_adjustments_source_type", "admin_adjustments", type_="check")
    op.create_check_constraint(
        "ck_admin_adjustments_source_type",
        "admin_adjustments",
        "source_document_type IN ('CS_TICKET', 'REFUND_APPROVAL', "
        "'COMPENSATION_APPROVAL', 'LEDGER_CORRECTION', 'FREE_GRANT')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Refuse the rollback while zero-amount FREE_GRANT rows exist: shrinking
    # the constraint back would have to delete audit evidence first.
    zero_amount_adjustments = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM recharge_orders "
            "WHERE provider = 'admin_adjustment' AND amount_fen <= 0)"
        )
    ).scalar()
    if zero_amount_adjustments:
        raise RuntimeError(
            "cannot downgrade 054_admin_free_grant_adjustments: zero-amount "
            "FREE_GRANT adjustment orders exist and must survive the rollback. "
            "Export or neutralize them first."
        )

    free_grant_adjustments = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM admin_adjustments "
            "WHERE source_document_type = 'FREE_GRANT')"
        )
    ).scalar()
    if free_grant_adjustments:
        raise RuntimeError(
            "cannot downgrade 054_admin_free_grant_adjustments: FREE_GRANT "
            "audit rows exist and must survive the rollback."
        )

    op.drop_constraint("ck_admin_adjustments_source_type", "admin_adjustments", type_="check")
    op.create_check_constraint(
        "ck_admin_adjustments_source_type",
        "admin_adjustments",
        "source_document_type IN ('CS_TICKET', 'REFUND_APPROVAL', "
        "'COMPENSATION_APPROVAL', 'LEDGER_CORRECTION')",
    )

    op.drop_constraint("ck_recharge_orders_amount", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_amount",
        "recharge_orders",
        "amount_fen > 0",
    )

    op.drop_constraint("ck_recharge_orders_credit_calculation", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_credit_calculation",
        "recharge_orders",
        "credits * charged_unit_price_fen_snapshot = amount_fen",
    )
