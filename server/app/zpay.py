from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
ZPAY_GATEWAY_URL = "https://zpayz.cn/submit.php"
ZPAY_API_PAYMENT_URL = "https://zpayz.cn/mapi.php"
ZPAY_QUERY_URL = "https://zpayz.cn/api.php"
ZPAY_NOTIFY_PATH = "/api/payments/zpay/notify"
ZPAY_RETURN_PATH = "/api/payments/zpay/return"
ALLOWED_ZPAY_CHANNELS = frozenset({"alipay", "wxpay"})
ZPAY_QUERY_TIMEOUT_SECONDS = 3.0
MAX_ZPAY_QUERY_RESPONSE_BYTES = 64 * 1024
MAX_ZPAY_PAYMENT_RESPONSE_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZPayMerchantConfig:
    pid: str
    key: str
    channel: str
    enabled_channels: tuple[str, ...] = ()

    @property
    def allowed_channels(self) -> tuple[str, ...]:
        return self.enabled_channels or (self.channel,)


@dataclass(frozen=True)
class ZPayDeploymentConfig:
    gateway_url: str
    query_url: str
    notify_url: str
    return_url: str


@dataclass(frozen=True)
class ZPayOrderQueryResult:
    paid: bool
    merchant_order_no: str
    provider_trade_no: str | None
    amount_fen: int | None
    channel: str | None
    response_digest: str


@dataclass(frozen=True)
class ZPayPaymentCodeResult:
    qr_image_url: str
    payment_url: str
    provider_order_no: str | None


class ZPayPaymentCodeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class ZPayOrderQueryError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class ZPayHTTPResponse(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, *_: object) -> None: ...

    def read(self, amount: int = -1) -> bytes: ...


class ZPayHTTPOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> ZPayHTTPResponse: ...


def zpay_signing_string(params: Mapping[str, object]) -> str:
    pairs = (
        (name, str(value))
        for name, value in params.items()
        if name not in {"sign", "sign_type"} and value is not None and str(value) != ""
    )
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def sign_zpay_params(params: Mapping[str, object], key: str) -> str:
    payload = f"{zpay_signing_string(params)}{key}".encode()
    # ZPay requires MD5 for protocol compatibility; it is not used for password storage.
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


def generate_merchant_order_no() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    random_suffix = f"{secrets.randbelow(10**12):012d}"
    return f"{timestamp}{random_suffix}"


def merchant_config_from_settings(settings: Mapping[str, str]) -> ZPayMerchantConfig:
    pid = settings.get("pid", "").strip()
    key = settings.get("key", "").strip()
    channels = parse_enabled_channels(settings.get("enabled_channels", ""))
    if not pid or not key or not channels:
        raise ValueError("ZPay merchant settings are incomplete")
    return ZPayMerchantConfig(
        pid=pid,
        key=key,
        channel=channels[0],
        enabled_channels=channels,
    )


def parse_enabled_channels(value: str) -> tuple[str, ...]:
    channels = tuple(channel.strip().lower() for channel in value.split(",") if channel.strip())
    if not channels:
        return ()
    if len(set(channels)) != len(channels) or any(
        channel not in ALLOWED_ZPAY_CHANNELS for channel in channels
    ):
        raise ValueError("ZPay enabled_channels must contain unique alipay or wxpay values")
    return channels


