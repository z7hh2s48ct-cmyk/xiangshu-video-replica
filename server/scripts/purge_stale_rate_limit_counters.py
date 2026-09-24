"""M2 review M3 / ACT-08 — sweep stale rate-limit counter rows.

Usage (server/ directory):

    uv run python -m scripts.purge_stale_rate_limit_counters \
        --database-url postgresql://USER:PASSWORD@HOST:5432/DBNAME [--dry-run]

The DSN resolves through ``app.db_pg.resolve_cli_pg_dsn`` (CW-057): the
argument wins over ``VIDEO_REPLICA_DATABASE_URL``, and missing DSNs,
``sqlite://`` URLs or ``VIDEO_REPLICA_DB_PATH`` leftovers fail closed with a
fixed, credential-free message and never create a database file (PG-01).
Counter rows on fully lapsed windows can never be consulted again
(``consume_rate_limit`` resets the window on the first hit after the
cutoff), so deleting them keeps the table bounded without touching any
enforcement. The append-only failure table (the actual audit trail) is
never written here. Output carries counts only. Intended to run from the
``video-replica-maintenance`` systemd timer (deploy/systemd/), daily.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import psycopg

from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn
from app.security_rate_limit import count_stale_counters, purge_stale_counters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge stale rate-limit counter rows (M2 review M3)."
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

    # The cutoff comes from the PostgreSQL clock (SELECT now()), never the
    # maintenance host's clock: consume_rate_limit() stamps counters with
    # the server clock, so a host clock ahead of PG would sweep counters
    # the limiter still treats as active and bypass the shared abuse budget
    # (Codex P2; same source as the idempotency purge).
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT now()").fetchone()
        if row is None:
            raise RuntimeError("SELECT now() returned no rows")
        now: datetime = row[0]
        if args.dry_run:
            eligible = count_stale_counters(conn, now=now)
            print(f"stale rate-limit counter rows eligible for purge: {eligible}")
            return 0
        with conn.transaction():
            purged = purge_stale_counters(conn, now=now)
    print(f"purged {purged} stale rate-limit counter row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
