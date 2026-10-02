"""Persist browser-compatible derivatives for oral-avatar source videos.

Original customer uploads remain the consent-bound asset.  A distinct derived
asset is used only for preview and the vendor upload, so a codec conversion
never changes the bytes to which consent was granted.

Revision ID: 20261002T1000_oral_video_compatibility
Revises: 20260930T1400_registration_bonus_settings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261002T1000_oral_video_compatibility"
down_revision = "20260930T1400_registration_bonus_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "video_compat_derivatives",
        sa.Column(
            "original_asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "compatible_asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "status IN ('READY', 'FAILED')", name="ck_video_compat_derivatives_status"
        ),
    )
    op.add_column("oral_avatars", sa.Column("original_source_asset_id", sa.Text()))
    op.create_foreign_key(
        "fk_oral_avatars_original_source_asset",
        "oral_avatars",
        "assets",
        ["original_source_asset_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_constraint("fk_oral_avatars_original_source_asset", "oral_avatars", type_="foreignkey")
    op.drop_column("oral_avatars", "original_source_asset_id")
    op.drop_table("video_compat_derivatives")
