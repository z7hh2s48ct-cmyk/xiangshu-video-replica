"""T13 / ACT-05 — customer sessions and idempotency envelopes.

The frozen code-checklist topics (§3.1): the *首次激活原子事务* (T13) writes
its session and idempotency tail into this revision's tables, and the later
T14/T19/T20 单设备单在线 family builds on the same data layer. Chained off
``028_customer_devices_and_activations``:

- ``customer_session_state`` — the single current online session per user
  (§11.2): ``user_id`` is the primary key and ``activation_code_id`` is
  unique, so the "one live session row" invariant is proven by the schema
  itself, never by application code (§11.3). The session epoch may only
  move forward — a BEFORE UPDATE trigger refuses any decrease, which is
  what makes fencing after a device switch sound (§12.3/§12.4: a stale
  device's write must never observe its epoch coming back);
- ``customer_session_events`` — append-only session audit (ACTIVATED,
  LOGIN, SWITCH, LOGOUT, TIMEOUT, HEARTBEAT) with the epoch snapshot at
  each event; the database refuses UPDATE/DELETE outright (027 precedent
  for audit immutability);
- ``customer_idempotency_envelopes`` — the client idempotency contract
  (§12.1): exactly one envelope per (operation, scope, key digest). The
  raw client key never reaches the database — only its digest. A complete
  envelope carries the AEAD-encrypted one-time response, the key version
  that decrypts it and the recovery window, proven coupled by CHECK;
  ``purged_at`` records the post-window cleanup (the T14 cleanup job ships
  later — the column lands now so the frozen topic stays whole).

PostgreSQL is the customer production source of truth (025–028/031
precedent); SQLite stays the internal P0 runtime where customers do not
exist, so this revision only executes there.

Revision ID: 029_customer_sessions_and_idempotency
Revises: 028_customer_devices_and_activations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029_customer_sessions_and_idempotency"
down_revision = "028_customer_devices_and_activations"
branch_labels = None
depends_on = None

_SESSION_EVENT_TYPES = "event IN ('ACTIVATED', 'LOGIN', 'SWITCH', 'LOGOUT', 'TIMEOUT', 'HEARTBEAT')"
_APPEND_ONLY_TRIGGER = """
CREATE FUNCTION customer_session_events_refuse_rewrite() RETURNS trigger AS $refuse$
BEGIN
    RAISE EXCEPTION 'customer_session_events is append-only';
END;
$refuse$ LANGUAGE plpgsql;

CREATE TRIGGER trg_customer_session_events_append_only
BEFORE UPDATE OR DELETE ON customer_session_events
FOR EACH ROW EXECUTE FUNCTION customer_session_events_refuse_rewrite();
"""
# §11.3: the session epoch only moves forward. Renewals keep the epoch
# (§12.3 same-device lease extension); switches and token re-issues raise it;
# nothing may ever lower it — a decrease would resurrect a fenced-out device.
_EPOCH_MONOTONIC_TRIGGER = """
CREATE FUNCTION customer_session_state_epoch_monotonic() RETURNS trigger AS $epoch$
BEGIN
    IF NEW.session_epoch < OLD.session_epoch THEN
        RAISE EXCEPTION 'customer_session_state.session_epoch must never decrease';
    END IF;
    RETURN NEW;
END;
$epoch$ LANGUAGE plpgsql;

