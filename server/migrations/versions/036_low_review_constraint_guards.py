"""M1/M2 review LOW (2026-08-23) — constraint-matrix and TRUNCATE guards.

Three audit-trail couplings the reviews found open, closed at the database
layer in one revision:

- ``recharge_orders.channel`` is the legacy internal payment rail; a
  customer order (``activation_code`` / ``admin_adjustment``) carrying one
  passed every 026 CHECK (T08 tests accepted ``channel='alipay'`` on an
  activation order). Now ``channel`` requires ``provider = 'zpay'``.
- ``status = 'PAID'`` without ``paid_at`` was writable (T13 always writes
  both at the application layer; the DB never enforced the pairing). Now
  a PAID row must carry its payment timestamp.
- ``admin_sessions`` ordering compared ``expires_at > created_at`` as TEXT:
  with mixed ``+08:00`` / ``Z`` offsets the comparison is unreliable (the
  029 tables already compare ``::timestamptz``). Recreated with the cast.

Plus one guard per append-only audit table: their ``BEFORE UPDATE OR
DELETE … FOR EACH ROW`` triggers do not fire for TRUNCATE, so a
statement-level ``BEFORE TRUNCATE`` trigger refuses the mass-delete path
on ``activation_code_events`` (027), ``customer_session_events`` (029) and
``security_auth_failures`` (032). Table owners can still drop the trigger
deliberately — that is an operator action, not a silent bypass.

Existing rows must already satisfy the new CHECKs (they do for every
shipped writer); violations abort the migration loudly.

PostgreSQL only (025/027 precedent): these tables never exist on the
internal SQLite lane.

Revision ID: 036_low_review_constraint_guards
Revises: 035_export_ciphertext_purge
"""

from __future__ import annotations

from alembic import op

revision = "036_low_review_constraint_guards"
down_revision = "035_export_ciphertext_purge"
branch_labels = None
depends_on = None


_TRUNCATE_GUARDS = """
CREATE FUNCTION refuse_truncate_of_audit_tables() RETURNS trigger AS $refuse$
BEGIN
    RAISE EXCEPTION '% is append-only: TRUNCATE is refused', TG_TABLE_NAME;
END;
$refuse$ LANGUAGE plpgsql;

CREATE TRIGGER trg_activation_code_events_no_truncate
BEFORE TRUNCATE ON activation_code_events
FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();

CREATE TRIGGER trg_customer_session_events_no_truncate
BEFORE TRUNCATE ON customer_session_events
FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();

CREATE TRIGGER trg_security_auth_failures_no_truncate
BEFORE TRUNCATE ON security_auth_failures
FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
"""

_DROP_TRUNCATE_GUARDS = """
DROP TRIGGER IF EXISTS trg_security_auth_failures_no_truncate ON security_auth_failures;
DROP TRIGGER IF EXISTS trg_customer_session_events_no_truncate ON customer_session_events;
DROP TRIGGER IF EXISTS trg_activation_code_events_no_truncate ON activation_code_events;
DROP FUNCTION IF EXISTS refuse_truncate_of_audit_tables();
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.create_check_constraint(
        "ck_recharge_orders_provider_channel",
        "recharge_orders",
        "channel IS NULL OR provider = 'zpay'",
    )
    op.create_check_constraint(
        "ck_recharge_orders_paid_at",
        "recharge_orders",
        "status != 'PAID' OR paid_at IS NOT NULL",
    )
    op.drop_constraint(
        "ck_admin_sessions_expires_after_created",
        "admin_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_admin_sessions_expires_after_created",
        "admin_sessions",
        "expires_at::timestamptz > created_at::timestamptz",
    )
    op.execute(_TRUNCATE_GUARDS)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(_DROP_TRUNCATE_GUARDS)
    op.drop_constraint(
        "ck_admin_sessions_expires_after_created",
        "admin_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_admin_sessions_expires_after_created",
        "admin_sessions",
        "expires_at > created_at",
    )
    op.drop_constraint("ck_recharge_orders_paid_at", "recharge_orders", type_="check")
    op.drop_constraint("ck_recharge_orders_provider_channel", "recharge_orders", type_="check")
