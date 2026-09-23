"""20260912T1353_customer_discounts — CW-075 折扣数据模型.

技术方案 §3.1 line 158-160 / §2.3 / §4 Phase 5.

合并文档计划的三支 Phase 5 折扣迁移（customer_discounts 表 /
generation_tasks.discount_rate_snapshot / wallet_transactions.discount_rate）为单一迁移——
因 089 号已被 CW-078 customer_api_keys 占用，本分支 re-linearize 到 089 之上；并按
MIGRATION-GUARD-20260912 命名策略改用时间戳前缀（原编号 090）。

customer_discounts 保存客户的消耗侧折扣配置：折扣率（0 < rate ≤ 1.0）、优先级（互斥取最高）、
有效期（valid_from/valid_until）、适用接口范围（applicable_interfaces TEXT-JSON 数组，维持 head
jsonb_columns=0，cw056 §617）。折扣是售价侧概念（user_price_fen × discount_rate），不涉及成本
cost_price_fen（R-A 红线）。

generation_tasks 增 discount_rate_snapshot（可空）：任务提交时冻结折扣率，事后改配置不重算历史
（R-D 历史账目冻结，沿 059 snapshot 机制）。填充逻辑归 CW-076。

wallet_transactions 增 discount_rate（可空）：交易级折扣追踪。文档 §4「形状 CHECK 兼容」——057
落地的形状 CHECK（ck_wallet_transactions_shape）只约束 amount/reserved/settled/released 四列，
不提及 discount_rate，故新增可空列天然兼容，本迁移**不动**形状 CHECK（把 RESERVE/SETTLE 收紧为
必填 discount_rate 归 CW-076 接入 reserve 流后再做，否则现有 reserve/finalize 流插入的 NULL
discount_rate 行会集体违反 CHECK）。仅追加 NULL 容忍的值域 CHECK 保证已落值的完整性。

PG-only：折扣是客户计费基础设施——沿 042/044/089 先例，非 postgresql 方言直接 return（internal/
桌面 lane 不建此表，客户泳道本就 PG-only，见 customer_fence._customer_database_configured）。

downgrade：generation_tasks.discount_rate_snapshot / wallet_transactions.discount_rate 是
append-only 历史账目（R-D），一旦落了非空折扣快照，删列会孤儿化历史，故按 R-B / 039/044/089
先例——有数据显式 RuntimeError 拒绝，空库对称回退。

Revision ID: 20260912T1353_customer_discounts
Revises: 089_customer_api_keys
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260912T1353_customer_discounts"
down_revision = "089_customer_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 1. customer_discounts 表（管理端配置的客户消耗侧折扣）
    op.create_table(
        "customer_discounts",
        # 代理主键用 Text uuid（沿 089 customer_api_keys；users.id 亦是 Text）。
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("discount_rate", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        # 适用接口范围存 TEXT-JSON 数组（维持 head jsonb_columns=0）；空数组 = 全部接口。
        sa.Column("applicable_interfaces", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        # 创建人（管理员）；管理员注销后置空而非拒删（折扣配置须存活）。
        sa.Column(
            "created_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "discount_rate > 0 AND discount_rate <= 1.0",
            name="ck_customer_discounts_rate_range",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_customer_discounts_valid_period",
        ),
        sa.CheckConstraint("priority >= 0", name="ck_customer_discounts_priority_non_negative"),
    )
    # 活跃折扣查询：用户 + 启用 + 有效期。
    op.create_index(
        "idx_customer_discounts_user_active",
        "customer_discounts",
        ["user_id", "is_active", "valid_from", "valid_until"],
    )
    # 优先级排序（互斥取最高）。
    op.create_index(
        "idx_customer_discounts_priority",
        "customer_discounts",
        ["user_id", "priority"],
    )

    # 2. generation_tasks 增 discount_rate_snapshot（可空；填充归 CW-076）。
    op.add_column(
        "generation_tasks",
        sa.Column("discount_rate_snapshot", sa.Numeric(precision=5, scale=4), nullable=True),
    )
    op.create_check_constraint(
        "ck_generation_tasks_discount_rate_snapshot_range",
        "generation_tasks",
        "discount_rate_snapshot IS NULL "
        "OR (discount_rate_snapshot > 0 AND discount_rate_snapshot <= 1.0)",
    )

    # 3. wallet_transactions 增 discount_rate（可空；形状 CHECK 不动，见模块 docstring）。
    op.add_column(
        "wallet_transactions",
        sa.Column("discount_rate", sa.Numeric(precision=5, scale=4), nullable=True),
    )
    op.create_check_constraint(
        "ck_wallet_transactions_discount_rate_range",
        "wallet_transactions",
        "discount_rate IS NULL OR (discount_rate > 0 AND discount_rate <= 1.0)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # R-B / R-D：折扣快照是 append-only 历史账目，落了非空值后删列会孤儿化历史。
    has_discount_history = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM generation_tasks WHERE discount_rate_snapshot IS NOT NULL"
            ") OR EXISTS ("
            "SELECT 1 FROM wallet_transactions WHERE discount_rate IS NOT NULL"
            ")"
        )
    ).scalar()
    if has_discount_history:
        raise RuntimeError(
            "cannot downgrade 20260912T1353_customer_discounts: discount_rate_snapshot / "
            "discount_rate historical ledger rows must survive the rollback (R-D frozen "
            "account history). Keep revision 090, or manually export the discount trail "
            "before rolling back."
        )

    # 空库对称回退：先撤值域 CHECK 再删列，最后拆索引与表（镜像 upgrade 逆序）。
    op.drop_constraint(
        "ck_wallet_transactions_discount_rate_range", "wallet_transactions", type_="check"
    )
    op.drop_column("wallet_transactions", "discount_rate")
    op.drop_constraint(
        "ck_generation_tasks_discount_rate_snapshot_range", "generation_tasks", type_="check"
    )
    op.drop_column("generation_tasks", "discount_rate_snapshot")
    op.drop_index("idx_customer_discounts_priority", table_name="customer_discounts")
    op.drop_index("idx_customer_discounts_user_active", table_name="customer_discounts")
    op.drop_table("customer_discounts")