CREATE TRIGGER trg_customer_session_state_epoch_monotonic
BEFORE UPDATE ON customer_session_state
FOR EACH ROW EXECUTE FUNCTION customer_session_state_epoch_monotonic();
"""
# A recoverable envelope always carries the AEAD ciphertext, the key version
# that decrypts it and the recovery window together; an incomplete row
# (in-flight placeholder, or purged) carries none of them.
_ENVELOPE_PAYLOAD_COUPLING = (
    "((ciphertext IS NULL) = (recovery_expires_at IS NULL)) "
    "AND ((ciphertext IS NULL) = (key_version IS NULL))"
)
# A purged envelope must have had its ciphertext removed (T14 cleanup);
# unpurged rows may still carry a recoverable payload.
_ENVELOPE_PURGE_COUPLING = "(purged_at IS NULL) OR (ciphertext IS NULL)"


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
        # SQLite: internal P0 runtime — customer sessions and idempotency
        # envelopes are a customer-production concern only.
        return
    _create_session_state()
    _create_session_events()
    _create_idempotency_envelopes()


def _create_session_state() -> None:
    op.create_table(
        "customer_session_state",
        # §11.3: one live session per user — the primary key *is* the
        # invariant; the activation transaction only ever INSERTs this row
        # once and later flows UPDATE it in place.
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), primary_key=True),
        # §11.3: one session row per activation code (one code per user).
        sa.Column(
            "activation_code_id",
            sa.Text(),
            sa.ForeignKey("activation_codes.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "device_id",
            sa.Text(),
            sa.ForeignKey("customer_devices.id"),
            nullable=False,
        ),
        # §7: session credentials are keyed digests, never plaintext tokens.
        sa.Column("session_id", sa.Text(), nullable=False, unique=True),
        sa.Column("token_digest", sa.Text(), nullable=False, unique=True),
        sa.Column("session_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.Text(), nullable=False),
        sa.Column("last_heartbeat_at", sa.Text()),
        _created_at(),
        sa.Column("updated_at", sa.Text()),
        sa.CheckConstraint(
            "session_epoch >= 1",
            name="ck_customer_session_state_epoch_positive",
        ),
        sa.CheckConstraint(
            "length(trim(session_id)) > 0",
            name="ck_customer_session_state_session_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(token_digest)) > 0",
            name="ck_customer_session_state_token_digest_not_blank",
        ),
        sa.CheckConstraint(
            "lease_until::timestamptz > created_at::timestamptz",
            name="ck_customer_session_state_lease_after_created",
        ),
        sa.CheckConstraint(
            "last_heartbeat_at IS NULL "
            "OR last_heartbeat_at::timestamptz >= created_at::timestamptz",
            name="ck_customer_session_state_heartbeat_not_before_created",
        ),
    )
    # PR #44 review P2: install the epoch-monotonicity trigger — the DDL was
    # defined above but never executed, so the database invariant (epoch may
    # never decrease, §11.3) was not actually enforced on upgraded databases.
    op.execute(_EPOCH_MONOTONIC_TRIGGER)


def _create_session_events() -> None:
    op.create_table(
        "customer_session_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "activation_code_id",
            sa.Text(),
            sa.ForeignKey("activation_codes.id"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.Text(),
            sa.ForeignKey("customer_devices.id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("session_epoch", sa.Integer(), nullable=False),
        # System-driven events (TIMEOUT sweeps) carry no acting user.
        sa.Column("actor_user_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("reason", sa.Text()),
        sa.Column("request_id", sa.Text()),
        _created_at(),
        sa.CheckConstraint(
            _SESSION_EVENT_TYPES,
            name="ck_customer_session_events_type",
        ),
        sa.CheckConstraint(
            "session_epoch >= 1",
            name="ck_customer_session_events_epoch_positive",
        ),
        sa.CheckConstraint(
            "length(trim(event)) > 0",
            name="ck_customer_session_events_event_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(session_id)) > 0",
            name="ck_customer_session_events_session_id_not_blank",
        ),
    )
    op.create_index(
        "idx_customer_session_events_user",
        "customer_session_events",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_customer_session_events_session",
        "customer_session_events",
        ["session_id", "created_at"],
    )
    op.execute(_APPEND_ONLY_TRIGGER)


def _create_idempotency_envelopes() -> None:
    op.create_table(
        "customer_idempotency_envelopes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        # §7: the raw client key never reaches the database — only its
        # SHA-256 digest (constant-length, not comparable to anything).
        sa.Column("key_digest", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        # Optional reference to a persisted result for future operations
        # that store outcomes in their own tables (T13 keeps the full
        # one-time response inside the AEAD ciphertext instead).
        sa.Column("result_ref", sa.Text()),
        # §12.1: AEAD-encrypted one-time response + the key version that
        # decrypts it + the recovery window, all-or-nothing (CHECK below).
        sa.Column("ciphertext", sa.Text()),
        sa.Column("key_version", sa.Integer()),
        sa.Column("recovery_expires_at", sa.Text()),
        _created_at(),
        sa.Column("purged_at", sa.Text()),
        sa.UniqueConstraint(
            "operation",
            "scope",
            "key_digest",
            name="uq_customer_idempotency_envelopes_key",
        ),
        sa.CheckConstraint(
            _ENVELOPE_PAYLOAD_COUPLING,
            name="ck_customer_idempotency_envelopes_payload_coupled",
        ),
        sa.CheckConstraint(
            _ENVELOPE_PURGE_COUPLING,
            name="ck_customer_idempotency_envelopes_purge_coupled",
        ),
        sa.CheckConstraint(
            "key_version IS NULL OR key_version >= 1",
            name="ck_customer_idempotency_envelopes_key_version_positive",
        ),
        sa.CheckConstraint(
            "length(trim(operation)) > 0",
            name="ck_customer_idempotency_envelopes_operation_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(scope)) > 0",
            name="ck_customer_idempotency_envelopes_scope_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(key_digest)) > 0",
            name="ck_customer_idempotency_envelopes_key_digest_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(request_hash)) > 0",
            name="ck_customer_idempotency_envelopes_request_hash_not_blank",
        ),
        sa.CheckConstraint(
            "recovery_expires_at IS NULL "
            "OR recovery_expires_at::timestamptz > created_at::timestamptz",
            name="ck_customer_idempotency_envelopes_recovery_after_created",
        ),
    )
    # T14's cleanup job scans for expired recovery windows.
    op.create_index(
        "idx_customer_idempotency_envelopes_recovery",
        "customer_idempotency_envelopes",
        ["recovery_expires_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Session state, the append-only session audit and the idempotency
    # envelopes all chain to activation facts — the envelope ciphertext is
    # the only recoverable copy of the one-time credentials. Refuse loudly
    # once any of them exists (027/028 precedent); an unused schema
    # downgrades symmetrically.
    has_sessions = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM customer_session_state)")
    ).scalar()
    has_events = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM customer_session_events)")
    ).scalar()
    has_envelopes = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM customer_idempotency_envelopes)")
    ).scalar()
    if has_sessions or has_events or has_envelopes:
        raise RuntimeError(
            "cannot downgrade 029_customer_sessions_and_idempotency: "
            "customer_session_state / customer_session_events / "
            "customer_idempotency_envelopes already hold customer session "
            "and recovery data that chains to activation facts. Keep "
            "revision 029, or resolve the session history manually before "
            "rolling back."
        )
    op.drop_index(
        "idx_customer_idempotency_envelopes_recovery",
        table_name="customer_idempotency_envelopes",
    )
    op.drop_table("customer_idempotency_envelopes")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_session_events_append_only ON customer_session_events"
    )
    op.execute("DROP FUNCTION IF EXISTS customer_session_events_refuse_rewrite()")
    op.drop_index(
        "idx_customer_session_events_session",
        table_name="customer_session_events",
    )
    op.drop_index(
        "idx_customer_session_events_user",
        table_name="customer_session_events",
    )
    op.drop_table("customer_session_events")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_session_state_epoch_monotonic "
        "ON customer_session_state"
    )
    op.execute("DROP FUNCTION IF EXISTS customer_session_state_epoch_monotonic()")
    op.drop_table("customer_session_state")
