"""Test suite for WeChat Native encryption primitives and configuration parsing.

TDD: Test-first development. This module tests the pure functions and dataclasses
from wechat_native_client before implementation. Tests use mock RSA keys, not real
WeChat credentials.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

from app.wechat_native_client import (
    CERTIFICATES_PATH,
    NATIVE_ORDER_PATH,
    WECHAT_API_BASE,
    PlatformCertificateManager,
    WeChatDeploymentConfig,
    WeChatMerchantConfig,
    WeChatNativeClient,
    WeChatNativeError,
    WeChatSignatureError,
    build_authorization_header,
    build_request_signature_message,
    build_response_verify_message,
    decrypt_aes_256_gcm,
    default_certificate_manager,
    deployment_config_from_environment,
    merchant_config_from_settings,
    sign_sha256_rsa,
    verify_sha256_rsa,
    wechat_error_detail,
)


@pytest.fixture(scope="module")
def merchant_private_key() -> rsa.RSAPrivateKey:
    """Generate a fresh 2048-bit RSA private key for testing."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def merchant_private_key_pem(merchant_private_key) -> str:
    """Export the private key to PEM format (unencrypted PKCS#8)."""
    return merchant_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def test_build_request_signature_message_has_five_lines_and_trailing_newline():
    """Signature message must be method\nurl\nts\nnonce\nbody\n (5 newlines)."""
    msg = build_request_signature_message(
        method="POST",
        url_path="/v3/pay/transactions/native",
        timestamp="1600000000",
        nonce="abc123",
        body='{"a":1}',
    )
    expected = 'POST\n/v3/pay/transactions/native\n1600000000\nabc123\n{"a":1}\n'
    assert msg == expected


def test_sign_sha256_rsa_roundtrip_verifies_with_public_key(
    merchant_private_key: rsa.RSAPrivateKey,
    merchant_private_key_pem: str,
):
    """Signing with private key should verify successfully with public key."""
    message = "POST\n/v3/x\n1600000000\nnonce_test123\n{}\n"
    sig_b64 = sign_sha256_rsa(private_key_pem=merchant_private_key_pem, message=message)
    assert isinstance(sig_b64, str)
    assert len(sig_b64) > 0
    # Verify using the public key
    result = verify_sha256_rsa(
        public_key=merchant_private_key.public_key(),
        signature_b64=sig_b64,
        message=message,
    )
    assert result is True


def test_sign_sha256_rsa_rejects_tampered_message(
    merchant_private_key: rsa.RSAPrivateKey,
    merchant_private_key_pem: str,
):
    """A tampered message should fail verification."""
    original_msg = "POST\n/v3/y\n1600000000\nnonce_xyz\n{}\n"
    sig_b64 = sign_sha256_rsa(private_key_pem=merchant_private_key_pem, message=original_msg)
    # Tamper the message
    assert (
        verify_sha256_rsa(
            public_key=merchant_private_key.public_key(),
            signature_b64=sig_b64,
            message="tampered message",
        )
        is False
    )


def test_sign_sha256_rsa_produces_valid_base64(
    merchant_private_key_pem: str,
):
    """Signature output must be valid Base64 ASCII."""
    import re

    sig_b64 = sign_sha256_rsa(private_key_pem=merchant_private_key_pem, message="test")
    assert re.fullmatch(r"[A-Za-z0-9+/]+=*", sig_b64)


def test_verify_sha256_rsa_rejects_invalid_base64(
    merchant_private_key: rsa.RSAPrivateKey,
):
    """Invalid Base64 input should return False."""
    sig_b64 = "!!!not-valid-base64!!!"
    result = verify_sha256_rsa(
        public_key=merchant_private_key.public_key(),
        signature_b64=sig_b64,
        message="any message",
    )
    assert result is False


def test_verify_sha256_rsa_rejects_wrong_length_signature(
    merchant_private_key: rsa.RSAPrivateKey,
):
    """Wrong-length signature bytes should fail decoding or verify."""
    # Too short: RSA-2048 signature is 256 bytes → base64 ~344 chars
    sig_b64 = "a" * 100
    assert (
        verify_sha256_rsa(
            public_key=merchant_private_key.public_key(),
            signature_b64=sig_b64,
            message="x",
        )
        is False
    )


