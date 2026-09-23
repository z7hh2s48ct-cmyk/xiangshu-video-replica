"""T37 — bounded observability indexes plus fencing and authorization evidence.

The cluster-global anomaly probe runs from one session-lock-protected timer,
not from each API process. Shared ``ops_alert_state`` makes fired/resolved
edges consistent even if two OPS hosts run at different times. This revision
also gives its application-table queries index-backed entry points and extends
the existing append-only ``security_auth_failures`` fact stream with
``session:fencing``. A compact,
append-only fencing-write table keeps one expected/verified epoch fact per
session tuple so the probe can detect a stale write that actually committed.
The session-event indexes also let the probe detect a heartbeat from a
displaced epoch after a newer session LOGIN without treating pre-switch
heartbeats as concurrent devices.
No ``pg_monitor`` or ``pg_stat_*`` privilege is required.

Revision ID: 042_t37_observability_indexes
Revises: 041_user_fair_queue
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "042_t37_observability_indexes"
down_revision = "041_user_fair_queue"
branch_labels = None
depends_on = None

_OLD_DIMENSIONS = "dimension IN ('activate:ip', 'activate:code', 'login:ip', 'login:account')"
_NEW_DIMENSIONS = (
    "dimension IN ('activate:ip', 'activate:code', 'login:ip', 'login:account', "
    "'admin:exchange:ip', 'session:fencing')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # T37 observability is customer multi-instance infrastructure. The
        # internal SQLite P0 lane keeps its existing schema and local logs.
        return

    op.drop_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        _NEW_DIMENSIONS,
    )
    _add_indexed_probe_timestamps()
    op.create_index(
        "idx_customer_session_events_event_occurred_at",
        "customer_session_events",
        ["event", "occurred_at"],
    )
    op.create_index(
        "idx_customer_session_events_user_event_occurred_at_id",
        "customer_session_events",
        ["user_id", "event", "occurred_at", "id"],
    )
    op.create_index(
        "idx_recharge_orders_status_paid_at",
        "recharge_orders",
        ["status", "paid_at"],
    )
    op.create_index(
        "idx_wallet_transactions_user_created_at",
        "wallet_transactions",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_wallets_updated_at_user",
        "wallets",
        ["updated_at", "user_id"],
    )
    op.create_index(
        "idx_audit_logs_action_occurred_at",
        "audit_logs",
        ["action", "occurred_at", "id"],
    )
    op.create_index(
        "idx_generation_tasks_created_at_utc_status_batch",
        "generation_tasks",
        ["created_at_utc", "status", "batch_id"],
    )
    _create_t37_evidence_schema()


def _add_indexed_probe_timestamps() -> None:
    """Materialize absolute instants without rewriting legacy TEXT facts.

    The old columns are immutable user-visible audit history, but their
    CURRENT_TIMESTAMP rendering includes the writer session's timezone. A
    typed companion lets the bounded T37 queries use btree indexes without
    lexical timezone errors. Offset-free historical SQLite values are UTC by
    the T07 import contract, so the migration pins that interpretation only
    while backfilling.
    """
    timestamp = sa.DateTime(timezone=True)
    op.add_column(
        "customer_session_events",
        sa.Column("occurred_at", timestamp, nullable=True),
    )
    op.add_column("audit_logs", sa.Column("occurred_at", timestamp, nullable=True))
    op.add_column(
        "generation_tasks",
        sa.Column("created_at_utc", timestamp, nullable=True),
    )
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    # customer_session_events is append-only. The migration derives a new
    # column from the original fact under a transactional, temporary trigger
    # suspension; it never changes created_at or any business value.
    op.execute(
        "ALTER TABLE customer_session_events "
        "DISABLE TRIGGER trg_customer_session_events_append_only"
    )
    op.execute("UPDATE customer_session_events SET occurred_at = created_at::timestamptz")
    op.execute(
        "ALTER TABLE customer_session_events ENABLE TRIGGER trg_customer_session_events_append_only"
    )
    op.execute("UPDATE audit_logs SET occurred_at = created_at::timestamptz")
    op.execute("UPDATE generation_tasks SET created_at_utc = created_at::timestamptz")
    for table_name, column_name in (
        ("customer_session_events", "occurred_at"),
        ("audit_logs", "occurred_at"),
        ("generation_tasks", "created_at_utc"),
    ):
        op.alter_column(
            table_name,
            column_name,
            existing_type=timestamp,
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        )


def _create_t37_evidence_schema() -> None:
    op.create_table(
        "ops_alert_state",
        sa.Column("alert_name", sa.Text(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "alert_name IN ("
            "'double_online', 'duplicate_charge', 'duplicate_provider_trade_no', "
            "'paid_without_charge', 'charge_without_paid_source', "
            "'wallet_balance_mismatch', 'fencing_rejected', "
            "'stale_write_committed', 'cross_user_access', "
            "'queue_starvation', 'dangling_reserve')",
            name="ck_ops_alert_state_name",
        ),
    )
    op.create_table(
        "customer_authorization_evidence",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("actor_digest", sa.Text(), nullable=False),
        sa.Column("owner_digest", sa.Text(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 128",
            name="ck_customer_authorization_request_id",
        ),
        sa.CheckConstraint(
            "resource_type IN ('project', 'asset')",
            name="ck_customer_authorization_resource_type",
        ),
        sa.CheckConstraint(
            "actor_digest ~ '^[0-9a-f]{64}$' AND owner_digest ~ '^[0-9a-f]{64}$'",
            name="ck_customer_authorization_digests",
        ),
        sa.UniqueConstraint(
            "resource_type",
            "actor_digest",
            "owner_digest",
            name="uq_customer_authorization_digest_pair",
        ),
    )
    op.create_index(
        "idx_customer_authorization_mismatch",
        "customer_authorization_evidence",
        ["observed_at", "id"],
        postgresql_where=sa.text("actor_digest <> owner_digest"),
    )
    op.execute(
        """
        CREATE FUNCTION customer_authorization_evidence_refuse_rewrite()
        RETURNS trigger AS $refuse$
        BEGIN
            RAISE EXCEPTION 'customer_authorization_evidence is append-only';
        END;
        $refuse$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_customer_authorization_evidence_append_only
        BEFORE UPDATE OR DELETE ON customer_authorization_evidence
        FOR EACH ROW EXECUTE FUNCTION customer_authorization_evidence_refuse_rewrite();

        CREATE TRIGGER trg_customer_authorization_evidence_no_truncate
        BEFORE TRUNCATE ON customer_authorization_evidence
        FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
        """
    )
    op.create_table(
        "customer_fencing_write_evidence",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("subject_digest", sa.Text(), nullable=False),
        sa.Column("expected_session_epoch", sa.BigInteger(), nullable=False),
        sa.Column("verified_session_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "committed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 128",
            name="ck_customer_fencing_write_request_id",
        ),
        sa.CheckConstraint(
            "subject_digest ~ '^[0-9a-f]{64}$'",
            name="ck_customer_fencing_write_subject_digest",
        ),
        sa.CheckConstraint(
            "expected_session_epoch >= 1 AND verified_session_epoch >= 1",
            name="ck_customer_fencing_write_epochs",
        ),
        sa.UniqueConstraint(
            "subject_digest",
            "expected_session_epoch",
            "verified_session_epoch",
            name="uq_customer_fencing_write_epoch_fact",
        ),
    )
    op.create_index(
        "idx_customer_fencing_write_mismatch",
        "customer_fencing_write_evidence",
        ["committed_at", "id"],
        postgresql_where=sa.text("expected_session_epoch <> verified_session_epoch"),
    )
    op.execute(
        """
        CREATE FUNCTION customer_fencing_write_evidence_refuse_rewrite()
        RETURNS trigger AS $refuse$
        BEGIN
            RAISE EXCEPTION 'customer_fencing_write_evidence is append-only';
        END;
        $refuse$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_customer_fencing_write_evidence_append_only
        BEFORE UPDATE OR DELETE ON customer_fencing_write_evidence
        FOR EACH ROW EXECUTE FUNCTION customer_fencing_write_evidence_refuse_rewrite();

        CREATE TRIGGER trg_customer_fencing_write_evidence_no_truncate
        BEFORE TRUNCATE ON customer_fencing_write_evidence
        FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    has_t37_evidence = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM security_auth_failures "
            "WHERE dimension IN ('admin:exchange:ip', 'session:fencing') LIMIT 1) "
            "OR EXISTS (SELECT 1 FROM customer_fencing_write_evidence LIMIT 1) "
            "OR EXISTS (SELECT 1 FROM customer_authorization_evidence LIMIT 1)"
        )
    ).scalar()
    if has_t37_evidence:
        raise RuntimeError(
            "cannot downgrade 042_t37_observability_indexes: append-only "
            "T37 security, authorization, or committed-write evidence exists and the prior "
            "schema cannot represent it. Keep revision 042 or export and retain "
            "the security audit trail before a manually supervised rollback."
        )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_fencing_write_evidence_no_truncate "
        "ON customer_fencing_write_evidence"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_fencing_write_evidence_append_only "
        "ON customer_fencing_write_evidence"
    )
    op.execute("DROP FUNCTION IF EXISTS customer_fencing_write_evidence_refuse_rewrite()")
    op.drop_index(
        "idx_customer_fencing_write_mismatch",
        table_name="customer_fencing_write_evidence",
    )
    op.drop_table("customer_fencing_write_evidence")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_authorization_evidence_no_truncate "
        "ON customer_authorization_evidence"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_authorization_evidence_append_only "
        "ON customer_authorization_evidence"
    )
    op.execute("DROP FUNCTION IF EXISTS customer_authorization_evidence_refuse_rewrite()")
    op.drop_index(
        "idx_customer_authorization_mismatch",
        table_name="customer_authorization_evidence",
    )
    op.drop_table("customer_authorization_evidence")
    op.drop_table("ops_alert_state")
    op.drop_index(
        "idx_generation_tasks_created_at_utc_status_batch",
        table_name="generation_tasks",
    )
    op.drop_index("idx_audit_logs_action_occurred_at", table_name="audit_logs")
    op.drop_index("idx_wallets_updated_at_user", table_name="wallets")
    op.drop_index(
        "idx_wallet_transactions_user_created_at",
        table_name="wallet_transactions",
    )
    op.drop_index(
        "idx_recharge_orders_status_paid_at",
        table_name="recharge_orders",
    )
    op.drop_index(
        "idx_customer_session_events_event_occurred_at",
        table_name="customer_session_events",
    )
    op.drop_index(
        "idx_customer_session_events_user_event_occurred_at_id",
        table_name="customer_session_events",
    )
    op.drop_column("generation_tasks", "created_at_utc")
    op.drop_column("audit_logs", "occurred_at")
    op.drop_column("customer_session_events", "occurred_at")
    op.drop_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        _OLD_DIMENSIONS,
    )
