"""20260922T1200_recharge_packages — 管理员配置充值套餐（档位 + 赠送积分 + 折扣权益）.

需求（2026-09-22 用户确认口径）：管理员在后台配置充值档位，例如「充值 1998 元到账
2000 积分 + 视频生成 9 折」；也允许「充值 500 元到账 500 积分、无任何权益」的朴素档位。
自定义金额保留，走基础汇率换算。

落库形状：

1. ``recharge_packages``：套餐配置表（名称 / 充值金额分 / 到账积分 / 折扣率可空 /
   折扣接口范围 TEXT-JSON / 排序 / 启用 / 版本乐观锁 / 创建管理员）。
2. ``recharge_orders`` 增 ``package_id``（FK RESTRICT，套餐被订单引用后不可删）与
   ``package_snapshot_json``：下单时冻结套餐内容，事后改套餐不重算历史订单（R-D）。
   同时把 ``ck_recharge_orders_credit_calculation`` 从两支重写为三支：
   - 第一支（无任何快照）：旧口径 ``credits × charged_unit_price_fen_snapshot = amount_fen``
     或 admin_adjustment 零额调整；新增 ``package_snapshot_json IS NULL`` 防止套餐订单
     用汇率口径蒙混过关。
   - 第二支（仅汇率快照）：沿用 2330 迁移的汇率换算口径，新增套餐快照为 NULL 条件。
   - 第三支（套餐快照）：订单 credits 必须逐字等于快照 credits（赠送口径冻结），且快照
     amount_fen 与订单金额一致——套餐价与订单金额不符同样拒绝。套餐单与汇率配置解耦：
     积分由套餐定义，未配置 ``customer_credit_pricing`` 时同样可下单（不要求汇率快照）。
   同时把 ``ck_recharge_orders_amount_price`` / ``ck_recharge_orders_amount_step`` 各加
   ``package_snapshot_json IS NOT NULL`` 豁免支：套餐档位是管理员定义的独立商品（如 1998 元
   档位对默认 10 元步长不整除），不受自定义金额的整除口径约束；无套餐金额仍逐字沿用。
   平台最低充值门槛（``ck_recharge_orders_amount_minimum``）对套餐单继续生效。
3. ``customer_discounts`` 增 ``source_recharge_order_id``（FK RESTRICT，唯一索引）：
   套餐权益的来源订单，授予幂等（同订单只授予一次）；NULL 表示管理员手工折扣，不受
   套餐覆盖逻辑影响。PG 唯一索引对多行 NULL 不冲突，手工折扣可并存多条。

PG-only：客户泳道本就是 PG-only（沿 042/089/090 先例），非 postgresql 方言直接 return。

downgrade：套餐订单与套餐权益是 append-only 历史账目（R-D），一旦落过数据，删列会
孤儿化历史；按 R-B / 039/089/090 先例——有数据显式 RuntimeError 拒绝，空库对称回退。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260922T1200_recharge_packages"
down_revision = "20260921T0000_merge_wallet_actor_and_billing_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 1. 套餐配置表（管理端写契约；运行时只读）。
    op.create_table(
        "recharge_packages",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("amount_fen", sa.Integer(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("discount_rate", sa.Numeric(precision=5, scale=4), nullable=True),
        # 折扣适用接口范围：TEXT-JSON 数组（维持 head jsonb_columns=0）；空数组 = 全部接口。
        sa.Column("discount_interfaces", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        # 创建管理员；注销后置空而非拒删（套餐配置须存活）。
        sa.Column(
            "created_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint("amount_fen > 0", name="ck_recharge_packages_amount_positive"),
        sa.CheckConstraint(
            "credits > 0 AND credits <= 2147483647",
            name="ck_recharge_packages_credits_range",
        ),
        sa.CheckConstraint(
            "discount_rate IS NULL OR (discount_rate > 0 AND discount_rate <= 1.0)",
            name="ck_recharge_packages_discount_rate_range",
        ),
        sa.CheckConstraint("sort_order >= 0", name="ck_recharge_packages_sort_order_non_negative"),
        sa.CheckConstraint("version >= 0", name="ck_recharge_packages_version_non_negative"),
    )
    # 客户侧列套餐：仅启用行、按排序（sort_order, id）稳定输出。
    op.create_index(
        "idx_recharge_packages_active_sort",
        "recharge_packages",
        ["is_active", "sort_order", "id"],
    )

    # 2. recharge_orders 增套餐来源与冻结快照。
    op.add_column("recharge_orders", sa.Column("package_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_recharge_orders_package",
        "recharge_orders",
        "recharge_packages",
        ["package_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("recharge_orders", sa.Column("package_snapshot_json", sa.Text(), nullable=True))
    op.create_index("idx_recharge_orders_package", "recharge_orders", ["package_id"])

    # 3. 赠送口径：CHECK 由两支重写为三支（见模块 docstring）。
    op.drop_constraint("ck_recharge_orders_credit_calculation", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_credit_calculation",
        "recharge_orders",
        "(credit_pricing_snapshot_json IS NULL AND package_snapshot_json IS NULL AND "
        "(credits::bigint * charged_unit_price_fen_snapshot = amount_fen OR "
        "(provider = 'admin_adjustment' AND amount_fen = 0))) OR "
        "(credit_pricing_snapshot_json IS NOT NULL AND package_snapshot_json IS NULL AND "
        "COALESCE((credit_pricing_snapshot_json::jsonb->>'points_per_yuan')::bigint "
        "BETWEEN 1 AND 1000000, false) AND "
        "credits = amount_fen::bigint * "
        "(credit_pricing_snapshot_json::jsonb->>'points_per_yuan')::bigint / 100) OR "
        "(package_snapshot_json IS NOT NULL AND "
        "COALESCE((package_snapshot_json::jsonb->>'amount_fen')::bigint = "
        "amount_fen::bigint, false) AND "
        "COALESCE((package_snapshot_json::jsonb->>'credits')::bigint "
        "BETWEEN 1 AND 2147483647, false) AND "
        "credits = (package_snapshot_json::jsonb->>'credits')::bigint)",
    )

    # 3b. 套餐档位与自定义金额的额度约束解耦（见模块 docstring）：
    # 1998 元档位对默认 10 元步长/单位价不整除，但套餐是独立商品，不是自定义金额的倍数。
    op.drop_constraint("ck_recharge_orders_amount_price", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_amount_price",
        "recharge_orders",
        "package_snapshot_json IS NOT NULL OR credit_pricing_snapshot_json IS NOT NULL "
        "OR amount_fen % charged_unit_price_fen_snapshot = 0",
    )
    op.drop_constraint("ck_recharge_orders_amount_step", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_amount_step",
        "recharge_orders",
        "package_snapshot_json IS NOT NULL OR "
        "provider NOT IN ('zpay', 'wechat_native') OR "
        "amount_fen % recharge_step_fen_snapshot = 0",
    )

    # 4. 套餐权益来源（幂等授予 + 手工折扣隔离）。
    op.add_column(
        "customer_discounts", sa.Column("source_recharge_order_id", sa.Text(), nullable=True)
    )
    op.create_foreign_key(
        "fk_customer_discounts_source_order",
        "customer_discounts",
        "recharge_orders",
        ["source_recharge_order_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_customer_discounts_source_order",
        "customer_discounts",
        ["source_recharge_order_id"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # R-B / R-D：套餐订单与套餐权益是 append-only 历史账目，落了数据后删列会孤儿化历史。
    has_history = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM recharge_orders WHERE package_id IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM recharge_packages) "
            "OR EXISTS (SELECT 1 FROM customer_discounts "
            "WHERE source_recharge_order_id IS NOT NULL)"
        )
    ).scalar()
    if has_history:
        raise RuntimeError(
            "cannot downgrade 20260922T1200_recharge_packages: package orders / granted "
            "package discounts are historical ledger rows and must survive the rollback "
            "(R-D frozen account history). Keep this revision, or manually export the "
            "package trail before rolling back."
        )

    # 空库对称回退（镜像 upgrade 逆序）。
    op.drop_index("uq_customer_discounts_source_order", table_name="customer_discounts")
    op.drop_constraint(
        "fk_customer_discounts_source_order", "customer_discounts", type_="foreignkey"
    )
    op.drop_column("customer_discounts", "source_recharge_order_id")

    op.drop_constraint("ck_recharge_orders_credit_calculation", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_credit_calculation",
        "recharge_orders",
        "(credit_pricing_snapshot_json IS NULL AND (credits::bigint * "
        "charged_unit_price_fen_snapshot = amount_fen OR "
        "(provider = 'admin_adjustment' AND amount_fen = 0))) OR "
        "(credit_pricing_snapshot_json IS NOT NULL AND "
        "COALESCE((credit_pricing_snapshot_json::jsonb->>'points_per_yuan')::bigint "
        "BETWEEN 1 AND 1000000, false) AND "
        "credits = amount_fen::bigint * "
        "(credit_pricing_snapshot_json::jsonb->>'points_per_yuan')::bigint / 100)",
    )
    op.drop_constraint("ck_recharge_orders_amount_step", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_amount_step",
        "recharge_orders",
        "provider NOT IN ('zpay', 'wechat_native') OR amount_fen % recharge_step_fen_snapshot = 0",
    )
    op.drop_constraint("ck_recharge_orders_amount_price", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_amount_price",
        "recharge_orders",
        "credit_pricing_snapshot_json IS NOT NULL OR "
        "amount_fen % charged_unit_price_fen_snapshot = 0",
    )
    op.drop_index("idx_recharge_orders_package", table_name="recharge_orders")
    op.drop_column("recharge_orders", "package_snapshot_json")
    op.drop_constraint("fk_recharge_orders_package", "recharge_orders", type_="foreignkey")
    op.drop_column("recharge_orders", "package_id")

    op.drop_index("idx_recharge_packages_active_sort", table_name="recharge_packages")
    op.drop_table("recharge_packages")
