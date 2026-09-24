from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

# Import to trigger provider registration
import app.zpay_provider  # noqa: F401
from app.admin_write_contract import AdminWriteContract
from app.admin_write_contract import require_write_contract as _require_write_contract
from app.auth import Database
from app.control_auth import ControlUser
from app.db_portable import BusinessConnection
from app.ops_metrics import get_or_create_request_id
from app.payment_provider import (
    MerchantConfig,
    OrderQueryError,
    PaymentProvider,
    get_payment_provider,
)
from app.permissions import write_audit
from app.recharge_routes import RechargeOrderStatusResponse
from app.wechat_native_provider import (
    WECHAT_CALLBACK_CERT_UNAVAILABLE,
    WeChatNativeProvider,
)
from app.zpay_payments import (
    WECHAT_NATIVE_SETTLEMENT_SPEC,
    PaymentConfirmationError,
    confirm_recharge_payment,
    read_recharge_order,
    serialize_recharge_order,
)

router = APIRouter(prefix="/api", tags=["payments"])
logger = logging.getLogger(__name__)


def get_zpay_provider() -> PaymentProvider:
    """Dependency: get the ZPay payment provider."""
    return get_payment_provider("zpay")


ZPayProviderDep = Annotated[PaymentProvider, Depends(get_zpay_provider)]


def get_wechat_provider() -> WeChatNativeProvider:
    """Dependency: the WeChat Native provider, typed for raw-body callback verify."""
    provider = get_payment_provider("wechat_native")
    assert isinstance(provider, WeChatNativeProvider)
    return provider


WeChatProviderDep = Annotated[WeChatNativeProvider, Depends(get_wechat_provider)]


@router.get("/payments/zpay/notify", response_class=PlainTextResponse)
def zpay_notify(
    request: Request,
    conn: Database,
    provider: ZPayProviderDep,
) -> PlainTextResponse:
    params = _unique_query_params(request)
    merchant = _load_merchant_config(conn, provider)

    # Use provider abstraction for notification verification
    verification = provider.verify_notification(params, merchant)
    if not verification.valid:
        logger.warning("ZPay callback rejected: %s", verification.error_code)
        return PlainTextResponse("failure", status_code=400)

    assert verification.merchant_order_no is not None
    assert verification.provider_trade_no is not None
    assert verification.amount_fen is not None
    assert verification.channel is not None
    assert verification.source_digest is not None

    try:
        confirm_recharge_payment(
            conn,
            merchant_order_no=verification.merchant_order_no,
            provider_trade_no=verification.provider_trade_no,
            amount_fen=verification.amount_fen,
            channel=verification.channel,
            source_digest=verification.source_digest,
            allowed_channels=merchant.allowed_channels,
        )
    except PaymentConfirmationError as exc:
        logger.warning("ZPay callback rejected: %s", exc.code)
        return PlainTextResponse("failure", status_code=exc.status_code)
    except (sqlite3.OperationalError, psycopg.errors.OperationalError) as exc:
        logger.warning("ZPay callback deferred because the payment database is busy: %s", exc)
        return PlainTextResponse("retry", status_code=503)
    return PlainTextResponse("success")


@router.get("/payments/zpay/return", response_class=HTMLResponse)
def zpay_return() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>支付确认中</title></head><body><main><h1>正在确认支付</h1>"
        "<p>请返回内部系统查看充值状态。</p></main></body></html>"
    )


@router.post("/payments/wechat_native/notify")
async def wechat_native_notify(
    request: Request,
    conn: Database,
    provider: WeChatProviderDep,
) -> JSONResponse:
    """WeChat Pay V3 Native transaction callback.

    Verifies the exact raw body against the Wechatpay-* headers (platform-certificate
    signature + AES-256-GCM resource decryption) then idempotently settles the recharge
    order through the shared confirm routine with the wechat_native spec (which writes
    the transaction_id column, not provider_trade_no). Answers the WeChat JSON
    acknowledgement shape rather than zpay's plain text: ``{"code":"SUCCESS"}`` stops
    retries, ``{"code":"FAIL"}`` (with a 5xx status for transient faults) triggers them.
    """
    raw_body = await request.body()
    try:
        merchant = provider.load_merchant_config(conn)
    except ValueError as exc:
        logger.warning("WeChat callback rejected: merchant config invalid: %s", exc)
        return _wechat_fail("WECHAT_CONFIGURATION_INVALID", status_code=503)

    result = provider.verify_notification_raw(
        raw_body=raw_body,
        timestamp=request.headers.get("Wechatpay-Timestamp"),
        nonce=request.headers.get("Wechatpay-Nonce"),
        signature=request.headers.get("Wechatpay-Signature"),
        serial=request.headers.get("Wechatpay-Serial"),
        merchant=merchant,
    )
    if not result.authenticated:
        # A missing/unfetchable platform certificate is transient -> 503 so WeChat
        # retries; a bad signature/body is final -> 400.
        status_code = 503 if result.error_code == WECHAT_CALLBACK_CERT_UNAVAILABLE else 400
        logger.warning("WeChat callback rejected: %s", result.error_code)
        return _wechat_fail(
            result.error_code or "WECHAT_CALLBACK_REJECTED", status_code=status_code
        )
    if result.error_code is not None:
        # Authentic but not settleable (e.g. SUCCESS missing its transaction_id).
        logger.warning("WeChat callback payload invalid: %s", result.error_code)
        return _wechat_fail(result.error_code, status_code=400)
    if result.trade_state != "SUCCESS":
        # Authentic non-final state (NOTPAY/USERPAYING/...): ACK so WeChat stops this
        # push and re-notifies when the trade reaches a terminal state.
        return _wechat_success()

    # An authentic SUCCESS with no error_code carries every settlement field (the
    # provider's payload guard rejects an incomplete SUCCESS as PAYLOAD_INVALID).
    assert result.merchant_order_no is not None
    assert result.provider_trade_no is not None
    assert result.amount_fen is not None
    assert result.channel is not None
    assert result.source_digest is not None
    try:
        # Synchronous PG lock waits must not block dependency teardown on this
        # event loop: another callback may hold the order lock until commit.
        await run_in_threadpool(
            confirm_recharge_payment,
            conn,
            merchant_order_no=result.merchant_order_no,
            provider_trade_no=result.provider_trade_no,
            amount_fen=result.amount_fen,
            channel=result.channel,
            source_digest=result.source_digest,
            allowed_channels=merchant.allowed_channels,
            provider_spec=WECHAT_NATIVE_SETTLEMENT_SPEC,
        )
    except PaymentConfirmationError as exc:
        logger.warning("WeChat callback settlement rejected: %s", exc.code)
        return _wechat_fail(exc.code, status_code=exc.status_code)
    except (sqlite3.OperationalError, psycopg.errors.OperationalError) as exc:
        logger.warning("WeChat callback deferred because the payment database is busy: %s", exc)
        return _wechat_fail("PAYMENT_DATABASE_BUSY", status_code=503)
    return _wechat_success()


