"""爆款视频路由（C4 重启）.

- ``GET /api/viral/videos?platform=&sort=``：按服务端配置的关键词聚合
  两个平台的最近 7 天爆款列表（结果带 TTL 缓存，作为计费护栏）。
- ``POST /api/viral/videos/media``：按需取媒体文件（抖音音频优先/低清
  兜底；视频号解密后直传主存储），返回带签名的可播放地址。

供应商红线：所有响应文案与字段保持中性，不出现数据源供应商名称。
"""

from __future__ import annotations

import hmac
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth import AuthenticatedUser, Database
from app.billing_catalog import SERVICES
from app.billing_meter import billing_context
from app.customer_fence import BusinessDbDep
from app.media_routes import api_base_url, get_media_storage
from app.permissions import require_not_auditor
from app.script_from_audio import cached_transcript, cached_transcripts
from app.settings import settings_encryption_key
from app.storage import StorageBackendUnavailable, local_download_signature
from app.usage_billing import accept_operation, finish_source
from app.viral_keywords import configured_viral_categories
from app.viral_media import (
    VIRAL_MEDIA_URL_TTL,
    ViralMediaPipeline,
    guess_image_content_type,
    viral_cover_key,
)
from app.viral_media_preparation import ViralMediaBusy
from app.viral_refresh import viral_refresh_status
from app.viral_store import (
    InvalidViralCursorError,
    ViralAvailability,
    add_viral_favorite,
    favorite_viral_video_ids,
    fetch_state_is_fresh,
    get_viral_video,
    is_viral_favorite,
    list_favorite_viral_video_page,
    list_viral_video_page,
    remove_viral_favorite,
    validate_viral_cursor,
    viral_fetched_at,
    viral_video_availabilities,
    viral_video_availability,
)
from app.viral_tikhub import (
    PLATFORM_DOUYIN,
    PLATFORM_WECHAT,
    PLATFORM_XIAOHONGSHU,
    ViralSourceClient,
    ViralSourceError,
    ViralSourceUnavailable,
    ViralVideo,
    viral_source_client_from_settings,
)

router = APIRouter(prefix="/api/viral", tags=["viral"])

SORT_HOT = "hot"
SORT_LATEST = "latest"
_VALID_SORTS = (SORT_HOT, SORT_LATEST)
_VALID_PLATFORMS = (PLATFORM_DOUYIN, PLATFORM_WECHAT)
_STORED_PLATFORMS = (*_VALID_PLATFORMS, PLATFORM_XIAOHONGSHU)

_DOUYIN_SORT_TYPE = {SORT_HOT: "1", SORT_LATEST: "2"}
_WECHAT_SORT = {SORT_HOT: "hot", SORT_LATEST: "latest"}

VIRAL_LIST_CACHE_TTL = timedelta(days=7)
VIRAL_MEDIA_FRESHNESS = timedelta(minutes=10)
# 源站直链只活几小时（§10）。这里给客户端一个保守上界用于安排下载窗口，
# 不是源站的承诺值，过期仍以源站实际响应为准。
VIRAL_SOURCE_URL_TTL = timedelta(hours=2)
logger = logging.getLogger(__name__)
# 桌面单进程：同平台并发页面请求共享一次回源，库内时间戳仍是刷新依据。


class ViralVideoItem(BaseModel):
    homepageFeatured: bool = False
    platform: str
    videoId: str
    category: str
    title: str
    sourceDescription: str | None
    author: str
    authorAvatar: str | None
    verified: bool
    coverUrl: str | None
    durationMs: int
    likes: int
    comments: int | None
    shares: int | None
    collects: int | None
    publishedAt: int | None
    publishedDisplay: str | None
    likeDisplay: str | None
    tags: list[str]
    hasPlayableAudio: bool
    playUrl: str | None = None
    native: dict[str, Any] = Field(default_factory=dict)
    isFavorite: bool = False
    hasCopy: bool = False
    availability: Literal["available", "hidden", "unavailable"] = "available"


