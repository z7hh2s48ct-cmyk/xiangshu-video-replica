from __future__ import annotations

import base64
import io
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

import psycopg
import qrcode  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, StrictInt

import app.wechat_native_provider  # noqa: F401

# Import to trigger provider registration
import app.zpay_provider  # noqa: F401
from app.auth import AuthenticatedUser, Database
from app.billing_catalog import SERVICES
from app.customer_fence import (
    BusinessDbDep,
    CustomerSessionSnapshot,
    customer_read_transaction,
    customer_session_snapshot,
    fenced_pg_transaction,
)
from app.customer_idempotency import (
    EnvelopeRecord,
    IdempotencyKeyError,
    complete_envelope,
    customer_aead_key,
    envelope_aad,
    highest_customer_aead_key,
    idempotency_key_digests,
    insert_envelope,
    load_envelope,
    open_response,
    recovery_window_seconds,
    request_hash,
    seal_response,
)
from app.customer_pricing import read_pricing
from app.db_portable import BusinessConnection
from app.ops_metrics import set_current_trace_fields
from app.payment_provider import (
    DeploymentConfig,
    MerchantConfig,
    PaymentCodeError,
    PaymentFormResult,
    PaymentProvider,
    get_payment_provider,
)
from app.permissions import require_not_auditor
from app.recharge_packages import RechargePackage, build_package_snapshot, read_package
from app.security_rate_limit import _server_now, client_ip_from_request
from app.settings import SettingsRepository, effective_customer_billing_settings
from app.sub_account_quota import read_quota_used
from app.usage_billing import resolve_wallet_owner
from app.wallet_routes import (
    WalletResponse,
    WalletTransactionPage,
    WalletTransactionResponse,
    pricing_breakdown,
)
from app.wechat_native_client import (
    NATIVE_ORDER_MIN_REMAINING_SECONDS,
    NATIVE_ORDER_VALIDITY_SECONDS,
)
from app.zpay import generate_merchant_order_no
from app.zpay_payments import read_recharge_order, serialize_recharge_order

router = APIRouter(prefix="/api", tags=["recharge"])
MAX_ORDER_NUMBER_ATTEMPTS = 3
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"
RECHARGE_OPERATION = "recharge:create"

# ``recharge_orders.amount_fen`` is an int4 column (migration 022); a value
# beyond this bound would surface as a PostgreSQL IntegerFieldOverflow 500
# instead of a 422 - the same class of gap PR #54 closed for admin
# adjustments (wallet int4 overflow). ``credits`` is bounded by the same
# constant because ``credits = amount_fen // charged_unit_price_fen``.
INT4_MAX_FEN = 2_147_483_647


class CreateRechargeOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_fen: StrictInt
    # 套餐下单（客户通道）：指名管理员配置的档位；amount_fen 必须等于套餐金额，
    # credits/权益以套餐为准并冻结进 package_snapshot_json。不选套餐时保持自定义金额。
    package_id: str | None = None


class RechargeOrderResponse(BaseModel):
    order_no: str
    status: Literal["PENDING"]
    amount_fen: int
    credits: int
    gateway_url: str
    method: Literal["POST"]
    form_fields: dict[str, str]


class RechargeOrderStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str
    status: Literal["PENDING", "PAID", "FAILED", "CLOSED"]
    amount_fen: int
    credits: int
    channel: str
    created_at: str
    paid_at: str | None


class RechargeOrderPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RechargeOrderStatusResponse]
    total: int
    limit: int
    offset: int


class CustomerPaymentCodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str
    amount_fen: int
    credits: int
    qr_image_url: str
    payment_url: str


class CustomerProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    username: str
    display_name: str
    joined_at: str
    activation_code_masked: str | None
    activation_status: str | None
    activated_at: str | None
    device_slots_used: int
    device_slots_total: int | None
    # CW-062 sub-account identity: mirrors the password-login response so a
    # restored session (heartbeat + profile) can badge the workspace without
    # a second round-trip. A master answers 'MASTER' with no parent.
    account_type: str = "MASTER"
    parent_user_id: str | None = None
    parent_display_name: str | None = None
    # Phase 3a monthly quota view: only a sub-account session carries these
    # (a master owns the wallet and is never capped, so it answers null/null).
    # A sub-account without a quota row answers null for the cap while
    # ``quota_used_credits`` still reports the month's consumption.
    monthly_quota_credits: int | None = None
    quota_used_credits: int | None = None


class UpdateCustomerProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str


def get_zpay_provider() -> PaymentProvider:
    """Dependency: get the ZPay payment provider."""
    return get_payment_provider("zpay")


ZPayProviderDep = Annotated[PaymentProvider, Depends(get_zpay_provider)]


# ---------------------------------------------------------------------------
# Shared recharge-order creation core (T22 review: the customer route and the
# internal route previously duplicated ~99 lines and the copy drifted - the
# drift broke the collision retry on PostgreSQL twice over). Both routes now
# share the staged helpers below and keep the transaction/retry shape
# identical: the retry loop sits OUTSIDE ``db.write()`` so every attempt runs
# in a fresh fenced transaction (a failed INSERT aborts the PG transaction,
# an in-transaction retry would hit InFailedSqlTransaction).
# ---------------------------------------------------------------------------


