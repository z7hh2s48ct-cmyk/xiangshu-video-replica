"""WeChat Native callback verification with real signatures — no network, no DB.

``verify_notification_raw`` is the security boundary in front of every wallet
credit: it decides whether a POST body really came from WeChat before the shared
settlement routine touches a wallet. The SQLite-lane suite that covered it was
retired together with that lane in #91, and the PostgreSQL settlement matrix
(``test_wechat_native_callback_pg``) reaches the route through a stub provider —
so between them nothing exercised the cryptography itself.

Every case here signs a real body with a self-signed platform certificate and
AES-256-GCM encrypts a real ``resource`` with the merchant api_v3_key. A
regression in the signature message layout, the AEAD parameters, the envelope
parsing or the settleable-payload guard fails here rather than in production.

The route cases assert the other half: how each verification outcome becomes a
WeChat acknowledgement. Settlement itself stays in the PostgreSQL matrix — these
cases deliberately cover only the answers that never reach a wallet.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import time
from collections.abc import Iterator

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_database
from app.payment_provider import MerchantConfig, get_payment_provider
from app.payment_routes import get_wechat_provider, router
from app.wechat_native_client import (
    WeChatMerchantConfig,
    WeChatNativeError,
    WeChatPlatformCertificate,
    build_response_verify_message,
    default_certificate_manager,
)
from app.wechat_native_provider import (
    WECHAT_CALLBACK_BODY_INVALID,
    WECHAT_CALLBACK_CERT_UNAVAILABLE,
    WECHAT_CALLBACK_CONFIG_INVALID,
    WECHAT_CALLBACK_DECRYPT_FAILED,
    WECHAT_CALLBACK_MERCHANT_MISMATCH,
    WECHAT_CALLBACK_MISSING_HEADERS,
    WECHAT_CALLBACK_PAYLOAD_INVALID,
    WECHAT_CALLBACK_RESOURCE_INVALID,
    WECHAT_CALLBACK_SIGNATURE_INVALID,
    WECHAT_CALLBACK_STALE_TIMESTAMP,
    WeChatNativeProvider,
)

API_V3_KEY = "0123456789abcdef0123456789abcdef"  # exactly 32 bytes
OTHER_API_V3_KEY = "fedcba9876543210fedcba9876543210"
PLATFORM_SERIAL = "PLATFORMSERIAL0001"
OUT_TRADE_NO = "20260921103000000000000000000001"
TRANSACTION_ID = "4200001234202609210000000001"
AMOUNT_FEN = 10000

SETTLEABLE_TRANSACTION: dict[str, object] = {
    "mchid": "1900000109",
    "appid": "wxprobeappid0001",
    "out_trade_no": OUT_TRADE_NO,
    "transaction_id": TRANSACTION_ID,
    "trade_state": "SUCCESS",
    "trade_type": "NATIVE",
    "amount": {"total": AMOUNT_FEN, "payer_total": AMOUNT_FEN, "currency": "CNY"},
}


# ---------------------------------------------------------------------------
# Fixtures: a self-signed stand-in for WeChat's platform certificate
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def platform_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def platform_cert(platform_key: rsa.RSAPrivateKey) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WeChat Pay Test Platform")])
    now = dt.datetime.now(dt.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(platform_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(platform_key, hashes.SHA256())
    )


@pytest.fixture(scope="module")
def merchant_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.fixture
def merchant(merchant_private_key_pem: str) -> MerchantConfig:
    return MerchantConfig(
        provider="wechat_native",
        raw={
            "appid": "wxprobeappid0001",
            "mchid": "1900000109",
            "serial_no": "MERCHANTSERIAL01",
            "api_v3_key": API_V3_KEY,
            "private_key": merchant_private_key_pem,
        },
        allowed_channels=("wxpay",),
    )


class StubCertManager:
    """Hands back the canned platform certificate, or fails like a download would."""

    def __init__(
        self, certificate: WeChatPlatformCertificate | None, *, unavailable: bool = False
    ) -> None:
        self._certificate = certificate
        self._unavailable = unavailable
        self.calls = 0

    def get_certificate(
        self, _merchant: WeChatMerchantConfig, serial_no: str
    ) -> WeChatPlatformCertificate:
        self.calls += 1
        if self._unavailable or self._certificate is None:
            raise WeChatNativeError("WeChat platform certificate serial not found")
        assert serial_no == self._certificate.serial_no
        return self._certificate


@pytest.fixture
def certificate(
    platform_key: rsa.RSAPrivateKey, platform_cert: x509.Certificate
) -> WeChatPlatformCertificate:
    return WeChatPlatformCertificate(
        serial_no=PLATFORM_SERIAL,
        public_key=platform_key.public_key(),
        not_after=platform_cert.not_valid_after_utc,
    )


@pytest.fixture
def provider(certificate: WeChatPlatformCertificate) -> WeChatNativeProvider:
    return WeChatNativeProvider(cert_manager=StubCertManager(certificate))


# ---------------------------------------------------------------------------
# Callback construction helpers (mirror what WeChat actually sends)
# ---------------------------------------------------------------------------


def encrypt_resource(
    transaction: dict[str, object] | bytes, *, api_v3_key: str = API_V3_KEY
) -> dict[str, object]:
    nonce = "callbacknonce"
    associated_data = "transaction"
    plaintext = (
        transaction
        if isinstance(transaction, bytes)
        else json.dumps(transaction, ensure_ascii=False).encode("utf-8")
    )
    ciphertext = AESGCM(api_v3_key.encode()).encrypt(
        nonce.encode(), plaintext, associated_data.encode()
    )
    return {
        "algorithm": "AEAD_AES_256_GCM",
        "original_type": "transaction",
        "associated_data": associated_data,
        "nonce": nonce,
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def envelope_bytes(resource: object) -> bytes:
    return json.dumps(
        {
            "id": "callback-event-id",
            "event_type": "TRANSACTION.SUCCESS",
            "resource_type": "encrypt-resource",
            "resource": resource,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def sign_headers(
    platform_key: rsa.RSAPrivateKey,
    raw_body: bytes,
    *,
    timestamp: str | None = None,
) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    nonce = "responsenonce01"
    message = build_response_verify_message(
        timestamp=timestamp, nonce=nonce, body=raw_body.decode("utf-8")
    )
    signature = platform_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return {
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": base64.b64encode(signature).decode("ascii"),
        "serial": PLATFORM_SERIAL,
    }


def signed_callback(
    platform_key: rsa.RSAPrivateKey,
    transaction: dict[str, object] | bytes = SETTLEABLE_TRANSACTION,
    *,
    api_v3_key: str = API_V3_KEY,
    timestamp: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    raw_body = envelope_bytes(encrypt_resource(transaction, api_v3_key=api_v3_key))
    return raw_body, sign_headers(platform_key, raw_body, timestamp=timestamp)


# ---------------------------------------------------------------------------
# The authentic, settleable notification
# ---------------------------------------------------------------------------


def test_settleable_callback_is_authenticated_and_fully_parsed(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    raw_body, headers = signed_callback(platform_key)

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    assert result.authenticated is True
    assert result.error_code is None
    assert result.trade_state == "SUCCESS"
    assert result.merchant_order_no == OUT_TRADE_NO
    assert result.provider_trade_no == TRANSACTION_ID
    assert result.amount_fen == AMOUNT_FEN
    assert result.channel == "wxpay"
    # The digest pins the exact bytes that were verified, so notify_digest is
    # evidence of what settled rather than of a re-serialized copy.
    assert result.source_digest == hashlib.sha256(raw_body).hexdigest()


def test_non_final_trade_state_is_authentic_but_carries_no_settlement(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    """NOTPAY is a real WeChat push; it must ACK without naming an error."""
    pending = {k: v for k, v in SETTLEABLE_TRANSACTION.items() if k != "transaction_id"}
    raw_body, headers = signed_callback(platform_key, {**pending, "trade_state": "NOTPAY"})

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    assert result.authenticated is True
    assert result.error_code is None
    assert result.trade_state == "NOTPAY"
    assert result.provider_trade_no is None


# ---------------------------------------------------------------------------
# Forgery and transport tampering
# ---------------------------------------------------------------------------


def test_tampered_body_fails_signature_verification(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    raw_body, headers = signed_callback(platform_key)
    tampered = bytearray(raw_body)
    tampered[-2] ^= 0x01

    result = provider.verify_notification_raw(
        raw_body=bytes(tampered), merchant=merchant, **headers
    )

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_SIGNATURE_INVALID


def test_signature_from_a_foreign_key_is_rejected(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    """An attacker who signs a well-formed body with their own key gets nothing."""
    raw_body, headers = signed_callback(platform_key)
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    result = provider.verify_notification_raw(
        raw_body=raw_body,
        merchant=merchant,
        **{**headers, **sign_headers(impostor, raw_body)},
    )

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_SIGNATURE_INVALID


@pytest.mark.parametrize("dropped", ["timestamp", "nonce", "signature", "serial"])
def test_any_missing_verification_header_is_rejected(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
    dropped: str,
) -> None:
    raw_body, headers = signed_callback(platform_key)

    result = provider.verify_notification_raw(
        raw_body=raw_body, merchant=merchant, **{**headers, dropped: None}
    )

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_MISSING_HEADERS


def test_signature_covers_the_timestamp_and_nonce_not_only_the_body(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    """Replacing the timestamp invalidates the signature — the message is 3 lines."""
    raw_body, headers = signed_callback(platform_key)
    # Fresh (so the freshness gate passes) but not the timestamp that was signed.
    replaced = {**headers, "timestamp": str(int(time.time()) + 60)}

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **replaced)

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_SIGNATURE_INVALID


def test_unavailable_platform_certificate_is_reported_separately(
    merchant: MerchantConfig, platform_key: rsa.RSAPrivateKey
) -> None:
    """A certificate the manager cannot supply is transient, not a forgery."""
    provider = WeChatNativeProvider(cert_manager=StubCertManager(None, unavailable=True))
    raw_body, headers = signed_callback(platform_key)

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_CERT_UNAVAILABLE


def test_incomplete_merchant_settings_are_reported_before_any_crypto(
    provider: WeChatNativeProvider, platform_key: rsa.RSAPrivateKey
) -> None:
    raw_body, headers = signed_callback(platform_key)
    unconfigured = MerchantConfig(provider="wechat_native", raw={}, allowed_channels=("wxpay",))

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=unconfigured, **headers)

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_CONFIG_INVALID


# ---------------------------------------------------------------------------
# Envelope and resource shapes
# ---------------------------------------------------------------------------


def test_body_that_is_not_a_json_object_is_rejected(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    raw_body = b"[1, 2, 3]"

    result = provider.verify_notification_raw(
        raw_body=raw_body, merchant=merchant, **sign_headers(platform_key, raw_body)
    )

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_BODY_INVALID


@pytest.mark.parametrize(
    "resource",
    [
        pytest.param("not-an-object", id="resource-not-object"),
        pytest.param({"nonce": "callbacknonce"}, id="ciphertext-missing"),
        pytest.param({"ciphertext": "AAAA"}, id="nonce-missing"),
    ],
)
def test_malformed_resource_is_rejected(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
    resource: object,
) -> None:
    raw_body = envelope_bytes(resource)

    result = provider.verify_notification_raw(
        raw_body=raw_body, merchant=merchant, **sign_headers(platform_key, raw_body)
    )

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_RESOURCE_INVALID


def test_resource_encrypted_under_a_different_api_v3_key_fails_to_decrypt(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    """The AEAD tag is what proves the merchant owns this notification."""
    raw_body, headers = signed_callback(
        platform_key, SETTLEABLE_TRANSACTION, api_v3_key=OTHER_API_V3_KEY
    )

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_DECRYPT_FAILED


# ---------------------------------------------------------------------------
# The settleable-payload guard: authentic, but must not credit a wallet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transaction",
    [
        pytest.param(
            {k: v for k, v in SETTLEABLE_TRANSACTION.items() if k != "transaction_id"},
            id="success-without-transaction-id",
        ),
        pytest.param(
            {k: v for k, v in SETTLEABLE_TRANSACTION.items() if k != "amount"},
            id="success-without-amount",
        ),
        pytest.param(
            {**SETTLEABLE_TRANSACTION, "amount": {"currency": "CNY"}},
            id="amount-without-total",
        ),
        pytest.param(
            {k: v for k, v in SETTLEABLE_TRANSACTION.items() if k != "out_trade_no"},
            id="no-order-number",
        ),
        pytest.param(
            {k: v for k, v in SETTLEABLE_TRANSACTION.items() if k != "trade_state"},
            id="no-trade-state",
        ),
    ],
)
def test_authentic_but_unsettleable_payloads_are_named_not_silently_dropped(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
    transaction: dict[str, object],
) -> None:
    raw_body, headers = signed_callback(platform_key, transaction)

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    # Authentic: the bytes really are WeChat's. Unusable: the route must FAIL so
    # WeChat re-pushes rather than ACK a notification that credited nothing.
    assert result.authenticated is True
    assert result.error_code == WECHAT_CALLBACK_PAYLOAD_INVALID


def test_plaintext_that_is_not_a_json_object_is_authentic_but_unusable(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    raw_body, headers = signed_callback(platform_key, b"not json at all")

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    assert result.authenticated is True
    assert result.error_code == WECHAT_CALLBACK_PAYLOAD_INVALID
    assert result.source_digest == hashlib.sha256(raw_body).hexdigest()


# ---------------------------------------------------------------------------
# The shared platform-certificate cache
# ---------------------------------------------------------------------------


def test_providers_built_by_the_registry_share_one_certificate_cache() -> None:
    """The registry builds a provider per request; a per-instance cache would mean
    every callback re-downloads /v3/certificates, which is what WeChat rate-limits."""
    first = get_payment_provider("wechat_native")
    second = get_payment_provider("wechat_native")

    assert first is not second
    assert isinstance(first, WeChatNativeProvider)
    assert isinstance(second, WeChatNativeProvider)
    assert first._cert_manager is second._cert_manager
    assert first._cert_manager is default_certificate_manager()


# ---------------------------------------------------------------------------
# Route mapping: verification outcome -> WeChat acknowledgement
# ---------------------------------------------------------------------------


class _CannedMerchantProvider(WeChatNativeProvider):
    """Real verification, canned settings: the route's DB read is not under test."""

    def __init__(self, merchant: MerchantConfig, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._merchant = merchant

    def load_merchant_config(self, conn: object) -> MerchantConfig:  # type: ignore[override]
        return self._merchant


def _notify_client(provider: WeChatNativeProvider) -> Iterator[TestClient]:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_wechat_provider] = lambda: provider
    # Every branch under test answers before settlement, so the connection is
    # never used; supplying one keeps the dependency from opening a real pool.
    application.dependency_overrides[get_database] = lambda: object()
    with TestClient(application) as client:
        yield client


