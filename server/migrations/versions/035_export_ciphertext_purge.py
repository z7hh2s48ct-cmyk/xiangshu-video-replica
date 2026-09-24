"""M2 review M2 (2026-08-23) — purgeable export-ciphertext payloads.

``activation_code_exports.ciphertext`` is sealed plaintext: anyone with a
DB read plus the AEAD key can redeem the codes. ``expires_at`` only gates
the one-time download — the ciphertext itself lingered forever, so a DB
read's exposure window extended indefinitely and the table grew unbounded
(T14 already purges idempotency envelopes; exports are the same security
posture).

This revision makes the payload purgeable (T14 envelope precedent): the
ciphertext, its SHA-256 digest and the key version become nullable, a
``purged_at`` timestamp records the sweep, and a CHECK coupling keeps a
purged row structurally unable to carry a secret — all three payload
columns must be NULL once ``purged_at`` is set. Audit columns (batch,
requester, created/expires/download moments, reason and request id) stay.

The maintenance sweep itself lives in
``scripts.purge_expired_export_ciphertexts`` (retention window past
expiry, default 7 days, env-tunable) and runs from the
``video-replica-maintenance`` timer.

PostgreSQL only (027/032 precedent): the activation catalog never exists
on the internal SQLite lane.

Revision ID: 035_export_ciphertext_purge
Revises: 034_device_fingerprint_canonical
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "035_export_ciphertext_purge"
down_revision = "034_device_fingerprint_canonical"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — the activation catalog is
        # customer-production only.
        return
    op.alter_column("activation_code_exports", "ciphertext", nullable=True)
    op.alter_column("activation_code_exports", "ciphertext_sha256", nullable=True)
    op.alter_column("activation_code_exports", "key_version", nullable=True)
    # The not-blank guards predate purgeability: a purged row is blank by
    # design, so the guards become NULL-permitting instead.
    op.drop_constraint(
        "ck_activation_code_exports_ciphertext_not_blank",
        "activation_code_exports",
        type_="check",
    )
    op.create_check_constraint(
        "ck_activation_code_exports_ciphertext_not_blank",
        "activation_code_exports",
        "ciphertext IS NULL OR length(trim(ciphertext)) > 0",
    )
    op.drop_constraint(
        "ck_activation_code_exports_sha256_not_blank",
        "activation_code_exports",
        type_="check",
    )
    op.create_check_constraint(
        "ck_activation_code_exports_sha256_not_blank",
        "activation_code_exports",
        "ciphertext_sha256 IS NULL OR length(trim(ciphertext_sha256)) > 0",
    )
    op.add_column("activation_code_exports", sa.Column("purged_at", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_activation_code_exports_purge_coupling",
        "activation_code_exports",
        "purged_at IS NULL OR (ciphertext IS NULL AND ciphertext_sha256 IS NULL "
        "AND key_version IS NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Guard precedent (026/028): purged rows cannot survive restoring the
    # NOT NULL payload shape — the audit trail must not be silently
    # destroyed to satisfy a rollback.
    purged = bind.execute(
        sa.text("SELECT count(*) FROM activation_code_exports WHERE purged_at IS NOT NULL")
    ).scalar()
    if purged:
        raise RuntimeError(
            f"cannot downgrade 035_export_ciphertext_purge: {purged} purged export row(s) exist"
        )
    op.drop_constraint(
        "ck_activation_code_exports_purge_coupling",
        "activation_code_exports",
        type_="check",
    )
    op.drop_column("activation_code_exports", "purged_at")
    op.drop_constraint(
        "ck_activation_code_exports_sha256_not_blank",
        "activation_code_exports",
        type_="check",
    )
    op.create_check_constraint(
        "ck_activation_code_exports_sha256_not_blank",
        "activation_code_exports",
        "length(trim(ciphertext_sha256)) > 0",
    )
    op.drop_constraint(
        "ck_activation_code_exports_ciphertext_not_blank",
        "activation_code_exports",
        type_="check",
    )
    op.create_check_constraint(
        "ck_activation_code_exports_ciphertext_not_blank",
        "activation_code_exports",
        "length(trim(ciphertext)) > 0",
    )
    op.alter_column("activation_code_exports", "key_version", nullable=False)
    op.alter_column("activation_code_exports", "ciphertext_sha256", nullable=False)
    op.alter_column("activation_code_exports", "ciphertext", nullable=False)
