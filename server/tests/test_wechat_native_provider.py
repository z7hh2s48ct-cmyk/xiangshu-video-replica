"""Test suite for WeChatNativeProvider (PaymentProvider Protocol conformance).

TDD cycle 3: exercises the provider's generic-type mapping, error translation,
deployment config, unsupported form flow, fail-closed notification handling, and
registry wiring. Uses an injected fake client so no network or database is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.payment_provider import (
    DeploymentConfig,
    MerchantConfig,
    OrderQueryError,
    OrderQueryResult,
    PaymentCodeError,
    PaymentCodeResult,
    PaymentProviderError,
    get_payment_provider,
    list_registered_providers,
)
from app.wechat_native_client import (
    WeChatNativeError,
    WeChatNativeOrderResult,
    WeChatOrderQueryResult,
)
from app.wechat_native_provider import WeChatNativeProvider

API_V3_KEY = "0123456789abcdef0123456789abcdef"


class _FakeNativeClient:
    """Duck-typed stand-in for WeChatNativeClient; records calls, returns canned data."""

    def __init__(
        self,
        *,
        order_result: WeChatNativeOrderResult | None = None,
        query_result: WeChatOrderQueryResult | None = None,
        order_error: WeChatNativeError | None = None,
        query_error: WeChatNativeError | None = None,
    ) -> None:
        self._order_result = order_result
        self._query_result = query_result
        self._order_error = order_error
        self._query_error = query_error
        self.order_calls: list[dict[str, object]] = []
        self.query_calls: list[dict[str, object]] = []

    def create_native_order(self, **kwargs: object) -> WeChatNativeOrderResult:
        self.order_calls.append(kwargs)
        if self._order_error is not None:
            raise self._order_error
        assert self._order_result is not None
        return self._order_result

    def query_order(self, **kwargs: object) -> WeChatOrderQueryResult:
        self.query_calls.append(kwargs)
        if self._query_error is not None:
            raise self._query_error
        assert self._query_result is not None
        return self._query_result


@pytest.fixture
def generic_merchant() -> MerchantConfig:
    return MerchantConfig(
        provider="wechat_native",
        raw={
            "appid": "wx8123456789",
            "mchid": "1900000001",
            "serial_no": "MERCHANTSERIAL0001",
            "api_v3_key": API_V3_KEY,
            "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        },
        allowed_channels=("wxpay",),
    )


@pytest.fixture
def generic_deployment() -> DeploymentConfig:
    return DeploymentConfig(
        notify_url="https://video.example.com/api/payments/wechat_native/notify",
        return_url="",
        gateway_url="",
    )


def test_provider_name_is_wechat_native() -> None:
    assert WeChatNativeProvider().name == "wechat_native"


def test_provider_exposes_all_protocol_methods() -> None:
    provider = WeChatNativeProvider()
    for attribute in (
        "name",
        "load_merchant_config",
        "load_deployment_config",
        "create_payment_form",
        "create_payment_code",
        "query_order",
        "verify_notification",
    ):
        assert hasattr(provider, attribute), f"missing protocol member: {attribute}"


def test_load_deployment_config_maps_notify_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://video.example.com")
    deployment = WeChatNativeProvider().load_deployment_config()
    assert deployment.notify_url == "https://video.example.com/api/payments/wechat_native/notify"
    assert deployment.return_url == ""
    assert deployment.gateway_url == ""


def test_load_deployment_config_rejects_missing_public_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    with pytest.raises(ValueError):
        WeChatNativeProvider().load_deployment_config()


def test_create_payment_form_is_unsupported(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    provider = WeChatNativeProvider()
    with pytest.raises(PaymentProviderError):
        provider.create_payment_form(
            merchant_order_no="OUT_FORM",
            amount_fen=100,
            credits=1,
            merchant=generic_merchant,
            deployment=generic_deployment,
        )


def test_create_payment_code_maps_code_url_to_payment_and_qr_fields(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    order_result = WeChatNativeOrderResult(
        code_url="weixin://wxpay/bizpayurl?pr=ABC123", response_digest="digest"
    )
    fake = _FakeNativeClient(order_result=order_result)
    provider = WeChatNativeProvider(client=fake)
    result = provider.create_payment_code(
        merchant=generic_merchant,
        deployment=generic_deployment,
        merchant_order_no="OUT_MAP_001",
        amount_fen=1000,
        credits=10,
        client_ip="203.0.113.7",
    )
    assert isinstance(result, PaymentCodeResult)
    assert result.payment_url == "weixin://wxpay/bizpayurl?pr=ABC123"
    assert result.qr_image_url == "weixin://wxpay/bizpayurl?pr=ABC123"
    assert result.provider_order_no is None
    call = fake.order_calls[0]
    assert call["out_trade_no"] == "OUT_MAP_001"
    assert call["amount_fen"] == 1000
    assert call["client_ip"] == "203.0.113.7"
    assert "10" in str(call["description"])


def _place_order(
    merchant: MerchantConfig, deployment: DeploymentConfig, **extra: object
) -> dict[str, object]:
    """Place one order through the provider and return the client call it made."""
    fake = _FakeNativeClient(
        order_result=WeChatNativeOrderResult(
            code_url="weixin://wxpay/bizpayurl?pr=EXPIRY", response_digest="digest"
        )
    )
    WeChatNativeProvider(client=fake).create_payment_code(
        merchant=merchant,
        deployment=deployment,
        merchant_order_no="OUT_EXPIRY_001",
        amount_fen=1000,
        credits=10,
        client_ip="203.0.113.7",
        **extra,  # type: ignore[arg-type]
    )
    return fake.order_calls[0]


def test_create_payment_code_passes_the_deadline_as_rfc3339_time_expire(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    """WeChat must retire the order at the same instant the expiry sweep does."""
    deadline = datetime(2026, 9, 21, 12, 30, 0, tzinfo=timezone(timedelta(hours=8)))

    call = _place_order(generic_merchant, generic_deployment, expires_at=deadline)

    assert call["time_expire"] == "2026-09-21T12:30:00+08:00"


def test_create_payment_code_without_a_deadline_sends_none(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    assert _place_order(generic_merchant, generic_deployment)["time_expire"] is None


def test_create_payment_code_refuses_a_naive_deadline(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    """An unqualified offset would move the deadline by hours; refuse instead of guessing."""
    with pytest.raises(PaymentCodeError):
        _place_order(
            generic_merchant, generic_deployment, expires_at=datetime(2026, 9, 21, 12, 30, 0)
        )


def test_create_payment_code_translates_wechat_error(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    fake = _FakeNativeClient(order_error=WeChatNativeError("boom", status_code=502))
    provider = WeChatNativeProvider(client=fake)
    with pytest.raises(PaymentCodeError) as exc_info:
        provider.create_payment_code(
            merchant=generic_merchant,
            deployment=generic_deployment,
            merchant_order_no="OUT_ERR",
            amount_fen=100,
            credits=1,
            client_ip="203.0.113.7",
        )
    assert exc_info.value.status_code == 502


def test_query_order_maps_paid_result(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    query_result = WeChatOrderQueryResult(
        trade_state="SUCCESS",
        paid=True,
        out_trade_no="OUT_Q_MAP",
        transaction_id="4200001234",
        amount_fen=1000,
        response_digest="digest64",
    )
    fake = _FakeNativeClient(query_result=query_result)
    provider = WeChatNativeProvider(client=fake)
    result = provider.query_order(
        merchant=generic_merchant,
        deployment=generic_deployment,
        merchant_order_no="OUT_Q_MAP",
    )
    assert isinstance(result, OrderQueryResult)
    assert result.paid is True
    assert result.merchant_order_no == "OUT_Q_MAP"
    assert result.provider_trade_no == "4200001234"
    assert result.amount_fen == 1000
    assert result.channel == "wxpay"
    assert result.response_digest == "digest64"


def test_query_order_maps_unpaid_result(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    query_result = WeChatOrderQueryResult(
        trade_state="NOTPAY",
        paid=False,
        out_trade_no="OUT_Q_UNPAID",
        transaction_id=None,
        amount_fen=None,
        response_digest="digest64",
    )
    fake = _FakeNativeClient(query_result=query_result)
    provider = WeChatNativeProvider(client=fake)
    result = provider.query_order(
        merchant=generic_merchant,
        deployment=generic_deployment,
        merchant_order_no="OUT_Q_UNPAID",
    )
    assert result.paid is False
    assert result.provider_trade_no is None
    assert result.amount_fen is None
    assert result.channel == "wxpay"


def test_query_order_translates_wechat_error(
    generic_merchant: MerchantConfig, generic_deployment: DeploymentConfig
) -> None:
    fake = _FakeNativeClient(query_error=WeChatNativeError("timeout", status_code=504))
    provider = WeChatNativeProvider(client=fake)
    with pytest.raises(OrderQueryError) as exc_info:
        provider.query_order(
            merchant=generic_merchant,
            deployment=generic_deployment,
            merchant_order_no="OUT_Q_ERR",
        )
    assert exc_info.value.status_code == 504


def test_verify_notification_fails_closed(generic_merchant: MerchantConfig) -> None:
    verification = WeChatNativeProvider().verify_notification({"any": "params"}, generic_merchant)
    assert verification.valid is False
    assert verification.error_code == "WECHAT_NATIVE_CALLBACK_UNSUPPORTED"
    assert verification.merchant_order_no is None


def test_provider_is_registered_in_registry() -> None:
    assert "wechat_native" in list_registered_providers()
    provider = get_payment_provider("wechat_native")
    assert isinstance(provider, WeChatNativeProvider)
    assert provider.name == "wechat_native"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
