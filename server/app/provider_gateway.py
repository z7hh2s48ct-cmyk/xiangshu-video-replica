"""Provider Gateway - Centralized billing and supplier management.

This module implements a "Billing Gateway" pattern that allows:
1. Unified API key management across all providers
2. Automatic cost deduction from user wallets
3. Transparent provider switching without code changes
4. Per-user billing configurations (custom pricing tiers)

Core concept: All external API calls go through this gateway, which:
- Routes to the appropriate provider based on configuration
- Tracks usage and charges users in real-time
- Supports failover between providers
- Maintains per-user pricing policies
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.db_portable import BusinessConnection
from app.settings import SettingsRepository

logger = logging.getLogger(__name__)


class BillingMode(Enum):
    """Billing modes for provider gateways."""

    # Direct billing: User pays provider directly via our platform
    DIRECT = "direct"

    # Reseller mode: We pay provider, then charge users at custom rates
    RESALE = "resale"

    # Internal only: No external charges (e.g., testing/dev)
    INTERNAL = "internal"


@dataclass
class ProviderEndpoint:
    """Represents an external provider's API endpoint."""

    name: str  # e.g., "tikhub", "metaso"
    base_url: str
    api_key: str | None
    secret_key: str | None = None
    custom_headers: dict[str, str] | None = None
    timeout_seconds: float = 30.0
    retry_count: int = 3


@dataclass
class UsageRecord:
    """Tracks API usage for billing purposes."""

    user_id: str
    provider: str
    endpoint: str
    units_consumed: int
    cost_credits: int
    timestamp: str

    # Optional metadata for debugging
    request_id: str | None = None
    error_code: str | None = None


class ProviderGatewayError(RuntimeError):
    """Base exception for provider gateway errors."""

    pass


class ProviderUnavailableError(ProviderGatewayError):
    """Provider is currently unavailable."""

    pass


class InsufficientCreditsError(ProviderGatewayError):
    """User lacks sufficient balance for the operation."""

    pass