def test_build_authorization_header_format_fields(
    merchant_private_key_pem: str,
):
    """Authorization header must contain all required fields in schema."""
    header = build_authorization_header(
        mchid="1900000001",
        serial_no="SERIAL_TEST_123",
        private_key_pem=merchant_private_key_pem,
        method="POST",
        url_path="/v3/pay/transactions/native",
        body='{"app_id":"wx123","mch_id":"1900000001"}',
        timestamp="1600000000",
        nonce="header_nonce_abc",
    )
    # Header prefix
    assert header.startswith("WECHATPAY2-SHA256-RSA2048 ")
    # Required fragments
    for fragment in [
        'mchid="1900000001"',
        'nonce_str="header_nonce_abc"',
        'timestamp="1600000000"',
        'serial_no="SERIAL_TEST_123"',
        'signature="',
    ]:
        assert fragment in header, f"Missing: {fragment}"


def test_build_authorization_header_contains_valid_signature(
    merchant_private_key: rsa.RSAPrivateKey,
    merchant_private_key_pem: str,
):
    """The signature in authorization header must verify against the message part of the header."""
    header = build_authorization_header(
        mchid="1900000001",
        serial_no="SERIAL_XYZ",
        private_key_pem=merchant_private_key_pem,
        method="GET",
        url_path="/v3/certificates",
        body="",
        timestamp="1700000000",
        nonce="auth_nonce",
    )
    # Parse signature from header
    import re

    match = re.search(r'signature="([^"]+)"', header)
    assert match is not None
    sig_in_header = match.group(1)

    # Reconstruct message
    message = build_request_signature_message(
        method="GET",
        url_path="/v3/certificates",
        timestamp="1700000000",
        nonce="auth_nonce",
        body="",
    )

    # Verify
    assert (
        verify_sha256_rsa(
            public_key=merchant_private_key.public_key(),
            signature_b64=sig_in_header,
            message=message,
        )
        is True
    )


def test_decrypt_aes_256_gcm_roundtrip_success():
    """AES-GCM decryption should recover plaintext after encryption."""
    key = "0123456789abcdef0123456789abcdef"  # 32 bytes string
    nonce = "unique_nonce"  # 12 bytes (recommended for GCM)
    aad = "certificate"  # associated data
    plaintext = b'{"serial_no":"ABC123","effective_time":"2026-01-01"}'
    ciphertext = AESGCM(key.encode()).encrypt(nonce.encode(), plaintext, aad.encode())
    result = decrypt_aes_256_gcm(
        api_v3_key=key,
        nonce=nonce,
        associated_data=aad,
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
    )
    assert result == plaintext


def test_decrypt_aes_256_gcm_rejects_wrong_api_v3_key():
    """Wrong key should raise InvalidTag error during decryption."""
    correct_key = "0123456789abcdef0123456789abcdef"
    wrong_key = "ffffffffffffffffffffffffffffffff"
    nonce = "correct_nonce"
    aad = "associated"
    ct = AESGCM(correct_key.encode()).encrypt(nonce.encode(), b"secret_data", aad.encode())
    with pytest.raises(WeChatNativeError, match=r"authentication tag mismatch|AES-256-GCM"):
        decrypt_aes_256_gcm(
            api_v3_key=wrong_key,
            nonce=nonce,
            associated_data=aad,
            ciphertext_b64=base64.b64encode(ct).decode("ascii"),
        )


def test_decrypt_aes_256_gcm_rejects_bad_api_v3_key_length():
    """api_v3_key must be exactly 32 bytes."""
    too_short_key = "short"
    with pytest.raises(WeChatNativeError, match=r"32 bytes"):
        decrypt_aes_256_gcm(
            api_v3_key=too_short_key,
            nonce="unique_nonce",
            associated_data="cert",
            ciphertext_b64=base64.b64encode(b"ct").decode("ascii"),
        )


def test_decrypt_aes_256_gcm_rejects_invalid_base64_ciphertext():
    """Invalid Base64 should raise WeChatNativeError."""
    with pytest.raises(WeChatNativeError):
        decrypt_aes_256_gcm(
            api_v3_key="0123456789abcdef0123456789abcdef",
            nonce="unique_nonce",
            associated_data="cert",
            ciphertext_b64="!!!invalid-b64!!!",
        )


