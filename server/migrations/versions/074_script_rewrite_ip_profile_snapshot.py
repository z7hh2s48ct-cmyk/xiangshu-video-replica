"""Snapshot an optional character IP profile for script rewrites.

Revision ID: 074_script_rewrite_ip_profile_snapshot
Revises: 073_oral_durable_billing
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "074_script_rewrite_ip_profile_snapshot"
down_revision = "073_oral_durable_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("character_personas") as batch_op:
        batch_op.add_column(
            sa.Column("ip_profile_revision", sa.Integer(), nullable=False, server_default="0")
        )
    with op.batch_alter_table("script_rewrite_tasks") as batch_op:
        batch_op.add_column(sa.Column("identity_id", sa.Text()))
        batch_op.add_column(sa.Column("ip_profile_snapshot_json", sa.Text()))
        batch_op.add_column(sa.Column("ip_profile_hash", sa.Text()))
        batch_op.create_check_constraint(
            "ck_script_rewrite_tasks_ip_profile_shape",
            "(identity_id IS NULL AND ip_profile_snapshot_json IS NULL "
            "AND ip_profile_hash IS NULL) OR "
            "(identity_id IS NOT NULL AND ip_profile_snapshot_json IS NOT NULL "
            "AND length(ip_profile_hash) = 64)",
        )
    op.create_index(
        "idx_script_rewrite_tasks_project_identity_created",
        "script_rewrite_tasks",
        ["project_id", "identity_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_script_rewrite_tasks_project_identity_created",
        table_name="script_rewrite_tasks",
    )
    with op.batch_alter_table("script_rewrite_tasks") as batch_op:
        batch_op.drop_constraint("ck_script_rewrite_tasks_ip_profile_shape", type_="check")
        batch_op.drop_column("ip_profile_hash")
        batch_op.drop_column("ip_profile_snapshot_json")
        batch_op.drop_column("identity_id")
    with op.batch_alter_table("character_personas") as batch_op:
        batch_op.drop_column("ip_profile_revision")
