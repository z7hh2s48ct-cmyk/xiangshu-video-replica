"""Close WeChat Native recharge orders whose payment window lapsed unpaid.

A Native order is payable for a fixed window (the same one passed to WeChat as
``time_expire``). Without this sweep an unpaid order stays PENDING forever, which
blocks changing the merchant identity and the customer's credit conversion, and
leaves the orders page full of rows that can never be paid.

CLOSED is not a refusal to settle: ``confirm_recharge_payment`` still accepts a
late notification on a CLOSED order, so a callback WeChat retries for hours still
credits the wallet.

Only aggregate counts are printed. No user, order, amount or merchant data leaves
the process.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn
from app.db_portable import BusinessConnection
from app.wechat_native_client import NATIVE_ORDER_VALIDITY_SECONDS
from app.zpay_payments import close_expired_native_orders, count_expired_native_orders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Close expired unpaid WeChat Native recharge orders."
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="PostgreSQL DSN (defaults to VIDEO_REPLICA_DATABASE_URL)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="maximum expired orders closed in this run",
    )
    parser.add_argument(
        "--validity-seconds",
        type=int,
        default=NATIVE_ORDER_VALIDITY_SECONDS,
        help="how long a Native order stays payable after creation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the expired-order backlog without closing anything",
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
    if args.validity_seconds < 1:
        print("error: --validity-seconds must be positive", file=sys.stderr)
        return 1

    with psycopg.connect(database_url) as raw, raw.transaction():
        conn = BusinessConnection.postgres(raw)
        if args.dry_run:
            backlog = count_expired_native_orders(conn, validity_seconds=args.validity_seconds)
            print(f"expired wechat_native orders awaiting closure: {backlog}")
            return 0
        closed = close_expired_native_orders(
            conn, limit=args.limit, validity_seconds=args.validity_seconds
        )

    print(f"closed expired wechat_native orders: {closed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
