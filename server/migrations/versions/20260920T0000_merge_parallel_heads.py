"""Merge parallel heads: browser_account_probe + oral_soft_delete.

Revision ID: 20260920T0000_merge_parallel_heads
Revises: 20260919T1000_browser_account_probe, 20260919T1000_oral_soft_delete

Both migrations branch from 20260918T1200_publish_account_avatar independently.
This merge revision linearises the chain so alembic has a single head.
"""

from __future__ import annotations

revision = "20260920T0000_merge_parallel_heads"
down_revision = (
    "20260919T1000_browser_account_probe",
    "20260919T1000_oral_soft_delete",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
