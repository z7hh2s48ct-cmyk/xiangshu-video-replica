"""057_second_based_billing — 按秒计费：放宽钱包流水形状约束（W11）。

视频生成计费从「每任务固定 1 条」切换为「按提交时长档位计秒」（产品决议
2026-09-05：预留 = 结算 = 提交秒数；实际输出秒数仅用于上游成本核算）。
本迁移只放宽 022 的 ``ck_wallet_transactions_shape`` 形状 CHECK：

- RESERVE: ``available_delta = -reserved_delta AND reserved_delta >= 1``
- SETTLE:  ``available_delta = 0 AND reserved_delta <= -1``
- RELEASE: ``available_delta = -reserved_delta AND reserved_delta <= -1``
- CHARGE 不变（充值仍为正向入账）。

红线遵守：022 为已发布迁移，只追加本修复而不篡改；旧数据全部兼容
（旧行的 ±1 天然满足新形状）。同时为 ``generation_tasks`` 增加
``billed_seconds``（提交档位秒数快照；NULL = 历史 1 条/任务，结算按 1）。

SQLite 无法 DROP CHECK：走建新表→拷贝→换名→重建部分索引的重建路径；
PostgreSQL 走 DROP/ADD CONSTRAINT。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "057_second_based_billing"
down_revision = "056_operation_cost_rates"
branch_labels = None
depends_on = None

_OLD_SHAPE = (
    "(type = 'CHARGE' AND available_delta > 0 AND reserved_delta = 0 "
    "AND recharge_order_id IS NOT NULL AND task_id IS NULL AND billing_round IS NULL) OR "
    "(type = 'RESERVE' AND available_delta = -1 AND reserved_delta = 1 "
    "AND recharge_order_id IS NULL AND task_id IS NOT NULL "
    "AND billing_round IS NOT NULL) OR "
    "(type = 'SETTLE' AND available_delta = 0 AND reserved_delta = -1 "
    "AND recharge_order_id IS NULL AND task_id IS NOT NULL "
    "AND billing_round IS NOT NULL) OR "
    "(type = 'RELEASE' AND available_delta = 1 AND reserved_delta = -1 "
    "AND recharge_order_id IS NULL AND task_id IS NOT NULL AND billing_round IS NOT NULL)"
)

_NEW_SHAPE = (
    "(type = 'CHARGE' AND available_delta > 0 AND reserved_delta = 0 "
    "AND recharge_order_id IS NOT NULL AND task_id IS NULL AND billing_round IS NULL) OR "
    "(type = 'RESERVE' AND available_delta = -reserved_delta AND reserved_delta >= 1 "
    "AND recharge_order_id IS NULL AND task_id IS NOT NULL "
    "AND billing_round IS NOT NULL) OR "
    "(type = 'SETTLE' AND available_delta = 0 AND reserved_delta <= -1 "
    "AND recharge_order_id IS NULL AND task_id IS NOT NULL "
    "AND billing_round IS NOT NULL) OR "
    "(type = 'RELEASE' AND available_delta = -reserved_delta AND available_delta >= 1 "
    "AND reserved_delta <= -1 "
    "AND recharge_order_id IS NULL AND task_id IS NOT NULL AND billing_round IS NOT NULL)"
)

_TRANSACTION_COLUMNS = (
    "id, user_id, type, available_delta, reserved_delta, "
    "recharge_order_id, task_id, billing_round, idempotency_key, created_at"
)


def _created_at() -> sa.Column[str]:
    return sa.Column(
        "created_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _wallet_transactions_columns(shape: str) -> list[sa.Column]:
    return [
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("available_delta", sa.Integer(), nullable=False),
        sa.Column("reserved_delta", sa.Integer(), nullable=False),
        sa.Column(
            "recharge_order_id",
            sa.Text(),
            sa.ForeignKey("recharge_orders.id"),
        ),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("generation_tasks.id")),
        sa.Column("billing_round", sa.Integer()),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        _created_at(),
        sa.CheckConstraint(
            "type IN ('CHARGE', 'RESERVE', 'SETTLE', 'RELEASE')",
            name="ck_wallet_transactions_type",
        ),
        sa.CheckConstraint(
            "billing_round IS NULL OR billing_round > 0",
            name="ck_wallet_transactions_billing_round",
        ),
        sa.CheckConstraint(shape, name="ck_wallet_transactions_shape"),
    ]


def _create_rate_indexes() -> None:
    op.create_index(
        "uq_wallet_transactions_charge_order",
        "wallet_transactions",
        ["recharge_order_id"],
        unique=True,
        sqlite_where=sa.text("type = 'CHARGE'"),
        postgresql_where=sa.text("type = 'CHARGE'"),
    )
    op.create_index(
        "uq_wallet_transactions_reserve_round",
        "wallet_transactions",
        ["task_id", "billing_round"],
        unique=True,
        sqlite_where=sa.text("type = 'RESERVE'"),
        postgresql_where=sa.text("type = 'RESERVE'"),
    )
    op.create_index(
        "uq_wallet_transactions_terminal_round",
        "wallet_transactions",
        ["task_id", "billing_round"],
        unique=True,
        sqlite_where=sa.text("type IN ('SETTLE', 'RELEASE')"),
        postgresql_where=sa.text("type IN ('SETTLE', 'RELEASE')"),
    )


def _sqlite_rebuild(shape: str) -> None:
    op.rename_table("wallet_transactions", "wallet_transactions_w57_old")
    op.create_table("wallet_transactions", *_wallet_transactions_columns(shape))
    op.execute(
        f"""
        INSERT INTO wallet_transactions ({_TRANSACTION_COLUMNS})
        SELECT {_TRANSACTION_COLUMNS} FROM wallet_transactions_w57_old
        """
    )
    op.drop_table("wallet_transactions_w57_old")
    _create_rate_indexes()


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_rebuild(_NEW_SHAPE)
    else:
        op.drop_constraint("ck_wallet_transactions_shape", "wallet_transactions", type_="check")
        op.create_check_constraint(
            "ck_wallet_transactions_shape", "wallet_transactions", _NEW_SHAPE
        )
    op.add_column(
        "generation_tasks",
        sa.Column(
            "billed_seconds",
            sa.Integer(),
            nullable=True,
            comment="计费秒数快照（提交档位）；NULL = 历史每任务 1 条",
        ),
    )


def downgrade() -> None:
    op.drop_column("generation_tasks", "billed_seconds")
    if op.get_bind().dialect.name == "sqlite":
        # 回滚要求先清算非 ±1 的秒数流水，否则重建时违反旧形状。
        _sqlite_rebuild(_OLD_SHAPE)
    else:
        op.drop_constraint("ck_wallet_transactions_shape", "wallet_transactions", type_="check")
        op.create_check_constraint(
            "ck_wallet_transactions_shape", "wallet_transactions", _OLD_SHAPE
        )
