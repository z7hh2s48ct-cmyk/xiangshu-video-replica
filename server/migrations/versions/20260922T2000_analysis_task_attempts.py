"""analysis_task_attempts：拆解失败历史按 attempt 留痕。

Revision ID: 20260922T2000_analysis_task_attempts
Revises: 20260922T1600_analysis_task_request_id

``analysis_tasks`` 只有一组 error_* 字段，回答的是「最后一次尝试怎么样了」：
失败终态写它、认领时清空它。用户重试两次以后，第一次为什么失败就查不到了——
终端与客服在复盘时只能看到最后一击，前面每次尝试的 error_code/阶段/上游诊断
都被覆盖。历史只保留"最后一次"的代价，在最常见的「修好代码再点一次」场景里
最贵：报障人说的往往是第一次的报错。

本迁移建 ``analysis_task_attempts``：一次尝试一行，(task_id, attempt) 唯一。
写入方共三处（``fail_analysis_task`` / 过期接管 sweep / 认领归档），都在
``analysis_routes`` 的同一 helper 里，失败同事务落库：

- status=FAILED：worker 报告失败，code/message/phase/diagnostic 与任务行同源；
- status=INTERRUPTED：锁过期被接管（``ANALYSIS_WORKER_INTERRUPTED``）；
- status=SUPERSEDED：带残留失败字段的任务行被认领，旧结论先归档再归零。

``upstream_diagnostic_json`` 用 JSONB 与 ``analysis_tasks`` 同型：诊断视图按
``->>'http_status'`` 过滤。外键 ON DELETE CASCADE：任务行被清理时历史随之清理，
不留孤儿。列全部可空（除定位键），存量数据不需要回填——历史从迁移点起积累。

Runtime is PG-only（与 20260922T1200 同款守卫）；离线归档工具仍遍历本链。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260922T2000_analysis_task_attempts"
down_revision = "20260922T1600_analysis_task_request_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "analysis_task_attempts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column("failure_phase", sa.Text()),
        sa.Column("retryable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("upstream_diagnostic_json", postgresql.JSONB()),
        sa.Column("request_id", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('FAILED', 'INTERRUPTED', 'SUPERSEDED')",
            name="ck_analysis_task_attempts_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_analysis_task_attempts_attempt"),
        sa.CheckConstraint("retryable IN (0, 1)", name="ck_analysis_task_attempts_retryable"),
        sa.UniqueConstraint(
            "task_id",
            "attempt",
            name="uq_analysis_task_attempts_task_attempt",
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("analysis_task_attempts")