def test_merchant_config_from_settings_parses_complete_wechat_config():
    """Complete WeChat Native settings should parse to WeChatMerchantConfig."""
    cfg = merchant_config_from_settings(
        {
            "appid": "wx8123456789",
            "mchid": "1900000001",
            "serial_no": "ABCDEF1234567890",
            "api_v3_key": "0123456789abcdef0123456789abcdef",
            "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        }
    )
    assert isinstance(cfg, WeChatMerchantConfig)
    assert cfg.appid == "wx8123456789"
    assert cfg.mchid == "1900000001"
    assert cfg.serial_no == "ABCDEF1234567890"
    assert cfg.api_v3_key == "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "missing_field",
    ["appid", "mchid", "serial_no", "api_v3_key", "private_key"],
)
def test_merchant_config_from_settings_rejects_incomplete_settings(missing_field: str):
    """Settings missing any required field should raise ValueError."""
    base_settings = {
        "appid": "wx123",
        "mchid": "1900000001",
        "serial_no": "SER1",
        "api_v3_key": "0123456789abcdef0123456789abcdef",
        "private_key": "privatekey",
    }
    base_settings.pop(missing_field)
    with pytest.raises(ValueError, match=r"incomplete|Incomplete"):
        merchant_config_from_settings(base_settings)


def test_merchant_config_rejects_api_v3_key_wrong_byte_length():
    """API v3 key must decode to exactly 32 bytes."""
    bad_key = "tooshort"  # <32 bytes
    with pytest.raises(ValueError, match=r"32 bytes"):
        merchant_config_from_settings(
            {
                "appid": "wx123",
                "mchid": "mch",
                "serial_no": "sn",
                "api_v3_key": bad_key,
                "private_key": "k",
            }
        )


def test_deployment_notify_url_constructed_from_public_base_url(monkeypatch):
    """Deployment config derives notify_url from PUBLIC_BASE_URL environment variable."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://video.example.com")
    dep = deployment_config_from_environment()
    assert dep.notify_url == "https://video.example.com/api/payments/wechat_native/notify"


@pytest.mark.parametrize(
    ("bad_url", "expected_error_type"),
    [
        ("", ValueError),
        ("http://plain.http", ValueError),
        ("https://example.com/path/extra", ValueError),
        ("https://user:pass@example.com", ValueError),
        ("https://example.com?query=1", ValueError),
        ("ftp://ftpsite.com", ValueError),
    ],
)
def test_deployment_rejects_invalid_base_url(
    monkeypatch, bad_url: str, expected_error_type: type[Exception]
):
    """A non-HTTPS origin, or one with path/query/fragment, must raise ValueError."""
    monkeypatch.setenv("PUBLIC_BASE_URL", bad_url)
    with pytest.raises(expected_error_type):
        deployment_config_from_environment()


# ---------------------------------------------------------------------------
# Cycle 2: mock HTTP infrastructure (opener dependency injection, zero network)
# ---------------------------------------------------------------------------

API_V3_KEY = "0123456789abcdef0123456789abcdef"


class _MockResponse:
    """Minimal stand-in for a urllib response: read() + case-insensitive getheader()."""

    def __init__(
        self, body: bytes, headers: dict[str, str] | None = None, status: int = 200
    ) -> None:
        self._body = body
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.status = status

    def __enter__(self) -> _MockResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            return self._body
        return self._body[:amount]

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)


class _RouteOpener:
    """Opener dispatching by URL substring; records every request for assertions."""

    def __init__(self, routes: dict[str, object]) -> None:
        self._routes = routes
        self.calls: list[Request] = []

    def __call__(self, request: Request, *, timeout: float) -> _MockResponse:
        self.calls.append(request)
        url = request.full_url
        for substring, handler in self._routes.items():
            if substring in url:
                outcome = handler(request) if callable(handler) else handler
                if isinstance(outcome, Exception):
                    raise outcome
                assert isinstance(outcome, _MockResponse)
                return outcome
        raise AssertionError(f"unrouted URL in test: {url}")


class _FakeClock:
    """Deterministic monotonic clock for cache-TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _encrypt_certificate(api_v3_key: str, cert_pem: bytes, serial_no: str) -> dict[str, object]:
    nonce = "certnonce123"
    associated_data = "certificate"
    ciphertext = AESGCM(api_v3_key.encode()).encrypt(
        nonce.encode(), cert_pem, associated_data.encode()
    )
    return {
        "serial_no": serial_no,
        "effective_time": "2026-01-01T00:00:00+08:00",
        "expire_time": "2031-01-01T00:00:00+08:00",
        "encrypt_certificate": {
            "algorithm": "AEAD_AES_256_GCM",
            "nonce": nonce,
            "associated_data": associated_data,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        },
    }


