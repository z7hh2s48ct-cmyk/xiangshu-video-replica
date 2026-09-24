"""Drop the h3_extended_modes_enabled gate column.

This removes the unused gate column that was never needed since T2V/R2V/I2V/L2V
are now always open (no vendor verification door required). The column was added
by 075_independent_creation with ``server_default FALSE``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260923T0000_open_h3_extended_modes"
down_revision = "20260922T2000_analysis_task_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the column entirely — no longer needed.
    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.drop_column("h3_extended_modes_enabled")


def downgrade() -> None:
    """Re-add the column exactly as 075 created it.

    ``server_default FALSE`` mirrors 075_independent_creation so the downgrade
    restores the schema this migration found, not a state that never shipped.
    """
    with op.batch_alter_table("runtime_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "h3_extended_modes_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("FALSE"),
            )
        )
