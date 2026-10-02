"""合并口播视频兼容与诊断告警两条迁移头。

两条分支都从同一已发布注册设置 revision 延伸。本 revision 不含 DDL，只让
任一合法前序都能收敛到单一 Alembic head，避免运维升级时选择分支。

Revision ID: 20261002T1015_merge_oral_compatibility_alerts
Revises: 20261002T0800_alert_delivery_rules, 20261002T1000_oral_video_compatibility
"""

revision = "20261002T1015_merge_oral_compatibility_alerts"
down_revision = (
    "20261002T0800_alert_delivery_rules",
    "20261002T1000_oral_video_compatibility",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic 只记录图收敛，DDL 仍由两个父 revision 分别负责。
    return


def downgrade() -> None:
    # 本 revision 未创建 schema，对图的拆分没有 DDL 可执行。
    return
