"""爆款数据源客户端测试：字段规范化、翻页聚合、红线与错误映射.

夹具形状取自 2026-09-06 真实接口抓包（脱敏），不包含任何凭据。
"""

from __future__ import annotations

import email.message
import gzip
import io
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from urllib.error import HTTPError

import pytest

from app.viral_tikhub import (
    DOUYIN_GENERAL_SEARCH_PATH,
    MAX_TAGS,
    WECHAT_SEARCH_VIDEOS_PATH,
    UrllibViralHttpTransport,
    ViralSourceClient,
    ViralSourceError,
    ViralSourceHttpStatusError,
    ViralSourceUnavailable,
    _pick_image_url,
    _reset_wechat_detail_cache,
    extract_wechat_tags,
    is_irrelevant_viral_video,
    normalize_douyin_aweme,
    normalize_wechat_item,
    parse_compact_count,
    parse_wechat_duration_ms,
    pick_douyin_play_url,
    strip_highlight,
    viral_source_client_from_config,
)


class FakeTransport:
    """可编程传输层：按队列返回响应并记录请求体."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, *, headers, body=None) -> bytes:
        assert method == "POST"
        self.requests.append({"url": url, "body": json.loads(body or b"{}")})
        payload = self.payloads.pop(0)
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _client(
    payloads: list[dict[str, Any]],
) -> tuple[ViralSourceClient, list[FakeTransport]]:
    _reset_wechat_detail_cache()
    transports = [FakeTransport(list(payloads)), FakeTransport([])]
    client = ViralSourceClient(
        api_key="test-key",
        transport=transports[0],
        detail_transport=transports[1],
    )
    return client, transports


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_strip_highlight_removes_em_tags_and_unescapes() -> None:
    raw = '效果图<em class="highlight">别墅</em>&#39;设计#宅基地建房'
    assert strip_highlight(raw) == "效果图别墅&#39;设计#宅基地建房".replace("&#39;", "'")


def test_extract_wechat_tags_caps_at_six() -> None:
    title = "主体#农村自建房#别墅设计#效果图#图纸#装修#同城#第七个"
    assert extract_wechat_tags(title) == [
        "农村自建房",
        "别墅设计",
        "效果图",
        "图纸",
        "装修",
        "同城",
    ]
    assert len(extract_wechat_tags(title)) == MAX_TAGS


def test_parse_wechat_duration_ms() -> None:
    assert parse_wechat_duration_ms("00:11") == 11_000
    assert parse_wechat_duration_ms("01:02:03") == 3_723_000
    assert parse_wechat_duration_ms("") is None
    assert parse_wechat_duration_ms("abc") is None


def test_parse_compact_count() -> None:
    assert parse_compact_count("6391") == 6391
    assert parse_compact_count("10万+") == 100_000
    assert parse_compact_count("1.2万") == 12_000
    assert parse_compact_count("") is None
    assert parse_compact_count(None) is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Minecraft 村庄别墅建造教程", True),
        ("我的世界现代别墅庭院", True),
        ("乡村庭院 #MC #游戏", True),
        ("普通中文农村庭院设计", False),
        ("MCN 机构的自建房案例", False),
        ("amc建筑事务所的庭院", False),
        ("mc子串不应被当成独立游戏标签", False),
    ],
)
def test_irrelevant_game_filter_only_matches_explicit_terms(title: str, expected: bool) -> None:
    assert is_irrelevant_viral_video(title) is expected


def test_pick_douyin_play_url_takes_default_play_addr() -> None:
    """P1：直取源站默认播放地址（不遍历 bit_rate 档位、不挑最低分辨率）."""
    video_block = {
        "play_addr": {"url_list": ["https://cdn/default.mp4", "https://cdn/backup.mp4"]},
        "bit_rate": [
            {"play_addr": {"height": 1280, "url_list": ["https://cdn/720.mp4"]}},
            {"play_addr": {"height": 1024, "url_list": ["https://cdn/540.mp4"]}},
        ],
    }
    assert pick_douyin_play_url(video_block) == "https://cdn/default.mp4"


def test_pick_image_url_prefers_renderable_format_and_host() -> None:
    block = {
        "url_list": [
            "https://p3-c-sign.douyinpic.com/a.heic?sig=1",
            "https://p96-sign.douyinpic.com/a.webp?sig=2",
            "https://p3-c-sign.douyinpic.com/b.webp?sig=3",
        ]
    }
    assert _pick_image_url(block) == "https://p96-sign.douyinpic.com/a.webp?sig=2"
    # 全是 heic 时退回首项（Safari 可渲染，Chromium 走前端占位）。
    assert _pick_image_url({"url_list": ["https://x/a.heic"]}) == "https://x/a.heic"
    assert _pick_image_url(None) is None
    assert _pick_image_url({"url_list": []}) is None


def test_pick_douyin_play_url_ignores_bit_rate_gears() -> None:
    """没有顶层 play_addr 时返回 None：不再回退到 bit_rate 档位."""
    assert pick_douyin_play_url({}) is None
    assert (
        pick_douyin_play_url({"bit_rate": [{"play_addr": {"url_list": ["https://cdn/540.mp4"]}}]})
        is None
    )


def test_normalize_douyin_aweme_caps_tags_and_maps_stats() -> None:
    aweme = {
        "aweme_id": "7680753914849346171",
        "desc": "迈巴赫锦鲤池 鱼池届天花板 #锦鲤体型 #鱼池养锦鲤",
        "create_time": 1788314878,
        "duration": 15700,
        "statistics": {
            "digg_count": 55569,
            "comment_count": 2757,
            "share_count": 38839,
            "collect_count": 3316,
            "play_count": 0,
        },
        "author": {
            "uid": "2491907694661319",
            "nickname": "张百万（乡墅版）",
            "is_verified": True,
            "avatar_thumb": {"url_list": ["https://cdn/avatar.jpeg"]},
        },
        "text_extra": [
            {"hashtag_name": "锦鲤体型"},
            {"hashtag_name": "鱼池养锦鲤"},
            {"hashtag_name": "第三个"},
            {"hashtag_name": "第四个"},
            {"hashtag_name": "第五个"},
            {"hashtag_name": "第六个"},
            {"hashtag_name": "第七个"},
        ],
        "video": {
            "duration": 15700,
            "width": 576,
            "height": 1024,
            "cover": {"url_list": ["https://cdn/cover.webp"]},
            "play_addr": {"url_list": ["https://cdn/default.mp4"]},
            "bit_rate": [
                {"play_addr": {"height": 1280, "url_list": ["https://cdn/p720.mp4"]}},
                {"play_addr": {"height": 1024, "url_list": ["https://cdn/p540.mp4"]}},
            ],
        },
        "music": {"title": "@余音创作的原声", "play_url": {"url_list": ["https://cdn/origin.mp3"]}},
    }
    video = normalize_douyin_aweme(aweme, "庭院案例")
    assert video is not None
    assert video.video_id == "7680753914849346171"
    assert video.platform == "douyin"
    assert video.likes == 55569
    assert video.verified is True
    assert video.cover_url == "https://cdn/cover.webp"
    assert video.play_url == "https://cdn/default.mp4"
    assert video.audio_url == "https://cdn/origin.mp3"
    assert video.tags == ["锦鲤体型", "鱼池养锦鲤", "第三个", "第四个", "第五个", "第六个"]
    client_dict = video.to_client_dict()
    assert client_dict["hasPlayableAudio"] is True
    assert client_dict["homepageRank"] is None
    assert client_dict["native"] == {"aweme_id": "7680753914849346171"}


def test_normalize_douyin_aweme_without_music_play_url() -> None:
    aweme = {
        "aweme_id": "1",
        "desc": "测试",
        "video": {"cover": {"url_list": ["https://cdn/c.jpg"]}},
        "music": {"title": "Sacrifice（奉献）", "play_url": {"url_list": []}},
    }
    video = normalize_douyin_aweme(aweme, "")
    assert video is not None
    assert video.audio_url is None
    assert video.to_client_dict()["hasPlayableAudio"] is False


def test_normalize_wechat_item_cleans_highlight_and_parses_like() -> None:
    item = {
        "docID": "finderobjv076vYLdkzrmBGLh00RsZnsArVVxC",
        "exportId": "export/UzFfAgtgekIEAQAAAAAA",
        "duration": "00:11",
        "width": 1080,
        "height": 1920,
        "image": "https://wxcdn/cover.jpg",
        "likeNum": "10万+",
        "pubTime": 1650331621,
        "dateTime": "4年前",
        "title": ('农村建房<em class="highlight">平屋顶</em>别墅#微信创作者#二层别墅#低成本'),
        "source": {"iconUrl": "https://wxcdn/head.png", "title": "乡墅建房徐工2"},
        "jumpInfo": {"extInfo": '{"behavior":[],"feedNonceId":"4488625110168773069"}'},
    }
    video = normalize_wechat_item(item, "建房预算")
    assert video is not None
    assert video.video_id == "finderobjv076vYLdkzrmBGLh00RsZnsArVVxC"
    # title 保留完整清洗后文本（与抖音 desc 行为一致），标签另行以 chips 展示。
    assert video.title == "农村建房平屋顶别墅#微信创作者#二层别墅#低成本"
    assert video.likes == 100_000
    assert video.like_display == "10万+"
    assert video.duration_ms == 11_000
    assert video.published_display == "4年前"
    assert video.author == "乡墅建房徐工2"
    assert video.tags == ["微信创作者", "二层别墅", "低成本"]
    assert video.native["object_nonce_id"] == "4488625110168773069"


def test_normalize_wechat_item_accepts_object_ext_info() -> None:
    item = {
        "docID": "d1",
        "exportId": "export/e1",
        "duration": "01:00",
        "image": "https://wxcdn/c.jpg",
        "likeNum": "6391",
        "pubTime": 1788602461,
        "title": "标题#标签",
        "source": {"title": "作者"},
        "jumpInfo": {"extInfo": {"feedNonceId": "123"}},
    }
    video = normalize_wechat_item(item, "")
    assert video is not None
    assert video.native["object_nonce_id"] == "123"
    assert video.duration_ms == 60_000


def test_normalize_wechat_item_requires_core_fields() -> None:
    assert normalize_wechat_item({"docID": "", "exportId": "e", "title": "t"}, "") is None
    assert normalize_wechat_item({"docID": "d", "exportId": "", "title": "t"}, "") is None


def test_normalizers_ignore_malformed_numeric_fields() -> None:
    douyin = normalize_douyin_aweme(
        {
            "aweme_id": "bad-counts",
            "desc": "农村庭院",
            "create_time": "unknown",
            "statistics": {
                "digg_count": "bad",
                "comment_count": [],
                "share_count": {},
                "collect_count": False,
            },
            "video": {"duration": "bad", "cover": {"url_list": ["https://cdn/c.jpg"]}},
        },
        "庭院案例",
    )
    wechat = normalize_wechat_item(
        {
            "docID": "wx-bad-time",
            "exportId": "export/wx-bad-time",
            "title": "农村庭院",
            "pubTime": "unknown",
        },
        "庭院案例",
    )

    assert douyin is not None
    assert (douyin.likes, douyin.comments, douyin.shares, douyin.collects) == (0, 0, 0, 0)
    assert douyin.published_at is None
    assert wechat is not None
    assert wechat.published_at is None


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def test_douyin_search_filters_related_word_cards() -> None:
    payload = {
        "code": 200,
        "data": {
            "business_data": [
                {"type": 6, "data": {"related_word_list": []}},
                {
                    "type": 1,
                    "data": {
                        "aweme_info": {
                            "aweme_id": "777",
                            "desc": "视频",
                            "video": {"cover": {"url_list": ["https://cdn/c.webp"]}},
                        }
                    },
                },
            ]
        },
    }
    client, transports = _client([payload])
    videos = client.douyin_search(keyword="乡墅", category="庭院案例")
    assert len(videos) == 1
    assert videos[0].video_id == "777"
    assert videos[0].category == "庭院案例"
    request = transports[0].requests[0]
    assert request["url"].endswith(DOUYIN_GENERAL_SEARCH_PATH)
    assert request["body"]["keyword"] == "乡墅"
    assert request["body"]["sort_type"] == "1"
    assert request["body"]["publish_time"] == "7"
    assert request["body"]["content_type"] == "1"


def test_douyin_search_page_encodes_cursor_and_publish_time() -> None:
    """翻页：请求带发布时间与还原出的 offset/search_id；响应字段归一成一页."""

    def aweme(video_id: str) -> dict[str, Any]:
        return {
            "type": 1,
            "data": {
                "aweme_info": {
                    "aweme_id": video_id,
                    "desc": "乡墅",
                    "video": {"cover": {"url_list": ["https://cdn/c.webp"]}},
                }
            },
        }

    first = {
        "code": 200,
        "data": {
            "business_data": [aweme("a-1")],
            "cursor": 12,
            "has_more": 1,
            "log_pb": {"impr_id": "impr-1"},
        },
    }
    second = {
        "code": 200,
        "data": {"business_data": [aweme("a-2")], "cursor": 22, "has_more": 0},
    }
    client, transports = _client([first, second])
    page = client.douyin_search_page(keyword="乡墅", publish_time="1")
    assert [video.video_id for video in page.videos] == ["a-1"]
    assert page.has_more is True
    first_cursor = json.loads(page.cursor or "{}")
    assert first_cursor == {"c": 12, "s": "impr-1", "b": ""}
    first_body = transports[0].requests[0]["body"]
    assert first_body["publish_time"] == "1"
    assert "cursor" not in first_body

    next_page = client.douyin_search_page(keyword="乡墅", publish_time="1", cursor=page.cursor)
    assert [video.video_id for video in next_page.videos] == ["a-2"]
    assert next_page.has_more is False and next_page.cursor is None
    second_body = transports[0].requests[1]["body"]
    assert second_body["cursor"] == 12
    assert second_body["search_id"] == "impr-1"


def test_douyin_search_page_tolerates_pagination_nested_shape() -> None:
    """翻页字段放在 data.pagination 下的接口版本同样可用；无翻页字段时判无更多."""
    payload = {
        "code": 200,
        "data": {
            "business_data": [],
            "pagination": {"offset": 30, "search_id": "sid-9", "backtrace": "bt-1"},
            "has_more_status": "1",
        },
    }
    client, _ = _client([payload])
    page = client.douyin_search_page(keyword="乡墅")
    assert page.has_more is True
    assert json.loads(page.cursor or "{}") == {"c": 30, "s": "sid-9", "b": "bt-1"}

    bare = {"code": 200, "data": {"business_data": []}}
    client, _ = _client([bare])
    page = client.douyin_search_page(keyword="乡墅")
    assert page.has_more is False and page.cursor is None


def test_decode_douyin_search_cursor_rejects_garbage() -> None:
    """损坏/异型游标按首页处理，绝不抛错打断搜索."""
    from app.viral_tikhub import decode_douyin_search_cursor

    assert decode_douyin_search_cursor(None) is None
    assert decode_douyin_search_cursor("") is None
    assert decode_douyin_search_cursor("not-json") is None
    assert decode_douyin_search_cursor('{"c":"12"}') is None
    assert decode_douyin_search_cursor('{"c":true}') is None
    assert decode_douyin_search_cursor('{"c":12,"s":"s","b":"b"}') == {
        "cursor": 12,
        "search_id": "s",
        "backtrace": "b",
    }


def test_douyin_search_accepts_gzip_response_and_requests_compression() -> None:
    """跨境链路优化：请求带 Accept-Encoding: gzip，压缩响应体透明解压."""

    class GzipTransport:
        """返回 gzip 压缩体的传输桩."""

        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload
            self.headers: dict[str, str] | None = None

        def request(self, method: str, url: str, *, headers, body=None) -> bytes:
            self.headers = dict(headers)
            raw = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
            return gzip.compress(raw)

    envelope = {
        "code": 200,
        "data": {
            "business_data": [
                {
                    "type": 1,
                    "data": {
                        "aweme_info": {
                            "aweme_id": "gz-1",
                            "desc": "乡墅",
                            "video": {"cover": {"url_list": ["https://cdn/c.webp"]}},
                        }
                    },
                }
            ]
        },
    }
    transport = GzipTransport(envelope)
    client = ViralSourceClient(api_key="test-key", transport=transport)
    videos = client.douyin_search(keyword="乡墅")
    assert [video.video_id for video in videos] == ["gz-1"]
    assert transport.headers is not None
    assert transport.headers.get("Accept-Encoding") == "gzip"


def test_douyin_search_rejects_corrupt_gzip_body() -> None:
    """gzip 魔数命中但解压失败：转成中性的数据源错误，不让 OSError 裸抛."""

    class CorruptGzipTransport:
        def request(self, method: str, url: str, *, headers, body=None) -> bytes:
            return b"\x1f\x8bnot-really-gzip"

    client = ViralSourceClient(api_key="test-key", transport=CorruptGzipTransport())
    with pytest.raises(ViralSourceError):
        client.douyin_search(keyword="乡墅")


def test_search_filters_minecraft_results_without_rejecting_normal_mc_substrings() -> None:
    def aweme(video_id: str, title: str) -> dict[str, Any]:
        return {
            "type": 1,
            "data": {
                "aweme_info": {
                    "aweme_id": video_id,
                    "desc": title,
                    "video": {"cover": {"url_list": ["https://cdn/c.webp"]}},
                }
            },
        }

    payload = {
        "code": 200,
        "data": {
            "business_data": [
                aweme("game", "Minecraft 农村别墅教程"),
                aweme("world", "我的世界庭院搭建"),
                aweme("normal", "MCN 设计师讲农村庭院"),
            ]
        },
    }
    client, _ = _client([payload])

    videos = client.douyin_search(keyword="农村庭院", category="庭院案例")

    assert [video.video_id for video in videos] == ["normal"]


def test_wechat_search_aggregates_three_pages() -> None:
    def page_payload(cursor: str, continue_flag: int) -> dict[str, Any]:
        items = [
            {
                "docID": f"doc-{cursor}-{index}",
                "exportId": f"export/{cursor}-{index}",
                "duration": "00:10",
                "image": "https://wxcdn/c.jpg",
                "likeNum": "100",
                "pubTime": 1788602461,
                "title": f"标题{index}#标签",
                "source": {"title": "作者"},
            }
            for index in range(10)
        ]
        return {
            "code": 200,
            "data": {
                "cursor": cursor,
                "continue_flag": continue_flag,
                "results": {"data": [{"subBoxes": [{"items": items}]}]},
            },
        }

    payloads = [
        page_payload("c1", 1),
        page_payload("c2", 1),
        page_payload("c3", 0),
    ]
    client, transports = _client(payloads)
    videos = client.wechat_search(keyword="乡墅")
    assert len(videos) == 30
    assert videos[0].video_id == "doc-c1-0"
    assert videos[-1].video_id == "doc-c3-9"
    bodies = [entry["body"] for entry in transports[0].requests]
    assert len(bodies) == 3
    assert "cursor" not in bodies[0]
    assert bodies[1]["cursor"] == "c1"
    assert bodies[2]["cursor"] == "c2"
    assert all(body["publish_time"] == "week" for body in bodies)
    assert all(body["sort"] == "hot" for body in bodies)
    assert transports[0].requests[0]["url"].endswith(WECHAT_SEARCH_VIDEOS_PATH)


def test_wechat_video_detail_parses_media_block() -> None:
    payload = {
        "code": 200,
        "data": {
            "id": 15003884913433053492,
            "object_nonce_id": "13031274272234543781_0",
            "nickname": "扬州福墅合家美宅-大帅建别墅",
            "title": "今天当着所有扬州业主的面发誓#乡墅#别墅",
            "create_time": 1788602461,
            "like_count": 6392,
            "fav_count": 3266,
            "forward_count": 417,
            "comment_count": 189,
            "location": {"city": "扬州市"},
            "media": {
                "full_url": "http://wxapp.tc.qq.com/download",
                "decode_key": "1789473271",
                "cover_url": "https://wxcdn/detail-cover.jpg",
                "duration": 11,
                "width": 1080,
                "height": 1920,
            },
        },
    }
    client, transports = _client([])
    client._detail_transport.payloads = [payload]  # noqa: SLF001
    detail = client.wechat_video_detail(export_id="export/e1")
    assert detail.object_id == "15003884913433053492"
    assert detail.decode_key == "1789473271"
    assert detail.full_url == "http://wxapp.tc.qq.com/download"
    assert detail.duration_ms == 11_000
    assert detail.city == "扬州市"
    assert detail.like_count == 6392
    assert transports[0].requests == []
    assert transports[1].requests[0]["body"]["export_id"] == "export/e1"
    assert transports[1].requests[0]["body"]["raw"] is False


def test_wechat_video_detail_preserves_present_zero_and_ignores_invalid_counts() -> None:
    payload = {
        "code": 200,
        "data": {
            "id": "15003884913433053492",
            "title": "农村庭院",
            "like_count": 0,
            "fav_count": "12",
            "forward_count": "unknown",
        },
    }
    client, _ = _client([])
    client._detail_transport.payloads = [payload]  # noqa: SLF001

    detail = client.wechat_video_detail(export_id="export/e1")

    assert detail.like_count == 0
    assert detail.fav_count == 12
    assert detail.forward_count is None
    assert detail.comment_count is None


def test_wechat_detail_accepts_exact_large_object_id_without_expiring_export_id() -> None:
    client, transports = _client([])
    client._detail_transport.payloads = [
        {"code": 200, "data": {"id": 15003884913433053492, "like_count": 0}}
    ]
    detail = client.wechat_video_detail(object_id="15003884913433053492")
    assert detail.object_id == "15003884913433053492"
    assert transports[1].requests[0]["body"] == {"object_id": "15003884913433053492", "raw": False}


def test_wechat_video_detail_cache_reuses_success_and_separates_api_keys() -> None:
    _reset_wechat_detail_cache()
    payload = {"code": 200, "data": {"id": "detail", "like_count": 7}}
    first_transport = FakeTransport([payload])
    second_transport = FakeTransport([payload])
    first = ViralSourceClient(
        api_key="key-one", transport=first_transport, detail_transport=first_transport
    )
    second = ViralSourceClient(
        api_key="key-two", transport=second_transport, detail_transport=second_transport
    )

    assert first.wechat_video_detail(export_id="export/shared").like_count == 7
    assert first.wechat_video_detail(export_id="export/shared").like_count == 7
    assert second.wechat_video_detail(export_id="export/shared").like_count == 7

    assert len(first_transport.requests) == 1
    assert len(second_transport.requests) == 1


def test_wechat_video_detail_cache_merges_concurrent_requests() -> None:
    _reset_wechat_detail_cache()

    class SlowTransport(FakeTransport):
        def request(self, method: str, url: str, *, headers, body=None) -> bytes:
            time.sleep(0.05)
            return super().request(method, url, headers=headers, body=body)

    payload = {"code": 200, "data": {"id": "detail", "comment_count": 3}}
    transport = SlowTransport([payload])
    client = ViralSourceClient(
        api_key="shared-key", transport=transport, detail_transport=transport
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        details = list(
            pool.map(
                lambda _: client.wechat_video_detail(
                    export_id="export/shared", object_nonce_id="nonce"
                ),
                range(2),
            )
        )

    assert [detail.comment_count for detail in details] == [3, 3]
    assert len(transport.requests) == 1


def test_wechat_video_detail_failures_are_not_cached() -> None:
    _reset_wechat_detail_cache()
    failure = {"code": 200, "data": {"ret": -1, "error": "private upstream text"}}
    transport = FakeTransport([failure, failure])
    client = ViralSourceClient(
        api_key="failure-key", transport=transport, detail_transport=transport
    )

    for _ in range(2):
        with pytest.raises(ViralSourceError):
            client.wechat_video_detail(export_id="export/failure")

    assert len(transport.requests) == 2


def test_wechat_video_detail_error_shape_raises() -> None:
    client, _ = _client([])
    client._detail_transport.payloads = [  # noqa: SLF001
        {"code": 200, "data": {"ret": -2026, "error": "未返回视频对象"}}
    ]
    with pytest.raises(ViralSourceError):
        client.wechat_video_detail(export_id="export/expired")


def test_wechat_video_detail_rejects_actual_error_envelope_with_neutral_message() -> None:
    client, _ = _client([])
    client._detail_transport.payloads = [  # noqa: SLF001
        {
            "code": 200,
            "data": {
                "error": "upstream vendor failure",
                "ret": -1,
                "message": "request failed",
                "debug_id": "debug-secret",
                "debug_info": {"provider": "vendor-name"},
            },
        }
    ]

    with pytest.raises(ViralSourceError) as exc_info:
        client.wechat_video_detail(export_id="export/expired")

    message = str(exc_info.value).lower()
    assert "vendor" not in message
    assert "debug" not in message
    assert "tikhub" not in message


def test_non_200_envelope_raises() -> None:
    failure = {"code": 429, "message": "rate limited"}
    client, _ = _client([failure, failure, failure])
    with pytest.raises(ViralSourceError):
        client.douyin_search(keyword="乡墅")


@pytest.mark.parametrize("status", [429, 502])
def test_retry_on_retryable_envelope_then_succeeds(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.viral_tikhub.time.sleep", lambda seconds: sleeps.append(seconds))
    client, transports = _client(
        [
            {"code": status, "message": "temporary failure"},
            {"code": 200, "data": {"business_data": []}},
        ]
    )

    assert client.douyin_search(keyword="乡墅") == []
    assert len(transports[0].requests) == 2
    assert sleeps == [0.5]


def test_http_error_log_does_not_echo_upstream_response(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail_request(*args: object, **kwargs: object) -> bytes:
        raise HTTPError(
            "https://source.test/search?token=query-secret",
            502,
            "bad gateway",
            hdrs=None,
            fp=io.BytesIO(b'{"api_key":"body-secret","provider":"vendor-name"}'),
        )

    monkeypatch.setattr("app.viral_tikhub.urlopen", fail_request)
    caplog.set_level(logging.WARNING, logger="app.viral_tikhub")

    with pytest.raises(ViralSourceError):
        UrllibViralHttpTransport().request(
            "POST",
            "https://source.test/search",
            headers={"Authorization": "Bearer header-secret"},
        )

    assert "body-secret" not in caplog.text
    assert "query-secret" not in caplog.text
    assert "header-secret" not in caplog.text
    assert "vendor-name" not in caplog.text


def test_client_dict_has_no_vendor_names() -> None:
    aweme = {
        "aweme_id": "1",
        "desc": "测试",
        "video": {"cover": {"url_list": ["https://cdn/c.webp"]}},
    }
    video = normalize_douyin_aweme(aweme, "")
    assert video is not None
    serialized = json.dumps(video.to_client_dict(), ensure_ascii=False).lower()
    assert "tikhub" not in serialized
    assert "api_key" not in serialized


def test_client_from_config_requires_api_key() -> None:
    with pytest.raises(ViralSourceUnavailable):
        viral_source_client_from_config({"api_key": " "})


def test_play_url_prefers_h264_over_smaller_bytevc2():
    block = {
        "is_bytevc1": 0,
        "play_addr": {"height": 1024, "url_list": ["https://cdn.test/h264.mp4"]},
        "bit_rate": [
            {
                "is_bytevc1": 2,
                "is_h265": 2,
                "play_addr": {"height": 540, "url_list": ["https://cdn.test/bytevc2.mp4"]},
            },
            {
                "is_bytevc1": 1,
                "play_addr": {"height": 720, "url_list": ["https://cdn.test/hevc.mp4"]},
            },
        ],
    }
    assert pick_douyin_play_url(block) == "https://cdn.test/h264.mp4"


# ---------------------------------------------------------------------------
# 429/5xx 重试与退避：仅重试"请求未被受理"的状态；网络错误不重试
# ---------------------------------------------------------------------------


class FlakyStatusTransport:
    """前 N 次抛出带状态码的传输层错误，之后恢复返回空搜索页."""

    def __init__(self, fail_times: int, status: int, retry_after: float | None = None) -> None:
        self.fail_times = fail_times
        self.status = status
        self.retry_after = retry_after
        self.calls = 0

    def request(self, method: str, url: str, *, headers, body=None) -> bytes:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ViralSourceHttpStatusError(self.status, self.retry_after)
        return json.dumps({"code": 200, "data": {"business_data": []}}).encode("utf-8")


class NetworkErrorTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, method: str, url: str, *, headers, body=None) -> bytes:
        self.calls += 1
        raise ViralSourceError("爆款数据源网络异常，请稍后重试")


def _retry_client(transport: Any) -> ViralSourceClient:
    return ViralSourceClient(
        api_key="test-key",
        transport=transport,
        detail_transport=FakeTransport([]),
    )


def test_retry_on_429_honours_retry_after_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.viral_tikhub.time.sleep", lambda seconds: sleeps.append(seconds))
    transport = FlakyStatusTransport(fail_times=1, status=429, retry_after=2.0)

    videos = _retry_client(transport).douyin_search(keyword="别墅")

    assert videos == []
    assert transport.calls == 2
    assert sleeps == [2.0]


def test_retry_on_429_uses_backoff_and_stops_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.viral_tikhub.time.sleep", lambda seconds: sleeps.append(seconds))
    transport = FlakyStatusTransport(fail_times=5, status=429)

    with pytest.raises(ViralSourceError):
        _retry_client(transport).douyin_search(keyword="别墅")

    assert transport.calls == 3
    assert sleeps == [0.5, 1.0]


def test_retry_on_5xx_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.viral_tikhub.time.sleep", lambda seconds: None)
    transport = FlakyStatusTransport(fail_times=1, status=502)

    assert _retry_client(transport).douyin_search(keyword="别墅") == []
    assert transport.calls == 2


def test_no_retry_on_other_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.viral_tikhub.time.sleep", lambda seconds: sleeps.append(seconds))
    transport = FlakyStatusTransport(fail_times=3, status=404)

    with pytest.raises(ViralSourceError):
        _retry_client(transport).douyin_search(keyword="别墅")

    assert transport.calls == 1
    assert sleeps == []


def test_no_retry_on_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.viral_tikhub.time.sleep", lambda seconds: sleeps.append(seconds))
    transport = NetworkErrorTransport()

    with pytest.raises(ViralSourceError):
        _retry_client(transport).douyin_search(keyword="别墅")

    assert transport.calls == 1
    assert sleeps == []


def test_transport_exposes_status_and_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = email.message.Message()
    message["Retry-After"] = "3"

    def fail_request(*args: object, **kwargs: object) -> bytes:
        raise HTTPError(
            "https://source.test/search",
            429,
            "too many requests",
            hdrs=message,
            fp=io.BytesIO(b"{}"),
        )

    monkeypatch.setattr("app.viral_tikhub.urlopen", fail_request)

    with pytest.raises(ViralSourceHttpStatusError) as excinfo:
        UrllibViralHttpTransport().request(
            "POST", "https://source.test/search", headers={}, body=b"{}"
        )

    assert excinfo.value.status == 429
    assert excinfo.value.retry_after == 3.0


def test_transport_without_retry_after_header_yields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_request(*args: object, **kwargs: object) -> bytes:
        raise HTTPError(
            "https://source.test/search",
            502,
            "bad gateway",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )

    monkeypatch.setattr("app.viral_tikhub.urlopen", fail_request)

    with pytest.raises(ViralSourceHttpStatusError) as excinfo:
        UrllibViralHttpTransport().request(
            "POST", "https://source.test/search", headers={}, body=b"{}"
        )

    assert excinfo.value.status == 502
    assert excinfo.value.retry_after is None


def test_retry_after_accepts_http_date() -> None:
    from app.viral_tikhub import _retry_after_seconds

    message = email.message.Message()
    message["Retry-After"] = format_datetime(datetime.now(UTC) + timedelta(seconds=3))

    retry_after = _retry_after_seconds(message)

    assert retry_after is not None
    assert 0.0 <= retry_after <= 3.0
