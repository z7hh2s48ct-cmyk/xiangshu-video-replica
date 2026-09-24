"""T18 — administrator device operations: verified approval, unbind, revocation.

Task list §4 T18 exit gate: *实现管理员核验批准、解绑和凭据撤销* with *真实
actor、原因、二次确认和审计存在*. Three lanes land here (dev doc §12.2 step 3,
§9.2, §15):

- the **admin-verified pairing approval** — §12.2 step 3 routes the
  first-device-unavailable case to a real administrator who verifies the
  delivery records and then approves through the same four-state machine;
  the pairing row therefore grows ``approved_by_admin_user_id`` alongside
  the device-approver column, and the shape CHECK is generalized: an
  approved row proves ``approved_at`` plus *exactly one* approver
  (device or administrator — never both, never neither);
- the **administrator unbind** and the **credential revocation** — §9.2
  requires both to invalidate the current session atomically; they land in
  the application layer (T16's ``unbind_device`` core, the ``REVOKED``
  terminal state of revision 028) and are audited here;
- ``admin_device_events`` — the append-only audit table carrying the real
  ``admin_user_id`` actor, the reason, the request id and the target
  identifiers for every one of the three operations (the 029 append-only
  trigger precedent + the 036 TRUNCATE guard).

PostgreSQL is the customer production source of truth (025–037 precedent),
so this revision only executes there; SQLite stays the internal P0 runtime
where customer devices and admin sessions do not exist.

Revision ID: 038_admin_device_operations
Revises: 037_device_pairing_requests
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "038_admin_device_operations"
down_revision = "037_device_pairing_requests"
branch_labels = None
depends_on = None

_ADMIN_DEVICE_EVENT_TYPES = (
    "event IN ('PAIRING_ADMIN_APPROVED', 'DEVICE_ADMIN_UNBOUND', 'DEVICE_CREDENTIAL_REVOKED')"
)
# The approval-lineage shape shared by every non-PENDING arm: approved_at is
# set exactly when one — and only one — approver column is set. The device
# lane and the administrator lane are mutually exclusive by construction
# (§12.2 step 3): an approval recorded with both or neither approver is a
# malformed audit row (the 037 half-pair precedent, generalized).
_APPROVAL_LINEAGE = (
    "((approved_at IS NULL AND approved_by_device_id IS NULL "
    "AND approved_by_admin_user_id IS NULL) OR "
    "(approved_at IS NOT NULL AND (approved_by_device_id IS NULL) "
    "!= (approved_by_admin_user_id IS NULL)))"
)
_PAIRING_STATUS_SHAPE = (
    "(status = 'PENDING' AND approved_at IS NULL AND approved_by_device_id IS NULL "
    "AND approved_by_admin_user_id IS NULL "
    "AND consumed_at IS NULL AND consumed_device_id IS NULL) OR "
    "(status = 'APPROVED' AND " + _APPROVAL_LINEAGE + " "
    "AND consumed_at IS NULL AND consumed_device_id IS NULL) OR "
    "(status = 'CONSUMED' AND " + _APPROVAL_LINEAGE + " "
    "AND consumed_at IS NOT NULL AND consumed_device_id IS NOT NULL) OR "
    "(status = 'EXPIRED' AND consumed_at IS NULL AND consumed_device_id IS NULL "
    "AND " + _APPROVAL_LINEAGE + ")"
)
# Every event proves the identifiers its lane actually touched: an approval
# names the pairing, an unbind/revocation names the device.
_EVENT_TARGET_SHAPE = (
    "((event = 'PAIRING_ADMIN_APPROVED' AND pairing_request_id IS NOT NULL "
    "AND device_id IS NULL) OR "
    "(event IN ('DEVICE_ADMIN_UNBOUND', 'DEVICE_CREDENTIAL_REVOKED') "
    "AND device_id IS NOT NULL AND pairing_request_id IS NULL))"
)

_APPEND_ONLY_TRIGGER = """
CREATE FUNCTION admin_device_events_refuse_rewrite() RETURNS trigger AS $refuse$
BEGIN
    RAISE EXCEPTION 'admin_device_events is append-only';
END;
$refuse$ LANGUAGE plpgsql;

