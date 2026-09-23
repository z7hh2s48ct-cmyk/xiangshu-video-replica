"""Hide revoked activation codes while preserving their audit lineage.

Revision ID: 053_activation_code_archive
Revises: 052_character_scene_look_tasks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "053_activation_code_archive"
down_revision = "052_character_scene_look_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column("activation_codes", sa.Column("archived_at", sa.Text()))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    archived = bind.execute(
        sa.text("SELECT 1 FROM activation_codes WHERE archived_at IS NOT NULL LIMIT 1")
    ).fetchone()
    if archived is not None:
        raise RuntimeError(
            "cannot downgrade activation-code archive support while archived rows exist"
        )
    with op.batch_alter_table("activation_codes") as batch_op:
        batch_op.drop_column("archived_at")
