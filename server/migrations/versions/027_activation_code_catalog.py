"""T10 / ACT-01 — activation code catalog schema.

Customer V3 sells the workbench through prepaid activation codes (dev doc
§3.1): an operator mints batches of codes, delivers them through sales
channels, and each code activates exactly once — creating the customer user,
wallet, first device, the ``provider=activation_code`` PAID first-charge
order and the unique CHARGE ledger row (T13 / ACT-05). This revision lands
the data layer for that flow; T11 (CSPRNG generation, HMAC digests, AEAD
export) and T12 (management API) build the application layer on top.

Tables (dev doc §5 / §11.2, code checklist §3.3):

- ``activation_code_batches`` — batch metadata with frozen commercial
  snapshots (face value, unit price, credits) so later price changes can
  never rewrite history, an activation-expiry window and the creating actor;
- ``activation_codes`` — one row per code: only the keyed digest and a
  masked display form ever reach the database (ACT-01 No-Go: never
  plaintext), a four-state machine mirroring dev doc §12.1
  (``ISSUED`` → ``ACTIVE``; ``SUSPENDED``/``REVOKED`` side states) enforced
  by CHECK, and the binding column for the owning customer;
- ``activation_code_deliveries`` — who handed which code to which channel,
  external order and recipient reference (never the plaintext code);
- ``activation_code_exports`` — AEAD ciphertext packages with SHA-256
  integrity digest, key version, short-lived expiry and a one-time download
  audit trail (ACT-03 groundwork);
- ``activation_code_activations`` — the one-shot activation fact: unique
  per code, per user and per first-charge order (dev doc §11.3).

Invariants proven by PostgreSQL, not application code:

- ``code_digest`` is globally unique (a CSPRNG never repeats, so a duplicate
  across batches means a broken generator and must fail loudly);
- at most one currently-valid binding per user (partial unique index over
  ``ACTIVE``/``SUSPENDED`` rows; revoked rows keep their binding for audit
  and free nothing, because activation itself is a once-per-user fact);
- ``activation_code_activations.code_id`` / ``user_id`` /
  ``recharge_order_id`` are each UNIQUE (§11.3).

``first_device_id`` has no FK yet: ``customer_devices`` arrives with the
T16 device migration (028–030 are reserved for their own topics); the
append-only fix rule lets that revision attach the FK afterwards.

PostgreSQL is the customer production source of truth (025/026 precedent),
so this revision only executes there; SQLite stays the internal P0 runtime
where activation codes are not sold.

Revision ID: 027_activation_code_catalog
Revises: 026_customer_security_and_billing
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "027_activation_code_catalog"
down_revision = "026_customer_security_and_billing"
branch_labels = None
depends_on = None

_BATCH_STATUS = "status IN ('OPEN', 'CLOSED')"
# Dev doc §12.1 + acceptance spec §2.1: GENERATED is the pre-delivery state
# (T11 lands codes there before handout), ISSUED follows delivery, activation
# validates ISSUED codes and flips them to ACTIVE with a binding;
# SUSPENDED/REVOKED are operator side states (suspended_at / revoked_at prove
# the transition, revoked rows keep their binding for audit); EXPIRED is the
# unactivated end state past the batch activation window (expired_at proves
# it — a bound code never expires through this state).
_CODE_STATUS = "status IN ('GENERATED', 'ISSUED', 'ACTIVE', 'SUSPENDED', 'REVOKED', 'EXPIRED')"
_CODE_STATUS_SHAPE = (
    "(status = 'GENERATED' AND issued_at IS NULL AND bound_user_id IS NULL "
    "AND activated_at IS NULL AND suspended_at IS NULL AND revoked_at IS NULL "
    "AND expired_at IS NULL) OR "
    "(status = 'ISSUED' AND issued_at IS NOT NULL AND bound_user_id IS NULL "
    "AND activated_at IS NULL AND suspended_at IS NULL AND revoked_at IS NULL "
    "AND expired_at IS NULL) OR "
    "(status = 'ACTIVE' AND issued_at IS NOT NULL AND bound_user_id IS NOT NULL "
    "AND activated_at IS NOT NULL AND suspended_at IS NULL AND revoked_at IS NULL "
    "AND expired_at IS NULL) OR "
    "(status = 'SUSPENDED' AND suspended_at IS NOT NULL AND revoked_at IS NULL "
    "AND expired_at IS NULL) OR "
    "(status = 'REVOKED' AND revoked_at IS NOT NULL AND expired_at IS NULL) OR "
    "(status = 'EXPIRED' AND expired_at IS NOT NULL AND bound_user_id IS NULL "
    "AND activated_at IS NULL AND suspended_at IS NULL AND revoked_at IS NULL)"
)
_EVENT_TYPES = (
    "event IN ('GENERATED', 'DELIVERED', 'ACTIVATED', 'SUSPENDED', 'RESUMED', "
    "'REVOKED', 'EXPIRED', 'EXPORTED')"
)
_APPEND_ONLY_TRIGGER = """
CREATE FUNCTION activation_code_events_refuse_rewrite() RETURNS trigger AS $refuse$
BEGIN
    RAISE EXCEPTION 'activation_code_events is append-only';
