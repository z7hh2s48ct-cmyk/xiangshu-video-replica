"""Settle WeChat Native orders WeChat has paid but no callback ever confirmed.

The customer client polls only our own database, and WeChat retries a failed
notification for hours at most: when the notify route was unreachable, money
arrived that nothing on our side ever credits. This sweep asks WeChat directly
about every Native order still PENDING past the callback grace and settles the
paid ones through ``confirm_recharge_payment`` — the same idempotent fund path
the callback uses — so running next to a live callback stream can never double
credit.

Unpaid answers stay PENDING for the expiry sweep to retire, and an answer the
settlement guards dispute settles nothing. Only aggregate counts are printed.
No user, order, amount or merchant data leaves the process.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn
from app.db_portable import BusinessConnection
from app.settings import SettingsRepository
from app.wechat_native_client import (
    NATIVE_RECONCILIATION_GRACE_SECONDS,
    WeChatNativeClient,
    merchant_config_from_settings,
)
from app.zpay_payments import (
    count_stale_pending_native_orders,
    reconcile_stale_pending_native_orders,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Settle WeChat Native orders whose payment callback was lost."
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="PostgreSQL DSN (defaults to VIDEO_REPLICA_DATABASE_URL)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="maximum orders reconciled in this run",
    )
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=NATIVE_RECONCILIATION_GRACE_SECONDS,
        help="how long an order sits PENDING before the sweep asks WeChat about it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the reconciliation backlog without contacting WeChat",
    )
    args = parser.parse_args(argv)

    if args.limit < 1:
        print("error: --limit must be positive", file=sys.stderr)
        return 1
    if args.grace_seconds < 1:
        print("error: --grace-seconds must be positive", file=sys.stderr)
        return 1
    try:
        database_url = resolve_cli_pg_dsn(args.database_url)
    except CliDatabaseConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Autocommit: the read snapshots close themselves, so no transaction ever
    # spans a WeChat query-order call, and each settlement inside the sweep is
    # its own short transaction (the fund path opens it explicitly).
    with psycopg.connect(database_url, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)
        backlog = count_stale_pending_native_orders(conn, grace_seconds=args.grace_seconds)
        if args.dry_run:
            print(f"stale pending wechat_native orders awaiting reconciliation: {backlog}")
            return 0
        if backlog == 0:
            print("stale pending wechat_native orders: 0; nothing to reconcile")
            return 0
        try:
            # Loaded before the sweep so an unusable merchant configuration fails
            # the run loudly instead of skipping every order one query at a time.
            merchant = merchant_config_from_settings(
                SettingsRepository(conn).load_wechat_native_config()
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        client = WeChatNativeClient()
        outcome = reconcile_stale_pending_native_orders(
            conn,
            query_order=lambda order_no: client.query_order(
                merchant=merchant, out_trade_no=order_no
            ),
            grace_seconds=args.grace_seconds,
            limit=args.limit,
        )

    print(
        f"reconciled stale wechat_native orders: {outcome.queried} queried, "
        f"{outcome.settled} settled, {outcome.unpaid} unpaid, {outcome.skipped} skipped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
