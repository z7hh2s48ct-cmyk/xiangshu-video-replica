"""Avatar for publish browser accounts.

Revision ID: 20260918T1200_publish_account_avatar
Revises: 20260917T1000_publish_records

A connected account is currently shown as a nickname plus an opaque platform
user id, which is hard to tell apart when several accounts are connected on the
same platform. The identity response both QR-login paths already parse also
carries the account's avatar, so no extra platform call is needed.

The column holds a URI for an object in our own storage, not a platform CDN
link: those links rate-limit and expire, so the import path copies the image
once (see ``app/publish_avatars.py``) before the URI is stored. Nullable because
re-hosting is best effort and older rows predate avatars.
"""

from __future__ import annotations

from alembic import op

revision = "20260918T1200_publish_account_avatar"
down_revision = "20260917T1000_publish_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Registered offline archive tools still traverse this chain; runtime is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE publish_browser_accounts ADD COLUMN avatar_url text")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE publish_browser_accounts DROP COLUMN avatar_url")
