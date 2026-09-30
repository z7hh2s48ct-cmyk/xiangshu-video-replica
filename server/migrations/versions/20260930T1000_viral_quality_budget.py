"""viral 采集质量规则与月度预算（方案 P1 内容模块·采集设置）.

新增列（``viral_runtime_controls`` 单行表）：
- ``quality_min_likes`` / ``quality_duration_min_ms`` / ``quality_duration_max_ms``
  / ``quality_exclude_words_json`` —— 采集质量门槛：入池前过滤低质内容。
- ``monthly_budget_fen`` —— 平台采集成本月度预算；达到 100% 时暂停定时采集
  （手动采集仍可用），由采集入口在提交前检查。

Revision ID: 20260930T1000_viral_quality_budget
Revises: 20260929T1200_admin_team_and_alert_settings
"""

import sqlalchemy as sa
from alembic import op

revision = "20260930T1000_viral_quality_budget"
down_revision = "20260929T1200_admin_team_and_alert_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — viral collection is a PG-only concern.
        return
    op.add_column(
        "viral_runtime_controls",
        sa.Column("quality_min_likes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "viral_runtime_controls",
        sa.Column("quality_duration_min_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "viral_runtime_controls",
        sa.Column("quality_duration_max_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "viral_runtime_controls",
        sa.Column("quality_exclude_words_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "viral_runtime_controls",
        sa.Column("monthly_budget_fen", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_viral_quality_likes_positive",
        "viral_runtime_controls",
        "quality_min_likes IS NULL OR quality_min_likes >= 0",
    )
    op.create_check_constraint(
        "ck_viral_quality_duration_range",
        "viral_runtime_controls",
        "quality_duration_min_ms IS NULL OR quality_duration_min_ms >= 0",
    )
    op.create_check_constraint(
        "ck_viral_quality_duration_max",
        "viral_runtime_controls",
        "quality_duration_max_ms IS NULL OR quality_duration_max_ms >= 0",
    )
    op.create_check_constraint(
        "ck_viral_monthly_budget_positive",
        "viral_runtime_controls",
        "monthly_budget_fen IS NULL OR monthly_budget_fen > 0",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_viral_monthly_budget_positive", "viral_runtime_controls", type_="check")
    op.drop_constraint("ck_viral_quality_duration_max", "viral_runtime_controls", type_="check")
    op.drop_constraint("ck_viral_quality_duration_range", "viral_runtime_controls", type_="check")
    op.drop_constraint("ck_viral_quality_likes_positive", "viral_runtime_controls", type_="check")
    op.drop_column("viral_runtime_controls", "monthly_budget_fen")
    op.drop_column("viral_runtime_controls", "quality_exclude_words_json")
    op.drop_column("viral_runtime_controls", "quality_duration_max_ms")
    op.drop_column("viral_runtime_controls", "quality_duration_min_ms")
    op.drop_column("viral_runtime_controls", "quality_min_likes")
