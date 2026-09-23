"""视频号列表互动统计的按需补采服务."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from app.db_portable import BusinessConnection
from app.viral_store import (
    STATISTICS_CHECKED_AT_KEY,
    STATISTICS_OBJECT_ID_KEY,
    STATISTICS_RETRY_AT_KEY,
    get_viral_video,
    mark_viral_statistics_failure,
    update_viral_statistics,
)
from app.viral_tikhub import PLATFORM_WECHAT, ViralSourceClient, ViralVideo, WechatVideoDetail

STATISTICS_SUCCESS_TTL = timedelta(hours=24)
STATISTICS_FAILURE_COOLDOWN = timedelta(minutes=10)
STATISTICS_MAX_WORKERS = 3

logger = logging.getLogger(__name__)
_refresh_lock = threading.Lock()


def _fresh(timestamp: object, max_age: timedelta) -> bool:
    if not isinstance(timestamp, str):
        return False
    try:
        checked_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - checked_at <= max_age


def _already_complete(video: ViralVideo) -> bool:
    return all(value is not None for value in (video.comments, video.shares, video.collects))


def _load_wechat_videos(conn: BusinessConnection, video_ids: list[str]) -> list[ViralVideo]:
    videos: list[ViralVideo] = []
    seen: set[str] = set()
    for video_id in video_ids:
        if video_id in seen:
            continue
        seen.add(video_id)
        video = get_viral_video(conn, platform=PLATFORM_WECHAT, video_id=video_id)
        if video is not None:
            videos.append(video)
    return videos


def _needs_refresh(video: ViralVideo) -> bool:
    native = video.native
    if _fresh(native.get(STATISTICS_RETRY_AT_KEY), STATISTICS_FAILURE_COOLDOWN):
        return False
    checked_at = native.get(STATISTICS_CHECKED_AT_KEY)
    if _fresh(checked_at, STATISTICS_SUCCESS_TTL):
        return False
    return checked_at is not None or not _already_complete(video)


def _fetch_detail(client: ViralSourceClient, video: ViralVideo) -> WechatVideoDetail:
    object_id = video.native.get(STATISTICS_OBJECT_ID_KEY)
    if isinstance(object_id, str) and object_id:
        return client.wechat_video_detail(object_id=object_id)
    return client.wechat_video_detail(
        export_id=str(video.native["export_id"]),
        object_nonce_id=str(video.native.get("object_nonce_id") or "") or None,
    )


def _has_statistics(detail: WechatVideoDetail) -> bool:
    return any(
        value is not None
        for value in (
            detail.like_count,
            detail.comment_count,
            detail.forward_count,
            detail.fav_count,
        )
    )


def refresh_viral_statistics(
    conn: BusinessConnection,
    client: ViralSourceClient | None,
    video_ids: list[str],
) -> list[ViralVideo]:
    """补采已有视频号条目的互动数，并返回数据库中的最新条目."""
    with _refresh_lock:
        videos = _load_wechat_videos(conn, video_ids)
        if client is None:
            return videos

        pending: list[ViralVideo] = []
        for video in videos:
            if not _needs_refresh(video):
                continue
            export_id = video.native.get("export_id")
            object_id = video.native.get(STATISTICS_OBJECT_ID_KEY)
            if not (isinstance(export_id, str) and export_id) and not (
                isinstance(object_id, str) and object_id
            ):
                mark_viral_statistics_failure(
                    conn, platform=video.platform, video_id=video.video_id
                )
                continue
            pending.append(video)

        futures: dict[Future[WechatVideoDetail], ViralVideo] = {}
        with ThreadPoolExecutor(max_workers=STATISTICS_MAX_WORKERS) as pool:
            for video in pending:
                futures[pool.submit(_fetch_detail, client, video)] = video
            for future, video in futures.items():
                try:
                    detail = future.result()
                except Exception as exc:  # noqa: BLE001 - 失败须降级为已有库值
                    logger.warning(
                        "Viral statistics refresh failed for %s (%s)",
                        video.video_id,
                        type(exc).__name__,
                    )
                    mark_viral_statistics_failure(
                        conn, platform=video.platform, video_id=video.video_id
                    )
                    continue
                if not _has_statistics(detail):
                    mark_viral_statistics_failure(
                        conn, platform=video.platform, video_id=video.video_id
                    )
                    continue
                update_viral_statistics(
                    conn,
                    platform=video.platform,
                    video_id=video.video_id,
                    detail=detail,
                )

        return _load_wechat_videos(conn, video_ids)
