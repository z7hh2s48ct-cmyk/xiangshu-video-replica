"""WeChat Pay V3 Native payment client implementation.

Implements the encryption primitives, configuration parsing, platform-certificate
management, and HTTP client for WeChat Pay V3 Native code payment flows. Follows
the zpay.py patterns: opener dependency injection, strict URL validation, response
size limits, and layered exception classification.

Security considerations:
- Request signing uses SHA256withRSA over the 5-line METHOD/URL/TIMESTAMP/NONCE/BODY message.
- API responses are signature-verified against the platform certificate selected by the
  Wechatpay-Serial header before their bodies are trusted.
- The /v3/certificates response is trusted via its api_v3_key AES-256-GCM auth tag, which
  avoids needing a certificate in order to verify the certificate download itself.
- Merchant private keys are loaded from decrypted provider_settings and are never logged.

TODO(CW-069 cycle 3): add WeChatNativeProvider conforming to the PaymentProvider
Protocol plus registry wiring.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# -----------------------------------------------------------------------------
# Constants (API paths & schemas)
# -----------------------------------------------------------------------------

WECHAT_API_BASE = "https://api.mch.weixin.qq.com"
NATIVE_ORDER_PATH = "/v3/pay/transactions/native"
CERTIFICATES_PATH = "/v3/certificates"
ORDER_QUERY_PATH_TEMPLATE = "/v3/pay/transactions/out-trade-no/{out_trade_no}"
WECHAT_NOTIFY_PATH = "/api/payments/wechat_native/notify"
AUTH_SCHEMA = "WECHATPAY2-SHA256-RSA2048"
PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
AES_GCM_KEY_LENGTH = 32  # 256 bits
WECHAT_TIMEOUT_SECONDS = 10.0
MAX_WECHAT_RESPONSE_BYTES = 256 * 1024
MAX_WECHAT_ERROR_BODY_BYTES = 4 * 1024
CERTIFICATE_CACHE_TTL_SECONDS = 12 * 60 * 60
WECHAT_USER_AGENT = "customer-v3-wechat-native/1.0"

# How long a Native order stays payable. WeChat's own default is two hours; we
# state it explicitly so the local order, the QR and the expiry sweep all agree
# on one number instead of each assuming the gateway default.
NATIVE_ORDER_VALIDITY_SECONDS = 2 * 60 * 60
# WeChat rejects a time_expire that is not comfortably in the future, so an
# order with less than this much life left can no longer be given a QR.
NATIVE_ORDER_MIN_REMAINING_SECONDS = 120
# How long an order may sit PENDING before the reconciliation sweep stops
# trusting the callback path alone and asks WeChat directly
# (scripts/reconcile_pending_native_orders). A callback normally lands within
# seconds; the grace only has to outlast a transient outage, and stays far
# short of the payment window so a lost callback is recovered while the order
# can still be settled in the same maintenance pass that would close it.
NATIVE_RECONCILIATION_GRACE_SECONDS = 10 * 60
# WeChat retries a payment notification on a fixed backoff schedule that tops
# out at roughly 24 hours (15s...6h). Inside the payment window plus this drain
# horizon an order can still become PAID — by a payment, a retrying callback,
# or the reconciliation sweep — so it blocks switching the merchant identity
# and legacy credit conversion. Past the horizon nothing can land anymore:
# payment is impossible (the code_url retired with the window), callbacks are
# exhausted, and the sweep has asked WeChat about whatever was real.
NATIVE_SETTLEMENT_DRAIN_SECONDS = 24 * 60 * 60
# How long a Native order blocks merchant-identity changes and legacy credit
# conversion: the payment window plus the callback drain horizon.
NATIVE_MERCHANT_SWITCH_BLOCK_SECONDS = (
    NATIVE_ORDER_VALIDITY_SECONDS + NATIVE_SETTLEMENT_DRAIN_SECONDS
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Core data classes (merchant/deployment configs)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class WeChatMerchantConfig:
    """Decrypted merchant configuration from provider_settings."""

    appid: str
    mchid: str
    serial_no: str
    api_v3_key: str
    private_key_pem: str


@dataclass(frozen=True)
class WeChatDeploymentConfig:
    """Deployment URLs derived from environment variables."""

    notify_url: str


# -----------------------------------------------------------------------------
# Exception hierarchy (classification per zpay pattern)
# -----------------------------------------------------------------------------


class WeChatNativeError(RuntimeError):
    """Base error for WeChat Native operations."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class WeChatSignatureError(WeChatNativeError):
    """Invalid signature or cryptographic verification failure."""

    pass


