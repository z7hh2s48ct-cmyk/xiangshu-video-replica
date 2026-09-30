"""邮件推送：账号验证码与安全通知的发信出口.

只做模板发信。内地地域的邮件推送只接受审核过的模板（不接受自由正文），而验证码
这类触发类邮件本来就该固定文案，所以不留「自由正文」这条路。直接按云 API v3
（TC3-HMAC-SHA256）签名调用，不引入 SDK——整个依赖面只有一次 JSON POST。

控制台里需要建两个模板（通知模板可选，缺省时重置成功后不发通知）：

- 验证码模板，变量 ``{{action}}``（如「绑定邮箱」「重置密码」）、``{{code}}``
  （6 位数字）、``{{minutes}}``（有效分钟数）；
- 通知模板，变量 ``{{username}}``、``{{time}}``（北京时间，精确到分钟）。

日志只记错误码与异常类型：请求异常的字符串里带着接口域名，而对外日志不出现
供应商名称（见 AGENTS.md 编码红线）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import requests
from fastapi import HTTPException

from app.db_portable import BusinessConnection

logger = logging.getLogger(__name__)

PROVIDER = "ses"
SUPPORTED_REGIONS = frozenset({"ap-guangzhou", "ap-hongkong"})
DEFAULT_REGION = "ap-guangzhou"

_HOST = "ses.tencentcloudapi.com"
_SERVICE = "ses"
_API_VERSION = "2020-10-02"
_CONTENT_TYPE = "application/json; charset=utf-8"
_TIMEOUT_SECONDS = (5, 10)
# 1 = 触发类邮件（验证码、通知）：走事务通道，不受营销类发送频控影响。
_TRIGGER_TYPE_TRANSACTIONAL = 1

CODE_SUBJECT = "账号验证码"
NOTICE_SUBJECT = "账号密码已重置"


class EmailDeliveryError(RuntimeError):
    """发信失败；``code`` 是服务端返回的错误码（网络层失败时为异常类型名）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EmailSender(Protocol):
    def send_code(self, *, to: str, code: str, action: str, minutes: int) -> None: ...

    def send_password_reset_notice(self, *, to: str, username: str, occurred_at: str) -> None: ...

    def send_alert_digest(
        self, *, to: str, total: int, danger_count: int, items: str, generated_at: str
    ) -> None:
        """告警摘要（方案 P2 推送通道）：未配置摘要模板的实现可静默跳过。"""
        ...


def validate_email_config(config: Mapping[str, str]) -> None:
    """保存前校验：配错的模板号要在保存时拒绝，而不是等到客户收不到验证码。"""
    region = config.get("region") or DEFAULT_REGION
    if region not in SUPPORTED_REGIONS:
        raise ValueError("发信地域只能是 ap-guangzhou 或 ap-hongkong")
    if "@" not in config.get("from_address", ""):
        raise ValueError("发信地址需要是完整邮箱，可写成「名称 <noreply@example.com>」")
    for field in ("code_template_id", "notice_template_id"):
        value = config.get(field, "")
        if value and not (value.isdigit() and int(value) > 0):
            raise ValueError("模板 ID 必须是正整数")


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signed_headers(
    *, secret_id: str, secret_key: str, action: str, region: str, payload: str, timestamp: int
) -> dict[str, str]:
    """TC3-HMAC-SHA256 请求头（公开给测试核对签名向量）。"""
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    canonical_request = "\n".join(
        (
            "POST",
            "/",
            "",
            f"content-type:{_CONTENT_TYPE}\nhost:{_HOST}\nx-tc-action:{action.lower()}\n",
            "content-type;host;x-tc-action",
            _sha256_hex(payload),
        )
    )
    credential_scope = f"{date}/{_SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        ("TC3-HMAC-SHA256", str(timestamp), credential_scope, _sha256_hex(canonical_request))
    )
    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, _SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders=content-type;host;x-tc-action, Signature={signature}"
        ),
        "Content-Type": _CONTENT_TYPE,
        "Host": _HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": _API_VERSION,
        "X-TC-Region": region,
    }


PostFn = Callable[..., requests.Response]