def _stage_recharge_preconditions(
    conn: BusinessConnection,
    *,
    amount_fen: int,
    provider: PaymentProvider,
    customer_user_id: str | None = None,
    validate_amount: bool = True,
) -> tuple[dict[str, int], MerchantConfig, DeploymentConfig]:
    """Billing settings + amount validation + provider configuration, shared."""
    settings_repo = SettingsRepository(conn)
    billing = (
        settings_repo.read_customer_billing_settings(user_id=customer_user_id)
        if customer_user_id is not None
        else settings_repo.read_billing_settings()
    )
    if customer_user_id is not None:
        try:
            billing = effective_customer_billing_settings(
                billing,
                user_id=customer_user_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ACCEPTANCE_PAYMENT_CONFIGURATION_INVALID",
                    "message": "The controlled payment rehearsal is not configured safely.",
                },
            ) from exc
    if customer_user_id is not None:
        price_version, credit_config = read_pricing(conn)
        if credit_config is not None:
            billing = {
                **billing,
                "points_per_yuan": credit_config.points_per_yuan,
                "credit_price_version": price_version,
            }
    if validate_amount:
        validate_recharge_amount(amount_fen, billing)
    elif amount_fen < billing["min_recharge_fen"]:
        # 套餐单豁免步长/整除校验，但生效起充额下限仍生效：低于起充额的套餐
        # 不得在客户价格调整后被下单（数据库 CHECK 会直接拒绝，必须在路由层
        # 先给出 422 语义而不是 500）。
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECHARGE_PACKAGE_BELOW_MINIMUM",
                "message": "该套餐金额低于当前起充金额，请选择其他档位。",
            },
        )
    try:
        merchant = provider.load_merchant_config(conn)
        deployment = provider.load_deployment_config()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ZPAY_CONFIGURATION_INVALID", "message": str(exc)},
        ) from exc
    return billing, merchant, deployment


def _insert_recharge_order(
    conn: BusinessConnection,
    *,
    user_id: str,
    amount_fen: int,
    pricing_scope: Literal["INTERNAL", "CUSTOMER_STANDARD"],
    merchant_order_no: str,
    billing: dict[str, int],
    merchant: MerchantConfig,
    deployment: DeploymentConfig,
    provider: PaymentProvider,
    package: RechargePackage | None = None,
) -> RechargeOrderResponse:
    """Insert one PENDING recharge order and build its payment form.

    ``package`` 非空时为套餐订单：credits 逐字取套餐（赠送口径），并冻结
    ``package_snapshot_json``；套餐金额已由调用方校验等于订单金额。
    """
    charged_unit_price_fen = billing["charged_unit_price_fen"]
    if package is not None:
        credits = package.credits
        package_snapshot_json: str | None = json.dumps(
            build_package_snapshot(package), ensure_ascii=False
        )
    else:
        credits = (
            amount_fen * billing["points_per_yuan"] // 100
            if "points_per_yuan" in billing
            else amount_fen // charged_unit_price_fen
        )
        package_snapshot_json = None
    if not 1 <= credits <= INT4_MAX_FEN:
        raise HTTPException(
            422, detail={"code": "INVALID_RECHARGE_AMOUNT", "message": "充值积分超出允许范围。"}
        )
    price_snapshot = (
        json.dumps(
            {
                "version": billing["credit_price_version"],
                "points_per_yuan": billing["points_per_yuan"],
            }
        )
        if "points_per_yuan" in billing
        else None
    )
    extra_column = ", credit_pricing_snapshot_json" if conn.is_postgres else ""
    extra_value = ", %s" if conn.is_postgres else ""
    # 套餐列随套餐迁移仅在 PG 泳道存在（客户通道本就是 PG-only）。
    package_column = ", package_id, package_snapshot_json" if conn.is_postgres else ""
    package_value = ", %s, %s" if conn.is_postgres else ""
    # Native QR is obtained after the local order commits; never call a paid
    # gateway while holding the wallet transaction or its idempotency envelope.
    payment_form = (
        PaymentFormResult(gateway_url="", method="POST", form_fields={})
        if provider.name == "wechat_native"
        else provider.create_payment_form(
            merchant_order_no=merchant_order_no,
            amount_fen=amount_fen,
            credits=credits,
            merchant=merchant,
            deployment=deployment,
        )
    )
    with conn:
        conn.execute(
            "INSERT INTO recharge_orders (\n"
            "    id, user_id, merchant_order_no, provider, provider_trade_no,\n"
            "    channel, status, pricing_scope,\n"
            "    base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,\n"
            "    min_recharge_fen_snapshot, recharge_step_fen_snapshot,\n"
            f"    amount_fen, credits{extra_column}"
            f"{package_column}\n"
            ") VALUES (%s, %s, %s, %s, NULL, %s, 'PENDING', %s, "
            f"%s, %s, %s, %s, %s, %s{extra_value}{package_value})\n",
            (
                str(uuid4()),
                user_id,
                merchant_order_no,
                provider.name,
                merchant.primary_channel,
                pricing_scope,
                billing["internal_base_unit_price_fen"],
                charged_unit_price_fen,
                billing["min_recharge_fen"],
                billing["recharge_step_fen"],
                amount_fen,
                credits,
            )
            + ((price_snapshot,) if conn.is_postgres else ())
            + (
                ((package.id if package is not None else None), package_snapshot_json)
                if conn.is_postgres
                else ()
            ),
        )
    set_current_trace_fields(user_id=user_id, order_id=merchant_order_no)
    return RechargeOrderResponse(
        order_no=merchant_order_no,
        status="PENDING",
        amount_fen=amount_fen,
        credits=credits,
        gateway_url=payment_form.gateway_url,
        method="POST",
        form_fields=payment_form.form_fields,
    )


def _retryable_merchant_order_collision(exc: Exception) -> bool:
    """True when the integrity failure is the retryable order-number draw.

    Matches both dialect messages: SQLite spells it
    ``UNIQUE constraint failed: recharge_orders.merchant_order_no`` while
    PostgreSQL reports the constraint name
    (``recharge_orders_merchant_order_key``) - the dotted form only exists on
    SQLite, so the narrower match silently broke the PG retry (review P1).
    """
    return "merchant_order_no" in str(exc)