@pytest.fixture
def notify_client(
    merchant: MerchantConfig, certificate: WeChatPlatformCertificate
) -> Iterator[TestClient]:
    yield from _notify_client(
        _CannedMerchantProvider(merchant, cert_manager=StubCertManager(certificate))
    )


def _post(client: TestClient, raw_body: bytes, headers: dict[str, str]) -> object:
    return client.post(
        "/api/payments/wechat_native/notify",
        content=raw_body,
        headers={
            "Wechatpay-Timestamp": headers["timestamp"],
            "Wechatpay-Nonce": headers["nonce"],
            "Wechatpay-Signature": headers["signature"],
            "Wechatpay-Serial": headers["serial"],
        },
    )


def test_route_acks_an_authentic_non_final_notification(
    notify_client: TestClient, platform_key: rsa.RSAPrivateKey
) -> None:
    """ACK stops this push; WeChat re-notifies when the trade reaches a terminal state."""
    pending = {k: v for k, v in SETTLEABLE_TRANSACTION.items() if k != "transaction_id"}
    raw_body, headers = signed_callback(platform_key, {**pending, "trade_state": "NOTPAY"})

    response = _post(notify_client, raw_body, headers)

    assert response.status_code == 200
    assert response.json() == {"code": "SUCCESS", "message": "OK"}


