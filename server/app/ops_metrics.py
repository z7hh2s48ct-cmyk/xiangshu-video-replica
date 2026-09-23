from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse

from app.auth_headers import bearer_token as _bearer_token

REQUEST_ID_HEADER = "X-Request-Id"
METRICS_TOKEN_FILE_ENV = "VIDEO_REPLICA_METRICS_TOKEN_FILE"
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
MAX_REQUEST_ID_LENGTH = 128
UNMATCHED_ROUTE_LABEL = "UNMATCHED"
OTHER_HTTP_METHOD_LABEL = "OTHER"
APPROVED_HTTP_METHOD_LABELS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)
APPROVED_TRACE_FIELDS = frozenset(
    {
        "actor_id",
        "user_id",
        "device_id",
        "session_id",
        "session_epoch",
        "task_id",
        "order_id",
        "code_mask",
    }
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    method: str
    route: str
    request: Request | None = None


_request_context_var: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "ops_metrics_request_context",
    default=None,
)
_result_code_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ops_metrics_result_code",
    default=None,
)
_trace_fields_var: contextvars.ContextVar[dict[str, str | int] | None] = contextvars.ContextVar(
    "ops_metrics_trace_fields",
    default=None,
)


def current_request_context() -> RequestContext | None:
    return _request_context_var.get()


def set_current_result_code(code: str) -> None:
    context = current_request_context()
    if context is not None and context.request is not None:
        context.request.state.result_code = code
    _result_code_var.set(code)


def get_current_result_code() -> str | None:
    context = current_request_context()
    if context is not None and context.request is not None:
        result_code = getattr(context.request.state, "result_code", None)
        if isinstance(result_code, str) and result_code:
            return result_code
    return _result_code_var.get()


def set_current_trace_fields(
    *,
    actor_id: str | None = None,
    user_id: str | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
    session_epoch: int | None = None,
    task_id: str | None = None,
    order_id: str | None = None,
    code_mask: str | None = None,
) -> None:
    """Attach the approved, non-secret business identifiers to this request.

    Callers opt in field-by-field; arbitrary dictionaries are deliberately not
    accepted, so tokens, signed URLs, provider payloads, and request bodies
    cannot accidentally be forwarded into the structured request log.
    """
    values: dict[str, str | int | None] = {
        "actor_id": actor_id,
        "user_id": user_id,
        "device_id": device_id,
        "session_id": session_id,
        "session_epoch": session_epoch,
        "task_id": task_id,
        "order_id": order_id,
        "code_mask": code_mask,
    }
    approved: dict[str, str | int] = {}
    for name, value in values.items():
        if value is None:
            continue
        if name == "session_epoch":
            if isinstance(value, int) and value >= 0:
                approved[name] = value
            continue
        text = str(value)
        if 0 < len(text) <= MAX_REQUEST_ID_LENGTH and all(
            ord(character) >= 32 for character in text
        ):
            approved[name] = text
    if not approved:
        return
    current = dict(_trace_fields_var.get() or {})
    current.update(approved)
    _trace_fields_var.set(current)
    context = current_request_context()
    if context is not None and context.request is not None:
        request_fields = dict(getattr(context.request.state, "observability_fields", {}) or {})
        request_fields.update(approved)
        context.request.state.observability_fields = request_fields


def get_current_trace_fields() -> dict[str, str | int]:
    context = current_request_context()
    if context is not None and context.request is not None:
        request_fields = getattr(context.request.state, "observability_fields", None)
        if isinstance(request_fields, dict):
            return {
                str(name): value
                for name, value in request_fields.items()
                if name in APPROVED_TRACE_FIELDS and isinstance(value, (str, int))
            }
    return dict(_trace_fields_var.get() or {})


def get_or_create_request_id(request: Request) -> str:
    existing = request.headers.get(REQUEST_ID_HEADER, "").strip()
    if _is_valid_request_id(existing):
        return existing
    return str(uuid.uuid4())


def _is_valid_request_id(value: str) -> bool:
    return bool(
        value
        and len(value) <= MAX_REQUEST_ID_LENGTH
        and all(33 <= ord(character) <= 126 for character in value)
    )


