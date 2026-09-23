"""Payment provider abstraction layer.

Defines the PaymentProvider Protocol that all payment gateways must implement,
along with generic data types and a registry for provider lookup.

CW-066: This module extracts the payment gateway interface from ZPay-specific
code, enabling multi-provider support (WeChat Pay, etc.) without changing
route logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.db_portable import BusinessConnection

# ---------------------------------------------------------------------------
# Generic data types (provider-agnostic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MerchantConfig:
    """Generic merchant configuration loaded from provider_settings."""

    provider: str
    raw: dict[str, str]
    allowed_channels: tuple[str, ...] = ()

    @property
    def primary_channel(self) -> str:
        """First allowed channel (for form-based providers)."""
        return self.allowed_channels[0] if self.allowed_channels else ""


@dataclass(frozen=True)
class DeploymentConfig:
    """Generic deployment configuration (URLs derived from environment)."""

    notify_url: str
    return_url: str
    gateway_url: str = ""  # For form-based providers (e.g., ZPay)


@dataclass(frozen=True)
class PaymentFormResult:
    """Result of creating a payment form for gateway submission."""

    gateway_url: str
    method: str
    form_fields: dict[str, str]


@dataclass(frozen=True)
class PaymentCodeResult:
    """Result of creating a payment code/QR for display."""

    qr_image_url: str
    payment_url: str
    provider_order_no: str | None


@dataclass(frozen=True)
class OrderQueryResult:
    """Result of querying order status from the payment provider."""

    paid: bool
    merchant_order_no: str
    provider_trade_no: str | None
    amount_fen: int | None
    channel: str | None
    response_digest: str


@dataclass(frozen=True)
class NotificationVerification:
    """Result of verifying a payment notification/callback."""

    valid: bool
    merchant_order_no: str | None
    provider_trade_no: str | None
    amount_fen: int | None
    channel: str | None
    source_digest: str | None
    error_code: str | None = None


# ---------------------------------------------------------------------------
# PaymentProvider Protocol
# ---------------------------------------------------------------------------


class PaymentProvider(Protocol):
    """Protocol that all payment providers must implement.

    Each provider encapsulates:
    - Merchant configuration loading (from provider_settings)
    - Deployment configuration (URLs from environment)
    - Payment form/code creation
    - Order status querying
    - Notification/callback verification
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g., 'zpay', 'wechat_native')."""
        ...

    def load_merchant_config(self, conn: BusinessConnection) -> MerchantConfig:
        """Load merchant configuration from the database.

        Raises ValueError if configuration is incomplete or invalid.
        """
        ...

    def load_deployment_config(self) -> DeploymentConfig:
        """Load deployment configuration from environment variables.

        Raises ValueError if PUBLIC_BASE_URL is not properly configured.
        """
        ...

    def create_payment_form(
        self,
        *,
        merchant_order_no: str,
        amount_fen: int,
        credits: int,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
    ) -> PaymentFormResult:
        """Create payment form for gateway submission.

        Returns PaymentFormResult with gateway URL, HTTP method, and form fields.
        """
        ...

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
        """Create a payment code/QR for display to the customer.

        ``expires_at`` is the instant the caller considers the order dead. A
        provider that can tell its gateway (WeChat Native's ``time_expire``)
        should pass it on so both sides retire the order together; a provider
        with no such field ignores it.

        Raises PaymentProviderError on failure.
        """
        ...

    def query_order(
        self,
        *,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
        merchant_order_no: str,
    ) -> OrderQueryResult:
        """Query order status from the payment provider.

        Raises PaymentProviderError on failure.
        """
        ...

    def verify_notification(
        self,
        params: Mapping[str, str],
        merchant: MerchantConfig,
    ) -> NotificationVerification:
        """Verify a payment notification/callback from the provider.

        Returns NotificationVerification with valid=True if the notification
        is authentic and contains valid payment data.
        """
        ...


# ---------------------------------------------------------------------------
# Provider errors
# ---------------------------------------------------------------------------


class PaymentProviderError(RuntimeError):
    """Base error for payment provider operations."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class PaymentCodeError(PaymentProviderError):
    """Error creating a payment code."""

    pass


class OrderQueryError(PaymentProviderError):
    """Error querying order status."""

    pass


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDER_REGISTRY: dict[str, type[PaymentProvider]] = {}


def register_provider(name: str, provider_class: type[PaymentProvider]) -> None:
    """Register a payment provider class by name.

    Called at module import time by provider implementations.
    """
    _PROVIDER_REGISTRY[name] = provider_class


def get_payment_provider(name: str) -> PaymentProvider:
    """Get a payment provider instance by name.

    Raises KeyError if the provider is not registered.
    """
    provider_class = _PROVIDER_REGISTRY.get(name)
    if provider_class is None:
        raise KeyError(f"Unknown payment provider: {name}")
    return provider_class()


def list_registered_providers() -> tuple[str, ...]:
    """List all registered provider names."""
    return tuple(sorted(_PROVIDER_REGISTRY.keys()))