class ViralListResponse(BaseModel):
    platform: str
    sort: str
    categories: list[str]
    items: list[ViralVideoItem]
    fetchedAt: str | None
    dataVersion: str | None
    source: Literal["database"] = "database"
    stale: bool = False
    refreshing: bool = False
    refreshError: str | None = None
    total: int
    hasMore: bool
    nextCursor: str | None


class ViralFavoritesResponse(BaseModel):
    items: list[ViralVideoItem]
    total: int
    hasMore: bool
    nextCursor: str | None


class ViralFavoriteMutationResponse(BaseModel):
    isFavorite: bool


class ViralMediaRequest(BaseModel):
    platform: str = Field(min_length=1)
    videoId: str = Field(min_length=1)
    # 播放需要视频；缺省按管线默认（抖音音频优先）。
    kind: Literal["audio", "video"] | None = None


class ViralStatisticsRequest(BaseModel):
    videoIds: list[str] = Field(min_length=1, max_length=12)


class ViralStatisticsResponse(BaseModel):
    items: list[ViralVideoItem]


class ViralMediaResponse(BaseModel):
    kind: str
    url: str
    contentType: str
    cacheHit: bool
    video: ViralVideoItem | None = None


class ViralSourceRequest(BaseModel):
    platform: str = Field(min_length=1)
    videoId: str = Field(min_length=1, max_length=512)


class ViralSourceBilling(BaseModel):
    charged: int
    unit: str


class ViralSourceResponse(BaseModel):
    platform: str
    videoId: str
    fullUrl: str
    # 视频号专用：与 fullUrl 同批下发。每次请求都会变，客户端不得缓存。
    decodeKey: str | None = None
    contentType: str = "video/mp4"
    # 直链有效期仅供客户端参考的保守上界，不是源站承诺。
    expiresAt: str
    billing: ViralSourceBilling


def _now() -> datetime:
    return datetime.now(UTC)


def get_viral_source_client(conn: Database) -> ViralSourceClient | None:
    """数据源客户端依赖入口（测试可通过 dependency_overrides 替换）."""
    try:
        return viral_source_client_from_settings(conn)
    except ViralSourceUnavailable:
        return None


ViralSourceClientDep = Annotated[ViralSourceClient | None, Depends(get_viral_source_client)]


def _item(
    video: ViralVideo,
    *,
    is_favorite: bool = False,
    availability: ViralAvailability = "available",
    has_copy: bool = False,
) -> ViralVideoItem:
    item = ViralVideoItem(
        **video.to_client_dict(),
        isFavorite=is_favorite,
        availability=availability,
        hasCopy=has_copy,
    )
    if not video.cover_key:
        item.coverUrl = None
    if item.coverUrl and item.coverUrl.startswith("/"):
        # 自有稳定封面路由：下发绝对地址，跨源前端（桌面/开发）可直接加载。
        item.coverUrl = f"{api_base_url()}{item.coverUrl}"
    return item


