from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022_internal_billing"
down_revision = "021_generation_batch_display_name"
branch_labels = None
depends_on = None


def _created_at() -> sa.Column[str]:
    return sa.Column(
        "created_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    op.add_column(
        "runtime_settings",
        sa.Column(
            "internal_base_unit_price_fen",
            sa.Integer(),
            nullable=False,
            server_default="1000",
        ),
    )
    op.add_column(
        "runtime_settings",
        sa.Column("min_recharge_fen", sa.Integer(), nullable=False, server_default="10000"),
    )
    op.add_column(
        "runtime_settings",
        sa.Column("recharge_step_fen", sa.Integer(), nullable=False, server_default="1000"),
    )

    op.create_table(
        "internal_access_tokens",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_digest", sa.Text(), nullable=False, unique=True),
        _created_at(),
        sa.Column("revoked_at", sa.Text()),
    )
    op.create_index(
        "idx_internal_access_tokens_user_status",
        "internal_access_tokens",
        ["user_id", "revoked_at"],
    )

    op.create_table(
        "wallets",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("available_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("available_credits >= 0", name="ck_wallets_available_nonnegative"),
        sa.CheckConstraint("reserved_credits >= 0", name="ck_wallets_reserved_nonnegative"),
    )

    op.create_table(
        "recharge_orders",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("merchant_order_no", sa.Text(), nullable=False, unique=True),
        sa.Column("provider", sa.Text(), nullable=False, server_default="zpay"),
        sa.Column("provider_trade_no", sa.Text()),
        sa.Column("channel", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("pricing_scope", sa.Text(), nullable=False, server_default="INTERNAL"),
        sa.Column("base_unit_price_fen_snapshot", sa.Integer(), nullable=False),
        sa.Column("charged_unit_price_fen_snapshot", sa.Integer(), nullable=False),
        sa.Column("min_recharge_fen_snapshot", sa.Integer(), nullable=False),
        sa.Column("recharge_step_fen_snapshot", sa.Integer(), nullable=False),
        sa.Column("amount_fen", sa.Integer(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("notify_digest", sa.Text()),
        _created_at(),
        sa.Column("paid_at", sa.Text()),
        sa.CheckConstraint("provider = 'zpay'", name="ck_recharge_orders_provider"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PAID', 'CLOSED', 'FAILED')",
            name="ck_recharge_orders_status",
        ),
        sa.CheckConstraint("pricing_scope = 'INTERNAL'", name="ck_recharge_orders_pricing_scope"),
        sa.CheckConstraint(
            "base_unit_price_fen_snapshot > 0", name="ck_recharge_orders_base_price"
        ),
        sa.CheckConstraint(
            "charged_unit_price_fen_snapshot > 0", name="ck_recharge_orders_charged_price"
        ),
        sa.CheckConstraint("min_recharge_fen_snapshot > 0", name="ck_recharge_orders_minimum"),
        sa.CheckConstraint("recharge_step_fen_snapshot > 0", name="ck_recharge_orders_step"),
        sa.CheckConstraint("amount_fen > 0", name="ck_recharge_orders_amount"),
        sa.CheckConstraint("credits > 0", name="ck_recharge_orders_credits"),
        sa.CheckConstraint(
            "provider_trade_no IS NULL OR length(trim(provider_trade_no)) > 0",
            name="ck_recharge_orders_provider_trade_no_not_blank",
        ),
        sa.CheckConstraint(
            "amount_fen >= min_recharge_fen_snapshot",
            name="ck_recharge_orders_amount_minimum",
        ),
        sa.CheckConstraint(
            "amount_fen % recharge_step_fen_snapshot = 0",
            name="ck_recharge_orders_amount_step",
        ),
        sa.CheckConstraint(
            "amount_fen % charged_unit_price_fen_snapshot = 0",
            name="ck_recharge_orders_amount_price",
        ),
        sa.CheckConstraint(
            "credits * charged_unit_price_fen_snapshot = amount_fen",
            name="ck_recharge_orders_credit_calculation",
        ),
    )
    op.create_index(
        "uq_recharge_orders_provider_trade_no",
        "recharge_orders",
        ["provider_trade_no"],
        unique=True,
        sqlite_where=sa.text("provider_trade_no IS NOT NULL"),
        postgresql_where=sa.text("provider_trade_no IS NOT NULL"),
    )

    op.create_table(
        "wallet_transactions",
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
        sa.CheckConstraint(
            "(type = 'CHARGE' AND available_delta > 0 AND reserved_delta = 0 "
            "AND recharge_order_id IS NOT NULL AND task_id IS NULL AND billing_round IS NULL) OR "
            "(type = 'RESERVE' AND available_delta = -1 AND reserved_delta = 1 "
            "AND recharge_order_id IS NULL AND task_id IS NOT NULL "
            "AND billing_round IS NOT NULL) OR "
            "(type = 'SETTLE' AND available_delta = 0 AND reserved_delta = -1 "
            "AND recharge_order_id IS NULL AND task_id IS NOT NULL "
            "AND billing_round IS NOT NULL) OR "
            "(type = 'RELEASE' AND available_delta = 1 AND reserved_delta = -1 "
            "AND recharge_order_id IS NULL AND task_id IS NOT NULL AND billing_round IS NOT NULL)",
            name="ck_wallet_transactions_shape",
        ),
    )
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
    )


def downgrade() -> None:
    op.drop_index("uq_wallet_transactions_terminal_round", table_name="wallet_transactions")
    op.drop_index("uq_wallet_transactions_reserve_round", table_name="wallet_transactions")
    op.drop_index("uq_wallet_transactions_charge_order", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")
    op.drop_index("uq_recharge_orders_provider_trade_no", table_name="recharge_orders")
    op.drop_table("recharge_orders")
    op.drop_table("wallets")
    op.drop_index("idx_internal_access_tokens_user_status", table_name="internal_access_tokens")
    op.drop_table("internal_access_tokens")
    op.drop_column("runtime_settings", "recharge_step_fen")
    op.drop_column("runtime_settings", "min_recharge_fen")
    op.drop_column("runtime_settings", "internal_base_unit_price_fen")
