"""任务中心类型保真（I13）：生成批次记录创作来源。

此前所有生成批次都来自项目复刻流，前端把类型硬编码为"视频生成"。
补一列 creation_kind 记录批次创建通道（replica=视频复刻），后续独立创作
（independent=视频生成）与人物置换（replacement=人物置换）接入时各自
写入自己的通道值，前端类型页签即可按真实类型筛选。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "066_generation_creation_kind"
down_revision = "065_oral_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_batches",
        sa.Column(
            "creation_kind",
            sa.Text(),
            nullable=False,
            server_default="replica",
        ),
    )


def downgrade() -> None:
    op.drop_column("generation_batches", "creation_kind")
