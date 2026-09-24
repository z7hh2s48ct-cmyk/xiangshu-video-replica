"""账号级隐藏口播任务：任务中心的「删除」只隐藏列表项，不删数据。

与 ``055_customer_batch_visibility``（普通批次的删除语义）对齐：任务中心对
两类任务都提供「从列表移除」，但 ``oral_tasks`` 行承载计费追溯（钱包事务按
``oral_task_id`` 对账），且被 ``oral_avatars``/``oral_voices`` 的 RESTRICT
外键引用，因此隐藏只写本偏好表——任务行、钱包事务与审计记录一律保留；
取消偏好即恢复可见。

Revision ID: 20260924T0200_customer_oral_task_visibility
Revises: 20260924T0100_oral_task_submitted_at
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924T0200_customer_oral_task_visibility"
down_revision = "20260924T0100_oral_task_submitted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_oral_task_visibility",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column(
            "hidden_at", sa.Text(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.PrimaryKeyConstraint("user_id", "task_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["oral_tasks.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    # 移除偏好即恢复可见；口播任务与账务历史全部原样保留。
    op.drop_table("customer_oral_task_visibility")
