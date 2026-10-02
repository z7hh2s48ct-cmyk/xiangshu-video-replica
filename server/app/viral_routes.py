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
from app.db_portable import BusinessConnection
from app.media_routes import api_base_url, get_media_storage
from app.permissions import require_not_auditor
from app.script_from_audio import cached_transcript, cached_transcripts
from app.settings import settings_encryption_key
from app.storage import StorageBackendUnavailable, local_download_signature
from app.usage_billing import accept_operation, finish_source, resolve_wallet_owner
from app.viral_keywords import configured_viral_categories
from app.viral_media import (
    VIRAL_MEDIA_URL_TTL,
    ViralMediaPipeline,
    guess_image_content_type,
    viral_cover_key,
)
from app.viral_media_preparation import ViralMediaBusy
from app.viral_refresh import viral_refresh_status
from app.viral_resource_metering import measured_object_chunks
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
    reclaimable_viral_video_ids,
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
    homepageRank: int | None = None
    homepageStartsAt: str | None = None
    homepageEndsAt: str | None = None
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
    # 本账号是否已为「查看详情」付过费（列表 / 收藏 / 详情统一回填，卡片据此
    # 显示「已查看」，也避免把重复查看当成新的收费动作）。
    detailCharged: bool = False


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
    serverTime: str | None = None
    nextChangeAt: str | None = None


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


class ViralDetailRequest(BaseModel):
    platform: str = Field(min_length=1)
    videoId: str = Field(min_length=1, max_length=512)


class ViralDetailBilling(BaseModel):
    """查看详情的计费回执（服务端权威值，客户端只展示）."""

    charged: int
    unit: str
    # 同一条视频对同一个付费账号只扣一次：true 表示复用既有购买，本次未再扣费。
    deduped: bool = False
    # 已下架 / 不可用的内容不计费，供客户端区分「已购买」与「本次不计费」。
    billable: bool = True


class ViralDetailViewResponse(BaseModel):
    item: ViralVideoItem
    billing: ViralDetailBilling


class ViralCopyClaimRequest(BaseModel):
    platform: str = Field(min_length=1)
    videoId: str = Field(min_length=1, max_length=512)


class ViralCopyBilling(BaseModel):
    """获取文案的计费回执（服务端权威值，客户端只展示）."""

    charged: int
    unit: str
    # 同一条视频对同一个付费账号只扣一次：true 表示复用既有购买，本次未再扣费。
    deduped: bool = False
    # 已下架 / 不可用的内容不计费，供客户端区分「已购买」与「本次不计费」。
    billable: bool = True


class ViralCopyClaimResponse(BaseModel):
    # 未命中共享缓存时双 null：这次没有交付，因此也不扣费，客户端按需再走转写。
    text: str | None
    updatedAt: str | None
    billing: ViralCopyBilling


class ViralStatisticsRequest(BaseModel):
    videoIds: list[str] = Field(min_length=1, max_length=12)


class ViralCacheReclaimRequest(BaseModel):
    platform: str = Field(min_length=1)
    # 本地缓存清单由客户端上报：服务端不知道用户机器上有什么，只能回答「该不该留」。
    # 上限 200 条（单行 IN 查询的合理批量），客户端超出时自行分批。
    videoIds: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        min_length=1, max_length=200
    )


