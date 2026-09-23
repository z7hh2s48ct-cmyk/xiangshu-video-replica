"""T17 / DEV-02 — one-shot second-device pairing requests.

Revision 028 created ``customer_devices`` and deliberately left
``device_pairing_requests`` — the pairing half of the frozen topic — to land
as its own revision on the then-current head (the 026 precedent that left
shared rate-limit data to T15). This is that revision: drafted as 033 on the
032 head, then renumbered to 037 when main gained the PR #48 chain
(033 batch-creation audit → 036 guard rails) — Alembic takes its order from
``down_revision``, never from the file name.

Dev doc §12.2 (the six-step second-device contract) maps onto the table:

- the second device submits the *main* activation code plus a candidate
  device digest; the server never re-charges (DEV-02 No-Go), so the pairing
  row carries no wallet or order columns at all;
- a pairing request is created *bound to the candidate digest* — the
  approval is not transferable to another fingerprint (acceptance spec §6);
- the first currently-bound device approves (the ``approved_by_device_id``
  lineage); the admin-verification approval lane is T18 and writes through
  the same state machine;
- consuming the request locks the code row, the current device rows and the
  pairing row, then takes the free slot 1/2 (the application layer calls
  ``next_free_slot`` inside the code-row lock);
- the request is one-shot: ``CONSUMED`` is terminal, ``EXPIRED`` is the lazy
  terminal for unapproved or un-consumed requests past ``expires_at``.

Status shape coupling (the 028 three-state precedent, generalized to four):
``PENDING`` proves neither transition column; ``APPROVED`` proves
``approved_at``/``approved_by_device_id`` and no consumption;
``CONSUMED`` proves both transitions and ``consumed_device_id``;
``EXPIRED`` proves no consumption (it may carry an approval that lapsed —
but the approval columns must still appear as a pair: a lone
``approved_at`` or ``approved_by_device_id`` is a malformed audit row,
PR #49 Codex review P2).

The partial unique index keeps at most one *active* pairing request per
(activation code, candidate digest): retried enrolls reuse the existing
PENDING/APPROVED row instead of stacking duplicates, while terminal rows
never block a fresh request after expiry.

PostgreSQL is the customer production source of truth (025–036 precedent),
so this revision only executes there; SQLite stays the internal P0 runtime
where customer devices do not exist.

Revision ID: 037_device_pairing_requests
Revises: 036_low_review_constraint_guards
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "037_device_pairing_requests"
down_revision = "036_low_review_constraint_guards"
branch_labels = None
depends_on = None

_PAIRING_STATUS = "status IN ('PENDING', 'APPROVED', 'CONSUMED', 'EXPIRED')"
# Four-state shape coupling: every state proves its own transition columns.
# EXPIRED may carry an approval that lapsed before consumption, so only the
# consumption columns are constrained there — plus the approval columns must
# arrive as a pair (both NULL or both set), or a lapsed approval could be
# recorded half-written (PR #49 Codex review P2).
_PAIRING_STATUS_SHAPE = (
    "(status = 'PENDING' AND approved_at IS NULL AND approved_by_device_id IS NULL "
    "AND consumed_at IS NULL AND consumed_device_id IS NULL) OR "
    "(status = 'APPROVED' AND approved_at IS NOT NULL "
    "AND approved_by_device_id IS NOT NULL "
    "AND consumed_at IS NULL AND consumed_device_id IS NULL) OR "
    "(status = 'CONSUMED' AND approved_at IS NOT NULL "
    "AND approved_by_device_id IS NOT NULL "
    "AND consumed_at IS NOT NULL AND consumed_device_id IS NOT NULL) OR "
    "(status = 'EXPIRED' AND consumed_at IS NULL AND consumed_device_id IS NULL "
    "AND (approved_at IS NULL) = (approved_by_device_id IS NULL))"
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
        # SQLite: internal P0 runtime — second-device pairing is a customer
        # production concern only.
        return
    op.create_table(
        "device_pairing_requests",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "activation_code_id",
            sa.Text(),
            sa.ForeignKey("activation_codes.id"),
            nullable=False,
        ),
        # The keyed digest of the *candidate* device fingerprint — the raw
        # fingerprint never reaches the database (acceptance spec §2.2), and
        # the pairing is bound to this digest so an approval cannot be
        # transferred to a different device (acceptance spec §6).
        sa.Column("candidate_fingerprint_hmac", sa.Text(), nullable=False),
        sa.Column("candidate_fingerprint_key_version", sa.Integer(), nullable=False),
        # The candidate's self-reported identity, shown to the approving
        # first device and copied onto the customer_devices row at
        # consumption time.
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.Text()),
        sa.Column(
            "approved_by_device_id",
            sa.Text(),
            sa.ForeignKey("customer_devices.id"),
        ),
        sa.Column("consumed_at", sa.Text()),
        sa.Column(
            "consumed_device_id",
            sa.Text(),
            sa.ForeignKey("customer_devices.id"),
        ),
        _created_at(),
        sa.CheckConstraint(_PAIRING_STATUS, name="ck_device_pairing_requests_status"),
        sa.CheckConstraint(_PAIRING_STATUS_SHAPE, name="ck_device_pairing_requests_status_shape"),
        sa.CheckConstraint(
            "candidate_fingerprint_key_version >= 1",
            name="ck_device_pairing_requests_key_version_positive",
        ),
        sa.CheckConstraint(
            "length(trim(candidate_fingerprint_hmac)) > 0",
            name="ck_device_pairing_requests_fingerprint_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0",
            name="ck_device_pairing_requests_display_name_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(platform)) > 0",
            name="ck_device_pairing_requests_platform_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(expires_at)) > 0",
            name="ck_device_pairing_requests_expires_not_blank",
        ),
    )
    # At most one *active* pairing request per (code, candidate digest):
    # retried enrolls reuse the existing row; terminal (CONSUMED/EXPIRED)
    # rows never block a fresh request (the DEV-01 partial-unique precedent
    # for release-aware constraints).
    op.create_index(
        "uq_device_pairing_requests_active",
        "device_pairing_requests",
        ["activation_code_id", "candidate_fingerprint_hmac"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'APPROVED')"),
    )
    # The enroll path walks (activation_code_id, status) to find the active
    # request for a candidate digest; the lazy expiry sweep walks the same
    # shape.
    op.create_index(
        "idx_device_pairing_requests_code_status",
        "device_pairing_requests",
        ["activation_code_id", "status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # A pairing row is the audit evidence of who approved the second device
    # and which binding consumed the approval: dropping the table on a live
    # host would destroy that lineage. Refuse loudly once any row exists
    # (027/028/032 guard precedent); an unused schema downgrades
    # symmetrically.
    has_rows = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM device_pairing_requests)")
    ).scalar()
    if has_rows:
        raise RuntimeError(
            "cannot downgrade 037_device_pairing_requests: the table already "
            "holds pairing approval lineage that must survive the rollback. "
            "Keep revision 037, or export the pairing audit trail manually "
            "before rolling back."
        )
    op.drop_index("idx_device_pairing_requests_code_status", table_name="device_pairing_requests")
    op.drop_index("uq_device_pairing_requests_active", table_name="device_pairing_requests")
    op.drop_table("device_pairing_requests")
