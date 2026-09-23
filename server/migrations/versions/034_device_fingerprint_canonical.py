"""M2 review M1 (2026-08-23) — cross-version device-fingerprint probe key.

During a key-rotation rollout the fleet is briefly heterogeneous: instance
B already carries V2 while instance A still runs V1-only. New bindings
carry the *highest* configured version's digest, and each instance's
binding check only recognises its own retained versions — so A's ANY([V1])
probe cannot see B's committed V2 row, and the ``uq_customer_devices_fingerprint``
partial unique index (same-string only) cannot settle the race either. The
same physical device could then redeem two codes into two customers with
two first charges (deterministically reproduced in
``test_rotation_window_cross_version_same_fingerprint_one_activation``).

The probe key closes that window: every new binding also stores the digest
under the *lowest* retained key version (``fingerprint_canonical``). In the
add-version rollout window every instance's lowest retained version is the
shared old version, so the canonical digest is identical across instances
regardless of which versions they carry on top — the check query and the
partial unique index on it therefore cross the version boundary.

Backfill is self-referential (``fingerprint_hmac`` copied as-is): the
catalog has no customer traffic yet and migrations must never read key
material from the environment. Deploy order matters: land this revision
*before* the next rotation starts, and rows written by a pre-034 build
during an active rotation keep their version digest as canonical — the
release notes call this out.

PostgreSQL only (028/032 precedent): ``customer_devices`` never exists on
the internal SQLite lane.

Revision ID: 034_device_fingerprint_canonical
Revises: 033_batch_creation_audit
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "034_device_fingerprint_canonical"
down_revision = "033_batch_creation_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — customer devices are
        # customer-production only.
        return
    op.add_column(
        "customer_devices",
        sa.Column("fingerprint_canonical", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE customer_devices SET fingerprint_canonical = fingerprint_hmac "
        "WHERE fingerprint_canonical IS NULL"
    )
    op.create_index(
        "uq_customer_devices_fingerprint_canonical",
        "customer_devices",
        ["fingerprint_canonical"],
        unique=True,
        postgresql_where=sa.text("status = 'BOUND'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_index(
        "uq_customer_devices_fingerprint_canonical",
        table_name="customer_devices",
    )
    op.drop_column("customer_devices", "fingerprint_canonical")