class TemplateEmailSender:
    def __init__(self, config: Mapping[str, str], *, post: PostFn | None = None) -> None:
        self._secret_id = config["secret_id"]
        self._secret_key = config["secret_key"]
        self._region = config.get("region") or DEFAULT_REGION
        self._from_address = config["from_address"]
        self._code_template_id = int(config["code_template_id"])
        notice = config.get("notice_template_id", "")
        self._notice_template_id = int(notice) if notice else None
        self._post = post or requests.post

    def call(self, action: str, params: Mapping[str, Any]) -> dict[str, Any]:
        payload = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        headers = signed_headers(
            secret_id=self._secret_id,
            secret_key=self._secret_key,
            action=action,
            region=self._region,
            payload=payload,
            timestamp=int(time.time()),
        )
        try:
            response = self._post(
                f"https://{_HOST}/",
                data=payload.encode("utf-8"),
                headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EmailDeliveryError(type(exc).__name__) from None
        result = body.get("Response") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise EmailDeliveryError("MalformedResponse")
        error = result.get("Error")
        if isinstance(error, dict):
            raise EmailDeliveryError(str(error.get("Code") or "UnknownError"))
        return result

    def _send_template(
        self, *, to: str, subject: str, template_id: int, data: Mapping[str, str]
    ) -> None:
        self.call(
            "SendEmail",
            {
                "FromEmailAddress": self._from_address,
                "Destination": [to],
                "Subject": subject,
                "Template": {
                    "TemplateID": template_id,
                    "TemplateData": json.dumps(data, ensure_ascii=False),
                },
                "TriggerType": _TRIGGER_TYPE_TRANSACTIONAL,
            },
        )

    def send_code(self, *, to: str, code: str, action: str, minutes: int) -> None:
        self._send_template(
            to=to,
            subject=CODE_SUBJECT,
            template_id=self._code_template_id,
            data={"action": action, "code": code, "minutes": str(minutes)},
        )

    def send_password_reset_notice(self, *, to: str, username: str, occurred_at: str) -> None:
        if self._notice_template_id is None:
            return
        self._send_template(
            to=to,
            subject=NOTICE_SUBJECT,
            template_id=self._notice_template_id,
            data={"username": username, "time": occurred_at},
        )

    def send_alert_digest(
        self, *, to: str, total: int, danger_count: int, items: str, generated_at: str
    ) -> None:
        """告警摘要邮件：复用通知模板（username/time 变量位），模板未配则跳过。

        摘要正文受模板变量结构限制，只放计数与生成时刻；明细引导回管理端
        告警页——邮件不做告警的唯一出口（页面红黄条始终在）。
        """
        if self._notice_template_id is None:
            return
        self._send_template(
            to=to,
            subject=f"管理端告警：{danger_count} 条紧急 / 共 {total} 条",
            template_id=self._notice_template_id,
            data={
                "username": "告警接收人",
                "time": generated_at,
            },
        )

    def check_template(self) -> None:
        """只读核对凭据与验证码模板（不发信、不计费）。"""
        self.call("GetEmailTemplate", {"TemplateID": self._code_template_id})


def email_sender_from_settings(conn: BusinessConnection) -> EmailSender | None:
    """未配置发信时回 ``None``，由调用方决定对外答什么。"""
    from app.settings import SettingsRepository

    config = SettingsRepository(conn).load_provider_config(PROVIDER)
    if not config:
        return None
    return TemplateEmailSender(config)


def deliver_quietly(send: Callable[[], None], *, kind: str) -> None:
    """后台发信：失败只告警。

    请求在发信之前就已答复（见 customer_email_routes 的防枚举说明），这里没有
    调用方可以报错；验证码邮件没收到，用户可以在冷却后重发。
    """
    try:
        send()
    except EmailDeliveryError as exc:
        logger.warning("email delivery failed kind=%s code=%s", kind, exc.code)
    except Exception as exc:  # pragma: no cover - 后台任务不得把异常抛进事件循环
        logger.warning("email delivery failed kind=%s error=%s", kind, type(exc).__name__)


class EmailProviderTester:
    """管理端「测试连接」：用读模板接口核对凭据，不发任何邮件。"""

    def __init__(self, *, fallback: Any, sender_factory: Callable[..., Any] | None = None) -> None:
        self.fallback = fallback
        self.sender_factory = sender_factory or TemplateEmailSender

    def connection_test(self, provider: str, config: dict[str, str]) -> Any:
        from app.settings import ProviderTestResult

        if provider != PROVIDER or not config:
            return self.fallback.connection_test(provider, config)
        try:
            self.sender_factory(config).check_template()
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "EMAIL_SETTINGS_INVALID", "message": "邮件推送配置不完整。"},
            ) from exc
        except EmailDeliveryError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "EMAIL_CHECK_FAILED",
                    "message": f"邮件推送检查失败（{exc.code}），请核对密钥、地域与模板 ID。",
                },
            ) from exc
        return ProviderTestResult(status="ok", provider=provider, test_kind="template")

    def paid_test(self, provider: str, config: dict[str, str]) -> Any:
        return self.fallback.paid_test(provider, config)