END;
$refuse$ LANGUAGE plpgsql;

CREATE TRIGGER trg_activation_code_events_append_only
BEFORE UPDATE OR DELETE ON activation_code_events
FOR EACH ROW EXECUTE FUNCTION activation_code_events_refuse_rewrite();
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
        # SQLite: internal P0 runtime — activation codes are a customer
        # product only, so the catalog simply does not exist there.
        return
    _create_batches()
    _create_codes()
    _create_deliveries()
    _create_exports()
    _create_activations()
    _create_events()


def _create_batches() -> None:
    op.create_table(
        "activation_code_batches",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        # Frozen commercial snapshots (dev doc §5): later price changes must
        # never rewrite what an already-sold code is worth.
        sa.Column("face_value_fen", sa.Integer(), nullable=False),
        sa.Column("unit_price_fen_snapshot", sa.Integer(), nullable=False),
        sa.Column("credits_snapshot", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("activation_expires_at", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column(
            "created_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        _created_at(),
        sa.CheckConstraint(_BATCH_STATUS, name="ck_activation_code_batches_status"),
        sa.CheckConstraint(
            "face_value_fen > 0", name="ck_activation_code_batches_face_value_positive"
        ),
        sa.CheckConstraint(
            "unit_price_fen_snapshot >= 0",
            name="ck_activation_code_batches_unit_price_unsigned",
        ),
        sa.CheckConstraint(
            "credits_snapshot > 0",
            name="ck_activation_code_batches_credits_positive",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_activation_code_batches_quantity_positive"),
        sa.CheckConstraint(
            "length(trim(name)) > 0", name="ck_activation_code_batches_name_not_blank"
        ),
        sa.CheckConstraint(
            # created_at carries the space-separated CURRENT_TIMESTAMP default
            # while applications write ISO-8601 'T' timestamps; a lexical TEXT
            # comparison lets a same-day earlier moment pass ('T' > ' '), so
            # the CHECK must compare real timestamps (PR-review P2). Invalid
            # text fails the cast loudly — fail-closed by construction.
            "activation_expires_at::timestamptz > created_at::timestamptz",
            name="ck_activation_code_batches_expires_after_created",
        ),
    )
    op.create_index(
        "idx_activation_code_batches_status",
        "activation_code_batches",
        ["status", "created_at"],
    )


def _create_codes() -> None:
    op.create_table(
        "activation_codes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Text(),
            sa.ForeignKey("activation_code_batches.id"),
            nullable=False,
        ),
        # ACT-01 No-Go: the database never stores activation-code plaintext —
        # only the keyed digest (with its key version) and a masked form.
        sa.Column("code_digest", sa.Text(), nullable=False, unique=True),
        sa.Column("digest_key_version", sa.Integer(), nullable=False),
        sa.Column("masked_code", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            # Codes land GENERATED first (pre-delivery); delivery flips them
            # to ISSUED with issued_at. A bare INSERT without a status cannot
            # claim ISSUED, because the shape matrix would then demand
            # issued_at — fail-closed by construction.
            server_default="GENERATED",
        ),
        sa.Column("bound_user_id", sa.Text(), sa.ForeignKey("users.id")),
        # issued_at marks delivery: GENERATED codes (not yet handed out) keep
        # it NULL; every delivered state (ISSUED onwards) proves it NOT NULL.
        sa.Column("issued_at", sa.Text()),
        sa.Column("activated_at", sa.Text()),
        sa.Column("suspended_at", sa.Text()),
        sa.Column("revoked_at", sa.Text()),
        sa.Column("expired_at", sa.Text()),
        sa.CheckConstraint(_CODE_STATUS, name="ck_activation_codes_status"),
        sa.CheckConstraint(
            _CODE_STATUS_SHAPE,
            name="ck_activation_codes_status_shape",
        ),
        sa.CheckConstraint(
            # A binding and an activation timestamp always appear together —
            # §12.1 binds a code only at activation, so even SUSPENDED/REVOKED
            # rows must keep the pair consistent (PR-review P3 depth).
            "(bound_user_id IS NULL) = (activated_at IS NULL)",
            name="ck_activation_codes_binding_activation_coupled",
        ),
        sa.CheckConstraint(
            "digest_key_version >= 1",
            name="ck_activation_codes_digest_key_version_positive",
        ),
        sa.CheckConstraint(
            "length(trim(code_digest)) > 0",
            name="ck_activation_codes_digest_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(masked_code)) > 0",
            name="ck_activation_codes_masked_not_blank",
        ),
    )
    op.create_index(
        "idx_activation_codes_batch_status",
        "activation_codes",
        ["batch_id", "status"],
    )
    # ACT-01 exit gate: one user owns at most one currently-valid code. Only
    # ACTIVE/SUSPENDED rows count as a current binding; a REVOKED row keeps
    # its binding for audit without squatting the user slot.
    op.create_index(
        "uq_activation_codes_bound_user_current",
        "activation_codes",
        ["bound_user_id"],
        unique=True,
        postgresql_where=sa.text("bound_user_id IS NOT NULL AND status IN ('ACTIVE', 'SUSPENDED')"),
    )


def _create_deliveries() -> None:
    op.create_table(
        "activation_code_deliveries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "code_id",
            sa.Text(),
            sa.ForeignKey("activation_codes.id"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("external_order_ref", sa.Text()),
        sa.Column("recipient_ref", sa.Text()),
        sa.Column(
            "delivered_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "delivered_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint(
            "length(trim(channel)) > 0",
            name="ck_activation_code_deliveries_channel_not_blank",
        ),
    )
    op.create_index(
        "idx_activation_code_deliveries_code",
        "activation_code_deliveries",
        ["code_id", "delivered_at"],
    )


def _create_exports() -> None:
    op.create_table(
        "activation_code_exports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Text(),
            sa.ForeignKey("activation_code_batches.id"),
            nullable=False,
        ),
        # ACT-03: AEAD ciphertext with a SHA-256 integrity digest — exports
        # never contain plaintext codes.
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("ciphertext_sha256", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column(
            "requested_by_user_id",
            sa.Text(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        _created_at(),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("downloaded_at", sa.Text()),
        sa.Column("downloaded_by_user_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.CheckConstraint(
            "key_version >= 1",
            name="ck_activation_code_exports_key_version_positive",
        ),
        sa.CheckConstraint(
            "expires_at::timestamptz > created_at::timestamptz",
            name="ck_activation_code_exports_expires_after_created",
        ),
        sa.CheckConstraint(
            "length(trim(ciphertext)) > 0",
            name="ck_activation_code_exports_ciphertext_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(ciphertext_sha256)) > 0",
            name="ck_activation_code_exports_sha256_not_blank",
        ),
    )
    op.create_index(
        "idx_activation_code_exports_batch",
        "activation_code_exports",
        ["batch_id", "created_at"],
    )


def _create_activations() -> None:
    op.create_table(
        "activation_code_activations",
        sa.Column("id", sa.Text(), primary_key=True),
        # §11.3: activation is a one-shot fact per code and per user, and the
        # first-charge order backs exactly one activation.
        sa.Column(
            "code_id",
            sa.Text(),
            sa.ForeignKey("activation_codes.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id"),
            nullable=False,
            unique=True,
        ),
        # FK to customer_devices arrives with the T16 device revision; the
        # append-only fix rule allows attaching it once that table exists.
        sa.Column("first_device_id", sa.Text()),
        sa.Column(
            "recharge_order_id",
            sa.Text(),
            sa.ForeignKey("recharge_orders.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "activated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_activation_code_activations_user",
        "activation_code_activations",
        ["user_id", "activated_at"],
    )


def _create_events() -> None:
    # ACT-01 work package requires an append-only event table: suspension,
    # revocation, delivery and activation leave an immutable
    # actor/reason/request record that the database itself refuses to
    # rewrite — mutable status/timestamp columns alone cannot carry that
    # audit history (PR-review P1).
    op.create_table(
        "activation_code_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "code_id",
            sa.Text(),
            sa.ForeignKey("activation_codes.id"),
            nullable=False,
        ),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("reason", sa.Text()),
        sa.Column("request_id", sa.Text()),
        _created_at(),
        sa.CheckConstraint(_EVENT_TYPES, name="ck_activation_code_events_type"),
        sa.CheckConstraint(
            "length(trim(event)) > 0",
            name="ck_activation_code_events_event_not_blank",
        ),
    )
    op.create_index(
        "idx_activation_code_events_code",
        "activation_code_events",
        ["code_id", "created_at"],
    )
    op.execute(_APPEND_ONLY_TRIGGER)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # An activation fact chains the customer user, wallet, PAID first-charge
    # order and CHARGE ledger row; dropping the fact table would sever that
    # audit chain, so refuse loudly once any activation exists (026
    # precedent). An unused catalog downgrades symmetrically.
    has_activations = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM activation_code_activations)")
    ).scalar()
    if has_activations:
        raise RuntimeError(
            "cannot downgrade 027_activation_code_catalog: "
            "activation_code_activations already holds activation facts that "
            "chain the customer user, first-charge PAID order and CHARGE "
            "ledger rows. Keep revision 027, or resolve the ledger manually "
            "before rolling back."
        )
    op.drop_index("idx_activation_code_activations_user", table_name="activation_code_activations")
    op.drop_table("activation_code_activations")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_activation_code_events_append_only ON activation_code_events"
    )
    op.execute("DROP FUNCTION IF EXISTS activation_code_events_refuse_rewrite()")
    op.drop_index("idx_activation_code_events_code", table_name="activation_code_events")
    op.drop_table("activation_code_events")
    op.drop_index("idx_activation_code_exports_batch", table_name="activation_code_exports")
    op.drop_table("activation_code_exports")
    op.drop_index(
        "idx_activation_code_deliveries_code",
        table_name="activation_code_deliveries",
    )
    op.drop_table("activation_code_deliveries")
    op.drop_index("uq_activation_codes_bound_user_current", table_name="activation_codes")
    op.drop_index("idx_activation_codes_batch_status", table_name="activation_codes")
    op.drop_table("activation_codes")
    op.drop_index("idx_activation_code_batches_status", table_name="activation_code_batches")
    op.drop_table("activation_code_batches")