def attach_request_id(request: Request, request_id: str) -> None:
    scope_headers = list(request.scope.get("headers", []))
    header_name = REQUEST_ID_HEADER.lower().encode("latin-1")
    header_value = request_id.encode("latin-1")
    for index, (name, _value) in enumerate(scope_headers):
        if name == header_name:
            scope_headers[index] = (header_name, header_value)
            break
    else:
        scope_headers.append((header_name, header_value))
    request.scope["headers"] = scope_headers
    request.state.request_id = request_id


@contextmanager
def bind_request_context(
    *,
    request_id: str,
    method: str,
    route: str,
    request: Request | None = None,
) -> Iterator[None]:
    context_token = _request_context_var.set(
        RequestContext(request_id=request_id, method=method, route=route, request=request)
    )
    result_token = _result_code_var.set(None)
    trace_token = _trace_fields_var.set({})
    try:
        yield
    finally:
        _trace_fields_var.reset(trace_token)
        _result_code_var.reset(result_token)
        _request_context_var.reset(context_token)


class _MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._http_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self._http_duration_totals: dict[tuple[str, str, str], float] = defaultdict(float)
        self._fencing_rejects: dict[str, int] = defaultdict(int)
        self._fencing_lock_wait_count = 0
        self._fencing_lock_wait_total = 0.0

    def reset(self) -> None:
        with self._lock:
            self._http_counts.clear()
            self._http_duration_totals.clear()
            self._fencing_rejects.clear()
            self._fencing_lock_wait_count = 0
            self._fencing_lock_wait_total = 0.0

    def record_http_request(
        self,
        *,
        method: str,
        route: str,
        status: int,
        duration_seconds: float,
    ) -> None:
        key = (method, route, str(status))
        with self._lock:
            self._http_counts[key] += 1
            self._http_duration_totals[key] += max(duration_seconds, 0.0)

    def record_fencing_reject(self, *, code: str) -> None:
        with self._lock:
            self._fencing_rejects[code] += 1

    def observe_fencing_lock_wait(self, *, duration_seconds: float) -> None:
        with self._lock:
            self._fencing_lock_wait_count += 1
            self._fencing_lock_wait_total += max(duration_seconds, 0.0)

    def render(self, *, is_ready: bool) -> str:
        with self._lock:
            http_counts = dict(self._http_counts)
            http_duration_totals = dict(self._http_duration_totals)
            fencing_rejects = dict(self._fencing_rejects)
            fencing_lock_wait_count = self._fencing_lock_wait_count
            fencing_lock_wait_total = self._fencing_lock_wait_total

        lines = [
            (
                "# HELP video_replica_http_requests_total "
                "Total local HTTP requests handled by this process."
            ),
            "# TYPE video_replica_http_requests_total counter",
        ]
        for (method, route, status), count in sorted(http_counts.items()):
            lines.append(
                "video_replica_http_requests_total"
                f'{{method="{_escape_label(method)}",'
                f'route="{_escape_label(route)}",status="{status}"}} '
                f"{count}"
            )

        lines.extend(
            [
                (
                    "# HELP video_replica_http_request_duration_seconds_total "
                    "Total local HTTP request latency observed by this process."
                ),
                "# TYPE video_replica_http_request_duration_seconds_total counter",
            ]
        )
        for (method, route, status), total in sorted(http_duration_totals.items()):
            lines.append(
                "video_replica_http_request_duration_seconds_total"
                f'{{method="{_escape_label(method)}",'
                f'route="{_escape_label(route)}",status="{status}"}} '
                f"{total:.6f}"
            )

        lines.extend(
            [
                (
                    "# HELP video_replica_customer_fencing_rejects_total "
                    "Total customer fencing rejects observed by this process."
                ),
                "# TYPE video_replica_customer_fencing_rejects_total counter",
            ]
        )
        for code, count in sorted(fencing_rejects.items()):
            lines.append(
                "video_replica_customer_fencing_rejects_total"
                f'{{code="{_escape_label(code)}"}} {count}'
            )

        lines.extend(
            [
                (
                    "# HELP video_replica_fencing_lock_wait_seconds_total "
                    "Total observed session fencing verification wait time in this process."
                ),
                "# TYPE video_replica_fencing_lock_wait_seconds_total counter",
                f"video_replica_fencing_lock_wait_seconds_total {fencing_lock_wait_total:.6f}",
                (
                    "# HELP video_replica_fencing_lock_wait_seconds_count "
                    "Count of observed session fencing verification waits in this process."
                ),
                "# TYPE video_replica_fencing_lock_wait_seconds_count counter",
                f"video_replica_fencing_lock_wait_seconds_count {fencing_lock_wait_count}",
                "# HELP video_replica_ready Whether this process currently judges itself ready.",
                "# TYPE video_replica_ready gauge",
                f'video_replica_ready{{service="video-replica-api"}} {1 if is_ready else 0}',
            ]
        )
        return "\n".join(lines) + "\n"


