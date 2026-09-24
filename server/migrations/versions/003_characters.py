from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "003_characters"
down_revision = "002_settings"
branch_labels = None
depends_on = None


def _created_at() -> sa.Column[str]:
    return sa.Column(
        "created_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _updated_at() -> sa.Column[str]:
    return sa.Column(
        "updated_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    op.create_table(
        "characters",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("reference_asset_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("authorization_project_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("authorization_expires_at", sa.Text()),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("is_active IN (0, 1)"),
    )
    op.create_index(
        "idx_characters_active",
        "characters",
        ["is_active", "authorization_expires_at"],
    )
    op.create_table(
        "project_main_characters",
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("character_id", sa.Text(), sa.ForeignKey("characters.id", ondelete="SET NULL")),
        sa.Column("version_id", sa.Text(), sa.ForeignKey("versions.id", ondelete="SET NULL")),
        sa.Column(
            "selected_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=False,
        ),
        sa.Column(
            "selected_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("project_main_characters")
    op.drop_index("idx_characters_active", table_name="characters")
    op.drop_table("characters")