def test_route_finally_rejects_a_forged_notification(
    notify_client: TestClient, platform_key: rsa.RSAPrivateKey
) -> None:
    raw_body, headers = signed_callback(platform_key)
    tampered = bytearray(raw_body)
    tampered[-2] ^= 0x01

    response = _post(notify_client, bytes(tampered), headers)

    # 4xx is final: a forgery must not earn WeChat's retry schedule.
    assert response.status_code == 400
    assert response.json()["code"] == "FAIL"
    assert response.json()["message"] == WECHAT_CALLBACK_SIGNATURE_INVALID


def test_route_rejects_a_notification_with_no_verification_headers(
    notify_client: TestClient, platform_key: rsa.RSAPrivateKey
) -> None:
    raw_body, _ = signed_callback(platform_key)

    response = notify_client.post("/api/payments/wechat_native/notify", content=raw_body)

    assert response.status_code == 400
    assert response.json()["message"] == WECHAT_CALLBACK_MISSING_HEADERS


def test_route_asks_wechat_to_retry_when_the_certificate_is_unavailable(
    merchant: MerchantConfig, platform_key: rsa.RSAPrivateKey
) -> None:
    """A certificate download failure is transient: 5xx buys WeChat's retry."""
    provider = _CannedMerchantProvider(
        merchant, cert_manager=StubCertManager(None, unavailable=True)
    )
    raw_body, headers = signed_callback(platform_key)

    for client in _notify_client(provider):
        response = _post(client, raw_body, headers)

    assert response.status_code == 503
    assert response.json()["message"] == WECHAT_CALLBACK_CERT_UNAVAILABLE


