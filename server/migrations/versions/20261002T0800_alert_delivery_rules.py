"""Independent alert rules and durable delivery outcomes; existing recipients stay unchanged."""

from alembic import op

revision = "20261002T0800_alert_delivery_rules"
down_revision = "20261002T0630_task_diagnostic_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE alert_settings ADD COLUMN notification_policies_json"
        " jsonb NOT NULL DEFAULT '[]'"
    )
    op.execute(
        "ALTER TABLE alert_settings ADD COLUMN failure_rules_json jsonb NOT NULL DEFAULT '[]'"
    )
    op.execute("""CREATE TABLE alert_deliveries (
        event_key text NOT NULL, recipient_key text NOT NULL,
        alert_key text NOT NULL, channel text NOT NULL DEFAULT 'email',
        state text NOT NULL CHECK(state IN ('CLAIMED','SENT','FAILED','UNCONFIGURED')),
        lease_token text, lease_until timestamptz, attempts integer NOT NULL DEFAULT 0,
        last_error text, last_attempt_at timestamptz, sent_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(event_key,recipient_key))""")
    op.execute("CREATE INDEX alert_delivery_status_idx ON alert_deliveries(state,updated_at)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE alert_deliveries")
    op.execute("ALTER TABLE alert_settings DROP COLUMN failure_rules_json")
    op.execute("ALTER TABLE alert_settings DROP COLUMN notification_policies_json")
