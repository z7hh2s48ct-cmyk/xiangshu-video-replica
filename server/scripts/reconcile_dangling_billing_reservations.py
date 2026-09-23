"""Finalize terminal generation reservations missing their ledger terminal row.

The sweep is deliberately narrow: archived successes are settled and
FAILED/CANCELLED tasks are released. Active and SUBMISSION_UNCERTAIN tasks are
never selected, because their provider outcome still needs normal processing
or explicit reconciliation.

Only aggregate counts are printed. No user, task, provider, credential, price,
or quota data leaves the process.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn
from app.db_portable import BusinessConnection
from app.internal_billing import (
    find_dangling_billing_reservations,
    reconcile_dangling_billing_reservations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile terminal generation billing reservations (BILL-03)."
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="PostgreSQL DSN (defaults to VIDEO_REPLICA_DATABASE_URL)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum terminal reservations processed in this run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the candidate count without changing ledger rows",
    )
    args = parser.parse_args(argv)

    try:
        database_url = resolve_cli_pg_dsn(args.database_url)
    except CliDatabaseConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.limit < 1:
        print("error: --limit must be positive", file=sys.stderr)
        return 1

    with psycopg.connect(database_url) as raw:
        with raw.transaction():
            conn = BusinessConnection.postgres(raw)
            if args.dry_run:
                count = len(find_dangling_billing_reservations(conn, limit=args.limit))
                print(f"dangling terminal reservations eligible for reconciliation: {count}")
                return 0
            result = reconcile_dangling_billing_reservations(conn, limit=args.limit)

    print(
        "reconciled terminal billing reservations: "
        f"scanned={result.scanned} settled={result.settled} "
        f"released={result.released} failed={result.failed}"
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
