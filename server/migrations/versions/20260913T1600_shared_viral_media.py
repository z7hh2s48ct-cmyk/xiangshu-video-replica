"""Publish immutable shared media objects independently of project imports."""

import sqlalchemy as sa
from alembic import op

revision = "20260913T1600_shared_viral_media"
down_revision = "20260913T1100_itemized_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Offline archive tools may traverse the chain; collection runtime is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE viral_collection_batches (
          id text PRIMARY KEY,
          platform text NOT NULL CHECK(platform IN ('douyin','wechat_channels')),
          config_json text NOT NULL,
          pricing_snapshot_json text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE viral_collection_members (
          batch_id text NOT NULL REFERENCES viral_collection_batches(id),
          user_id text NOT NULL REFERENCES users(id),
          PRIMARY KEY(batch_id,user_id)
        );
        ALTER TABLE billing_operations ADD COLUMN collection_batch_id text
          REFERENCES viral_collection_batches(id);
        CREATE INDEX idx_billing_collection ON billing_operations(collection_batch_id,created_at)
          WHERE collection_batch_id IS NOT NULL;
        CREATE TABLE viral_collection_charges (
          request_id text NOT NULL REFERENCES billing_operations(id),
          user_id text NOT NULL REFERENCES users(id),
          operation_id text UNIQUE REFERENCES billing_operations(id),
          state text NOT NULL
            CHECK(state IN ('SUCCEEDED','INSUFFICIENT_CREDITS','SKIPPED_INACTIVE')),
          due_credits integer NOT NULL CHECK(due_credits >= 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(request_id,user_id),
          CHECK((state='SUCCEEDED') = (operation_id IS NOT NULL))
        );
    """)
    op.add_column(
        "viral_runtime_controls",
        sa.Column("collection_interval_days", sa.Integer(), nullable=False, server_default="7"),
    )
    op.add_column(
        "viral_videos",
        sa.Column("homepage_featured", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("viral_videos", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column(
        "viral_videos",
        sa.Column("collection_published", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("viral_media_preparations", sa.Column("cache_scope", sa.Text()))
    op.add_column("viral_media_preparations", sa.Column("storage_uri", sa.Text()))
    op.add_column(
        "viral_runtime_controls",
        sa.Column("keywords_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "viral_runtime_controls",
        sa.Column("per_keyword_limit", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "viral_runtime_controls", sa.Column("next_collection_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "viral_refresh_tasks",
        sa.Column("collection_config_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "viral_refresh_tasks",
        sa.Column("checkpoint_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "viral_refresh_tasks",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("viral_collection_charges")
    op.drop_column("billing_operations", "collection_batch_id")
    op.drop_table("viral_collection_members")
    op.drop_table("viral_collection_batches")
    op.drop_column("viral_runtime_controls", "collection_interval_days")
    op.drop_column("viral_videos", "deleted_at")
    op.drop_column("viral_videos", "homepage_featured")
    op.drop_column("viral_videos", "collection_published")
    for column in ("retry_count", "checkpoint_json", "collection_config_json"):
        op.drop_column("viral_refresh_tasks", column)
    for column in ("next_collection_at", "per_keyword_limit", "keywords_json"):
        op.drop_column("viral_runtime_controls", column)
    op.drop_column("viral_media_preparations", "storage_uri")
    op.drop_column("viral_media_preparations", "cache_scope")
