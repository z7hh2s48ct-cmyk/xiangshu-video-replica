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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

Outcome = Literal["SUCCEEDED", "PROVIDER_ERROR", "TIMEOUT", "NETWORK_ERROR", "PARSE_ERROR"]

# 失败正文超过 64 KB 外存全文；成功正文只保留 4 KB 摘要。
FAILED_BODY_LIMIT = 64 * 1024
SUCCEEDED_BODY_LIMIT = 4 * 1024
PROVIDER_MESSAGE_LIMIT = 500
# 取日志连接最多等 2 秒：池子忙时宁可丢一条诊断日志，也不能拖住生成任务。
LOG_CONNECTION_TIMEOUT_SECONDS = 2.0

# 保留期（方案 P0-9）：失败调用要留到客户投诉、对账都过了才有用，成功调用只用来
# 和失败的响应对比，留得短。响应全文不是账务事实，超期直接删行。
FAILED_RETENTION_DAYS = 180
SUCCEEDED_RETENTION_DAYS = 30
# 每批删多少行：日志表可能一次积了很多行，分批提交避免一个长事务锁住写入路径。
PURGE_BATCH_SIZE = 5000

REDACTED = "[已脱敏]"

_SECRET_KEYS = frozenset(
    {
        "sign",
        "sig",
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
        "client_secret",
        "access_key",
        "access_key_id",
        "secret_id",
        "cookie",
        "set-cookie",
        "credential",
        "credentials",
        "session_token",
        "private_key",
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
_NORMALIZED_SECRET_KEYS = frozenset(re.sub(r"[^a-z0-9]", "", key) for key in _SECRET_KEYS)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_COOKIE_HEADER_PATTERN = re.compile(
    r"(?im)(?P<prefix>(?<![\w-])(?:cookie|set-cookie)\s*:\s*)[^\r\n]*"
)
# 只匹配敏感键，包装字段不能遮住内部凭据；字段拼写允许大小写及分隔符差异。
_SECRET_KEY_PATTERN = "|".join(
    "[_-]*".join(re.escape(char) for char in key)
    for key in sorted(_NORMALIZED_SECRET_KEYS, key=len, reverse=True)
)
_SECRET_FIELD_PATTERN = re.compile(
    rf"(?i)(?P<prefix>(?<![\w-])(?:\\*[\"'])?(?:{_SECRET_KEY_PATTERN})"
    r"(?:\\*[\"'])?\s*[:=]\s*)"
    r"(?P<value>(?P<escape>\\+)(?P<quote>[\"'])"
    r"(?:(?!(?P=escape)(?P=quote)).)*(?P=escape)(?P=quote)|"
    r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,&}\]]+)"
)


def _is_secret_key(key: str) -> bool:
    # 各接口的字段命名不统一；大小写、下划线和连字符不能改变凭据的敏感等级。
    return re.sub(r"[^a-z0-9]", "", key.lower()) in _NORMALIZED_SECRET_KEYS


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
    from app.ops_metrics import (
        bind_request_context,
        current_request_context,
        set_current_trace_fields,
    )

    request = current_request_context()
    request_id = request.request_id if request is not None and request.method != "WORKER" else None
    if request_id is None:
        request_id = _task_request_id(task_type, task_id)
    token = _CONTEXT.set(CallContext(task_type=task_type, task_id=task_id, attempt=attempt))
    try:
        with bind_request_context(
            request_id=request_id,
            method=request.method if request is not None else "WORKER",
            route=request.route if request is not None else f"task/{task_type}",
            request=request.request if request is not None else None,
        ):
            set_current_trace_fields(task_id=task_id)
            trace = {
                "request_id": request_id,
                "task_type": task_type,
                "task_id": task_id,
                "attempt": attempt,
            }
            logger.info(json.dumps({"event": "task_call_context_started", **trace}, sort_keys=True))
            try:
                yield
            finally:
                logger.info(
                    json.dumps({"event": "task_call_context_finished", **trace}, sort_keys=True)
                )
    finally:
        _CONTEXT.reset(token)


