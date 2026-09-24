"""T14 / ACT-07 — purge expired customer idempotency envelopes.

Usage (server/ directory):

    uv run python -m scripts.purge_idempotency_envelopes \
        --database-url postgresql://USER:PASSWORD@HOST:5432/DBNAME [--dry-run]

The DSN resolves through ``app.db_pg.resolve_cli_pg_dsn`` (CW-057): the
argument wins over ``VIDEO_REPLICA_DATABASE_URL``, and missing DSNs,
``sqlite://`` URLs or ``VIDEO_REPLICA_DB_PATH`` leftovers fail closed with a
fixed, credential-free message and never create a database file (PG-01).
Output carries counts only — no business values, credentials or envelope
contents are ever printed. Intended to run from the
``video-replica-maintenance`` systemd timer (deploy/systemd/), daily.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import psycopg

from app.customer_idempotency import count_expired_envelopes, purge_expired_envelopes
from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge expired customer idempotency envelopes (T14/ACT-07)."
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="PostgreSQL DSN (defaults to VIDEO_REPLICA_DATABASE_URL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the purge candidate count without changing anything",
    )
    args = parser.parse_args(argv)

    try:
        database_url = resolve_cli_pg_dsn(args.database_url)
    except CliDatabaseConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with psycopg.connect(database_url) as conn:
        # The recovery window is decided on the server clock: writes, route
        # checks and this purge must all read the same PostgreSQL clock, or
        # host-clock drift shifts the window boundary (M2 review LOW).
        now: datetime = conn.execute("SELECT now()").fetchone()[0]
        if args.dry_run:
            eligible = count_expired_envelopes(conn, now=now)
            print(f"expired idempotency envelopes eligible for purge: {eligible}")
            return 0
        with conn.transaction():
            purged = purge_expired_envelopes(conn, now=now)
    print(f"purged {purged} expired idempotency envelope(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
