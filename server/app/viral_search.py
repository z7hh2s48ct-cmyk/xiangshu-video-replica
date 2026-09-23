"""搜索驱动的爆款业务（P1）：外呼 → 封面归档 → 计费 → 落库.

搜索成为唯一内容入口后，采集调度退场（design §5.5）。本模块只做四件事：

1. 按平台执行一次搜索外呼（抖音单次列表 / 视频号游标翻页）；
2. 把命中视频的封面归档到自有存储（源站签名链接会过期）；
3. 幂等地预留/推进客户侧搜索计费（每翻页一次 = 一次计量）；
4. 把结果 upsert 进内容池、回写封面 key、记录客户发现、结算计费单。

供应商成本由 ``billing_meter.meter_call`` 在 ``viral_tikhub._request`` 内记账
（``billing_context(source_id)`` 存在时自动落客户单的 source attempt）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from app.admin_dates import SHANGHAI
from app.db_portable import BusinessConnection
from app.usage_billing import accept_operation, finish_source
from app.viral_media import CoverEnricher, UrlFetcher, ViralStorage
from app.viral_store import mark_viral_discoveries, update_viral_cover, upsert_viral_videos
from app.viral_tikhub import (
    PLATFORM_DOUYIN,
    PLATFORM_WECHAT,
    ViralSourceClient,
    ViralVideo,
)

SEARCH_SERVICE = "viral_search"
SEARCH_COVER_MAX_BYTES = 10 * 1024 * 1024
SEARCH_COVER_WORKERS = 8


@dataclass(frozen=True)
class ViralSearchPage:
    """一次搜索外呼的结果（两个平台归一后的形态）."""

    items: list[ViralVideo]
    cursor: str | None
    has_more: bool


def search_date_shanghai(now: datetime | None = None) -> str:
    """发现日期按上海日历归属，客户端与后台看到同一天."""
    instant = now or datetime.now(UTC)
    return instant.astimezone(SHANGHAI).strftime("%Y-%m-%d")


def run_viral_search(
    client: ViralSourceClient,
    *,
    keyword: str,
    platform: str,
    cursor: str | None = None,
) -> ViralSearchPage:
    """执行一次搜索外呼：抖音单次列表（无翻页）；视频号按游标翻页."""
    if platform == PLATFORM_WECHAT:
        page = client.wechat_search_page(keyword=keyword, cursor=cursor)
        return ViralSearchPage(items=list(page.videos), cursor=page.cursor, has_more=page.has_more)
    if platform != PLATFORM_DOUYIN:
        raise ValueError(f"unsupported search platform: {platform}")
    return ViralSearchPage(
        items=list(client.douyin_search(keyword=keyword)), cursor=None, has_more=False
    )


def archive_search_covers(
    storage: ViralStorage,
    videos: list[ViralVideo],
    *,
    max_workers: int = SEARCH_COVER_WORKERS,
) -> list[ViralVideo]:
    """并发把搜索结果封面归档到自有存储；单条失败保留源站链接兜底."""
    if not videos:
        return []
    enricher = CoverEnricher(storage=storage, fetcher=UrlFetcher(max_bytes=SEARCH_COVER_MAX_BYTES))
    with ThreadPoolExecutor(max_workers=min(max_workers, len(videos))) as pool:
        return list(pool.map(enricher.enrich, videos))


def reserve_search_operation(
    conn: BusinessConnection,
    *,
    user_id: str,
    source_id: str,
    request_fingerprint: str,
) -> str:
    """幂等预留一次搜索计费（1 单位）；失败/取消后的重试才开新轮次.

    同一 ``source_id``（= 幂等键派生）在 PENDING/SUCCEEDED 状态下复用原单，
    不重复扣费；FAILED/CANCELLED 表示本次搜索未成功交付，重试重新预留。
    """
    latest = conn.execute(
        "SELECT billing_round, state FROM billing_operations WHERE user_id=%s AND service=%s "
        "AND source_id=%s ORDER BY billing_round DESC LIMIT 1",
        (user_id, SEARCH_SERVICE, source_id),
    ).fetchone()
    billing_round = 1
    if latest is not None:
        billing_round = int(latest["billing_round"])
        if str(latest["state"]) in {"FAILED", "CANCELLED"}:
            billing_round += 1
    return accept_operation(
        conn,
        user_id=user_id,
        service=SEARCH_SERVICE,
        source_id=source_id,
        units=1,
        billing_round=billing_round,
        request_fingerprint=request_fingerprint,
    )


def persist_viral_search(
    conn: BusinessConnection,
    *,
    user_id: str,
    source_id: str,
    keyword: str,
    platform: str,
    videos: list[ViralVideo],
    search_date: str,
    searched_at: str,
) -> None:
    """搜索结果落库（内容池 + 封面 key + 发现记录 + 计费结算），不提交事务.

    调用方必须已在 ``db.write()`` 事务内、且已通过 SESSION_REPLACED 校验。
    0 结果也算成功交付（供应商成本已发生，客户消耗一次搜索）。
    """
    upsert_viral_videos(conn, videos, commit=False)
    for video in videos:
        if video.cover_key:
            update_viral_cover(
                conn,
                platform=video.platform,
                video_id=video.video_id,
                cover_key=video.cover_key,
                commit=False,
            )
    mark_viral_discoveries(
        conn,
        user_id=user_id,
        keyword=keyword,
        platform=platform,
        video_ids=[video.video_id for video in videos],
        search_date=search_date,
        searched_at=searched_at,
    )
    finish_source(conn, source_id, units=1, succeeded=True, service=SEARCH_SERVICE)
