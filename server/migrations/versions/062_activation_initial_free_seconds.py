"""Allow explicitly authorized initial free seconds without booking revenue.

Revision ID: 062_activation_initial_free_seconds
Revises: 061_saved_prompt_metadata
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "062_activation_initial_free_seconds"
down_revision = "061_saved_prompt_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_constraint(
        "ck_activation_code_batches_license_credit_shape", "activation_code_batches", type_="check"
    )
    op.create_check_constraint(
        "ck_activation_code_batches_license_credit_shape",
        "activation_code_batches",
        "face_value_fen = 0 OR credits_snapshot > 0",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM activation_code_batches "
            "WHERE face_value_fen = 0 AND credits_snapshot > 0)"
        )
    ).scalar():
        raise RuntimeError(
            "Cannot downgrade 062 while initial free-second batches exist; "
            "preserve their authorization and ledger history."
        )
    op.drop_constraint(
        "ck_activation_code_batches_license_credit_shape", "activation_code_batches", type_="check"
    )
    op.create_check_constraint(
        "ck_activation_code_batches_license_credit_shape",
        "activation_code_batches",
        "(face_value_fen = 0 AND credits_snapshot = 0) "
        "OR (face_value_fen > 0 AND credits_snapshot > 0)",
    )
