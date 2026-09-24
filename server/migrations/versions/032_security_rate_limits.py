"""T15 / ACT-08 — shared security rate limiting and failure auditing.

Revision 032 adds the PostgreSQL-backed abuse controls every API instance
behind the load balancer shares (ACT-08 red line: in-process rate limiting
is not a multi-API answer):

- ``security_rate_limit_counters`` — one row per fixed-window bucket
  (``dimension|identifier``). The atomic UPSERT in
  ``app.security_rate_limit.consume_rate_limit`` either starts a fresh
  window or increments the shared hit count, so N API instances draw from
  one budget.
- ``security_auth_failures`` — append-only failure events for the
  activation/login lanes (dimension + identifier + request id + server
  clock). This is the data source for the failure metrics and the alert
  threshold operators key on; the identifier for the code dimension is the
  keyed digest, never the plaintext activation code.

Both tables are customer-production only and never materialize on SQLite
(internal P0 lane), matching 027–029.

Revision ID: 032_security_rate_limits
Revises: 029_customer_sessions_and_idempotency
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "032_security_rate_limits"
# Restored to the published link (029): the M5 revision originally re-pointed
# this to 030_user_fair_queue, which would silently skip the fair-queue schema
# on databases already stamped 032–040 (P1-3). The T25 revision now lands as
# 041_user_fair_queue descending from 040; this file is unchanged from the
# published base.
down_revision = "029_customer_sessions_and_idempotency"
branch_labels = None
depends_on = None

# Fixed vocabulary: only these dimensions may keep counters or failure
# events — an unknown dimension string is a programming error, not data.
_DIMENSIONS = "dimension IN ('activate:ip', 'activate:code', 'login:ip', 'login:account')"

_APPEND_ONLY_TRIGGER = """
CREATE FUNCTION security_auth_failures_refuse_rewrite() RETURNS trigger AS $refuse$
BEGIN
    RAISE EXCEPTION 'security_auth_failures is append-only';
END;
$refuse$ LANGUAGE plpgsql;

CREATE TRIGGER trg_security_auth_failures_append_only
BEFORE UPDATE OR DELETE ON security_auth_failures
FOR EACH ROW EXECUTE FUNCTION security_auth_failures_refuse_rewrite();
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
        # SQLite: internal P0 runtime — shared rate limiting and failure
        # auditing are customer-production concerns only.
        return

    op.create_table(
        "security_rate_limit_counters",
        # bucket_key = "{dimension}|{identifier}" — the fixed-window
        # identity two API instances must agree on.
        sa.Column("bucket_key", sa.Text(), primary_key=True),
        sa.Column("window_start", sa.Text(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "hit_count >= 0", name="ck_security_rate_limit_counters_hits_nonnegative"
        ),
        sa.CheckConstraint(
            "length(trim(window_start)) > 0",
            name="ck_security_rate_limit_counters_window_not_blank",
        ),
    )
    # Metrics scans walk (dimension, occurred_at); the purge/cleanup story
    # (later OPS task) walks occurred_at alone. The append-only trigger
    # below refuses plain row DELETEs and UPDATEs: the future retention
    # cleanup cannot be a time-windowed DELETE against this table — it must
    # land as a session-identifier exemption in the trigger (or partition
    # drops), a constraint the OPS task inherits by design.
    op.create_table(
        "security_auth_failures",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("dimension", sa.Text(), nullable=False),
        # For the code dimension this is the keyed digest — the plaintext
        # activation code must never reach this table (ACT-08 red line).
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text()),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint(_DIMENSIONS, name="ck_security_auth_failures_dimension"),
        sa.CheckConstraint(
            "length(trim(identifier)) > 0",
            name="ck_security_auth_failures_identifier_not_blank",
        ),
    )
    op.create_index(
        "idx_security_auth_failures_dimension_time",
        "security_auth_failures",
        ["dimension", "occurred_at"],
    )
    op.execute(_APPEND_ONLY_TRIGGER)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # security_auth_failures is the append-only audit evidence for the
    # activation/login abuse controls: dropping it on a live host would
    # destroy the only record of (attempted) enumeration attacks. Refuse
    # loudly once any failure event exists (026/028 guard precedent); an
    # unused schema downgrades symmetrically.
    has_failures = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM security_auth_failures)")
    ).scalar()
    if has_failures:
        raise RuntimeError(
            "cannot downgrade 032_security_rate_limits: security_auth_failures "
            "already holds audit events that must survive the rollback. Keep "
            "revision 032, or export the audit trail manually before rolling back."
        )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_security_auth_failures_append_only ON security_auth_failures"
    )
    op.execute("DROP FUNCTION IF EXISTS security_auth_failures_refuse_rewrite()")
    op.drop_index("idx_security_auth_failures_dimension_time", table_name="security_auth_failures")
    op.drop_table("security_auth_failures")
    op.drop_table("security_rate_limit_counters")