def _task_request_id(task_type: str, task_id: str) -> str:
    from app.db_pg import get_pg_pool

    try:
        with get_pg_pool().connection(timeout=LOG_CONNECTION_TIMEOUT_SECONDS) as conn:
            row = conn.execute(
                "SELECT request_id FROM task_diagnostic_refs WHERE task_type=%s AND task_id=%s",
                (task_type, task_id),
            ).fetchone()
            if row:
                return str(row[0])
    except Exception as exc:  # noqa: BLE001 — diagnostic enrichment cannot stop a task
        logger.debug("task correlation lookup unavailable: %s", type(exc).__name__)
    # A task that predates correlation capture still gets one stable key across all attempts.
    return f"task_{task_type}_{task_id}"


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
        host = parts.hostname or ""
        netloc = f"[{host}]" if ":" in host else host
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
    except ValueError:
        return _URL_PATTERN.sub(REDACTED, url)
    query = urlencode(
        [
            (key, REDACTED if _is_secret_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        safe="[]",
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def redact_text(text: str) -> str:
    # JSON 先递归处理，避免空格、转义、嵌套字段绕过普通文本的匹配；不截断响应原文。
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return _redact_plain_text(text)
    if isinstance(payload, dict | list):
        return json.dumps(_redact_response_value(payload), ensure_ascii=False)
    return _redact_plain_text(text)


def _redact_response_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _is_secret_key(str(key)) else _redact_response_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_response_value(item) for item in value]
    return _redact_plain_text(value) if isinstance(value, str) else value


def _redact_plain_text(text: str) -> str:
    # Cookie 每个分号段都可能是会话凭据，按完整头行遮蔽，不能只删第一项。
    text = _COOKIE_HEADER_PATTERN.sub(lambda match: f"{match['prefix']}{REDACTED}", text)
    text = _URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)
    text = _BEARER_PATTERN.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    return _SECRET_FIELD_PATTERN.sub(lambda match: f"{match['prefix']}{REDACTED}", text)


SUMMARY_STRING_LIMIT = 2000


def redact_value(value: Any, *, field_name: str = "") -> Any:
    """递归脱敏请求摘要：密钥类字段整体替换，字符串里的签名链接逐个替换。

    提示词与文案保留脱敏全文；内联媒体只记录引用和长度。
    """
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED
            if _is_secret_key(str(key))
            else redact_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(item, field_name=field_name) for item in value]
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value[:200]:
            return {"media_reference": value.split(",", 1)[0], "characters": len(value)}
        if field_name.casefold() in {"b64_json", "base64", "image_base64", "audio_base64"}:
            return {"media_reference": "inline_base64", "characters": len(value)}
        if (
            field_name.casefold() in {"image", "images", "audio", "video"}
            and len(value) > SUMMARY_STRING_LIMIT
            and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value)
        ):
            return {"media_reference": "inline_base64", "characters": len(value)}
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
    body: bytes | str | None, *, allow_plain_text: bool = True, provider: str = ""
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
    return _error_from_payload(payload, provider=provider)


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


def _error_from_payload(payload: Any, *, provider: str = "") -> tuple[str | None, str | None]:
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
    status = str(payload.get("status", "")).lower()
    if status in _FAILED_STATUSES or (provider == "hifly" and status == "4"):
        return None, _clean_message(generic) or "服务商返回任务失败，未附原因"
    if isinstance(detail, Mapping):
        return _error_from_payload(detail, provider=provider)
    for nested_key in ("data", "result", "output"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            nested_code, nested_message = _error_from_payload(nested, provider=provider)
            if nested_code or nested_message:
                return nested_code, nested_message
    return None, None


def _clean_code(code: Any) -> str | None:
    if code is None or code == "":
        return None
    return redact_text(str(code))[:120]


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
    response_truncated: bool
    poll_state: str | None
    provider_error_code: str | None
    provider_message: str | None
    provider_task_id: str | None
    provider_request_id: str | None
    error_code: str | None
    error_message: str | None
    exception_type: str | None
    model: str | None
    context: CallContext | None
    request_id: str


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
    exception_type: str | None = None,
    model: str | None = None,
) -> PreparedCall:
    raw_bytes: int | None = None
    body_text: str | None = None
    truncated = False
    if response_body is not None:
        raw_bytes = (
            len(response_body)
            if isinstance(response_body, bytes)
            else len(response_body.encode("utf-8"))
        )
        if not binary_response:
            body_text = redact_text(_decode(response_body) or "")
            if outcome == "SUCCEEDED":
                truncated = len(body_text.encode("utf-8")) > SUCCEEDED_BODY_LIMIT
                body_text = _utf8_excerpt(body_text, SUCCEEDED_BODY_LIMIT)
    code, message = (None, None)
    if outcome != "SUCCEEDED" and not binary_response:
        code, message = parse_provider_error(response_body, provider=provider)
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
        response_truncated=truncated,
        poll_state=_pending_poll_state(response_body, provider=provider)
        if method.upper() == "GET" and outcome == "SUCCEEDED"
        else None,
        provider_error_code=code,
        provider_message=message,
        provider_task_id=provider_task_id,
        provider_request_id=_provider_request_id(response_headers),
        error_code=error_code,
        error_message=_clean_message(error_message) if error_message else None,
        exception_type=exception_type,
        model=model or _MODEL.get(),
        context=current_call_context(),
        request_id=_request_id(),
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
    call_id = str(uuid4())
    try:
        _insert_with_id(call, call_id)
    finally:
        if (
            call.response_body is not None
            and call.outcome != "SUCCEEDED"
            and len(call.response_body.encode("utf-8")) > FAILED_BODY_LIMIT
        ):
            try:
                from app.db_pg import get_pg_pool

                with get_pg_pool().connection(timeout=LOG_CONNECTION_TIMEOUT_SECONDS) as conn:
                    _resolve_pending_response_object(conn, call_id)
            except Exception as exc:  # noqa: BLE001 — 登记仍保留，数据库恢复后可重试
                logger.warning("external response cleanup deferred: %s", type(exc).__name__)


