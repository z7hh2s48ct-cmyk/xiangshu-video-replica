"""P0-9 — sweep expired third-party call logs.

Usage (server/ directory):

    uv run python -m scripts.purge_external_call_logs \
        --database-url postgresql://USER:PASSWORD@HOST:5432/DBNAME [--dry-run]

The DSN resolves through ``app.db_pg.resolve_cli_pg_dsn`` (CW-057): the
argument wins over ``VIDEO_REPLICA_DATABASE_URL``, and missing DSNs,
``sqlite://`` URLs or ``VIDEO_REPLICA_DB_PATH`` leftovers fail closed with a
fixed, credential-free message and never create a database file (PG-01).

``external_call_logs`` keeps the redacted request summary and the provider's
raw response so operators can explain a failed task. That is diagnostic
evidence, not an accounting fact: failed calls are kept 180 days, successful
ones 30 days, and rows past their window are deleted (rows written before the
column existed have no outcome and are kept as long as failures). The cutoffs
come from the PostgreSQL clock (``SELECT now()``), never the maintenance
host's clock. Deletion runs in committed batches so a large backlog never
holds one long transaction against the write path. Output carries counts
only. Intended to run from the ``video-replica-maintenance`` systemd timer
(deploy/systemd/), daily.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import psycopg

from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn
from app.external_calls import (
    FAILED_RETENTION_DAYS,
    SUCCEEDED_RETENTION_DAYS,
    count_expired_calls,
    purge_expired_call_batch,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge expired third-party call logs (P0-9).")
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

    # autocommit so each batch below is its own real transaction (with an
    # implicit outer transaction the per-batch blocks would only be
    # savepoints and the locks would live until the connection closed).
    with psycopg.connect(database_url, autocommit=True) as conn:
        row = conn.execute("SELECT now()").fetchone()
        if row is None:
            raise RuntimeError("SELECT now() returned no rows")
        now: datetime = row[0]
        if args.dry_run:
            eligible = count_expired_calls(conn, now=now)
            print(f"expired external call log rows eligible for purge: {eligible}")
            return 0
        purged = 0
        with conn.transaction():
            conn.execute(
                "DELETE FROM external_call_observations WHERE created_at < %s - interval '2 days'",
                (now,),
            )
        while True:
            with conn.transaction():
                batch = purge_expired_call_batch(conn, now=now)
            if batch == 0 and count_expired_calls(conn, now=now, ready_only=True) == 0:
                break
            purged += batch
    print(
        f"purged {purged} external call log row(s) "
        f"(failed > {FAILED_RETENTION_DAYS} days, succeeded > {SUCCEEDED_RETENTION_DAYS} days)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
