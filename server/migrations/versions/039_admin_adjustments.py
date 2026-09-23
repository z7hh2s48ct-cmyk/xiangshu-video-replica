"""T23 / BILL-02 — audited admin adjustments (审计化后台调账).

Task list §4 T23 exit gate: *双确认、来源单、幂等、真实 actor；禁止直接改余额*.
The adjustment lands as one atomic transaction that writes the `PAID`
`provider='admin_adjustment'` recharge order (revision 026 shapes), the wallet
`CHARGE` ledger row, and one append-only ``admin_adjustments`` audit row naming
the real acting administrator (dev doc §15).

This migration creates:
- ``admin_adjustments`` — append-only audit table with CHECK constraints for
  source_document_type enum and non-blank fields (refuse malformed rows);
- Foreign key to ``recharge_orders`` ensuring one audit row per adjustment order;
- Indexes by target_user_id, admin_user_id, created_at for audit trail queries.

PostgreSQL is the customer production source of truth (025+ precedent),
so this revision only executes there; SQLite stays the internal P0 runtime
where admin adjustments do not exist.

Revision ID: 039_admin_adjustments
Revises: 038_admin_device_operations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "039_admin_adjustments"
down_revision = "038_admin_device_operations"
branch_labels = None
depends_on = None

_SOURCE_DOCUMENT_TYPES = (
    "source_document_type IN ('CS_TICKET', 'REFUND_APPROVAL', "
    "'COMPENSATION_APPROVAL', 'LEDGER_CORRECTION')"
)


def _created_at() -> sa.Column[str]:
    return sa.Column(
        "created_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — admin adjustments are a customer-production concern only.
        return

    # Create the append-only audit table
    op.create_table(
        "admin_adjustments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "recharge_order_id",
            sa.Text(),
            # RESTRICT (the 038 precedent: plain FK): CASCADE would silently
            # erase audit rows whenever the row trigger is suspended
            # (e.g. session_replication_role = replica), destroying the
            # append-only lineage this table exists to protect.
            sa.ForeignKey("recharge_orders.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,  # One audit row per adjustment order
        ),
        sa.Column(
            "target_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "admin_user_id",
            sa.Text(),
            # RESTRICT, not SET NULL: the actor is NOT NULL by design — a
            # deleted admin with audit rows must fail loudly, not silently
            # orphan the audit trail.
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="The real acting administrator (§15)",
        ),
        sa.Column(
            "source_document_type",
            sa.Text(),
            nullable=False,
            comment="Enum type of source document (来源单)",
        ),
        sa.Column(
            "source_document_ref",
            sa.Text(),
            nullable=False,
            comment="Non-blank reference to the source document",
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint(_SOURCE_DOCUMENT_TYPES, name="ck_admin_adjustments_source_type"),
        sa.CheckConstraint(
            "length(trim(source_document_ref)) > 0",
            name="ck_admin_adjustments_doc_ref_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_admin_adjustments_reason_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(request_id)) > 0",
            name="ck_admin_adjustments_request_id_not_blank",
        ),
    )

    # Indexes for audit trail performance
    op.create_index(
        "idx_admin_adjustments_target_user",
        "admin_adjustments",
        ["target_user_id", "created_at"],
    )
    op.create_index(
        "idx_admin_adjustments_admin",
        "admin_adjustments",
        ["admin_user_id", "created_at"],
    )
    op.create_index(
        "idx_admin_adjustments_created_at",
        "admin_adjustments",
        ["created_at"],
    )

    # Append-only trigger (same pattern as 038 admin_device_events)
    _APPEND_ONLY_TRIGGER = """
CREATE FUNCTION admin_adjustments_refuse_rewrite() RETURNS trigger AS $refuse$
BEGIN
    RAISE EXCEPTION 'admin_adjustments is append-only';
END;
$refuse$ LANGUAGE plpgsql;

CREATE TRIGGER trg_admin_adjustments_append_only
BEFORE UPDATE OR DELETE ON admin_adjustments
FOR EACH ROW EXECUTE FUNCTION admin_adjustments_refuse_rewrite();
"""

    # 036 created the shared refusal function; this revision adds its own trigger
    _TRUNCATE_GUARD_TRIGGER = """
CREATE TRIGGER trg_admin_adjustments_no_truncate
BEFORE TRUNCATE ON admin_adjustments
FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
"""

    op.execute(_APPEND_ONLY_TRIGGER)
    op.execute(_TRUNCATE_GUARD_TRIGGER)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Refuse downgrade if any audit data exists (preserve lineage)
    has_adjustments = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM admin_adjustments)")
    ).scalar()
    if has_adjustments:
        raise RuntimeError(
            "cannot downgrade 039_admin_adjustments: admin adjustment audit data must survive "
            "the rollback. Keep revision 039, or manually export the audit trail "
            "before rolling back."
        )

    op.execute("DROP TRIGGER IF EXISTS trg_admin_adjustments_no_truncate ON admin_adjustments")
    op.execute("DROP TRIGGER IF EXISTS trg_admin_adjustments_append_only ON admin_adjustments")
    op.execute("DROP FUNCTION IF EXISTS admin_adjustments_refuse_rewrite()")
    op.drop_index("idx_admin_adjustments_created_at", table_name="admin_adjustments")
    op.drop_index("idx_admin_adjustments_admin", table_name="admin_adjustments")
    op.drop_index("idx_admin_adjustments_target_user", table_name="admin_adjustments")
    op.drop_table("admin_adjustments")
