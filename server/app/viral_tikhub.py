"""爆款视频（抖音 / 视频号）数据源客户端（C4 重启）.

对两个平台的内容搜索接口做统一封装与字段规范化，产出平台无关的
``ViralVideo`` DTO；平台原生字段只保留媒体管线所需的少量键。

- 抖音：综合搜索（一次返回数量浮动，需过滤 type=6 相关词卡）。
- 视频号：搜一搜视频（每次 10 条，靠 cursor 翻页聚合到 30 条）；标题内嵌
  ``<em class="highlight">`` 高亮标记，必须清洗；标签只能从标题 ``#`` 解析。
- 视频号详情（精简结构）：仅媒体管线需要（full_url + decode_key）。

传输层与 ``app.hifly`` 一致：stdlib urllib、可注入以便测试。凭据一律来自
加密供应商配置存储。所有对外错误文案保持中性，不出现数据源供应商名称。
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.db_portable import BusinessConnection
from app.external_calls import endpoint_from_url, recorded_urlopen
from app.settings import SettingsRepository, SettingsUnavailableError

TIKHUB_BASE_URL = "https://api.tikhub.io"

DOUYIN_GENERAL_SEARCH_PATH = "/api/v1/douyin/search/fetch_general_search_v2"
WECHAT_SEARCH_VIDEOS_PATH = "/api/v1/wechat_search/v2/fetch_search_videos"
WECHAT_VIDEO_DETAIL_PATH = "/api/v1/wechat_channels/v2/fetch_video_detail"

# 路径 → 计费元数据 api_type（后台成本报表按节点归集）。
_API_TYPE_BY_PATH = {
    DOUYIN_GENERAL_SEARCH_PATH: "douyin_search",
    WECHAT_SEARCH_VIDEOS_PATH: "wechat_search_page",
    WECHAT_VIDEO_DETAIL_PATH: "wechat_video_detail",
}

# 备用通道配置字段（加密供应商配置存储）。备用入口是主形态（同协议兼容域名），
# 备用密钥用于备用入口属于另一个账号时；两者都缺省即单通道运行。
BACKUP_BASE_URL_FIELD = "backup_base_url"
BACKUP_API_KEY_FIELD = "backup_api_key"

# 通道故障冷却：详情节点要求 ≥30s 超时、搜索响应也是大体积 JSON，主通道刚失败
# 就立刻重试等于白等一次超时。冷却期内优先走备用通道，冷却结束自动恢复尝试。
_CHANNEL_COOLDOWN_SECONDS = 120.0
_CHANNEL_COOLDOWNS: dict[str, float] = {}
_CHANNEL_COOLDOWNS_LOCK = threading.Lock()

PLATFORM_DOUYIN = "douyin"
PLATFORM_XIAOHONGSHU = "xiaohongshu"
PLATFORM_WECHAT = "wechat_channels"

MAX_TAGS = 6
WECHAT_SEARCH_PAGES = 3
_WECHAT_DETAIL_TIMEOUT_SECONDS = 30.0
_WECHAT_DETAIL_CACHE_TTL_SECONDS = 60.0
_WECHAT_DETAIL_CACHE_MAX_SIZE = 128

_EM_TAG_PATTERN = re.compile(r"<em[^>]*>|</em>", re.IGNORECASE)
_IRRELEVANT_GAME_PATTERN = re.compile(r"minecraft|我的世界|(?<!\w)mc(?!\w)", re.IGNORECASE)
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 数据源重试（T6）：仅对 429/5xx 这类「请求未被上游受理」的状态重试，
# 退避尊重 Retry-After（封顶防阻塞 worker），否则 0.5s/1.0s 指数退避。
_VIRAL_SOURCE_MAX_ATTEMPTS = 3
_VIRAL_SOURCE_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0)
_VIRAL_SOURCE_MAX_BACKOFF_SECONDS = 5.0


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _retry_backoff_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, _VIRAL_SOURCE_MAX_BACKOFF_SECONDS)
    index = min(attempt - 1, len(_VIRAL_SOURCE_BACKOFF_SECONDS) - 1)
    return _VIRAL_SOURCE_BACKOFF_SECONDS[index]


logger = logging.getLogger(__name__)


class ViralSourceError(RuntimeError):
    """爆款数据源调用失败（对外文案保持中性，不含供应商名称）。"""


class ViralSourceUnavailable(ViralSourceError):
    """爆款数据源未配置或不可读。"""


class ViralSourceHttpStatusError(ViralSourceError):
    """数据源返回了可分类的 HTTP 状态（429/5xx 驱动重试决策）."""

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__("爆款数据源暂时不可用，请稍后重试")
        self.status = status
        self.retry_after = retry_after


def _retry_after_seconds(headers: Any) -> float | None:
    """从响应头读 Retry-After（秒或 HTTP 日期）；缺失或非法时返回 None."""
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


class ViralHttpTransport:
    """Minimal HTTP surface the client needs (mirrors the Metaso transport)."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        raise NotImplementedError


