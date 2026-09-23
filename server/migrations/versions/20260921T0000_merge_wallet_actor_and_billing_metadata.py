"""Merge parallel heads: device_parent_cascade + add_api_metadata_to_billing_ops.

Revision ID: 20260921T0000_merge_wallet_actor_and_billing_metadata
Revises: 20260919T1500_device_parent_cascade, 20260920T0100_add_api_metadata_to_billing_ops

CUSTOMER-CENTER-V2-20260919（20260919T1200_sub_accounts → 20260919T1300_wallet_actor →
20260919T1500_device_parent_cascade）与 main（20260919T1000_oral_soft_delete →
20260920T0000_merge_parallel_heads → 20260920T0100_add_api_metadata_to_billing_ops）
两条链都从 20260919T1000_browser_account_probe 一侧并行生长。本 merge revision 把
两条链线性化，保持 alembic 单头；纯拓扑合并，无 schema 变更。
"""

from __future__ import annotations

revision = "20260921T0000_merge_wallet_actor_and_billing_metadata"
down_revision = (
    "20260919T1500_device_parent_cascade",
    "20260920T0100_add_api_metadata_to_billing_ops",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