@router.get("/videos", response_model=ViralListResponse)
def list_viral_videos(
    conn: Database,
    actor: AuthenticatedUser,
    client: ViralSourceClientDep,
    platform: str = PLATFORM_DOUYIN,
    sort: str = SORT_HOT,
    limit: Annotated[int, Query(ge=1, le=50)] = 12,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    featured_only: bool = False,
) -> ViralListResponse:
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    if sort not in _VALID_SORTS:
        raise HTTPException(
            status_code=400, detail={"code": "VIRAL_SORT_INVALID", "message": "不支持的排序方式"}
        )
    if cursor is not None:
        try:
            validate_viral_cursor(cursor, platform=platform, sort=sort)
        except InvalidViralCursorError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "VIRAL_CURSOR_INVALID", "message": "分页游标无效，请刷新列表"},
            ) from exc
    fresh = fetch_state_is_fresh(conn, platform=platform, sort=sort, max_age=VIRAL_LIST_CACHE_TTL)
    refreshing, refresh_error = viral_refresh_status(conn, platform=platform, sort=SORT_HOT)
    try:
        page = list_viral_video_page(
            conn,
            platform=platform,
            sort=sort,
            limit=limit,
            cursor=cursor,
            featured_only=featured_only,
        )
    except InvalidViralCursorError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_CURSOR_INVALID", "message": "分页游标无效，请刷新列表"},
        ) from exc
    favorite_ids = favorite_viral_video_ids(
        conn,
        user_id=actor.id,
        platform=platform,
        video_ids=[video.video_id for video in page.items],
    )
    availability_by_id = viral_video_availabilities(
        conn,
        platform=platform,
        video_ids=[video.video_id for video in page.items],
    )
    # 浏览列表同样回填共享文案命中：与搜索接口口径一致，卡片不用先搜一次
    # 才知道这篇文案已经提取过。
    copy_hits = cached_transcripts(conn, [(video.platform, video.video_id) for video in page.items])
    return ViralListResponse(
        platform=platform,
        sort=sort,
        categories=configured_viral_categories(conn),
        items=[
            _item(
                video,
                is_favorite=video.video_id in favorite_ids,
                availability=availability_by_id.get(video.video_id, "available"),
                has_copy=(video.platform, video.video_id) in copy_hits,
            )
            for video in page.items
        ],
        fetchedAt=viral_fetched_at(conn, platform=platform, sort=sort),
        dataVersion=viral_fetched_at(conn, platform=platform, sort=sort),
        stale=not fresh,
        refreshing=refreshing,
        refreshError=refresh_error,
        total=page.total,
        hasMore=page.has_more,
        nextCursor=page.next_cursor,
    )


@router.get("/favorites", response_model=ViralFavoritesResponse)
def list_viral_favorites(
    conn: Database,
    actor: AuthenticatedUser,
    platform: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 24,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> ViralFavoritesResponse:
    if platform is not None and platform not in _STORED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    try:
        page = list_favorite_viral_video_page(
            conn,
            user_id=actor.id,
            platform=platform,
            limit=limit,
            cursor=cursor,
        )
    except InvalidViralCursorError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_CURSOR_INVALID", "message": "分页游标无效，请刷新列表"},
        ) from exc
    availability_by_platform: dict[str, dict[str, ViralAvailability]] = {}
    for video_platform in {video.platform for video in page.items}:
        availability_by_platform[video_platform] = viral_video_availabilities(
            conn,
            platform=video_platform,
            video_ids=[video.video_id for video in page.items if video.platform == video_platform],
        )
    copy_hits = cached_transcripts(conn, [(video.platform, video.video_id) for video in page.items])
    return ViralFavoritesResponse(
        items=[
            _item(
                video,
                is_favorite=True,
                availability=availability_by_platform[video.platform].get(
                    video.video_id, "available"
                ),
                has_copy=(video.platform, video.video_id) in copy_hits,
            )
            for video in page.items
        ],
        total=page.total,
        hasMore=page.has_more,
        nextCursor=page.next_cursor,
    )


def _require_stored_video(
    conn: Database,
    *,
    platform: str,
    video_id: str,
    require_available: bool = False,
) -> ViralVideo:
    if platform not in _STORED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    video = get_viral_video(conn, platform=platform, video_id=video_id)
    if video is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "VIRAL_VIDEO_NOT_FOUND", "message": "该爆款视频不存在"},
        )
    if (
        require_available
        and viral_video_availability(conn, platform=platform, video_id=video_id) != "available"
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VIRAL_VIDEO_UNAVAILABLE",
                "message": "该爆款视频当前不可用于创作",
            },
        )
    return video


@router.put("/favorites/{platform}/{video_id:path}", response_model=ViralFavoriteMutationResponse)
def add_viral_video_favorite(
    db: BusinessDbDep,
    platform: str,
    video_id: str,
) -> ViralFavoriteMutationResponse:
    with db.write() as (conn, actor):
        _require_stored_video(conn, platform=platform, video_id=video_id, require_available=True)
        add_viral_favorite(conn, user_id=actor.id, platform=platform, video_id=video_id)
    return ViralFavoriteMutationResponse(isFavorite=True)


