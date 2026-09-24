"""Browser-account health-probe scheduling columns (24h cookie freshness check).

Revision ID: 20260919T1000_browser_account_probe
Revises: 20260918T1200_publish_account_avatar

QR-login accounts (``publish_browser_accounts``) previously only flipped to
``invalid`` reactively — when a real delivery was rejected by the platform. There
was no scheduled health check, so a silently expired cookie was only discovered
at publish time. This adds a lease/CAS probe schedule mirroring the legacy
``publish_accounts`` verify queue: the publish worker claims ``connected``
accounts whose ``next_probe_at`` is due, re-runs the platform probe
(``probe_channels`` / ``probe_douyin``) against the decrypted cookie, and on
success pushes ``next_probe_at`` 24h out; on failure it marks the account
``invalid`` so the UI prompts a re-scan.
"""

from __future__ import annotations

from alembic import op

revision = "20260919T1000_browser_account_probe"
down_revision = "20260918T1200_publish_account_avatar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Registered offline archive tools still traverse this chain; runtime is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        ALTER TABLE publish_browser_accounts
            ADD COLUMN next_probe_at timestamptz,
            ADD COLUMN probe_lease_owner text,
            ADD COLUMN probe_lease_expires_at timestamptz,
            ADD COLUMN probe_attempt_count integer NOT NULL DEFAULT 0
                CHECK (probe_attempt_count >= 0);
        CREATE INDEX idx_publish_browser_accounts_probe
            ON publish_browser_accounts (next_probe_at, probe_lease_expires_at)
            WHERE status = 'connected';
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        DROP INDEX IF EXISTS idx_publish_browser_accounts_probe;
        ALTER TABLE publish_browser_accounts
            DROP COLUMN probe_attempt_count,
            DROP COLUMN probe_lease_expires_at,
            DROP COLUMN probe_lease_owner,
            DROP COLUMN next_probe_at;
    """)