@router.post(
    "/recharge-orders",
    response_model=RechargeOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_recharge_order(
    payload: CreateRechargeOrderRequest,
    db: BusinessDbDep,
    provider: ZPayProviderDep,
) -> RechargeOrderResponse:
    for _ in range(MAX_ORDER_NUMBER_ATTEMPTS):
        merchant_order_no = generate_merchant_order_no()
        try:
            with db.write() as (conn, user):
                require_not_auditor(
                    conn,
                    actor=user,
                    action="recharge.internal.create",
                    entity_type="recharge_order",
                    entity_id="new",
                )
                if user.role == "customer":
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "CUSTOMER_RECHARGE_ROUTE_REQUIRED",
                            "message": (
                                "Customer accounts must use /api/customer/recharge-orders."
                            ),
                        },
                    )
                if payload.package_id is not None:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "RECHARGE_PACKAGE_UNAVAILABLE",
                            "message": "充值套餐仅支持客户通道下单。",
                        },
                    )
                billing, merchant, deployment = _stage_recharge_preconditions(
                    conn,
                    amount_fen=payload.amount_fen,
                    provider=provider,
                )
                return _insert_recharge_order(
                    conn,
                    user_id=user.id,
                    amount_fen=payload.amount_fen,
                    pricing_scope="INTERNAL",
                    merchant_order_no=merchant_order_no,
                    billing=billing,
                    merchant=merchant,
                    deployment=deployment,
                    provider=provider,
                )
        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation) as exc:
            # The merchant-order-number collision is a retryable random draw; any
            # other integrity failure is a real bug and must surface.
            if _retryable_merchant_order_collision(exc):
                continue
            raise

    raise HTTPException(
        status_code=503,
        detail={"code": "ORDER_NUMBER_UNAVAILABLE", "message": "Unable to allocate order number."},
    )


# ============================================================================
# T22 / BILL-01: Customer top-up route (session-authenticated recharge)
# ============================================================================


def _require_master_for_recharge(conn: BusinessConnection, *, user_id: str) -> None:
    """Refuse top-ups requested from a sub-account seat (T2.10).

    The wallet belongs to the master; letting a sub create a recharge order
    would collect payment against an account with no wallet to credit.
    """
    row = conn.execute("SELECT parent_user_id FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is not None and row[0] is not None:
        raise HTTPException(
            status_code=403,
            detail={"code": "MASTER_ACCOUNT_REQUIRED", "message": "该操作仅限母账号执行。"},
        )


@router.post(
    "/customer/recharge-orders",
    response_model=RechargeOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customer_recharge_order(
    payload: CreateRechargeOrderRequest,
    request: Request,
    response: Response,
    db: BusinessDbDep,
    provider: ZPayProviderDep,
) -> RechargeOrderResponse:
    """T22: Customer can reuse ZPay to top-up the same wallet under their session.

    Key invariant guarantees (BILL-01):
    - Recharge does NOT change main code, device slots, session or user concurrency
    - Idempotency-Key envelope (T14 engine): a retry with the same key replays
      the sealed response without creating a second order; a different request
      under a spent key is a 409
    - Credits enter the same customer wallet (not P0 internal wallet)
    """
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "IDEMPOTENCY_KEY_REQUIRED",
                "message": "An Idempotency-Key header is required.",
            },
        )
    key_digests = idempotency_key_digests(idempotency_key)
    key_digest = key_digests[0]
    # 套餐选择必须进指纹：同金额不同套餐的 credits/权益不同（重放不得串档）。
    fingerprint_fields = {"amount_fen": str(payload.amount_fen)}
    if payload.package_id is not None:
        fingerprint_fields["package_id"] = payload.package_id
    req_hash = request_hash(fingerprint_fields)
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
    except IdempotencyKeyError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "IDEMPOTENCY_KEYS_UNAVAILABLE",
                "message": "Idempotency keys are not configured; recharge is refused.",
            },
        ) from None

    for _ in range(MAX_ORDER_NUMBER_ATTEMPTS):
        merchant_order_no = generate_merchant_order_no()
        try:
            with db.write() as (conn, user):
                # Sub-accounts never recharge: the organisation's credits are
                # topped up by the master (T2.10).
                _require_master_for_recharge(conn, user_id=user.id)
                # Serialize new orders against legacy wallet conversion; no network call here.
                conn.execute(
                    "SELECT user_id FROM wallets WHERE user_id = %s FOR UPDATE", (user.id,)
                ).fetchone()
                scope = f"recharge:{user.id}"
                matched = next(
                    (
                        (candidate, loaded)
                        for candidate in key_digests
                        if (
                            loaded := load_envelope(
                                _pg_conn(conn),
                                operation=RECHARGE_OPERATION,
                                scope=scope,
                                key_digest=candidate,
                            )
                        )
                        is not None
                    ),
                    None,
                )
                record = matched[1] if matched is not None else None
                matched_key_digest = matched[0] if matched is not None else key_digest
                envelope_id: str | None = None
                if record is None:
                    envelope_id = insert_envelope(
                        _pg_conn(conn),
                        operation=RECHARGE_OPERATION,
                        scope=scope,
                        key_digest=key_digest,
                        request_hash=req_hash,
                    )
                    if envelope_id is None:
                        # Concurrent same-key writer won the placeholder insert;
                        # load the committed envelope and treat it as a replay.
                        record = load_envelope(
                            _pg_conn(conn),
                            operation=RECHARGE_OPERATION,
                            scope=scope,
                            key_digest=key_digest,
                        )
                if record is not None:
                    _enforce_envelope_conflicts(record, req_hash=req_hash, conn=conn)
                    replayed = _open_recharge_envelope(
                        record,
                        scope=scope,
                        key_digest=matched_key_digest,
                    )
                    replayed_order = RechargeOrderResponse.model_validate(replayed)
                    set_current_trace_fields(user_id=user.id, order_id=replayed_order.order_no)
                    response.headers[REPLAY_HEADER] = "true"
                    return replayed_order

                # Serialize merchant changes with order creation so a new pending order
                # cannot race a merchant-identity update.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('payment:settings'))")
                active_provider = SettingsRepository(conn).read_active_payment_provider()
                selected_provider = (
                    provider if active_provider == "zpay" else get_payment_provider(active_provider)
                )
                # 套餐档位：金额由套餐定义（自定义金额保留：不选套餐时走原校验）。
                package: RechargePackage | None = None
                if payload.package_id is not None:
                    package = read_package(conn, payload.package_id)
                    if package is None or not package.is_active:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "RECHARGE_PACKAGE_NOT_FOUND",
                                "message": "充值套餐不存在或已下架，请刷新后重试。",
                            },
                        )
                    if package.amount_fen != payload.amount_fen:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "RECHARGE_PACKAGE_AMOUNT_MISMATCH",
                                "message": "充值金额与套餐不符，请刷新后重试。",
                            },
                        )
                billing, merchant, deployment = _stage_recharge_preconditions(
                    conn,
                    amount_fen=payload.amount_fen,
                    provider=selected_provider,
                    customer_user_id=user.id,
                    validate_amount=package is None,
                )
                order = _insert_recharge_order(
                    conn,
                    user_id=user.id,
                    amount_fen=payload.amount_fen,
                    pricing_scope="CUSTOMER_STANDARD",
                    merchant_order_no=merchant_order_no,
                    billing=billing,
                    merchant=merchant,
                    deployment=deployment,
                    provider=selected_provider,
                    package=package,
                )
                assert envelope_id is not None
                recovery_expires_at = (
                    (_server_now(_pg_conn(conn)) + timedelta(seconds=recovery_window_seconds()))
                    .replace(microsecond=0)
                    .isoformat()
                )
                sealed_ciphertext = seal_response(
                    order.model_dump(),
                    key=aead_key,
                    aad=envelope_aad(RECHARGE_OPERATION, scope, key_digest),
                )
                complete_envelope(
                    _pg_conn(conn),
                    envelope_id,
                    ciphertext=sealed_ciphertext,
                    key_version=aead_key_version,
                    recovery_expires_at=recovery_expires_at,
                )
                return order
        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation) as exc:
            if _retryable_merchant_order_collision(exc):
                continue
            raise

    raise HTTPException(
        status_code=503,
        detail={"code": "ORDER_NUMBER_UNAVAILABLE", "message": "Unable to allocate order number."},
    )