@router.delete(
    "/favorites/{platform}/{video_id:path}", response_model=ViralFavoriteMutationResponse
)
def remove_viral_video_favorite(
    db: BusinessDbDep,
    platform: str,
    video_id: str,
) -> ViralFavoriteMutationResponse:
    if platform not in _STORED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    with db.write() as (conn, actor):
        remove_viral_favorite(conn, user_id=actor.id, platform=platform, video_id=video_id)
    return ViralFavoriteMutationResponse(isFavorite=False)


@router.post("/videos/statistics", response_model=ViralStatisticsResponse)
def fetch_viral_video_statistics(
    payload: ViralStatisticsRequest,
    conn: Database,
    actor: AuthenticatedUser,
    client: ViralSourceClientDep,
) -> ViralStatisticsResponse:
    from app.viral_statistics import _load_wechat_videos

    videos = _load_wechat_videos(conn, payload.videoIds)
    return ViralStatisticsResponse(items=[_item(video) for video in videos])


@router.post("/videos/media", response_model=ViralMediaResponse)
def fetch_viral_video_media(payload: ViralMediaRequest, db: BusinessDbDep) -> ViralMediaResponse:
    if payload.platform not in _STORED_PLATFORMS:
        raise HTTPException(
            400, detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"}
        )
    with db.write() as (conn, actor):
        video = _require_stored_video(
            conn, platform=payload.platform, video_id=payload.videoId, require_available=True
        )
        storage = get_media_storage(conn)
    try:
        result = ViralMediaPipeline(
            client=None, storage=storage, shared=True, cached_only=True
        ).fetch(video, prefer=payload.kind or "video")
    except ViralMediaBusy as exc:
        raise HTTPException(
            503,
            headers={"Retry-After": "30"},
            detail={"code": "VIRAL_MEDIA_PREPARATION_BUSY", "message": str(exc)},
        ) from exc
    with db.write() as (conn, actor):
        video = _require_stored_video(
            conn, platform=payload.platform, video_id=payload.videoId, require_available=True
        )
    return ViralMediaResponse(
        video=_item(video),
        kind=result.kind,
        url=_browser_playable_url(result.url, actor.id),
        contentType=result.content_type,
        cacheHit=result.cache_hit,
    )


VIRAL_SOURCE_SERVICE = "viral_search_refresh"


def _resolve_source_link(
    client: ViralSourceClient,
    video: ViralVideo,
    *,
    platform: str,
    video_id: str,
) -> tuple[str, str | None]:
    """取源站直链；视频号额外返回**同一批响应**里的 ``decode_key``.

    视频号只加密 MP4 前 128 KiB，密钥流由 ``decode_key`` 生成，而它每次请求都会
    变，所以直链与密钥必须来自同一次详情调用——分两次拿必然对不上。
    """
    if platform == PLATFORM_WECHAT:
        export_id = str(video.native.get("export_id") or "")
        if not export_id:
            raise ViralSourceError("该视频缺少可用的资源标识")
        detail = client.wechat_video_detail(
            export_id=export_id,
            object_nonce_id=video.native.get("object_nonce_id") or None,
        )
        if not detail.full_url or not detail.decode_key:
            raise ViralSourceError("该视频素材暂时无法获取，请稍后重试")
        return detail.full_url, detail.decode_key
    refreshed = client.douyin_refresh(platform=platform, video_id=video_id)
    # 上游是按关键词「模拟」刷新的，必须精确匹配回同一个 video_id，
    # 否则会把别的视频的直链下发给客户端。
    exact = [item for item in refreshed if item.video_id == video_id and item.play_url]
    if not exact:
        raise ViralSourceError("该视频素材暂时无法获取，请稍后重试")
    return exact[0].play_url or "", None