def test_route_fails_an_authentic_success_that_cannot_settle(
    notify_client: TestClient, platform_key: rsa.RSAPrivateKey
) -> None:
    """A SUCCESS without its transaction_id must never ACK: nothing was credited."""
    incomplete = {k: v for k, v in SETTLEABLE_TRANSACTION.items() if k != "transaction_id"}
    raw_body, headers = signed_callback(platform_key, incomplete)

    response = _post(notify_client, raw_body, headers)

    assert response.status_code == 400
    assert response.json()["message"] == WECHAT_CALLBACK_PAYLOAD_INVALID


def test_route_reports_unconfigured_merchant_settings_as_retryable(
    certificate: WeChatPlatformCertificate, platform_key: rsa.RSAPrivateKey
) -> None:
    """Settings an operator has not finished are transient, not a bad callback."""

    class _UnconfiguredProvider(WeChatNativeProvider):
        def load_merchant_config(self, conn: object) -> MerchantConfig:  # type: ignore[override]
            raise ValueError("WeChat Native merchant settings are incomplete")

    raw_body, headers = signed_callback(platform_key)

    for client in _notify_client(_UnconfiguredProvider(cert_manager=StubCertManager(certificate))):
        response = _post(client, raw_body, headers)

    assert response.status_code == 503
    assert response.json()["message"] == "WECHAT_CONFIGURATION_INVALID"


