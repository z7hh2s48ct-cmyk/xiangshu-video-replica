"""Allow an administrator-controlled unit price for each activated customer.

Revision ID: 044_customer_unit_prices
Revises: 043_admin_password_login

The internal base price remains an immutable order snapshot for reporting, but
it no longer constrains the price charged to a customer.  The effective sale
price is controlled by ``customer_unit_prices`` and is frozen on every new
recharge or adjustment order.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "044_customer_unit_prices"
down_revision = "043_admin_password_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.create_table(
        "customer_unit_prices",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("unit_price_fen", sa.Integer(), nullable=False),
        sa.Column(
            "updated_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
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
        sa.CheckConstraint(
            "unit_price_fen > 0",
            name="ck_customer_unit_prices_positive",
        ),
    )
    op.drop_constraint(
        "ck_recharge_orders_customer_price_floor",
        "recharge_orders",
        type_="check",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    has_below_base_orders = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM recharge_orders "
            "WHERE pricing_scope = 'CUSTOMER_STANDARD' "
            "AND charged_unit_price_fen_snapshot < base_unit_price_fen_snapshot"
            ")"
        )
    ).scalar()
    if has_below_base_orders:
        raise RuntimeError(
            "cannot downgrade 044_customer_unit_prices: customer orders already "
            "contain a charged price below the internal base snapshot"
        )

    op.create_check_constraint(
        "ck_recharge_orders_customer_price_floor",
        "recharge_orders",
        "pricing_scope = 'INTERNAL' OR "
        "charged_unit_price_fen_snapshot >= base_unit_price_fen_snapshot",
    )
    op.drop_table("customer_unit_prices")
