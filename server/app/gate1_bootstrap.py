"""Seed a migrated, empty Gate 1 PostgreSQL database with its runtime identity.

Usage (server/ directory):

    uv run python -m app.gate1_bootstrap \
        --database-url postgresql://USER:PASSWORD@HOST:5432/DBNAME \
        [--user-id gate1_admin] [--display-name "Gate 1 Admin"]

CW-057 (PG-01/PG-08): the historical SQLite form (``--db-path`` +
``initialize_database``) is retired. The seed only accepts a ``postgresql://``
DSN resolved through ``app.db_pg.resolve_cli_pg_dsn`` — a missing DSN, a
``sqlite://`` URL or a ``VIDEO_REPLICA_DB_PATH`` leftover fails closed with a
fixed, credential-free message and never creates a database file. The target
must already carry the migrated schema (run ``deploy/postgres/migrate.sh`` or
``alembic upgrade head`` first); seeding is a single transaction guarded by a
PG advisory lock and a pristine-state check, so a re-run against an already
seeded database is refused instead of duplicating identity or wallet rows.
The JSON summary redacts the DSN (``app.db_pg.redact_postgres_dsn``) — no
credential ever reaches logs or evidence files.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import uuid4

import psycopg

from app.db_pg import CliDatabaseConfigError, redact_postgres_dsn, resolve_cli_pg_dsn
from app.db_portable import BusinessConnection
from app.settings import SettingsRepository

# Dedicated advisory-lock id for the Gate 1 seed so concurrent harness runs
# serialize their pristine-state check on the database, not on operator
# timing (same shape as the provision-empty-customer bootstrap lock).
GATE1_BOOTSTRAP_LOCK_ID = 0x4741_5431  # "GAT1"


def bootstrap_gate1_database(
    database_url: str,
    *,
    user_id: str,
    display_name: str,
) -> dict[str, str]:
    """Seed a migrated, empty Gate 1 PG database with its identity/runtime."""
    dsn = resolve_cli_pg_dsn(database_url)

    with psycopg.connect(dsn) as conn:
        schema_row = conn.execute("SELECT to_regclass('public.users')").fetchone()
        if schema_row is None or schema_row[0] is None:
            raise RuntimeError(
                "Gate 1 database has no migrated schema; run "
                "deploy/postgres/migrate.sh (alembic upgrade head) first"
            )
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (GATE1_BOOTSTRAP_LOCK_ID,))
            count_row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            existing_users = int(count_row[0]) if count_row is not None else 0
            if existing_users:
                raise RuntimeError(
                    "Gate 1 database is not pristine: "
                    f"{existing_users} user row(s) already exist; "
                    "seed only a freshly migrated empty database"
                )
            conn.execute(
                """
                INSERT INTO users (id, username, display_name, role)
                VALUES (%s, %s, %s, 'admin')
                """,
                (user_id, user_id, display_name),
            )
            conn.execute(
                """
                INSERT INTO wallets (user_id, available_credits, reserved_credits)
                VALUES (%s, 10, 0)
                """,
                (user_id,),
            )
            recharge_order_id = f"gate1-recharge-{uuid4().hex}"
            # provider='admin_adjustment' + INTERNAL scope is the PG constraint
            # model (migration 026) for an internally-funded PAID order: no
            # third-party trade number, no zpay amount ladder to satisfy.
            conn.execute(
                """
                INSERT INTO recharge_orders (
                    id, user_id, merchant_order_no, provider, status, pricing_scope,
                    base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                    min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                    amount_fen, credits, paid_at
                ) VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', 'INTERNAL',
                          1000, 1000, 10000, 1000, 10000, 10, CURRENT_TIMESTAMP)
                """,
                (recharge_order_id, user_id, recharge_order_id),
            )
            conn.execute(
                """
                INSERT INTO wallet_transactions (
                    id, user_id, type, available_delta, reserved_delta,
                    recharge_order_id, task_id, billing_round, idempotency_key
                ) VALUES (%s, %s, 'CHARGE', 10, 0, %s, NULL, NULL, %s)
                """,
                (
                    f"gate1-charge-{uuid4().hex}",
                    user_id,
                    recharge_order_id,
                    f"gate1-charge:{user_id}",
                ),
            )
            SettingsRepository(BusinessConnection.postgres(conn)).save_runtime_settings(
                max_generation_count_per_batch=6,
                max_concurrent_h3_tasks=2,
                active_storage_provider="local",
                actor_user_id=user_id,
            )

    return {
        "database": redact_postgres_dsn(dsn),
        "desktop_user_id": user_id,
        "active_storage_provider": "local",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Seed a freshly migrated, empty Gate 1 PostgreSQL database "
            "(CW-057: PostgreSQL is the only accepted target)"
        )
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="PostgreSQL DSN (defaults to VIDEO_REPLICA_DATABASE_URL)",
    )
    parser.add_argument("--user-id", default="gate1_admin")
    parser.add_argument("--display-name", default="Gate 1 Admin")
    args = parser.parse_args(argv)
    try:
        summary = bootstrap_gate1_database(
            args.database_url,
            user_id=args.user_id,
            display_name=args.display_name,
        )
    except CliDatabaseConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
