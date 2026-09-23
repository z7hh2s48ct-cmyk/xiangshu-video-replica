from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal, TypedDict, cast
from uuid import uuid4

from app.db_portable import BusinessConnection, IntegrityConstraintError
from app.recharge_packages import grant_discount_from_snapshot, parse_package_snapshot
from app.wechat_native_client import (
    NATIVE_MERCHANT_SWITCH_BLOCK_SECONDS,
    NATIVE_ORDER_VALIDITY_SECONDS,
    NATIVE_RECONCILIATION_GRACE_SECONDS,
    WeChatOrderQueryResult,
)
from app.zpay import ALLOWED_ZPAY_CHANNELS

logger = logging.getLogger(__name__)

ZPAY_NOTIFY_BUSY_TIMEOUT_MS = 1000
RechargeStatus = Literal["PENDING", "PAID", "FAILED", "CLOSED"]


@dataclass(frozen=True)
class SettlementProviderSpec:
    """Provider-specific knobs for the single shared recharge settlement routine.

    ``confirm_recharge_payment`` is the one idempotent settlement path for every
    provider; this spec carries the only per-provider variation so the fund logic
    never forks. ``trade_no_column`` names the ``recharge_orders`` column that stores
    the provider trade reference: ZPay uses ``provider_trade_no`` while WeChat Native
    uses ``transaction_id`` (migration 083 forces a wechat_native order's
    ``provider_trade_no`` to stay NULL and its ``transaction_id`` to be NOT NULL once
    PAID). The column name is a code-controlled constant, never user input.
    """

    provider_name: str
    provider_label: str
    error_prefix: str
    channel_universe: Collection[str]
    trade_no_column: str


# WeChat Native only ever settles the wxpay channel; kept local so this fund module
# does not import the provider module (avoids an import cycle at registration time).
_WECHAT_NATIVE_CHANNEL = "wxpay"
_WECHAT_NATIVE_CHANNEL_UNIVERSE = frozenset({_WECHAT_NATIVE_CHANNEL})

ZPAY_SETTLEMENT_SPEC = SettlementProviderSpec(
    provider_name="zpay",
    provider_label="ZPay",
    error_prefix="ZPAY",
    channel_universe=ALLOWED_ZPAY_CHANNELS,
    trade_no_column="provider_trade_no",
)
WECHAT_NATIVE_SETTLEMENT_SPEC = SettlementProviderSpec(
    provider_name="wechat_native",
    provider_label="WeChat Pay",
    error_prefix="WECHAT",
    channel_universe=_WECHAT_NATIVE_CHANNEL_UNIVERSE,
    trade_no_column="transaction_id",
)

# Allowlist guard for the column identifier interpolated into settlement SQL. It only
# ever comes from the frozen specs above, but a fund path validates the identifier it
# splices rather than trusting the call site.
_SETTLEMENT_TRADE_COLUMNS = frozenset({"provider_trade_no", "transaction_id"})


class RechargeOrderData(TypedDict):
    order_no: str
    status: RechargeStatus
    amount_fen: int
    credits: int
    channel: str
    created_at: str
    paid_at: str | None


class PaymentConfirmationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def read_recharge_order(
    conn: BusinessConnection,
    *,
    merchant_order_no: str,
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            """
            SELECT
                id, user_id, merchant_order_no, provider, provider_trade_no, channel, status,
                amount_fen, credits, notify_digest, created_at, paid_at
            FROM recharge_orders
            WHERE merchant_order_no = %s
            """,
            (merchant_order_no,),
        ).fetchone(),
    )


