"""Preserve logical token groups, credential versions and attributable ledger history."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260912T2200_customer_token_lifecycle"
down_revision = "20260912T1910_xiaohongshu_link_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column("customer_api_keys", sa.Column("token_group_id", sa.Text(), nullable=True))
    op.execute("UPDATE customer_api_keys SET token_group_id = id")
    op.alter_column("customer_api_keys", "token_group_id", nullable=False)
    op.create_foreign_key(
        "fk_api_key_group", "customer_api_keys", "customer_api_keys", ["token_group_id"], ["id"]
    )
    op.add_column(
        "customer_api_keys",
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "customer_api_keys",
        sa.Column("is_default", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_api_key_credential_version", "customer_api_keys", "credential_version > 0"
    )
    op.create_check_constraint("ck_api_key_default", "customer_api_keys", "is_default IN (0, 1)")
    op.create_unique_constraint(
        "uq_api_key_group_version", "customer_api_keys", ["token_group_id", "credential_version"]
    )
    op.create_index(
        "uq_api_key_active_group",
        "customer_api_keys",
        ["token_group_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_api_key_default_user",
        "customer_api_keys",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default = 1 AND revoked_at IS NULL"),
    )
    op.add_column("wallet_transactions", sa.Column("api_key_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_wallet_transaction_api_key",
        "wallet_transactions",
        "customer_api_keys",
        ["api_key_id"],
        ["id"],
    )
    op.create_index("ix_wallet_transaction_api_key", "wallet_transactions", ["api_key_id", "type"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM customer_api_keys "
            "WHERE credential_version > 1 OR is_default = 1) "
            "OR EXISTS (SELECT 1 FROM wallet_transactions WHERE api_key_id IS NOT NULL)"
        )
    ).scalar():
        raise RuntimeError("cannot downgrade: token lifecycle or consumption history exists")
    op.drop_index("ix_wallet_transaction_api_key", table_name="wallet_transactions")
    op.drop_constraint("fk_wallet_transaction_api_key", "wallet_transactions", type_="foreignkey")
    op.drop_column("wallet_transactions", "api_key_id")
    op.drop_index("uq_api_key_default_user", table_name="customer_api_keys")
    op.drop_index("uq_api_key_active_group", table_name="customer_api_keys")
    op.drop_constraint("uq_api_key_group_version", "customer_api_keys", type_="unique")
    op.drop_constraint("ck_api_key_default", "customer_api_keys", type_="check")
    op.drop_constraint("ck_api_key_credential_version", "customer_api_keys", type_="check")
    op.drop_constraint("fk_api_key_group", "customer_api_keys", type_="foreignkey")
    for column in ("is_default", "credential_version", "token_group_id"):
        op.drop_column("customer_api_keys", column)
