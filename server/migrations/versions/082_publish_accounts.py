"""082_publish_accounts — C5 发布管理第一阶段：平台发布账号授权.

publish_accounts 保存用户在抖音/视频号的创作者登录态：Cookie 与抖音
security_sdk 材料以 Fernet 密文落库（VIDEO_REPLICA_SETTINGS_KEY 体系），
任何 API 响应都不回传凭据明文。verify_requested 驱动 publish_worker 的
异步登录态探测，失效账号标记 invalid 并要求重新连接。

lease 三件套（lease_owner/lease_expires_at/attempt_count）承载探测抢占的
CAS 租约：claim 用 ``FOR UPDATE SKIP LOCKED`` 取一条候选，再以
``lease_owner = 令牌 AND attempt_count = 领到时的值`` 做 fenced 回写，
探测中断（租约过期）由下一轮 claim 前的 quarantine 复位。

本迁移**只建 publish_accounts**。正式发布链路（publish_records 草稿/排队/
封面/定时/平台回执）属第二阶段，按 CW002-SCOPE-DECISIONS.md §8 另立任务
再追加迁移，此处不预建空表。小红书暂缓，platform CHECK 只含抖音/视频号。

idx_publish_accounts_verify_queue 直接服务探测 claim 的候选查询
（``WHERE verify_requested = 1 AND (lease_expires_at IS NULL OR ...)``），
属本阶段范围，故与 user_created 索引一并建立。

Revision ID: 082_publish_accounts
Revises: 081_oral_unit_price
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "082_publish_accounts"
down_revision = "081_oral_unit_price"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column[str]]:
    return [
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    ]


def _lease_columns() -> list[sa.Column[str | int]]:
    return [
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.Text()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    ]


def upgrade() -> None:
    op.create_table(
        "publish_accounts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("cookie_enc", sa.Text(), nullable=False),
        sa.Column("security_sdk_enc", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="connected"),
        sa.Column("verify_requested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_verified_at", sa.Text()),
        sa.Column("error_message", sa.Text()),
        *_lease_columns(),
        *_timestamps(),
        sa.CheckConstraint(
            "platform IN ('douyin', 'wechat_channels')",
            name="ck_publish_accounts_platform",
        ),
        sa.CheckConstraint("status IN ('connected', 'invalid')", name="ck_publish_accounts_status"),
        sa.CheckConstraint("verify_requested IN (0, 1)", name="ck_publish_accounts_verify_flag"),
    )
    op.create_index(
        "idx_publish_accounts_user_created",
        "publish_accounts",
        ["user_id", "created_at", "id"],
    )
    op.create_index(
        "idx_publish_accounts_verify_queue",
        "publish_accounts",
        ["verify_requested", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_table("publish_accounts")