@router.post("/videos/source", response_model=ViralSourceResponse)
def fetch_viral_video_source(
    payload: ViralSourceRequest,
    db: BusinessDbDep,
    client: ViralSourceClientDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ViralSourceResponse:
    """下发源站直链（视频号额外**同批**下发 ``decode_key``），不代理下载、不落存储.

    P2 客户端本地缓存的前置：客户端拿到直链后自行多线程取回并在本地解密
    （决策 #17），平台不再代理下载、也不再为播放生成签名流（§6.3）。这与既有
    ``/videos/media`` 的区别正在于此——后者是 ``cached_only`` 且回服务端签名 URL。

    ``decode_key`` 每次请求都会变化，客户端必须与 ``fullUrl`` 同批取用、不得缓存；
    因此本响应不做任何服务端缓存。

    计费：走既有 ``viral_search_refresh``（爆款视频刷新）科目，按次 1 单位。
    它是真实的上游调用，与 ``viral_search`` 一样必须受 fail-closed 资费守卫约束，
    否则漏配即「能取直链但不收钱」。只有 PENDING（在途重试）复用同一计费轮次，
    已终态的轮次一律开新一轮——每一次真实外呼都对应一次扣费。
    """
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SOURCE_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    if payload.platform not in _VALID_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_SOURCE_UNAVAILABLE",
                "message": "爆款视频数据源暂不可用，请联系管理员。",
            },
        )
    with db.write() as (conn, actor):
        with conn:
            require_not_auditor(
                conn,
                actor=actor,
                action="viral.source",
                entity_type="viral_source",
                entity_id=payload.platform,
            )
            # 只发内容池里已有的视频。否则本端点会变成「拿任意 videoId 去上游取直链」
            # 的开放代理，把供应商配额暴露给任何登录用户。
            video = _require_stored_video(
                conn, platform=payload.platform, video_id=payload.videoId, require_available=True
            )
            # 循环导入：viral_search_routes 反向依赖本模块的 ViralVideoItem，
            # 只能在函数内延迟导入。
            from app.viral_search_routes import require_priced_viral_service

            require_priced_viral_service(
                conn, VIRAL_SOURCE_SERVICE, error_code="VIRAL_SOURCE_UNPRICED"
            )
            source_id = f"viral-source:{actor.id}:{key}"
            latest = conn.execute(
                "SELECT billing_round, state FROM billing_operations "
                "WHERE user_id=%s AND service=%s AND source_id=%s "
                "ORDER BY billing_round DESC LIMIT 1",
                (actor.id, VIRAL_SOURCE_SERVICE, source_id),
            ).fetchone()
            billing_round = 1
            if latest is not None:
                billing_round = int(latest["billing_round"])
                # 只有 PENDING（在途重试）复用轮次；SUCCEEDED 也开新一轮，
                # 否则同一幂等键可以免费反复刷新一条视频的源站直链。
                if str(latest["state"]) != "PENDING":
                    billing_round += 1
            accept_operation(
                conn,
                user_id=actor.id,
                service=VIRAL_SOURCE_SERVICE,
                source_id=source_id,
                units=1,
                billing_round=billing_round,
                request_fingerprint=f"{payload.platform}:{payload.videoId}:{key}",
            )

    def release_reservation() -> None:
        """外呼失败时释放预留（0 单位、未成功），避免凭空扣费。"""
        with db.write() as (fail_conn, _fail_actor):
            with fail_conn:
                pending = fail_conn.execute(
                    "SELECT state FROM billing_operations WHERE service=%s "
                    "AND source_id=%s AND user_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1 FOR UPDATE",
                    (VIRAL_SOURCE_SERVICE, source_id, actor.id),
                ).fetchone()
                if pending is not None and str(pending["state"]) == "PENDING":
                    finish_source(
                        fail_conn,
                        source_id,
                        units=0,
                        succeeded=False,
                        service=VIRAL_SOURCE_SERVICE,
                    )

    try:
        with billing_context(source_id):
            full_url, decode_key = _resolve_source_link(
                client, video, platform=payload.platform, video_id=payload.videoId
            )
    except ViralSourceError as exc:
        release_reservation()
        raise HTTPException(
            status_code=503,
            detail={"code": "VIRAL_SOURCE_UPSTREAM_FAILED", "message": str(exc)},
        ) from exc
    except Exception:
        release_reservation()
        raise
    with db.write() as (conn, completed_actor):
        with conn:
            if completed_actor.id != actor.id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            finish_source(conn, source_id, units=1, succeeded=True, service=VIRAL_SOURCE_SERVICE)
            charged = int(
                conn.execute(
                    "SELECT charged_credits FROM billing_operations "
                    "WHERE service=%s AND source_id=%s AND user_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1",
                    (VIRAL_SOURCE_SERVICE, source_id, actor.id),
                ).fetchone()[0]
            )
    return ViralSourceResponse(
        platform=payload.platform,
        videoId=payload.videoId,
        fullUrl=full_url,
        decodeKey=decode_key,
        expiresAt=(_now() + VIRAL_SOURCE_URL_TTL).isoformat(),
        billing=ViralSourceBilling(charged=charged, unit=SERVICES[VIRAL_SOURCE_SERVICE].unit),
    )


