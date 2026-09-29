"""第三方接口调用日志（方案 P0-9 / P0-12）：脱敏、错误解析与记录版 urlopen。

真 PG 落库另见 test_external_calls_pg.py；这里只验证纯逻辑，不连数据库。
"""

from __future__ import annotations

import io
import json
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from app import external_calls
from app.external_calls import (
    FAILED_BODY_LIMIT,
    SUCCEEDED_BODY_LIMIT,
    external_call_context,
    parse_provider_error,
    prepare_external_call,
    provider_task_id_from,
    recorded_urlopen,
    redact_text,
    redact_url,
    summarize_request_body,
)


def test_redact_url_masks_signature_parameters_and_credentials() -> None:
    url = (
        "https://user:pass@bucket.cos.example.com/a/b.mp4"
        "?q-sign-algorithm=sha1&q-signature=abcdef123456&size=720&token=zzz"
    )
    redacted = redact_url(url)
    assert "abcdef123456" not in redacted
    assert "zzz" not in redacted
    assert "pass" not in redacted
    assert "size=720" in redacted
    assert redacted.startswith("https://bucket.cos.example.com/a/b.mp4?")


def test_redact_text_masks_bearer_tokens_and_secret_fields() -> None:
    text = (
        'Authorization: Bearer sk-live-1234567890abcdef {"api_key": "sk-abcdefgh1234"} '
        "see https://x.example.com/v.mp4?Signature=SIG123456"
    )
    redacted = redact_text(text)
    assert "sk-live-1234567890abcdef" not in redacted
    assert "sk-abcdefgh1234" not in redacted
    assert "SIG123456" not in redacted


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"error": {"code": "insufficient_quota", "message": "quota exceeded"}},
            ("insufficient_quota", "quota exceeded"),
        ),
        ({"code": 40012, "msg": "voice sample too short"}, ("40012", "voice sample too short")),
        (
            {"code": 0, "msg": "ok", "data": {"status": "failed", "fail_reason": "审核未通过"}},
            (None, "审核未通过"),
        ),
        ({"task_id": "t1", "status": "failed"}, (None, "服务商返回任务失败，未附原因")),
        ({"code": 200, "message": "success", "data": {"items": []}}, (None, None)),
        ({"status": "queued", "message": "task created"}, (None, None)),
        ({"detail": {"code": "BAD", "message": "bad input"}}, ("BAD", "bad input")),
    ],
)
def test_parse_provider_error_only_reports_explicit_failures(
    body: dict[str, Any], expected: tuple[str | None, str | None]
) -> None:
    assert parse_provider_error(json.dumps(body)) == expected


def test_plain_text_is_only_a_reason_for_known_failures() -> None:
    assert parse_provider_error(b"Bad Gateway") == (None, "Bad Gateway")
    assert parse_provider_error(b"Bad Gateway", allow_plain_text=False) == (None, None)


def test_provider_task_id_is_read_from_top_level_or_data() -> None:
    assert provider_task_id_from(b'{"task_id": "vt_1"}') == "vt_1"
    assert provider_task_id_from(b'{"code": 0, "data": {"taskId": 42}}') == "42"
    assert provider_task_id_from(b"not json") is None


def test_request_summary_drops_inline_media_and_secrets() -> None:
    summary = summarize_request_body(
        json.dumps({"prompt": "秋日街头", "image": "A" * 5000, "api_key": "sk-123456789"})
    )
    assert summary["prompt"] == "秋日街头"
    assert summary["image"] == "[已省略 5000 个字符]"
    assert summary["api_key"] == "[已脱敏]"


def test_failed_calls_keep_more_body_than_successful_ones() -> None:
    body = "x" * (FAILED_BODY_LIMIT + 10)
    failed = prepare_external_call(
        provider="p",
        endpoint="e",
        method="post",
        url=None,
        outcome="PROVIDER_ERROR",
        response_body=body,
    )
    succeeded = prepare_external_call(
        provider="p",
        endpoint="e",
        method="post",
        url=None,
        outcome="SUCCEEDED",
        response_body=body,
    )
    assert failed.response_body is not None and len(failed.response_body) == FAILED_BODY_LIMIT
    assert succeeded.response_body is not None
    assert len(succeeded.response_body) == SUCCEEDED_BODY_LIMIT
    assert failed.response_body_bytes == len(body)
    assert failed.method == "POST"