class UrllibViralHttpTransport(ViralHttpTransport):
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        try:
            # 上游网关（Cloudflare 1010）拦截默认 Python-urllib UA，必须伪装浏览器。
            request = Request(
                url,
                data=body,
                headers={**dict(headers), "User-Agent": _BROWSER_USER_AGENT},
                method=method,
            )
            response_body, _headers, _status = recorded_urlopen(
                request,
                timeout=self.timeout_seconds,
                provider="tikhub",
                endpoint=endpoint_from_url(url),
                opener=urlopen,
            )
            return response_body
        except HTTPError as exc:
            logger.warning(
                "Viral source request failed with HTTP status %s (%s)",
                exc.code,
                type(exc).__name__,
            )
            raise ViralSourceHttpStatusError(exc.code, _retry_after_seconds(exc.headers)) from exc
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("Viral source request failed: %s", type(exc).__name__)
            raise ViralSourceError("爆款数据源网络异常，请稍后重试") from exc


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViralVideo:
    """平台无关的爆款视频条目（客户端 DTO 的唯一来源）."""

    platform: str
    video_id: str
    category: str
    title: str
    author: str
    author_avatar: str | None
    verified: bool
    cover_url: str | None
    duration_ms: int
    likes: int
    comments: int | None
    shares: int | None
    collects: int | None
    published_at: int | None
    published_display: str | None
    like_display: str | None
    tags: list[str] = field(default_factory=list)
    play_url: str | None = None
    audio_url: str | None = None
    native: dict[str, Any] = field(default_factory=dict)
    # 封面落主存储后的对象 key；存在时客户端下发自有稳定地址。
    cover_key: str | None = None
    homepage_featured: bool = False
    # 策展置顶序（T4）：NULL 表示未置顶，featured 列表按 ASC NULLS LAST 排序。
    homepage_rank: int | None = None

    def to_client_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "videoId": self.video_id,
            "homepageFeatured": self.homepage_featured,
            "homepageRank": self.homepage_rank,
            "category": self.category,
            "title": self.title,
            "sourceDescription": str(self.native.get("source_description") or "") or None,
            "author": self.author,
            "authorAvatar": self.author_avatar,
            "verified": self.verified,
            "coverUrl": (
                f"/api/viral/covers/{self.platform}/{quote(self.video_id, safe='')}"
                if self.cover_key
                else self.cover_url
            ),
            "durationMs": self.duration_ms,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "collects": self.collects,
            "publishedAt": self.published_at,
            "publishedDisplay": self.published_display,
            "likeDisplay": self.like_display,
            "tags": list(self.tags[:MAX_TAGS]),
            "hasPlayableAudio": bool(self.audio_url),
            "playUrl": self.play_url,
            "native": {
                key: value
                for key, value in self.native.items()
                if not key.startswith("_") and key != "source_description"
            },
        }


@dataclass(frozen=True)
class DouyinSearchPage:
    """一次抖音综合搜索 = 一页；翻页字段打包进不透明 ``cursor``."""

    videos: list[ViralVideo]
    cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class WechatSearchPage:
    videos: list[ViralVideo]
    cursor: str | None
    has_more: bool


# 时间范围是对外中立的业务枚举；两个数据面取值不同，在此集中映射，
# 搜索层只透传中立值。缺省「week」与历史行为（写死近 7 天）保持一致。
SEARCH_TIME_RANGES = ("all", "day", "week", "half_year")
DOUYIN_PUBLISH_TIME = {"all": "0", "day": "1", "week": "7", "half_year": "180"}
WECHAT_PUBLISH_TIME = {"all": "all", "day": "day", "week": "week", "half_year": "half_year"}


def encode_douyin_search_cursor(*, offset: int, search_id: str, backtrace: str) -> str:
    """把抖音翻页所需的三个上游字段打包成一个不透明游标.

    上游翻页要求同时回传 offset、search_id、backtrace；对客户端只暴露一个
    不透明字符串，避免把数据源的游标结构固化成我们自己的接口契约。
    """
    return json.dumps(
        {"c": offset, "s": search_id, "b": backtrace},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_douyin_search_cursor(cursor: str | None) -> dict[str, Any] | None:
    """还原翻页游标；解析失败一律按首页处理（损坏值不值得让整次搜索报错）."""
    if not cursor:
        return None
    try:
        payload = json.loads(cursor)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("c"), int):
        return None
    if isinstance(payload["c"], bool):
        return None
    return {
        "cursor": payload["c"],
        "search_id": str(payload.get("s") or ""),
        "backtrace": str(payload.get("b") or ""),
    }


