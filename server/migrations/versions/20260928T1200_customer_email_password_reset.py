"""客户邮箱绑定与邮箱找回密码.

需求（2026-09-28 用户确认）：客户可自助绑定邮箱，忘记密码时凭邮箱验证码重置；
发信走邮件推送服务（凭据与其他供应商一样只进 ``provider_settings`` 的 Fernet 密文）。

- ``users.email`` / ``users.email_verified_at``：只存**验证通过**的邮箱。待验证的地址
  只活在验证码行里（``target_email``），所以 ``email`` 非空即已验证，找回密码不会
  把验证码发到一个从未证明归属的地址。部分唯一索引保证一个邮箱只对应一个账号，
  找回时按邮箱定位不会有歧义；CHECK 锁住小写形态，唯一性不被大小写绕开。
- ``customer_email_codes``：一次性 6 位验证码，只存带密钥的摘要（与设备指纹同一
  密钥域），限时、限次、用过即作废。随账号删除级联清理——验证码不是审计事实，
  审计走既有 ``audit_logs``。
- ``provider_settings`` 白名单追加 ``ses``（邮件推送）。

PG-only（客户泳道本就 PG-only）。downgrade：已有已验证邮箱时拒绝回退，避免静默
丢掉客户的找回凭据。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260928T1200_customer_email_password_reset"
down_revision = "20260927T0000_oral_voice_language_settings"
branch_labels = None
depends_on = None

_PREVIOUS_PROVIDERS = (
    "'apilio','metaso','cos','deepseek','zpay','hifly',"
    "'tikhub','dashscope','douyidou','wechat_native'"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column("users", sa.Column("email", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_users_email_shape",
        "users",
        "email IS NULL OR (email = lower(email) AND email_verified_at IS NOT NULL)",
    )
    op.create_index(
        "uq_users_email",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    op.create_table(
        "customer_email_codes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("target_email", sa.Text(), nullable=False),
        sa.Column("code_digest", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purpose IN ('bind_email', 'reset_password')",
            name="ck_customer_email_codes_purpose",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_customer_email_codes_attempts"),
    )
    op.create_index(
        "ix_customer_email_codes_user_purpose",
        "customer_email_codes",
        ["user_id", "purpose", "created_at"],
    )

    op.execute(
        "ALTER TABLE provider_settings DROP CONSTRAINT ck_provider_settings_supported_provider"
    )
    op.execute(
        "ALTER TABLE provider_settings ADD CONSTRAINT ck_provider_settings_supported_provider "
        f"CHECK (provider IN ({_PREVIOUS_PROVIDERS},'ses'))"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    has_email = bind.execute(
        sa.text("SELECT EXISTS(SELECT 1 FROM users WHERE email IS NOT NULL)")
    ).scalar()
    if has_email:
        raise RuntimeError(
            "cannot downgrade 20260928T1200_customer_email_password_reset: customers have "
            "verified emails bound for password recovery."
        )
    op.execute("DELETE FROM provider_settings WHERE provider = 'ses'")
    op.execute(
        "ALTER TABLE provider_settings DROP CONSTRAINT ck_provider_settings_supported_provider"
    )
    op.execute(
        "ALTER TABLE provider_settings ADD CONSTRAINT ck_provider_settings_supported_provider "
        f"CHECK (provider IN ({_PREVIOUS_PROVIDERS}))"
    )
    op.drop_index("ix_customer_email_codes_user_purpose", table_name="customer_email_codes")
    op.drop_table("customer_email_codes")
    op.drop_index("uq_users_email", table_name="users")
    op.drop_constraint("ck_users_email_shape", "users", type_="check")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email")