def _pg_conn(conn: BusinessConnection) -> psycopg.Connection:
    """Narrow the BusinessConnection backend to the PostgreSQL connection the
    idempotency engine and the server clock operate on (customer lane only)."""
    return conn.raw


def _enforce_envelope_conflicts(
    record: EnvelopeRecord,
    *,
    req_hash: str,
    conn: BusinessConnection,
) -> None:
    """The T14 replay contract: hash conflict / unrecoverable / expired -> 409."""
    if record.request_hash != req_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This idempotency key was already used for a different request.",
            },
        )
    if record.ciphertext is None or record.key_version is None:
        # Purged or never completed: the key is spent and the response is no
        # longer recoverable (T14 / ACT-07 contract).
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This idempotency key is no longer recoverable.",
            },
        )
    recovery_expires_at = record.recovery_expires_at
    if recovery_expires_at is not None and (
        datetime.fromisoformat(str(recovery_expires_at)) <= _server_now(_pg_conn(conn))
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "The recovery window for this key has expired.",
            },
        )


def _open_recharge_envelope(
    record: EnvelopeRecord,
    *,
    scope: str,
    key_digest: str,
) -> dict[str, object]:
    """Unseal a replayable response; an unopenable seal is unrecoverable."""
    assert record.ciphertext is not None and record.key_version is not None
    try:
        return open_response(
            record.ciphertext,
            key=customer_aead_key(record.key_version),
            aad=envelope_aad(RECHARGE_OPERATION, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This idempotency key is no longer recoverable.",
            },
        ) from None


@router.get(
    "/customer/recharge-orders/{order_no}",
    response_model=RechargeOrderStatusResponse,
)
def read_customer_recharge_order_status(
    order_no: str,
    request: Request,
) -> RechargeOrderStatusResponse:
    """Customer-lane order status on the API-Key whitelist (§2.2): a session
    token is re-verified inside the fenced read transaction, an ``xsk_live_``
    key rides the independent lane, and another user's order number is a 404
    (no existence leak), mirroring the internal-lane route's ownership check."""
    with customer_read_transaction(request) as (conn, user_id):
        business_conn = BusinessConnection.postgres(conn)
        order = read_recharge_order(business_conn, merchant_order_no=order_no)
        if order is None or str(order["user_id"]) != user_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RECHARGE_ORDER_NOT_FOUND",
                    "message": "Recharge order does not exist.",
                },
            )
        return RechargeOrderStatusResponse(**serialize_recharge_order(order))


