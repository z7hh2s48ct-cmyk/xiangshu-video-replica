"""Resolve supported public links into the existing viral import source model."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.db_portable import BusinessConnection
from app.settings import SettingsRepository, SettingsUnavailableError
from app.viral_tikhub import MAX_TAGS, parse_compact_count

DOUYIDOU_BASE_URL = "https://gateway.diadi.cn"
DOUYIDOU_TIMEOUT_SECONDS = 25.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+")
# 小红书 2025 起分享口令不再带 http:// 前缀；裸域名仅认已支持平台的域名。
_BARE_URL_PATTERN = re.compile(
    r"\b(?:[a-zA-Z0-9-]+\.)*(?:douyin\.com|xiaohongshu\.com|xhslink\.com|iesdouyin\.com)"
    r"(?:/[^\s<>'\"]+)?",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)）]】}"
_WECHAT_HOSTS = {"channels.weixin.qq.com", "weixin.qq.com", "www.weixin.qq.com"}
_DOUYIN_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}
_XIAOHONGSHU_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com"}
# 这些主机只产出短链，无法直接提取内容 ID，需要跟随 302 还原。
_SHORT_LINK_HOSTS = {"v.douyin.com", "z.douyin.com", "xhslink.com", "www.xhslink.com"}
_DOUYIN_ID_PATTERNS = (
    re.compile(r"/video/(\d{15,22})(?!\d)"),
    re.compile(r"/note/(\d{15,22})(?!\d)"),
    re.compile(r"/slides/(\d{15,22})(?!\d)"),
    re.compile(r"[?&]modal_id=(\d{15,22})(?!\d)"),
)
_XHS_ID_PATTERNS = (
    re.compile(r"/explore/([0-9a-fA-F]{24})(?![0-9a-fA-F])"),
    re.compile(r"/discovery/item/([0-9a-fA-F]{24})(?![0-9a-fA-F])"),
    re.compile(r"/user/profile/[^/]+/([0-9a-fA-F]{24})(?![0-9a-fA-F])"),
    re.compile(r"[?&]note_id=([0-9a-fA-F]{24})(?![0-9a-fA-F])"),
)
# 规范形态钉住网关已知稳定入口；如后续真实探针验证 /video/{id} 亦可用，仅需改这里。
DOUYIN_CANONICAL_URL_TEMPLATE = "https://www.douyin.com/jingxuan?modal_id={video_id}"
XIAOHONGSHU_CANONICAL_URL_TEMPLATE = "https://www.xiaohongshu.com/explore/{note_id}"
_MAX_REDIRECT_HOPS = 3
_REDIRECT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

logger = logging.getLogger(__name__)


class ViralLinkError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.uncertain = uncertain


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class DouyidouHttpTransport:
    def request(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        raise NotImplementedError


class UrllibDouyidouHttpTransport(DouyidouHttpTransport):
    def __init__(self, *, timeout_seconds: float = DOUYIDOU_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def request(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "gateway.diadi.cn":
            raise ViralLinkError(
                502,
                "VIRAL_LINK_GATEWAY_INVALID",
                "视频链接解析服务地址异常，请上传 MP4/MOV 文件。",
                retryable=False,
            )
        request = Request(url, headers=dict(headers), method="GET")
        opener = build_opener(NoRedirectHandler())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
        except HTTPError as exc:
            logger.warning("Link resolver returned HTTP %s", exc.code)
            raise
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("Link resolver network request failed: %s", type(exc).__name__)
            raise ViralLinkError(
                504,
                "VIRAL_LINK_SUBMISSION_UNCERTAIN",
                "视频链接解析结果未知，请使用原请求重试查询，或上传 MP4/MOV 文件。",
                retryable=False,
                uncertain=True,
            ) from exc


@dataclass(frozen=True)
class ResolvedViralMetadata:
    """链接解析顺带拿到的可选互动字段；上游没给的一律为 None.

    这些字段用于管理端展示内容质量（头像 / 互动 / 发布时间 / 标签）。解析接口
    并不承诺返回它们，缺失时管理端标注「字段待补全」，绝不填默认值冒充真实数据。
    """

    author_avatar: str | None = None
    verified: bool = False
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    collects: int | None = None
    published_at: int | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedViralLink:
    platform: Literal["douyin", "xiaohongshu"]
    video_id: str
    title: str
    author: str
    cover_url: str | None
    video_url: str | None
    audio_url: str | None
    duration_ms: int
    source_description: str
    metadata: ResolvedViralMetadata = ResolvedViralMetadata()


def normalize_supported_link(raw: str) -> str:
    text = raw.strip()
    match = _URL_PATTERN.search(text)
    if match is None:
        match = _BARE_URL_PATTERN.search(text)
    if match is None:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_INVALID",
            "请输入完整的抖音或小红书视频链接，或上传 MP4/MOV 文件。",
            retryable=False,
        )
    url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        invalid_authority = bool(parsed.username or parsed.password) or parsed.port not in (
            None,
            443 if parsed.scheme == "https" else 80,
        )
    except ValueError:
        invalid_authority = True
        host = ""
    if invalid_authority:
        raise ViralLinkError(
            422, "VIRAL_LINK_INVALID", "视频链接格式无效，请重新复制分享链接。", retryable=False
        )
    if host in _WECHAT_HOSTS or host.endswith(".weixin.qq.com"):
        raise ViralLinkError(
            422,
            "WECHAT_CHANNELS_LINK_UNSUPPORTED",
            "视频号链接暂不支持解析，请上传 MP4 或 MOV 文件。",
            retryable=False,
        )
    if host not in _DOUYIN_HOSTS | _XIAOHONGSHU_HOSTS and not host.endswith(".douyin.com"):
        raise ViralLinkError(
            422,
            "VIRAL_LINK_PLATFORM_UNSUPPORTED",
            "当前支持抖音和小红书视频链接，其他平台请上传 MP4 或 MOV 文件。",
            retryable=False,
        )
    return url


def supported_link_platform(normalized_url: str) -> Literal["douyin", "xiaohongshu"]:
    """Classify only URLs already checked by normalize_supported_link."""
    return "xiaohongshu" if urlsplit(normalized_url).hostname in _XIAOHONGSHU_HOSTS else "douyin"


class LinkRedirectTransport:
    def resolve_redirect(self, url: str, *, headers: Mapping[str, str]) -> str | None:
        """Return the next hop of an HTTP redirect, or None when unavailable."""
        raise NotImplementedError


class UrllibLinkRedirectTransport(LinkRedirectTransport):
    def __init__(self, *, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds

    def resolve_redirect(self, url: str, *, headers: Mapping[str, str]) -> str | None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            return None
        request = Request(url, headers=dict(headers), method="GET")
        opener = build_opener(NoRedirectHandler())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                if response.status in (301, 302, 303, 307, 308):
                    return cast("str | None", response.headers.get("Location"))
                return None
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return exc.headers.get("Location")
            logger.warning("Short-link resolver got HTTP %s", exc.code)
            return None
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("Short-link resolution failed: %s", type(exc).__name__)
            return None


def _query_param(url: str, name: str) -> str | None:
    query = urlsplit(url).query
    if not query:
        return None
    values = parse_qs(query).get(name)
    return values[0] if values else None


def _extract_content_id(url: str, platform: Literal["douyin", "xiaohongshu"]) -> str | None:
    for pattern in _DOUYIN_ID_PATTERNS if platform == "douyin" else _XHS_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            content_id = match.group(1)
            return content_id.lower() if platform == "xiaohongshu" else content_id
    return None


def _redirect_target_allowed(target: str) -> bool:
    try:
        parsed = urlsplit(target)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        return False
    if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
        return False
    return (
        host in _DOUYIN_HOSTS
        or host in _XIAOHONGSHU_HOSTS
        or host.endswith(".douyin.com")
        or host.endswith(".xiaohongshu.com")
        or host.endswith(".xhslink.com")
    )


def _canonical_url(
    platform: Literal["douyin", "xiaohongshu"],
    content_id: str,
    *,
    original: str,
    resolved: str,
) -> str:
    if platform == "douyin":
        return DOUYIN_CANONICAL_URL_TEMPLATE.format(video_id=content_id)
    canonical = XIAOHONGSHU_CANONICAL_URL_TEMPLATE.format(note_id=content_id)
    token = _query_param(original, "xsec_token") or _query_param(resolved, "xsec_token")
    if token:
        token_pair = (("xsec_token", token),)
        canonical += "?" + urlencode(token_pair)
    return canonical


def canonicalize_viral_link(
    normalized_url: str,
    *,
    redirect_transport: LinkRedirectTransport | None = None,
) -> str:
    """把任意受支持的入口链接归一为网关已知稳定的规范形态。

    能直接提取内容 ID 的形态立即重写；短链（v/z.douyin.com、xhslink.com）跟随
    302 还原后再提取。redirect_transport 为 None 时跳过网络还原（离线测试与既有
    行为保持一致）。任何一步失败都原样返回输入，绝不阻塞解析主流程。
    """
    platform = supported_link_platform(normalized_url)
    direct_id = _extract_content_id(normalized_url, platform)
    if direct_id:
        return _canonical_url(platform, direct_id, original=normalized_url, resolved=normalized_url)
    if redirect_transport is None:
        return normalized_url
    try:
        host = (urlsplit(normalized_url).hostname or "").lower()
    except ValueError:
        return normalized_url
    if host not in _SHORT_LINK_HOSTS:
        return normalized_url
    resolved = normalized_url
    for _ in range(_MAX_REDIRECT_HOPS):
        target = redirect_transport.resolve_redirect(resolved, headers=_REDIRECT_HEADERS)
        if not target or not _redirect_target_allowed(target):
            return normalized_url
        try:
            resolved = normalize_supported_link(target)
        except ViralLinkError:
            return normalized_url
        content_id = _extract_content_id(resolved, platform)
        if content_id:
            return _canonical_url(platform, content_id, original=normalized_url, resolved=resolved)
    return normalized_url


def _first_url(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        candidate: object = item
        if isinstance(item, dict):
            candidate = item.get("url") or item.get("play_url")
        if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
            return candidate
    return None


def _coerce_duration_ms(raw_duration: object) -> int:
    """将单个原始时长值归一为毫秒：>10000 视为毫秒，否则视为秒。"""
    if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float, str)):
        return 0
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        return 0
    if duration <= 0:
        return 0
    return round(duration if duration > 10_000 else duration * 1000)


def _duration_ms(payload: dict[str, Any]) -> int:
    # 顶层键优先；douyidou 把时长放在 data.other.duration（毫秒），顶层缺失时兜底读取。
    for key in ("duration", "video_duration", "time", "length"):
        resolved = _coerce_duration_ms(payload.get(key))
        if resolved > 0:
            return resolved
    other = payload.get("other")
    if isinstance(other, dict):
        resolved = _coerce_duration_ms(other.get("duration"))
        if resolved > 0:
            return resolved
    return 0


# 解析上游的字段命名沿用平台侧通用键名；同义键按优先级取第一个可解析值。
_AVATAR_KEYS = ("avatar", "avatar_url", "avatar_thumb", "avatar_medium", "avatar_larger")
_STATISTIC_KEYS = {
    "likes": ("digg_count", "like_count", "likes"),
    "comments": ("comment_count", "comments_count", "comments"),
    "shares": ("share_count", "shares_count", "shares"),
    "collects": ("collect_count", "collects_count", "collects"),
}
_PUBLISHED_AT_KEYS = ("create_time", "createTime", "publish_time", "pub_time")


def _image_url(value: object) -> str | None:
    """头像 / 封面字段可能是字符串、地址数组或多地址对象，只认 http(s) 字符串。"""
    if isinstance(value, str):
        return value if value.startswith(("https://", "http://")) else None
    if isinstance(value, Mapping):
        return _image_url(value.get("url_list")) or _image_url(value.get("url"))
    if isinstance(value, list):
        for item in value:
            resolved = _image_url(item)
            if resolved:
                return resolved
    return None


def _count(value: object) -> int | None:
    """计数只认非负整数与可解析的计数文本；其余（含 bool）一律视为未提供。"""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    parsed = value if isinstance(value, int) else parse_compact_count(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _hashtags(payload: Mapping[str, Any]) -> tuple[str, ...]:
    tags: list[str] = []

    def add(raw: object) -> None:
        tag = str(raw or "").strip()
        if tag and tag not in tags and len(tags) < MAX_TAGS:
            tags.append(tag)

    for key in ("text_extra", "hashtag_list", "tags"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, Mapping):
                add(entry.get("hashtag_name") or entry.get("keyword") or entry.get("name"))
            else:
                add(entry)
    return tuple(tags)


def _optional_metadata(payload: Mapping[str, Any]) -> ResolvedViralMetadata:
    """上游带上互动数据就取用，缺失一律留空.

    链接导入的价值是媒体本身，互动字段是加分项。这里只做只读提取，取不到就留
    None，绝不按默认值编造——管理端据此显示「字段待补全」，运营不会把 0 当成
    真实互动去判断内容质量。
    """
    author = payload.get("author")
    author_block = author if isinstance(author, Mapping) else {}
    statistics = payload.get("statistics")
    # 计数可能在 statistics 子对象里，也可能平铺在顶层；其余字段一律只看顶层。
    stats = statistics if isinstance(statistics, Mapping) else payload

    def first(source: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
        parsed = (_count(source.get(key)) for key in keys)
        return next((value for value in parsed if value is not None), None)

    return ResolvedViralMetadata(
        author_avatar=next(
            (
                resolved
                for key in _AVATAR_KEYS
                if (resolved := _image_url(author_block.get(key) or payload.get(key)))
            ),
            None,
        ),
        verified=bool(author_block.get("is_verified") or payload.get("is_verified")),
        likes=first(stats, _STATISTIC_KEYS["likes"]),
        comments=first(stats, _STATISTIC_KEYS["comments"]),
        shares=first(stats, _STATISTIC_KEYS["shares"]),
        collects=first(stats, _STATISTIC_KEYS["collects"]),
        published_at=first(payload, _PUBLISHED_AT_KEYS),
        tags=_hashtags(payload),
    )


class DouyidouLinkClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        transport: DouyidouHttpTransport | None = None,
        redirect_transport: LinkRedirectTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.transport = transport or UrllibDouyidouHttpTransport()
        self.redirect_transport = redirect_transport

    def _request(self, source_url: str) -> dict[str, Any]:
        params = {"app_id": self.app_id, "url": source_url}
        query = urlencode(sorted(params.items()))
        signature = hashlib.md5(  # noqa: S324 - required vendor signature protocol
            f"{query}{self.app_secret}".encode(), usedforsecurity=False
        ).hexdigest()
        endpoint = f"{DOUYIDOU_BASE_URL}/api/parse?{query}"
        try:
            body = self.transport.request(endpoint, headers={"Sign": signature})
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError
            decoded = json.loads(body)
            if not isinstance(decoded, dict):
                raise ValueError
            return decoded
        except HTTPError as exc:
            uncertain = exc.code >= 500
            raise ViralLinkError(
                503 if uncertain else 422,
                "VIRAL_LINK_SUBMISSION_UNCERTAIN" if uncertain else "VIRAL_LINK_PROVIDER_FAILED",
                (
                    "视频链接解析结果未知，请使用原请求重试查询，或上传 MP4/MOV 文件。"
                    if uncertain
                    else "视频链接暂时无法解析，请检查链接或上传 MP4/MOV 文件。"
                ),
                retryable=False,
                uncertain=uncertain,
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ViralLinkError(
                502,
                "VIRAL_LINK_RESPONSE_INVALID",
                "视频链接解析结果异常，请稍后重试或上传 MP4/MOV 文件。",
                retryable=False,
            ) from exc

    def resolve(self, raw_url: str, *, purpose: str) -> ResolvedViralLink:
        source_url = normalize_supported_link(raw_url)
        source_url = canonicalize_viral_link(source_url, redirect_transport=self.redirect_transport)
        platform = supported_link_platform(source_url)
        response = self._request(source_url)
        if response.get("code") != 0:
            message = str(response.get("message") or response.get("msg") or "")
            expired = any(word in message for word in ("过期", "失效", "不存在", "下架"))
            raise ViralLinkError(
                410 if expired else 422,
                "VIRAL_LINK_EXPIRED" if expired else "VIRAL_LINK_PARSE_FAILED",
                (
                    "视频链接已过期，请重新复制链接或上传 MP4/MOV 文件。"
                    if expired
                    else "视频链接无法解析，请检查链接或上传 MP4/MOV 文件。"
                ),
                retryable=False,
            )
        payload = response.get("data")
        if not isinstance(payload, dict):
            payload = {}
        video_url = _first_url(payload.get("video"))
        audio_url = _first_url(payload.get("audio"))
        if not video_url:
            raise ViralLinkError(
                422,
                "VIRAL_LINK_MEDIA_MISSING",
                "链接中没有可用视频，请上传 MP4 或 MOV 文件。",
                retryable=False,
            )
        source_id = next(
            (
                str(payload[key]).strip()
                for key in (
                    ("note_id", "item_id", "id")
                    if platform == "xiaohongshu"
                    else ("aweme_id", "item_id", "id")
                )
                if payload.get(key)
            ),
            "",
        )
        id_pattern = r"[0-9a-fA-F]{24}" if platform == "xiaohongshu" else r"\d{15,22}"
        if not re.fullmatch(id_pattern, source_id):
            raise ViralLinkError(
                502,
                "VIRAL_LINK_NATIVE_ID_INVALID",
                "链接未返回有效视频标识，请上传 MP4 或 MOV 文件。",
                retryable=False,
            )
        video_id = source_id.lower() if platform == "xiaohongshu" else source_id
        author = payload.get("author")
        author_name = ""
        if isinstance(author, dict):
            author_name = str(author.get("nickname") or author.get("name") or "")
        elif isinstance(author, str):
            author_name = author
        text = str(payload.get("text") or "").strip()
        title = str(payload.get("title") or "").strip() or text[:40] or "链接视频"
        cover = payload.get("cover")
        metadata = _optional_metadata(payload)
        return ResolvedViralLink(
            platform=platform,
            video_id=video_id,
            title=title[:200],
            author=author_name[:120],
            cover_url=(str(cover) if isinstance(cover, str) and cover else None),
            video_url=video_url,
            audio_url=audio_url,
            duration_ms=_duration_ms(payload),
            source_description=text[:2000],
            metadata=metadata,
        )


def douyidou_link_client_from_settings(conn: BusinessConnection) -> DouyidouLinkClient:
    try:
        config = SettingsRepository(conn).load_provider_config("douyidou")
    except SettingsUnavailableError as exc:
        raise ViralLinkError(
            503,
            "VIRAL_LINK_SETTINGS_UNAVAILABLE",
            "视频链接解析服务配置不可用，请联系管理员或上传 MP4/MOV 文件。",
        ) from exc
    app_id = config.get("app_id", "").strip()
    app_secret = config.get("app_secret", "").strip()
    if not app_id or not app_secret:
        raise ViralLinkError(
            503,
            "VIRAL_LINK_NOT_CONFIGURED",
            "视频链接解析服务尚未配置，请联系管理员或上传 MP4/MOV 文件。",
        )
    return DouyidouLinkClient(
        app_id=app_id,
        app_secret=app_secret,
        redirect_transport=UrllibLinkRedirectTransport(),
    )