def _certificates_response(api_v3_key: str, cert_pem: bytes, serial_no: str) -> _MockResponse:
    entry = _encrypt_certificate(api_v3_key, cert_pem, serial_no)
    body = json.dumps({"data": [entry]}).encode()
    return _MockResponse(body=body, headers={"Wechatpay-Serial": serial_no}, status=200)


def _signed_response(platform_key: rsa.RSAPrivateKey, body: bytes, serial_no: str) -> _MockResponse:
    timestamp = "1700000000"
    nonce = "respnonce123456"
    message = build_response_verify_message(
        timestamp=timestamp, nonce=nonce, body=body.decode("utf-8")
    )
    signature = platform_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    headers = {
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Signature": base64.b64encode(signature).decode("ascii"),
        "Wechatpay-Serial": serial_no,
    }
    return _MockResponse(body=body, headers=headers, status=200)


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
def platform_cert_pem(platform_cert: x509.Certificate) -> bytes:
    return platform_cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def platform_serial(platform_cert: x509.Certificate) -> str:
    return format(platform_cert.serial_number, "X")


@pytest.fixture(scope="module")
def merchant_config(merchant_private_key_pem: str) -> WeChatMerchantConfig:
    return WeChatMerchantConfig(
        appid="wx8123456789",
        mchid="1900000001",
        serial_no="MERCHANTSERIAL0001",
        api_v3_key=API_V3_KEY,
        private_key_pem=merchant_private_key_pem,
    )


@pytest.fixture(scope="module")
def deployment() -> WeChatDeploymentConfig:
    return WeChatDeploymentConfig(
        notify_url="https://video.example.com/api/payments/wechat_native/notify"
    )


def _native_routes(
    handler: object, platform_cert_pem: bytes, platform_serial: str
) -> dict[str, object]:
    return {
        NATIVE_ORDER_PATH: handler,
        CERTIFICATES_PATH: _certificates_response(API_V3_KEY, platform_cert_pem, platform_serial),
    }


def _query_routes(
    handler: object, platform_cert_pem: bytes, platform_serial: str
) -> dict[str, object]:
    return {
        "/v3/pay/transactions/out-trade-no": handler,
        CERTIFICATES_PATH: _certificates_response(API_V3_KEY, platform_cert_pem, platform_serial),
    }


# --- Platform certificate manager -------------------------------------------


def test_certificate_manager_downloads_and_decrypts_platform_certificate(
    platform_cert_pem: bytes, platform_serial: str, merchant_config: WeChatMerchantConfig
) -> None:
    opener = _RouteOpener(
        {CERTIFICATES_PATH: _certificates_response(API_V3_KEY, platform_cert_pem, platform_serial)}
    )
    manager = PlatformCertificateManager(opener=opener)
    cert = manager.get_certificate(merchant_config, platform_serial)
    assert cert.serial_no == platform_serial
    assert cert.public_key is not None
    assert isinstance(cert.not_after, dt.datetime)


def test_certificate_manager_caches_without_redownload(
    platform_cert_pem: bytes, platform_serial: str, merchant_config: WeChatMerchantConfig
) -> None:
    opener = _RouteOpener(
        {CERTIFICATES_PATH: _certificates_response(API_V3_KEY, platform_cert_pem, platform_serial)}
    )
    manager = PlatformCertificateManager(opener=opener)
    manager.get_certificate(merchant_config, platform_serial)
    manager.get_certificate(merchant_config, platform_serial)
    assert len(opener.calls) == 1


