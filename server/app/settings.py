from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol, cast

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel

from app import content_store
from app.db_portable import BusinessConnection
from app.storage import (
    CloudStorageAdapter,
    CloudStorageConfig,
    StorageAdapter,
    StorageBackendUnavailable,
    cloud_storage_config_from_settings,
    create_storage_adapter,
)
from app.zpay import parse_enabled_channels

ProviderName = Literal[
    "apilio", "metaso", "cos", "deepseek", "hifly", "tikhub", "dashscope", "douyidou", "ses"
]

SETTINGS_KEY_ENV = "VIDEO_REPLICA_SETTINGS_KEY"
ACCEPTANCE_PAYMENT_USER_ID_ENV = "VIDEO_REPLICA_ACCEPTANCE_PAYMENT_USER_ID"
ACCEPTANCE_PAYMENT_AMOUNT_FEN_ENV = "VIDEO_REPLICA_ACCEPTANCE_PAYMENT_AMOUNT_FEN"
MAX_ACCEPTANCE_PAYMENT_FEN = 500
SECRET_FIELDS = (
    "api_key",
    "api_v3_key",
    "private_key",
    "access_key_id",
    "secret",
    "token",
    "password",
    "authorization",
)
REQUIRED_PROVIDER_FIELDS: dict[ProviderName, tuple[str, ...]] = {
    # Apilio can use a dedicated Gemini key while image generation is not configured yet.
    "apilio": (),
    "metaso": ("api_key",),
    "cos": ("access_key_id", "secret_access_key", "bucket", "region"),
    # 二创口播稿改写默认走 DeepSeek；除 API Key 外的参数（base_url/model）
    # 由服务端固定，界面无需暴露。
    "deepseek": ("api_key",),
    # 飞影数字人（C1 数字人口播整链）：Bearer Token 即 api_key，见
    # docs/飞影数字人API-V2-集成参考.md。
    "hifly": ("api_key",),
    # 爆款视频参考库（C4 重启）：抖音/视频号内容搜索与媒体下载，
    # 见 docs/爆款视频TikHub接入-需求理解-2026-09-06.md。
    "tikhub": ("api_key",),
    # 工作台"提取文案"（script-from-audio）：Fun-ASR 语音转写，方案移植自
    # oral-ip-agents-research，见 docs/文案工坊优化-C7草稿库与ASR文案链路设计-2026-09-06.md。
    "dashscope": ("api_key",),
    # 链接解析（C3）：抖音/快手/小红书等去水印与文案提取网关凭据。
    "douyidou": ("app_id", "app_secret"),
    # 邮件推送：客户绑定邮箱与找回密码的验证码。只用模板发信（内地地域不支持
    # 自由正文），模板变量约定见 app.email_delivery。
    "ses": ("secret_id", "secret_key", "from_address", "code_template_id"),
}
DEFAULT_RUNTIME_SETTINGS: dict[str, int | str] = {
    "max_generation_count_per_batch": 4,
    "max_concurrent_h3_tasks": 2,
    "active_storage_provider": "cos",
}
DEFAULT_BILLING_SETTINGS: dict[str, int] = {
    "internal_base_unit_price_fen": 1000,
    "charged_unit_price_fen": 1000,
    "oral_unit_price_fen": 1000,
    "min_recharge_fen": 10000,
    "recharge_step_fen": 1000,
}


class SettingsUnavailableError(RuntimeError):
    pass


class SettingsKeyMissing(SettingsUnavailableError):
    pass


class SettingsKeyInvalid(SettingsUnavailableError):
    pass


class SettingsDecryptError(SettingsUnavailableError):
    pass