def _douyin_next_page_state(data: Mapping[str, Any]) -> tuple[str | None, bool]:
    """从综合搜索响应里提取下一页游标与 has_more（响应是源站结构透传）.

    不同接口版本把翻页字段放在 data 顶层或 data.pagination 下，这里两处
    都兼容；search_id 兜底取 log_pb.impr_id。取不到下一页 offset 或标志位
    明确为否时判为「没有更多」——宁可少翻一页，也不能下发一个翻不动的游标。
    """
    pagination = data.get("pagination")
    nested: Mapping[str, Any] = pagination if isinstance(pagination, Mapping) else {}
    log_pb = data.get("log_pb")
    search_id = str(
        data.get("search_id")
        or nested.get("search_id")
        or (log_pb.get("impr_id") if isinstance(log_pb, Mapping) else "")
        or ""
    )
    backtrace = str(data.get("backtrace") or nested.get("backtrace") or "")
    offset: int | None = None
    for value in (data.get("cursor"), nested.get("offset"), nested.get("cursor")):
        if isinstance(value, int) and not isinstance(value, bool):
            offset = value
            break
    flag = data.get("has_more", data.get("has_more_status"))
    has_more = flag in (1, "1", True) if flag is not None else offset is not None
    if offset is None or not has_more:
        return None, False
    cursor_token = encode_douyin_search_cursor(
        offset=offset, search_id=search_id, backtrace=backtrace
    )
    return cursor_token, True


@dataclass(frozen=True)
class WechatVideoDetail:
    object_id: str
    object_nonce_id: str | None
    title: str
    description: str | None
    nickname: str
    username: str | None
    create_time: int | None
    like_count: int | None
    fav_count: int | None
    forward_count: int | None
    comment_count: int | None
    city: str | None
    full_url: str | None
    decode_key: str | None
    cover_url: str | None
    duration_ms: int | None
    width: int | None
    height: int | None


_WechatDetailCacheKey = tuple[bytes, str, str, str, str]
_WECHAT_DETAIL_CACHE: OrderedDict[_WechatDetailCacheKey, tuple[float, WechatVideoDetail]] = (
    OrderedDict()
)
_WECHAT_DETAIL_CACHE_LOCK = threading.Lock()
_WECHAT_DETAIL_REQUEST_LOCKS = tuple(threading.Lock() for _ in range(32))


def _reset_wechat_detail_cache() -> None:
    """清空进程内短缓存；仅供隔离测试和进程生命周期管理。"""
    with _WECHAT_DETAIL_CACHE_LOCK:
        _WECHAT_DETAIL_CACHE.clear()


# ---------------------------------------------------------------------------
# 纯函数：字段解析与规范化
# ---------------------------------------------------------------------------


def strip_highlight(text: str) -> str:
    """清洗视频号标题里的高亮标记并还原 HTML 实体."""
    return html.unescape(_EM_TAG_PATTERN.sub("", text or "")).strip()


def extract_wechat_tags(title: str) -> list[str]:
    """从视频号标题的 ``#`` 分段解析话题标签（最多 6 个）."""
    cleaned = strip_highlight(title)
    tags: list[str] = []
    for segment in cleaned.split("#")[1:]:
        tag = segment.strip()
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def parse_wechat_duration_ms(duration: str) -> int | None:
    """把 "00:11" / "01:02:03" 形式的时长解析为毫秒."""
    parts = (duration or "").strip().split(":")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def parse_compact_count(text: str | int | None) -> int | None:
    """解析 "6391" / "10万+" / "1.2万" 形式的计数字符串."""
    if text is None:
        return None
    if isinstance(text, int):
        return text
    cleaned = (text or "").strip().rstrip("+").replace(" ", "")
    if not cleaned:
        return None
    try:
        if cleaned.endswith("万"):
            return int(float(cleaned[:-1]) * 10_000)
        return int(cleaned)
    except ValueError:
        return None


def is_irrelevant_viral_video(title: str) -> bool:
    """识别明确的 Minecraft 内容，避免把普通 ``mc`` 子串误杀."""
    return bool(_IRRELEVANT_GAME_PATTERN.search(strip_highlight(title)))


def _optional_count(value: Any) -> int | None:
    """详情计数仅保留上游实际提供的非负整数，缺失或异常值返回 None."""
    if isinstance(value, bool):
        return None
    parsed = parse_compact_count(value) if isinstance(value, (str, int)) else None
    return parsed if parsed is not None and parsed >= 0 else None


def _first_url(block: Mapping[str, Any] | None) -> str | None:
    if not isinstance(block, Mapping):
        return None
    urls = block.get("url_list")
    if isinstance(urls, list) and urls and isinstance(urls[0], str):
        return urls[0]
    return None


def _pick_image_url(block: Mapping[str, Any] | None) -> str | None:
    """封面/头像择址：优先浏览器可渲染的格式与可达主机.

    上游偶发返回 ``.heic`` 模板（Chromium 无法渲染）与 ``c-sign`` 主机
    （部分网络不可达）的变体，按「非 heic > 非 c-sign > 首个」排序取优.
    """
    if not isinstance(block, Mapping):
        return None
    urls = block.get("url_list")
    if not isinstance(urls, list) or not urls:
        return None
    strings = [url for url in urls if isinstance(url, str)]
    if not strings:
        return None

    def rank(url: str) -> tuple[int, int]:
        return (".heic" in url, "c-sign" in url)

    return sorted(strings, key=rank)[0]


