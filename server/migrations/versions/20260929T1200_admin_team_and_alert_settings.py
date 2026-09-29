"""团队与权限 + 通知与告警设置（方案 P2-4）。

「系统设置改造」要求新增两组配置：团队与权限（管理员 / 审计员账号列表、
新增、停用、重置密码、最近登录）与通知与告警（接收人与阈值）。

- ``users.is_super_admin``：按方案决策「在现有 admin 角色上加权限标记，
  不新增角色枚举」——团队页与高敏配置只对带标记的管理员开放，审计员与
  普通管理员看不到。与 ``is_active`` 同为标记位，仅 PG 侧存在。
- ``alert_settings``：单行（id=1）配置表——告警接收人（管理员账号，可空；
  没人接收时显式为空，而不是编造默认收件人）与失败率告警口径（阈值 /
  窗口 / 最小样本）。用单行表而不是键值表：配置永远是一份整体，读写
  都要能一眼看全。
- 初始超管引导：升级前的部署只有 bootstrap 创建的 admin（无标记），
  若不引导，升级后没有任何账号能进入团队页。若「尚无任何超管且存在
  活跃 admin」，把最早创建的活跃 admin 提升为超管；此后 bootstrap
  新部署直接以超管创建（``bootstrap.py`` 同步修改）。

PG-only（业务库唯一真源）。downgrade 删表删列：超管标记是权限位而非
账务事实，不做补偿。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260929T1200_admin_team_and_alert_settings"
down_revision = "20260929T1000_customer_annotations"
branch_labels = None
depends_on = None

_ALERT_SETTINGS = "alert_settings"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "users",
        sa.Column(
            "is_super_admin",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_users_is_super_admin_flag",
        "users",
        "is_super_admin IN (0, 1)",
    )
    # 引导已有部署：bootstrap 创建的 admin 是唯一的管理员，提升最早创建的
    # 活跃 admin。``NOT EXISTS`` 前置保证幂等且不覆盖后续手工指定（迁移在
    # 已发布链中只会执行一次，这条守卫是防御性的）。
    op.execute(
        """
        UPDATE users SET is_super_admin = 1
        WHERE role = 'admin' AND is_active = 1
          AND NOT EXISTS (SELECT 1 FROM users WHERE is_super_admin = 1)
          AND id = (
              SELECT id FROM users
              WHERE role = 'admin' AND is_active = 1
              ORDER BY created_at, id
              LIMIT 1
          )
        """
    )

    op.create_table(
        _ALERT_SETTINGS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "recipient_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # 默认值与 failure_rate_alerts 模块常量一致（30% / 60 分钟 / 5 条）：
        # 升级瞬间报告口径不变，改设置才改变行为。
        sa.Column(
            "failure_rate_threshold_percent",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="30",
        ),
        sa.Column(
            "failure_rate_window_minutes",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
        sa.Column(
            "failure_rate_min_sample",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "updated_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint("id = 1", name="ck_alert_settings_singleton"),
        sa.CheckConstraint(
            "failure_rate_threshold_percent >= 0 AND failure_rate_threshold_percent <= 100",
            name="ck_alert_settings_threshold_range",
        ),
        sa.CheckConstraint(
            "failure_rate_window_minutes >= 1 AND failure_rate_window_minutes <= 10080",
            name="ck_alert_settings_window_range",
        ),
        sa.CheckConstraint(
            "failure_rate_min_sample >= 1",
            name="ck_alert_settings_min_sample_positive",
        ),
    )
    op.execute(f"INSERT INTO {_ALERT_SETTINGS} (id) VALUES (1)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table(_ALERT_SETTINGS)
    op.drop_constraint("ck_users_is_super_admin_flag", "users", type_="check")
    op.drop_column("users", "is_super_admin")