def test_certificate_manager_refreshes_after_ttl(
    platform_cert_pem: bytes, platform_serial: str, merchant_config: WeChatMerchantConfig
) -> None:
    opener = _RouteOpener(
        {CERTIFICATES_PATH: _certificates_response(API_V3_KEY, platform_cert_pem, platform_serial)}
    )
    clock = _FakeClock()
    manager = PlatformCertificateManager(opener=opener, cache_ttl_seconds=100.0, clock=clock)
    manager.get_certificate(merchant_config, platform_serial)
    clock.advance(50.0)
    manager.get_certificate(merchant_config, platform_serial)
    assert len(opener.calls) == 1
    clock.advance(60.0)
    manager.get_certificate(merchant_config, platform_serial)
    assert len(opener.calls) == 2


def test_certificate_manager_unknown_serial_raises(
    platform_cert_pem: bytes, platform_serial: str, merchant_config: WeChatMerchantConfig
) -> None:
    opener = _RouteOpener(
        {CERTIFICATES_PATH: _certificates_response(API_V3_KEY, platform_cert_pem, platform_serial)}
    )
    manager = PlatformCertificateManager(opener=opener)
    with pytest.raises(WeChatNativeError):
        manager.get_certificate(merchant_config, "UNKNOWN-SERIAL-999")


# --- Native order ------------------------------------------------------------


