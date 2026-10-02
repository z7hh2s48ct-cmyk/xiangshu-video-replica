"""Preserve transport exception types and index provider outcomes."""

from alembic import op

revision = "20261002T0600_call_diagnostics"
down_revision = "20261002T0430_payment_methods"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE external_call_logs ADD COLUMN exception_type text")
    op.execute(
        "CREATE INDEX external_call_provider_outcome_idx "
        "ON external_call_logs (provider, outcome, created_at)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX external_call_provider_outcome_idx")
    op.execute("ALTER TABLE external_call_logs DROP COLUMN exception_type")
