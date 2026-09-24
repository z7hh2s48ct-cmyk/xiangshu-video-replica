"""Add per-operator password credentials for routine administrator login.

Revision ID: 043_admin_password_login
Revises: 042_t37_observability_indexes

The existing ASX1 exchange remains the recovery/bootstrap path. Routine
sessions identify whether they were established by recovery exchange or by a
password so only a recovery-authenticated session may replace a password.
Password plaintext never reaches this schema; only a salted memory-hard hash
is persisted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "043_admin_password_login"
down_revision = "042_t37_observability_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.add_column(
        "admin_sessions",
        sa.Column(
            "auth_method",
            sa.Text(),
            nullable=False,
            server_default="exchange",
        ),
    )
    op.create_check_constraint(
        "ck_admin_sessions_auth_method",
        "admin_sessions",
        "auth_method IN ('exchange', 'password')",
    )
    op.create_table(
        "admin_password_credentials",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "credential_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "length(trim(password_hash)) > 0",
            name="ck_admin_password_hash_not_blank",
        ),
        sa.CheckConstraint(
            "credential_version >= 1",
            name="ck_admin_password_credential_version_positive",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_table("admin_password_credentials")
    op.drop_constraint(
        "ck_admin_sessions_auth_method",
        "admin_sessions",
        type_="check",
    )
    op.drop_column("admin_sessions", "auth_method")