class ViralCacheReclaimResponse(BaseModel):
    platform: str
    reclaim: list[str]


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
    detail_charged: bool = False,
) -> ViralVideoItem:
    item = ViralVideoItem(
        **video.to_client_dict(),
        isFavorite=is_favorite,
        availability=availability,
        hasCopy=has_copy,
        detailCharged=detail_charged,
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
    homepage_clock: tuple[str, str | None] = ("", None)
    if featured_only:
        from app.viral_content_state import ready_sql

        clock = conn.execute(
            "SELECT now(),(SELECT min(boundary) FROM viral_videos v "
            "CROSS JOIN LATERAL (VALUES (v.homepage_starts_at),(v.homepage_ends_at)) "
            "change(boundary) WHERE v.platform=%s AND v.homepage_featured=1 "
            f"AND v.deleted_at IS NULL AND {ready_sql()} AND boundary>now() "
            "AND NOT EXISTS (SELECT 1 FROM viral_video_visibility vis "
            "WHERE vis.platform=v.platform AND vis.video_id=v.video_id "
            "AND vis.status!='AVAILABLE'))",
            (platform,),
        ).fetchone()
        homepage_clock = (clock[0].isoformat(), clock[1].isoformat() if clock[1] else None)
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
    detail_charged_ids = viral_detail_charged_ids(
        conn,
        user_id=actor.id,
        platform=platform,
        video_ids=[video.video_id for video in page.items],
    )
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
                detail_charged=video.video_id in detail_charged_ids,
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
        serverTime=homepage_clock[0] if featured_only else None,
        nextChangeAt=homepage_clock[1] if featured_only else None,
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
    detail_charged_by_platform: dict[str, set[str]] = {}
    for video_platform in {video.platform for video in page.items}:
        detail_charged_by_platform[video_platform] = viral_detail_charged_ids(
            conn,
            user_id=actor.id,
            platform=video_platform,
            video_ids=[video.video_id for video in page.items if video.platform == video_platform],
        )
    return ViralFavoritesResponse(
        items=[
            _item(
                video,
                is_favorite=True,
                availability=availability_by_platform[video.platform].get(
                    video.video_id, "available"
                ),
                has_copy=(video.platform, video.video_id) in copy_hits,
                detail_charged=video.video_id in detail_charged_by_platform[video.platform],
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


@router.post("/videos/cache-reclaim", response_model=ViralCacheReclaimResponse)
def audit_local_viral_cache(
    payload: ViralCacheReclaimRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> ViralCacheReclaimResponse:
    """回答「这些本地缓存条目还有没有服务端来源」，供桌面端回收。

    只读、不计费：回收动作发生在用户机器上，但判据必须由服务端给出——删除与下架
    都不会在客户端的旧分页数据里留下痕迹。
    """
    if payload.platform not in _STORED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    return ViralCacheReclaimResponse(
        platform=payload.platform,
        reclaim=reclaimable_viral_video_ids(
            conn,
            user_id=actor.id,
            platform=payload.platform,
            video_ids=list(dict.fromkeys(payload.videoIds)),
        ),
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
        measured_object_chunks(
            storage.iter_object(key, start=start, end=end),
            namespace=storage.cache_namespace,
            key=stored.key,
        ),
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


VIRAL_DETAIL_SERVICE = "viral_detail"
VIRAL_COPY_SERVICE = "viral_copy"


def _detail_source_id(owner_id: str, platform: str, video_id: str) -> str:
    """「查看详情」的台账行键：付费账号 + 平台 + 视频.

    来源标识本身就是去重键——同一条视频对同一个付费账号最多留下一行 ``SUCCEEDED``，
    因此再次查看不会重新扣费。用**付费账号**（钱包主体）而不是操作者：子账号共享
    母账号钱包，也就共享这份已购内容，否则同一个客户会为同一条视频被扣多次。
    """
    return f"viral-detail:{owner_id}:{platform}:{video_id}"


def _detail_purchased(conn: BusinessConnection, source_id: str) -> bool:
    """该账号是否已买过这条视频的详情（只有终态 ``SUCCEEDED`` 算已购买）."""
    row = conn.execute(
        "SELECT 1 FROM billing_operations WHERE service=%s AND source_id=%s "
        "AND state='SUCCEEDED' LIMIT 1",
        (VIRAL_DETAIL_SERVICE, source_id),
    ).fetchone()
    return row is not None


def viral_detail_charged_ids(
    conn: BusinessConnection, *, user_id: str, platform: str, video_ids: list[str]
) -> set[str]:
    """批量收口「本账号已购买详情」的视频 id，供列表 / 收藏回填卡片状态."""
    if not video_ids:
        return set()
    owner_id = resolve_wallet_owner(conn, user_id)
    by_source_id = {
        _detail_source_id(owner_id, platform, video_id): video_id for video_id in video_ids
    }
    rows = conn.execute(
        "SELECT source_id FROM billing_operations WHERE service=%s AND state='SUCCEEDED' "
        "AND source_id = ANY(%s)",
        (VIRAL_DETAIL_SERVICE, list(by_source_id)),
    ).fetchall()
    return {by_source_id[str(row[0])] for row in rows if str(row[0]) in by_source_id}


def _detail_charged(
    conn: BusinessConnection, *, user_id: str, platform: str, video_id: str
) -> bool:
    return video_id in viral_detail_charged_ids(
        conn, user_id=user_id, platform=platform, video_ids=[video_id]
    )


def _copy_source_id(owner_id: str, platform: str, video_id: str) -> str:
    """「获取文案」的台账行键：付费账号 + 平台 + 视频.

    与「查看详情」同一套去重口径——同一条视频对同一个付费账号最多留下一行
    ``SUCCEEDED``。用**付费账号**（钱包主体）而不是操作者：子账号共享母账号钱包，
    也就共享这份已购文案，否则同一个客户会为同一条视频的文案被反复扣费。
    """
    return f"viral-copy:{owner_id}:{platform}:{video_id}"


def _copy_purchased(conn: BusinessConnection, source_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM billing_operations WHERE service=%s AND source_id=%s "
        "AND state='SUCCEEDED' LIMIT 1",
        (VIRAL_COPY_SERVICE, source_id),
    ).fetchone()
    return row is not None


def charge_viral_copy(
    conn: BusinessConnection,
    *,
    actor: AuthenticatedUser,
    platform: str,
    video_id: str,
    billable: bool = True,
) -> tuple[int, bool]:
    """交付共享文案前扣一次「获取文案」费，返回 (本次扣费积分, 是否复用已购).

    零售口径（已拍板）：文案一旦被别人转写进共享缓存，后续账号拿到它照样付费——
    秒回省下的是平台的转写成本，不是用户手里的内容价值；同一条视频对同一个付费
    账号只扣一次。资费 fail-closed（未配置 / 已停用 / 单价为 0 一律拒绝），且与
    ``accept_operation`` 共用同一把用户锁：并发的首次获取在这里排队，后到者看到
    已购买行即走免费分支，不会各扣一次。失败（未命中 / 拒绝 / 事务回滚）都不扣费。
    """
    if not billable:
        return 0, False
    from app.viral_search_routes import require_priced_viral_service

    source_id = _copy_source_id(resolve_wallet_owner(conn, actor.id), platform, video_id)
    if _copy_purchased(conn, source_id):
        return 0, True
    require_not_auditor(
        conn,
        actor=actor,
        action="viral.copy_claim",
        entity_type="viral_video",
        entity_id=video_id,
    )
    require_priced_viral_service(conn, VIRAL_COPY_SERVICE, error_code="VIRAL_COPY_UNPRICED")
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        ("billing:user:" + actor.id,),
    )
    if _copy_purchased(conn, source_id):
        return 0, True
    accept_operation(
        conn,
        user_id=actor.id,
        service=VIRAL_COPY_SERVICE,
        source_id=source_id,
        units=1,
        request_fingerprint=f"{platform}:{video_id}",
    )
    charged = int(
        finish_source(conn, source_id, units=1, succeeded=True, service=VIRAL_COPY_SERVICE) or 0
    )
    return charged, False


def viral_copy_purchased(
    conn: BusinessConnection, *, user_id: str, platform: str, video_id: str
) -> bool:
    """该付费账号是否已买过这条视频的文案（只读状态查询回填 ``purchased``）."""
    return _copy_purchased(
        conn, _copy_source_id(resolve_wallet_owner(conn, user_id), platform, video_id)
    )


@router.get("/videos/{platform}/{video_id:path}", response_model=ViralVideoItem)
def get_viral_video_detail(
    conn: Database,
    actor: AuthenticatedUser,
    platform: str,
    video_id: str,
) -> ViralVideoItem:
    """免费读取单条爆款视频：工作区搜索与旧客户端的只读路径.

    「查看详情」的计费产品走 ``POST /videos/detail``。本端点不扣费，且它下发的
    字段与免费列表完全同源（列表本就免费返回同样的字段），所以它不是绕过计费的
    入口，只是同一条内容在库里的读取口径。
    """
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
        detail_charged=_detail_charged(
            conn, user_id=actor.id, platform=platform, video_id=video_id
        ),
    )


@router.post("/videos/detail", response_model=ViralDetailViewResponse)
def open_viral_video_detail(
    payload: ViralDetailRequest, db: BusinessDbDep
) -> ViralDetailViewResponse:
    """打开爆款视频详情：读内容池 + 按次计费（同一付费账号同一条视频只扣一次）.

    计费口径（已拍板）：详情与统计字段在同一个响应里返回，因此只算一次「查看详情」；
    浏览、缓存、播放都不在本端点计费。预留在 ``accept_operation``、结算在
    ``finish_source``，两者与内容读取同处一个事务——内容读不到（404/409）或事务
    回滚时预留一并消失，**失败不扣费**。

    免费分支有三类，客户端据 ``billing`` 区分：已购买（``deduped``）、已下架 /
    不可用（``billable=false``）、以及审核账号读取**已购买**内容（不产生新扣费）。
    审核账号不得产生新的客户扣费，所以首次购买前先过 ``require_not_auditor``；
    这与 ``/videos/source`` 等计费端点同一口径。
    """
    if payload.platform not in _STORED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    # 循环导入：viral_search_routes 反向依赖本模块的 ViralVideoItem，只能延迟导入。
    from app.viral_search_routes import require_priced_viral_service

    with db.write() as (conn, actor):
        with conn:
            video = _require_stored_video(conn, platform=payload.platform, video_id=payload.videoId)
            availability = viral_video_availability(
                conn, platform=payload.platform, video_id=payload.videoId
            )
            source_id = _detail_source_id(
                resolve_wallet_owner(conn, actor.id), payload.platform, payload.videoId
            )
            billable = availability == "available"
            charged = 0
            deduped = billable and _detail_purchased(conn, source_id)
            if billable and not deduped:
                require_not_auditor(
                    conn,
                    actor=actor,
                    action="viral.detail",
                    entity_type="viral_video",
                    entity_id=payload.videoId,
                )
                require_priced_viral_service(
                    conn, VIRAL_DETAIL_SERVICE, error_code="VIRAL_DETAIL_UNPRICED"
                )
                # 与 accept_operation 同一把用户锁：并发的首次查看在这里排队，
                # 后到者看到已购买行即走免费分支，不会各扣一次。
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("billing:user:" + actor.id,),
                )
                if _detail_purchased(conn, source_id):
                    deduped = True
                else:
                    accept_operation(
                        conn,
                        user_id=actor.id,
                        service=VIRAL_DETAIL_SERVICE,
                        source_id=source_id,
                        units=1,
                        request_fingerprint=f"{payload.platform}:{payload.videoId}",
                    )
                    charged = int(
                        finish_source(
                            conn,
                            source_id,
                            units=1,
                            succeeded=True,
                            service=VIRAL_DETAIL_SERVICE,
                        )
                        or 0
                    )
            item = _item(
                video,
                is_favorite=is_viral_favorite(
                    conn,
                    user_id=actor.id,
                    platform=payload.platform,
                    video_id=payload.videoId,
                ),
                availability=availability,
                has_copy=cached_transcript(
                    conn, platform=payload.platform, video_id=payload.videoId
                )
                is not None,
                detail_charged=deduped or charged > 0,
            )
            from app.viral_content_observations import record_customer_read

            record_customer_read(
                conn,
                platform=payload.platform,
                video_id=payload.videoId,
                user_id=actor.id,
                kind="detail",
            )
    return ViralDetailViewResponse(
        item=item,
        billing=ViralDetailBilling(
            charged=charged,
            unit=SERVICES[VIRAL_DETAIL_SERVICE].unit,
            deduped=deduped,
            billable=billable,
        ),
    )


@router.post("/videos/copy/claim", response_model=ViralCopyClaimResponse)
def claim_viral_video_copy(
    payload: ViralCopyClaimRequest, db: BusinessDbDep
) -> ViralCopyClaimResponse:
    """获取共享文案：命中缓存即扣一次「获取文案」费并下发正文，未命中不扣费.

    共享缓存的写入方是转写链路（``POST /videos/copy`` 与工作台文案工坊），它们各自
    付转写费；本端点是**交付侧**的计费点：谁拿到文案谁付费，秒回别人的转写结果不
    例外。未命中时如实回 null 且分文不扣——没有交付就没有收费。
    """
    if payload.platform not in _STORED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VIRAL_PLATFORM_INVALID", "message": "不支持的视频平台"},
        )
    with db.write() as (conn, actor):
        with conn:
            _require_stored_video(conn, platform=payload.platform, video_id=payload.videoId)
            hit = cached_transcript(conn, platform=payload.platform, video_id=payload.videoId)
            if hit is None:
                return ViralCopyClaimResponse(
                    text=None,
                    updatedAt=None,
                    billing=ViralCopyBilling(charged=0, unit=SERVICES[VIRAL_COPY_SERVICE].unit),
                )
            # 已下架 / 不可用的内容不计费（与「查看详情」同口径），但仍把已有文案
            # 交给用户：内容早已产出，此时再收费等于对一条看不成的视频收钱。
            billable = (
                viral_video_availability(conn, platform=payload.platform, video_id=payload.videoId)
                == "available"
            )
            charged, deduped = charge_viral_copy(
                conn,
                actor=actor,
                platform=payload.platform,
                video_id=payload.videoId,
                billable=billable,
            )
            from app.viral_content_observations import record_customer_read

            record_customer_read(
                conn,
                platform=payload.platform,
                video_id=payload.videoId,
                user_id=actor.id,
                kind="copy",
            )
    return ViralCopyClaimResponse(
        text=hit.result.text,
        updatedAt=hit.updated_at,
        billing=ViralCopyBilling(
            charged=charged,
            unit=SERVICES[VIRAL_COPY_SERVICE].unit,
            deduped=deduped,
            billable=billable,
        ),
    )
