"""M2 review M2 / ACT-03 — purge expired activation-export ciphertext.

Usage (server/ directory):

    uv run python -m scripts.purge_expired_export_ciphertexts \
        --database-url postgresql://USER:PASSWORD@HOST:5432/DBNAME [--dry-run]

The DSN resolves through ``app.db_pg.resolve_cli_pg_dsn`` (CW-057): the
argument wins over ``VIDEO_REPLICA_DATABASE_URL``, and missing DSNs,
``sqlite://`` URLs or ``VIDEO_REPLICA_DB_PATH`` leftovers fail closed with a
fixed, credential-free message and never create a database file (PG-01).
The retention window past expiry defaults to 7 days and is tunable via
``VIDEO_REPLICA_EXPORT_CIPHERTEXT_RETENTION_SECONDS``. Output carries counts
only — no business values, ciphertext or credentials are ever printed.
Intended to run from the ``video-replica-maintenance`` systemd timer
(deploy/systemd/), daily.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

import psycopg

from app.activation_code_service import (
    count_expired_export_ciphertexts,
    purge_expired_export_ciphertexts,
)
from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge expired activation-export ciphertext (M2 review M2)."
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

    now = datetime.now(UTC)
    with psycopg.connect(database_url) as conn:
        if args.dry_run:
            eligible = count_expired_export_ciphertexts(conn, now=now)
            print(f"expired export ciphertexts eligible for purge: {eligible}")
            return 0
        with conn.transaction():
            purged = purge_expired_export_ciphertexts(conn, now=now)
    print(f"purged {purged} expired export ciphertext package(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