_REGISTRY = _MetricsRegistry()


def reset_metrics_for_tests() -> None:
    _REGISTRY.reset()


def render_metrics_document(*, is_ready: bool) -> str:
    return _REGISTRY.render(is_ready=is_ready)


def render_metrics_document_for_tests(*, is_ready: bool) -> str:
    return render_metrics_document(is_ready=is_ready)


def record_fencing_reject(*, code: str, lock_wait_seconds: float) -> None:
    _REGISTRY.observe_fencing_lock_wait(duration_seconds=lock_wait_seconds)
    _REGISTRY.record_fencing_reject(code=code)


def observe_fencing_lock_wait(*, lock_wait_seconds: float) -> None:
    _REGISTRY.observe_fencing_lock_wait(duration_seconds=lock_wait_seconds)


def _record_http_request(*, method: str, route: str, status: int, duration_seconds: float) -> None:
    _REGISTRY.record_http_request(
        method=_metric_method_label(method),
        route=route,
        status=status,
        duration_seconds=duration_seconds,
    )


def _metric_method_label(method: str) -> str:
    normalized = method.upper()
    if normalized in APPROVED_HTTP_METHOD_LABELS:
        return normalized
    return OTHER_HTTP_METHOD_LABEL


def _resolve_route_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return UNMATCHED_ROUTE_LABEL


def current_route_label() -> str:
    """Resolve the matched route for logs emitted before middleware unwinds."""
    context = current_request_context()
    if context is None:
        return "-"
    if context.request is not None:
        return _resolve_route_label(context.request)
    return context.route


def _default_result_code(status_code: int) -> str:
    return "OK" if status_code < 400 else f"HTTP_{status_code}"


async def business_http_exception_handler(
    request: Request,
    error: HTTPException,
) -> Response:
    """Preserve stable business codes from legacy HTTPException helpers."""
    detail = error.detail
    if isinstance(detail, dict):
        code = detail.get("code")
        if isinstance(code, str) and code:
            set_current_result_code(code)
    return await http_exception_handler(request, error)


def _attach_route_trace_fields(request: Request) -> None:
    """Copy only approved path identifiers from the matched route.

    Starlette populates ``path_params`` during routing, so the outer HTTP
    middleware reads it after the endpoint returns. Activation-code path ids
    are deliberately excluded: only the already-masked code may be logged by
    an endpoint that has loaded the catalog row.
    """
    path_params = request.scope.get("path_params")
    if not isinstance(path_params, dict):
        return
    task_id = path_params.get("task_id")
    order_id = path_params.get("order_id", path_params.get("order_no"))
    set_current_trace_fields(
        task_id=task_id if isinstance(task_id, str) else None,
        order_id=order_id if isinstance(order_id, str) else None,
    )


def _safe_record_http_request(
    *,
    method: str,
    route: str,
    status: int,
    duration_seconds: float,
) -> None:
    try:
        _record_http_request(
            method=method,
            route=route,
            status=status,
            duration_seconds=duration_seconds,
        )
    except Exception as metrics_error:
        logger.warning("request metrics update failed (%s)", type(metrics_error).__name__)


