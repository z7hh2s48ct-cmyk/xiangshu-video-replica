"""T13 / ACT-05 — customer devices and activation runtime schema.

The frozen 028 topic (code checklist §3.1) is ``customer_devices_and_activations``:
the two current device slots, their credential digests and the unbind history
that outlives slot reuse. Revision 027 already landed the activation *fact*
table (``activation_code_activations``); this revision creates
``customer_devices`` itself and attaches the deferred ``first_device_id``
foreign key that 027 deliberately left dangling (append-only fix rule).

Dev doc §11.2 column contract for ``customer_devices``:

- ``id``, ``activation_code_id``, ``user_id``, ``slot_no`` (1 or 2),
  ``display_name``, ``platform``;
- ``fingerprint_hmac`` + ``fingerprint_key_version`` — the keyed digest of the
  client-supplied device fingerprint (never the raw fingerprint);
- ``token_digest`` + ``token_key_version`` — the keyed digest of the
  server-issued device credential (the plaintext token is returned once at
  bind time and never stored);
- ``status`` (``BOUND`` / ``UNBOUND`` / ``REVOKED``) with shape-coupled
  ``bound_at`` / ``unbound_at`` / ``revoked_at`` columns.

Invariants proven by PostgreSQL, not application code (dev doc §11.3,
acceptance spec §3.3):

- the current slot occupancy is a *partial* unique index on
  ``(activation_code_id, slot_no) WHERE status = 'BOUND'`` — unbound history
  rows keep their slot number without blocking reuse (the DEV-01 No-Go: an
  unconditional ``(code_id, slot_no)`` unique constraint must not exist);
- the current binding fingerprint is a partial unique index on
  ``fingerprint_hmac WHERE status = 'BOUND'`` — one physical fingerprint can
  hold at most one current binding across all customers;
- ``token_digest`` is globally unique: a revoked device credential can never
  be re-issued to another device.

``device_pairing_requests`` (the pairing half of the frozen topic) is the
T17 / DEV-02 application layer and lands as its own revision on the
then-current head, following the 026 precedent that left shared rate-limit
data to T15.

PostgreSQL is the customer production source of truth (025/026/027/031
precedent), so this revision only executes there; SQLite stays the internal
P0 runtime where customer devices do not exist.

Revision ID: 028_customer_devices_and_activations
Revises: 031_admin_write_idempotency
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028_customer_devices_and_activations"
down_revision = "031_admin_write_idempotency"
branch_labels = None
depends_on = None

_DEVICE_STATUS = "status IN ('BOUND', 'UNBOUND', 'REVOKED')"
# The three terminal-ish shapes are mutually exclusive and every state proves
# its own transition timestamp: a BOUND row carries neither release column, an
# UNBOUND row proves unbind_at, a REVOKED row proves revoked_at.
_DEVICE_STATUS_SHAPE = (
    "(status = 'BOUND' AND unbound_at IS NULL AND revoked_at IS NULL) OR "
    "(status = 'UNBOUND' AND unbound_at IS NOT NULL AND revoked_at IS NULL) OR "
    "(status = 'REVOKED' AND revoked_at IS NOT NULL AND unbound_at IS NULL)"
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
        # SQLite: internal P0 runtime — customer devices are a customer
        # production concern only, so the table simply does not exist there.
        return
    op.create_table(
        "customer_devices",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "activation_code_id",
            sa.Text(),
            sa.ForeignKey("activation_codes.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        # Keyed digest of the client fingerprint — the raw fingerprint never
        # reaches the database (acceptance spec §2.2).
        sa.Column("fingerprint_hmac", sa.Text(), nullable=False),
        sa.Column("fingerprint_key_version", sa.Integer(), nullable=False),
        # Keyed digest of the server-issued device credential — the plaintext
        # token is returned exactly once and never stored.
        sa.Column("token_digest", sa.Text(), nullable=False, unique=True),
        sa.Column("token_key_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="BOUND",
        ),
        sa.Column(
            "bound_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("last_active_at", sa.Text()),
        sa.Column("unbound_at", sa.Text()),
        sa.Column("revoked_at", sa.Text()),
        _created_at(),
        sa.CheckConstraint("slot_no IN (1, 2)", name="ck_customer_devices_slot_range"),
        sa.CheckConstraint(_DEVICE_STATUS, name="ck_customer_devices_status"),
        sa.CheckConstraint(_DEVICE_STATUS_SHAPE, name="ck_customer_devices_status_shape"),
        sa.CheckConstraint(
            "fingerprint_key_version >= 1",
            name="ck_customer_devices_fingerprint_key_version_positive",
        ),
        sa.CheckConstraint(
            "token_key_version >= 1",
            name="ck_customer_devices_token_key_version_positive",
        ),
        sa.CheckConstraint(
            "length(trim(fingerprint_hmac)) > 0",
            name="ck_customer_devices_fingerprint_hmac_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(token_digest)) > 0",
            name="ck_customer_devices_token_digest_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0",
            name="ck_customer_devices_display_name_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(platform)) > 0",
            name="ck_customer_devices_platform_not_blank",
        ),
    )
    # §11.3 / acceptance spec §3.3: only the *current* binding occupies a
    # slot — a partial unique index. Unbound/revoked history rows keep their
    # slot_no without blocking reuse, which an unconditional unique
    # constraint would (DEV-01 No-Go).
    op.create_index(
        "uq_customer_devices_slot",
        "customer_devices",
        ["activation_code_id", "slot_no"],
        unique=True,
        postgresql_where=sa.text("status = 'BOUND'"),
    )
    # One fingerprint holds at most one current binding across all customers
    # (acceptance spec §2.2); released rows free the fingerprint for rebinding
    # while keeping their audit history.
    op.create_index(
        "uq_customer_devices_fingerprint",
        "customer_devices",
        ["fingerprint_hmac"],
        unique=True,
        postgresql_where=sa.text("status = 'BOUND'"),
    )
    op.create_index(
        "idx_customer_devices_user_status",
        "customer_devices",
        ["user_id", "status"],
    )
    op.create_index(
        "idx_customer_devices_code_status",
        "customer_devices",
        ["activation_code_id", "status"],
    )
    # 027 left first_device_id dangling on purpose (append-only fix rule):
    # attach the foreign key now that the referenced table exists.
    op.create_foreign_key(
        "fk_activation_code_activations_first_device",
        "activation_code_activations",
        "customer_devices",
        ["first_device_id"],
        ["id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # An activation fact references its first device (and chains the customer
    # user, first-charge PAID order and CHARGE ledger rows); dropping the
    # device table would sever that audit chain, so refuse loudly once any
    # activation exists (027 precedent). An unused schema downgrades
    # symmetrically.
    has_activations = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM activation_code_activations)")
    ).scalar()
    if has_activations:
        raise RuntimeError(
            "cannot downgrade 028_customer_devices_and_activations: "
            "activation_code_activations already holds activation facts that "
            "reference their first device. Keep revision 028, or resolve the "
            "ledger manually before rolling back."
        )
    op.drop_constraint(
        "fk_activation_code_activations_first_device",
        "activation_code_activations",
        type_="foreignkey",
    )
    op.drop_index("idx_customer_devices_code_status", table_name="customer_devices")
    op.drop_index("idx_customer_devices_user_status", table_name="customer_devices")
    op.drop_index("uq_customer_devices_fingerprint", table_name="customer_devices")
    op.drop_index("uq_customer_devices_slot", table_name="customer_devices")
    op.drop_table("customer_devices")
