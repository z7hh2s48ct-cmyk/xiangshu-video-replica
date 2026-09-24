from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021_generation_batch_display_name"
down_revision = "020_deepseek_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 批次支持运营者重命名；display_name 为空时前端回退显示项目名。
    op.add_column("generation_batches", sa.Column("display_name", sa.Text()))


def downgrade() -> None:
    op.drop_column("generation_batches", "display_name")
