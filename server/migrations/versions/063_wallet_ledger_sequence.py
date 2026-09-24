"""Preserve causal ordering for new wallet ledger entries.

Revision ID: 063_wallet_ledger_sequence
Revises: 062_activation_initial_free_seconds

Existing rows deliberately remain unsequenced: their second-resolution timestamps
cannot prove the order of concurrent-looking entries. New rows receive an immutable
database-assigned sequence so administrative balance snapshots can be reconstructed
without guessing from random transaction ids.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "063_wallet_ledger_sequence"
down_revision = "062_activation_initial_free_seconds"
branch_labels = None
depends_on = None

_SEQUENCE = "wallet_ledger_sequence_seq"
_INDEX = "uq_wallet_transactions_ledger_sequence"
_USER_INDEX = "idx_wallet_transactions_user_ledger_sequence"
_SQLITE_TRIGGER = "trg_wallet_transactions_assign_ledger_sequence"
_SQLITE_INSERT_GUARD = "trg_wallet_transactions_reject_explicit_ledger_sequence"
_SQLITE_UPDATE_GUARD = "trg_wallet_transactions_reject_ledger_sequence_update"
_POSTGRES_TRIGGER = "trg_wallet_transactions_assign_ledger_sequence"
_POSTGRES_FUNCTION = "assign_wallet_ledger_sequence"


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "wallet_transactions",
        sa.Column("ledger_sequence", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        _INDEX,
        "wallet_transactions",
        ["ledger_sequence"],
        unique=True,
        sqlite_where=sa.text("ledger_sequence IS NOT NULL"),
        postgresql_where=sa.text("ledger_sequence IS NOT NULL"),
    )
    op.create_index(
        _USER_INDEX,
        "wallet_transactions",
        ["user_id", "ledger_sequence"],
    )

    if bind.dialect.name == "postgresql":
        op.execute(sa.text(f"CREATE SEQUENCE {_SEQUENCE}"))
        op.execute(
            sa.text(
                f"""
                CREATE FUNCTION {_POSTGRES_FUNCTION}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    IF TG_OP = 'UPDATE' THEN
                        IF NEW.ledger_sequence IS DISTINCT FROM OLD.ledger_sequence THEN
                            RAISE EXCEPTION 'ledger_sequence is immutable';
                        END IF;
                        RETURN NEW;
                    END IF;
                    IF NEW.ledger_sequence IS NOT NULL THEN
                        RAISE EXCEPTION 'ledger_sequence is database assigned';
                    END IF;
                    PERFORM 1 FROM wallets
                    WHERE user_id = NEW.user_id
                    FOR UPDATE;
                    NEW.ledger_sequence := nextval('{_SEQUENCE}');
                    RETURN NEW;
                END;
                $$
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_POSTGRES_TRIGGER}
                BEFORE INSERT OR UPDATE ON wallet_transactions
                FOR EACH ROW EXECUTE FUNCTION {_POSTGRES_FUNCTION}()
                """
            )
        )
        return

    if bind.dialect.name == "sqlite":
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_SQLITE_INSERT_GUARD}
                BEFORE INSERT ON wallet_transactions
                WHEN NEW.ledger_sequence IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_sequence is database assigned');
                END
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_SQLITE_UPDATE_GUARD}
                BEFORE UPDATE OF ledger_sequence ON wallet_transactions
                WHEN OLD.ledger_sequence IS NOT NULL
                     AND NEW.ledger_sequence IS NOT OLD.ledger_sequence
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_sequence is immutable');
                END
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_SQLITE_TRIGGER}
                AFTER INSERT ON wallet_transactions
                WHEN NEW.ledger_sequence IS NULL
                BEGIN
                    UPDATE wallet_transactions
                    SET ledger_sequence = (
                        SELECT COALESCE(MAX(ledger_sequence), 0) + 1
                        FROM wallet_transactions
                        WHERE id <> NEW.id
                    )
                    WHERE id = NEW.id;
                END
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER {_POSTGRES_TRIGGER} ON wallet_transactions"))
        op.execute(sa.text(f"DROP FUNCTION {_POSTGRES_FUNCTION}()"))
        op.execute(sa.text(f"DROP SEQUENCE {_SEQUENCE}"))
    elif bind.dialect.name == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER {_SQLITE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER {_SQLITE_UPDATE_GUARD}"))
        op.execute(sa.text(f"DROP TRIGGER {_SQLITE_INSERT_GUARD}"))

    op.drop_index(_USER_INDEX, table_name="wallet_transactions")
    op.drop_index(_INDEX, table_name="wallet_transactions")
    op.drop_column("wallet_transactions", "ledger_sequence")
