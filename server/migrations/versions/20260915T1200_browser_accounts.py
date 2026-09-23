"""Encrypted browser platform accounts and bounded temporary login sessions."""

from alembic import op

revision = "20260915T1200_browser_accounts"
down_revision = "20260915T1600_viral_copy_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Registered offline archive tools still traverse this chain; runtime is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE publish_browser_accounts (
            id text PRIMARY KEY,
            user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            platform text NOT NULL CHECK (platform IN ('douyin','wechat_channels','xiaohongshu')),
            platform_user_id text NOT NULL,
            username text NOT NULL,
            storage_state_enc text NOT NULL,
            verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (user_id, platform, platform_user_id)
        );
        CREATE TABLE publish_browser_logins (
            id text PRIMARY KEY,
            user_id text NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            platform text NOT NULL CHECK (platform IN ('douyin','wechat_channels','xiaohongshu')),
            account_id text REFERENCES publish_browser_accounts(id) ON DELETE CASCADE,
            expires_at timestamptz NOT NULL
        );
        CREATE INDEX publish_browser_login_expiry ON publish_browser_logins(expires_at);
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE publish_browser_logins; DROP TABLE publish_browser_accounts;")
