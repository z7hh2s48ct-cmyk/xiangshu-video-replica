"""合并轮询仍逐次保留无正文指标，孤儿删除失败延后重试。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261001T1600_external_call_metrics"
down_revision = "20261001T1500_external_call_full_response"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "external_call_response_pending", sa.Column("cleanup_retry_at", sa.DateTime(timezone=True))
    )
    op.create_table(
        "external_call_observations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
    )
    op.create_index(
        "ix_external_call_observations_time", "external_call_observations", ["created_at"]
    )
    # 历史被合并的轮询耗时不可倒推，不回填伪造观测。


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("external_call_observations")
    op.drop_column("external_call_response_pending", "cleanup_retry_at")