def _read_settlement_order(
    conn: BusinessConnection, *, merchant_order_no: str, trade_no_column: str
) -> sqlite3.Row | None:
    """Read a recharge order for settlement, aliasing the trade reference column.

    Same projection as ``read_recharge_order`` but the provider trade reference is
    selected from ``trade_no_column`` (``provider_trade_no`` for ZPay,
    ``transaction_id`` for WeChat Native) under the fixed alias ``trade_ref`` so the
    settlement routine stays column-agnostic. The public ``read_recharge_order`` keeps
    its ZPay-shaped ``provider_trade_no`` projection for the manual-sync route.
    """
    if trade_no_column not in _SETTLEMENT_TRADE_COLUMNS:
        raise ValueError(f"Unsupported settlement trade column: {trade_no_column}")
    return cast(
        sqlite3.Row | None,
        conn.execute(
            f"""
            SELECT
                id, user_id, merchant_order_no, provider, channel, status,
                amount_fen, credits, notify_digest, created_at, paid_at,
                package_snapshot_json,
                {trade_no_column} AS trade_ref
            FROM recharge_orders
            WHERE merchant_order_no = %s
            FOR UPDATE
            """,
            (merchant_order_no,),
        ).fetchone(),
    )


def serialize_recharge_order(row: sqlite3.Row) -> RechargeOrderData:
    return {
        "order_no": str(row["merchant_order_no"]),
        "status": cast(RechargeStatus, str(row["status"])),
        "amount_fen": int(row["amount_fen"]),
        "credits": int(row["credits"]),
        "channel": str(row["channel"]),
        "created_at": str(row["created_at"]),
        "paid_at": None if row["paid_at"] is None else str(row["paid_at"]),
    }


# "PENDING and older than N seconds" — the shared shape behind the expiry sweep
# (N = the payment window) and the reconciliation sweep (N = the callback grace).
_PENDING_NATIVE_OLDER_THAN = (
    "provider = 'wechat_native' AND status = 'PENDING' "
    "AND created_at::timestamptz + make_interval(secs => %s) <= now()"
)


