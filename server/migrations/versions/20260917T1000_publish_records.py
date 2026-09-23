"""Publish records (phase 2 delivery path) and browser-account status columns.

Revision ID: 20260917T1000_publish_records
Revises: 20260916T2000_prompt_optimization_receipts

``publish_records`` is the formal delivery queue reserved by 082_publish_accounts
(docs/evidence/CW002-SCOPE-DECISIONS.md §8.3): one row per video posted to one
platform account, immediately (``scheduled_at IS NULL``) or at a scheduled
time, claimed by the publish worker with the same lease/CAS pattern as account
verification. Stats / sync columns are declared here so the follow-up
result-collection task adds logic only, never DDL.

``publish_browser_accounts`` gains ``status`` / ``error_message`` so a delivery
rejected by the platform can mark the stored login state invalid, and
``source`` to distinguish cloud QR logins from desktop WebView2 exports.
"""

from __future__ import annotations

from alembic import op

revision = "20260917T1000_publish_records"
down_revision = "20260916T2000_prompt_optimization_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Registered offline archive tools still traverse this chain; runtime is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        ALTER TABLE publish_browser_accounts
            ADD COLUMN status text NOT NULL DEFAULT 'connected'
                CHECK (status IN ('connected','invalid')),
            ADD COLUMN error_message text,
            ADD COLUMN source text NOT NULL DEFAULT 'cloud'
                CHECK (source IN ('cloud','desktop'));

        CREATE TABLE publish_records (
            id text PRIMARY KEY,
            user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            account_id text REFERENCES publish_browser_accounts(id) ON DELETE SET NULL,
            platform text NOT NULL
                CHECK (platform IN ('douyin','wechat_channels','xiaohongshu')),
            video_asset_id text NOT NULL REFERENCES assets(id),
            cover_asset_id text REFERENCES assets(id),
            title text NOT NULL DEFAULT '',
            description text NOT NULL DEFAULT '',
            tags jsonb NOT NULL DEFAULT '[]'::jsonb,
            options jsonb NOT NULL DEFAULT '{}'::jsonb,
            scheduled_at timestamptz,
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','publishing','published','failed','cancelled')),
            delivery_mode text CHECK (delivery_mode IN ('api','browser')),
            platform_item_id text,
            platform_short_url text,
            platform_status text,
            stats jsonb,
            stats_synced_at timestamptz,
            sync_requested integer NOT NULL DEFAULT 0 CHECK (sync_requested IN (0,1)),
            error_message text,
            published_at timestamptz,
            lease_owner text,
            lease_expires_at timestamptz,
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX idx_publish_records_user
            ON publish_records (user_id, created_at DESC, id);
        CREATE INDEX idx_publish_records_queue
            ON publish_records (status, COALESCE(scheduled_at, created_at), lease_expires_at);
        CREATE INDEX idx_publish_records_account_active
            ON publish_records (account_id) WHERE status = 'publishing';
        CREATE INDEX idx_publish_records_sync
            ON publish_records (sync_requested, stats_synced_at) WHERE status = 'published';
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        DROP TABLE publish_records;
        ALTER TABLE publish_browser_accounts
            DROP COLUMN status, DROP COLUMN error_message, DROP COLUMN source;
    """)