_VIRAL_FILE_SCHEME = "local://"
_VIRAL_KEY_PREFIX = "viral/"
_VIRAL_SIGNATURE_ASSET = "viral-media"
_VIRAL_SIGNATURE_EPOCH = "viral"


def _browser_playable_url(intent_url: str, user_id: str) -> str:
    """把本地存储的 ``local://`` URI 转成浏览器可用的签名文件路由.

    云存储（COS）返回预签名 HTTPS 直链，原样返回；本地盘（桌面单机）
    的 ``local://`` URI 浏览器无法访问，改发自带 HMAC 的文件端点。
    """
    if intent_url.startswith(("http://", "https://")):
        return intent_url
    if not intent_url.startswith(_VIRAL_FILE_SCHEME):
        raise HTTPException(
            status_code=502,
            detail={"code": "VIRAL_MEDIA_URL_UNSUPPORTED"},
        )
    # urlsplit 会把 local:// 后的 bucket 解析成 hostname，path 即 /<key>。
    path = unquote(urlsplit(intent_url).path)
    key = path.lstrip("/")
    if not key.startswith(_VIRAL_KEY_PREFIX):
        raise HTTPException(status_code=502, detail={"code": "VIRAL_MEDIA_URL_UNSUPPORTED"})
    expires_at = str(int(time.time()) + int(VIRAL_MEDIA_URL_TTL.total_seconds()))
    signature = local_download_signature(
        key,
        expires_at,
        user_id=user_id,
        asset_id=_VIRAL_SIGNATURE_ASSET,
        session_epoch=_VIRAL_SIGNATURE_EPOCH,
        secret=settings_encryption_key(),
    )
    return (
        f"{api_base_url()}/api/viral/videos/media/file?key={quote(key)}"
        f"&expires={expires_at}&user_id={quote(user_id)}&sig={signature}"
    )


@router.get("/covers/{platform}/{video_id:path}")
def get_viral_cover(
    conn: Database,
    platform: str,
    video_id: Annotated[str, Path(min_length=1, max_length=256)],
) -> Response:
    """自有存储的长期封面副本（源站签名链接会过期）.

    无需登录：封面本身是公开内容，对象 key 由路由参数确定性派生，
    不接受任意 key。视频号 ID 是可含斜杠的 opaque ID，必须与库中记录精确匹配。
    """
    if platform not in _STORED_PLATFORMS or not video_id:
        raise HTTPException(status_code=404, detail={"code": "OBJECT_NOT_FOUND"})
    video = get_viral_video(conn, platform=platform, video_id=video_id)
    if (
        video is None
        or "\\" in video_id
        or any(part in {".", ".."} for part in video_id.split("/"))
    ):
        raise HTTPException(status_code=404, detail={"code": "OBJECT_NOT_FOUND"})
    storage = get_media_storage(conn)
    key = viral_cover_key(platform, video_id)
    try:
        stored = storage.head_object(key)
    except StorageBackendUnavailable:
        raise HTTPException(
            status_code=503, detail={"code": "STORAGE_BACKEND_UNAVAILABLE"}
        ) from None
    if stored is None:
        raise HTTPException(status_code=404, detail={"code": "OBJECT_NOT_FOUND"})
    content = storage.get_object(key)
    # 本地盘适配器按文件名猜类型，封面 key 无扩展名 → 按魔数自行判定。
    return Response(
        content=content,
        media_type=guess_image_content_type(content, key),
        headers={"Cache-Control": "public, max-age=604800"},
    )