@router.delete(
    "/customer/recharge-orders/{order_no}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def close_customer_recharge_order(order_no: str, request: Request) -> Response:
    """Close an unpaid order without erasing its accounting lineage.

    The UI calls this action "delete", while the database keeps the order as
    CLOSED so callbacks, support and reconciliation retain one authoritative
    record. Repeating the request is intentionally idempotent.
    """
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        row = conn.execute(
            "SELECT id, status FROM recharge_orders "
            "WHERE merchant_order_no = %s AND user_id = %s FOR UPDATE",
            (order_no, ctx.user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RECHARGE_ORDER_NOT_FOUND",
                    "message": "Recharge order does not exist.",
                },
            )
        current_status = str(row[1])
        if current_status == "CLOSED":
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if current_status != "PENDING":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RECHARGE_ORDER_NOT_PENDING",
                    "message": "Only an unpaid recharge order can be closed.",
                },
            )
        conn.execute(
            "UPDATE recharge_orders SET status = 'CLOSED' WHERE id = %s",
            (str(row[0]),),
        )
        _insert_customer_audit(
            conn,
            user_id=ctx.user_id,
            action="customer.recharge_order.closed",
            entity_type="recharge_order",
            entity_id=order_no,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/customer/recharge-orders/{order_no}/payment-code",
    response_model=CustomerPaymentCodeResponse,
)
def create_customer_payment_code(
    order_no: str,
    request: Request,
    provider: ZPayProviderDep,
) -> CustomerPaymentCodeResponse:
    """Return a display-ready QR image for one owned pending order.

    Merchant credentials and signed protocol fields stay server-side. The
    PostgreSQL connection is released before the external request so a slow
    payment provider cannot consume the shared database pool.
    """
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        business_conn = BusinessConnection.postgres(conn)
        order = read_recharge_order(business_conn, merchant_order_no=order_no)
        if order is None or str(order["user_id"]) != ctx.user_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RECHARGE_ORDER_NOT_FOUND",
                    "message": "Recharge order does not exist.",
                },
            )
        if str(order["status"]) != "PENDING":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RECHARGE_ORDER_NOT_PENDING",
                    "message": "This recharge order is no longer pending.",
                },
            )
        provider_name = str(order["provider"])
        if provider_name not in {"zpay", "wechat_native"}:
            raise HTTPException(409, detail="此订单不支持在线支付。")
        expires_at: datetime | None = None
        if provider_name == "wechat_native":
            provider = get_payment_provider(provider_name)
            # Check the deadline before serving the cached QR: a lapsed order's
            # code_url still renders but WeChat will refuse the payment, which is
            # indistinguishable from a broken scanner to the customer.
            expires_at, remaining = _native_order_deadline(conn, order_no=order_no)
            if remaining <= timedelta(seconds=NATIVE_ORDER_MIN_REMAINING_SECONDS):
                raise HTTPException(409, detail="该充值订单已超过支付时限，请重新创建订单。")
            cached = business_conn.execute(
                "SELECT code_url FROM recharge_orders WHERE merchant_order_no=%s", (order_no,)
            ).fetchone()
            if cached and cached[0]:
                return _native_payment_code_response(
                    order_no, int(order["amount_fen"]), int(order["credits"]), str(cached[0])
                )
        try:
            merchant = provider.load_merchant_config(business_conn)
            deployment = provider.load_deployment_config()
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PAYMENT_CONFIGURATION_UNAVAILABLE",
                    "message": "支付服务暂不可用，请稍后重试。",
                },
            ) from exc
        amount_fen = int(order["amount_fen"])
        credits = int(order["credits"])

    try:
        payment_code = provider.create_payment_code(
            merchant=merchant,
            deployment=deployment,
            merchant_order_no=order_no,
            amount_fen=amount_fen,
            credits=credits,
            client_ip=client_ip_from_request(request),
            expires_at=expires_at,
        )
    except PaymentCodeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": "PAYMENT_CODE_UNAVAILABLE",
                "message": "支付二维码暂时无法生成，请稍后重试。",
            },
        ) from exc
    if provider_name == "wechat_native":
        code_url = payment_code.payment_url
        if not code_url.startswith("weixin://") or len(code_url) > 2048:
            raise HTTPException(502, detail="支付二维码内容无效，请稍后重试。")
        with fenced_pg_transaction(snapshot) as (conn, ctx):
            stored = conn.execute(
                "UPDATE recharge_orders SET code_url=COALESCE(code_url,%s) "
                "WHERE merchant_order_no=%s AND user_id=%s AND provider='wechat_native' "
                "AND status='PENDING' RETURNING code_url",
                (code_url, order_no, ctx.user_id),
            ).fetchone()
            if stored is None:
                raise HTTPException(409, detail="订单状态已更新，请刷新充值记录。")
            code_url = str(stored[0])
        return _native_payment_code_response(order_no, amount_fen, credits, code_url)
    return CustomerPaymentCodeResponse(
        order_no=order_no,
        amount_fen=amount_fen,
        credits=credits,
        qr_image_url=payment_code.qr_image_url,
        payment_url=payment_code.payment_url,
    )


def _native_order_deadline(
    conn: psycopg.Connection, *, order_no: str
) -> tuple[datetime, timedelta]:
    """The instant a Native order stops being payable, plus the life it has left.

    ``recharge_orders.created_at`` is a text column, so the arithmetic is done in
    SQL against the database clock: the QR, WeChat's ``time_expire`` and the expiry
    sweep then all read one timeline instead of three process-local ones.
    """
    row = conn.execute(
        "SELECT created_at::timestamptz + make_interval(secs => %s), now() "
        "FROM recharge_orders WHERE merchant_order_no = %s",
        (float(NATIVE_ORDER_VALIDITY_SECONDS), order_no),
    ).fetchone()
    # The caller has already read this order inside the same transaction.
    assert row is not None
    deadline, server_now = row[0], row[1]
    return deadline, deadline - server_now


def _native_payment_code_response(
    order_no: str, amount_fen: int, credits: int, code_url: str
) -> CustomerPaymentCodeResponse:
    buffer = io.BytesIO()
    qrcode.make(code_url).save(buffer, format="PNG")
    return CustomerPaymentCodeResponse(
        order_no=order_no,
        amount_fen=amount_fen,
        credits=credits,
        qr_image_url="data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
        payment_url=code_url,
    )


