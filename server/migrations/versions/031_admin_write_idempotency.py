"""T12 / ACT-04 — admin write idempotency.

The code checklist §3.1 freezes migration *themes* with suggested numbers
025–030, and the AGENTS.md / task-list red line freezes those exact file
names — ``028_customer_devices_and_activations`` through ``030_user_fair_queue``
are reserved for the T16/T20/T25 themes. The
admin-write idempotency table is an inserted theme with no frozen number of
its own (dev doc §11.3 only fixes the invariant "管理写幂等键按 actor、
canonical route 和 key digest 唯一"), so this revision lands as **031** to
keep the 028–030 suggested-number window free for the frozen themes (PR #43
review P1; the revision is still part of this unpublished PR, so renaming is
allowed under the append-only rule). The device/session revisions chain off
the then-current head and keep their frozen file names; the T25 queue theme
lands as **041_user_fair_queue** on the released head (P1-3: re-pointing the
published 032 would silently skip the queue schema on stamped databases).

``admin_write_idempotency`` snapshots one admin write per
(actor, canonical route, idempotency key digest):

- ``route`` is the canonical route template ("POST
  /api/control/activation-code-batches"), so the same key against a
  different resource path can never collide silently;
- the raw key never reaches the database (sha256 digest, the
  ``admin_sessions`` precedent);
- ``request_hash`` freezes the canonical request (route + path parameters +
  JSON body), so the same key with a different body — or the same body aimed
  at a *different* concrete resource of the same parameterized route — is
  rejected as an idempotency conflict instead of replaying an unrelated
  stored response;
- ``response_status`` / ``response_body`` persist the committed response so
  a retry after a lost response replays the same business output.

The revision also appends the download audit columns to
``activation_code_exports`` (PR #43 review P1): the one-time plaintext
download is the highest-risk admin operation on the catalog, and its
``reason`` / ``X-Request-Id`` were validated but discarded — the durable
record kept only the actor and timestamp. The new columns land here (the
revision is still part of the same unpublished PR) with a coupling CHECK:
once ``downloaded_at`` is set, the reason and request id are present too,
and the one-shot ``downloaded_at IS NULL`` update guard makes the row
effectively immutable afterwards.

Rows live inside the same transaction as the business write: a placeholder
``INSERT ... ON CONFLICT DO NOTHING`` serializes concurrent same-key
writers (PostgreSQL waits on the conflicting insert), the winner back-fills
the response snapshot before commit, and a rolled-back business failure
releases the key with the transaction. The paired NULL / NOT NULL response
columns make an unfinished placeholder visible to the CHECK itself.

PostgreSQL only (025–027 precedent): admin activation writes are a
customer-production concern; the internal SQLite lane keeps its legacy
control path and never grows this table.

Revision ID: 031_admin_write_idempotency
Revises: 027_activation_code_catalog
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031_admin_write_idempotency"
down_revision = "027_activation_code_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — the admin activation API and its
        # idempotency snapshots are customer-production only.
        return
    op.create_table(
        "admin_write_idempotency",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("idempotency_key_digest", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_body", sa.Text()),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "length(trim(route)) > 0",
            name="ck_admin_write_idempotency_route_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key_digest)) > 0",
            name="ck_admin_write_idempotency_key_digest_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(request_hash)) > 0",
            name="ck_admin_write_idempotency_request_hash_not_blank",
        ),
        sa.CheckConstraint(
            # A finished snapshot carries both response halves; a placeholder
            # carries neither. Half-written rows fail closed here.
            "(response_status IS NULL) = (response_body IS NULL)",
            name="ck_admin_write_idempotency_response_pair",
        ),
    )
    op.create_index(
        "uq_admin_write_idempotency_actor_route_key",
        "admin_write_idempotency",
        ["actor_user_id", "route", "idempotency_key_digest"],
        unique=True,
    )
    # Download audit columns (PR #43 review P1): reason + request id must be
    # durably recorded with the one-time plaintext download. Coupled to
    # downloaded_at so a "downloaded" row without its justification is
    # impossible; legacy rows downloaded before this column set keep all
    # three NULL. The one-shot update guard in the service layer is what
    # makes the tuple immutable in practice.
    op.add_column(
        "activation_code_exports",
        sa.Column("download_reason", sa.Text()),
    )
    op.add_column(
        "activation_code_exports",
        sa.Column("download_request_id", sa.Text()),
    )
    op.create_check_constraint(
        "ck_activation_code_exports_download_audit_pair",
        "activation_code_exports",
        "(downloaded_at IS NULL) = (download_reason IS NULL) "
        "AND (downloaded_at IS NULL) = (download_request_id IS NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint(
        "ck_activation_code_exports_download_audit_pair",
        "activation_code_exports",
        type_="check",
    )
    op.drop_column("activation_code_exports", "download_request_id")
    op.drop_column("activation_code_exports", "download_reason")
    # Idempotency snapshots are replay caches, not business facts — dropping
    # them only costs operators a re-issue, so the downgrade is symmetric.
    op.drop_index(
        "uq_admin_write_idempotency_actor_route_key",
        table_name="admin_write_idempotency",
    )
    op.drop_table("admin_write_idempotency")