@router.post(
    "/control/recharge-orders/{order_no}/sync",
    response_model=RechargeOrderStatusResponse,
)
def sync_recharge_order_with_zpay(
    order_no: str,
    body: AdminWriteContract,
    request: Request,
    conn: Database,
    _actor: ControlUser,
    provider: ZPayProviderDep,
) -> RechargeOrderStatusResponse:
    """Manual single-order query with the admin write contract (A4, A2).

    The operator must send confirm + reason + an Idempotency-Key like every
    other control-plane write, and the attempt lands an ``audit_logs`` row:
    a manual sync can credit a wallet, so it must name who asked for it.
    The query itself stays naturally idempotent (PAID orders replay, the
    confirmed credit is unique-constrained), so no snapshot layer is needed.
    """
    _key, reason = _require_write_contract(request, body)
    request_id = get_or_create_request_id(request)
    write_audit(
        conn,
        actor=_actor,
        action="payment.sync",
        entity_type="recharge_order",
        entity_id=order_no,
        metadata={"reason": reason, "request_id": request_id},
    )
    local_order = read_recharge_order(conn, merchant_order_no=order_no)
    if local_order is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RECHARGE_ORDER_NOT_FOUND",
                "message": "Recharge order does not exist.",
            },
        )
    if str(local_order["status"]) == "PAID":
        return RechargeOrderStatusResponse(**serialize_recharge_order(local_order))
    if str(local_order["status"]) != "PENDING":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RECHARGE_ORDER_NOT_SYNCABLE",
                "message": "Recharge order is not waiting for payment.",
            },
        )

    merchant = _load_merchant_config(conn, provider)
    try:
        deployment = provider.load_deployment_config()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ZPAY_CONFIGURATION_INVALID", "message": str(exc)},
        ) from exc
    try:
        remote_order = provider.query_order(
            merchant=merchant,
            deployment=deployment,
            merchant_order_no=order_no,
        )
    except OrderQueryError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": "ZPAY_QUERY_FAILED",
                "message": "ZPay order status could not be confirmed.",
            },
        ) from exc

    if not remote_order.paid:
        return RechargeOrderStatusResponse(**serialize_recharge_order(local_order))
    if (
        remote_order.merchant_order_no != order_no
        or remote_order.provider_trade_no is None
        or remote_order.amount_fen is None
        or remote_order.channel is None
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ZPAY_QUERY_RESPONSE_INVALID",
                "message": "ZPay paid order response is incomplete.",
            },
        )

    try:
        confirmed = confirm_recharge_payment(
            conn,
            merchant_order_no=order_no,
            provider_trade_no=remote_order.provider_trade_no,
            amount_fen=remote_order.amount_fen,
            channel=remote_order.channel,
            source_digest=remote_order.response_digest,
            allowed_channels=merchant.allowed_channels,
        )
    except PaymentConfirmationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PAYMENT_DATABASE_BUSY",
                "message": "Payment settlement is temporarily busy.",
            },
        ) from exc
    return RechargeOrderStatusResponse(**serialize_recharge_order(confirmed))


def _load_merchant_config(
    conn: BusinessConnection,
    provider: PaymentProvider,
) -> MerchantConfig:
    """Load merchant configuration via the provider abstraction."""
    try:
        return provider.load_merchant_config(conn)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ZPAY_CONFIGURATION_INVALID", "message": str(exc)},
        ) from exc


def _unique_query_params(request: Request) -> dict[str, str]:
    params: dict[str, str] = {}
    for name, value in request.query_params.multi_items():
        if name in params:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "ZPAY_DUPLICATE_PARAMETER",
                    "message": "ZPay callback contains duplicate parameters.",
                },
            )
        params[name] = value
    return params


def _wechat_success() -> JSONResponse:
    """WeChat JSON acknowledgement that stops the notification retries."""
    return JSONResponse(content={"code": "SUCCESS", "message": "OK"})


def _wechat_fail(code: str, *, status_code: int) -> JSONResponse:
    """WeChat JSON failure; a 5xx status makes WeChat retry the notification."""
    return JSONResponse(content={"code": "FAIL", "message": code}, status_code=status_code)