def _safe_log_request(
    *,
    request_id: str,
    method: str,
    route: str,
    status: int,
    latency_ms: int,
    result_code: str,
    trace_fields: dict[str, str | int],
) -> None:
    try:
        payload: dict[str, str | int] = {
            "request_id": request_id,
            "method": method,
            "route": route,
            "operation": f"{method} {route}",
            "status": status,
            "latency_ms": max(latency_ms, 0),
            "result_code": result_code,
        }
        payload.update(trace_fields)
        logger.info(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except Exception as log_error:
        logger.warning("request log emission failed (%s)", type(log_error).__name__)


async def unhandled_exception_response(request: Request, _: Exception) -> Response:
    """Return FastAPI's redacted 500 body while preserving correlation."""
    response = PlainTextResponse("Internal Server Error", status_code=500)
    request_id = request.headers.get(REQUEST_ID_HEADER, "").strip()
    if _is_valid_request_id(request_id):
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


async def request_observability_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = get_or_create_request_id(request)
    attach_request_id(request, request_id)
    started = time.perf_counter()
    method = request.method
    initial_route = request.url.path or "/"
    with bind_request_context(
        request_id=request_id,
        method=method,
        route=initial_route,
        request=request,
    ):
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - started
            route = _resolve_route_label(request)
            _attach_route_trace_fields(request)
            status = 500
            _safe_record_http_request(
                method=method,
                route=route,
                status=status,
                duration_seconds=elapsed,
            )
            _safe_log_request(
                request_id=request_id,
                method=method,
                route=route,
                status=status,
                latency_ms=int(elapsed * 1000),
                result_code=get_current_result_code() or "UNHANDLED_EXCEPTION",
                trace_fields=get_current_trace_fields(),
            )
            raise
        elapsed = time.perf_counter() - started
        route = _resolve_route_label(request)
        _attach_route_trace_fields(request)
        status = response.status_code
        # Idempotent replays deliberately return the original operation's
        # durable request id. Prefer that response header so the HTTP log,
        # response, and persisted audit fact share one correlation key.
        replay_request_id = response.headers.get(REQUEST_ID_HEADER, "").strip()
        effective_request_id = (
            replay_request_id if _is_valid_request_id(replay_request_id) else request_id
        )
        response.headers[REQUEST_ID_HEADER] = effective_request_id
        _safe_record_http_request(
            method=method,
            route=route,
            status=status,
            duration_seconds=elapsed,
        )
        _safe_log_request(
            request_id=effective_request_id,
            method=method,
            route=route,
            status=status,
            latency_ms=int(elapsed * 1000),
            result_code=get_current_result_code() or _default_result_code(status),
            trace_fields=get_current_trace_fields(),
        )
        return response


def metrics_response(
    request: Request,
    *,
    readiness_check: Callable[[], bool],
) -> PlainTextResponse:
    expected_token = _load_metrics_token()
    presented_token = _bearer_token(request)
    if presented_token is None or not hmac.compare_digest(presented_token, expected_token):
        raise HTTPException(
            401,
            detail={
                "code": "METRICS_UNAUTHORIZED",
                "message": "Metrics authentication failed.",
            },
        )
    # Authentication must complete before a scrape can trigger PostgreSQL/COS
    # readiness work. This keeps the private endpoint cheap under probing and
    # prevents an unauthenticated caller from turning it into a dependency
    # oracle or load source.
    is_ready = readiness_check()
    return PlainTextResponse(
        render_metrics_document(is_ready=is_ready),
        media_type=PROMETHEUS_CONTENT_TYPE,
    )


def _load_metrics_token() -> str:
    token_file = os.environ.get(METRICS_TOKEN_FILE_ENV, "").strip()
    if not token_file:
        raise HTTPException(
            503,
            detail={
                "code": "METRICS_UNAVAILABLE",
                "message": "Metrics endpoint is unavailable.",
            },
        )
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        raise HTTPException(
            503,
            detail={
                "code": "METRICS_UNAVAILABLE",
                "message": "Metrics endpoint is unavailable.",
            },
        ) from None
    if not token:
        raise HTTPException(
            503,
            detail={
                "code": "METRICS_UNAVAILABLE",
                "message": "Metrics endpoint is unavailable.",
            },
        )
    return token


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
