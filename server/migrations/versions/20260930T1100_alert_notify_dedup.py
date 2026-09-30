"""告警邮件推送防打扰去重表（方案 P2「通知与告警」推送通道）.

``alert_notify_dedup`` 单行表记录最近一次告警邮件投递时刻：同一小时内
重复拉取告警总览不会重复发信。发送本身走后台（deliver_quietly），失败
只记日志——通知从不拖垮告警页。

Revision ID: 20260930T1100_alert_notify_dedup
Revises: 20260930T1000_viral_quality_budget
"""

import sqlalchemy as sa
from alembic import op

revision = "20260930T1100_alert_notify_dedup"
down_revision = "20260930T1000_viral_quality_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — alert push is a PG-only concern.
        return
    op.create_table(
        "alert_notify_dedup",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_table("alert_notify_dedup")
