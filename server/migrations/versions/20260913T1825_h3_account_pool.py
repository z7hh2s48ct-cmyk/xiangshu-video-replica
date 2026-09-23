"""Encrypted H3 accounts and durable task-to-account routing."""

from alembic import op

revision = "20260913T1825_h3_account_pool"
down_revision = "20260913T1800_payment_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE h3_provider_accounts (
            id text PRIMARY KEY,
            name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
            encrypted_api_key text NOT NULL,
            key_digest text NOT NULL UNIQUE,
            concurrency_limit integer NOT NULL CHECK (concurrency_limit BETWEEN 1 AND 1000000),
            enabled boolean NOT NULL DEFAULT true,
            version integer NOT NULL DEFAULT 1 CHECK (version > 0),
            last_dispatched_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE h3_provider_task_accounts (
            task_id text PRIMARY KEY REFERENCES generation_tasks(id),
            account_id text NOT NULL REFERENCES h3_provider_accounts(id),
            assigned_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_h3_task_account ON h3_provider_task_accounts(account_id);
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM h3_provider_accounts)
          THEN RAISE EXCEPTION 'H3 account data exists; refusing destructive downgrade'; END IF;
        END $$;
        DROP TABLE h3_provider_task_accounts;
        DROP TABLE h3_provider_accounts;
    """)
