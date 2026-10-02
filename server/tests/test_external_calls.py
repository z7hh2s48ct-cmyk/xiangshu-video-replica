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


@pytest.mark.parametrize(
    ("provider", "path"),
    [
        ("hifly", "/api/v2/hifly/avatar/create_by_video"),
        ("hifly", "/api/v2/hifly/avatar/task"),
        ("hifly", "/api/v2/hifly/voice/create"),
        ("hifly", "/api/v2/hifly/voice/task"),
        ("hifly", "/api/v2/hifly/video/create_by_audio"),
        ("hifly", "/api/v2/hifly/video/task"),
        ("asr", "/api/v1/services/audio/asr/transcription"),
        ("asr", "/api/v1/services/aigc/multimodal-generation/generation"),
        ("asr", "/api/v1/tasks/synthetic-task"),
    ],
)
def test_documented_body_receipt_is_distinct_from_task_and_our_request(
    provider: str, path: str
) -> None:
    body = json.dumps({"request_id": "synthetic-provider-receipt", "task_id": "synthetic-task"})
    with external_call_context(task_type="ORAL_VIDEO", task_id="our-task", attempt=2):
        call = prepare_external_call(
            provider=provider,
            endpoint=path,
            method="POST",
            url=f"https://provider.invalid{path}",
            outcome="SUCCEEDED",
            response_body=body,
            provider_task_id="synthetic-task",
        )
    assert call.provider_request_id == "synthetic-provider-receipt"
    assert call.provider_task_id == "synthetic-task"
    assert call.request_id != call.provider_request_id
    assert call.context is not None and call.context.task_id == "our-task"


@pytest.mark.parametrize(
    "value", [None, 123, True, [], {}, "", "a" * 201, "bad\nreceipt", "bad receipt"]
)
def test_invalid_body_receipts_are_not_truncated_or_invented(value: object) -> None:
    call = prepare_external_call(
        provider="hifly",
        endpoint="hifly/video/task",
        method="GET",
        url="https://provider.invalid/api/v2/hifly/video/task",
        outcome="PROVIDER_ERROR",
        response_body=json.dumps({"request_id": value, "task_id": "not-a-receipt"}),
    )
    assert call.provider_request_id is None


@pytest.mark.parametrize(
    ("provider", "path", "body"),
    [
        ("other", "/api/v2/hifly/video/task", {"request_id": "unproven"}),
        ("hifly", "/unverified", {"request_id": "unproven"}),
        ("asr", "/api/v1/tasks/a/extra", {"request_id": "unproven"}),
        ("hifly", "/api/v2/hifly/video/task", {"data": {"request_id": "unproven"}}),
        ("hifly", "/api/v2/hifly/video/task", {"id": "completion-id", "task_id": "task-id"}),
    ],
)
def test_body_receipt_requires_known_top_level_contract(
    provider: str, path: str, body: object
) -> None:
    call = prepare_external_call(
        provider=provider,
        endpoint=path,
        method="GET",
        url=f"https://provider.invalid{path}",
        outcome="SUCCEEDED",
        response_body=json.dumps(body),
    )
    assert call.provider_request_id is None


def test_distinct_header_and_body_receipts_keep_both_original_evidence() -> None:
    body = json.dumps({"request_id": "body-receipt", "message": "x" * (SUCCEEDED_BODY_LIMIT + 100)})
    call = prepare_external_call(
        provider="hifly",
        endpoint="hifly/video/task",
        method="GET",
        url="https://provider.invalid/api/v2/hifly/video/task",
        outcome="SUCCEEDED",
        response_headers={"X-Request-Id": "header-receipt"},
        response_body=body,
    )
    assert call.provider_request_id == "header-receipt"
    assert call.response_headers == {"x-request-id": "header-receipt"}
    assert call.response_body is not None and "body-receipt" in call.response_body
    assert call.response_truncated is True
    body_only = prepare_external_call(
        provider="hifly",
        endpoint="hifly/video/task",
        method="GET",
        url="https://provider.invalid/api/v2/hifly/video/task",
        outcome="SUCCEEDED",
        response_body=body,
    )
    assert body_only.provider_request_id == "body-receipt"


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
    "key",
    ["signature", "authorization", "secret", "clientSecret", "apiKey", "accessKeyId", "cookie"],
)
def test_redaction_handles_nested_json_and_short_credentials(key: str) -> None:
    text = json.dumps({"data": [{key: "X7", "message": "原始失败原因", "prompt": "客户提示词"}]})
    redacted = redact_text(text)
    assert "X7" not in redacted
    assert json.loads(redacted)["data"][0][key] == "[已脱敏]"
    assert "原始失败原因" in redacted
    assert "客户提示词" in redacted
    assert "X7" not in redact_text(f"{key}=X7 other=ok")
    assert "X7" not in redact_url(f"https://example.test/a?{key}=X7&page=1")