def test_context_binds_task_and_restores_outer_scope() -> None:
    with external_call_context("VIDEO", "task-1", attempt=2):
        inner = prepare_external_call(
            provider="p", endpoint="e", method="GET", url=None, outcome="SUCCEEDED"
        )
        with external_call_context("ANALYSIS", "task-2"):
            nested = prepare_external_call(
                provider="p", endpoint="e", method="GET", url=None, outcome="SUCCEEDED"
            )
    outside = prepare_external_call(
        provider="p", endpoint="e", method="GET", url=None, outcome="SUCCEEDED"
    )
    assert inner.context is not None and (inner.context.task_id, inner.context.attempt) == (
        "task-1",
        2,
    )
    assert nested.context is not None and nested.context.task_type == "ANALYSIS"
    assert outside.context is None


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["X-Request-Id"] = "up-123"
        self.headers["Set-Cookie"] = "session=secret"


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(external_calls, "_insert", calls.append)
    return calls


def test_recorded_urlopen_records_business_failure_inside_http_200(captured: list[Any]) -> None:
    body = json.dumps(
        {
            "task_id": "vt_9",
            "status": "failed",
            "error": {"code": "CONTENT_REJECTED", "message": "portrait rights"},
        }
    ).encode()
    request = Request("https://api.example.com/v1/tasks/vt_9?sign=abc123456", method="GET")
    with external_call_context("VIDEO", "task-9"):
        result, headers, status = recorded_urlopen(
            request,
            timeout=5,
            provider="video",
            endpoint="tasks",
            opener=lambda _request, timeout: _FakeResponse(body),
        )
    assert (result, status) == (body, 200)
    assert headers["X-Request-Id"] == "up-123"
    [call] = captured
    assert call.outcome == "PROVIDER_ERROR"
    assert call.provider_error_code == "CONTENT_REJECTED"
    assert call.provider_message == "portrait rights"
    assert call.provider_task_id == "vt_9"
    assert call.provider_request_id == "up-123"
    assert "set-cookie" not in (call.response_headers or {})
    assert "abc123456" not in (call.url_redacted or "")
    assert call.context is not None and call.context.task_id == "task-9"


def test_recorded_urlopen_keeps_http_error_body_readable(captured: list[Any]) -> None:
    def failing(_request: Any, timeout: float) -> Any:
        raise HTTPError(
            "https://api.example.com/v1/images",
            402,
            "Payment Required",
            Message(),
            io.BytesIO(b'{"error": {"code": "insufficient_quota", "message": "quota exceeded"}}'),
        )

    request = Request("https://api.example.com/v1/images", data=b'{"n": 5}', method="POST")
    with pytest.raises(HTTPError) as raised:
        recorded_urlopen(request, timeout=5, provider="image", endpoint="images", opener=failing)
    # 调用方原有的 exc.read() 仍然读得到响应体。
    assert b"quota exceeded" in raised.value.read()
    [call] = captured
    assert (call.outcome, call.http_status) == ("PROVIDER_ERROR", 402)
    assert call.provider_error_code == "insufficient_quota"
    assert call.request_summary == {"n": 5}


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (TimeoutError("timed out"), "TIMEOUT"),
        (URLError(TimeoutError("timed out")), "TIMEOUT"),
        (URLError(ConnectionRefusedError()), "NETWORK_ERROR"),
    ],
)
def test_recorded_urlopen_classifies_transport_failures(
    captured: list[Any], error: Exception, outcome: str
) -> None:
    def failing(_request: Any, timeout: float) -> Any:
        raise error

    with pytest.raises(type(error)):
        recorded_urlopen(
            Request("https://api.example.com/x"),
            timeout=3,
            provider="p",
            endpoint="x",
            opener=failing,
        )
    [call] = captured
    assert call.outcome == outcome
    assert call.response_body is None


def test_logging_failure_never_breaks_the_business_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_call: Any) -> None:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(external_calls, "_insert", broken)
    body, _headers, _status = recorded_urlopen(
        Request("https://api.example.com/x"),
        timeout=3,
        provider="p",
        endpoint="x",
        opener=lambda _request, timeout: _FakeResponse(b'{"ok": true}'),
    )
    assert body == b'{"ok": true}'
