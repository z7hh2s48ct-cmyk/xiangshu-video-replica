"""C7 studio drafts: cloud persistence for the V1.4 copy workshop.

Revision ID: 067_studio_drafts
Revises: 066_generation_creation_kind

Two user-scoped stores: the auto-saved working draft (one row per user and
workspace kind, payload kept as opaque JSON text for dual-dialect storage)
and the explicitly saved script versions backing the "我的文案" list.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "067_studio_drafts"
down_revision = "066_generation_creation_kind"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column[str]]:
    return [
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "studio_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("draft_kind", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("script_confirmed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.CheckConstraint("script_confirmed IN (0, 1)", name="ck_studio_drafts_confirmed"),
        sa.CheckConstraint("revision >= 0", name="ck_studio_drafts_revision"),
    )
    op.create_index(
        "uq_studio_drafts_user_kind",
        "studio_drafts",
        ["user_id", "draft_kind"],
        unique=True,
    )

    op.create_table(
        "studio_saved_scripts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("script_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("original", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("ip_id", sa.Text()),
        sa.Column("source_project_id", sa.Text()),
        sa.Column("source_kind", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint("version >= 1", name="ck_studio_saved_scripts_version"),
    )
    op.create_index(
        "uq_studio_saved_scripts_user_script",
        "studio_saved_scripts",
        ["user_id", "script_id"],
        unique=True,
    )
    op.create_index(
        "idx_studio_saved_scripts_user_created",
        "studio_saved_scripts",
        ["user_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("studio_saved_scripts")
    op.drop_table("studio_drafts")