CREATE TRIGGER trg_admin_device_events_append_only
BEFORE UPDATE OR DELETE ON admin_device_events
FOR EACH ROW EXECUTE FUNCTION admin_device_events_refuse_rewrite();
"""
# 036 created the shared refusal function; this revision only adds the
# trigger for its own table (the downgrade drops the trigger, never the
# shared function — 036 owns it).
_TRUNCATE_GUARD_TRIGGER = """
CREATE TRIGGER trg_admin_device_events_no_truncate
BEFORE TRUNCATE ON admin_device_events
FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
"""


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
        # SQLite: internal P0 runtime — administrator device operations are
        # a customer-production concern only.
        return
    op.add_column(
        "device_pairing_requests",
        sa.Column("approved_by_admin_user_id", sa.Text(), sa.ForeignKey("users.id")),
    )
    # Generalize the four-state shape coupling to the dual-approver world
    # (drop + recreate: the constraint itself is not schema-visible data).
    op.drop_constraint(
        "ck_device_pairing_requests_status_shape",
        "device_pairing_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_device_pairing_requests_status_shape",
        "device_pairing_requests",
        _PAIRING_STATUS_SHAPE,
    )
    op.create_table(
        "admin_device_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("event", sa.Text(), nullable=False),
        # The real acting operator (§15): every event names its admin.
        sa.Column(
            "admin_user_id",
            sa.Text(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # The affected customer (device owner / pairing code owner).
        sa.Column(
            "target_user_id",
            sa.Text(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.Text(),
            sa.ForeignKey("customer_devices.id"),
        ),
        sa.Column(
            "pairing_request_id",
            sa.Text(),
            sa.ForeignKey("device_pairing_requests.id"),
        ),
        sa.Column(
            "activation_code_id",
            sa.Text(),
            sa.ForeignKey("activation_codes.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            _ADMIN_DEVICE_EVENT_TYPES,
            name="ck_admin_device_events_type",
        ),
        sa.CheckConstraint(
            _EVENT_TARGET_SHAPE,
            name="ck_admin_device_events_target_shape",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_admin_device_events_reason_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(request_id)) > 0",
            name="ck_admin_device_events_request_id_not_blank",
        ),
    )
    op.create_index(
        "idx_admin_device_events_device",
        "admin_device_events",
        ["device_id", "created_at"],
    )
    op.create_index(
        "idx_admin_device_events_pairing",
        "admin_device_events",
        ["pairing_request_id", "created_at"],
    )
    op.create_index(
        "idx_admin_device_events_admin",
        "admin_device_events",
        ["admin_user_id", "created_at"],
    )
    op.execute(_APPEND_ONLY_TRIGGER)
    op.execute(_TRUNCATE_GUARD_TRIGGER)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Audit lineage guards (the 037 precedent): the event rows are the
    # evidence of who approved/unbound/revoked what, and a pairing row
    # approved by an administrator would lose its only approver record when
    # the column drops — refuse loudly once either exists.
    has_events = bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM admin_device_events)")).scalar()
    has_admin_approvals = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM device_pairing_requests "
            "WHERE approved_by_admin_user_id IS NOT NULL)"
        )
    ).scalar()
    if has_events or has_admin_approvals:
        raise RuntimeError(
            "cannot downgrade 038_admin_device_operations: admin device "
            "audit lineage (admin_device_events rows or administrator-"
            "approved pairings) must survive the rollback. Keep revision "
            "038, or export the audit trail manually before rolling back."
        )
    op.execute("DROP TRIGGER IF EXISTS trg_admin_device_events_no_truncate ON admin_device_events")
    op.execute("DROP TRIGGER IF EXISTS trg_admin_device_events_append_only ON admin_device_events")
    op.execute("DROP FUNCTION IF EXISTS admin_device_events_refuse_rewrite()")
    op.drop_index("idx_admin_device_events_admin", table_name="admin_device_events")
    op.drop_index("idx_admin_device_events_pairing", table_name="admin_device_events")
    op.drop_index("idx_admin_device_events_device", table_name="admin_device_events")
    op.drop_table("admin_device_events")
    op.drop_constraint(
        "ck_device_pairing_requests_status_shape",
        "device_pairing_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_device_pairing_requests_status_shape",
        "device_pairing_requests",
        "(status = 'PENDING' AND approved_at IS NULL AND approved_by_device_id IS NULL "
        "AND consumed_at IS NULL AND consumed_device_id IS NULL) OR "
        "(status = 'APPROVED' AND approved_at IS NOT NULL "
        "AND approved_by_device_id IS NOT NULL "
        "AND consumed_at IS NULL AND consumed_device_id IS NULL) OR "
        "(status = 'CONSUMED' AND approved_at IS NOT NULL "
        "AND approved_by_device_id IS NOT NULL "
        "AND consumed_at IS NOT NULL AND consumed_device_id IS NOT NULL) OR "
        "(status = 'EXPIRED' AND consumed_at IS NULL AND consumed_device_id IS NULL "
        "AND (approved_at IS NULL) = (approved_by_device_id IS NULL))",
    )
    op.drop_column("device_pairing_requests", "approved_by_admin_user_id")