class ProviderGateway:
    """Central gateway for routing and billing all provider calls.

    This class acts as a single point of entry for all external API calls.
    It handles:
    1. Provider selection and routing
    2. Real-time cost deduction
    3. Failover between providers
    4. Usage tracking and analytics

    Usage example:
        gateway = ProviderGateway(conn, user_id="user-123")

        # Make a viral search call (automatically billed)
        response = gateway.request("tikhub", "douyin_search", {
            "keyword": "别墅",
            "category": ""
        })

        # The gateway will:
        # 1. Load tikhub credentials from database
        # 2. Check user wallet balance
        # 3. Make the API call to TikHub
        # 4. Record usage and deduct credits if successful
    """

    def __init__(
        self,
        conn: BusinessConnection,
        user_id: str | None = None,
        billing_mode: BillingMode = BillingMode.RESALE,
    ):
        self.conn = conn
        self.user_id = user_id
        self.billing_mode = billing_mode
        self.settings_repo = SettingsRepository(conn)

        # Cache for provider configs (invalidate on each request for consistency)
        self._config_cache: dict[str, ProviderEndpoint] = {}

    def request(
        self,
        provider_name: str,
        endpoint: str,
        payload: dict[str, Any],
        *,
        expected_units: int = 1,
        custom_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make an external API call through the gateway with automatic billing.

        Args:
            provider_name: The provider identifier (e.g., "tikhub", "metaso")
            endpoint: The endpoint path (e.g., "douyin_search", "video_generation")
            payload: The request payload
            expected_units: Expected billing units (default: 1)
            custom_headers: Optional additional headers

        Returns:
            The API response as a dictionary

        Raises:
            ProviderUnavailableError: If the provider is not configured or unavailable
            InsufficientCreditsError: If the user lacks sufficient balance
            HTTPException: On API failures (4xx/5xx responses)
        """

        # 1. Load provider configuration
        provider = self._get_provider_endpoint(provider_name)
        if not provider:
            raise ProviderUnavailableError(f"Provider '{provider_name}' is not configured")

        # 2. Pre-check billing (for resale mode)
        if self.billing_mode == BillingMode.RESALE and self.user_id:
            self._pre_check_balance(expected_units)

        # 3. Make the actual API call
        try:
            response = self._make_api_call(
                provider=provider,
                endpoint=endpoint,
                payload=payload,
                custom_headers=custom_headers,
            )

            # 4. Record usage and charge (only for successful calls)
            if self.billing_mode != BillingMode.INTERNAL and self.user_id:
                cost = self._calculate_cost(expected_units, provider)
                self._record_usage_and_charge(
                    provider=provider.name,
                    endpoint=endpoint,
                    units=expected_units,
                    cost=cost,
                )

            return response

        except Exception as exc:
            # Record failed attempt for billing reconciliation
            if self.billing_mode != BillingMode.INTERNAL and self.user_id:
                logger.warning(
                    "API call failed, recording failed attempt for reconciliation",
                    extra={
                        "provider": provider_name,
                        "endpoint": endpoint,
                        "error": str(exc),
                    },
                )
                self._record_failed_attempt(
                    provider=provider_name,
                    endpoint=endpoint,
                    error=str(exc),
                )
            raise

    def _get_provider_endpoint(self, provider_name: str) -> ProviderEndpoint | None:
        """Load provider configuration from database cache."""
        if provider_name in self._config_cache:
            return self._config_cache[provider_name]

        try:
            config = self.settings_repo.load_provider_config(provider_name)
        except Exception as exc:
            logger.error("Failed to load provider config: %s", exc)
            return None

        # Build ProviderEndpoint object
        endpoint = ProviderEndpoint(
            name=provider_name,
            base_url=self._get_base_url(provider_name, config),
            api_key=config.get("api_key"),
            secret_key=config.get("secret_access_key"),  # For COS
            custom_headers=self._build_custom_headers(provider_name, config),
        )

        # Cache for this request cycle
        self._config_cache[provider_name] = endpoint
        return endpoint

    def _get_base_url(self, provider_name: str, config: dict[str, str]) -> str:
        """Get base URL for the provider."""
        urls = {
            "tikhub": "https://api.tikhub.io",
            "metaso": "https://metaso.cn",
            "hifly": "https://api.hifly.ai",
            "deepseek": "https://api.deepseek.com",
            "dashscope": "https://dashscope.aliyuncs.com",
            "douyidou": "https://api.douyidou.com",
            "cos": "https://cos.ap-guangzhou.myqcloud.com",
            "apilio": "https://api.apilio.dev",
        }
        return urls.get(provider_name, config.get("base_url", ""))

    def _build_custom_headers(self, provider_name: str, config: dict[str, str]) -> dict[str, str]:
        """Build custom headers for the provider."""
        headers = {}

        # Add authentication headers based on provider requirements
        if provider_name == "tikhub" and config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"
        elif provider_name == "hifly" and config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"

        return headers

    def _pre_check_balance(self, expected_units: int) -> None:
        """Pre-check if user has sufficient balance before making API call."""
        if not self.user_id:
            return

        # Get estimated cost per unit for this provider
        cost_per_unit = self._get_estimated_cost_per_unit()
        total_needed = cost_per_unit * expected_units

        # Query user wallet
        row = self.conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s FOR SHARE",
            (self.user_id,),
        ).fetchone()

        if not row:
            raise InsufficientCreditsError("User wallet not found")

        if row["available_credits"] < total_needed:
            raise InsufficientCreditsError(
                f"Insufficient balance: required {total_needed}, "
                f"available {row['available_credits']}"
            )

    def _calculate_cost(self, units: int, provider: ProviderEndpoint) -> int:
        """Calculate cost in credits for the given number of units."""
        # Default: 1 unit = 1 credit (can be overridden by per-provider rates)
        return units

    def _get_estimated_cost_per_unit(self) -> int:
        """Get estimated cost per unit for this provider."""
        # Can be customized per provider type
        # Default to internal_base_unit_price_fen / 1000 (convert fen to credits)
        return 1  # Simplified for now

    def _make_api_call(
        self,
        provider: ProviderEndpoint,
        endpoint: str,
        payload: dict[str, Any],
        custom_headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Make the actual HTTP request to the provider."""
        import json
        import urllib.request
        from urllib.error import HTTPError, URLError

        url = f"{provider.base_url}/api/v1/{endpoint}"

        headers = {
            "Content-Type": "application/json",
            **(provider.custom_headers or {}),
            **(custom_headers or {}),
        }

        # Add API key if not already in headers
        if provider.api_key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:
            request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method="POST",
            )

            with urllib.request.urlopen(request, timeout=provider.timeout_seconds) as response:
                content = response.read()
                decoded: dict[str, Any] = json.loads(content.decode("utf-8"))
                return decoded

        except HTTPError as exc:
            raise ProviderGatewayError(f"API call failed: {exc.code} {exc.reason}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ProviderGatewayError(f"Network error: {type(exc).__name__}") from exc

    def _record_usage_and_charge(self, provider: str, endpoint: str, units: int, cost: int) -> None:
        """Record usage and charge user's wallet."""
        if not self.user_id:
            return

        # Deduct credits from wallet
        self.conn.execute(
            """UPDATE wallets 
               SET available_credits = available_credits - %s,
                   updated_at = CURRENT_TIMESTAMP
               WHERE user_id = %s AND available_credits >= %s""",
            (cost, self.user_id, cost),
        )

        # Record transaction for audit trail
        self.conn.execute(
            """INSERT INTO wallet_transactions (
                 user_id, type, available_delta, description, task_id, created_at
             ) VALUES (%s, 'DEBIT', %s, %s, NULL, CURRENT_TIMESTAMP)""",
            (self.user_id, -cost, f"{provider}:{endpoint} ({units} units)"),
        )

        logger.info(
            "Charged user %s %d credits for %s:%s",
            self.user_id,
            cost,
            provider,
            endpoint,
        )

    def _record_failed_attempt(self, provider: str, endpoint: str, error: str) -> None:
        """Record a failed API attempt for reconciliation purposes."""
        if not self.user_id:
            return

        # Failed attempts should NOT charge users, but we track them for auditing
        self.conn.execute(
            """INSERT INTO wallet_transactions (
                 user_id, type, available_delta, description, task_id, created_at
             ) VALUES (%s, 'ATTEMPT_FAILED', 0, %s, NULL, CURRENT_TIMESTAMP)""",
            (self.user_id, f"{provider}:{endpoint} - {error}"),
        )

    def switch_provider(self, provider_name: str, new_config: dict[str, str]) -> bool:
        """Dynamically switch to a different provider configuration.

        This allows runtime updates without restarting the service.

        Example:
            gateway.switch_provider("tikhub", {
                "api_key": "new-api-key-from-database"
            })
        """
        try:
            self.settings_repo.save_provider_config(
                provider=provider_name,
                config=new_config,
                actor_user_id="system-admin",  # Or auto-generated admin ID
            )
            # Invalidate cache
            self._config_cache.pop(provider_name, None)
            return True
        except Exception as exc:
            logger.error("Failed to switch provider: %s", exc)
            return False

    def list_available_providers(self) -> list[str]:
        """List all configured providers."""
        from app.settings import REQUIRED_PROVIDER_FIELDS

        return list(REQUIRED_PROVIDER_FIELDS.keys())

    def get_provider_status(self, provider_name: str) -> dict[str, Any]:
        """Get status and health check for a provider."""
        provider = self._get_provider_endpoint(provider_name)

        if not provider:
            return {"configured": False, "available": False}

        return {
            "configured": True,
            "available": bool(provider.api_key),
            "base_url": provider.base_url,
            "has_custom_headers": bool(provider.custom_headers),
        }


# Convenience functions for backward compatibility
