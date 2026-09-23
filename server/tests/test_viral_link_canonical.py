"""链接入口全格式规范化回归（离线表驱动，零真实网络、零付费网关调用）。

覆盖：抖音手机分享短链（v.douyin.com）、新式短链（z.douyin.com）、网页长链、
精选页、主页视频（modal_id）、发现页、iesdouyin 分享页、图文 note；小红书短链
（xhslink.com，含无 scheme 的分享文本）、explore、主页作品、discovery 旧链。

短链重定向还原使用假 transport；重定向失败、跨白名单目标、超出跳数一律降级
回原链接，绝不阻塞解析主流程。规范形态钉住网关已知稳定入口：
抖音 ``/jingxuan?modal_id=``、小红书 ``/explore/{note_id}``（保留 xsec_token）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.viral_link import (
    DouyidouHttpTransport,
    DouyidouLinkClient,
    LinkRedirectTransport,
    ViralLinkError,
    canonicalize_viral_link,
    normalize_supported_link,
)

_DOUYIN_ID = "7672703482771972081"
_XHS_ID = "66e012345678901234abcdef"
_DOUYIN_CANONICAL = f"https://www.douyin.com/jingxuan?modal_id={_DOUYIN_ID}"
_XHS_TOKEN = "ABcdef123xyz"


class FakeRedirectTransport(LinkRedirectTransport):
    def __init__(self, targets: dict[str, str] | None = None) -> None:
        self.targets = targets or {}
        self.calls: list[str] = []
        self.seen_headers: list[Mapping[str, str]] = []

    def resolve_redirect(self, url: str, *, headers: Mapping[str, str]) -> str | None:
        self.calls.append(url)
        self.seen_headers.append(headers)
        return self.targets.get(url)


class RecordingGatewayTransport(DouyidouHttpTransport):
    def __init__(self, aweme_id: str = _DOUYIN_ID) -> None:
        self.aweme_id = aweme_id
        self.source_urls: list[str] = []

    def request(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        self.source_urls.append(url)
        source = parse_qs(urlsplit(url).query).get("url", [""])[0]
        identity = (
            {"note_id": self.aweme_id} if "xiaohongshu" in source else {"aweme_id": self.aweme_id}
        )
        return json.dumps(
            {
                "code": 0,
                "data": {
                    **identity,
                    "video": [f"https://media.example/{self.aweme_id}.mp4"],
                    "audio": [f"https://media.example/{self.aweme_id}.m4a"],
                },
            }
        ).encode()


def _source_url_of(gateway: RecordingGatewayTransport, call: int = 0) -> str:
    return parse_qs(urlsplit(gateway.source_urls[call]).query)["url"][0]


# ── 裸域名分享文本（小红书新版口令不再带 http:// 前缀）────────────────────────


def test_bare_domain_share_text_extracts_url_and_normalizes() -> None:
    raw = "67 bls 发布了一篇小红书笔记，快来看吧！ xhslink.com/m/1gJ5tOG。"
    assert normalize_supported_link(raw) == "https://xhslink.com/m/1gJ5tOG"


def test_bare_domain_unknown_platform_still_rejected() -> None:
    with pytest.raises(ViralLinkError) as result:
        normalize_supported_link("看这个 notdouyin.com/video/123")
    assert result.value.code == "VIRAL_LINK_INVALID"


def test_scheme_url_takes_priority_over_bare_domain() -> None:
    raw = "xhslink.com/fallback https://www.xiaohongshu.com/explore/" + _XHS_ID
    assert normalize_supported_link(raw) == ("https://www.xiaohongshu.com/explore/" + _XHS_ID)


# ── 抖音：全形态 → 规范精选链接 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source_url", "targets", "expected_calls"),
    [
        # 手机分享短链：一跳 302 到 iesdouyin 分享页
        (
            "https://v.douyin.com/iRNBho6u/",
            {
                "https://v.douyin.com/iRNBho6u/": "https://www.iesdouyin.com/share/video/"
                f"{_DOUYIN_ID}/?region=CN&mid=0"
            },
            1,
        ),
        # 新式短链
        (
            "https://z.douyin.com/mqh8",
            {"https://z.douyin.com/mqh8": f"https://www.douyin.com/video/{_DOUYIN_ID}"},
            1,
        ),
        # 网页长链：直接提取，不需要重定向
        (f"https://www.douyin.com/video/{_DOUYIN_ID}?previous_page=web_code_link", {}, 0),
        # 精选页两种形态
        (f"https://www.douyin.com/jingxuan/video/{_DOUYIN_ID}", {}, 0),
        (f"https://www.douyin.com/jingxuan?modal_id={_DOUYIN_ID}", {}, 0),
        # 主页视频入口
        (
            "https://www.douyin.com/user/MS4wLjABAAAAx-1?modal_id=" + _DOUYIN_ID,
            {},
            0,
        ),
        # 发现页
        (f"https://www.douyin.com/discover?modal_id={_DOUYIN_ID}", {}, 0),
        # iesdouyin 分享页
        (f"https://www.iesdouyin.com/share/video/{_DOUYIN_ID}/", {}, 0),
        # 图文 note 链接
        (f"https://www.douyin.com/note/{_DOUYIN_ID}", {}, 0),
    ],
)
def test_douyin_link_formats_canonicalize(
    source_url: str, targets: dict[str, str], expected_calls: int
) -> None:
    transport = FakeRedirectTransport(targets)
    canonical = canonicalize_viral_link(source_url, redirect_transport=transport)
    assert canonical == _DOUYIN_CANONICAL
    assert len(transport.calls) == expected_calls


# ── 小红书：全形态 → 规范 explore 链接（保留 xsec_token）────────────────────


@pytest.mark.parametrize(
    ("source_url", "targets", "expected"),
    [
        # 短链还原后带 token
        (
            "http://xhslink.com/a/local-contract",
            {
                "http://xhslink.com/a/local-contract": (
                    "https://www.xiaohongshu.com/explore/"
                    f"{_XHS_ID}?xsec_token={_XHS_TOKEN}&xsec_source=ss_feed"
                )
            },
            f"https://www.xiaohongshu.com/explore/{_XHS_ID}?xsec_token={_XHS_TOKEN}",
        ),
        # 短链还原后无 token
        (
            "https://xhslink.com/m/1gJ5tOG",
            {
                "https://xhslink.com/m/1gJ5tOG": (
                    f"https://www.xiaohongshu.com/user/profile/5ff0e6410000000001008400/{_XHS_ID}"
                )
            },
            f"https://www.xiaohongshu.com/explore/{_XHS_ID}",
        ),
        # explore 直链：token 原样保留，无需重定向
        (
            f"https://www.xiaohongshu.com/explore/{_XHS_ID}?xsec_token={_XHS_TOKEN}",
            {},
            f"https://www.xiaohongshu.com/explore/{_XHS_ID}?xsec_token={_XHS_TOKEN}",
        ),
        # 主页作品链接
        (
            f"https://www.xiaohongshu.com/user/profile/5ff0e641/{_XHS_ID}?xsec_token={_XHS_TOKEN}",
            {},
            f"https://www.xiaohongshu.com/explore/{_XHS_ID}?xsec_token={_XHS_TOKEN}",
        ),
        # 旧版 discovery 链接
        (
            f"https://www.xiaohongshu.com/discovery/item/{_XHS_ID}",
            {},
            f"https://www.xiaohongshu.com/explore/{_XHS_ID}",
        ),
    ],
)
def test_xiaohongshu_link_formats_canonicalize(
    source_url: str, targets: dict[str, str], expected: str
) -> None:
    transport = FakeRedirectTransport(targets)
    canonical = canonicalize_viral_link(source_url, redirect_transport=transport)
    assert canonical == expected


# ── 降级路径：任何失败都回到原链接 ──────────────────────────────────────────


def test_short_link_without_redirect_result_falls_back_to_original() -> None:
    transport = FakeRedirectTransport()
    assert (
        canonicalize_viral_link("https://v.douyin.com/iRNBho6u/", redirect_transport=transport)
        == "https://v.douyin.com/iRNBho6u/"
    )


def test_redirect_to_non_allowlisted_host_falls_back() -> None:
    transport = FakeRedirectTransport(
        {"https://v.douyin.com/iRNBho6u/": "https://evil.example.com/video/" + _DOUYIN_ID}
    )
    assert (
        canonicalize_viral_link("https://v.douyin.com/iRNBho6u/", redirect_transport=transport)
        == "https://v.douyin.com/iRNBho6u/"
    )


def test_redirect_hop_limit_falls_back_after_three_hops() -> None:
    transport = FakeRedirectTransport(
        {
            "https://v.douyin.com/a": "https://www.douyin.com/hop1",
            "https://www.douyin.com/hop1": "https://www.douyin.com/hop2",
            "https://www.douyin.com/hop2": "https://www.douyin.com/hop3",
            "https://www.douyin.com/hop3": f"https://www.douyin.com/video/{_DOUYIN_ID}",
        }
    )
    assert (
        canonicalize_viral_link("https://v.douyin.com/a", redirect_transport=transport)
        == "https://v.douyin.com/a"
    )
    assert len(transport.calls) == 3


def test_non_short_link_without_id_returns_input_unchanged() -> None:
    url = "https://www.douyin.com/search/乡墅"
    transport = FakeRedirectTransport()
    assert canonicalize_viral_link(url, redirect_transport=transport) == url
    assert transport.calls == []


def test_canonicalize_is_idempotent() -> None:
    transport = FakeRedirectTransport()
    once = canonicalize_viral_link(
        f"https://www.douyin.com/video/{_DOUYIN_ID}", redirect_transport=transport
    )
    assert canonicalize_viral_link(once, redirect_transport=transport) == once


def test_redirect_requests_carry_mobile_user_agent() -> None:
    transport = FakeRedirectTransport({"https://v.douyin.com/x": "about:blank"})
    canonicalize_viral_link("https://v.douyin.com/x", redirect_transport=transport)
    agent = transport.seen_headers[0].get("User-Agent", "")
    assert "iPhone" in agent or "Android" in agent


# ── 网关调用收到的是规范链接 ────────────────────────────────────────────────


def test_resolve_sends_canonical_url_to_gateway() -> None:
    gateway = RecordingGatewayTransport()
    redirect = FakeRedirectTransport(
        {
            "https://v.douyin.com/iRNBho6u/": (
                "https://www.iesdouyin.com/share/video/" + _DOUYIN_ID + "/"
            )
        }
    )
    client = DouyidouLinkClient(
        app_id="test", app_secret="test", transport=gateway, redirect_transport=redirect
    )
    resolved = client.resolve("https://v.douyin.com/iRNBho6u/", purpose="copy")
    assert resolved.video_id == _DOUYIN_ID
    assert _source_url_of(gateway) == _DOUYIN_CANONICAL


def test_resolve_without_redirect_transport_keeps_short_link() -> None:
    gateway = RecordingGatewayTransport()
    client = DouyidouLinkClient(app_id="test", app_secret="test", transport=gateway)
    client.resolve("https://v.douyin.com/iRNBho6u/", purpose="copy")
    assert _source_url_of(gateway) == "https://v.douyin.com/iRNBho6u/"


def test_resolve_sends_xhs_canonical_with_token() -> None:
    gateway = RecordingGatewayTransport(aweme_id=_XHS_ID)
    redirect = FakeRedirectTransport(
        {
            "https://xhslink.com/m/1gJ5tOG": (
                f"https://www.xiaohongshu.com/explore/{_XHS_ID}?xsec_token={_XHS_TOKEN}"
            )
        }
    )
    client = DouyidouLinkClient(
        app_id="test", app_secret="test", transport=gateway, redirect_transport=redirect
    )
    resolved = client.resolve("https://xhslink.com/m/1gJ5tOG", purpose="copy")
    assert resolved.video_id == _XHS_ID
    assert _source_url_of(gateway) == (
        f"https://www.xiaohongshu.com/explore/{_XHS_ID}?xsec_token={_XHS_TOKEN}"
    )


# ── 网关返回 ID 的兼容宽度（近年 19 位为主，放宽到 15–22 位）────────────────


def test_gateway_aweme_id_within_widened_range_is_accepted() -> None:
    gateway = RecordingGatewayTransport(aweme_id="7" * 21)
    client = DouyidouLinkClient(app_id="test", app_secret="test", transport=gateway)
    resolved = client.resolve(f"https://www.douyin.com/video/{'7' * 21}", purpose="copy")
    assert resolved.video_id == "7" * 21


def test_gateway_aweme_id_outside_range_is_rejected() -> None:
    gateway = RecordingGatewayTransport(aweme_id="7" * 14)
    client = DouyidouLinkClient(app_id="test", app_secret="test", transport=gateway)
    with pytest.raises(ViralLinkError) as result:
        client.resolve(f"https://www.douyin.com/video/{'7' * 14}", purpose="copy")
    assert result.value.code == "VIRAL_LINK_NATIVE_ID_INVALID"


# ── 时长解析：douyidou 把时长放在 data.other.duration（毫秒），需嵌套兜底 ──────


def test_douyidou_duration_falls_back_to_nested_other_field() -> None:
    """顶层时长键缺失时，须兜底读取 data.other.duration（毫秒），否则拆解报“时长不可用”。"""

    class NestedDurationTransport(DouyidouHttpTransport):
        def request(self, url: str, *, headers: Any) -> bytes:
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "aweme_id": _DOUYIN_ID,
                        "video": [f"https://media.example/{_DOUYIN_ID}.mp4"],
                        "other": {"duration": 81083},
                    },
                }
            ).encode()

    client = DouyidouLinkClient(
        app_id="test", app_secret="test", transport=NestedDurationTransport()
    )
    resolved = client.resolve(
        f"https://www.douyin.com/jingxuan?modal_id={_DOUYIN_ID}", purpose="copy"
    )
    assert resolved.duration_ms == 81083


def test_douyidou_top_level_duration_takes_priority_over_nested() -> None:
    """顶层时长优先于 other.duration 兜底，嵌套值不得覆盖更精确的顶层值。"""

    class BothDurationTransport(DouyidouHttpTransport):
        def request(self, url: str, *, headers: Any) -> bytes:
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "aweme_id": _DOUYIN_ID,
                        "video": [f"https://media.example/{_DOUYIN_ID}.mp4"],
                        "duration": 5,
                        "other": {"duration": 81083},
                    },
                }
            ).encode()

    client = DouyidouLinkClient(app_id="test", app_secret="test", transport=BothDurationTransport())
    resolved = client.resolve(
        f"https://www.douyin.com/jingxuan?modal_id={_DOUYIN_ID}", purpose="copy"
    )
    assert resolved.duration_ms == 5000


def test_unused_import_guard() -> None:
    # 占位防止未来误删 Any 导入；保持与既有测试文件风格一致。
    payload: dict[str, Any] = {}
    assert payload == {}
