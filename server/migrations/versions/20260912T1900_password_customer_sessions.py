"""Allow registered customers to establish sessions without activation codes."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260912T1900_password_customer_sessions"
down_revision = "20260912T1400_customer_registration_credentials"
branch_labels = None
depends_on = None

_TABLES = (
    "customer_devices",
    "customer_session_state",
    "customer_session_events",
    "admin_device_events",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.alter_column(table, "activation_code_id", existing_type=sa.Text(), nullable=True)
    op.drop_constraint("customer_session_state_pkey", "customer_session_state", type_="primary")
    op.drop_constraint(
        "customer_session_state_activation_code_id_key", "customer_session_state", type_="unique"
    )
    op.create_primary_key("customer_session_state_pkey", "customer_session_state", ["device_id"])
    op.create_index("ix_customer_session_state_user", "customer_session_state", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT user_id FROM customer_session_state "
            "GROUP BY user_id HAVING count(*) > 1)"
        )
    ).scalar():
        raise RuntimeError("cannot downgrade: multiple device sessions exist")
    for table in _TABLES:
        if bind.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE activation_code_id IS NULL)")
        ).scalar():
            raise RuntimeError("cannot downgrade: password customer session history exists")
    for table in reversed(_TABLES):
        op.alter_column(table, "activation_code_id", existing_type=sa.Text(), nullable=False)
    op.drop_index("ix_customer_session_state_user", table_name="customer_session_state")
    op.drop_constraint("customer_session_state_pkey", "customer_session_state", type_="primary")
    op.create_primary_key("customer_session_state_pkey", "customer_session_state", ["user_id"])
    op.create_unique_constraint(
        "customer_session_state_activation_code_id_key",
        "customer_session_state",
        ["activation_code_id"],
    )