class SettingsRepository:
    def __init__(self, conn: BusinessConnection, fernet: Fernet | None = None) -> None:
        self.conn = conn
        self.fernet = fernet or fernet_from_environment()

    def save_provider_config(
        self,
        provider: str,
        config: dict[str, Any],
        *,
        actor_user_id: str,
    ) -> dict[str, Any]:
        provider_name = normalize_provider(provider)
        normalized = normalize_config(config)
        validate_provider_config(provider_name, normalized)
        return self._save_encrypted_config(
            provider_name,
            normalized,
            actor_user_id=actor_user_id,
        )

    def save_zpay_config(
        self,
        config: dict[str, Any],
        *,
        actor_user_id: str,
    ) -> dict[str, Any]:
        normalized = normalize_config(config)
        validate_zpay_config(normalized)
        normalized["enabled_channels"] = ",".join(
            parse_enabled_channels(normalized["enabled_channels"])
        )
        return self._save_encrypted_config(
            "zpay",
            normalized,
            actor_user_id=actor_user_id,
        )

    def _save_encrypted_config(
        self,
        provider: str,
        normalized: dict[str, str],
        *,
        actor_user_id: str,
    ) -> dict[str, Any]:
        encrypted_config = self.fernet.encrypt(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO provider_settings (
                    provider,
                    encrypted_config,
                    updated_by_user_id,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(provider) DO UPDATE SET
                    encrypted_config = excluded.encrypted_config,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (provider, encrypted_config, actor_user_id),
            )

        return self._read_encrypted_config(provider)

    def read_provider_config(self, provider: str) -> dict[str, Any]:
        provider_name = normalize_provider(provider)
        return self._read_encrypted_config(provider_name)

    def read_zpay_config(self) -> dict[str, Any]:
        return self._read_encrypted_config("zpay")

    def _read_encrypted_config(self, provider: str) -> dict[str, Any]:
        config = self._load_encrypted_config(provider)
        return {
            "provider": provider,
            "configured": bool(config),
            "config": mask_config(config),
        }

    def read_all_provider_configs(self) -> dict[str, dict[str, Any]]:
        return {
            provider: self.read_provider_config(provider) for provider in REQUIRED_PROVIDER_FIELDS
        }

    def load_provider_config(self, provider: str) -> dict[str, str]:
        provider_name = normalize_provider(provider)
        return self._load_encrypted_config(provider_name)

    def load_wechat_native_config(self) -> dict[str, str]:
        return self._load_encrypted_config("wechat_native")

    def read_wechat_native_config(self) -> dict[str, Any]:
        config = self.load_wechat_native_config()
        return {
            "provider": "wechat_native",
            "configured": bool(config),
            "config": {
                key: ("********" if value else "")
                if key in {"api_v3_key", "private_key"}
                else value
                for key, value in config.items()
            },
        }

    def save_wechat_native_config(
        self, changes: dict[str, str], *, actor_user_id: str
    ) -> dict[str, Any]:
        from app.wechat_native_client import load_private_key, merchant_config_from_settings

        # public_key_id / public_key 为微信支付公钥模式；二者留空即回到平台证书
        # 模式，所以不像两个密钥那样“留空保留”。
        allowed = {
            "appid",
            "mchid",
            "serial_no",
            "api_v3_key",
            "private_key",
            "public_key_id",
            "public_key",
        }
        if set(changes) - allowed:
            raise ValueError("Unsupported WeChat merchant setting")
        current = self.load_wechat_native_config()
        config = dict(current)
        for key, value in changes.items():
            value = value.strip()
            if key in {"api_v3_key", "private_key"} and not value:
                continue
            config[key] = value
        merchant = merchant_config_from_settings(config)
        load_private_key(merchant.private_key_pem)
        if current and any(config.get(key) != current.get(key) for key in ("appid", "mchid")):
            # Settlement-drain criterion: orders inside their payment window
            # (payable QR) plus WeChat's callback retry horizon (about a day)
            # can still become PAID under the current identity, so a change now
            # would strand them. Past the horizon nothing can land anymore —
            # counting CLOSED forever only deadlocked the merchant identity.
            from app.zpay_payments import count_blocking_native_orders

            blocking = count_blocking_native_orders(self.conn)
            if blocking:
                raise ValueError(
                    f"{blocking} WeChat order(s) are inside their settlement window "
                    "(payment window + callback retry horizon); changing merchant "
                    "identity would strand them. Wait for the window to pass."
                )
        self._save_encrypted_config("wechat_native", config, actor_user_id=actor_user_id)
        return self.read_wechat_native_config()

    def read_active_payment_provider(self) -> str:
        row = self.conn.execute(
            "SELECT active_payment_provider FROM runtime_settings WHERE id=1 FOR SHARE"
        ).fetchone()
        return str(row[0]) if row else "zpay"

    def load_zpay_config(self) -> dict[str, str]:
        return self._load_encrypted_config("zpay")

    def _load_encrypted_config(self, provider: str) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT encrypted_config FROM provider_settings WHERE provider = %s",
            (provider,),
        ).fetchone()
        if row is None:
            return {}

        try:
            raw = self.fernet.decrypt(str(row["encrypted_config"]).encode("ascii"))
        except InvalidToken as exc:
            raise SettingsDecryptError("provider settings cannot be decrypted") from exc

        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise SettingsDecryptError("provider settings payload is invalid")
        return {str(key): str(value) for key, value in decoded.items()}

    def save_runtime_settings(
        self,
        *,
        max_generation_count_per_batch: int,
        max_concurrent_h3_tasks: int,
        active_storage_provider: str,
        actor_user_id: str,
        fair_queue_enabled: bool | None = None,
    ) -> dict[str, int | str]:
        validate_runtime_settings(
            max_generation_count_per_batch=max_generation_count_per_batch,
            max_concurrent_h3_tasks=max_concurrent_h3_tasks,
            active_storage_provider=active_storage_provider,
        )
        # M4/M5 review M2: the fair-queue rollout switch (revised ADR §4)
        # gets an audited write path instead of ad-hoc SQL. The column only
        # exists on PostgreSQL (migration 041); the desktop SQLite lane keeps
        # its legacy global FIFO, so a provided value there is a caller error.
        # ``getattr`` rather than ``is_postgres`` because unit tests inject a
        # raw sqlite3 connection, which is by definition not the PG lane.
        # The saved dict deliberately does NOT echo the switch (PR #68 Codex
        # P2): echoing it would let a stale SettingsPanel round-trip silently
        # disable the queue on an unrelated limits save. The switch is read
        # explicitly via read_fair_queue_enabled / the control-plane route.
        if fair_queue_enabled is not None and not getattr(self.conn, "is_postgres", False):
            raise ValueError("fair_queue_enabled is only available on the PostgreSQL lane")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO runtime_settings (
                    id,
                    max_generation_count_per_batch,
                    max_concurrent_h3_tasks,
                    active_storage_provider,
                    updated_by_user_id,
                    created_at,
                    updated_at
                )
                VALUES (1, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    max_generation_count_per_batch = excluded.max_generation_count_per_batch,
                    max_concurrent_h3_tasks = excluded.max_concurrent_h3_tasks,
                    active_storage_provider = excluded.active_storage_provider,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    max_generation_count_per_batch,
                    max_concurrent_h3_tasks,
                    active_storage_provider,
                    actor_user_id,
                ),
            )
            if fair_queue_enabled is not None:
                # The upsert above guarantees the id=1 row; flip the switch in
                # the same transaction so limits and the queue mode commit
                # (or roll back) together.
                self.conn.execute(
                    """
                    UPDATE runtime_settings
                    SET fair_queue_enabled = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                    """,
                    (fair_queue_enabled,),
                )
        return self.read_runtime_settings()

    def read_fair_queue_enabled(self) -> bool:
        """The fair-queue switch on the PostgreSQL lane; anything else (the
        SQLite lane, a missing settings row) is the documented feature-off
        state (revised ADR §4, mirroring ``_fair_queue_enabled``)."""
        if not getattr(self.conn, "is_postgres", False):
            return False
        row = self.conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
        return bool(row[0]) if row is not None else False

    def read_runtime_settings(self) -> dict[str, int | str]:
        row = self.conn.execute(
            """
            SELECT max_generation_count_per_batch, max_concurrent_h3_tasks, active_storage_provider
            FROM runtime_settings
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return dict(DEFAULT_RUNTIME_SETTINGS)
        return {
            "max_generation_count_per_batch": int(row["max_generation_count_per_batch"]),
            "max_concurrent_h3_tasks": int(row["max_concurrent_h3_tasks"]),
            "active_storage_provider": str(row["active_storage_provider"]),
        }

    def save_billing_settings(
        self,
        *,
        internal_base_unit_price_fen: int,
        oral_unit_price_fen: int,
        min_recharge_fen: int,
        recharge_step_fen: int,
        actor_user_id: str | None,
    ) -> dict[str, int]:
        validate_billing_settings(
            internal_base_unit_price_fen=internal_base_unit_price_fen,
            oral_unit_price_fen=oral_unit_price_fen,
            min_recharge_fen=min_recharge_fen,
            recharge_step_fen=recharge_step_fen,
        )
        with self.conn:
            self.conn.execute(
                """
                UPDATE runtime_settings
                SET internal_base_unit_price_fen = %s,
                    oral_unit_price_fen = %s,
                    min_recharge_fen = %s,
                    recharge_step_fen = %s,
                    updated_by_user_id = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (
                    internal_base_unit_price_fen,
                    oral_unit_price_fen,
                    min_recharge_fen,
                    recharge_step_fen,
                    actor_user_id,
                ),
            )
        return self.read_billing_settings()

    def read_billing_settings(self) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT internal_base_unit_price_fen, oral_unit_price_fen,
                   min_recharge_fen, recharge_step_fen
            FROM runtime_settings
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return dict(DEFAULT_BILLING_SETTINGS)
        base_price = int(row["internal_base_unit_price_fen"])
        return {
            "internal_base_unit_price_fen": base_price,
            "charged_unit_price_fen": base_price,
            "oral_unit_price_fen": int(row["oral_unit_price_fen"]),
            "min_recharge_fen": int(row["min_recharge_fen"]),
            "recharge_step_fen": int(row["recharge_step_fen"]),
        }

    def read_customer_billing_settings(self, *, user_id: str) -> dict[str, int]:
        """Return billing settings with an optional per-customer sale price.

        Customized customers recharge in whole-video increments.  The global
        minimum remains the lower bound and is rounded up to the next whole
        video at that customer's price.
        """
        billing = self.read_billing_settings()
        row = self.conn.execute(
            "SELECT unit_price_fen FROM customer_unit_prices WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if row is None:
            return billing
        return apply_customer_unit_price(billing, unit_price_fen=int(row["unit_price_fen"]))


def fernet_from_environment() -> Fernet:
    return Fernet(settings_encryption_key().encode("ascii"))


def settings_encryption_key() -> str:
    key = os.environ.get(SETTINGS_KEY_ENV)
    if not key:
        # CW-042-b: the desktop OS-keystore fallback is retired with the
        # SQLite lane — the customer server requires the provisioned key.
        raise SettingsKeyMissing(f"{SETTINGS_KEY_ENV} is required")
    try:
        Fernet(key.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise SettingsKeyInvalid("settings encryption key is invalid") from exc
    return key


def normalize_provider(provider: str) -> ProviderName:
    normalized = provider.lower()
    if normalized not in REQUIRED_PROVIDER_FIELDS:
        raise ValueError(f"unsupported provider: {provider}")
    return normalized


def normalize_config(config: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in config.items():
        if value is None:
            continue
        normalized[str(key)] = str(value).strip()
    return normalized


def validate_provider_config(provider: ProviderName, config: dict[str, str]) -> None:
    missing = [field for field in REQUIRED_PROVIDER_FIELDS[provider] if not config.get(field)]
    if missing:
        raise ValueError(f"missing required setting: {', '.join(missing)}")
    if provider == "tikhub":
        _validate_tikhub_backup_channel(config)
    if provider == "ses":
        from app.email_delivery import validate_email_config

        validate_email_config(config)
    if provider == "apilio":
        from app.analysis import parse_analysis_fallback_models

        parse_analysis_fallback_models(config.get("analysis_model_fallbacks"))


def _validate_tikhub_backup_channel(config: dict[str, str]) -> None:
    """爆款数据源备用通道（可选项）：入口必须是 URL，密钥不能单独出现.

    字段名与 ``app.viral_tikhub`` 的备用通道配置一致；在此拒绝错配，避免保存
    后才在采集日志里暴露问题。
    """
    backup_base_url = (config.get("backup_base_url") or "").strip()
    if backup_base_url and not backup_base_url.startswith(("http://", "https://")):
        raise ValueError("备用入口需要以 http:// 或 https:// 开头")
    if (config.get("backup_api_key") or "").strip() and not backup_base_url:
        raise ValueError("备用密钥需要与备用入口一起配置")


def validate_zpay_config(config: dict[str, str]) -> None:
    allowed_fields = {"pid", "key", "enabled_channels"}
    unexpected = sorted(set(config) - allowed_fields)
    if unexpected:
        raise ValueError(f"unsupported ZPay setting: {', '.join(unexpected)}")
    missing = [field for field in allowed_fields if not config.get(field)]
    if missing:
        raise ValueError(f"missing required ZPay setting: {', '.join(sorted(missing))}")
    parse_enabled_channels(config["enabled_channels"])


def validate_runtime_settings(
    *,
    max_generation_count_per_batch: int,
    max_concurrent_h3_tasks: int,
    active_storage_provider: str,
) -> None:
    if max_generation_count_per_batch < 1:
        raise ValueError("max_generation_count_per_batch must be at least 1")
    if max_concurrent_h3_tasks < 1:
        raise ValueError("max_concurrent_h3_tasks must be at least 1")
    if active_storage_provider not in {"cos", "local"}:
        raise ValueError("active_storage_provider must be cos or local")


def validate_billing_settings(
    *,
    internal_base_unit_price_fen: int,
    oral_unit_price_fen: int,
    min_recharge_fen: int,
    recharge_step_fen: int,
) -> None:
    values = (
        internal_base_unit_price_fen,
        oral_unit_price_fen,
        min_recharge_fen,
        recharge_step_fen,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("billing settings must use integer fen values")
    if internal_base_unit_price_fen <= 0:
        raise ValueError("internal_base_unit_price_fen must be positive")
    if oral_unit_price_fen <= 0:
        raise ValueError("oral_unit_price_fen must be positive")
    if min_recharge_fen < 10000:
        raise ValueError("min_recharge_fen must be at least 10000")
    if recharge_step_fen < 1000:
        raise ValueError("recharge_step_fen must be at least 1000")
    if min_recharge_fen % recharge_step_fen != 0:
        raise ValueError("min_recharge_fen must be divisible by recharge_step_fen")
    if recharge_step_fen % internal_base_unit_price_fen != 0:
        raise ValueError("recharge_step_fen must be divisible by internal_base_unit_price_fen")


def apply_customer_unit_price(
    billing: dict[str, int],
    *,
    unit_price_fen: int,
) -> dict[str, int]:
    """Apply a positive per-customer sale price to a global billing snapshot."""
    if isinstance(unit_price_fen, bool) or not isinstance(unit_price_fen, int):
        raise ValueError("unit_price_fen must be an integer fen value")
    if unit_price_fen < 1 or unit_price_fen > 2_147_483_647:
        raise ValueError("unit_price_fen must be between 1 and 2147483647")
    global_minimum = billing["min_recharge_fen"]
    effective_minimum = ((global_minimum + unit_price_fen - 1) // unit_price_fen) * unit_price_fen
    return {
        **billing,
        "charged_unit_price_fen": unit_price_fen,
        "min_recharge_fen": effective_minimum,
        "recharge_step_fen": unit_price_fen,
    }


def effective_customer_billing_settings(
    billing: dict[str, int],
    *,
    user_id: str,
) -> dict[str, int]:
    """Return the normal billing snapshot or a tightly scoped real-chain rehearsal.

    The deployment-only override never changes stored global pricing.  It is
    enabled only when both environment variables are present, only for the
    exact customer id, and can never raise the real payment above five yuan.
    """
    configured_user_id = os.environ.get(ACCEPTANCE_PAYMENT_USER_ID_ENV, "").strip()
    raw_amount = os.environ.get(ACCEPTANCE_PAYMENT_AMOUNT_FEN_ENV, "").strip()
    if not configured_user_id and not raw_amount:
        return dict(billing)
    if not configured_user_id or not raw_amount:
        raise ValueError("acceptance payment requires both user id and amount")
    try:
        amount_fen = int(raw_amount)
    except ValueError as exc:
        raise ValueError("acceptance payment amount must be integer fen") from exc
    if amount_fen < 1 or amount_fen > MAX_ACCEPTANCE_PAYMENT_FEN:
        raise ValueError(
            f"acceptance payment amount must be between 1 and {MAX_ACCEPTANCE_PAYMENT_FEN} fen"
        )
    if user_id != configured_user_id:
        return dict(billing)
    charged_unit_price_fen = billing["charged_unit_price_fen"]
    if amount_fen % charged_unit_price_fen != 0:
        raise ValueError("acceptance payment amount must be divisible by the configured unit price")
    return {
        **billing,
        "min_recharge_fen": amount_fen,
        "recharge_step_fen": amount_fen,
    }


def mask_config(config: dict[str, str]) -> dict[str, str]:
    return {
        key: mask_secret(value) if is_secret_field(key) else value for key, value in config.items()
    }


def is_secret_field(key: str) -> bool:
    lowered = key.lower()
    return lowered == "key" or any(marker in lowered for marker in SECRET_FIELDS)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "********"
    return f"********{value[-4:]}"


logger = logging.getLogger(__name__)


class ProviderTestResult(BaseModel):
    status: str
    provider: str
    test_kind: str
    account_credit: int | None = None


class ProviderTester(Protocol):
    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult: ...

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult: ...


class NoopProviderTester:
    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        return ProviderTestResult(
            status="configured_only" if config else "not_configured",
            provider=provider,
            test_kind="connection",
        )

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "PROVIDER_TEST_NOT_IMPLEMENTED",
                "message": "A real provider client is required before paid tests can run.",
            },
        )


class HiflyAccountProbe(Protocol):
    def account_credit(self) -> int: ...


class HiflyPaidProbe(HiflyAccountProbe, Protocol):
    """付费探针在只读面之外需要的客户端面：一个已有声音 + 一次最小计费提交。"""

    def list_voices(
        self, *, page: int = 1, size: int = 10, kind: int | None = None
    ) -> list[dict[str, Any]]: ...

    def create_audio_by_tts(self, *, voice: str, text: str, title: str) -> str: ...


# 付费探针提交的最小任务：2 个字的 TTS 音频足以走通「余额-权限-结算」全链，
# 又让供应商侧实际产生的费用可以忽略。文本与标题都取同一常量，避免两处漂移。
_PAID_PROBE_TEXT = "计费探针"


def _default_hifly_client(config: Mapping[str, str]) -> HiflyAccountProbe:
    # app.hifly 顶层反向导入本模块（SettingsRepository）；默认客户端工厂只能在
    # 调用期解析，否则形成 settings -> hifly -> settings 模块级循环导入。
    from app.hifly import hifly_client_from_config

    return hifly_client_from_config(config)


class HiflyProviderTester:
    def __init__(
        self,
        *,
        fallback: ProviderTester | None = None,
        client_factory: Callable[[Mapping[str, str]], HiflyAccountProbe] | None = None,
    ) -> None:
        self.fallback = fallback or NoopProviderTester()
        self.client_factory = client_factory or _default_hifly_client

    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        if provider != "hifly" or not config:
            return self.fallback.connection_test(provider, config)
        from app.hifly import HiflyError, HiflySettingsUnavailable, HiflyTimeoutError

        try:
            account_credit = self.client_factory(config).account_credit()
        except HiflyTimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail={
                    "code": "HIFLY_ACCOUNT_CHECK_TIMEOUT",
                    "failure_phase": "account_credit",
                    "message": "Hifly 只读账户检查超时；未创建收费任务。",
                },
            ) from exc
        except HiflySettingsUnavailable as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "HIFLY_SETTINGS_INVALID",
                    "failure_phase": "configuration",
                    "message": "Hifly 配置不完整；请重新保存 API Key。",
                },
            ) from exc
        except HiflyError as exc:
            is_auth_failure = exc.vendor_code == 2003 or exc.http_status in {401, 403}
            raise HTTPException(
                # A vendor credential failure is an invalid saved setting, not
                # an expired administrator session. Returning 401 here would
                # make both settings clients sign the operator out.
                status_code=422 if is_auth_failure else 503,
                detail={
                    "code": (
                        "HIFLY_AUTH_FAILED" if is_auth_failure else "HIFLY_ACCOUNT_CHECK_FAILED"
                    ),
                    "failure_phase": "authenticate" if is_auth_failure else "account_credit",
                    "message": (
                        "Hifly 凭据认证失败；未创建收费任务。"
                        if is_auth_failure
                        else "Hifly 只读账户检查失败；未创建收费任务。"
                    ),
                },
            ) from exc
        return ProviderTestResult(
            status="ok",
            provider=provider,
            test_kind="account_credit",
            account_credit=account_credit,
        )

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        if provider != "hifly":
            return self.fallback.paid_test(provider, config)
        from app.hifly import (
            HiflyError,
            HiflySettingsUnavailable,
            HiflySubmissionUncertain,
            HiflyTimeoutError,
        )

        # 最小真实计费链路：先只读取一个已有声音与余额快照，最后才提交一次
        # 2 字 TTS 音频。计费提交刻意放在最后一步——它之前的任何失败都发生在
        # 提交之前，「未创建收费任务」因此可以无歧义地断言；提交本身失败的
        # 措辞则由异常种类决定（见下）。构造客户端也在 try 内：默认工厂在
        # API Key 缺失时抛 HiflySettingsUnavailable，须映射为配置错误而非漏出。
        phase = "configuration"
        try:
            probe = cast(HiflyPaidProbe, self.client_factory(config))
            phase = "list_voices"
            voices = probe.list_voices(page=1, size=1)
            voice = next(
                (
                    row["voice"]
                    for row in voices
                    if isinstance(row.get("voice"), str) and row["voice"].strip()
                ),
                None,
            )
            if voice is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "HIFLY_PAID_PROBE_NO_VOICE",
                        "failure_phase": "list_voices",
                        "message": "Hifly 账号没有可用于计费探针的声音；未创建收费任务。",
                    },
                )
            phase = "account_credit"
            account_credit = probe.account_credit()
            phase = "submit"
            probe.create_audio_by_tts(voice=voice, text=_PAID_PROBE_TEXT, title=_PAID_PROBE_TEXT)
        except HiflySubmissionUncertain as exc:
            # 提交可能已到达供应商却没有拿到凭证：不得断言未计费。
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "HIFLY_PAID_PROBE_UNCERTAIN",
                    "failure_phase": "submit",
                    "message": "Hifly 计费调用结果无法确认；可能已产生费用，请核对账单后再重试。",
                },
            ) from exc
        except HiflyTimeoutError as exc:
            # 提交阶段的超时会被客户端的 _creation_request 包装成
            # HiflySubmissionUncertain，走不到这里；submit 分支仅作防御。
            raise HTTPException(
                status_code=504,
                detail={
                    "code": "HIFLY_PAID_PROBE_TIMEOUT",
                    "failure_phase": phase,
                    "message": (
                        "Hifly 计费调用提交超时；无法确认是否已产生费用，请核对账单后再重试。"
                        if phase == "submit"
                        else "Hifly 只读检查超时；未创建收费任务。"
                    ),
                },
            ) from exc
        except HiflySettingsUnavailable as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "HIFLY_SETTINGS_INVALID",
                    "failure_phase": "configuration",
                    "message": "Hifly 配置不完整；请重新保存 API Key。",
                },
            ) from exc
        except HiflyError as exc:
            is_auth_failure = exc.vendor_code == 2003 or exc.http_status in {401, 403}
            raise HTTPException(
                status_code=422 if is_auth_failure else 503,
                detail={
                    "code": ("HIFLY_AUTH_FAILED" if is_auth_failure else "HIFLY_PAID_PROBE_FAILED"),
                    "failure_phase": "authenticate" if is_auth_failure else phase,
                    "message": (
                        "Hifly 凭据认证失败；未创建收费任务。"
                        if is_auth_failure
                        else f"{exc}；未创建收费任务。"
                    ),
                },
            ) from exc
        return ProviderTestResult(
            status="ok",
            provider=provider,
            test_kind="paid_probe",
            account_credit=account_credit,
        )


