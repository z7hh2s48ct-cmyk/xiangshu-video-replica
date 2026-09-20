"""拆解失败必须可诊断：上游拒绝的原话要留下来，且不能泄露签名地址。

生产事故背景：2026-09-20 云端 worker 打出
``Apilio video analysis request failed with HTTP status 400``，桌面端却只
显示「视频拆解服务返回了无法解析的结果，请重试或更换参考视频。」——五条
互不相干的失败路径共用这一句，落库的 ``error_message_redacted`` 存的也是
同一句，于是根因在 UI、数据库、日志三处同时消失。
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from urllib.error import HTTPError

import pytest

from app.analysis import (
    HTTP_FAILURE_PHASE,
    RESPONSE_FAILURE_PHASE,
    AnalysisProviderFailed,
    UrllibApilioChatTransport,
)
from app.analysis_routes import analysis_provider_error

SIGNED_URL = (
    "https://bucket.cos.ap-shanghai.myqcloud.com/verified-uploads/a/b/c.mp4"
    "?q-sign-algorithm=sha1&q-signature=deadbeefcafe&q-key-time=1;2"
)


def _http_error(status: int, body: str) -> HTTPError:
    return HTTPError(
        url="https://api.apilio.ai/v1/chat/completions",
        code=status,
        msg="Bad Request",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


class _RefusingUrlopen:
    """A urlopen stand-in that always raises the given HTTPError."""

    def __init__(self, error: HTTPError) -> None:
        self.error = error

    def __call__(self, request: object, timeout: float) -> object:
        raise self.error


def _post_and_capture(
    monkeypatch: pytest.MonkeyPatch, *, status: int, body: str
) -> AnalysisProviderFailed:
    monkeypatch.setattr("app.analysis.urlopen", _RefusingUrlopen(_http_error(status, body)))
    transport = UrllibApilioChatTransport()
    headers: Mapping[str, str] = {"Authorization": "Bearer secret-key"}
    with pytest.raises(AnalysisProviderFailed) as caught:
        transport.post("https://api.apilio.ai/v1/chat/completions", headers=headers, body=b"{}")
    return caught.value


def test_http_rejection_carries_the_upstream_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """400 的响应体里写着上游为什么拒，必须带出来而不是只留一个状态码。"""
    body = json.dumps({"error": {"message": "model gemini-3.1-pro-preview is not available"}})
    failure = _post_and_capture(monkeypatch, status=400, body=body)

    assert failure.http_status == 400
    assert failure.failure_phase == HTTP_FAILURE_PHASE
    assert "gemini-3.1-pro-preview is not available" in str(failure)


def test_http_rejection_redacts_signed_urls_from_the_upstream_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游常把请求原样回显，响应体里可能带签名地址；不能落进消息或库。"""
    body = json.dumps({"error": {"message": f"could not fetch {SIGNED_URL}"}})
    failure = _post_and_capture(monkeypatch, status=400, body=body)

    message = str(failure)
    assert "q-signature" not in message
    assert "deadbeefcafe" not in message
    assert "bucket.cos.ap-shanghai.myqcloud.com" not in message


def test_http_rejection_reason_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游可能吐回整个请求体；诊断信息不该撑爆日志和 DB 字段。"""
    failure = _post_and_capture(monkeypatch, status=400, body="x" * 10_000)

    assert len(str(failure)) < 600


def test_unreadable_upstream_body_still_reports_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """响应体读不出来时要降级，不能让诊断代码自己抛异常盖掉真错误。"""
    failure = _post_and_capture(monkeypatch, status=400, body="")

    assert failure.http_status == 400
    assert "400" in str(failure)


def test_provider_error_surfaces_the_specific_reason_not_the_catch_all() -> None:
    """五条路径不能再共用「返回了无法解析的结果」这一句。"""
    failure = AnalysisProviderFailed(
        "Apilio 拒绝了请求（HTTP 400）：model is not available",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
    )

    mapped = analysis_provider_error(failure)
    detail = mapped.detail
    assert isinstance(detail, dict)
    assert "model is not available" in str(detail["message"])


def test_provider_error_does_not_tell_users_to_retry_what_cannot_be_retried() -> None:
    """retryable=False 时还说「请重试」，会让用户反复白试。"""
    failure = AnalysisProviderFailed(
        "拆解结果格式无效。",
        failure_phase=RESPONSE_FAILURE_PHASE,
        retryable=False,
    )

    mapped = analysis_provider_error(failure)
    detail = mapped.detail
    assert isinstance(detail, dict)
    assert detail["retryable"] is False
    assert "请重试" not in str(detail["message"])


def test_rate_limited_and_network_failures_keep_their_actionable_wording() -> None:
    """限流和断网本来就有好文案，这次改动不能把它们弄丢。"""
    limited = analysis_provider_error(
        AnalysisProviderFailed("429", http_status=429, failure_phase=HTTP_FAILURE_PHASE)
    )
    assert isinstance(limited.detail, dict)
    assert "限流" in str(limited.detail["message"])
    assert limited.status_code == 429

    offline = analysis_provider_error(
        AnalysisProviderFailed("URLError", failure_phase="network", retryable=True)
    )
    assert isinstance(offline.detail, dict)
    assert "网络" in str(offline.detail["message"])
    assert offline.status_code == 503
