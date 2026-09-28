"""第三方接口调用日志：记录请求摘要与对方原始响应，供管理端排查（方案 P0-9 / P0-12）。

视频、图片、口播等任务失败时，服务商返回的失败原因此前没有落库，管理端只能看到
笼统的错误码。本模块是唯一的记录入口：

- 各服务商的传输类在每次 HTTP 调用结束后调用 :func:`record_external_call`，
  不改动业务流程；
- 调用属于哪条任务，由工作线程用 :func:`external_call_context` 标注（上下文变量），
  传输类不需要知道任务编号；
- 写日志用独立的短超时连接：业务事务回滚时失败证据仍在，日志写不进去也只告警、
  绝不影响业务。

落库前统一脱敏：密钥类请求头不记录，地址与响应文本里的签名参数、Bearer 令牌、
形似密钥的字段值一律替换（AGENTS.md：密钥不得进入日志）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

logger = logging.getLogger(__name__)

Outcome = Literal["SUCCEEDED", "PROVIDER_ERROR", "TIMEOUT", "NETWORK_ERROR", "PARSE_ERROR"]

# 失败调用保留全文（上限 64 KB）；成功调用只留开头，用来和失败的响应对比。
FAILED_BODY_LIMIT = 64 * 1024
SUCCEEDED_BODY_LIMIT = 4 * 1024
PROVIDER_MESSAGE_LIMIT = 500
# 取日志连接最多等 2 秒：池子忙时宁可丢一条诊断日志，也不能拖住生成任务。
LOG_CONNECTION_TIMEOUT_SECONDS = 2.0

REDACTED = "[已脱敏]"

_SECRET_KEYS = frozenset(
    {
        "sign",
        "signature",
        "token",
        "access_token",
        "refresh_token",
        "security_token",
        "x-cos-security-token",
        "key",
        "api_key",
        "apikey",
        "api-key",
        "secret",
        "secret_key",
        "secret_access_key",
        "password",
        "authorization",
        "expires",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "q-signature",
        "q-ak",
        "q-sign-algorithm",
        "q-key-time",
        "q-sign-time",
        "q-header-list",
        "q-url-param-list",
    }
)
# 只留排查用得上的响应头；其余（含 set-cookie）一律不记。
_KEPT_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "retry-after",
        "x-request-id",
        "request-id",
        "x-trace-id",
        "x-tt-logid",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    }
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|secret(?:_access)?_key|access_token|password|token)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,&}]{6,})"
)


@dataclass(frozen=True)
class CallContext:
    task_type: str
    task_id: str
    attempt: int | None = None


_CONTEXT: ContextVar[CallContext | None] = ContextVar("external_call_context", default=None)
# 模型名单独存一个上下文：调用方比任务层更清楚本次用哪个模型（如视频分析的主/备
# 模型切换），与任务归属上下文互不覆盖。
_MODEL: ContextVar[str | None] = ContextVar("external_call_model", default=None)


@contextmanager
def external_call_context(
    task_type: str, task_id: str, *, attempt: int | None = None
) -> Iterator[None]:
    """标注接下来的第三方调用属于哪条任务；离开时恢复外层上下文。"""
    token = _CONTEXT.set(CallContext(task_type=task_type, task_id=task_id, attempt=attempt))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@contextmanager
def external_call_model(model: str | None) -> Iterator[None]:
    """标注接下来的第三方调用使用的模型名（写进调用日志的 model 列）。"""
    token = _MODEL.set(model)
    try:
        yield
    finally:
        _MODEL.reset(token)


def current_call_context() -> CallContext | None:
    return _CONTEXT.get()


def redact_url(url: str) -> str:
    """去掉地址里的签名、令牌与账号口令，保留路径和普通参数便于定位。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _URL_PATTERN.sub(REDACTED, url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    query = urlencode(
        [
            (key, REDACTED if key.lower() in _SECRET_KEYS else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        safe="[]",
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def redact_text(text: str) -> str:
    text = _URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)
    text = _BEARER_PATTERN.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    return _SECRET_FIELD_PATTERN.sub(lambda match: f"{match.group(1)}{REDACTED}", text)


SUMMARY_STRING_LIMIT = 2000


def redact_value(value: Any) -> Any:
    """递归脱敏请求摘要：密钥类字段整体替换，字符串里的签名链接逐个替换。

    超长字符串（多为内联的 base64 图片或音频）只记长度：摘要是给人看的业务参数，
    不是请求体备份。
    """
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if str(key).lower() in _SECRET_KEYS else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        if len(value) > SUMMARY_STRING_LIMIT:
            return f"[已省略 {len(value)} 个字符]"
        return redact_text(value)
    return value


def summarize_request_body(body: bytes | str | None) -> Any:
    """请求体 → 摘要：JSON 解析后脱敏；表单解析成字典；其余只记类型与字节数。"""
    if body is None:
        return None
    text = _decode(body) or ""
    try:
        return redact_value(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        pass
    if "=" in text and "\n" not in text and len(text) < 20000:
        return redact_value(dict(parse_qsl(text, keep_blank_values=True)))
    size = len(body) if isinstance(body, bytes) else len(text.encode("utf-8"))
    return {"body": f"[非文本请求体，{size} 字节]"}


def _decode(body: bytes | str | None) -> str | None:
    if body is None:
        return None
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body


def parse_provider_error(
    body: bytes | str | None, *, allow_plain_text: bool = True
) -> tuple[str | None, str | None]:
    """从常见的响应结构里取服务商错误码与原话；取不到时返回 (None, None)。

    只有「明确是错误」的字段才算：``error`` 对象或字符串、非成功的 ``code``、
    ``fail_reason`` 一类字段、``status`` 为 failed；成功响应里顺带的 ``message``
    （如 "task created"）不算。嵌在 ``data`` / ``result`` / ``output`` 里的同形结构
    一并识别。``allow_plain_text`` 时非 JSON 响应体整体当原话（用于已知失败的调用）。
    """
    text = _decode(body)
    if not text:
        return None, None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        if not allow_plain_text:
            return None, None
        return None, _clean_message(text)
    return _error_from_payload(payload)


_SUCCESS_CODES = frozenset({"0", "200", "success", "ok", "succeeded"})
_FAILED_STATUSES = frozenset({"failed", "failure", "error", "fail"})
_EXPLICIT_FAILURE_KEYS = (
    "fail_reason",
    "failed_reason",
    "failure_reason",
    "error_message",
    "error_msg",
    "err_msg",
)


def _error_from_payload(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, Mapping):
        return None, None
    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code") or error.get("type")
        message = error.get("message") or error.get("msg")
        return _clean_code(code), _clean_message(message)
    if isinstance(error, str) and error.strip():
        return None, _clean_message(error)
    code = payload.get("code", payload.get("error_code"))
    code_ok = code is None or code == "" or str(code).lower() in _SUCCESS_CODES
    generic = payload.get("msg") or payload.get("message") or payload.get("status_msg")
    detail = payload.get("detail")
    if generic is None and isinstance(detail, str):
        generic = detail
    explicit = next((payload.get(key) for key in _EXPLICIT_FAILURE_KEYS if payload.get(key)), None)
    if explicit:
        return _clean_code(None if code_ok else code), _clean_message(explicit)
    if not code_ok:
        return _clean_code(code), _clean_message(generic)
    if str(payload.get("status", "")).lower() in _FAILED_STATUSES:
        return None, _clean_message(generic) or "服务商返回任务失败，未附原因"
    if isinstance(detail, Mapping):
        return _error_from_payload(detail)
    for nested_key in ("data", "result", "output"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            nested_code, nested_message = _error_from_payload(nested)
            if nested_code or nested_message:
                return nested_code, nested_message
    return None, None


def _clean_code(code: Any) -> str | None:
    if code is None or code == "":
        return None
    return str(code)[:120]


def _clean_message(message: Any) -> str | None:
    if message is None:
        return None
    raw = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
    text = " ".join(redact_text(raw).split())
    if not text:
        return None
    if len(text) > PROVIDER_MESSAGE_LIMIT:
        return text[:PROVIDER_MESSAGE_LIMIT] + "…"
    return text


def summarize_provider_message(message: str | None) -> str | None:
    """服务商原话 → 脱敏、压空白、截断后的展示文本；空值返回 None。"""
    return _clean_message(message) if message else None


def _kept_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    if not headers:
        return None
    kept = {
        str(name).lower(): redact_text(str(value))[:200]
        for name, value in headers.items()
        if str(name).lower() in _KEPT_RESPONSE_HEADERS
    }
    return kept or None


def _provider_request_id(headers: Mapping[str, str] | None) -> str | None:
    if not headers:
        return None
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    for name in ("x-request-id", "request-id", "x-trace-id", "x-tt-logid"):
        if lowered.get(name):
            return lowered[name][:200]
    return None


@dataclass(frozen=True)
class PreparedCall:
    """一次调用落库前的全部字段（已脱敏、已截断）；拆出来便于单测。"""

    provider: str
    endpoint: str
    method: str
    url_redacted: str | None
    request_summary: Any
    http_status: int | None
    outcome: Outcome
    latency_ms: int | None
    response_headers: dict[str, str] | None
    response_body: str | None
    response_body_bytes: int | None
    provider_error_code: str | None
    provider_message: str | None
    provider_task_id: str | None
    provider_request_id: str | None
    error_code: str | None
    error_message: str | None
    model: str | None
    context: CallContext | None


def prepare_external_call(
    *,
    provider: str,
    endpoint: str,
    method: str,
    url: str | None,
    outcome: Outcome,
    request_summary: Any = None,
    http_status: int | None = None,
    response_headers: Mapping[str, str] | None = None,
    response_body: bytes | str | None = None,
    binary_response: bool = False,
    latency_ms: int | None = None,
    provider_task_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    model: str | None = None,
) -> PreparedCall:
    raw_bytes: int | None = None
    body_text: str | None = None
    if response_body is not None:
        raw_bytes = (
            len(response_body)
            if isinstance(response_body, bytes)
            else len(response_body.encode("utf-8"))
        )
        if not binary_response:
            limit = SUCCEEDED_BODY_LIMIT if outcome == "SUCCEEDED" else FAILED_BODY_LIMIT
            body_text = redact_text(_decode(response_body) or "")
            if len(body_text) > limit:
                body_text = body_text[:limit]
    code, message = (None, None)
    if outcome != "SUCCEEDED" and not binary_response:
        code, message = parse_provider_error(response_body)
    return PreparedCall(
        provider=provider,
        endpoint=endpoint,
        method=method.upper(),
        url_redacted=redact_url(url) if url else None,
        request_summary=redact_value(request_summary) if request_summary is not None else None,
        http_status=http_status,
        outcome=outcome,
        latency_ms=max(0, latency_ms) if latency_ms is not None else None,
        response_headers=_kept_headers(response_headers),
        response_body=body_text,
        response_body_bytes=raw_bytes,
        provider_error_code=code,
        provider_message=message,
        provider_task_id=provider_task_id,
        provider_request_id=_provider_request_id(response_headers),
        error_code=error_code,
        error_message=_clean_message(error_message) if error_message else None,
        model=model or _MODEL.get(),
        context=current_call_context(),
    )


def record_external_call(**kwargs: Any) -> None:
    """落一条调用日志；任何失败只告警，绝不向业务流程抛错。"""
    try:
        prepared = prepare_external_call(**kwargs)
        _insert(prepared)
    except Exception as exc:  # noqa: BLE001 — 诊断日志不得影响业务
        logger.warning(
            "external call log was not recorded: %s (%s)",
            type(exc).__name__,
            kwargs.get("endpoint"),
        )


def _insert(call: PreparedCall) -> None:
    from app.db_pg import get_pg_pool

    context = call.context
    pool = get_pg_pool()
    with pool.connection(timeout=LOG_CONNECTION_TIMEOUT_SECONDS) as conn, conn.transaction():
        conn.execute(
            """
            INSERT INTO external_call_logs (
                id, provider, model, endpoint_name, method, url_redacted,
                request_summary_json, http_status, latency_ms, outcome,
                response_headers_json, response_body, response_body_bytes,
                provider_error_code, provider_message, provider_task_id,
                provider_request_id, error_code, error_message_redacted,
                task_type, task_id, attempt, request_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                str(uuid4()),
                call.provider,
                call.model,
                call.endpoint,
                call.method,
                call.url_redacted,
                None
                if call.request_summary is None
                else json.dumps(call.request_summary, ensure_ascii=False),
                call.http_status,
                call.latency_ms,
                call.outcome,
                None
                if call.response_headers is None
                else json.dumps(call.response_headers, ensure_ascii=False),
                call.response_body,
                call.response_body_bytes,
                call.provider_error_code,
                call.provider_message,
                call.provider_task_id,
                call.provider_request_id,
                call.error_code,
                call.error_message,
                None if context is None else context.task_type,
                None if context is None else context.task_id,
                None if context is None else context.attempt,
                _request_id(),
            ),
        )


def _request_id() -> str:
    """HTTP 请求内沿用该请求的编号（与审计、服务日志一致）；后台工作线程另起一个。"""
    from app.ops_metrics import current_request_context

    request = current_request_context()
    if request is not None and request.request_id:
        return request.request_id
    return f"call_{uuid4().hex[:16]}"


_TASK_ID_KEYS = ("task_id", "taskId", "task_no", "job_id", "jobId", "id")


def provider_task_id_from(body: bytes | str | None) -> str | None:
    """从提交接口的响应里取第三方任务号（顶层或 data / result 下的常见键）。"""
    text = _decode(body)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    scopes = [payload, payload.get("data"), payload.get("result")]
    for scope in scopes:
        if not isinstance(scope, Mapping):
            continue
        for key in _TASK_ID_KEYS:
            value = scope.get(key)
            if isinstance(value, str | int) and not isinstance(value, bool) and str(value):
                return str(value)[:200]
    return None


_BINARY_CONTENT_PREFIXES = ("video/", "image/", "audio/", "application/octet-stream")


def _is_binary_content(headers: Mapping[str, str]) -> bool:
    """下载成片、图片、音频时只记元数据（大小、类型），不把文件内容写进日志。"""
    content_type = next(
        (str(value) for name, value in headers.items() if str(name).lower() == "content-type"),
        "",
    ).lower()
    return content_type.startswith(_BINARY_CONTENT_PREFIXES)


def _is_business_failure(body: bytes | str | None) -> bool:
    """HTTP 200 但业务失败：视频任务的失败原因就在这类响应里。"""
    code, message = parse_provider_error(body, allow_plain_text=False)
    return bool(code or message)


def recorded_urlopen(
    request: Any,
    *,
    timeout: float,
    provider: str,
    endpoint: str,
    opener: Any = None,
    read_limit: int | None = None,
    model: str | None = None,
) -> tuple[bytes, dict[str, str], int]:
    """``urlopen`` 的记录版：返回 (响应体, 响应头, 状态码)，异常与原来一致。

    HTTPError 的响应体被读出来记录后，重新包进一个同样的 HTTPError 抛出，调用方
    原有的 ``exc.read()`` 依旧能读到——各服务商传输类的错误处理不用改。
    ``read_limit`` 限制最多读取的字节数，调用方据返回长度自行判断是否超限。
    二进制响应（图片/音频/视频下载）按 Content-Type 自动识别：只记元数据，
    不把文件内容写进日志，也不需要调用方声明。
    """
    import io
    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen

    open_url = opener or urlopen
    url = request.full_url if hasattr(request, "full_url") else str(request)
    method = request.get_method() if hasattr(request, "get_method") else "GET"
    summary = summarize_request_body(getattr(request, "data", None))
    timer = CallTimer()
    common = {
        "provider": provider,
        "endpoint": endpoint,
        "method": method,
        "url": url,
        "request_summary": summary,
        "model": model,
    }
    try:
        with open_url(request, timeout=timeout) as response:
            body = response.read() if read_limit is None else response.read(read_limit)
            headers = dict(response.headers.items())
            status = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        try:
            # 错误响应只需要看原因，最多读 1 MB，避免异常大的错误页占满内存。
            error_body = exc.read(1024 * 1024)
        except (OSError, ValueError, AttributeError):
            error_body = b""
        record_external_call(
            **common,
            outcome="PROVIDER_ERROR",
            http_status=exc.code,
            response_headers=dict(exc.headers.items()) if exc.headers else None,
            response_body=error_body,
            latency_ms=timer.elapsed_ms(),
        )
        raise HTTPError(exc.url, exc.code, exc.msg, exc.headers, io.BytesIO(error_body)) from exc
    except TimeoutError as exc:
        record_external_call(
            **common,
            outcome="TIMEOUT",
            latency_ms=timer.elapsed_ms(),
            error_message=f"等待 {timeout:g} 秒未收到响应",
        )
        raise exc
    except URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        record_external_call(
            **common,
            outcome="TIMEOUT" if timed_out else "NETWORK_ERROR",
            latency_ms=timer.elapsed_ms(),
            error_message=type(exc.reason).__name__ if exc.reason else type(exc).__name__,
        )
        raise
    except OSError as exc:
        record_external_call(
            **common,
            outcome="NETWORK_ERROR",
            latency_ms=timer.elapsed_ms(),
            error_message=type(exc).__name__,
        )
        raise
    binary_response = _is_binary_content(headers)
    failed = not binary_response and _is_business_failure(body)
    record_external_call(
        **common,
        outcome="PROVIDER_ERROR" if failed else "SUCCEEDED",
        http_status=status,
        response_headers=headers,
        response_body=body,
        binary_response=binary_response,
        latency_ms=timer.elapsed_ms(),
        provider_task_id=None if binary_response else provider_task_id_from(body),
    )
    return body, headers, status


def endpoint_from_url(url: str) -> str:
    """接口名取地址最后两段路径（不含查询参数），例如 ``v1/images/edits``。"""
    try:
        path = urlsplit(url).path
    except ValueError:
        return "unknown"
    parts = [part for part in path.split("/") if part]
    return "/".join(parts[-3:]) or "/"


class CallTimer:
    """记录耗时的小工具：``timer = CallTimer(); ...; timer.elapsed_ms()``。"""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)
