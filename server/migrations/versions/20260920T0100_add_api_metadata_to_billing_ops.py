"""API 元数据列：billing_operations.api_metadata 记录 viral_data 各 API 类型。

Revision ID: 20260920T0100_add_api_metadata_to_billing_ops
Revises: 20260920T0000_merge_parallel_heads

viral_data 服务的成本由多个上游 API 构成（douyin_search / wechat_search_page /
wechat_video_detail），此前 billing_operations 只能记到 service 粒度，管理端无法
按 API 类型做成本拆解与利润核算。本迁移补一个可空 JSONB 列，由 billing_meter
的 meter_call 上下文写入 ``{"api_type": ...}``，管理端统计按
``api_metadata->>'api_type'`` 分组。

用 JSONB 而非 JSON：管理端统计对表达式 ``api_metadata->>'api_type'`` 分组过滤，
jsonb 的操作符与索引路径更完整；列可空，存量行与其它服务的操作不受影响。
刻意不建表达式/gin 索引：统计查询先按 collection_batch_id / user_id 圈定小
结果集再分组，api_type 索引对该查询无可测收益，只会扩大 schema 冻结面。
"""

from __future__ import annotations

from alembic import op

revision = "20260920T0100_add_api_metadata_to_billing_ops"
down_revision = "20260920T0000_merge_parallel_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Registered offline archive tools still traverse this chain; runtime is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE billing_operations ADD COLUMN api_metadata jsonb")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE billing_operations DROP COLUMN api_metadata")
