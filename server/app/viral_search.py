"""搜索驱动的爆款业务（P1）：外呼 → 封面归档 → 计费 → 落库.

搜索成为唯一内容入口后，采集调度退场（design §5.5）。本模块只做四件事：

1. 按平台执行一次搜索外呼（一次外呼 = 一页；两平台都按游标翻页）；
2. 把命中视频的封面归档到自有存储（源站签名链接会过期）；
3. 幂等地预留/推进客户侧搜索计费（每翻页一次 = 一次计量）；
4. 把结果 upsert 进内容池、回写封面 key、记录客户发现、结算计费单。

供应商成本由 ``billing_meter.meter_call`` 在 ``viral_tikhub._request`` 内记账
（``billing_context(source_id)`` 存在时自动落客户单的 source attempt）。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from app.admin_dates import SHANGHAI
from app.db_portable import BusinessConnection
from app.usage_billing import accept_operation, finish_source
from app.viral_media import CoverEnricher, UrlFetcher, ViralStorage
from app.viral_store import mark_viral_discoveries, update_viral_cover, upsert_viral_videos
from app.viral_tikhub import (
    DOUYIN_PUBLISH_TIME,
    PLATFORM_DOUYIN,
    PLATFORM_WECHAT,
    SEARCH_TIME_RANGES,
    WECHAT_PUBLISH_TIME,
    ViralSourceClient,
    ViralVideo,
)

SEARCH_SERVICE = "viral_search"
SEARCH_COVER_MAX_BYTES = 10 * 1024 * 1024
SEARCH_COVER_WORKERS = 8
# 一次搜索（外呼 + 封面归档）的服务端总时限。必须显著小于客户端 240s 与
# nginx proxy_read_timeout 300s：urllib 的 socket 超时管不到 DNS 解析
# （getaddrinfo 是无超时的阻塞调用），服务器出网/DNS 异常时外呼会无限
# 挂起，路由必须在有界时间内给出带 CORS 头的 HTTP 响应，否则客户端只能
# 等到自己的超时并把请求报成 "Failed to fetch"。
SEARCH_DEADLINE_SECONDS = 150.0


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
    time_range: str = "week",
) -> ViralSearchPage:
    """执行一次搜索外呼：一次外呼 = 一页，两个平台都按游标翻页.

    抖音把上游的 offset/search_id/backtrace 打包成不透明游标下发（见
    ``viral_tikhub.encode_douyin_search_cursor``），视频号透传上游 cursor；
    翻一页就是一次新外呼、一次新计量。
    """
    if time_range not in SEARCH_TIME_RANGES:
        raise ValueError(f"unsupported search time range: {time_range}")
    if platform == PLATFORM_WECHAT:
        page = client.wechat_search_page(
            keyword=keyword, cursor=cursor, publish_time=WECHAT_PUBLISH_TIME[time_range]
        )
        return ViralSearchPage(items=list(page.videos), cursor=page.cursor, has_more=page.has_more)
    if platform != PLATFORM_DOUYIN:
        raise ValueError(f"unsupported search platform: {platform}")
    douyin_page = client.douyin_search_page(
        keyword=keyword, publish_time=DOUYIN_PUBLISH_TIME[time_range], cursor=cursor
    )
    return ViralSearchPage(
        items=list(douyin_page.videos), cursor=douyin_page.cursor, has_more=douyin_page.has_more
    )


def run_viral_search_bounded(
    client: ViralSourceClient,
    *,
    keyword: str,
    platform: str,
    cursor: str | None = None,
    time_range: str = "week",
    deadline_seconds: float = SEARCH_DEADLINE_SECONDS,
    billing_batch_id: str | None = None,
) -> ViralSearchPage:
    """把一次搜索外呼关进带总时限的工作线程（DNS 挂起也能按时返回）.

    超时抛 ``TimeoutError``，由路由转成 503 并释放预留。底层线程无法强杀，
    超时后仍在后台跑完（结果弃用），因此 ``shutdown(wait=False)`` 立即返回，
    绝不能让线程池的隐式 join 把调用方重新拖回无界等待。
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viral-search-bounded")
    try:

        def search() -> ViralSearchPage:
            from contextlib import nullcontext

            from app.billing_meter import collection_billing_context

            with (
                collection_billing_context(billing_batch_id) if billing_batch_id else nullcontext()
            ):
                return run_viral_search(
                    client, keyword=keyword, platform=platform, cursor=cursor, time_range=time_range
                )

        future = pool.submit(search)
        return future.result(timeout=deadline_seconds)
    finally:
        pool.shutdown(wait=False)