def _insert_with_id(call: PreparedCall, call_id: str) -> None:
    from app.db_pg import get_pg_pool
    from app.db_portable import BusinessConnection
    from app.media_routes import get_media_storage

    context = call.context
    body = call.response_body
    storage_uri = None
    pool = get_pg_pool()
    with pool.connection(timeout=LOG_CONNECTION_TIMEOUT_SECONDS) as conn, conn.transaction():
        # 指标逐次记录，不能让合并后的长轮询低估调用数和失败率分母。
        conn.execute(
            "INSERT INTO external_call_observations (id, provider, outcome, latency_ms) "
            "VALUES (%s, %s, %s, %s)",
            (call_id, call.provider, call.outcome, call.latency_ms),
        )
        if context is not None and call.poll_state is not None:
            # 同任务同接口的轮询串行比较最近状态；只压缩连续等待，不合并失败或提交请求。
            lock_key = f"external-poll:{context.task_type}:{context.task_id}:{call.endpoint}"
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
            previous = conn.execute(
                "SELECT id, poll_state, url_redacted, http_status, attempt FROM external_call_logs "
                "WHERE task_type = %s AND task_id = %s AND provider = %s AND endpoint_name = %s "
                "AND method = 'GET' ORDER BY created_at::timestamptz DESC, id DESC LIMIT 1",
                (context.task_type, context.task_id, call.provider, call.endpoint),
            ).fetchone()
            if previous is not None and tuple(previous[1:]) == (
                call.poll_state,
                call.url_redacted,
                call.http_status,
                context.attempt,
            ):
                conn.execute(
                    "UPDATE external_call_logs SET poll_count = poll_count + 1, "
                    "last_seen_at = clock_timestamp() WHERE id = %s",
                    (previous[0],),
                )
                return
        if (
            body is not None
            and call.outcome != "SUCCEEDED"
            and len(body.encode()) > FAILED_BODY_LIMIT
        ):
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"external-response:{call_id}",),
            )
            try:
                with conn.transaction():
                    storage = get_media_storage(BusinessConnection.postgres(conn))
                    key = f"diagnostics/external-calls/{call_id}.txt"
                    expected_uri = f"{storage.cache_namespace.rstrip('/')}/{key}"
                    # 独立提交登记后才允许写对象；主日志事务回滚不能抹掉回收线索。
                    with pool.connection(timeout=LOG_CONNECTION_TIMEOUT_SECONDS) as pending_conn:
                        with pending_conn.transaction():
                            pending_conn.execute(
                                "INSERT INTO external_call_response_pending (call_id, storage_uri) "
                                "VALUES (%s, %s)",
                                (call_id, expected_uri),
                            )
                    stored = storage.put_object(
                        key,
                        body.encode("utf-8"),
                        content_type="text/plain; charset=utf-8",
                    )
                storage_uri = stored.uri
                body = _utf8_excerpt(body, FAILED_BODY_LIMIT)
            except Exception as exc:  # noqa: BLE001 — 外存暂不可用也不能丢失失败证据
                logger.warning("external response storage unavailable: %s", type(exc).__name__)
                # PostgreSQL 保留脱敏全文作兜底；恢复外存前不会悄悄丢掉尾部。
        conn.execute(
            """
            INSERT INTO external_call_logs (
                id, provider, model, endpoint_name, method, url_redacted,
                request_summary_json, http_status, latency_ms, outcome,
                response_headers_json, response_body, response_body_bytes,
                provider_error_code, provider_message, provider_task_id,
                provider_request_id, error_code, error_message_redacted,
                task_type, task_id, attempt, request_id, response_storage_uri, response_truncated,
                poll_state, exception_type
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                call_id,
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
                body,
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
                call.request_id,
                storage_uri,
                call.response_truncated,
                call.poll_state,
                call.exception_type,
            ),
        )
        if storage_uri is not None:
            conn.execute(
                "DELETE FROM external_call_response_pending WHERE call_id = %s", (call_id,)
            )


def _resolve_pending_response_object(conn: psycopg.Connection, call_id: str) -> bool:
    from app.content_store import delete_object_outside_content_namespace
    from app.db_portable import BusinessConnection
    from app.media_routes import storage_for_asset
    from app.storage import storage_object_ref_from_uri

    # 只回收已退出写入事务的对象；超时或进程暂停不能让清理与提交同时进行。
    lock = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"external-response:{call_id}",),
    ).fetchone()
    if lock is None or not lock[0]:
        return False
    pending = conn.execute(
        "SELECT storage_uri FROM external_call_response_pending WHERE call_id = %s",
        (call_id,),
    ).fetchone()
    if pending is None:
        return False
    uri = str(pending[0])
    referenced = conn.execute(
        "SELECT 1 FROM external_call_logs WHERE response_storage_uri = %s",
        (uri,),
    ).fetchone()
    if referenced is None:
        storage = storage_for_asset(BusinessConnection.postgres(conn), uri)
        if not delete_object_outside_content_namespace(
            storage, storage_object_ref_from_uri(uri).key
        ):
            return False
    conn.execute("DELETE FROM external_call_response_pending WHERE call_id = %s", (call_id,))
    return True


def resolve_pending_response_objects(
    conn: psycopg.Connection,
    *,
    now: datetime,
    batch_size: int = PURGE_BATCH_SIZE,
) -> int:
    """人工维护入口：只处理一小时前的登记，不干扰仍在写入的请求。未接入生产定时器。"""
    rows = conn.execute(
        "SELECT call_id FROM external_call_response_pending WHERE created_at < %s "
        "AND (cleanup_retry_at IS NULL OR cleanup_retry_at <= %s) "
        "ORDER BY created_at, call_id LIMIT %s",
        (now - timedelta(hours=1), now, batch_size),
    ).fetchall()
    resolved = 0
    for row in rows:
        try:
            with conn.transaction():
                done = _resolve_pending_response_object(conn, str(row[0]))
                resolved += int(done)
                if not done:
                    conn.execute(
                        "UPDATE external_call_response_pending SET cleanup_retry_at = %s "
                        "WHERE call_id = %s",
                        (now + timedelta(hours=1), row[0]),
                    )
        except Exception as exc:  # noqa: BLE001 — 对象服务暂不可用时保留登记等待重试
            logger.warning("external response cleanup deferred: %s", type(exc).__name__)
            conn.execute(
                "UPDATE external_call_response_pending SET cleanup_retry_at = %s "
                "WHERE call_id = %s",
                (now + timedelta(hours=1), row[0]),
            )
    return resolved


def _utf8_excerpt(text: str, byte_limit: int) -> str:
    # 从完整字符边界截取，避免中文摘要超出字节上限或尾部变成乱码。
    return text.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")


def _pending_poll_state(body: bytes | str | None, *, provider: str = "") -> str | None:
    try:
        value = json.loads(_decode(body) or "")
    except ValueError:
        return None
    if not isinstance(value, Mapping):
        return None
    for scope in (value, value.get("data"), value.get("result")):
        if not isinstance(scope, Mapping):
            continue
        state = str(scope.get("status") or scope.get("state") or "").lower()
        # 数字状态只按已有数字人协议解释，不能把其他接口的 status=1 猜成等待。
        if provider == "hifly" and state in {"1", "2"}:
            return "queued" if state == "1" else "running"
        if state in {"pending", "queued", "running", "processing", "submitted", "waiting"}:
            return state
    return None


# 超期判定：``created_at`` 是文本列（001 迁移），PG 默认值写成带时区的时间文本，
# 转 timestamptz 后与 PG 时钟算出的截止时刻比较。``outcome`` 为空的是 P0-9 之前的
# 旧行，无法判断成败，按失败对待——宁可多留，不误删可能的失败证据。
_EXPIRED_PREDICATE = """
    created_at::timestamptz < CASE
        WHEN outcome = 'SUCCEEDED' THEN %s::timestamptz
        ELSE %s::timestamptz
    END
"""


def _retention_cutoffs(now: datetime) -> tuple[datetime, datetime]:
    """(成功调用截止时刻, 失败调用截止时刻)；早于截止时刻的行算超期。"""
    return (
        now - timedelta(days=SUCCEEDED_RETENTION_DAYS),
        now - timedelta(days=FAILED_RETENTION_DAYS),
    )


def count_expired_calls(
    conn: psycopg.Connection, *, now: datetime, ready_only: bool = False
) -> int:
    """超过保留期的调用日志行数（``--dry-run`` 用，不改任何数据）。"""
    succeeded_cutoff, failed_cutoff = _retention_cutoffs(now)
    retry_predicate = (
        " AND (cleanup_retry_at IS NULL OR cleanup_retry_at <= %s)" if ready_only else ""
    )
    params = (
        (succeeded_cutoff, failed_cutoff, now) if ready_only else (succeeded_cutoff, failed_cutoff)
    )
    row = conn.execute(
        f"SELECT count(*) FROM external_call_logs WHERE ({_EXPIRED_PREDICATE}){retry_predicate}",
        params,
    ).fetchone()
    return int(row[0]) if row is not None else 0


def purge_expired_call_batch(
    conn: psycopg.Connection, *, now: datetime, batch_size: int = PURGE_BATCH_SIZE
) -> int:
    """删一批超期调用日志，返回本批删除的行数；返回 0 表示已清完。

    调用方在独立事务里循环调用直到返回 0：每批单独提交，长时间清理也不会
    持有一个长事务。幂等——重复执行只会找不到可删的行。
    """
    succeeded_cutoff, failed_cutoff = _retention_cutoffs(now)
    from app.content_store import delete_object_outside_content_namespace
    from app.db_portable import BusinessConnection
    from app.media_routes import storage_for_asset
    from app.storage import storage_object_ref_from_uri

    candidates = conn.execute(
        f"""
        SELECT id, response_storage_uri FROM external_call_logs
        WHERE ({_EXPIRED_PREDICATE}) AND (cleanup_retry_at IS NULL OR cleanup_retry_at <= %s)
        ORDER BY created_at, id LIMIT %s
        """,
        (succeeded_cutoff, failed_cutoff, now, batch_size),
    ).fetchall()
    removed: list[str] = []
    for row in candidates:
        call_id, uri = row[0], row[1]
        if uri:
            try:
                storage = storage_for_asset(BusinessConnection.postgres(conn), str(uri))
                if not delete_object_outside_content_namespace(
                    storage, storage_object_ref_from_uri(str(uri)).key
                ):
                    raise ValueError("storage deletion gate retained the response object")
            except Exception as exc:  # noqa: BLE001 — 引用保留至下次重试，避免遗留失联对象
                logger.warning("external response cleanup deferred: %s", type(exc).__name__)
                conn.execute(
                    "UPDATE external_call_logs SET cleanup_retry_at = %s WHERE id = %s",
                    (now + timedelta(hours=1), call_id),
                )
                continue
        removed.append(str(call_id))
    if not removed:
        return 0
    return int(
        conn.execute("DELETE FROM external_call_logs WHERE id = ANY(%s)", (removed,)).rowcount
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


def _is_business_failure(body: bytes | str | None, *, provider: str = "") -> bool:
    """HTTP 200 但业务失败：视频任务的失败原因就在这类响应里。"""
    code, message = parse_provider_error(body, allow_plain_text=False, provider=provider)
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
    expected_json: bool = False,
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
            # 失败正文需要全文留存，脱敏后按大小转存，不能在传输入口先丢掉尾部。
            error_body = exc.read()
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
            exception_type=type(exc).__name__,
        )
        raise exc
    except URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        record_external_call(
            **common,
            outcome="TIMEOUT" if timed_out else "NETWORK_ERROR",
            latency_ms=timer.elapsed_ms(),
            error_message=type(exc.reason).__name__ if exc.reason else type(exc).__name__,
            exception_type=type(exc.reason).__name__ if exc.reason else type(exc).__name__,
        )
        raise
    except OSError as exc:
        record_external_call(
            **common,
            outcome="NETWORK_ERROR",
            latency_ms=timer.elapsed_ms(),
            error_message=type(exc).__name__,
            exception_type=type(exc).__name__,
        )
        raise
    binary_response = _is_binary_content(headers)
    failed = not binary_response and _is_business_failure(body, provider=provider)
    parse_error = False
    if expected_json and not binary_response:
        try:
            json.loads(body)
        except (ValueError, UnicodeError):
            parse_error = True
    record_external_call(
        **common,
        outcome="PARSE_ERROR" if parse_error else "PROVIDER_ERROR" if failed else "SUCCEEDED",
        http_status=status,
        response_headers=headers,
        response_body=body,
        binary_response=binary_response,
        latency_ms=timer.elapsed_ms(),
        provider_task_id=None if binary_response else provider_task_id_from(body),
        error_message="服务响应不是有效 JSON" if parse_error else None,
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
