"""语音转写（ASR）provider 合约与实现。

移植自 oral-ip-agents-research 的 Fun-ASR 接入方案并同步化：按时长智能
分流——短音频走 Flash 同步端点（秒级返回），长音频走异步 提交→轮询→
下载结果。云存储使用签名 URL；本地短音频使用受限的 Base64 直传，
本地长音频在提交前拒绝。凭据一律来自服务端加密供应商
配置存储（``dashscope`` provider），错误文案保持中性、不出现供应商名称。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from app.db_portable import BusinessConnection
from app.external_calls import endpoint_from_url, recorded_urlopen
from app.settings import SettingsRepository, SettingsUnavailableError

logger = logging.getLogger("app.asr")

DASHSCOPE_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
DASHSCOPE_DEFAULT_MODEL = "fun-asr"
DASHSCOPE_DEFAULT_FLASH_MODEL = "fun-asr-flash-2026-06-15"
DASHSCOPE_FLASH_THRESHOLD_SECONDS = 300.0
DASHSCOPE_POLL_INTERVAL_SECONDS = 2.0
DASHSCOPE_POLL_MAX_ATTEMPTS = 90
DASHSCOPE_TIMEOUT_SECONDS = 120.0
DASHSCOPE_INLINE_MAX_BYTES = 10_000_000
_INLINE_AUDIO_PREFIX = "data:audio/mp4;base64,"

ASR_PROVIDER_OVERRIDE_ENV = "VIDEO_REPLICA_ASR_PROVIDER"


class AsrProviderError(RuntimeError):
    """Provider-side failure surfaced to the task as a redacted message."""

    def __init__(self, message: str, *, usage_seconds: float | None = None) -> None:
        super().__init__(message)
        self.usage_seconds = usage_seconds


def max_inline_audio_bytes() -> int:
    """本地音频 base64 直传允许的**原始字节**上限（与 ``prepare_audio_input`` 同口径）.

    ``prepare_audio_input`` 按 ``len(prefix) + 4*ceil(n/3)`` 算编码后长度，这里
    反解出仍满足 ``DASHSCOPE_INLINE_MAX_BYTES`` 的最大 ``n``。上传端先用它拦截，
    免得用户传完整个文件才收到「编码后过大」。
    """
    return ((DASHSCOPE_INLINE_MAX_BYTES - len(_INLINE_AUDIO_PREFIX)) // 4) * 3


def max_inline_audio_seconds(conn: BusinessConnection) -> float:
    """本地音频直传允许的最长时长：provider 分流阈值与 Flash 硬上限取小.

    ``_uses_flash`` 要求 ``duration <= min(配置的 flash_threshold_sec, 300)``，
    所以两者取小才是真正能走直传的上界。
    """
    return min(
        load_asr_configuration(conn).flash_threshold_sec,
        DASHSCOPE_FLASH_THRESHOLD_SECONDS,
    )


def _confirmed_duration(value: object, *, divisor: float = 1) -> float | None:
    try:
        duration = float(str(value)) / divisor
        return duration if math.isfinite(duration) and duration > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


class AsrSubmissionUncertain(AsrProviderError):
    """The transport cannot prove whether a submit was accepted."""


class AsrServiceUnavailable(AsrProviderError):
    """An existing receipt can survive a temporary service/configuration failure."""


class AsrTaskPending(AsrProviderError):
    """A known receipt can be polled again without another paid submission."""


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    duration_sec: float | None
    language: str | None


@dataclass(frozen=True)
class AsrConfiguration:
    base_url: str
    api_key: str
    model: str
    flash_model: str
    flash_threshold_sec: float
    poll_interval_sec: float
    poll_max_attempts: int


class AsrProvider(Protocol):
    """Minimal provider contract used by the extraction pipeline."""

    name: str

    def transcribe(
        self, file_url: str, *, duration_sec: float | None = None
    ) -> TranscriptResult: ...


class AsrTransport(Protocol):
    """Injectable HTTP boundary: (method, url, headers, body_bytes, timeout) → (status, body)."""

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> tuple[int, bytes]: ...


def _default_transport(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=headers, method=method)  # noqa: S310
    try:
        # 语音转写（口播稿识别）的原始响应落调用日志（方案 P0-9）。
        response_body, _headers, status = recorded_urlopen(
            request,
            expected_json=True,
            timeout=timeout_seconds,
            provider="asr",
            endpoint=endpoint_from_url(url),
            opener=urlopen,
        )
        return status, response_body
    except HTTPError as exc:
        return exc.code, exc.read()
    except (TimeoutError, URLError, OSError) as exc:
        logger.warning("ASR transport failed: %s", type(exc).__name__)
        raise AsrSubmissionUncertain("语音转写服务连接暂时中断。") from exc


class FakeAsrProvider:
    """Deterministic provider for tests and 内部联调（env override）。"""

    name = "fake-asr"

    def __init__(self, text: str = "（测试转写）这是语音转写服务返回的原始文案。") -> None:
        self._text = text
        self.calls: list[str] = []

    def transcribe(self, file_url: str, *, duration_sec: float | None = None) -> TranscriptResult:
        self.calls.append(file_url)
        return TranscriptResult(
            text=self._text,
            duration_sec=duration_sec if duration_sec is not None else 12.0,
            language="zh",
        )


class DashScopeFunAsr:
    """阿里云 DashScope Fun-ASR：短音频同步 Flash / 长音频异步轮询。"""

    name = "dashscope-fun-asr"

    def __init__(
        self,
        config: AsrConfiguration,
        *,
        transport: AsrTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._transport = transport or _default_transport
        self._sleep = sleep

    # ---------------- public ----------------

    def _uses_flash(self, duration_sec: float | None) -> bool:
        return (
            duration_sec is not None
            and math.isfinite(duration_sec)
            and 0
            < duration_sec
            <= min(self._config.flash_threshold_sec, DASHSCOPE_FLASH_THRESHOLD_SECONDS)
        )

    def prepare_audio_input(
        self, file_url: str, *, audio_bytes: bytes, duration_sec: float | None
    ) -> str:
        """Prepare extracted M4A before recording a paid attempt; never publish local files."""
        if file_url.startswith("local://"):
            if not self._uses_flash(duration_sec):
                raise AsrProviderError(
                    "本地音频直传需要已确认时长且不超过 5 分钟。"
                    "请缩短音频，或由管理员启用云存储后重新上传。"
                )
            encoded_size = len(_INLINE_AUDIO_PREFIX) + 4 * ((len(audio_bytes) + 2) // 3)
            if encoded_size > DASHSCOPE_INLINE_MAX_BYTES:
                raise AsrProviderError(
                    "本地音频编码后过大，无法直接转写。请压缩音频或启用云存储后重新上传。"
                )
            file_url = _INLINE_AUDIO_PREFIX + base64.b64encode(audio_bytes).decode("ascii")
        self._validate_audio_input(file_url, duration_sec=duration_sec)
        return file_url

    def _validate_audio_input(self, file_url: str, *, duration_sec: float | None) -> None:
        if file_url.startswith(_INLINE_AUDIO_PREFIX):
            if not self._uses_flash(duration_sec) or len(file_url) > DASHSCOPE_INLINE_MAX_BYTES:
                raise AsrProviderError("本地音频超出直传限制，请启用云存储后重新上传。")
            return
        try:
            parsed = urlsplit(file_url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if (
                parsed.scheme not in {"http", "https"}
                or not host
                or parsed.username
                or parsed.password
            ):
                raise ValueError("not a public audio URL")
            if host == "localhost" or host.endswith((".localhost", ".local")):
                raise ValueError("local host")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError("non-public address")
        except ValueError as exc:
            raise AsrProviderError(
                "转写服务无法读取此音频地址。请使用本地短音频直传或云存储链接。"
            ) from exc

    def transcribe(
        self,
        file_url: str,
        *,
        duration_sec: float | None = None,
        on_submitted: Callable[[str], None] | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> TranscriptResult:
        cfg = self._config
        if not cfg.api_key:
            raise AsrProviderError("语音转写服务未配置")
        self._validate_audio_input(file_url, duration_sec=duration_sec)
        if self._uses_flash(duration_sec):
            logger.info(
                "ASR flash sync mode (duration %.0fs <= %.0fs)",
                duration_sec,
                cfg.flash_threshold_sec,
            )
            return self._transcribe_flash(file_url)
        return self._transcribe_async(file_url, on_submitted=on_submitted, heartbeat=heartbeat)

    # ---------------- flash (sync) ----------------

    def _transcribe_flash(self, file_url: str) -> TranscriptResult:
        cfg = self._config
        payload = {
            "model": cfg.flash_model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {"data": file_url}}],
                    }
                ]
            },
            "parameters": {"format": "m4a", "sample_rate": "16000"},
        }
        status, body = self._transport(
            "POST",
            f"{cfg.base_url}/api/v1/services/aigc/multimodal-generation/generation",
            headers=self._headers(sse_disable=True),
            body=json.dumps(payload).encode("utf-8"),
            timeout_seconds=DASHSCOPE_TIMEOUT_SECONDS,
        )
        data = self._decode(status, body)
        duration = _confirmed_duration(data.get("usage", {}).get("duration"))
        text = (data.get("output") or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            raise AsrProviderError("语音转写服务返回空文本", usage_seconds=duration)
        return TranscriptResult(
            text=text.strip(),
            duration_sec=duration,
            language="zh",
        )

    # ---------------- async (submit → poll → download) ----------------

    def _transcribe_async(
        self,
        file_url: str,
        *,
        on_submitted: Callable[[str], None] | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> TranscriptResult:
        task_id = self._submit_async_task(file_url)
        if on_submitted is not None:
            on_submitted(task_id)
        return self.resume(task_id, heartbeat=heartbeat)

    def resume(
        self,
        task_id: str,
        *,
        heartbeat: Callable[[], None] | None = None,
    ) -> TranscriptResult:
        try:
            output = self._poll_async_task(task_id, heartbeat=heartbeat)
            return self._download_transcription(output)
        except (AsrSubmissionUncertain, AsrServiceUnavailable) as exc:
            raise AsrTaskPending("已有转写任务连接暂时中断，正在恢复查询。") from exc

    def _submit_async_task(self, file_url: str) -> str:
        cfg = self._config
        payload = {
            "model": cfg.model,
            "input": {"file_urls": [file_url]},
            "parameters": {"channel_id": [0]},
        }
        status, body = self._transport(
            "POST",
            f"{cfg.base_url}/api/v1/services/audio/asr/transcription",
            headers=self._headers(async_header=True),
            body=json.dumps(payload).encode("utf-8"),
            timeout_seconds=30.0,
        )
        if status >= 500:
            logger.warning("ASR submit returned an uncertain HTTP status %s", status)
            raise AsrSubmissionUncertain("转写请求可能已受理，请先核查任务状态。")
        data = self._decode(status, body)
        task_id = data.get("output", {}).get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise AsrSubmissionUncertain("转写服务未返回有效任务号，请先核查任务状态。")
        return task_id

    def _poll_async_task(
        self,
        task_id: str,
        *,
        heartbeat: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        cfg = self._config
        for attempt in range(1, cfg.poll_max_attempts + 1):
            if heartbeat is not None:
                heartbeat()
            self._sleep(cfg.poll_interval_sec)
            status, body = self._transport(
                "GET",
                f"{cfg.base_url}/api/v1/tasks/{task_id}",
                headers=self._headers(),
                body=None,
                timeout_seconds=15.0,
            )
            if status >= 500:
                logger.warning("ASR poll transient failure attempt=%s", attempt)
                continue
            data = self._decode(status, body)
            output: dict[str, Any] = data.get("output", {})
            task_status = str(output.get("task_status", ""))
            if task_status == "SUCCEEDED":
                return output
            if task_status in ("FAILED", "CANCELED"):
                message = ""
                for item in output.get("results", []) or []:
                    if item.get("subtask_status") == "FAILED":
                        message = str(item.get("message") or item.get("code") or "")
                        break
                raise AsrProviderError(f"语音转写任务失败：{message or task_status}")
        raise AsrTaskPending("语音转写任务轮询超时，正在继续查询原任务。")

    def _download_transcription(self, output: dict[str, Any]) -> TranscriptResult:
        transcription_url = None
        for item in output.get("results", []) or []:
            if item.get("subtask_status") == "SUCCEEDED" and item.get("transcription_url"):
                transcription_url = str(item["transcription_url"])
                break
        if not transcription_url:
            raise AsrProviderError("语音转写服务未返回可用的转写结果")
        status, body = self._transport(
            "GET",
            transcription_url,
            headers={},
            body=None,
            timeout_seconds=30.0,
        )
        data = self._decode(status, body)
        duration = _confirmed_duration(
            (data.get("properties") or {}).get("original_duration_in_milliseconds"), divisor=1000
        )
        transcripts = data.get("transcripts") or []
        if not transcripts:
            raise AsrProviderError("语音转写结果缺少转写内容", usage_seconds=duration)
        full_text = transcripts[0].get("text") if isinstance(transcripts[0], dict) else None
        if not isinstance(full_text, str) or not full_text.strip():
            raise AsrProviderError("语音转写返回空文本", usage_seconds=duration)
        return TranscriptResult(
            text=full_text.strip(),
            duration_sec=duration,
            language="zh",
        )

    # ---------------- helpers ----------------

    def _headers(self, *, sse_disable: bool = False, async_header: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        if sse_disable:
            headers["X-DashScope-SSE"] = "disable"
        if async_header:
            headers["X-DashScope-Async"] = "enable"
        return headers

    def _decode(self, status: int, body: bytes) -> dict[str, Any]:
        if status in (401, 403):
            raise AsrServiceUnavailable("语音转写服务凭据无效或无权限，请检查设置")
        if status == 429 or status >= 500:
            logger.warning("ASR service temporarily unavailable: HTTP %s", status)
            raise AsrServiceUnavailable("语音转写服务暂不可用，请稍后重试。")
        if status >= 400:
            # Provider messages may echo signed input URLs or credentials. Only
            # preserve the UUID request reference; never log the response body.
            reference = "unavailable"
            try:
                error_data = json.loads(body)
                if isinstance(error_data, dict):
                    reference = str(UUID(str(error_data.get("request_id", ""))))
            except (ValueError, TypeError):
                pass
            logger.warning("ASR request rejected: HTTP %s request_id=%s", status, reference)
            if status == 400:
                raise AsrProviderError(
                    "语音转写请求未被接受，请检查音频格式、可访问地址与模型配置（HTTP 400）。"
                )
            raise AsrProviderError(f"语音转写服务返回错误（HTTP {status}）")
        try:
            data: dict[str, Any] = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AsrProviderError("语音转写服务响应解析失败") from exc
        return data


def load_asr_configuration(conn: BusinessConnection) -> AsrConfiguration:
    """Fail-fast credential read (enqueue path); secrets never enter task rows."""
    if _provider_override() == "fake":
        # fake 模式从不发起真实请求；占位值只满足 dataclass 必填，
        # 且刻意短于 secret 扫描器的最小长度阈值。
        return AsrConfiguration(
            base_url=DASHSCOPE_DEFAULT_BASE_URL,
            api_key="fake",
            model=DASHSCOPE_DEFAULT_MODEL,
            flash_model=DASHSCOPE_DEFAULT_FLASH_MODEL,
            flash_threshold_sec=DASHSCOPE_FLASH_THRESHOLD_SECONDS,
            poll_interval_sec=DASHSCOPE_POLL_INTERVAL_SECONDS,
            poll_max_attempts=DASHSCOPE_POLL_MAX_ATTEMPTS,
        )
    try:
        config = SettingsRepository(conn).load_provider_config("dashscope")
    except SettingsUnavailableError as exc:
        raise AsrProviderError("本地配置暂不可用，请稍后重试。") from exc
    api_key = str(config.get("api_key", ""))
    if not api_key:
        raise AsrProviderError(
            "尚未配置语音转写服务，请管理员在「设置 → 语音转写」中保存 API Key。"
        )
    workspace_id = str(config.get("workspace_id") or "")
    region = str(config.get("region") or "cn-beijing")
    base_url = str(config.get("base_url") or "").rstrip("/")
    if not base_url:
        base_url = (
            f"https://{workspace_id}.{region}.maas.aliyuncs.com"
            if workspace_id
            else DASHSCOPE_DEFAULT_BASE_URL
        )
    return AsrConfiguration(
        base_url=base_url,
        api_key=api_key,
        model=str(config.get("model") or DASHSCOPE_DEFAULT_MODEL),
        flash_model=str(config.get("flash_model") or DASHSCOPE_DEFAULT_FLASH_MODEL),
        flash_threshold_sec=float(
            config.get("flash_threshold_sec") or DASHSCOPE_FLASH_THRESHOLD_SECONDS
        ),
        poll_interval_sec=float(config.get("poll_interval_sec") or 2.0),
        poll_max_attempts=int(config.get("poll_max_attempts") or 90),
    )


def get_asr_provider(conn: BusinessConnection) -> FakeAsrProvider | DashScopeFunAsr:
    """Resolve the active provider; ``VIDEO_REPLICA_ASR_PROVIDER=fake`` forces
    the deterministic provider for tests and internal联调."""
    override = _provider_override()
    config = load_asr_configuration(conn)
    if override == "fake":
        return FakeAsrProvider()
    return DashScopeFunAsr(config)


def _provider_override() -> str:
    return os.environ.get(ASR_PROVIDER_OVERRIDE_ENV, "").strip().lower()