def pick_douyin_cover(video_block: Mapping[str, Any]) -> str | None:
    """封面择优：cover/origin_cover/dynamic_cover 全部候选里按可渲染度取优."""
    best: tuple[tuple[int, int], str] | None = None
    for block_name in ("cover", "origin_cover", "dynamic_cover"):
        url = _pick_image_url(video_block.get(block_name))
        if not url:
            continue
        rank = (".heic" in url, "c-sign" in url)
        if best is None or rank < best[0]:
            best = (rank, url)
    return best[1] if best else None


def pick_douyin_play_url(video_block: Mapping[str, Any]) -> str | None:
    """直取源站默认播放地址（分辨率按默认档，不做档位挑选）.

    2026-09-22 产品确认：客户端本地缓存按源站默认分辨率，不采用压缩低清档。
    默认地址即顶层 ``play_addr`` 首个 URL；不再遍历 ``bit_rate`` 档位、
    不做 ByteVC/HEVC 过滤（旧"浏览器预览选最低档"妥协废弃）。
    """
    play_addr = video_block.get("play_addr")
    if not isinstance(play_addr, Mapping):
        return None
    return _first_url(play_addr)


def _wechat_nonce(item: Mapping[str, Any]) -> str | None:
    jump_info = item.get("jumpInfo")
    if not isinstance(jump_info, Mapping):
        return None
    ext_info = jump_info.get("extInfo")
    if isinstance(ext_info, str):
        try:
            ext_info = json.loads(ext_info)
        except json.JSONDecodeError:
            return None
    if isinstance(ext_info, Mapping):
        nonce = ext_info.get("feedNonceId")
        if nonce:
            return str(nonce)
    return None


def normalize_douyin_aweme(aweme: Mapping[str, Any], category: str) -> ViralVideo | None:
    """把抖音综合搜索的 aweme_info 规范化为平台无关 DTO."""
    video_id = str(aweme.get("aweme_id") or "").strip()
    if not video_id:
        return None
    video_block = aweme.get("video")
    if not isinstance(video_block, Mapping):
        return None
    tags: list[str] = []
    text_extra = aweme.get("text_extra")
    if isinstance(text_extra, list):
        for entry in text_extra:
            if not isinstance(entry, Mapping):
                continue
            tag = str(entry.get("hashtag_name") or entry.get("keyword") or "").strip()
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= MAX_TAGS:
                break
    statistics = aweme.get("statistics")
    stats = statistics if isinstance(statistics, Mapping) else {}
    duration = aweme.get("duration")
    if not isinstance(duration, int):
        duration = video_block.get("duration")
    author = aweme.get("author")
    author_block = author if isinstance(author, Mapping) else {}
    avatar = (
        _pick_image_url(author_block.get("avatar_thumb"))
        or _pick_image_url(author_block.get("avatar_medium"))
        or _pick_image_url(author_block.get("avatar_larger"))
    )
    music = aweme.get("music")
    music_block = music if isinstance(music, Mapping) else {}
    play_url_block = music_block.get("play_url")
    audio_url = _first_url(play_url_block if isinstance(play_url_block, Mapping) else None)
    duration_ms = duration if isinstance(duration, int) else 0
    return ViralVideo(
        platform=PLATFORM_DOUYIN,
        video_id=video_id,
        category=category,
        title=str(aweme.get("desc") or "").strip(),
        author=str(author_block.get("nickname") or "").strip(),
        author_avatar=avatar,
        verified=bool(author_block.get("is_verified")),
        cover_url=pick_douyin_cover(video_block),
        duration_ms=max(duration_ms, 0),
        likes=_optional_count(stats.get("digg_count")) or 0,
        comments=_optional_count(stats.get("comment_count")) or 0,
        shares=_optional_count(stats.get("share_count")) or 0,
        collects=_optional_count(stats.get("collect_count")) or 0,
        published_at=_optional_count(aweme.get("create_time")),
        published_display=None,
        like_display=None,
        tags=tags,
        play_url=pick_douyin_play_url(video_block),
        audio_url=audio_url,
        native={
            "aweme_id": video_id,
            "source_description": str(aweme.get("desc") or "").strip(),
            "_playback_version": 1,
        },
    )


