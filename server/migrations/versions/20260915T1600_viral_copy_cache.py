"""Cache shared viral-video transcripts independently of project/task lifetime."""

from alembic import op

revision = "20260915T1600_viral_copy_cache"
down_revision = "20260914T0000_local_joint_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Offline history tools may traverse the chain; runtime caching is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE viral_script_cache (
            platform text NOT NULL,
            video_id text NOT NULL,
            producer_task_id text,
            result_json text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (platform, video_id)
        )
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("viral_script_cache")