def count_expired_native_orders(
    conn: BusinessConnection, *, validity_seconds: int = NATIVE_ORDER_VALIDITY_SECONDS
) -> int:
    """How many Native orders have outlived their payment window while still PENDING."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_PENDING_NATIVE_OLDER_THAN}",  # noqa: S608
        (float(validity_seconds),),
    ).fetchone()
    assert row is not None
    return int(row[0])


def close_expired_native_orders(
    conn: BusinessConnection,
    *,
    limit: int,
    validity_seconds: int = NATIVE_ORDER_VALIDITY_SECONDS,
) -> int:
    """Retire Native orders nobody paid before their window lapsed; return the count.

    ``CLOSED`` is the same terminal state a customer-closed order reaches, and
    ``confirm_recharge_payment`` still settles from it, so a notification that
    arrives late — WeChat retries for hours — still credits the wallet. The sweep
    only stops orders that can no longer be paid from sitting in the orders page
    and the reconciliation reports as PENDING forever.

    It deliberately does *not* relax the merchant-identity guard in
    ``SettingsRepository.save_wechat_native_config`` or the credit-conversion
    check: both count CLOSED as well, precisely because a CLOSED order can still
    settle against the current merchant. Narrowing either one needs a decision
    about how long after expiry WeChat may still push a notification.

    ``SKIP LOCKED`` leaves any order a callback is currently settling to that
    callback rather than waiting on its lock.
    """
    updated = conn.execute(
        f"""
        UPDATE recharge_orders SET status = 'CLOSED'
        WHERE id IN (
            SELECT id FROM recharge_orders
            WHERE {_PENDING_NATIVE_OLDER_THAN}
            ORDER BY created_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        """,  # noqa: S608
        (float(validity_seconds), limit),
    )
    return int(updated.rowcount)


def count_stale_pending_native_orders(
    conn: BusinessConnection, *, grace_seconds: int = NATIVE_RECONCILIATION_GRACE_SECONDS
) -> int:
    """How many Native orders have sat PENDING past the callback grace.

    The reconciliation sweep's backlog: same shape as the expiry predicate, but
    the window is the much shorter grace after which the callback path alone is
    no longer trusted.
    """
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_PENDING_NATIVE_OLDER_THAN}",  # noqa: S608
        (float(grace_seconds),),
    ).fetchone()
    assert row is not None
    return int(row[0])


_MERCHANT_SWITCH_BLOCKING_NATIVE_PREDICATE = (
    "provider = 'wechat_native' AND status IN ('PENDING', 'CLOSED') "
    "AND created_at::timestamptz + make_interval(secs => %s) > now()"
)


def count_blocking_native_orders(
    conn: BusinessConnection, *, block_seconds: int = NATIVE_MERCHANT_SWITCH_BLOCK_SECONDS
) -> int:
    """Native orders still inside their payment-plus-callback window.

    The settlement-drain criterion behind the merchant-identity guard: an order
    blocks a merchant change only while it can still become PAID — payable QR
    until the window ends (``NATIVE_ORDER_VALIDITY_SECONDS``), retrying
    callbacks for roughly a day after that (``NATIVE_SETTLEMENT_DRAIN_SECONDS``).
    Past the horizon nothing can land anymore, so the order stops blocking and
    a merchant identity can always be changed in finite time.
    """
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_MERCHANT_SWITCH_BLOCKING_NATIVE_PREDICATE}",  # noqa: S608
        (float(block_seconds),),
    ).fetchone()
    assert row is not None
    return int(row[0])


def list_stale_pending_native_orders(
    conn: BusinessConnection, *, grace_seconds: int, limit: int
) -> list[str]:
    """Order numbers of the oldest Native orders still PENDING past the grace.

    Read-only and lock-free on purpose: the caller must have no transaction open
    that would span the external query-order calls, so a concurrently arriving
    callback never waits on the sweep's snapshot.
    """
    rows = conn.execute(
        f"""
        SELECT merchant_order_no FROM recharge_orders
        WHERE {_PENDING_NATIVE_OLDER_THAN}
        ORDER BY created_at
        LIMIT %s
        """,  # noqa: S608
        (float(grace_seconds), limit),
    ).fetchall()
    return [str(row[0]) for row in rows]


@dataclass(frozen=True)
class NativeReconciliationOutcome:
    """Counts-only result of one reconciliation sweep pass (the CLI prints these)."""

    queried: int
    settled: int
    unpaid: int
    skipped: int


def reconcile_stale_pending_native_orders(
    conn: BusinessConnection,
    *,
    query_order: Callable[[str], WeChatOrderQueryResult],
    grace_seconds: int = NATIVE_RECONCILIATION_GRACE_SECONDS,
    limit: int = 200,
) -> NativeReconciliationOutcome:
    """Recover Native orders WeChat has paid but no callback ever confirmed.

    掉单兜底 — the lost-order fallback. The customer polls only our own database
    and WeChat retries a failed notification for hours at most, so a notify
    route that was down means money arrived that nothing on our side credits.
    The sweep asks WeChat about each stale PENDING order and settles the paid
    ones through ``confirm_recharge_payment`` — the same idempotent fund path,
    spec and guards as the callback — so a sweep racing a late callback is a
    PAID short-circuit, never a double credit.

    ``query_order`` performs the external call, so settlement row locks must
    never be held across it: the CLI runs the sweep on an autocommit connection
    (each settlement below is then its own short transaction), while tests may
    drive the whole pass inside one outer transaction. A query that fails,
    answers for a different order, or is refused by the settlement guards is
    skipped with a warning and retried on the next run; an unpaid answer stays
    PENDING for the expiry sweep to retire.
    """
    order_nos = list_stale_pending_native_orders(conn, grace_seconds=grace_seconds, limit=limit)
    queried = settled = unpaid = skipped = 0
    for order_no in order_nos:
        try:
            answer = query_order(order_no)
        except Exception as exc:  # noqa: BLE001 - one dead order must not strand the rest
            logger.warning(
                "reconciliation query for order %s failed (%s); it stays PENDING",
                order_no,
                exc,
            )
            skipped += 1
            continue
        queried += 1
        if answer.out_trade_no != order_no:
            logger.warning(
                "reconciliation query answered for %s while asking about %s; settling nothing",
                answer.out_trade_no,
                order_no,
            )
            skipped += 1
            continue
        if not answer.paid:
            unpaid += 1
            continue
        if answer.transaction_id is None or answer.amount_fen is None:
            logger.warning(
                "reconciliation query for order %s is paid but incomplete; settling nothing",
                order_no,
            )
            skipped += 1
            continue
        try:
            confirm_recharge_payment(
                conn,
                merchant_order_no=order_no,
                provider_trade_no=answer.transaction_id,
                amount_fen=answer.amount_fen,
                channel=_WECHAT_NATIVE_CHANNEL,
                source_digest=answer.response_digest,
                provider_spec=WECHAT_NATIVE_SETTLEMENT_SPEC,
            )
        except PaymentConfirmationError as exc:
            logger.warning(
                "reconciliation refused to settle order %s: %s; it stays PENDING",
                order_no,
                exc.code,
            )
            skipped += 1
            continue
        settled += 1
    return NativeReconciliationOutcome(
        queried=queried, settled=settled, unpaid=unpaid, skipped=skipped
    )


def confirm_recharge_payment(
    conn: BusinessConnection,
    *,
    merchant_order_no: str,
    provider_trade_no: str,
    amount_fen: int,
    channel: str,
    source_digest: str,
    allowed_channels: Collection[str] | None = None,
    provider_spec: SettlementProviderSpec = ZPAY_SETTLEMENT_SPEC,
) -> sqlite3.Row:
    """Idempotently settle a recharge order and credit the owner's wallet.

    ``provider_trade_no`` carries the provider's trade reference *value* regardless of
    which column stores it: ``provider_spec.trade_no_column`` names that column
    (``provider_trade_no`` for ZPay, ``transaction_id`` for WeChat Native). The default
    spec keeps every existing ZPay caller byte-identical.
    """
    spec = provider_spec
    prefix = spec.error_prefix
    label = spec.provider_label
    if not merchant_order_no or not provider_trade_no.strip():
        raise PaymentConfirmationError(
            f"{prefix}_PAYMENT_REFERENCE_INVALID",
            f"{label} order and trade numbers are required.",
            status_code=400,
        )

    conn.execute(f"PRAGMA busy_timeout = {ZPAY_NOTIFY_BUSY_TIMEOUT_MS}")
    # A PG nested transaction is a real savepoint. Route handlers may catch a
    # business error and return a normal response, so the rollback must happen
    # here before that catch. Releasing this savepoint never commits the outer
    # request transaction. BusinessConnection owns a PostgreSQL connection.
    transaction = conn.raw.transaction()
    try:
        with transaction:
            order = _read_settlement_order(
                conn, merchant_order_no=merchant_order_no, trade_no_column=spec.trade_no_column
            )
            if order is None:
                raise PaymentConfirmationError(
                    f"{prefix}_ORDER_NOT_FOUND",
                    "Recharge order does not exist.",
                    status_code=404,
                )
            if str(order["provider"]) != spec.provider_name:
                raise PaymentConfirmationError(
                    f"{prefix}_PROVIDER_MISMATCH",
                    f"Recharge order provider does not match {label}.",
                )
            if int(order["amount_fen"]) != amount_fen:
                raise PaymentConfirmationError(
                    f"{prefix}_AMOUNT_MISMATCH",
                    f"{label} amount does not match the stored recharge order.",
                )
            merchant_channels = set(allowed_channels or (str(order["channel"]),))
            if channel not in spec.channel_universe or channel not in merchant_channels:
                raise PaymentConfirmationError(
                    f"{prefix}_CHANNEL_MISMATCH",
                    f"{label} channel is not enabled for this merchant.",
                )

            bound_order = conn.execute(
                f"""
                SELECT merchant_order_no
                FROM recharge_orders
                WHERE {spec.trade_no_column} = %s AND merchant_order_no != %s
                """,
                (provider_trade_no, merchant_order_no),
            ).fetchone()
            if bound_order is not None:
                raise PaymentConfirmationError(
                    f"{prefix}_TRADE_ALREADY_BOUND",
                    f"{label} trade number is already bound to another recharge order.",
                )

            existing_trade_no = order["trade_ref"]
            if existing_trade_no is not None and str(existing_trade_no) != provider_trade_no:
                raise PaymentConfirmationError(
                    f"{prefix}_TRADE_NO_MISMATCH",
                    f"{label} trade number does not match the stored recharge order.",
                )
            if str(order["status"]) == "PAID":
                # The order lock serializes concurrent replays before any writes.
                return order
            if str(order["status"]) not in {"PENDING", "CLOSED"}:
                raise PaymentConfirmationError(
                    f"{prefix}_ORDER_NOT_SETTLEABLE",
                    "Recharge order is not waiting for settlement.",
                )

            updated = conn.execute(
                f"""
                UPDATE recharge_orders
                SET status = 'PAID',
                    {spec.trade_no_column} = %s,
                    notify_digest = %s,
                    paid_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status IN ('PENDING', 'CLOSED')
                """,
                (provider_trade_no, source_digest, str(order["id"])),
            )
            if updated.rowcount != 1:
                raise PaymentConfirmationError(
                    f"{prefix}_ORDER_CHANGED",
                    "Recharge order changed while payment was being confirmed.",
                )

            conn.execute(
                """
                INSERT INTO wallet_transactions (
                    id, user_id, type, available_delta, reserved_delta,
                    recharge_order_id, task_id, billing_round, idempotency_key, auth_source
                ) VALUES (%s, %s, 'CHARGE', %s, 0, %s, NULL, NULL, %s, 'internal')
                """,
                (
                    str(uuid4()),
                    str(order["user_id"]),
                    int(order["credits"]),
                    str(order["id"]),
                    f"{spec.provider_name}:charge:{order['id']}",
                ),
            )
            wallet = conn.execute(
                """
                UPDATE wallets
                SET available_credits = available_credits + %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND available_credits <= 2147483647 - %s
                """,
                (int(order["credits"]), str(order["user_id"]), int(order["credits"])),
            )
            if wallet.rowcount != 1:
                # Either the wallet is missing or the credit would overflow int4
                # (the T23 admin-adjustment bound, M3 review LOW). Distinguish so
                # the callback answers a final 409 instead of a retried 500.
                exists = conn.execute(
                    "SELECT 1 FROM wallets WHERE user_id = %s", (str(order["user_id"]),)
                ).fetchone()
                if exists is None:
                    raise PaymentConfirmationError(
                        "WALLET_NOT_FOUND",
                        "Wallet record is missing for the recharge order owner.",
                        status_code=500,
                    )
                raise PaymentConfirmationError(
                    "WALLET_CREDIT_OVERFLOW",
                    "Wallet credit balance would overflow; settle manually.",
                    status_code=409,
                )

            # 套餐权益在资金入账后同事务授予（幂等：重放走上方 PAID 早退，不重复授予）。
            # 快照漂移（非法折扣率等）不得回滚已支付入账：记录告警并跳过授予，
            # 资金流水必须落定，权益由人工按订单快照补授。
            try:
                grant_discount_from_snapshot(
                    conn,
                    user_id=str(order["user_id"]),
                    source_recharge_order_id=str(order["id"]),
                    package_snapshot=parse_package_snapshot(order["package_snapshot_json"]),
                )
            except (ValueError, TypeError) as exc:
                logger.error(
                    "package discount grant skipped after payment settle: "
                    "merchant_order_no=%s user_id=%s error=%s",
                    merchant_order_no,
                    order["user_id"],
                    exc,
                )

            confirmed = _read_settlement_order(
                conn, merchant_order_no=merchant_order_no, trade_no_column=spec.trade_no_column
            )
            if confirmed is None:  # pragma: no cover - protected by the transaction above
                raise RuntimeError("confirmed recharge order disappeared")
            return confirmed
    except IntegrityConstraintError as exc:
        if exc.sqlstate != "23505":
            raise
        raise PaymentConfirmationError(
            f"{prefix}_SETTLEMENT_CONFLICT",
            "Payment settlement conflicts with an existing ledger entry.",
        ) from exc