def archive_search_covers_bounded(
    storage: ViralStorage,
    videos: list[ViralVideo],
    *,
    max_workers: int = SEARCH_COVER_WORKERS,
    deadline_seconds: float = SEARCH_DEADLINE_SECONDS,
    metered: bool = False,
) -> list[ViralVideo]:
    """封面归档的限时版本；超时同样抛 ``TimeoutError``，已完成的封面保留."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viral-cover-bounded")
    try:
        future = pool.submit(
            archive_search_covers, storage, videos, max_workers=max_workers, metered=metered
        )
        return future.result(timeout=deadline_seconds)
    finally:
        pool.shutdown(wait=False)


def archive_search_covers(
    storage: ViralStorage,
    videos: list[ViralVideo],
    *,
    max_workers: int = SEARCH_COVER_WORKERS,
    metered: bool = False,
) -> list[ViralVideo]:
    """并发把搜索结果封面归档到自有存储；单条失败保留源站链接兜底."""
    if not videos:
        return []
    enricher = CoverEnricher(
        storage=storage, fetcher=UrlFetcher(max_bytes=SEARCH_COVER_MAX_BYTES), metered=metered
    )
    with ThreadPoolExecutor(max_workers=min(max_workers, len(videos))) as pool:
        return list(pool.map(enricher.enrich, videos))


def reserve_search_operation(
    conn: BusinessConnection,
    *,
    user_id: str,
    source_id: str,
    request_fingerprint: str,
) -> str:
    """幂等预留一次搜索计费（1 单位）；每次真实外呼都必须对应一次扣费.

    轮次语义（防“使用未扣费”）：
    - **PENDING**（同一次尝试的在途重试/并发重放）复用同一轮次，不重复扣费，
      指纹或用量不一致会被 ``accept_operation`` 判 409；
    - **FAILED/CANCELLED**（本次搜索未成功交付）重试开新一轮重新预留；
    - **SUCCEEDED** 同样开新一轮——旧行为复用轮次会让同一幂等键免费重刷
      上游（同关键词每次结果都会更新），出现“调用了但没扣费”。
    """
    latest = conn.execute(
        "SELECT billing_round, state FROM billing_operations WHERE user_id=%s AND service=%s "
        "AND source_id=%s ORDER BY billing_round DESC LIMIT 1",
        (user_id, SEARCH_SERVICE, source_id),
    ).fetchone()
    billing_round = 1
    if latest is not None:
        billing_round = int(latest["billing_round"])
        if str(latest["state"]) != "PENDING":
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
    # 计费轮次是一次真实交付的身份：重放不重复计数，重复搜索的新轮次单独计数。
    operation = conn.execute(
        "SELECT id FROM billing_operations WHERE user_id=%s AND service=%s "
        "AND source_id=%s ORDER BY billing_round DESC LIMIT 1",
        (user_id, SEARCH_SERVICE, source_id),
    ).fetchone()
    if operation is None:
        raise RuntimeError("搜索交付缺少预留计费轮次")
    conn.execute(
        "INSERT INTO viral_search_events "
        "(id,user_id,keyword,platform,search_date,searched_at,video_ids_json) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
        (
            operation["id"],
            user_id,
            keyword,
            platform,
            search_date,
            searched_at,
            json.dumps(list(dict.fromkeys(video.video_id for video in videos))),
        ),
    )
    finish_source(conn, source_id, units=1, succeeded=True, service=SEARCH_SERVICE)