# ---------------------------------------------------------------------------
# Freshness gate — a replayed notification dies before any certificate work
# ---------------------------------------------------------------------------


def test_stale_callback_timestamp_is_rejected_before_any_crypto(
    certificate: WeChatPlatformCertificate,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    stub = StubCertManager(certificate)
    provider = WeChatNativeProvider(cert_manager=stub)
    raw_body, headers = signed_callback(platform_key)
    stale = {**headers, "timestamp": str(int(time.time()) - 600)}

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **stale)

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_STALE_TIMESTAMP
    assert stub.calls == 0  # the cheap freshness gate fires before certificate work


def test_future_callback_timestamp_is_rejected_too(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    raw_body, headers = signed_callback(platform_key)
    future = {**headers, "timestamp": str(int(time.time()) + 600)}

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **future)

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_STALE_TIMESTAMP


def test_callback_within_the_freshness_window_verifies(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    """A minute-old notification — a normal retry — still settles."""
    raw_body, headers = signed_callback(platform_key, timestamp=str(int(time.time()) - 60))

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    assert result.authenticated is True
    assert result.error_code is None
    assert result.merchant_order_no == OUT_TRADE_NO


def test_non_numeric_callback_timestamp_is_rejected(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    raw_body, headers = signed_callback(platform_key)
    garbage = {**headers, "timestamp": "yesterday"}

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **garbage)

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_STALE_TIMESTAMP


# ---------------------------------------------------------------------------
# Merchant-ownership gate — an authentic push for another identity settles nothing
# ---------------------------------------------------------------------------


def test_callback_plaintext_for_another_merchant_is_not_settleable(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    """A genuinely WeChat-signed push naming a different mchid must not settle
    under this configuration (merchant-transition replay window)."""
    transaction = {**SETTLEABLE_TRANSACTION, "mchid": "9999999999"}
    raw_body, headers = signed_callback(platform_key, transaction)

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    assert result.authenticated is True  # the bytes are authentically WeChat's
    assert result.error_code == WECHAT_CALLBACK_MERCHANT_MISMATCH
    assert result.trade_state == "SUCCESS"


def test_callback_plaintext_for_another_appid_is_not_settleable(
    provider: WeChatNativeProvider,
    merchant: MerchantConfig,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    transaction = {**SETTLEABLE_TRANSACTION, "appid": "wxotherappid00001"}
    raw_body, headers = signed_callback(platform_key, transaction)

    result = provider.verify_notification_raw(raw_body=raw_body, merchant=merchant, **headers)

    assert result.authenticated is True
    assert result.error_code == WECHAT_CALLBACK_MERCHANT_MISMATCH


# ---------------------------------------------------------------------------
# 微信支付公钥模式 —— 新商户没有平台证书，老商户切换期两种签名灰度混用
# ---------------------------------------------------------------------------

PUBLIC_KEY_ID = "PUB_KEY_ID_0114232134912410000000000000"


@pytest.fixture
def public_key_merchant(
    merchant: MerchantConfig, platform_key: rsa.RSAPrivateKey
) -> MerchantConfig:
    public_key_pem = (
        platform_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )
    return MerchantConfig(
        provider=merchant.provider,
        raw={**merchant.raw, "public_key_id": PUBLIC_KEY_ID, "public_key": public_key_pem},
        allowed_channels=merchant.allowed_channels,
    )


def test_public_key_signed_callback_verifies_without_the_certificate_manager(
    public_key_merchant: MerchantConfig, platform_key: rsa.RSAPrivateKey
) -> None:
    stub = StubCertManager(None, unavailable=True)
    provider = WeChatNativeProvider(cert_manager=stub)
    raw_body, headers = signed_callback(platform_key)

    result = provider.verify_notification_raw(
        raw_body=raw_body, merchant=public_key_merchant, **{**headers, "serial": PUBLIC_KEY_ID}
    )

    assert result.authenticated is True
    assert result.error_code is None
    assert result.merchant_order_no == OUT_TRADE_NO
    assert stub.calls == 0  # 公钥回调不触发任何证书下载


def test_certificate_signed_callback_still_verifies_after_configuring_a_public_key(
    public_key_merchant: MerchantConfig,
    certificate: WeChatPlatformCertificate,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    """灰度期内同一商户的回调可能仍用平台证书签名。"""
    provider = WeChatNativeProvider(cert_manager=StubCertManager(certificate))
    raw_body, headers = signed_callback(platform_key)

    result = provider.verify_notification_raw(
        raw_body=raw_body, merchant=public_key_merchant, **headers
    )

    assert result.authenticated is True
    assert result.error_code is None


def test_public_key_callback_forged_with_another_key_is_rejected(
    public_key_merchant: MerchantConfig, platform_key: rsa.RSAPrivateKey
) -> None:
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raw_body, _ = signed_callback(platform_key)
    forged = {**sign_headers(impostor, raw_body), "serial": PUBLIC_KEY_ID}

    result = WeChatNativeProvider(
        cert_manager=StubCertManager(None, unavailable=True)
    ).verify_notification_raw(raw_body=raw_body, merchant=public_key_merchant, **forged)

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_SIGNATURE_INVALID


def test_public_key_callback_without_a_configured_public_key_is_retryable(
    merchant: MerchantConfig, platform_key: rsa.RSAPrivateKey
) -> None:
    """商户还没录公钥时，公钥签名的回调按“暂不可用”处理：路由回 503 让微信重试，
    运营补完配置后仍能入账，而不是 400 让微信放弃。"""
    stub = StubCertManager(None, unavailable=True)
    raw_body, headers = signed_callback(platform_key)

    result = WeChatNativeProvider(cert_manager=stub).verify_notification_raw(
        raw_body=raw_body, merchant=merchant, **{**headers, "serial": PUBLIC_KEY_ID}
    )

    assert result.authenticated is False
    assert result.error_code == WECHAT_CALLBACK_CERT_UNAVAILABLE
    assert stub.calls == 0


# ---------------------------------------------------------------------------
# 回调的同步工作不得阻塞事件循环
# ---------------------------------------------------------------------------


def test_route_runs_merchant_load_and_verification_off_the_event_loop(
    merchant: MerchantConfig,
    certificate: WeChatPlatformCertificate,
    platform_key: rsa.RSAPrivateKey,
) -> None:
    """读配置查库、下载证书都是同步阻塞调用，必须在线程池里跑。"""
    seen: list[str] = []

    def assert_off_loop(step: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append(step)
            return
        raise AssertionError(f"{step} ran on the event loop")

    class _LoopCheckingProvider(_CannedMerchantProvider):
        def load_merchant_config(self, conn: object) -> MerchantConfig:  # type: ignore[override]
            assert_off_loop("load_merchant_config")
            return super().load_merchant_config(conn)

        def verify_notification_raw(self, **kwargs):  # type: ignore[no-untyped-def,override]
            assert_off_loop("verify_notification_raw")
            return super().verify_notification_raw(**kwargs)

    provider = _LoopCheckingProvider(merchant, cert_manager=StubCertManager(certificate))
    pending = {k: v for k, v in SETTLEABLE_TRANSACTION.items() if k != "transaction_id"}
    raw_body, headers = signed_callback(platform_key, {**pending, "trade_state": "NOTPAY"})

    for client in _notify_client(provider):
        response = _post(client, raw_body, headers)

    assert response.status_code == 200
    assert seen == ["load_merchant_config", "verify_notification_raw"]
