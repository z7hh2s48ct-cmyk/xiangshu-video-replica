"""爆款视频上线闭环：收藏、可见性与项目导入任务。

Revision ID: 078_viral_module_launch
Revises: 077_durable_script_from_audio

公共爆款库继续作为可复用参考数据；用户收藏与项目导入分别持久化，
避免把公共缓存对象直接当成用户项目资产。导入任务使用已有 PostgreSQL
租约 Worker 模式，并保留 SQLite 桌面兼容。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "078_viral_module_launch"
down_revision = "077_durable_script_from_audio"
branch_labels = None
depends_on = None


def _created_at() -> sa.Column[str]:
    return sa.Column(
        "created_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    op.create_table(
        "viral_video_favorites",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("user_id", "platform", "video_id"),
        sa.CheckConstraint(
            "platform IN ('douyin', 'wechat_channels')",
            name="ck_viral_video_favorites_platform",
        ),
    )
    op.create_index(
        "idx_viral_video_favorites_user_created",
        "viral_video_favorites",
        ["user_id", "created_at", "platform", "video_id"],
    )

    op.create_table(
        "viral_video_visibility",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="AVAILABLE"),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "updated_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("platform", "video_id"),
        sa.CheckConstraint(
            "platform IN ('douyin', 'wechat_channels')",
            name="ck_viral_video_visibility_platform",
        ),
        sa.CheckConstraint(
            "status IN ('AVAILABLE', 'HIDDEN', 'UNAVAILABLE')",
            name="ck_viral_video_visibility_status",
        ),
    )

    op.create_table(
        "viral_runtime_controls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("collection_enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("import_enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("id = 1", name="ck_viral_runtime_controls_singleton"),
        sa.CheckConstraint(
            "collection_enabled IN (0, 1)",
            name="ck_viral_runtime_controls_collection_enabled",
        ),
        sa.CheckConstraint(
            "import_enabled IN (0, 1)",
            name="ck_viral_runtime_controls_import_enabled",
        ),
    )
    op.execute(
        "INSERT INTO viral_runtime_controls (id, collection_enabled, import_enabled) "
        "VALUES (1, 1, 1)"
    )

    op.create_table(
        "viral_import_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "source_asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column("retryable", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        sa.CheckConstraint(
            "platform IN ('douyin', 'wechat_channels')",
            name="ck_viral_import_tasks_platform",
        ),
        sa.CheckConstraint(
            "purpose IN ('copy', 'replica')",
            name="ck_viral_import_tasks_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_viral_import_tasks_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_viral_import_tasks_attempt"),
        sa.CheckConstraint(
            "retryable IN (0, 1)",
            name="ck_viral_import_tasks_retryable",
        ),
    )
    op.create_index(
        "uq_viral_import_tasks_owner_idempotency",
        "viral_import_tasks",
        ["owner_user_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "idx_viral_import_tasks_claim",
        "viral_import_tasks",
        ["status", "locked_until", "created_at", "id"],
    )
    op.create_index(
        "idx_viral_import_tasks_owner_created",
        "viral_import_tasks",
        ["owner_user_id", "created_at", "id"],
    )
    op.create_index(
        "idx_viral_import_tasks_source",
        "viral_import_tasks",
        ["platform", "video_id", "created_at"],
    )

    op.create_table(
        "viral_media_preparations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("media_kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
        sa.Column("owner_task_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.Text()),
        sa.UniqueConstraint(
            "platform", "video_id", "media_kind", name="uq_viral_media_preparations_source"
        ),
        sa.CheckConstraint(
            "platform IN ('douyin', 'wechat_channels')",
            name="ck_viral_media_preparations_platform",
        ),
        sa.CheckConstraint(
            "media_kind IN ('audio', 'video')",
            name="ck_viral_media_preparations_kind",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_viral_media_preparations_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_viral_media_preparations_attempt"),
    )
    op.create_index(
        "idx_viral_media_preparations_claim",
        "viral_media_preparations",
        ["status", "locked_until", "updated_at", "id"],
    )

    op.create_table(
        "viral_refresh_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("sort", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column("retryable", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        sa.UniqueConstraint("platform", "sort", name="uq_viral_refresh_tasks_scope"),
        sa.CheckConstraint(
            "platform IN ('douyin', 'wechat_channels')",
            name="ck_viral_refresh_tasks_platform",
        ),
        sa.CheckConstraint(
            "sort IN ('hot', 'latest')",
            name="ck_viral_refresh_tasks_sort",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_viral_refresh_tasks_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_viral_refresh_tasks_attempt"),
        sa.CheckConstraint(
            "retryable IN (0, 1)",
            name="ck_viral_refresh_tasks_retryable",
        ),
    )
    op.create_index(
        "idx_viral_refresh_tasks_claim",
        "viral_refresh_tasks",
        ["status", "locked_until", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("viral_refresh_tasks")
    op.drop_table("viral_media_preparations")
    op.drop_table("viral_import_tasks")
    op.drop_table("viral_runtime_controls")
    op.drop_table("viral_video_visibility")
    op.drop_table("viral_video_favorites")