class StorageProviderTester:
    def __init__(
        self,
        *,
        fallback: ProviderTester | None = None,
        storage_factory: Callable[[CloudStorageConfig], StorageAdapter] = create_storage_adapter,
    ) -> None:
        self.fallback = fallback or NoopProviderTester()
        self.storage_factory = storage_factory

    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        if provider != "cos":
            return self.fallback.connection_test(provider, config)
        if not config:
            return self.fallback.connection_test(provider, config)

        for namespace in (
            "projects",
            "generation-results",
            "users",
            "materials",
            "verified-uploads",
            "viral/cover",
            "viral/prepared",
        ):
            self._check_namespace(provider, config, namespace)
        return ProviderTestResult(status="ok", provider=provider, test_kind="storage_connection")

    def _check_namespace(self, provider: str, config: dict[str, str], namespace: str) -> None:
        adapter: StorageAdapter | None = None
        cleanup_required = False
        put_succeeded = False
        test_key = f"{namespace}/settings-diagnostics/{uuid.uuid4().hex}.txt"
        payload = b"video-replica storage connection check"
        failure_phase = "initialize"
        operation_error: Exception | None = None
        cleanup_error: Exception | None = None
        try:
            adapter = self.storage_factory(cloud_storage_config_from_settings(provider, config))
            cleanup_required = True
            failure_phase = "put"
            adapter.put_object(test_key, payload, content_type="text/plain")
            put_succeeded = True
            failure_phase = "head"
            metadata = adapter.head_object(test_key)
            failure_phase = "get"
            content = adapter.get_object(test_key)
            failure_phase = "verify"
            if metadata is None or metadata.size != len(payload) or content != payload:
                raise StorageBackendUnavailable("storage connection test verification failed")
        except Exception as exc:
            operation_error = exc
        finally:
            if adapter is not None and cleanup_required:
                try:
                    content_store.delete_object_outside_content_namespace(
                        adapter, test_key, actor_id="settings-diagnostic"
                    )
                except Exception as exc:
                    cleanup_error = exc
                    logger.error("Storage connection test cleanup failed for provider %s", provider)

        if cleanup_error is not None and put_succeeded:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "STORAGE_CONNECTION_TEST_CLEANUP_FAILED",
                    "cleanup_failed": True,
                    "failure_phase": "delete",
                    "namespace": namespace,
                    "message": "对象存储测试对象清理失败；可能残留测试对象，请检查本地服务日志。",
                },
            ) from cleanup_error
        if isinstance(operation_error, ValueError):
            logger.warning("Storage connection test has invalid settings for provider %s", provider)
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "STORAGE_SETTINGS_INVALID",
                    "cleanup_failed": cleanup_error is not None,
                    "failure_phase": failure_phase,
                    "namespace": namespace,
                    "message": "对象存储配置无效；请检查必填参数。",
                },
            ) from operation_error
        if operation_error is not None:
            logger.warning("Storage connection test failed for provider %s", provider)
            message = f"对象存储目录 {namespace}/ 验证失败；请核对该目录的上传、读取及删除权限。"
            if cleanup_error is not None:
                message = (
                    f"对象存储目录 {namespace}/ 验证及清理失败；请核对该目录权限，"
                    "并检查是否残留测试对象。"
                )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "STORAGE_CONNECTION_TEST_FAILED",
                    "cleanup_failed": cleanup_error is not None,
                    "failure_phase": failure_phase,
                    "namespace": namespace,
                    "message": message,
                },
            ) from operation_error

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        return self.fallback.paid_test(provider, config)


