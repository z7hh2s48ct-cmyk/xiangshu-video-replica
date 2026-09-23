"""Credit configuration and immutable, same-account billing provenance."""

import sqlalchemy as sa
from alembic import op

revision = "20260912T2330_customer_credit_pricing"
down_revision = "20260912T2200_customer_token_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "customer_credit_pricing",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1 AND version >= 0", name="ck_customer_credit_pricing_singleton"),
    )
    op.execute("INSERT INTO customer_credit_pricing (id) VALUES (1)")
    # Only the former standard full-use scope set expands. Custom restricted keys stay restricted.
    op.execute(
        'UPDATE customer_api_keys SET scopes = \'["recharge","wallet","pricing","generation"]\' '
        "WHERE revoked_at IS NULL AND scopes::jsonb @> "
        '\'["recharge","wallet","pricing"]\'::jsonb '
        "AND jsonb_array_length(scopes::jsonb) = 3"
    )
    op.create_unique_constraint("uq_api_key_id_owner", "customer_api_keys", ["id", "user_id"])
    op.create_foreign_key(
        "fk_api_key_group_owner",
        "customer_api_keys",
        "customer_api_keys",
        ["token_group_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_foreign_key(
        "fk_wallet_api_key_owner",
        "wallet_transactions",
        "customer_api_keys",
        ["api_key_id", "user_id"],
        ["id", "user_id"],
    )
    op.add_column("wallet_transactions", sa.Column("auth_source", sa.Text(), nullable=True))
    op.add_column(
        "wallet_transactions", sa.Column("pricing_snapshot_json", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "ck_wallet_auth_source",
        "wallet_transactions",
        "auth_source IS NULL OR auth_source IN ('session', 'api_key', 'internal')",
    )
    op.create_check_constraint(
        "ck_wallet_key_source",
        "wallet_transactions",
        "auth_source IS NULL OR ((auth_source = 'api_key') = (api_key_id IS NOT NULL))",
    )
    op.add_column(
        "recharge_orders", sa.Column("credit_pricing_snapshot_json", sa.Text(), nullable=True)
    )
    op.drop_constraint("ck_recharge_orders_amount_price", "recharge_orders", type_="check")
    op.drop_constraint("ck_recharge_orders_credit_calculation", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_amount_price",
        "recharge_orders",
        "credit_pricing_snapshot_json IS NOT NULL OR amount_fen % "
        "charged_unit_price_fen_snapshot = 0",
    )
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


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM customer_credit_pricing WHERE version > 0) "
            "OR EXISTS (SELECT 1 FROM wallet_transactions WHERE auth_source IS NOT NULL "
            "OR pricing_snapshot_json IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM recharge_orders WHERE credit_pricing_snapshot_json "
            "IS NOT NULL)"
        )
    ).scalar():
        raise RuntimeError("cannot downgrade: credit pricing or attribution history exists")
    op.drop_constraint("ck_recharge_orders_amount_price", "recharge_orders", type_="check")
    op.drop_constraint("ck_recharge_orders_credit_calculation", "recharge_orders", type_="check")
    op.create_check_constraint(
        "ck_recharge_orders_amount_price",
        "recharge_orders",
        "amount_fen % charged_unit_price_fen_snapshot = 0",
    )
    op.create_check_constraint(
        "ck_recharge_orders_credit_calculation",
        "recharge_orders",
        "credits * charged_unit_price_fen_snapshot = amount_fen "
        "OR (provider = 'admin_adjustment' AND amount_fen = 0)",
    )
    op.drop_column("recharge_orders", "credit_pricing_snapshot_json")
    op.drop_constraint("ck_wallet_key_source", "wallet_transactions", type_="check")
    op.drop_constraint("ck_wallet_auth_source", "wallet_transactions", type_="check")
    op.drop_column("wallet_transactions", "pricing_snapshot_json")
    op.drop_column("wallet_transactions", "auth_source")
    op.drop_constraint("fk_wallet_api_key_owner", "wallet_transactions", type_="foreignkey")
    op.drop_constraint("fk_api_key_group_owner", "customer_api_keys", type_="foreignkey")
    op.drop_constraint("uq_api_key_id_owner", "customer_api_keys", type_="unique")
    op.drop_table("customer_credit_pricing")