@router.get("/videos/media/file")
def download_viral_media_file(
    conn: Database,
    key: Annotated[str, Query(min_length=1)],
    expires: Annotated[str, Query(min_length=1, max_length=20)],
    user_id: Annotated[str, Query(min_length=1)],
    sig: Annotated[str, Query(min_length=1)],
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    # 签名即授权（绑定 user_id + 过期时间），与本地资产签名下载同一模式；
    # 浏览器 <audio>/<video> 标签无法携带身份头，故不设登录依赖。
    if not expires.isascii() or not expires.isdecimal() or int(expires) < int(time.time()):
        raise HTTPException(status_code=403, detail={"code": "VIRAL_MEDIA_FORBIDDEN"})
    if not key.startswith(_VIRAL_KEY_PREFIX):
        raise HTTPException(status_code=403, detail={"code": "VIRAL_MEDIA_FORBIDDEN"})
    expected = local_download_signature(
        key,
        expires,
        user_id=user_id,
        asset_id=_VIRAL_SIGNATURE_ASSET,
        session_epoch=_VIRAL_SIGNATURE_EPOCH,
        secret=settings_encryption_key(),
    )
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail={"code": "VIRAL_MEDIA_FORBIDDEN"})
    storage = get_media_storage(conn)
    try:
        stored = storage.head_object(key)
        if stored is None:
            raise HTTPException(status_code=404, detail={"code": "OBJECT_NOT_FOUND"})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"code": "OBJECT_NOT_FOUND"}) from None
    except StorageBackendUnavailable:
        raise HTTPException(
            status_code=503, detail={"code": "STORAGE_BACKEND_UNAVAILABLE"}
        ) from None
    start, end, is_partial = _requested_byte_range(range_header, stored.size)
    content_length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "Content-Length": str(content_length),
    }
    if is_partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{stored.size}"
    return StreamingResponse(
        storage.iter_object(key, start=start, end=end),
        status_code=206 if is_partial else 200,
        media_type=stored.content_type,
        headers=headers,
    )


def _requested_byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if size <= 0:
        raise HTTPException(
            status_code=416,
            detail={"code": "VIRAL_MEDIA_RANGE_INVALID"},
            headers={"Content-Range": "bytes */0"},
        )
    if value is None:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(
            status_code=416,
            detail={"code": "VIRAL_MEDIA_RANGE_INVALID"},
            headers={"Content-Range": f"bytes */{size}"},
        )
    raw_start, separator, raw_end = value[6:].partition("-")
    try:
        if not separator:
            raise ValueError
        if raw_start:
            start = int(raw_start)
            end = size - 1 if not raw_end else min(int(raw_end), size - 1)
        else:
            suffix_length = int(raw_end)
            if suffix_length <= 0:
                raise ValueError
            start = max(0, size - suffix_length)
            end = size - 1
        if start < 0 or start >= size or end < start:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(
            status_code=416,
            detail={"code": "VIRAL_MEDIA_RANGE_INVALID"},
            headers={"Content-Range": f"bytes */{size}"},
        ) from exc
    return start, end, True


@router.get("/videos/{platform}/{video_id:path}", response_model=ViralVideoItem)
def get_viral_video_detail(
    conn: Database,
    actor: AuthenticatedUser,
    platform: str,
    video_id: str,
) -> ViralVideoItem:
    video = _require_stored_video(conn, platform=platform, video_id=video_id)
    availability = viral_video_availability(conn, platform=platform, video_id=video_id)
    return _item(
        video,
        is_favorite=is_viral_favorite(
            conn,
            user_id=actor.id,
            platform=platform,
            video_id=video_id,
        ),
        availability=availability,
        has_copy=cached_transcript(conn, platform=platform, video_id=video_id) is not None,
    )
