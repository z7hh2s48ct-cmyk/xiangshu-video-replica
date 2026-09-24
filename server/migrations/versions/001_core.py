from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "001_core"
down_revision = None
branch_labels = None
depends_on = None


def _created_at() -> sa.Column[str]:
    return sa.Column(
        "created_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _updated_at() -> sa.Column[str]:
    return sa.Column(
        "updated_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="employee"),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("is_active IN (0, 1)"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("owner_user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        _created_at(),
        _updated_at(),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.Text()),
        sa.Column(
            "created_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        _created_at(),
        sa.CheckConstraint("size_bytes >= 0"),
    )
    op.create_table(
        "versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_id", sa.Text(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        _created_at(),
        sa.CheckConstraint("version_number > 0"),
        sa.UniqueConstraint("project_id", "kind", "version_number"),
    )
    op.create_table(
        "generation_batches",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("request_snapshot_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("created_by_user_id", "idempotency_key"),
    )
    op.create_table(
        "generation_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Text(),
            sa.ForeignKey("generation_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation_mode", sa.Text(), nullable=False, server_default="I2V"),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider_task_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_provider_status", sa.Text()),
        sa.Column("last_polled_at", sa.Text()),
        sa.Column("next_poll_at", sa.Text()),
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.Text()),
        sa.Column(
            "provider_response_asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
        ),
        sa.Column("archive_status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("quality_status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("quality_issue_codes", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        sa.Column("result_asset_id", sa.Text(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("estimated_cost", sa.Float()),
        sa.Column("actual_cost", sa.Float()),
        sa.Column("submitted_at", sa.Text()),
        sa.Column("started_at", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("attempt >= 0"),
    )
    op.create_index(
        "idx_generation_tasks_lease",
        "generation_tasks",
        ["status", "locked_until", "next_poll_at", "created_at"],
    )
    op.create_table(
        "external_call_logs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "generation_task_id",
            sa.Text(),
            sa.ForeignKey("generation_tasks.id", ondelete="SET NULL"),
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("endpoint_name", sa.Text(), nullable=False),
        sa.Column("provider_request_id", sa.Text()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("request_hash", sa.Text()),
        sa.Column("response_asset_id", sa.Text(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message_redacted", sa.Text()),
        _created_at(),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("actor_user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        _created_at(),
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("external_call_logs")
    op.drop_index("idx_generation_tasks_lease", table_name="generation_tasks")
    op.drop_table("generation_tasks")
    op.drop_table("generation_batches")
    op.drop_table("versions")
    op.drop_table("assets")
    op.drop_table("projects")
    op.drop_table("users")