def remove_cos_lifecycle_rules(
    config: dict[str, str],
    *,
    actor_id: str,
) -> dict[str, str]:
    """保存 COS 配置后删除自动过期规则；失败不阻断配置保存。"""
    try:
        adapter = create_storage_adapter(cloud_storage_config_from_settings("cos", config))
    except (ValueError, StorageBackendUnavailable) as exc:
        logger.warning("COS lifecycle rule removal skipped: %s", exc, exc_info=True)
        return {
            "status": "skipped",
            "message": "对象存储配置不完整或客户端初始化失败，已跳过旧媒体过期规则清理。",
        }
    cloud_adapter = adapter if isinstance(adapter, CloudStorageAdapter) else None
    if cloud_adapter is None:
        return {
            "status": "skipped",
            "message": "当前存储适配器不支持生命周期规则。",
        }
    try:
        cloud_adapter.remove_lifecycle_rules(actor_id=actor_id)
    except StorageBackendUnavailable as exc:
        logger.warning("COS lifecycle rules removal failed: %s", exc)
        return {
            "status": "failed",
            "message": "自动过期规则删除失败；配置已保存，可重新保存以重试。",
        }
    return {
        "status": "removed",
        "message": "已移除项目素材与成片的 180 天自动过期规则，按永久保存执行。",
    }


def get_provider_tester() -> ProviderTester:
    # 函数内导入：email_delivery 反向依赖本模块的 SettingsRepository。
    from app.email_delivery import EmailProviderTester

    return StorageProviderTester(
        fallback=HiflyProviderTester(fallback=EmailProviderTester(fallback=NoopProviderTester()))
    )


def require_supported_provider(provider: str) -> ProviderName:
    try:
        return normalize_provider(provider)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_PROVIDER",
                "message": "该服务已不受支持。",
            },
        ) from exc


def merge_provider_config(
    saved_config: dict[str, str], incoming_config: dict[str, str]
) -> dict[str, str]:
    merged = dict(saved_config)
    for key, value in incoming_config.items():
        # 掩码值（mask_secret 产出的 ******** 尾号形态）回传等于"未修改该
        # 密钥"：保留库中原值，避免把真实凭据覆盖成星号字符串。
        if is_secret_field(key) and value.strip().startswith("********"):
            continue
        if value.strip():
            merged[key] = value
        elif not is_secret_field(key):
            merged.pop(key, None)
    return merged