def test_redaction_tolerates_invalid_url_ports_and_preserves_ipv6() -> None:
    assert "X7" not in redact_text("https://example.test:bad/a?token=X7")
    assert redact_url("https://[::1]:8000/a?page=2") == "https://[::1]:8000/a?page=2"


@pytest.mark.parametrize(
    "text",
    [
        'error={"token":"FICTIONAL-WRAPPED-CRED"}',
        'message="upstream token=FICTIONAL-WRAPPED-CRED failed"',
        json.dumps({"message": '{"token":"FICTIONAL-WRAPPED-CRED","reason":"失败"}'}),
        r'error="{\"token\":\"FICTIONAL-WRAPPED-CRED\"}"',
        'message="failure" error={"data":{"clientSecret":"FICTIONAL-WRAPPED-CRED"}}',
    ],
)
def test_wrapped_text_and_embedded_json_do_not_hide_credentials(text: str) -> None:
    redacted = redact_text(text)
    assert "FICTIONAL-WRAPPED-CRED" not in redacted
    assert "[已脱敏]" in redacted


@pytest.mark.parametrize("header", ["Cookie", "Set-Cookie"])
def test_cookie_headers_mask_all_values_and_preserve_surrounding_lines(header: str) -> None:
    redacted = redact_text(f"safe before\n{header}: sid=FAKE_A; other=FAKE_B\nsafe after")
    assert "FAKE_A" not in redacted and "FAKE_B" not in redacted
    assert "safe before" in redacted and "safe after" in redacted


@pytest.mark.parametrize("escaping", [1, 2, 3])
def test_escaped_quoted_credentials_mask_spaces_and_preserve_safe_fields(escaping: int) -> None:
    quote = "\\" * escaping + '"'
    text = f"safe before error={{{quote}token{quote}:{quote}FAKE_A FAKE_B{quote}}} safe after"
    redacted = redact_text(text)
    assert "FAKE_A" not in redacted and "FAKE_B" not in redacted
    assert "safe before" in redacted and "safe after" in redacted


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
    assert summary["image"] == {"media_reference": "inline_base64", "characters": 5000}
    assert summary["api_key"] == "[已脱敏]"


def test_long_prompts_and_copy_keep_the_tail_but_inline_media_do_not() -> None:
    prompt = "完整提示词 " * 1800 + " tail marker https://cdn.test/a?Signature=FICTIONAL-SIGNATURE"
    summary = summarize_request_body(
        json.dumps(
            {"prompt": prompt, "text": "A" * 8000, "image": "data:image/png;base64," + "A" * 5000}
        )
    )
    assert "tail marker" in summary["prompt"]
    assert "FICTIONAL-SIGNATURE" not in summary["prompt"]
    assert summary["text"] == "A" * 8000
    assert summary["image"]["media_reference"] == "data:image/png;base64"


@pytest.mark.parametrize("wrapped", [False, True])
def test_timeout_records_exception_type_and_latency(captured: list[Any], wrapped: bool) -> None:
    def fail(_request: Any, timeout: float) -> Any:
        error = TimeoutError("FICTIONAL-SENSITIVE-TIMEOUT-MESSAGE")
        raise URLError(error) if wrapped else error

    with pytest.raises((TimeoutError, URLError)):
        recorded_urlopen(
            "https://api.test/tasks", timeout=3, provider="apilio", endpoint="poll", opener=fail
        )
    [call] = captured
    assert call.outcome == "TIMEOUT"
    assert call.exception_type == "TimeoutError"
    assert call.latency_ms is not None and call.latency_ms >= 0
    assert "FICTIONAL-SENSITIVE-TIMEOUT-MESSAGE" not in (call.error_message or "")


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
    assert failed.response_body == body
    assert failed.response_truncated is False
    assert succeeded.response_body is not None
    assert len(succeeded.response_body) == SUCCEEDED_BODY_LIMIT
    assert succeeded.response_truncated is True
    assert failed.response_body_bytes == len(body)
    assert failed.method == "POST"


