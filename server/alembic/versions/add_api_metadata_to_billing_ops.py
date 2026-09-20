"""add api_metadata column to billing_operations table.

Revision ID: add-api-metadata-to-billing-ops
Revises: current_head
Create Date: 2026-09-20

This migration adds a JSONB column to store the specific TikTok Hub API type
for each billing operation, enabling detailed cost breakdown by API type:
- douyin_search
- wechat_search_page  
- wechat_video_detail
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add-api-metadata-to-billing-ops'
down_revision: Union[str, None] = 'current_head'  # Replace with actual latest revision
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add api_metadata JSONB column to billing_operations."""
    op.add_column('billing_operations', sa.Column(
        'api_metadata',
        sa.JSON(),
        nullable=True,
        comment='API metadata including type, parameters, and response metrics'
    ))
    
    # Add index for efficient filtering by API type
    op.create_index(
        'idx_billing_operations_api_type',
        'billing_operations',
        ['api_metadata->>\'api_type\''],
        postgresql_using='gin'
    )


def downgrade() -> None:
    """Remove api_metadata column from billing_operations."""
    op.drop_index('idx_billing_operations_api_type', table_name='billing_operations')
    op.drop_column('billing_operations', 'api_metadata')