# -----------------------------------------------------------------------------
# Cryptographic primitives (pure functions - testable without network/DB)
# -----------------------------------------------------------------------------


def build_request_signature_message(
    *, method: str, url_path: str, timestamp: str, nonce: str, body: str
) -> str:
    """Build request signature message: METHOD\\nURL\\nTIMESTAMP\\nNONCE\\nBODY\\n."""
    return f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"


def build_response_verify_message(*, timestamp: str, nonce: str, body: str) -> str:
    """Build response verification message (server→client): TIMESTAMP\\nNONCE\\nBODY\\n."""
    return f"{timestamp}\n{nonce}\n{body}\n"


def load_private_key(private_key_pem: str) -> RSAPrivateKey:
    """Load unencrypted PEM private key as RSAPrivateKey (runtime type check for mypy strict)."""
    key_bytes = private_key_pem.encode("utf-8")
    key = serialization.load_pem_private_key(key_bytes, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise WeChatNativeError("WeChat merchant private key must be RSA")
    return key


def sign_sha256_rsa(*, private_key_pem: str, message: str) -> str:
    """Sign message with RSA SHA256, return Base64-encoded signature."""
    private_key = load_private_key(private_key_pem)
    msg_bytes = message.encode("utf-8")
    sig_bytes = private_key.sign(msg_bytes, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig_bytes).decode("ascii")


def verify_sha256_rsa(*, public_key: RSAPublicKey, signature_b64: str, message: str) -> bool:
    """Verify RSA SHA256 signature, return True on success or False on any failure."""
    try:
        sig_bytes = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        return False
    try:
        msg_bytes = message.encode("utf-8")
        public_key.verify(sig_bytes, msg_bytes, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


def build_authorization_header(
    *,
    mchid: str,
    serial_no: str,
    private_key_pem: str,
    method: str,
    url_path: str,
    body: str,
    timestamp: str,
    nonce: str,
) -> str:
    """Build full Authorization header: WECHATPAY2-SHA256-RSA2048 mchid=...,signature=..."""
    message = build_request_signature_message(
        method=method, url_path=url_path, timestamp=timestamp, nonce=nonce, body=body
    )
    signature = sign_sha256_rsa(private_key_pem=private_key_pem, message=message)
    return (
        f'{AUTH_SCHEMA} mchid="{mchid}",nonce_str="{nonce}",'
        f'timestamp="{timestamp}",serial_no="{serial_no}",signature="{signature}"'
    )


def decrypt_aes_256_gcm(
    *, api_v3_key: str, nonce: str, associated_data: str, ciphertext_b64: str
) -> bytes:
    """Decrypt ciphertext using AES-256-GCM, return plaintext bytes."""
    key = api_v3_key.encode("utf-8")
    if len(key) != AES_GCM_KEY_LENGTH:
        raise WeChatNativeError("WeChat api_v3_key must be exactly 32 bytes")
    try:
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
    except ValueError as exc:
        raise WeChatNativeError("Ciphertext is not valid Base64") from exc
    aesgcm = AESGCM(key)
    try:
        aad_bytes = associated_data.encode("utf-8")
        return aesgcm.decrypt(nonce.encode("utf-8"), ciphertext, aad_bytes)
    except InvalidTag as exc:
        raise WeChatSignatureError("AES-256-GCM authentication tag mismatch") from exc


# -----------------------------------------------------------------------------
# Configuration parsing (load from SettingsRepository.provider_settings)
# -----------------------------------------------------------------------------


def merchant_config_from_settings(settings: Mapping[str, str]) -> WeChatMerchantConfig:
    """Parse WeChat Native settings from provider_settings row."""
    appid = settings.get("appid", "").strip()
    mchid = settings.get("mchid", "").strip()
    serial_no = settings.get("serial_no", "").strip()
    api_v3_key = settings.get("api_v3_key", "").strip()
    private_key = settings.get("private_key", "").strip()
    if not all([appid, mchid, serial_no, api_v3_key, private_key]):
        raise ValueError("WeChat Native merchant settings are incomplete")
    if len(api_v3_key.encode("utf-8")) != AES_GCM_KEY_LENGTH:
        raise ValueError("WeChat api_v3_key must be exactly 32 bytes")
    return WeChatMerchantConfig(
        appid=appid,
        mchid=mchid,
        serial_no=serial_no,
        api_v3_key=api_v3_key,
        private_key_pem=private_key,
    )


def deployment_config_from_environment() -> WeChatDeploymentConfig:
    """Derive WeChat Native deployment URLs from PUBLIC_BASE_URL (HTTPS origin only)."""
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
    return WeChatDeploymentConfig(notify_url=f"https://{parsed.netloc}{WECHAT_NOTIFY_PATH}")


# -----------------------------------------------------------------------------
# Cycle 2: HTTP transport protocols (opener dependency injection per zpay.py)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class WeChatNativeOrderResult:
    """Result of a successful Native order placement."""

    code_url: str
    response_digest: str


@dataclass(frozen=True)
class WeChatOrderQueryResult:
    """Result of an out-trade-no order query."""

    trade_state: str
    paid: bool
    out_trade_no: str
    transaction_id: str | None
    amount_fen: int | None
    response_digest: str


@dataclass(frozen=True)
class WeChatPlatformCertificate:
    """A decrypted WeChat platform certificate used to verify API responses."""

    serial_no: str
    public_key: RSAPublicKey
    not_after: datetime


class WeChatHTTPResponse(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, *_: object) -> None: ...

    def read(self, amount: int = -1) -> bytes: ...

    def getheader(self, name: str, default: str | None = None) -> str | None: ...


class WeChatHTTPOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> WeChatHTTPResponse: ...


@dataclass(frozen=True)
class _RawResponse:
    """Raw HTTP response body plus the Wechatpay-* verification headers."""

    body: bytes
    timestamp: str | None
    nonce: str | None
    signature: str | None
    serial: str | None


def wechat_error_detail(exc: HTTPError) -> str:
    """Summarize a WeChat error response as ``HTTP <status> code=... message=...``.

    WeChat names the actionable cause (``SIGN_ERROR``, ``NOAUTH``, ``PARAM_ERROR``,
    ``ORDERPAID``, ...) only in the error body. Without it every misconfiguration —
    wrong serial_no, wrong private key, merchant not enabled for Native — reads as
    the same opaque transport failure in the logs. The read is bounded and any
    failure to parse degrades to the bare status rather than masking the original
    error. The result is for server-side logs only; routes answer customers with
    their own fixed messages.
    """
    status = f"HTTP {exc.code}"
    if getattr(exc, "fp", None) is None:
        return status
    try:
        body = exc.read(MAX_WECHAT_ERROR_BODY_BYTES)
        payload = json.loads(body.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return status
    if not isinstance(payload, dict):
        return status
    parts = [status]
    code = payload.get("code")
    if isinstance(code, str) and code:
        parts.append(f"code={code}")
    message = payload.get("message")
    if isinstance(message, str) and message:
        parts.append(f"message={message[:200]}")
    return " ".join(parts)


def _execute_request(
    opener: WeChatHTTPOpener, request: Request, *, timeout_seconds: float
) -> _RawResponse:
    """Send a request, classify transport errors, and enforce the response size cap."""
    try:
        with opener(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_WECHAT_RESPONSE_BYTES + 1)
            timestamp = response.getheader("Wechatpay-Timestamp")
            nonce = response.getheader("Wechatpay-Nonce")
            signature = response.getheader("Wechatpay-Signature")
            serial = response.getheader("Wechatpay-Serial")
    except HTTPError as exc:
        detail = wechat_error_detail(exc)
        logger.warning("WeChat API rejected the request: %s", detail)
        raise WeChatNativeError(f"WeChat API request failed ({detail})") from exc
    except (TimeoutError, URLError, OSError) as exc:
        logger.warning("WeChat API request failed: %s", type(exc).__name__)
        raise WeChatNativeError("WeChat API request timed out", status_code=504) from exc
    if len(body) > MAX_WECHAT_RESPONSE_BYTES:
        raise WeChatNativeError("WeChat API response is too large")
    return _RawResponse(
        body=body, timestamp=timestamp, nonce=nonce, signature=signature, serial=serial
    )


def _parse_json_object(body: bytes) -> dict[str, object]:
    """Decode a JSON object body, raising WeChatNativeError on malformed input."""
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeChatNativeError("WeChat response is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise WeChatNativeError("WeChat response is not a JSON object")
    return {str(key): value for key, value in decoded.items()}


# -----------------------------------------------------------------------------
# Cycle 2: platform certificate download / decrypt / cache / TTL rotation
# -----------------------------------------------------------------------------


class PlatformCertificateManager:
    """Downloads, decrypts, caches, and TTL-rotates WeChat platform certificates.

    The /v3/certificates response is trusted via the api_v3_key AES-256-GCM
    authentication tag rather than signature verification, which avoids the
    chicken-and-egg problem of needing a certificate to verify the download of
    the certificates themselves.
    """

    def __init__(
        self,
        *,
        opener: WeChatHTTPOpener | None = None,
        timeout_seconds: float = WECHAT_TIMEOUT_SECONDS,
        cache_ttl_seconds: float = CERTIFICATE_CACHE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._opener = opener or cast(WeChatHTTPOpener, urlopen)
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock or time.monotonic
        self._cache: dict[str, WeChatPlatformCertificate] = {}
        self._fetched_at: float | None = None
        # One manager is shared by every request (see default_certificate_manager),
        # and callbacks settle on a thread pool, so refreshes must be serialized:
        # without this every concurrent caller on a cold cache would download the
        # certificates again, which is exactly the burst WeChat rate-limits.
        self._lock = threading.Lock()

    def get_certificate(
        self, merchant: WeChatMerchantConfig, serial_no: str
    ) -> WeChatPlatformCertificate:
        """Return the cached certificate for serial_no, refreshing when stale or missing."""
        certificate = self._cached(serial_no)
        if certificate is None:
            with self._lock:
                # Another thread may have refreshed while this one waited.
                certificate = self._cached(serial_no)
                if certificate is None:
                    self._refresh(merchant, now=self._clock())
                    certificate = self._cache.get(serial_no)
        if certificate is None:
            raise WeChatNativeError("WeChat platform certificate serial not found")
        return certificate

    def _cached(self, serial_no: str) -> WeChatPlatformCertificate | None:
        """The cached certificate for serial_no, or None when stale or absent."""
        fetched_at = self._fetched_at
        if fetched_at is None or (self._clock() - fetched_at) >= self._cache_ttl_seconds:
            return None
        return self._cache.get(serial_no)

    def check_credentials(self, merchant: WeChatMerchantConfig) -> int:
        """Live credential probe: download the platform certificates once.

        Proves the whole merchant triple — the signed request is only accepted
        when ``mchid`` is enabled for Native and ``serial_no`` names a
        certificate matching ``private_key`` — and that ``api_v3_key`` decrypts
        the downloaded list. Returns the certificate count; raises
        ``WeChatNativeError`` (carrying WeChat's error code in the message)
        otherwise. Always hits the network on purpose: a diagnostic that
        answered from cache would prove nothing about the credentials.
        """
        request = self._build_request(merchant)
        raw = _execute_request(self._opener, request, timeout_seconds=self._timeout_seconds)
        self._cache = self._parse_certificates(merchant, raw.body)
        self._fetched_at = self._clock()
        return len(self._cache)

    def _refresh(self, merchant: WeChatMerchantConfig, *, now: float) -> None:
        request = self._build_request(merchant)
        raw = _execute_request(self._opener, request, timeout_seconds=self._timeout_seconds)
        self._cache = self._parse_certificates(merchant, raw.body)
        self._fetched_at = now

    def _build_request(self, merchant: WeChatMerchantConfig) -> Request:
        authorization = build_authorization_header(
            mchid=merchant.mchid,
            serial_no=merchant.serial_no,
            private_key_pem=merchant.private_key_pem,
            method="GET",
            url_path=CERTIFICATES_PATH,
            body="",
            timestamp=str(int(time.time())),
            nonce=secrets.token_hex(16),
        )
        request = Request(WECHAT_API_BASE + CERTIFICATES_PATH, method="GET")
        request.add_header("Authorization", authorization)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", WECHAT_USER_AGENT)
        return request

    def _parse_certificates(
        self, merchant: WeChatMerchantConfig, body: bytes
    ) -> dict[str, WeChatPlatformCertificate]:
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WeChatNativeError("WeChat certificates response is invalid") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("data"), list):
            raise WeChatNativeError("WeChat certificates response is invalid")
        certificates: dict[str, WeChatPlatformCertificate] = {}
        for item in decoded["data"]:
            certificate = self._parse_one(merchant, item)
            if certificate is not None:
                certificates[certificate.serial_no] = certificate
        if not certificates:
            raise WeChatNativeError("WeChat certificates response had no usable certificates")
        return certificates

    def _parse_one(
        self, merchant: WeChatMerchantConfig, item: object
    ) -> WeChatPlatformCertificate | None:
        if not isinstance(item, dict):
            return None
        serial = item.get("serial_no")
        encrypt = item.get("encrypt_certificate")
        if not isinstance(serial, str) or not isinstance(encrypt, dict):
            return None
        nonce = encrypt.get("nonce")
        ciphertext = encrypt.get("ciphertext")
        associated_data = encrypt.get("associated_data")
        if not isinstance(nonce, str) or not isinstance(ciphertext, str):
            return None
        associated = associated_data if isinstance(associated_data, str) else ""
        cert_pem = decrypt_aes_256_gcm(
            api_v3_key=merchant.api_v3_key,
            nonce=nonce,
            associated_data=associated,
            ciphertext_b64=ciphertext,
        )
        certificate = x509.load_pem_x509_certificate(cert_pem)
        public_key = certificate.public_key()
        if not isinstance(public_key, RSAPublicKey):
            raise WeChatNativeError("WeChat platform certificate must be RSA")
        return WeChatPlatformCertificate(
            serial_no=serial,
            public_key=public_key,
            not_after=certificate.not_valid_after_utc,
        )


_DEFAULT_CERTIFICATE_MANAGER: PlatformCertificateManager | None = None
_DEFAULT_CERTIFICATE_MANAGER_LOCK = threading.Lock()


def default_certificate_manager() -> PlatformCertificateManager:
    """The process-wide platform-certificate manager used by production callers.

    Providers and clients are constructed per request (the registry hands back a
    fresh instance on every ``get_payment_provider``), so a per-instance manager
    starts cold every time and the 12-hour cache never applies: each order and
    each callback would re-download ``/v3/certificates``. Sharing one manager is
    what makes that TTL real. Tests that inject an opener or a manager keep their
    own isolated instance.
    """
    global _DEFAULT_CERTIFICATE_MANAGER
    with _DEFAULT_CERTIFICATE_MANAGER_LOCK:
        if _DEFAULT_CERTIFICATE_MANAGER is None:
            _DEFAULT_CERTIFICATE_MANAGER = PlatformCertificateManager()
        return _DEFAULT_CERTIFICATE_MANAGER


# -----------------------------------------------------------------------------
# Cycle 2: WeChat Pay V3 Native HTTP client (order placement + query)
# -----------------------------------------------------------------------------


class WeChatNativeClient:
    """WeChat Pay V3 Native client: places orders and queries them by out_trade_no.

    Every API response is signature-verified against the platform certificate
    matching the Wechatpay-Serial header before its body is trusted.
    """

    def __init__(
        self,
        *,
        opener: WeChatHTTPOpener | None = None,
        timeout_seconds: float = WECHAT_TIMEOUT_SECONDS,
        cert_manager: PlatformCertificateManager | None = None,
    ) -> None:
        self._opener = opener or cast(WeChatHTTPOpener, urlopen)
        self._timeout_seconds = timeout_seconds
        if cert_manager is not None:
            self._cert_manager = cert_manager
        elif opener is None:
            # Production path: share the process-wide cache so the TTL applies.
            self._cert_manager = default_certificate_manager()
        else:
            # An injected opener must also serve the certificate download, so this
            # client keeps its own manager bound to that opener.
            self._cert_manager = PlatformCertificateManager(
                opener=self._opener, timeout_seconds=timeout_seconds
            )

    def create_native_order(
        self,
        *,
        merchant: WeChatMerchantConfig,
        deployment: WeChatDeploymentConfig,
        out_trade_no: str,
        description: str,
        amount_fen: int,
        attach: str | None = None,
        client_ip: str | None = None,
        time_expire: str | None = None,
    ) -> WeChatNativeOrderResult:
        """Place a Native order and return the weixin:// code_url for QR rendering.

        ``time_expire`` is an RFC 3339 instant after which WeChat stops accepting
        payment for this order. Passing it keeps the gateway's idea of the order's
        life identical to ours instead of relying on WeChat's implicit default.
        """
        if amount_fen <= 0:
            raise WeChatNativeError("WeChat native order amount must be positive")
        payload: dict[str, object] = {
            "appid": merchant.appid,
            "mchid": merchant.mchid,
            "description": description,
            "out_trade_no": out_trade_no,
            "notify_url": deployment.notify_url,
            "amount": {"total": amount_fen, "currency": "CNY"},
        }
        if time_expire:
            payload["time_expire"] = time_expire
        if attach:
            payload["attach"] = attach
        if client_ip:
            payload["scene_info"] = {"payer_client_ip": client_ip}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        raw = self._request(merchant, method="POST", url_path=NATIVE_ORDER_PATH, body=body)
        verified = self._verify_response(merchant, raw)
        parsed = _parse_json_object(verified)
        code_url = parsed.get("code_url")
        if not isinstance(code_url, str) or not code_url:
            raise WeChatNativeError("WeChat native order response is missing code_url")
        return WeChatNativeOrderResult(
            code_url=code_url, response_digest=hashlib.sha256(raw.body).hexdigest()
        )

    def query_order(
        self, *, merchant: WeChatMerchantConfig, out_trade_no: str
    ) -> WeChatOrderQueryResult:
        """Query an order by out_trade_no and normalize the trade_state into paid/unpaid."""
        url_path = ORDER_QUERY_PATH_TEMPLATE.format(out_trade_no=out_trade_no)
        raw = self._request(merchant, method="GET", url_path=url_path, body="")
        verified = self._verify_response(merchant, raw)
        parsed = _parse_json_object(verified)
        trade_state = parsed.get("trade_state")
        if not isinstance(trade_state, str) or not trade_state:
            raise WeChatNativeError("WeChat order query response is missing trade_state")
        if parsed.get("out_trade_no") != out_trade_no:
            raise WeChatNativeError("WeChat order query number mismatch")
        paid = trade_state == "SUCCESS"
        transaction_id = parsed.get("transaction_id")
        amount_fen: int | None = None
        amount = parsed.get("amount")
        if paid and isinstance(amount, dict):
            total = amount.get("total")
            if isinstance(total, int):
                amount_fen = total
        return WeChatOrderQueryResult(
            trade_state=trade_state,
            paid=paid,
            out_trade_no=out_trade_no,
            transaction_id=transaction_id if isinstance(transaction_id, str) else None,
            amount_fen=amount_fen,
            response_digest=hashlib.sha256(raw.body).hexdigest(),
        )

    def _request(
        self, merchant: WeChatMerchantConfig, *, method: str, url_path: str, body: str
    ) -> _RawResponse:
        authorization = build_authorization_header(
            mchid=merchant.mchid,
            serial_no=merchant.serial_no,
            private_key_pem=merchant.private_key_pem,
            method=method,
            url_path=url_path,
            body=body,
            timestamp=str(int(time.time())),
            nonce=secrets.token_hex(16),
        )
        data = body.encode("utf-8") if method == "POST" else None
        request = Request(WECHAT_API_BASE + url_path, data=data, method=method)
        request.add_header("Authorization", authorization)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", WECHAT_USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return _execute_request(self._opener, request, timeout_seconds=self._timeout_seconds)

    def _verify_response(self, merchant: WeChatMerchantConfig, raw: _RawResponse) -> bytes:
        if (
            raw.timestamp is None
            or raw.nonce is None
            or raw.signature is None
            or raw.serial is None
        ):
            raise WeChatSignatureError("WeChat response is missing verification headers")
        certificate = self._cert_manager.get_certificate(merchant, raw.serial)
        try:
            body_text = raw.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeChatNativeError("WeChat response body is not valid UTF-8") from exc
        message = build_response_verify_message(
            timestamp=raw.timestamp, nonce=raw.nonce, body=body_text
        )
        if not verify_sha256_rsa(
            public_key=certificate.public_key, signature_b64=raw.signature, message=message
        ):
            raise WeChatSignatureError("WeChat response signature verification failed")
        return raw.body
