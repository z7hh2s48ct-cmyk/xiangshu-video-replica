"""Separate account activation from the first recharge.

Newly issued licence codes may carry zero face value and zero initial credits.
Activation still creates the customer wallet and first device, but a zero-credit
activation has no synthetic PAID recharge order or CHARGE ledger entry.

Revision ID: 050_activation_license_zero_credit
Revises: 049_async_generation_reconcile
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "050_activation_license_zero_credit"
down_revision = "049_async_generation_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.drop_constraint(
        "ck_activation_code_batches_face_value_positive",
        "activation_code_batches",
        type_="check",
    )
    op.drop_constraint(
        "ck_activation_code_batches_credits_positive",
        "activation_code_batches",
        type_="check",
    )
    op.create_check_constraint(
        "ck_activation_code_batches_face_value_nonnegative",
        "activation_code_batches",
        "face_value_fen >= 0",
    )
    op.create_check_constraint(
        "ck_activation_code_batches_credits_nonnegative",
        "activation_code_batches",
        "credits_snapshot >= 0",
    )
    op.create_check_constraint(
        "ck_activation_code_batches_license_credit_shape",
        "activation_code_batches",
        "(face_value_fen = 0 AND credits_snapshot = 0) OR "
        "(face_value_fen > 0 AND credits_snapshot > 0)",
    )
    op.alter_column(
        "activation_code_activations",
        "recharge_order_id",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    incompatible = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM activation_code_batches "
            "WHERE face_value_fen = 0 OR credits_snapshot = 0"
            ") OR EXISTS ("
            "SELECT 1 FROM activation_code_activations WHERE recharge_order_id IS NULL"
            ")"
        )
    ).scalar()
    if incompatible:
        raise RuntimeError(
            "cannot downgrade 050_activation_license_zero_credit while zero-credit "
            "batches or activation-only facts exist"
        )

    op.alter_column(
        "activation_code_activations",
        "recharge_order_id",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.drop_constraint(
        "ck_activation_code_batches_license_credit_shape",
        "activation_code_batches",
        type_="check",
    )
    op.drop_constraint(
        "ck_activation_code_batches_credits_nonnegative",
        "activation_code_batches",
        type_="check",
    )
    op.drop_constraint(
        "ck_activation_code_batches_face_value_nonnegative",
        "activation_code_batches",
        type_="check",
    )
    op.create_check_constraint(
        "ck_activation_code_batches_credits_positive",
        "activation_code_batches",
        "credits_snapshot > 0",
    )
    op.create_check_constraint(
        "ck_activation_code_batches_face_value_positive",
        "activation_code_batches",
        "face_value_fen > 0",
    )