def deployment_config_from_environment() -> ZPayDeploymentConfig:
    public_base_url = os.environ.get(PUBLIC_BASE_URL_ENV, "").strip()
    parsed = urlsplit(public_base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin without path, query, or fragment")

    origin = f"https://{parsed.netloc}"
    return ZPayDeploymentConfig(
        gateway_url=ZPAY_GATEWAY_URL,
        query_url=ZPAY_QUERY_URL,
        notify_url=f"{origin}{ZPAY_NOTIFY_PATH}",
        return_url=f"{origin}{ZPAY_RETURN_PATH}",
    )


def format_yuan(amount_fen: int) -> str:
    yuan, fen = divmod(amount_fen, 100)
    return f"{yuan}.{fen:02d}"


def build_zpay_payment_form(
    *,
    merchant_order_no: str,
    amount_fen: int,
    credits: int,
    merchant: ZPayMerchantConfig,
    deployment: ZPayDeploymentConfig,
) -> dict[str, str]:
    fields = {
        "pid": merchant.pid,
        "type": merchant.channel,
        "out_trade_no": merchant_order_no,
        "notify_url": deployment.notify_url,
        "return_url": deployment.return_url,
        "name": f"内部视频生成条数充值 {credits} 条",
        "money": format_yuan(amount_fen),
    }
    fields["sign"] = sign_zpay_params(fields, merchant.key)
    fields["sign_type"] = "MD5"
    return fields


def parse_zpay_money_to_fen(value: str) -> int:
    text = value.strip()
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", text) is None:
        raise ValueError("ZPay money must be a positive decimal with at most two decimals")
    yuan, separator, decimals = text.partition(".")
    amount_fen = int(yuan) * 100 + int(decimals.ljust(2, "0") if separator else "0")
    if amount_fen <= 0:
        raise ValueError("ZPay money must be positive")
    return amount_fen


def build_zpay_order_query_url(
    query_url: str,
    *,
    pid: str,
    key: str,
    out_trade_no: str,
) -> str:
    query = urlencode({"act": "order", "pid": pid, "key": key, "out_trade_no": out_trade_no})
    return f"{query_url}?{query}"


class ZPayOrderQueryClient:
    def __init__(
        self,
        *,
        opener: ZPayHTTPOpener | None = None,
        timeout_seconds: float = ZPAY_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        self._opener = opener or cast(ZPayHTTPOpener, urlopen)
        self._timeout_seconds = timeout_seconds

    def query_order(
        self,
        *,
        merchant: ZPayMerchantConfig,
        deployment: ZPayDeploymentConfig,
        merchant_order_no: str,
    ) -> ZPayOrderQueryResult:
        request_url = build_zpay_order_query_url(
            deployment.query_url,
            pid=merchant.pid,
            key=merchant.key,
            out_trade_no=merchant_order_no,
        )
        request = Request(request_url, method="GET")
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                body = response.read(MAX_ZPAY_QUERY_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            logger.warning("ZPay order query returned HTTP %s", exc.code)
            raise ZPayOrderQueryError("ZPay order query failed") from exc
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("ZPay order query failed: %s", type(exc).__name__)
            raise ZPayOrderQueryError("ZPay order query timed out", status_code=504) from exc

        if len(body) > MAX_ZPAY_QUERY_RESPONSE_BYTES:
            raise ZPayOrderQueryError("ZPay order query response is too large")

        try:
            decoded: object = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ZPayOrderQueryError("ZPay order query response is invalid") from exc
        if not isinstance(decoded, dict):
            raise ZPayOrderQueryError("ZPay order query response is invalid")

        payload = {str(key): value for key, value in decoded.items()}
        if _query_field(payload, "code") != "1":
            raise ZPayOrderQueryError("ZPay rejected the order query")
        if _query_field(payload, "pid") != merchant.pid:
            raise ZPayOrderQueryError("ZPay order query merchant mismatch")
        if _query_field(payload, "out_trade_no") != merchant_order_no:
            raise ZPayOrderQueryError("ZPay order query number mismatch")

        response_digest = hashlib.sha256(body).hexdigest()
        payment_status = _query_field(payload, "status")
        if payment_status == "0":
            return ZPayOrderQueryResult(
                paid=False,
                merchant_order_no=merchant_order_no,
                provider_trade_no=None,
                amount_fen=None,
                channel=None,
                response_digest=response_digest,
            )
        if payment_status != "1":
            raise ZPayOrderQueryError("ZPay order query status is invalid")

        provider_trade_no = _query_field(payload, "trade_no")
        money = _query_field(payload, "money")
        channel = _query_field(payload, "type")
        if not provider_trade_no or channel not in ALLOWED_ZPAY_CHANNELS:
            raise ZPayOrderQueryError("ZPay paid order response is incomplete")
        try:
            amount_fen = parse_zpay_money_to_fen(money)
        except ValueError as exc:
            raise ZPayOrderQueryError("ZPay paid order amount is invalid") from exc

        return ZPayOrderQueryResult(
            paid=True,
            merchant_order_no=merchant_order_no,
            provider_trade_no=provider_trade_no,
            amount_fen=amount_fen,
            channel=channel,
            response_digest=response_digest,
        )


class ZPayPaymentCodeClient:
    """Create one desktop payment code without exposing merchant secrets.

    The returned image URL is short-lived provider output intended only for an
    ``img`` element. The customer API never returns the signed merchant fields
    or provider name to the desktop.
    """

    def __init__(
        self,
        *,
        opener: ZPayHTTPOpener | None = None,
        timeout_seconds: float = ZPAY_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        self._opener = opener or cast(ZPayHTTPOpener, urlopen)
        self._timeout_seconds = timeout_seconds

    def create_payment_code(
        self,
        *,
        merchant: ZPayMerchantConfig,
        deployment: ZPayDeploymentConfig,
        merchant_order_no: str,
        amount_fen: int,
        credits: int,
        client_ip: str,
    ) -> ZPayPaymentCodeResult:
        fields = {
            "pid": merchant.pid,
            "type": merchant.channel,
            "out_trade_no": merchant_order_no,
            "notify_url": deployment.notify_url,
            "name": f"视频生成条数充值 {credits} 条",
            "money": format_yuan(amount_fen),
            "clientip": client_ip,
            "device": "pc",
        }
        fields["sign"] = sign_zpay_params(fields, merchant.key)
        fields["sign_type"] = "MD5"
        request = Request(
            ZPAY_API_PAYMENT_URL,
            data=urlencode(fields).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                body = response.read(MAX_ZPAY_PAYMENT_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            logger.warning("Payment-code request returned HTTP %s", exc.code)
            raise ZPayPaymentCodeError("payment-code request failed") from exc
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("Payment-code request failed: %s", type(exc).__name__)
            raise ZPayPaymentCodeError(
                "payment-code request timed out",
                status_code=504,
            ) from exc

        if len(body) > MAX_ZPAY_PAYMENT_RESPONSE_BYTES:
            raise ZPayPaymentCodeError("payment-code response is too large")
        try:
            decoded: object = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ZPayPaymentCodeError("payment-code response is invalid") from exc
        if not isinstance(decoded, dict) or str(decoded.get("code", "")) != "1":
            raise ZPayPaymentCodeError("payment-code request was rejected")

        qr_image_url = _https_url_field(decoded, "img")
        payment_url = _https_url_field(decoded, "payurl")
        provider_order_no = str(decoded.get("O_id", "")).strip() or None
        return ZPayPaymentCodeResult(
            qr_image_url=qr_image_url,
            payment_url=payment_url,
            provider_order_no=provider_order_no,
        )


def _https_url_field(payload: Mapping[str, object], name: str) -> str:
    value = str(payload.get(name, "")).strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ZPayPaymentCodeError("payment-code response URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ZPayPaymentCodeError("payment-code response URL is invalid")
    return value


def _query_field(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    return "" if value is None else str(value)
