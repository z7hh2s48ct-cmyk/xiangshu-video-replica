"""失败响应全文外存，明确区分摘要截断和脱敏导致的字节变化。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261001T1500_external_call_full_response"
down_revision = "20260930T1400_registration_bonus_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column("external_call_logs", sa.Column("response_storage_uri", sa.Text()))
    # 旧行是否被截断无法追溯，保留 NULL，不能把脱敏后变短的正文直接判成截断。
    op.add_column("external_call_logs", sa.Column("response_truncated", sa.Boolean()))
    op.add_column("external_call_logs", sa.Column("poll_state", sa.Text()))
    op.add_column(
        "external_call_logs",
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("external_call_logs", sa.Column("last_seen_at", sa.DateTime(timezone=True)))
    op.add_column("external_call_logs", sa.Column("cleanup_retry_at", sa.DateTime(timezone=True)))
    # 在对象写入前独立提交登记，日志事务失败或进程退出后仍能追踪未引用对象。
    op.create_table(
        "external_call_response_pending",
        sa.Column("call_id", sa.Text(), primary_key=True),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("external_call_response_pending")
    op.drop_column("external_call_logs", "cleanup_retry_at")
    op.drop_column("external_call_logs", "response_truncated")
    op.drop_column("external_call_logs", "response_storage_uri")
    op.drop_column("external_call_logs", "last_seen_at")
    op.drop_column("external_call_logs", "poll_count")
    op.drop_column("external_call_logs", "poll_state")
