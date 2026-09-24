"""Allow durable character tasks to publish a scene-specific appearance.

Revision ID: 052_character_scene_look_tasks
Revises: 051_identity_owner_backfill
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "052_character_scene_look_tasks"
down_revision = "051_identity_owner_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("character_sheet_tasks") as batch_op:
        batch_op.drop_constraint("ck_character_sheet_tasks_operation", type_="check")
        batch_op.create_check_constraint(
            "ck_character_sheet_tasks_operation",
            "operation IN ('CREATE','REGENERATE','SCENE')",
        )


def downgrade() -> None:
    existing = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM character_sheet_tasks WHERE operation = 'SCENE' LIMIT 1"))
        .fetchone()
    )
    if existing is not None:
        raise RuntimeError(
            "cannot downgrade scene-look task support while SCENE task history exists"
        )
    with op.batch_alter_table("character_sheet_tasks") as batch_op:
        batch_op.drop_constraint("ck_character_sheet_tasks_operation", type_="check")
        batch_op.create_check_constraint(
            "ck_character_sheet_tasks_operation",
            "operation IN ('CREATE','REGENERATE')",
        )