def normalize_wechat_item(item: Mapping[str, Any], category: str) -> ViralVideo | None:
    """把视频号搜一搜的视频条目规范化为平台无关 DTO."""
    doc_id = str(item.get("docID") or "").strip()
    export_id = str(item.get("exportId") or "").strip()
    title = strip_highlight(str(item.get("title") or ""))
    if not doc_id or not export_id or not title:
        return None
    source = item.get("source")
    source_block = source if isinstance(source, Mapping) else {}
    like_display = item.get("likeNum")
    width = item.get("width")
    height = item.get("height")
    pub_time = item.get("pubTime")
    nonce = _wechat_nonce(item)
    return ViralVideo(
        platform=PLATFORM_WECHAT,
        video_id=doc_id,
        category=category,
        title=title,
        author=str(source_block.get("title") or "").strip(),
        author_avatar=str(source_block.get("iconUrl") or "") or None,
        verified=False,
        cover_url=str(item.get("image") or "") or None,
        duration_ms=parse_wechat_duration_ms(str(item.get("duration") or "")) or 0,
        likes=parse_compact_count(like_display if isinstance(like_display, str) else None) or 0,
        comments=None,
        shares=None,
        collects=None,
        published_at=_optional_count(pub_time),
        published_display=str(item.get("dateTime") or "") or None,
        like_display=str(like_display) if like_display else None,
        tags=extract_wechat_tags(title),
        play_url=None,
        audio_url=None,
        native={
            "doc_id": doc_id,
            "export_id": export_id,
            "object_nonce_id": nonce or "",
            "width": width if isinstance(width, int) else None,
            "height": height if isinstance(height, int) else None,
        },
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def _maybe_gunzip(payload: bytes) -> bytes:
    """响应体按 gzip 魔数嗅探解压；未压缩内容原样返回.

    请求带 ``Accept-Encoding: gzip`` 后源站可能返回压缩体（urllib 不会自动
    解压）；按魔数而不是响应头判断，这样自建传输桩与忽略该头的网关都兼容。
    """
    if len(payload) >= 2 and payload[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(payload)
        except OSError as exc:
            raise ViralSourceError("爆款数据源返回了无法解析的响应") from exc
    return payload


def _parse_viral_envelope(content: bytes) -> dict[str, Any]:
    """解压并校验响应信封，返回 ``data`` 段；任何异常响应统一抛 ViralSourceError.

    除 JSON 解码失败外还兜住 ``UnicodeDecodeError``（非 UTF-8 的网关错误页）：
    这两类响应都代表"这次调用没拿到数据"，需要能被上层判定为可切换的失败。
    可重试状态（429/5xx）带状态码抛出，供请求层决定"原地重试还是换通道"。
    """
    try:
        envelope = json.loads(_maybe_gunzip(content))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ViralSourceError("爆款数据源返回了无法解析的响应") from exc
    if not isinstance(envelope, dict):
        raise ViralSourceError("爆款数据源响应结构异常")
    code = envelope.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 200:
        if _is_retryable_status(code):
            raise ViralSourceHttpStatusError(code)
        raise ViralSourceError("爆款数据源暂时不可用，请稍后重试")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise ViralSourceError("爆款数据源响应缺少数据")
    if data.get("ret") not in (None, 0) or data.get("error"):
        raise ViralSourceError("爆款内容暂时无法获取，请稍后重试")
    return data


@dataclass(frozen=True)
class _SourceChannel:
    """数据源的一个物理通道：入口 + 凭据（主/备结构相同，只是优先级不同）."""

    name: str
    base_url: str
    api_key: str

    @property
    def fingerprint(self) -> str:
        """通道指纹：兼作冷却表键，顺手避开"换凭据后仍被冷却"的旧状态."""
        digest = hashlib.sha256(f"{self.base_url}\x00{self.api_key}".encode())
        return digest.hexdigest()


def _channel_is_cooling_down(channel: _SourceChannel, *, now: float) -> bool:
    with _CHANNEL_COOLDOWNS_LOCK:
        deadline = _CHANNEL_COOLDOWNS.get(channel.fingerprint)
    return deadline is not None and now < deadline


def _mark_channel_failed(channel: _SourceChannel, *, now: float) -> None:
    with _CHANNEL_COOLDOWNS_LOCK:
        _CHANNEL_COOLDOWNS[channel.fingerprint] = now + _CHANNEL_COOLDOWN_SECONDS


def _clear_channel_cooldown(channel: _SourceChannel) -> None:
    with _CHANNEL_COOLDOWNS_LOCK:
        _CHANNEL_COOLDOWNS.pop(channel.fingerprint, None)


def _reset_channel_cooldowns() -> None:
    """清空通道冷却表（冷却表是进程级状态，测试之间必须隔离）."""
    with _CHANNEL_COOLDOWNS_LOCK:
        _CHANNEL_COOLDOWNS.clear()


class ViralSourceClient:
    """统一爆款数据源客户端（一个实例 = 一组主/备通道 + 两个传输层）."""

    def __init__(
        self,
        *,
        api_key: str,
        transport: ViralHttpTransport | None = None,
        base_url: str = TIKHUB_BASE_URL,
        detail_transport: ViralHttpTransport | None = None,
        backup_api_key: str | None = None,
        backup_base_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self._transport = transport or UrllibViralHttpTransport()
        # 视频号详情接口响应慢，官方要求 ≥30s 超时，独立传输层承载。
        self._detail_transport = detail_transport or UrllibViralHttpTransport(
            timeout_seconds=_WECHAT_DETAIL_TIMEOUT_SECONDS
        )
        self._base_url = base_url.rstrip("/")
        self._backup_base_url = (backup_base_url or "").strip().rstrip("/")
        self._backup_api_key = (backup_api_key or "").strip()
        self._channels = self._build_channels()

    def _build_channels(self) -> tuple[_SourceChannel, ...]:
        """主通道恒在；备用入口非空时追加备用通道（备用密钥缺省沿用主密钥）.

        备用密钥单配（没有备用入口）不构成通道：那只是同一入口换凭据，与"节点
        失效自动切换"无关；配置层已拒绝这种组合。
        """
        channels = [_SourceChannel(name="primary", base_url=self._base_url, api_key=self.api_key)]
        if not self._backup_base_url:
            return tuple(channels)
        backup = _SourceChannel(
            name="backup",
            base_url=self._backup_base_url,
            api_key=self._backup_api_key or self.api_key,
        )
        # 备用入口被原样填成主入口时不产生无意义的重试。
        if (backup.base_url, backup.api_key) != (channels[0].base_url, channels[0].api_key):
            channels.append(backup)
        return tuple(channels)

    def _candidate_channels(self) -> tuple[_SourceChannel, ...]:
        """本轮尝试的通道顺序：处于冷却期的通道只在别无选择时使用."""
        if len(self._channels) == 1:
            return self._channels
        now = time.monotonic()
        available = tuple(
            channel for channel in self._channels if not _channel_is_cooling_down(channel, now=now)
        )
        # 全部处于冷却期时放弃择优：可用性优先于省一次超时。
        return available or self._channels

    def _request(
        self,
        transport: ViralHttpTransport,
        path: str,
        payload: Mapping[str, Any],
        *,
        billing_units: int = 1,
    ) -> dict[str, Any]:
        """按主→备顺序请求；单次失败自动切下一通道，成功立即返回.

        每次物理请求独立计量：客户侧只对真正取到数据的那一次调用计费；供应商侧
        按每次拿到响应独立确认用量，平台成本因此可能高于客户计费。
        """
        channels = self._candidate_channels()
        last_error: ViralSourceError | None = None
        for index, channel in enumerate(channels):
            try:
                data = self._request_once(
                    transport, path, payload, channel=channel, billing_units=billing_units
                )
            except ViralSourceError as exc:
                last_error = exc
                _mark_channel_failed(channel, now=time.monotonic())
                if index + 1 < len(channels):
                    logger.warning(
                        "Viral source %s channel failed (%s), falling back to %s channel",
                        channel.name,
                        type(exc).__name__,
                        channels[index + 1].name,
                    )
                elif len(channels) > 1:
                    logger.warning("Viral source request failed on every configured channel")
                continue
            _clear_channel_cooldown(channel)
            return data
        if last_error is None:  # 通道列表恒非空，仅为类型收窄保留
            raise ViralSourceError("爆款数据源暂时不可用，请稍后重试")
        raise last_error

    def _request_once(
        self,
        transport: ViralHttpTransport,
        path: str,
        payload: Mapping[str, Any],
        *,
        channel: _SourceChannel,
        billing_units: int,
    ) -> dict[str, Any]:
        url = f"{channel.base_url}{path}"
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {channel.api_key}",
            "Accept": "application/json",
            # 服务器与数据源之间是跨境链路、带宽有限，而搜索响应是大体积
            # JSON（数 MB）。开启 gzip 后传输体积通常缩到一个量级以下，
            # 是不换线路前提下唯一有效的提速手段。
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }
        from app.billing_meter import meter_call, set_api_type

        api_type = _API_TYPE_BY_PATH.get(path)
        # 没有备用通道时"原地重试"是唯一的韧性手段；有备用通道时换节点优先——
        # 刚失败的节点立刻重试等于白等一次超时（见 _CHANNEL_COOLDOWN_SECONDS 的取舍）。
        max_attempts = _VIRAL_SOURCE_MAX_ATTEMPTS if len(self._channels) == 1 else 1
        # 搜索是只读查询，HTTP 状态或业务包中的 429/5xx 可安全重试。超时/网络
        # 错误的结果不可知且重试会放大计费成本，不做重试。每次物理外呼都独立
        # 计量：供应商已返回响应即确认供应商用量，客户侧成功与否由整段计量圈定。
        with set_api_type(api_type) if api_type else nullcontext():
            for attempt in range(1, max_attempts + 1):
                try:
                    with meter_call("viral_data", units=billing_units) as metered_call:
                        content = transport.request("POST", url, headers=headers, body=body)
                        metered_call.record_usage()
                        return _parse_viral_envelope(content)
                except ViralSourceHttpStatusError as exc:
                    if attempt >= max_attempts or not _is_retryable_status(exc.status):
                        raise
                    delay = _retry_backoff_delay(attempt, exc.retry_after)
                    logger.warning(
                        "Viral source request got HTTP %s, retrying in %.1fs (attempt %s/%s)",
                        exc.status,
                        delay,
                        attempt,
                        max_attempts - 1,
                    )
                    time.sleep(delay)
        raise AssertionError("数据源重试循环必须返回或抛出异常")

    # -- 抖音 ----------------------------------------------------------------

    def douyin_search(
        self,
        *,
        keyword: str,
        category: str = "",
        sort_type: str = "1",
        publish_time: str = "7",
    ) -> list[ViralVideo]:
        """单次综合搜索（采集调度用，不翻页）."""
        return self.douyin_search_page(
            keyword=keyword,
            category=category,
            sort_type=sort_type,
            publish_time=publish_time,
        ).videos

    def douyin_search_page(
        self,
        *,
        keyword: str,
        category: str = "",
        sort_type: str = "1",
        publish_time: str = "7",
        cursor: str | None = None,
    ) -> DouyinSearchPage:
        """一次综合搜索 = 一页；翻页传回上一页响应派生的不透明游标."""
        payload: dict[str, Any] = {
            "keyword": keyword,
            "sort_type": sort_type,
            "publish_time": publish_time,
            "filter_duration": "0",
            "content_type": "1",
        }
        page_state = decode_douyin_search_cursor(cursor)
        if page_state is not None:
            payload["cursor"] = page_state["cursor"]
            if page_state["search_id"]:
                payload["search_id"] = page_state["search_id"]
            if page_state["backtrace"]:
                payload["backtrace"] = page_state["backtrace"]
        data = self._request(self._transport, DOUYIN_GENERAL_SEARCH_PATH, payload)
        next_cursor, has_more = _douyin_next_page_state(data)
        return DouyinSearchPage(
            videos=self._douyin_videos_from_data(data, category),
            cursor=next_cursor,
            has_more=has_more,
        )

    def _douyin_videos_from_data(self, data: Mapping[str, Any], category: str) -> list[ViralVideo]:
        """把综合搜索响应的 ``business_data`` 卡片解析成 DTO（搜索与刷新共用）."""
        cards = data.get("business_data")
        videos: list[ViralVideo] = []
        if isinstance(cards, list):
            for card in cards:
                if not isinstance(card, Mapping) or card.get("type") != 1:
                    continue
                card_data = card.get("data")
                if not isinstance(card_data, Mapping):
                    continue
                aweme = card_data.get("aweme_info")
                if not isinstance(aweme, Mapping):
                    continue
                normalized = normalize_douyin_aweme(aweme, category)
                if normalized and not is_irrelevant_viral_video(normalized.title):
                    videos.append(normalized)
        return videos

    def douyin_refresh(
        self,
        *,
        platform: str,
        video_id: str,
    ) -> list[ViralVideo]:
        """刷新单条视频信息（刷新封面/播放地址等资源）.

        上游没有单条刷新接口：把视频 ID 当关键词走综合搜索，命中结果由调用方
        按 ID 认领。统一走 :meth:`_request`，因此同样带 gzip、计量与主备切换
        （此前手工拼请求绕过了这三点）。
        """
        data = self._request(
            self._transport,
            DOUYIN_GENERAL_SEARCH_PATH,
            {
                "keyword": f"#{video_id}",
                "sort_type": "1",
                "publish_time": "7",
                "filter_duration": "0",
                "content_type": "1",
            },
        )
        return self._douyin_videos_from_data(data, "")

    # -- 视频号 ---------------------------------------------------------------

    def wechat_search_page(
        self,
        *,
        keyword: str,
        category: str = "",
        sort: str = "hot",
        publish_time: str = "week",
        cursor: str | None = None,
    ) -> WechatSearchPage:
        payload: dict[str, Any] = {
            "keyword": keyword,
            "sort": sort,
            "publish_time": publish_time,
        }
        if cursor:
            payload["cursor"] = cursor
        data = self._request(self._transport, WECHAT_SEARCH_VIDEOS_PATH, payload)
        videos: list[ViralVideo] = []
        results = data.get("results")
        groups = results.get("data") if isinstance(results, Mapping) else None
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, Mapping):
                    continue
                sub_boxes = group.get("subBoxes")
                if not isinstance(sub_boxes, list):
                    continue
                for box in sub_boxes:
                    if not isinstance(box, Mapping):
                        continue
                    items = box.get("items")
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, Mapping):
                            continue
                        normalized = normalize_wechat_item(item, category)
                        if normalized and not is_irrelevant_viral_video(normalized.title):
                            videos.append(normalized)
        cursor_value = data.get("cursor")
        continue_flag = data.get("continue_flag")
        has_more = bool(continue_flag) and isinstance(cursor_value, str) and bool(cursor_value)
        return WechatSearchPage(
            videos=videos,
            cursor=cursor_value if isinstance(cursor_value, str) else None,
            has_more=has_more,
        )

    def wechat_search(
        self,
        *,
        keyword: str,
        category: str = "",
        sort: str = "hot",
        publish_time: str = "week",
        pages: int = WECHAT_SEARCH_PAGES,
    ) -> list[ViralVideo]:
        videos: list[ViralVideo] = []
        cursor: str | None = None
        for _ in range(max(1, pages)):
            page = self.wechat_search_page(
                keyword=keyword,
                category=category,
                sort=sort,
                publish_time=publish_time,
                cursor=cursor,
            )
            videos.extend(page.videos)
            if not page.has_more:
                break
            cursor = page.cursor
        return videos

    def wechat_video_detail(
        self,
        *,
        export_id: str = "",
        object_nonce_id: str | None = None,
        object_id: str | None = None,
    ) -> WechatVideoDetail:
        cache_key: _WechatDetailCacheKey = (
            hashlib.sha256(
                "\x00".join(channel.fingerprint for channel in self._channels).encode("utf-8")
            ).digest(),
            self._base_url,
            export_id,
            object_nonce_id or "",
            object_id or "",
        )
        request_lock = _WECHAT_DETAIL_REQUEST_LOCKS[
            hash(cache_key) % len(_WECHAT_DETAIL_REQUEST_LOCKS)
        ]
        with request_lock:
            now = time.monotonic()
            with _WECHAT_DETAIL_CACHE_LOCK:
                cached = _WECHAT_DETAIL_CACHE.get(cache_key)
                if cached is not None and now - cached[0] <= _WECHAT_DETAIL_CACHE_TTL_SECONDS:
                    _WECHAT_DETAIL_CACHE.move_to_end(cache_key)
                    return cached[1]
                _WECHAT_DETAIL_CACHE.pop(cache_key, None)

            detail = self._fetch_wechat_video_detail(
                export_id=export_id, object_nonce_id=object_nonce_id, object_id=object_id
            )
            with _WECHAT_DETAIL_CACHE_LOCK:
                _WECHAT_DETAIL_CACHE[cache_key] = (time.monotonic(), detail)
                _WECHAT_DETAIL_CACHE.move_to_end(cache_key)
                while len(_WECHAT_DETAIL_CACHE) > _WECHAT_DETAIL_CACHE_MAX_SIZE:
                    _WECHAT_DETAIL_CACHE.popitem(last=False)
            return detail

    def _fetch_wechat_video_detail(
        self, *, export_id: str, object_nonce_id: str | None, object_id: str | None = None
    ) -> WechatVideoDetail:
        payload: dict[str, Any] = {"export_id": export_id, "raw": False}
        if object_id:
            payload = {"object_id": object_id, "raw": False}
        if object_nonce_id:
            payload["object_nonce_id"] = object_nonce_id
        data = self._request(self._detail_transport, WECHAT_VIDEO_DETAIL_PATH, payload)
        media = data.get("media")
        media_block = media if isinstance(media, Mapping) else {}
        location = data.get("location")
        location_block = location if isinstance(location, Mapping) else {}
        city = location_block.get("city")
        duration = media_block.get("duration")
        width = media_block.get("width")
        height = media_block.get("height")
        object_id = data.get("id")
        return WechatVideoDetail(
            object_id=str(object_id) if object_id is not None else "",
            object_nonce_id=str(data.get("object_nonce_id") or "") or None,
            title=strip_highlight(str(data.get("title") or "")),
            description=str(data.get("description") or "") or None,
            nickname=str(data.get("nickname") or "").strip(),
            username=str(data.get("username") or "") or None,
            create_time=_optional_count(data.get("create_time")),
            like_count=_optional_count(data.get("like_count")),
            fav_count=_optional_count(data.get("fav_count")),
            forward_count=_optional_count(data.get("forward_count")),
            comment_count=_optional_count(data.get("comment_count")),
            city=str(city) if city else None,
            full_url=str(media_block.get("full_url") or "") or None,
            decode_key=str(media_block.get("decode_key") or "") or None,
            cover_url=str(media_block.get("cover_url") or "") or None,
            duration_ms=int(duration) * 1000 if isinstance(duration, int) else None,
            width=width if isinstance(width, int) else None,
            height=height if isinstance(height, int) else None,
        )


def viral_source_client_from_config(config: Mapping[str, str]) -> ViralSourceClient:
    api_key = (config.get("api_key") or "").strip()
    if not api_key:
        raise ViralSourceUnavailable("爆款视频数据源未配置，请联系管理员")
    return ViralSourceClient(
        api_key=api_key,
        backup_base_url=(config.get(BACKUP_BASE_URL_FIELD) or "").strip() or None,
        backup_api_key=(config.get(BACKUP_API_KEY_FIELD) or "").strip() or None,
    )


def viral_source_client_from_settings(conn: BusinessConnection) -> ViralSourceClient:
    try:
        config = SettingsRepository(conn).load_provider_config("tikhub")
    except SettingsUnavailableError as exc:
        raise ViralSourceUnavailable("爆款视频数据源暂不可用，请联系管理员") from exc
    return viral_source_client_from_config(config)