def _customer_profile(conn: psycopg.Connection, *, user_id: str) -> CustomerProfileResponse:
    row = conn.execute(
        """
        SELECT u.username, u.display_name, u.created_at,
               code.masked_code, code.status, code.activated_at,
               (
                   SELECT COUNT(*)
                   FROM customer_devices device
                   WHERE device.user_id = u.id AND device.status = 'BOUND'
               ) AS device_slots_used,
               u.max_devices,
               u.account_type, u.parent_user_id,
               (
                   SELECT parent.display_name
                   FROM users parent
                   WHERE parent.id = u.parent_user_id
               ) AS parent_display_name,
               (
                   SELECT quota.monthly_credits
                   FROM sub_account_quotas quota
                   WHERE quota.user_id = u.id
               ) AS monthly_quota_credits
        FROM users u
        LEFT JOIN LATERAL (
            SELECT masked_code, status, activated_at
            FROM activation_codes
            WHERE bound_user_id = u.id
            ORDER BY activated_at DESC NULLS LAST, id DESC
            LIMIT 1
        ) code ON TRUE
        WHERE u.id = %s
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CUSTOMER_PROFILE_NOT_FOUND",
                "message": "Customer profile does not exist.",
            },
        )
    return CustomerProfileResponse(
        user_id=user_id,
        username=str(row[0]),
        display_name=str(row[1]),
        joined_at=str(row[2]),
        activation_code_masked=str(row[3]) if row[3] is not None else None,
        activation_status=str(row[4]) if row[4] is not None else None,
        activated_at=str(row[5]) if row[5] is not None else None,
        device_slots_used=int(row[6]),
        device_slots_total=None,
        account_type=str(row[8]) if row[8] is not None else "MASTER",
        parent_user_id=str(row[9]) if row[9] is not None else None,
        parent_display_name=str(row[10]) if row[10] is not None else None,
        monthly_quota_credits=None if row[11] is None else int(str(row[11])),
        quota_used_credits=read_quota_used(conn, user_id) if row[9] is not None else None,
    )


def _insert_customer_audit(
    conn: psycopg.Connection,
    *,
    user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_logs "
        "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            str(uuid4()),
            user_id,
            action,
            entity_type,
            entity_id,
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
        ),
    )


def _require_customer_snapshot(request: Request) -> CustomerSessionSnapshot:
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    return snapshot


@router.get("/customer/profile", response_model=CustomerProfileResponse)
def read_customer_profile(request: Request) -> CustomerProfileResponse:
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        return _customer_profile(conn, user_id=ctx.user_id)


@router.patch("/customer/profile", response_model=CustomerProfileResponse)
def update_customer_profile(
    payload: UpdateCustomerProfileRequest,
    request: Request,
) -> CustomerProfileResponse:
    display_name = payload.display_name.strip()
    if not display_name or len(display_name) > 50:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_DISPLAY_NAME",
                "message": "Display name must contain 1 to 50 characters.",
            },
        )
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        updated = conn.execute(
            "UPDATE users SET display_name = %s WHERE id = %s AND role = 'customer'",
            (display_name, ctx.user_id),
        )
        if updated.rowcount != 1:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "CUSTOMER_PROFILE_NOT_FOUND",
                    "message": "Customer profile does not exist.",
                },
            )
        _insert_customer_audit(
            conn,
            user_id=ctx.user_id,
            action="customer.profile.updated",
            entity_type="user",
            entity_id=ctx.user_id,
            metadata={"display_name_length": len(display_name)},
        )
        return _customer_profile(conn, user_id=ctx.user_id)


@router.get("/customer/wallet", response_model=WalletResponse)
def read_customer_wallet(request: Request) -> WalletResponse:
    """Customer-lane wallet read on the API-Key whitelist (§2.2): balance +
    billing under the fenced session, or the independent lane for an
    ``xsk_live_`` key. Mirrors the internal /api/wallet read (BILL-01: credits
    live in the same customer wallet the activation grant funded)."""
    with customer_read_transaction(request) as (conn, user_id):
        # A sub-account session reads the organisation's wallet: the credits
        # belong to the master (T2.10), so the balance and billing settings
        # resolve to the wallet owner.
        wallet_owner_id = resolve_wallet_owner(BusinessConnection.postgres(conn), user_id)
        row = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
            (wallet_owner_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "WALLET_NOT_FOUND", "message": "Wallet does not exist."},
            )
        billing = SettingsRepository(
            BusinessConnection.postgres(conn)
        ).read_customer_billing_settings(user_id=wallet_owner_id)
        try:
            billing = effective_customer_billing_settings(billing, user_id=wallet_owner_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ACCEPTANCE_PAYMENT_CONFIGURATION_INVALID",
                    "message": "The controlled payment rehearsal is not configured safely.",
                },
            ) from exc
        price_version, credit_config = read_pricing(BusinessConnection.postgres(conn))
        return WalletResponse(
            available_credits=int(row[0]),
            reserved_credits=int(row[1]),
            # Keep the legacy response field for desktop compatibility; on
            # the customer lane it represents the effective sale price.
            internal_unit_price_fen=billing["charged_unit_price_fen"],
            min_recharge_fen=billing["min_recharge_fen"],
            recharge_step_fen=billing["recharge_step_fen"],
            points_per_yuan=credit_config.points_per_yuan if credit_config else None,
            credit_price_version=price_version,
        )


@router.get("/customer/wallet/transactions", response_model=WalletTransactionPage)
def list_customer_wallet_transactions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    token_group_id: str | None = Query(default=None, max_length=128),
    auth_source: Literal["session", "api_key", "internal", "historical"] | None = None,
    transaction_type: Literal["CHARGE", "RESERVE", "SETTLE", "RELEASE", "CONVERSION"] | None = None,
    business: Literal[
        "video",
        "oral",
        "recharge",
        "character",
        "first_frame",
        "analysis",
        "rewrite",
        "asr",
        "link_resolution",
        "prompt_optimize",
        "avatar_clone",
        "voice_clone",
        "viral_data",
    ]
    | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> WalletTransactionPage:
    if any(value is not None and value.tzinfo is None for value in (started_at, ended_at)):
        raise HTTPException(422, detail="筛选时间必须包含时区。")
    if started_at and ended_at and ended_at <= started_at:
        raise HTTPException(422, detail="结束时间必须晚于开始时间。")
    with customer_read_transaction(request) as (conn, user_id):
        # Same wallet-owner resolution as the balance read: every ledger row of
        # the organisation sits on the master's wallet (T2.10), including a
        # sub-account's consumption (attributed through actor_user_id).
        wallet_owner_id = resolve_wallet_owner(BusinessConnection.postgres(conn), user_id)
        clauses = ["wt.user_id = %s"]
        params: list[object] = [wallet_owner_id]
        if token_group_id:
            clauses.append("k.token_group_id = %s")
            params.append(token_group_id)
        if auth_source == "historical":
            clauses.append("wt.auth_source IS NULL")
        elif auth_source:
            clauses.append("wt.auth_source = %s")
            params.append(auth_source)
        if transaction_type:
            clauses.append("wt.type = %s")
            params.append(transaction_type)
        if business:
            clauses.append(
                {
                    "video": "(wt.task_id IS NOT NULL OR op.service IN ('video_768p','video_2k'))",
                    "oral": "(wt.oral_task_id IS NOT NULL OR op.service = 'oral')",
                    "recharge": "wt.type = 'CHARGE'",
                }.get(business, "op.service = %s")
            )
            if business not in {"video", "oral", "recharge"}:
                params.append(business)
        if started_at:
            clauses.append("wt.created_at::timestamptz >= %s")
            params.append(started_at)
        if ended_at:
            clauses.append("wt.created_at::timestamptz < %s")
            params.append(ended_at)
        from_sql = (
            " FROM wallet_transactions wt LEFT JOIN customer_api_keys k ON k.id = "
            "wt.api_key_id AND k.user_id = wt.user_id "
            "LEFT JOIN billing_operations op ON op.id=wt.billing_operation_id "
            "AND (op.user_id=wt.user_id OR op.user_id=wt.actor_user_id) "
            "LEFT JOIN recharge_orders credit_order ON credit_order.id = wt.recharge_order_id "
            "AND credit_order.user_id = wt.user_id "
            "LEFT JOIN admin_adjustments credit_adjustment ON "
            "credit_adjustment.recharge_order_id = credit_order.id "
            "AND credit_adjustment.target_user_id = wt.user_id WHERE " + " AND ".join(clauses)
        )
        total_row = conn.execute(
            "SELECT COUNT(*)" + from_sql,
            params,
        ).fetchone()
        assert total_row is not None
        total = int(total_row[0])
        rows = conn.execute(
            """
            SELECT wt.id, wt.user_id, wt.type, wt.available_delta, wt.reserved_delta,
                   wt.recharge_order_id, wt.task_id, wt.oral_task_id,
                   wt.billing_round, wt.created_at,
                   wt.api_key_id, k.token_group_id, k.label, k.credential_version, wt.auth_source,
                   wt.pricing_snapshot_json,
                   (SELECT task.batch_id FROM generation_tasks task WHERE task.id = wt.task_id),
                   CASE WHEN wt.type = 'CHARGE' THEN
                     COALESCE(credit_adjustment.source_document_type, credit_order.provider)
                   END, wt.billing_operation_id,
                   (SELECT o.service FROM billing_operations o WHERE o.id=wt.billing_operation_id),
                   wt.actor_user_id,
                   (SELECT u.display_name FROM users u WHERE u.id=wt.actor_user_id),
                   -- P1-7：配对态按全量账本算，组内每行同值。分页把 RESERVE 与
                   -- 结算/退回切开、或按类型筛选后只剩一行时，界面仍知道它是
                   -- 「已结算」还是「退回」；(billing_operation_id,type) 索引支撑
                   -- 这两个 EXISTS。
                   CASE
                     WHEN wt.billing_operation_id IS NULL THEN NULL
                     WHEN EXISTS (
                       SELECT 1 FROM wallet_transactions settled
                       WHERE settled.billing_operation_id = wt.billing_operation_id
                         AND settled.type = 'SETTLE'
                     ) THEN 'SETTLED'
                     WHEN EXISTS (
                       SELECT 1 FROM wallet_transactions released
                       WHERE released.billing_operation_id = wt.billing_operation_id
                         AND released.type = 'RELEASE'
                     ) THEN 'RELEASED'
                     ELSE 'PENDING'
                   END
            """
            + from_sql
            + """
            ORDER BY (wt.ledger_sequence IS NULL), wt.ledger_sequence DESC,
                     wt.created_at DESC, wt.id DESC
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ).fetchall()
        return WalletTransactionPage(
            items=[_customer_ledger_entry(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )


def _customer_ledger_entry(row: Sequence[Any]) -> WalletTransactionResponse:
    """One customer ledger row, priced by the retail side of its frozen snapshot.

    P0-3: the customer must be able to check a delta against the unit price,
    usage, discount and rounding it was priced with. The projection is a
    whitelist — the snapshot's cost side never crosses this boundary.

    P1-7: ``pair_state`` names the row's billing-cycle group state so the
    client can fold RESERVE→SETTLE/RELEASE into one entry.
    """
    pricing = pricing_breakdown(json.loads(row[15]) if row[15] else None)
    return WalletTransactionResponse(
        id=str(row[0]),
        user_id=str(row[1]),
        type=row[2],
        available_delta=int(row[3]),
        reserved_delta=int(row[4]),
        recharge_order_id=str(row[5]) if row[5] is not None else None,
        task_id=str(row[6]) if row[6] is not None else None,
        oral_task_id=str(row[7]) if row[7] is not None else None,
        billing_round=int(row[8]) if row[8] is not None else None,
        created_at=str(row[9]),
        api_key_id=row[10],
        token_group_id=row[11],
        token_label=row[12],
        credential_version=row[13],
        auth_source=row[14],
        credit_price_version=pricing.version if pricing else None,
        generation_batch_id=row[16],
        credit_source=row[17],
        billing_operation_id=row[18],
        service=row[19],
        service_name=SERVICES[row[19]].name if row[19] in SERVICES else None,
        actor_user_id=str(row[20]) if row[20] is not None else None,
        actor_name=row[21],
        pricing=pricing,
        pair_state=row[22],
    )


@router.get("/customer/recharge-orders", response_model=RechargeOrderPage)
def list_customer_recharge_orders(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RechargeOrderPage:
    """Customer-lane order list on the API-Key whitelist (§2.2): only this
    principal's orders — a session is re-verified inside the fenced read
    transaction, an ``xsk_live_`` key rides the independent lane."""
    with customer_read_transaction(request) as (conn, user_id):
        # Codex P1 (PR #65): serialize_recharge_order reads named columns, so
        # the rows must come from a connection with the named-row factory
        # installed. BusinessConnection.postgres() sets it; a raw pooled
        # psycopg connection returns plain tuples and would 500 on a fresh
        # connection that no earlier request had already mutated.
        business_conn = BusinessConnection.postgres(conn)
        total_row = business_conn.execute(
            "SELECT COUNT(*) FROM recharge_orders WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        assert total_row is not None
        total = int(total_row[0])
        rows = business_conn.execute(
            """
            SELECT id, user_id, merchant_order_no, provider, provider_trade_no,
                   channel, status, amount_fen, credits, notify_digest, created_at, paid_at
            FROM recharge_orders
            WHERE user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (user_id, limit, offset),
        ).fetchall()
        return RechargeOrderPage(
            items=[
                RechargeOrderStatusResponse(**serialize_recharge_order(cast(sqlite3.Row, row)))
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/recharge-orders", response_model=RechargeOrderPage)
def list_recharge_orders(
    conn: Database,
    actor: AuthenticatedUser,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RechargeOrderPage:
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM recharge_orders WHERE user_id = %s",
            (actor.id,),
        ).fetchone()[0]
    )
    rows = conn.execute(
        """
        SELECT
            id, user_id, merchant_order_no, provider, provider_trade_no, channel, status,
            amount_fen, credits, notify_digest, created_at, paid_at
        FROM recharge_orders
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
        """,
        (actor.id, limit, offset),
    ).fetchall()
    return RechargeOrderPage(
        items=[RechargeOrderStatusResponse(**serialize_recharge_order(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/recharge-orders/{order_no}", response_model=RechargeOrderStatusResponse)
def read_recharge_order_status(
    order_no: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> RechargeOrderStatusResponse:
    order = read_recharge_order(conn, merchant_order_no=order_no)
    if order is None or str(order["user_id"]) != actor.id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RECHARGE_ORDER_NOT_FOUND",
                "message": "Recharge order does not exist.",
            },
        )
    return RechargeOrderStatusResponse(**serialize_recharge_order(order))


def validate_recharge_amount(amount_fen: int, billing: dict[str, int]) -> None:
    if (
        amount_fen < billing["min_recharge_fen"]
        or amount_fen % billing["recharge_step_fen"] != 0
        or (
            "points_per_yuan" not in billing and amount_fen % billing["charged_unit_price_fen"] != 0
        )
        or amount_fen > INT4_MAX_FEN
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_RECHARGE_AMOUNT",
                "message": "Recharge amount must meet the configured minimum and step.",
            },
        )


class CustomerCenterSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    available_credits: int
    reserved_credits: int
    total_consumed_credits: int
    active_tokens: int


@router.get("/customer/center-summary", response_model=CustomerCenterSummaryResponse)
def read_customer_center_summary(
    request: Request, response: Response
) -> CustomerCenterSummaryResponse:
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(401, detail={"code": "SESSION_REQUIRED", "message": "请先登录账号。"})
    response.headers["Cache-Control"] = "no-store"
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        # A sub-account session reads the organisation's summary: balance,
        # reservation, consumed total and tokens all belong to the master's
        # wallet (T2.10). The response keeps naming the caller.
        wallet_owner_id = resolve_wallet_owner(BusinessConnection.postgres(conn), ctx.user_id)
        row = conn.execute(
            "SELECT available_credits, reserved_credits, "
            "(SELECT COALESCE(SUM(-reserved_delta), 0) FROM wallet_transactions "
            "WHERE user_id = %s AND type = 'SETTLE'), "
            "(SELECT COUNT(*) FROM customer_api_keys WHERE user_id = %s AND revoked_at IS NULL) "
            "FROM wallets WHERE user_id = %s",
            (wallet_owner_id, wallet_owner_id, wallet_owner_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                404, detail={"code": "WALLET_NOT_FOUND", "message": "账号钱包不存在。"}
            )
        return CustomerCenterSummaryResponse(
            user_id=ctx.user_id,
            available_credits=int(row[0]),
            reserved_credits=int(row[1]),
            total_consumed_credits=int(row[2]),
            active_tokens=int(row[3]),
        )