def test_create_native_order_returns_code_url(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    body = json.dumps({"code_url": "weixin://wxpay/bizpayurl?pr=TESTCODE"}).encode()
    signed = _signed_response(platform_key, body, platform_serial)
    opener = _RouteOpener(_native_routes(signed, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    result = client.create_native_order(
        merchant=merchant_config,
        deployment=deployment,
        out_trade_no="OUT20260912001",
        description="内部视频充值 10 条",
        amount_fen=1000,
    )
    assert result.code_url == "weixin://wxpay/bizpayurl?pr=TESTCODE"
    assert len(result.response_digest) == 64


def test_create_native_order_request_body_contains_required_fields(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    body = json.dumps({"code_url": "weixin://wxpay/bizpayurl?pr=X"}).encode()
    signed = _signed_response(platform_key, body, platform_serial)
    opener = _RouteOpener(_native_routes(signed, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    client.create_native_order(
        merchant=merchant_config,
        deployment=deployment,
        out_trade_no="OUT_BODY_001",
        description="测试商品",
        amount_fen=2500,
    )
    native_request = next(c for c in opener.calls if NATIVE_ORDER_PATH in c.full_url)
    assert native_request.data is not None
    payload = json.loads(native_request.data.decode("utf-8"))
    assert payload["appid"] == merchant_config.appid
    assert payload["mchid"] == merchant_config.mchid
    assert payload["out_trade_no"] == "OUT_BODY_001"
    assert payload["description"] == "测试商品"
    assert payload["notify_url"] == deployment.notify_url
    assert payload["amount"]["total"] == 2500
    assert payload["amount"]["currency"] == "CNY"


def test_create_native_order_authorization_verifies_with_merchant_key(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_private_key: rsa.RSAPrivateKey,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    body = json.dumps({"code_url": "weixin://x"}).encode()
    signed = _signed_response(platform_key, body, platform_serial)
    opener = _RouteOpener(_native_routes(signed, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    client.create_native_order(
        merchant=merchant_config,
        deployment=deployment,
        out_trade_no="OUT_AUTH_001",
        description="d",
        amount_fen=100,
    )
    native_request = next(c for c in opener.calls if NATIVE_ORDER_PATH in c.full_url)
    authorization = native_request.get_header("Authorization")
    assert authorization is not None
    assert native_request.data is not None
    signature = re.search(r'signature="([^"]+)"', authorization)
    timestamp = re.search(r'timestamp="([^"]+)"', authorization)
    nonce = re.search(r'nonce_str="([^"]+)"', authorization)
    assert signature is not None and timestamp is not None and nonce is not None
    message = build_request_signature_message(
        method="POST",
        url_path=NATIVE_ORDER_PATH,
        timestamp=timestamp.group(1),
        nonce=nonce.group(1),
        body=native_request.data.decode("utf-8"),
    )
    assert (
        verify_sha256_rsa(
            public_key=merchant_private_key.public_key(),
            signature_b64=signature.group(1),
            message=message,
        )
        is True
    )


def test_create_native_order_rejects_invalid_response_signature(
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    body = json.dumps({"code_url": "weixin://x"}).encode()
    tampered = _MockResponse(
        body=body,
        headers={
            "Wechatpay-Timestamp": "1700000000",
            "Wechatpay-Nonce": "respnonce123456",
            "Wechatpay-Signature": base64.b64encode(b"not-a-real-signature").decode(),
            "Wechatpay-Serial": platform_serial,
        },
    )
    opener = _RouteOpener(_native_routes(tampered, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    with pytest.raises(WeChatSignatureError):
        client.create_native_order(
            merchant=merchant_config,
            deployment=deployment,
            out_trade_no="OUT_BAD_SIG",
            description="d",
            amount_fen=100,
        )


def test_create_native_order_http_error_raises(
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    error = HTTPError(WECHAT_API_BASE + NATIVE_ORDER_PATH, 500, "Server Error", {}, None)
    opener = _RouteOpener(_native_routes(error, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    with pytest.raises(WeChatNativeError):
        client.create_native_order(
            merchant=merchant_config,
            deployment=deployment,
            out_trade_no="OUT_HTTP_ERR",
            description="d",
            amount_fen=100,
        )


def test_create_native_order_rejects_missing_code_url(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    body = json.dumps({"prepay_id": "wx123-no-code-url"}).encode()
    signed = _signed_response(platform_key, body, platform_serial)
    opener = _RouteOpener(_native_routes(signed, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    with pytest.raises(WeChatNativeError):
        client.create_native_order(
            merchant=merchant_config,
            deployment=deployment,
            out_trade_no="OUT_NO_CODEURL",
            description="d",
            amount_fen=100,
        )


# --- Order query -------------------------------------------------------------


def test_query_order_paid_returns_transaction_details(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
) -> None:
    body = json.dumps(
        {
            "trade_state": "SUCCESS",
            "out_trade_no": "OUT_Q_001",
            "transaction_id": "4200001234202609120001",
            "amount": {"total": 1000, "payer_total": 1000},
        }
    ).encode()
    signed = _signed_response(platform_key, body, platform_serial)
    opener = _RouteOpener(_query_routes(signed, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    result = client.query_order(merchant=merchant_config, out_trade_no="OUT_Q_001")
    assert result.paid is True
    assert result.trade_state == "SUCCESS"
    assert result.transaction_id == "4200001234202609120001"
    assert result.amount_fen == 1000
    assert result.out_trade_no == "OUT_Q_001"


def test_query_order_not_paid_returns_unpaid(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
) -> None:
    body = json.dumps({"trade_state": "NOTPAY", "out_trade_no": "OUT_Q_002"}).encode()
    signed = _signed_response(platform_key, body, platform_serial)
    opener = _RouteOpener(_query_routes(signed, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    result = client.query_order(merchant=merchant_config, out_trade_no="OUT_Q_002")
    assert result.paid is False
    assert result.trade_state == "NOTPAY"
    assert result.transaction_id is None
    assert result.amount_fen is None


# --- Order expiry (time_expire) ----------------------------------------------


def _placed_order_payload(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
    **order_kwargs: object,
) -> dict[str, object]:
    """Place one Native order and return the JSON body that reached WeChat."""
    body = json.dumps({"code_url": "weixin://wxpay/bizpayurl?pr=expiry"}).encode()
    signed = _signed_response(platform_key, body, platform_serial)
    opener = _RouteOpener(_native_routes(signed, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)
    client.create_native_order(
        merchant=merchant_config,
        deployment=deployment,
        out_trade_no="OUT_EXPIRY_001",
        description="d",
        amount_fen=100,
        **order_kwargs,  # type: ignore[arg-type]
    )
    order_request = next(call for call in opener.calls if NATIVE_ORDER_PATH in call.full_url)
    assert order_request.data is not None
    return cast(dict[str, object], json.loads(order_request.data.decode("utf-8")))


def test_create_native_order_forwards_time_expire(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    """Telling WeChat the deadline keeps its idea of the order's life equal to ours."""
    payload = _placed_order_payload(
        platform_key,
        platform_cert_pem,
        platform_serial,
        merchant_config,
        deployment,
        time_expire="2026-09-21T12:30:00+08:00",
    )

    assert payload["time_expire"] == "2026-09-21T12:30:00+08:00"


def test_create_native_order_omits_time_expire_when_none(
    platform_key: rsa.RSAPrivateKey,
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    """An absent deadline must not become an empty string WeChat would reject."""
    payload = _placed_order_payload(
        platform_key, platform_cert_pem, platform_serial, merchant_config, deployment
    )

    assert "time_expire" not in payload


# --- Error reporting ----------------------------------------------------------


def test_wechat_error_detail_surfaces_the_actionable_code_and_message() -> None:
    """Wrong serial, wrong key and a disabled merchant are only distinguishable here."""
    body = json.dumps({"code": "SIGN_ERROR", "message": "签名错误"}).encode()
    error = HTTPError(
        WECHAT_API_BASE + NATIVE_ORDER_PATH, 401, "Unauthorized", {}, io.BytesIO(body)
    )

    detail = wechat_error_detail(error)

    assert "HTTP 401" in detail
    assert "code=SIGN_ERROR" in detail
    assert "message=签名错误" in detail


@pytest.mark.parametrize(
    "fp",
    [
        pytest.param(None, id="no-body"),
        pytest.param(io.BytesIO(b"<html>gateway</html>"), id="not-json"),
        pytest.param(io.BytesIO(b'"a string"'), id="json-but-not-an-object"),
    ],
)
def test_wechat_error_detail_degrades_to_the_bare_status(fp: io.BytesIO | None) -> None:
    """An unparseable body must never mask the original failure."""
    error = HTTPError(WECHAT_API_BASE + NATIVE_ORDER_PATH, 502, "Bad Gateway", {}, fp)

    assert wechat_error_detail(error) == "HTTP 502"


def test_create_native_order_error_carries_the_wechat_code(
    platform_cert_pem: bytes,
    platform_serial: str,
    merchant_config: WeChatMerchantConfig,
    deployment: WeChatDeploymentConfig,
) -> None:
    body = json.dumps({"code": "NOAUTH", "message": "商户无权限"}).encode()
    error = HTTPError(WECHAT_API_BASE + NATIVE_ORDER_PATH, 403, "Forbidden", {}, io.BytesIO(body))
    opener = _RouteOpener(_native_routes(error, platform_cert_pem, platform_serial))
    client = WeChatNativeClient(opener=opener)

    with pytest.raises(WeChatNativeError, match="NOAUTH"):
        client.create_native_order(
            merchant=merchant_config,
            deployment=deployment,
            out_trade_no="OUT_NOAUTH",
            description="d",
            amount_fen=100,
        )


# --- Shared certificate cache -------------------------------------------------


def test_default_certificate_manager_is_shared_process_wide() -> None:
    assert default_certificate_manager() is default_certificate_manager()


def test_client_without_an_injected_opener_uses_the_shared_manager() -> None:
    """Production clients are built per call, so their cache must not be per call."""
    assert WeChatNativeClient()._cert_manager is default_certificate_manager()
    # An injected opener must still serve the certificate download it stubs.
    stubbed = WeChatNativeClient(opener=_RouteOpener({}))
    assert stubbed._cert_manager is not default_certificate_manager()


def test_concurrent_cold_cache_reads_download_the_certificates_once(
    platform_cert_pem: bytes, platform_serial: str, merchant_config: WeChatMerchantConfig
) -> None:
    """Without serialization a callback burst becomes a /v3/certificates burst."""
    downloads = 0

    def certificates(_request: Request) -> _MockResponse:
        nonlocal downloads
        downloads += 1
        time.sleep(0.05)  # widen the window every racing thread would slip through
        return _certificates_response(API_V3_KEY, platform_cert_pem, platform_serial)

    manager = PlatformCertificateManager(opener=_RouteOpener({CERTIFICATES_PATH: certificates}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: manager.get_certificate(merchant_config, platform_serial), range(8))
        )

    assert downloads == 1
    assert {certificate.serial_no for certificate in results} == {platform_serial}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
