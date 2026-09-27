"""WeChat Pay V3 Native payment provider implementation.

Adapts the WeChatNativeClient (V3 Native HTTP client) to the generic
PaymentProvider Protocol so routes can select WeChat Native through the provider
registry without WeChat-specific branching.

CW-069: request signing, platform-certificate management, and response signature
verification live in wechat_native_client; this module only maps the generic data
types, translates errors, and registers the provider. WeChat Native is QR/code
based, so create_payment_code is the supported flow while create_payment_form
(gateway redirect) is intentionally unsupported.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.db_portable import BusinessConnection
from app.payment_provider import (
    DeploymentConfig,
    MerchantConfig,
    NotificationVerification,
    OrderQueryError,
    OrderQueryResult,
    PaymentCodeError,
    PaymentCodeResult,
    PaymentFormResult,
    PaymentProvider,
    PaymentProviderError,
    register_provider,
)
from app.settings import SettingsRepository
from app.wechat_native_client import (
    CertificateSource,
    WeChatDeploymentConfig,
    WeChatMerchantConfig,
    WeChatNativeClient,
    WeChatNativeError,
    build_response_verify_message,
    decrypt_aes_256_gcm,
    default_certificate_manager,
    deployment_config_from_environment,
    merchant_config_from_settings,
    resolve_verification_key,
    verify_sha256_rsa,
)

WECHAT_NATIVE_PROVIDER_NAME = "wechat_native"
WECHAT_NATIVE_CHANNEL = "wxpay"
WECHAT_NATIVE_CALLBACK_UNSUPPORTED = "WECHAT_NATIVE_CALLBACK_UNSUPPORTED"

# Raw-body callback verification outcomes (CW-070). These are stable error codes the
# notify route maps onto the WeChat JSON acknowledgement shape.
WECHAT_CALLBACK_MISSING_HEADERS = "WECHAT_CALLBACK_MISSING_HEADERS"
WECHAT_CALLBACK_CONFIG_INVALID = "WECHAT_CALLBACK_CONFIG_INVALID"
WECHAT_CALLBACK_CERT_UNAVAILABLE = "WECHAT_CALLBACK_CERT_UNAVAILABLE"
WECHAT_CALLBACK_BODY_INVALID = "WECHAT_CALLBACK_BODY_INVALID"
WECHAT_CALLBACK_SIGNATURE_INVALID = "WECHAT_CALLBACK_SIGNATURE_INVALID"
WECHAT_CALLBACK_RESOURCE_INVALID = "WECHAT_CALLBACK_RESOURCE_INVALID"
WECHAT_CALLBACK_DECRYPT_FAILED = "WECHAT_CALLBACK_DECRYPT_FAILED"
WECHAT_CALLBACK_PAYLOAD_INVALID = "WECHAT_CALLBACK_PAYLOAD_INVALID"
WECHAT_CALLBACK_STALE_TIMESTAMP = "WECHAT_CALLBACK_STALE_TIMESTAMP"
WECHAT_CALLBACK_MERCHANT_MISMATCH = "WECHAT_CALLBACK_MERCHANT_MISMATCH"

# WeChat's own guidance: a notification older than five minutes is treated as a
# replay and refused without any certificate work. The signature covers the
# timestamp, so this gate only ever refuses what a retry (or a replay) looks
# like — a skewed local clock shows up as every callback failing with this code.
WECHAT_NOTIFY_FRESHNESS_SECONDS = 5 * 60


@dataclass(frozen=True)
class WeChatNotificationResult:
    """Outcome of raw-body WeChat Native callback verification.

    ``authenticated`` is True only when the signature verified against the platform
    certificate AND the resource decrypted with the api_v3_key. ``provider_trade_no``
    carries the WeChat ``transaction_id`` (the settlement trade reference for the
    wechat_native spec). ``error_code`` is None on a clean, settleable notification;
    an authentic-but-unusable payload keeps ``authenticated`` True while naming the
    ``WECHAT_CALLBACK_*`` reason so the route can ACK vs FAIL correctly.
    """

    authenticated: bool
    trade_state: str | None = None
    merchant_order_no: str | None = None
    provider_trade_no: str | None = None
    amount_fen: int | None = None
    channel: str | None = None
    source_digest: str | None = None
    error_code: str | None = None


class WeChatNativeProvider(PaymentProvider):
    """WeChat Pay V3 Native provider conforming to the PaymentProvider Protocol.

    Delegates order placement and querying to WeChatNativeClient. The client may be
    injected for testing; when omitted (as when constructed by the registry) a
    default client using the real urllib opener is created lazily per call.
    """

    def __init__(
        self,
        *,
        client: WeChatNativeClient | None = None,
        cert_manager: CertificateSource | None = None,
    ) -> None:
        self._client = client
        # Provider-level certificate manager for raw callback verification. Injected in
        # tests (a stub with a canned self-signed platform cert); defaults to the
        # process-wide downloader. The registry builds a fresh provider per request, so
        # a per-instance manager would re-download the certificates on every callback.
        self._cert_manager: CertificateSource = cert_manager or default_certificate_manager()

    @property
    def name(self) -> str:
        return WECHAT_NATIVE_PROVIDER_NAME

    def load_merchant_config(self, conn: BusinessConnection) -> MerchantConfig:
        """Load and validate WeChat Native merchant config from provider_settings."""
        raw = SettingsRepository(conn).load_wechat_native_config()
        # Validate eagerly so incomplete settings raise ValueError per the Protocol.
        merchant_config_from_settings(raw)
        return MerchantConfig(
            provider=WECHAT_NATIVE_PROVIDER_NAME,
            raw=raw,
            allowed_channels=(WECHAT_NATIVE_CHANNEL,),
        )

    def load_deployment_config(self) -> DeploymentConfig:
        """Derive deployment config from PUBLIC_BASE_URL (WeChat Native needs only notify_url)."""
        deployment = deployment_config_from_environment()
        return DeploymentConfig(
            notify_url=deployment.notify_url,
            return_url="",
            gateway_url="",
        )

    def create_payment_form(
        self,
        *,
        merchant_order_no: str,
        amount_fen: int,
        credits: int,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
    ) -> PaymentFormResult:
        """WeChat Native is code/QR based; the form-redirect flow is unsupported."""
        raise PaymentProviderError(
            "WeChat Native does not support form-based payment; use create_payment_code"
        )

    def create_payment_code(
        self,
        *,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
        merchant_order_no: str,
        amount_fen: int,
        credits: int,
        client_ip: str,
        expires_at: datetime | None = None,
    ) -> PaymentCodeResult:
        """Place a Native order and surface the weixin:// code_url for QR rendering.

        ``expires_at`` becomes WeChat's ``time_expire`` so the gateway retires the
        order at the same instant the local expiry sweep does; a naive datetime is
        rejected rather than guessed at, because an unqualified offset would move
        the deadline by hours.
        """
        wechat_merchant = merchant_config_from_settings(merchant.raw)
        wechat_deployment = WeChatDeploymentConfig(notify_url=deployment.notify_url)
        client = self._client or WeChatNativeClient()
        if expires_at is not None and expires_at.tzinfo is None:
            raise PaymentCodeError("WeChat native order expiry must carry a timezone")
        time_expire = expires_at.isoformat(timespec="seconds") if expires_at else None
        try:
            result = client.create_native_order(
                merchant=wechat_merchant,
                deployment=wechat_deployment,
                out_trade_no=merchant_order_no,
                description=f"众墅之家积分充值 {credits} 积分",
                amount_fen=amount_fen,
                client_ip=client_ip,
                time_expire=time_expire,
            )
        except WeChatNativeError as exc:
            raise PaymentCodeError(str(exc), status_code=exc.status_code) from exc
        # WeChat returns only code_url (the QR content); there is no hosted image and
        # no order number at creation time, so both QR fields carry the code_url.
        return PaymentCodeResult(
            qr_image_url=result.code_url,
            payment_url=result.code_url,
            provider_order_no=None,
        )

    def query_order(
        self,
        *,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
        merchant_order_no: str,
    ) -> OrderQueryResult:
        """Query an order by out-trade-no and normalize it into the generic result."""
        wechat_merchant = merchant_config_from_settings(merchant.raw)
        client = self._client or WeChatNativeClient()
        try:
            result = client.query_order(merchant=wechat_merchant, out_trade_no=merchant_order_no)
        except WeChatNativeError as exc:
            raise OrderQueryError(str(exc), status_code=exc.status_code) from exc
        return OrderQueryResult(
            paid=result.paid,
            merchant_order_no=result.out_trade_no,
            provider_trade_no=result.transaction_id,
            amount_fen=result.amount_fen,
            channel=WECHAT_NATIVE_CHANNEL,
            response_digest=result.response_digest,
        )

    def verify_notification(
        self,
        params: Mapping[str, str],
        merchant: MerchantConfig,
    ) -> NotificationVerification:
        """Fail closed: WeChat callbacks need raw-body + header signature verification.

        A WeChat Native notification is a JSON body whose authenticity depends on the
        Wechatpay-Timestamp/Nonce/Signature/Serial headers verified over the exact raw
        bytes, plus AES-256-GCM decryption of the resource with the api_v3_key. That
        cannot be reconstructed from a flattened Mapping[str, str], so this params-based
        entry point never marks a notification valid. Raw-body callback verification is
        a route-level follow-up task.
        """
        return NotificationVerification(
            valid=False,
            merchant_order_no=None,
            provider_trade_no=None,
            amount_fen=None,
            channel=None,
            source_digest=None,
            error_code=WECHAT_NATIVE_CALLBACK_UNSUPPORTED,
        )

    def verify_notification_raw(
        self,
        *,
        raw_body: bytes,
        timestamp: str | None,
        nonce: str | None,
        signature: str | None,
        serial: str | None,
        merchant: MerchantConfig,
    ) -> WeChatNotificationResult:
        """Verify a raw WeChat Native callback body against its Wechatpay-* headers.

        Mirrors ``WeChatNativeClient._verify_response``: the signature is checked over
        the exact raw bytes (the TIMESTAMP / NONCE / BODY verification message) against
        the platform certificate selected by ``Wechatpay-Serial``, then ``resource`` is
        AES-256-GCM decrypted with the merchant ``api_v3_key`` to recover the
        transaction plaintext. Fail-closed: a bad callback never raises — it returns
        ``authenticated=False`` with a ``WECHAT_CALLBACK_*`` ``error_code``. An
        authentic notification whose plaintext is not a settleable SUCCESS keeps
        ``authenticated=True`` and names the reason so the route can ACK vs FAIL.
        """
        if timestamp is None or nonce is None or signature is None or serial is None:
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_MISSING_HEADERS
            )
        try:
            header_seconds = int(timestamp)
        except ValueError:
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_STALE_TIMESTAMP
            )
        if abs(time.time() - header_seconds) > WECHAT_NOTIFY_FRESHNESS_SECONDS:
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_STALE_TIMESTAMP
            )
        try:
            wechat_merchant = merchant_config_from_settings(merchant.raw)
        except ValueError:
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_CONFIG_INVALID
            )
        try:
            public_key = resolve_verification_key(wechat_merchant, serial, self._cert_manager)
        except WeChatNativeError:
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_CERT_UNAVAILABLE
            )
        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_BODY_INVALID
            )
        message = build_response_verify_message(timestamp=timestamp, nonce=nonce, body=body_text)
        if not verify_sha256_rsa(public_key=public_key, signature_b64=signature, message=message):
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_SIGNATURE_INVALID
            )

        # Signature is valid from here on: the bytes are authentically WeChat's.
        digest = hashlib.sha256(raw_body).hexdigest()
        try:
            envelope = json.loads(body_text)
        except json.JSONDecodeError:
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_BODY_INVALID
            )
        if not isinstance(envelope, dict):
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_BODY_INVALID
            )
        resource = envelope.get("resource")
        if not isinstance(resource, dict):
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_RESOURCE_INVALID
            )
        ciphertext = resource.get("ciphertext")
        resource_nonce = resource.get("nonce")
        if not isinstance(ciphertext, str) or not isinstance(resource_nonce, str):
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_RESOURCE_INVALID
            )
        associated_data = resource.get("associated_data")
        associated = associated_data if isinstance(associated_data, str) else ""
        try:
            plaintext = decrypt_aes_256_gcm(
                api_v3_key=wechat_merchant.api_v3_key,
                nonce=resource_nonce,
                associated_data=associated,
                ciphertext_b64=ciphertext,
            )
        except WeChatNativeError:
            return WeChatNotificationResult(
                authenticated=False, error_code=WECHAT_CALLBACK_DECRYPT_FAILED
            )

        return self._parse_transaction_plaintext(plaintext, digest=digest, merchant=wechat_merchant)

    def _parse_transaction_plaintext(
        self, plaintext: bytes, *, digest: str, merchant: WeChatMerchantConfig
    ) -> WeChatNotificationResult:
        """Parse the decrypted transaction plaintext into a settlement-ready result."""
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return WeChatNotificationResult(
                authenticated=True,
                channel=WECHAT_NATIVE_CHANNEL,
                source_digest=digest,
                error_code=WECHAT_CALLBACK_PAYLOAD_INVALID,
            )
        out_trade_no = payload.get("out_trade_no")
        trade_state = payload.get("trade_state")
        transaction_id = payload.get("transaction_id")
        amount = payload.get("amount")
        amount_fen: int | None = None
        if isinstance(amount, dict):
            total = amount.get("total")
            if isinstance(total, int):
                amount_fen = total
        merchant_order_no = out_trade_no if isinstance(out_trade_no, str) else None
        state = trade_state if isinstance(trade_state, str) else None
        trade_no = transaction_id if isinstance(transaction_id, str) else None
        appid = payload.get("appid")
        mchid = payload.get("mchid")
        if (isinstance(mchid, str) and mchid != merchant.mchid) or (
            isinstance(appid, str) and appid != merchant.appid
        ):
            # The bytes are authentically WeChat's, but they describe a trade of
            # another merchant identity — the replay window around a merchant
            # change. Never settle under this configuration.
            return WeChatNotificationResult(
                authenticated=True,
                trade_state=state,
                merchant_order_no=None,
                provider_trade_no=None,
                amount_fen=None,
                channel=WECHAT_NATIVE_CHANNEL,
                source_digest=digest,
                error_code=WECHAT_CALLBACK_MERCHANT_MISMATCH,
            )
        # A SUCCESS notification must carry the transaction_id (settlement reference)
        # and the amount; anything else is authentic but not settleable.
        incomplete = (
            state is None
            or merchant_order_no is None
            or (state == "SUCCESS" and (trade_no is None or amount_fen is None))
        )
        if incomplete:
            return WeChatNotificationResult(
                authenticated=True,
                trade_state=state,
                merchant_order_no=merchant_order_no,
                provider_trade_no=trade_no,
                amount_fen=amount_fen,
                channel=WECHAT_NATIVE_CHANNEL,
                source_digest=digest,
                error_code=WECHAT_CALLBACK_PAYLOAD_INVALID,
            )
        return WeChatNotificationResult(
            authenticated=True,
            trade_state=state,
            merchant_order_no=merchant_order_no,
            provider_trade_no=trade_no,
            amount_fen=amount_fen,
            channel=WECHAT_NATIVE_CHANNEL,
            source_digest=digest,
        )


# Register WeChatNativeProvider at import time.
register_provider(WECHAT_NATIVE_PROVIDER_NAME, WeChatNativeProvider)
