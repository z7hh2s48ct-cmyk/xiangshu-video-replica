"""API-first / browser-fallback decision matrix and media materialization (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app import publish_delivery
from app.publish_credentials import PublishCredentials
from app.publishers.base import PublisherUnavailableError, PublishResult

_STATE = {
    "cookies": [{"name": "sessionid", "value": "s", "domain": ".douyin.com"}],
    "origins": [],
}
_MEDIA = {
    "video_path": Path("video.mp4"),
    "cover_path": None,
    "title": "t",
    "description": "d",
    "tags": ["a"],
}


def _api(result: PublishResult, calls: list[str]) -> Any:
    def fake(platform: str, credentials: PublishCredentials, **kwargs: Any) -> PublishResult:
        calls.append("api")
        assert credentials.cookie == "sessionid=s"
        return result

    return fake


def _browser(result: PublishResult | Exception, calls: list[str]) -> Any:
    def fake(platform: str, storage_state: dict[str, Any], **kwargs: Any) -> PublishResult:
        calls.append("browser")
        if isinstance(result, Exception):
            raise result
        return result

    return fake


def test_api_success_never_touches_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        publish_delivery,
        "deliver_via_api",
        _api(PublishResult(platform="douyin", status="published", item_id="1"), calls),
    )
    monkeypatch.setattr(
        publish_delivery,
        "deliver_via_browser",
        _browser(PublishResult(platform="douyin", status="published"), calls),
    )
    outcome = publish_delivery.deliver("douyin", _STATE, **_MEDIA)
    assert outcome.mode == "api" and outcome.result.item_id == "1"
    assert calls == ["api"]


def test_transient_api_failure_falls_back_to_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        publish_delivery,
        "deliver_via_api",
        _api(PublishResult(platform="douyin", status="failed", message="签名失败"), calls),
    )
    monkeypatch.setattr(
        publish_delivery,
        "deliver_via_browser",
        _browser(PublishResult(platform="douyin", status="published", item_id="b"), calls),
    )
    outcome = publish_delivery.deliver("douyin", _STATE, **_MEDIA)
    assert outcome.mode == "browser" and outcome.result.item_id == "b"
    assert calls == ["api", "browser"]


def test_account_invalid_is_terminal_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        publish_delivery,
        "deliver_via_api",
        _api(
            PublishResult(
                platform="douyin", status="failed", message="登录失效", account_invalid=True
            ),
            calls,
        ),
    )
    monkeypatch.setattr(
        publish_delivery,
        "deliver_via_browser",
        _browser(PublishResult(platform="douyin", status="published"), calls),
    )
    outcome = publish_delivery.deliver("douyin", _STATE, **_MEDIA)
    assert outcome.mode == "api" and outcome.result.account_invalid
    assert calls == ["api"]


def test_fallback_disabled_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv(publish_delivery.BROWSER_FALLBACK_ENV, "0")
    failed = PublishResult(platform="douyin", status="failed", message="x")
    monkeypatch.setattr(publish_delivery, "deliver_via_api", _api(failed, calls))
    monkeypatch.setattr(publish_delivery, "deliver_via_browser", _browser(failed, calls))
    outcome = publish_delivery.deliver("douyin", _STATE, **_MEDIA)
    assert outcome.mode == "api" and outcome.result is failed
    assert calls == ["api"]


def test_unavailable_browser_keeps_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    failed = PublishResult(platform="douyin", status="failed", message="接口变更")
    monkeypatch.setattr(publish_delivery, "deliver_via_api", _api(failed, calls))
    monkeypatch.setattr(
        publish_delivery,
        "deliver_via_browser",
        _browser(PublisherUnavailableError("douyin", "not shipped"), calls),
    )
    outcome = publish_delivery.deliver("douyin", _STATE, **_MEDIA)
    assert outcome.mode == "api" and outcome.result is failed
    assert calls == ["api", "browser"]


def test_browser_crash_reports_both_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    failed = PublishResult(platform="douyin", status="failed", message="接口变更")
    monkeypatch.setattr(publish_delivery, "deliver_via_api", _api(failed, calls))
    monkeypatch.setattr(
        publish_delivery, "deliver_via_browser", _browser(RuntimeError("selector"), calls)
    )
    outcome = publish_delivery.deliver("douyin", _STATE, **_MEDIA)
    assert outcome.mode == "browser"
    assert outcome.result.status == "failed"
    assert outcome.result.message == "接口变更；浏览器兜底也未成功"
    assert not outcome.result.account_invalid


def test_missing_cookie_material_is_account_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        publish_delivery,
        "deliver_via_api",
        _api(PublishResult(platform="douyin", status="published"), calls),
    )
    outcome = publish_delivery.deliver("douyin", {"cookies": [], "origins": []}, **_MEDIA)
    assert outcome.mode is None and outcome.result.account_invalid
    assert calls == []


def test_default_browser_stub_is_unavailable() -> None:
    with pytest.raises(PublisherUnavailableError):
        publish_delivery.deliver_via_browser("douyin", _STATE, options={}, **_MEDIA)


def test_unsupported_platform_fails_via_api_seam() -> None:
    result = publish_delivery.deliver_via_api(
        "xiaohongshu", PublishCredentials(cookie="a=b"), options={}, **_MEDIA
    )
    assert result.status == "failed" and result.message == "该平台暂不支持自动发布"


class _Storage:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def iter_object(self, key: str, *args: Any, **kwargs: Any) -> Iterator[bytes]:
        data = self.blobs[key]
        for offset in range(0, len(data), 4):
            yield data[offset : offset + 4]


def test_materialized_media_streams_video_and_cover_then_cleans_up() -> None:
    storage = _Storage({"v/1": b"video-bytes-here", "c/1": b"cover"})
    with publish_delivery.materialized_media(
        storage,  # type: ignore[arg-type]
        video_uri="cos://bucket/v/1",
        video_content_type="video/mp4",
        cover_uri="cos://bucket/c/1",
        cover_content_type="image/png",
    ) as (video, cover):
        assert video.suffix == ".mp4" and video.read_bytes() == b"video-bytes-here"
        assert cover is not None and cover.suffix == ".png" and cover.read_bytes() == b"cover"
        root = video.parent.parent
    assert not root.exists()


def test_materialized_media_rejects_empty_objects() -> None:
    storage = _Storage({"v/1": b""})
    with (
        pytest.raises(RuntimeError, match="empty"),
        publish_delivery.materialized_media(
            storage,  # type: ignore[arg-type]
            video_uri="cos://bucket/v/1",
            video_content_type=None,
            cover_uri=None,
            cover_content_type=None,
        ),
    ):
        pass


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("1", True), ("on", True), ("0", False), ("off", False), ("FALSE", False)],
)
def test_browser_fallback_flag_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv(publish_delivery.BROWSER_FALLBACK_ENV, raising=False)
    else:
        monkeypatch.setenv(publish_delivery.BROWSER_FALLBACK_ENV, value)
    assert publish_delivery.browser_fallback_enabled() is expected
