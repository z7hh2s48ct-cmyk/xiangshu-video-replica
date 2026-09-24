from __future__ import annotations

from alembic import op

revision = "024_wallet_backfill"
down_revision = "023_zpay_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO wallets (user_id)
        SELECT users.id
        FROM users
        LEFT JOIN wallets ON wallets.user_id = users.id
        WHERE wallets.user_id IS NULL
        """
    )


def downgrade() -> None:
    # Keep repaired wallet rows: they are valid under revision 023 and may have
    # received real ledger activity after this migration ran.
    pass
