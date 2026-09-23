"""Legacy viral collection reader and executor.

Scheduled collection was retired when customer keyword search became the sole
content-pool entry. Existing task rows remain readable and executable for
recovery, but this module never creates a new scheduled task.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from app.billing_meter import collection_billing_context
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.storage import StorageAdapter
from app.viral_collection_billing import create_collection_batch, eligible_collection_users
from app.viral_keywords import ViralKeywordConfig
from app.viral_media import CoverEnricher, UrlFetcher, ViralMediaPipeline
from app.viral_refresh import ViralRefreshLease, _require_lease
from app.viral_store import (
    get_viral_video,
    update_viral_cover,
    update_viral_statistics,
    upsert_viral_videos,
    viral_runtime_controls,
)
from app.viral_tikhub import ViralSourceError, viral_source_client_from_settings


def enqueue_due_viral_collections(conn: BusinessConnection) -> None:
    """Compatibility no-op: scheduled collection must never enqueue again."""
    return


@contextmanager
def _connection() -> Iterator[BusinessConnection]:
    with pg_transaction() as raw:
        yield BusinessConnection.postgres(raw)


@contextmanager
def _keep_lease(lease: ViralRefreshLease) -> Iterator[Callable[[], None]]:
    stop, lost = threading.Event(), threading.Event()

    def heartbeat() -> None:
        while not stop.wait(20):
            try:
                with _connection() as conn:
                    _require_lease(conn, lease)
                    if not viral_runtime_controls(conn)[0]:
                        lost.set()
                        return
                    conn.execute(
                        "UPDATE viral_refresh_tasks SET locked_until=%s WHERE id=%s",
                        (
                            (datetime.now(UTC) + timedelta(minutes=10)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            lease.id,
                        ),
                    )
            except Exception:
                lost.set()
                return

    def check() -> None:
        if lost.is_set():
            raise ViralSourceError("后台采集已暂停或任务租约已失效。")
        with _connection() as conn:
            _require_lease(conn, lease)
            if not viral_runtime_controls(conn)[0]:
                raise ViralSourceError("后台采集已暂停。")

    thread = threading.Thread(target=heartbeat, daemon=True, name="viral-collection-lease")
    thread.start()
    try:
        yield check
    finally:
        stop.set()
        thread.join()


def _checkpoint(
    conn: BusinessConnection, lease: ViralRefreshLease, progress: dict[str, Any]
) -> None:
    _require_lease(conn, lease)
    conn.execute(
        "UPDATE viral_refresh_tasks SET checkpoint_json=%s WHERE id=%s",
        (json.dumps(progress, ensure_ascii=False), lease.id),
    )


def _run_single_archive(lease: ViralRefreshLease, storage: StorageAdapter, video_id: str) -> None:
    """Prepare an existing item only; never search, bill customers or replace a list."""
    with _keep_lease(lease) as check:
        check()
        with _connection() as conn:
            video = get_viral_video(conn, platform=lease.platform, video_id=video_id)
            client = viral_source_client_from_settings(conn)
        if video is None:
            raise ViralSourceError("视频已删除，停止归档。")
        pipeline = ViralMediaPipeline(
            client=client, storage=storage, shared=True, cancellation_check=check
        )
        pipeline.fetch(video, prefer="video")
        check()
        cover = CoverEnricher(
            storage=storage, fetcher=UrlFetcher(max_bytes=10 * 1024 * 1024)
        ).enrich(video)
        check()
        with _connection() as conn:
            _require_lease(conn, lease)
            if get_viral_video(conn, platform=lease.platform, video_id=video_id) is None:
                raise ViralSourceError("视频已删除，停止归档。")
            if pipeline.detail is not None:
                update_viral_statistics(
                    conn, platform=lease.platform, video_id=video_id, detail=pipeline.detail
                )
            if cover.cover_key:
                update_viral_cover(
                    conn, platform=lease.platform, video_id=video_id, cover_key=cover.cover_key
                )
        if video.cover_url and not cover.cover_key:
            raise ViralSourceError("视频已转存，封面尚未完成，请重试。")


def run_viral_collection(lease: ViralRefreshLease, storage: StorageAdapter) -> None:
    with _connection() as conn:
        _require_lease(conn, lease)
        row = conn.execute(
            "SELECT collection_config_json,checkpoint_json FROM viral_refresh_tasks WHERE id=%s",
            (lease.id,),
        ).fetchone()
        config, progress = json.loads(row[0]), json.loads(row[1])
    if config.get("kind") == "single_archive":
        _run_single_archive(lease, storage, str(config["video_id"]))
        return
    with _connection() as conn:
        _require_lease(conn, lease)
        # Compatibility for already-queued development tasks: establish the batch
        # once under the task lease, never use the reused refresh-task ID as a bill.
        if "billing_batch_id" not in config:
            config["billing_batch_id"] = create_collection_batch(
                conn,
                platform=lease.platform,
                config=config,
                user_ids=eligible_collection_users(conn),
            )
            conn.execute(
                "UPDATE viral_refresh_tasks SET collection_config_json=%s WHERE id=%s",
                (json.dumps(config), lease.id),
            )
        client = viral_source_client_from_settings(conn)
    entries = [ViralKeywordConfig.model_validate(item) for item in config.get("keywords", [])]
    if not entries:
        raise ViralSourceError("请先在管理后台配置采集关键词。")
    keywords_done = progress.setdefault("keywords", {})
    prepared = set(progress.setdefault("prepared", []))
    detailed = set(progress.setdefault("detailed", []))
    failures = 0
    with _keep_lease(lease) as check, collection_billing_context(config["billing_batch_id"]):
        for index, entry in enumerate(entries):
            check()
            step = str(index)
            if step not in keywords_done:
                try:
                    videos = (
                        client.douyin_search(keyword=entry.keyword, category=entry.category)
                        if entry.platform == "douyin"
                        else client.wechat_search(keyword=entry.keyword, category=entry.category)
                    )
                except Exception:
                    failures += 1
                    continue
                videos = videos[: int(config["limit"])]
                keywords_done[step] = list(dict.fromkeys(video.video_id for video in videos))
                with _connection() as conn:
                    _require_lease(conn, lease)
                    upsert_viral_videos(conn, videos, commit=False)
                    _checkpoint(conn, lease, progress)
            for video_id in keywords_done[step]:
                if video_id in prepared:
                    continue
                check()
                with _connection() as conn:
                    video = get_viral_video(conn, platform=entry.platform, video_id=video_id)
                if video is None:
                    # An administrator can remove a staged item while collecting.
                    # Its tombstone prevents both re-publication and re-download.
                    continue
                pipeline = ViralMediaPipeline(
                    client=client, storage=storage, shared=True, cancellation_check=check
                )
                try:
                    cover = CoverEnricher(
                        storage=storage, fetcher=UrlFetcher(max_bytes=10 * 1024 * 1024)
                    ).enrich(video)
                    check()
                    if entry.platform == "wechat_channels" and video_id not in detailed:
                        detail = client.wechat_video_detail(
                            export_id=str(video.native.get("export_id") or ""),
                            object_nonce_id=video.native.get("object_nonce_id") or None,
                        )
                        with _connection() as conn:
                            _require_lease(conn, lease)
                            update_viral_statistics(
                                conn, platform=entry.platform, video_id=video_id, detail=detail
                            )
                            next_detailed = detailed | {video_id}
                            next_progress = {**progress, "detailed": sorted(next_detailed)}
                            _checkpoint(conn, lease, next_progress)
                        detailed, progress = next_detailed, next_progress
                    check()
                    pipeline.fetch(video, prefer="video")
                    check()
                    with _connection() as conn:
                        _require_lease(conn, lease)
                        if cover.cover_key:
                            update_viral_cover(
                                conn,
                                platform=entry.platform,
                                video_id=video_id,
                                cover_key=cover.cover_key,
                            )
                        if video.cover_url and not cover.cover_key:
                            raise ViralSourceError("封面转存尚未完成。")
                        next_prepared = prepared | {video_id}
                        next_progress = {**progress, "prepared": sorted(next_prepared)}
                        _checkpoint(conn, lease, next_progress)
                    prepared, progress = next_prepared, next_progress
                except Exception:
                    failures += 1
                finally:
                    if pipeline.detail is not None:
                        with _connection() as conn:
                            _require_lease(conn, lease)
                            update_viral_statistics(
                                conn,
                                platform=entry.platform,
                                video_id=video_id,
                                detail=pipeline.detail,
                            )
        check()
        if failures:
            raise ViralSourceError(
                f"有 {failures} 项采集或转存尚未完成，后台将重试；保留上轮列表。"
            )
        with _connection() as conn:
            _require_lease(conn, lease)
            conn.execute(
                "UPDATE viral_videos SET collection_published=0 WHERE platform=%s",
                (lease.platform,),
            )
            for video_id in prepared:
                conn.execute(
                    "UPDATE viral_videos SET collection_published=1 "
                    "WHERE platform=%s AND video_id=%s",
                    (lease.platform, video_id),
                )
            stamp = datetime.now(UTC).isoformat()
            window = datetime.fromtimestamp(int(config["window_end"]), UTC).isoformat()
            for sort, value in (("hot", stamp), ("latest", stamp), ("weekly_window", window)):
                conn.execute(
                    """INSERT INTO viral_fetch_state(platform,sort,fetched_at) VALUES(%s,%s,%s)
                    ON CONFLICT(platform,sort) DO UPDATE SET fetched_at=excluded.fetched_at""",
                    (lease.platform, sort, value),
                )
