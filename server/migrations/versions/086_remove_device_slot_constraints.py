"""086_remove_device_slot_constraints — decouple device limits from hard-coded slots.

CW-073: Remove the two-slot device model.  The partial unique index
``uq_customer_devices_slot`` and the CHECK ``ck_customer_devices_slot_range``
(``slot_no IN (1, 2)``) are dropped; a per-user ``max_devices`` column on
``users`` replaces the hard-coded constant.  CW-074 will make the default
configurable via ``runtime_settings``.

The ``slot_no`` column itself is retained (NOT NULL, no range constraint) so
existing rows stay valid and the display-order semantics survive; the
application layer switches from slot assignment to a simple BOUND-device count
against ``users.max_devices``.

PostgreSQL only (028 precedent).  SQLite remains the internal P0 runtime.

Revision ID: 086_remove_device_slot_constraints
Revises: 081_oral_unit_price
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "086_remove_device_slot_constraints"
down_revision = "082_publish_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 1. Drop the per-activation-code slot uniqueness (partial unique index).
    op.drop_index("uq_customer_devices_slot", table_name="customer_devices")

    # 2. Drop the hard-coded slot range CHECK (slot_no IN (1, 2)).
    op.drop_constraint("ck_customer_devices_slot_range", "customer_devices", type_="check")

    # 3. Add per-user device limit on the users table.
    op.add_column(
        "users",
        sa.Column(
            "max_devices",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
    )
    op.create_check_constraint(
        "ck_users_max_devices_positive",
        "users",
        "max_devices >= 1",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Guard: refuse if any user has max_devices != 2 (the pre-086 implicit
    # constant).  Restoring the slot constraints with a non-2 limit would
    # silently brick device enrollment.
    has_custom_limits = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM users WHERE max_devices != 2)")
    ).scalar()
    if has_custom_limits:
        raise RuntimeError(
            "cannot downgrade 086_remove_device_slot_constraints: "
            "users already holds custom max_devices values (!= 2), which the "
            "pre-086 two-slot model cannot represent. Keep revision 086, or "
            "reset all max_devices to 2 before rolling back."
        )

    # Guard: refuse if any device has slot_no outside (1, 2).
    has_out_of_range_slots = bind.execute(
        sa.text("SELECT EXISTS (  SELECT 1 FROM customer_devices WHERE slot_no NOT IN (1, 2))")
    ).scalar()
    if has_out_of_range_slots:
        raise RuntimeError(
            "cannot downgrade 086_remove_device_slot_constraints: "
            "customer_devices already holds slot_no values outside (1, 2), "
            "which the pre-086 CHECK constraint cannot hold. Keep revision 086, "
            "or reassign slot numbers before rolling back."
        )

    # 1. Drop the per-user device limit.
    op.drop_constraint("ck_users_max_devices_positive", "users", type_="check")
    op.drop_column("users", "max_devices")

    # 2. Restore the hard-coded slot range CHECK.
    op.create_check_constraint(
        "ck_customer_devices_slot_range",
        "customer_devices",
        "slot_no IN (1, 2)",
    )

    # 3. Restore the per-activation-code slot uniqueness (partial unique index).
    op.create_index(
        "uq_customer_devices_slot",
        "customer_devices",
        ["activation_code_id", "slot_no"],
        unique=True,
        postgresql_where=sa.text("status = 'BOUND'"),
    )
