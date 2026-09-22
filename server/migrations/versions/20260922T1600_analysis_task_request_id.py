"""analysis_tasks.request_id：入队请求编号随任务落库，报障可直查日志。

Revision ID: 20260922T1600_analysis_task_request_id
Revises: 20260922T1200_analysis_task_failure_diagnostic

用户报障时只说得出「哪个任务拆解失败」，客服拿到任务编号后仍要在 API 日志里
反查是哪次请求触发；P0-1 的日志基建已经把 ``X-Request-Id`` 写进每个进程日志。
本迁移补一个可空 TEXT 列，由 ``enqueue_analysis_task`` 在入队事务里写入发起
任务的请求编号，``AnalysisTaskResponse`` 再回显给桌面端失败卡片
（任务编号 + 问题编号），无需另查日志即可把 UI 现象对到具体请求。

列可空：存量任务、自动拆解（上传完成触发）与未带请求头的内部调用不受影响。
TEXT 而非定长：request id 可能是 UUID，也可能是客户端透传的 ASCII token
（``get_or_create_request_id`` 只校验长度与可打印字符）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260922T1600_analysis_task_request_id"
down_revision = "20260922T1200_analysis_task_failure_diagnostic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_tasks", sa.Column("request_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_tasks", "request_id")