def test_success_summary_uses_utf8_bytes_and_does_not_mislabel_redaction() -> None:
    call = prepare_external_call(
        provider="p",
        endpoint="e",
        method="GET",
        url=None,
        outcome="SUCCEEDED",
        response_body="中文" * 4000,
    )
    assert call.response_body is not None
    assert len(call.response_body.encode("utf-8")) <= SUCCEEDED_BODY_LIMIT
    assert call.response_truncated is True
    redacted = prepare_external_call(
        provider="p",
        endpoint="e",
        method="GET",
        url=None,
        outcome="PROVIDER_ERROR",
        response_body=json.dumps({"secret": "fictional-long-secret"}),
    )
    assert redacted.response_truncated is False


@pytest.mark.parametrize("transport_name", ["video", "oral"])
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("wrapped", [False, True, "cookie", "escape"])
def test_http_error_service_logs_never_contain_echoed_credentials(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    transport_name: str,
    status: int,
    wrapped: bool | str,
) -> None:
    from app.generation import H3ProviderFailed, UrllibMetasoHttpTransport
    from app.hifly import HiflyError, UrllibHiflyHttpTransport

    echoed = json.dumps(
        {
            "message": "虚构上游失败",
            "signature": "FICTIONAL-SIGN-7",
            "data": {"authorization": "FICTIONAL-AUTH-8", "clientSecret": "FICTIONAL-SECRET-9"},
        },
        ensure_ascii=False,
    ).encode()
    if wrapped:
        echoed = b"error=" + echoed + b' message="token=FICTIONAL-WRAPPED-CRED failed"'
    if wrapped == "cookie":
        echoed += b"\nCookie: sid=FICTIONAL-COOKIE-A; other=FICTIONAL-COOKIE-B\nsafe after"
    if wrapped == "escape":
        quote = "\\" * 3 + '"'
        escaped_error = (
            f" error={{{quote}token{quote}:{quote}FICTIONAL-ESCAPE-A FICTIONAL-ESCAPE-B{quote}}}"
        )
        echoed += escaped_error.encode()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise HTTPError("https://fake.test/task", status, "fake", None, io.BytesIO(echoed))

    module = "app.generation" if transport_name == "video" else "app.hifly"
    monkeypatch.setattr(f"{module}.recorded_urlopen", fail)
    transport = (
        UrllibMetasoHttpTransport() if transport_name == "video" else UrllibHiflyHttpTransport()
    )
    with pytest.raises((H3ProviderFailed, HiflyError)):
        transport.request("GET", "https://fake.test/task", headers={})
    assert "虚构上游失败" in caplog.text
    for secret in [
        "FICTIONAL-SIGN-7",
        "FICTIONAL-AUTH-8",
        "FICTIONAL-SECRET-9",
        "FICTIONAL-WRAPPED-CRED",
        "FICTIONAL-COOKIE-A",
        "FICTIONAL-COOKIE-B",
        "FICTIONAL-ESCAPE-A",
        "FICTIONAL-ESCAPE-B",
    ]:
        assert secret not in caplog.text


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
    "body, provider, outcome",
    [
        (
            '{"code":0,"data":{"status":4,"message":"虚构数字状态失败原话"}}',
            "hifly",
            "PROVIDER_ERROR",
        ),
        ('{"code":0,"data":{"status":3,"message":"done"}}', "hifly", "SUCCEEDED"),
        ('{"status":4,"message":"unrelated numeric status"}', "other", "SUCCEEDED"),
        ("not-json token=FICTIONAL-BODY-CRED", "hifly", "PARSE_ERROR"),
    ],
)
def test_recorded_json_api_classifies_numeric_failures_and_invalid_json(
    captured: list[Any],
    body: str,
    provider: str,
    outcome: str,
) -> None:
    result, _, _ = recorded_urlopen(
        Request("https://fake.test/task"),
        timeout=5,
        provider=provider,
        endpoint="task",
        expected_json=True,
        opener=lambda request, timeout: _FakeResponse(body.encode("utf-8")),
    )
    assert result == body.encode("utf-8")
    [call] = captured
    assert call.outcome == outcome
    assert "FICTIONAL-BODY-CRED" not in (call.response_body or "")
    if outcome == "PROVIDER_ERROR":
        assert call.provider_message == "虚构数字状态失败原话"


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
