"""M2 review H1 (2026-08-23) — batch-creation audit columns.

The admin write contract validates ``reason`` and mints an
``X-Request-Id`` for every write, but the two highest-risk minting paths
let them evaporate after validation: the batch row recorded neither, the
GENERATED events carried no reason, and the EXPORTED events carried no
request id (only the actor). PR #43 review P1 fixed the same gap for the
one-time download (031 added ``download_reason`` / ``download_request_id``
to ``activation_code_exports``); this revision closes the remaining source
of the audit chain with the same shape on the batch row.

``creation_reason`` / ``creation_request_id`` are write-once columns: they
are set by the creating transaction and never updated afterwards (the
append-only spirit of the catalog's event tables; no rewrite path exists in
the application).

Also in the same fix (no schema change needed): ``generate_batch_codes``
now writes the write-contract reason into every GENERATED event and
``create_batch_export`` writes the request id into every EXPORTED event —
``activation_code_events.reason`` / ``.request_id`` already exist since
027.

PostgreSQL only (025–031 precedent): the activation catalog is a
customer-production concern; the internal SQLite lane never grows this
table.

Revision ID: 033_batch_creation_audit
Revises: 032_security_rate_limits
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "033_batch_creation_audit"
down_revision = "032_security_rate_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — the activation catalog is
        # customer-production only.
        return
    op.add_column(
        "activation_code_batches",
        sa.Column("creation_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "activation_code_batches",
        sa.Column("creation_request_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("activation_code_batches", "creation_request_id")
    op.drop_column("activation_code_batches", "creation_reason")
