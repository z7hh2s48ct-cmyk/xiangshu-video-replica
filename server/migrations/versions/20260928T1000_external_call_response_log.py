"""第三方接口调用日志：保留原始响应、任务归属与第三方任务号。

方案 P0-9 / P0-12（管理端报错排查）：视频生成、首帧与人物图片、口播失败时，服务商
返回的失败原因此前没有落库——``external_call_logs``（001）只有状态码、耗时、错误码，
且只在视频提交成功与人物视图两处写入，管理端也没有读取它的页面。本迁移只追加列与
索引，不改已有列：

- ``task_type`` / ``task_id`` / ``attempt``：调用属于哪类任务、哪条任务、第几次尝试，
  替代「每类任务一个外键」的做法（原 generation_task_id / character_generation_task_id
  保留不动）；
- ``method`` / ``url_redacted`` / ``request_summary_json``：去掉签名参数后的地址与
  业务参数摘要（不含图片、视频本体）；
- ``outcome``：成功 / 服务商报错 / 超时 / 网络错误 / 响应解析失败；
- ``response_headers_json`` / ``response_body`` / ``response_body_bytes``：排查用的
  响应头子集、原始响应体（落库前已去掉密钥与签名链接，超长截断）与原始字节数；
- ``provider_error_code`` / ``provider_message``：从响应里解析出的服务商错误码与原话；
- ``provider_task_id`` / ``request_id``：第三方任务号与我方请求编号，后台按任一编号
  都能查到同一条生成记录（P0-12）。

PG-only（业务库唯一真源）。downgrade 直接删列与索引：这些是诊断数据，不是账务事实。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260928T1000_external_call_response_log"
down_revision = "20260927T0000_oral_voice_language_settings"
branch_labels = None
depends_on = None

_TABLE = "external_call_logs"
_OUTCOMES = "'SUCCEEDED', 'PROVIDER_ERROR', 'TIMEOUT', 'NETWORK_ERROR', 'PARSE_ERROR'"
_TEXT_COLUMNS = (
    "task_type",
    "task_id",
    "method",
    "url_redacted",
    "outcome",
    "response_body",
    "provider_error_code",
    "provider_message",
    "provider_task_id",
    "request_id",
)
_JSONB_COLUMNS = ("request_summary_json", "response_headers_json")
_INTEGER_COLUMNS = ("attempt", "response_body_bytes")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name in _TEXT_COLUMNS:
        op.add_column(_TABLE, sa.Column(name, sa.Text(), nullable=True))
    for name in _JSONB_COLUMNS:
        op.add_column(_TABLE, sa.Column(name, postgresql.JSONB(), nullable=True))
    for name in _INTEGER_COLUMNS:
        op.add_column(_TABLE, sa.Column(name, sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_external_call_logs_outcome",
        _TABLE,
        f"outcome IS NULL OR outcome IN ({_OUTCOMES})",
    )
    op.create_check_constraint(
        "ck_external_call_logs_response_body_bytes",
        _TABLE,
        "response_body_bytes IS NULL OR response_body_bytes >= 0",
    )
    op.create_index(
        "ix_external_call_logs_task",
        _TABLE,
        ["task_type", "task_id", "created_at"],
    )
    op.create_index("ix_external_call_logs_created_at", _TABLE, ["created_at"])
    op.create_index(
        "ix_external_call_logs_provider_task",
        _TABLE,
        ["provider", "provider_task_id"],
        postgresql_where=sa.text("provider_task_id IS NOT NULL"),
    )
    op.create_index(
        "ix_external_call_logs_provider_request",
        _TABLE,
        ["provider_request_id"],
        postgresql_where=sa.text("provider_request_id IS NOT NULL"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index("ix_external_call_logs_provider_request", table_name=_TABLE)
    op.drop_index("ix_external_call_logs_provider_task", table_name=_TABLE)
    op.drop_index("ix_external_call_logs_created_at", table_name=_TABLE)
    op.drop_index("ix_external_call_logs_task", table_name=_TABLE)
    op.drop_constraint("ck_external_call_logs_response_body_bytes", _TABLE, type_="check")
    op.drop_constraint("ck_external_call_logs_outcome", _TABLE, type_="check")
    for name in (*_INTEGER_COLUMNS, *_JSONB_COLUMNS, *_TEXT_COLUMNS):
        op.drop_column(_TABLE, name)
