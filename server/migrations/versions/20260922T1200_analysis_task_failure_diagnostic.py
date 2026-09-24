"""analysis_tasks.upstream_diagnostic_json：失败终态保留上游结构化诊断。

Revision ID: 20260922T1200_analysis_task_failure_diagnostic
Revises: 20260922T1800_sub_account_permissions

2026-09-20 线上拆解 100% 失败时，``error_message_redacted`` 与日志只留下「上游
拒绝」的状态码，管理端与数据库都无法回答「上游到底说了什么」。本迁移补一个可空
JSONB 列，由 ``fail_analysis_task`` 在写入 FAILED 终态时落库
``AnalysisProviderFailed.upstream_diagnostic``（http_status / failure_phase /
已脱敏且有界的 reason），供管理端诊断视图与运维排查读取。

用 JSONB 而非 JSON：管理端诊断聚合将按 ``upstream_diagnostic_json->>'http_status'``
过滤；列可空，存量失败行与成功行不受影响。

Runtime is PG-only（与 20260920T0100 同款守卫）；离线归档工具仍遍历本链。
"""

from __future__ import annotations

from alembic import op

revision = "20260922T1200_analysis_task_failure_diagnostic"
down_revision = "20260922T1800_sub_account_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE analysis_tasks ADD COLUMN upstream_diagnostic_json jsonb")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE analysis_tasks DROP COLUMN upstream_diagnostic_json")
