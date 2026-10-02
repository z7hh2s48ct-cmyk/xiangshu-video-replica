"""Weekly keyword collection and background media archiving; customer reads never enqueue."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.billing_meter import collection_billing_context, video_billing_context
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.storage import StorageAdapter
from app.viral_collection_billing import (
    FrozenCollectionBilling,
    create_collection_batch,
    eligible_collection_users,
    freeze_collection_billing,
)
from app.viral_collection_budget import collection_budget
from app.viral_collection_failures import classify_collection_failure
from app.viral_collection_schedule import advance_collection_time
from app.viral_content_observations import record_content_source, record_content_stage
from app.viral_keywords import ViralKeywordConfig
from app.viral_media import CoverEnricher, UrlFetcher, ViralMediaPipeline
from app.viral_refresh import ViralRefreshLease, _require_lease
from app.viral_store import (
    get_viral_video,
    update_viral_cover,
    update_viral_statistics,
    upsert_viral_videos,
    viral_runtime_controls,
    viral_video_availability,
)
from app.viral_tikhub import ViralSourceError, ViralVideo, viral_source_client_from_settings


@dataclass(frozen=True)
class ViralQualityRules:
    """采集质量门槛（方案 P1 采集设置）：入池前过滤低质内容。

    四项都是「不设即不限」。这些值由管理端写入 ``viral_runtime_controls``；此前只存不用，
    运营配了「点赞不低于 5000」，低于它的视频照样被采集、下载、占存储和成本。
    """

    min_likes: int | None = None
    duration_min_ms: int | None = None
    duration_max_ms: int | None = None
    exclude_words: tuple[str, ...] = ()

    def rejects(self, video: ViralVideo) -> bool:
        if self.min_likes is not None and video.likes < self.min_likes:
            return True
        # 时长为 0 表示上游没给（并非真的 0 毫秒）：无法判断就不因时长规则误杀，
        # 否则一个不返回时长的平台会被「最短 15 秒」整批拒光。
        if video.duration_ms > 0:
            if self.duration_min_ms is not None and video.duration_ms < self.duration_min_ms:
                return True
            if self.duration_max_ms is not None and video.duration_ms > self.duration_max_ms:
                return True
        if self.exclude_words:
            text = " ".join(
                [
                    video.title,
                    *video.tags,
                    str(video.native.get("source_description") or ""),
                ]
            ).lower()
            if any(word.lower() in text for word in self.exclude_words):
                return True
        return False


def load_quality_rules(conn: BusinessConnection) -> ViralQualityRules:
    row = conn.execute(
        "SELECT quality_min_likes, quality_duration_min_ms, quality_duration_max_ms, "
        "quality_exclude_words_json FROM viral_runtime_controls WHERE id=1"
    ).fetchone()
    if row is None:
        return ViralQualityRules()
    try:
        words = json.loads(row["quality_exclude_words_json"] or "[]")
    except json.JSONDecodeError:
        words = []
    return ViralQualityRules(
        min_likes=row["quality_min_likes"],
        duration_min_ms=row["quality_duration_min_ms"],
        duration_max_ms=row["quality_duration_max_ms"],
        exclude_words=tuple(str(word) for word in words if str(word).strip()),
    )


def enqueue_due_viral_collections(
    conn: BusinessConnection,
    *,
    manual: bool = False,
    frozen_billing: FrozenCollectionBilling | None = None,
) -> bool:
    """Singleton row lock makes each daily/weekly schedule unique across workers.

    返回本次调用是否真的入队了一批采集任务（False：未启用 / 无关键词 / 预算已满 /
    未到期 / 已有任务在队）。手动「立即采集」据此如实回答运营，而不是不管有没有入队
    都说「已入队」。``manual=True`` 表示运营知情下的手动触发，不受月度预算门槛拦截。
    """
    row = conn.execute(
        "SELECT * FROM viral_runtime_controls WHERE id=1 AND collection_enabled=1 "
        "FOR UPDATE SKIP LOCKED"
    ).fetchone()
    if row is None:
        return False
    keywords = [
        ViralKeywordConfig.model_validate(item) for item in json.loads(row["keywords_json"])
    ]
    keywords = [item for item in keywords if item.enabled]
    if not keywords:
        return False
    # 月度预算门槛（方案 P1 采集设置）：本月平台侧采集成本已达预算时跳过
    # 定时入队；手动「立即采集」不拦——运营知情下的手动动作仍可用。
    if not manual and collection_budget(conn)["budget_status"] == "exhausted":
        return False
    due = conn.execute(
        "SELECT 1 FROM viral_runtime_controls WHERE id=1 "
        "AND (next_collection_at IS NULL OR next_collection_at <= CURRENT_TIMESTAMP)"
    ).fetchone()
    if due or manual:
        busy = conn.execute(
            "SELECT 1 FROM viral_refresh_tasks WHERE status IN ('PENDING','RUNNING') LIMIT 1"
        ).fetchone()
        if busy is not None:
            return False
        window_end = conn.execute(
            "SELECT extract(epoch FROM CURRENT_TIMESTAMP)::bigint"
        ).fetchone()[0]
        frozen = frozen_billing if frozen_billing is not None else freeze_collection_billing(conn)
        for platform in dict.fromkeys(item.platform for item in keywords):
            batch_config = {
                "keywords": [item.model_dump() for item in keywords if item.platform == platform],
                "limit": int(row["per_keyword_limit"]),
                "window_end": int(window_end),
                "trigger_kind": "manual" if manual else "scheduled",
            }
            batch_id = create_collection_batch(
                conn,
                platform=platform,
                config=batch_config,
                user_ids=frozen.user_ids,
                pricing_json=frozen.pricing_json,
            )
            config = json.dumps({**batch_config, "billing_batch_id": batch_id})
            task = conn.execute(
                """INSERT INTO viral_refresh_tasks(id,platform,sort,collection_config_json)
                VALUES(%s,%s,'hot',%s) ON CONFLICT(platform,sort) DO UPDATE SET status='PENDING',
                    collection_config_json=excluded.collection_config_json, checkpoint_json='{}',
                    retry_count=0, locked_by=NULL, locked_until=NULL, error_code=NULL,
                    error_message_redacted=NULL, completed_at=NULL, updated_at=CURRENT_TIMESTAMP
                    RETURNING id""",
                (str(uuid4()), platform, config),
            ).fetchone()
            assert task is not None
            conn.execute(
                "UPDATE viral_collection_batches SET related_task_id=%s WHERE id=%s",
                (str(task[0]), batch_id),
            )
        if not manual:
            if row["collection_time"] is not None:
                now = conn.execute("SELECT now()").fetchone()[0]
                conn.execute(
                    "UPDATE viral_runtime_controls SET next_collection_at=%s WHERE id=1",
                    (
                        advance_collection_time(
                            now,
                            int(row["collection_interval_days"]),
                            row["collection_time"],
                            datetime.fromisoformat(str(row["next_collection_at"]))
                            if row["next_collection_at"] is not None
                            else None,
                        ),
                    ),
                )
            else:
                conn.execute(
                    "UPDATE viral_runtime_controls SET next_collection_at="
                    "CURRENT_TIMESTAMP + (%s * interval '1 day') WHERE id=1",
                    (int(row["collection_interval_days"]),),
                )
        return True
    else:
        # Two retries per scheduled batch. Completed keywords and media remain
        # checkpointed, so retries only resume missing work, never restart a batch.
        conn.execute(
            """UPDATE viral_refresh_tasks SET status='PENDING', retry_count=retry_count+1,
                locked_by=NULL, locked_until=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE status='FAILED' AND retryable=1 AND retry_count < 2
                AND collection_config_json != '{}'
                AND COALESCE(collection_config_json::jsonb->>'kind','') != 'single_archive'
                AND updated_at::timestamptz <=
                    CURRENT_TIMESTAMP - interval '15 minutes'"""
        )
        return False


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
    conn.execute(
        "UPDATE viral_collection_batches SET failed_video_count=%s,video_outcomes_json=%s WHERE id="
        "(SELECT collection_config_json::jsonb->>'billing_batch_id' "
        "FROM viral_refresh_tasks WHERE id=%s)",
        (
            len(progress.get("failed_video_ids", [])),
            json.dumps(progress.get("video_outcomes", {}), ensure_ascii=False),
            lease.id,
        ),
    )


def _run_single_archive(lease: ViralRefreshLease, storage: StorageAdapter, video_id: str) -> None:
    """Prepare an existing item only; never search, bill customers or replace a list."""
    with _keep_lease(lease) as check, video_billing_context(lease.platform, video_id):
        check()
        with _connection() as conn:
            video = get_viral_video(conn, platform=lease.platform, video_id=video_id)
            if (
                video is None
                or viral_video_availability(conn, platform=lease.platform, video_id=video_id)
                != "available"
            ):
                raise ViralSourceError("视频已删除、下架或屏蔽，停止准备。")
            client = viral_source_client_from_settings(conn)
        if video is None:
            raise ViralSourceError("视频已删除，停止归档。")
        pipeline = ViralMediaPipeline(
            client=client, storage=storage, shared=True, cancellation_check=check
        )
        pipeline.fetch(video, prefer="video")
        check()
        cover = CoverEnricher(
            storage=storage, fetcher=UrlFetcher(max_bytes=10 * 1024 * 1024), metered=True
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
    if config.get("kind") == "archive_batch":
        # 批量素材准备（方案 P1 内容模块 C-2）：逐条复用单条转存管线；
        # 带 feature_after 意图时，每条就绪后当场上首页并按原始操作人写审计
        # ——操作发生在后台，但责任落在发起「上首页」的那个人身上。
        feature_after = config.get("feature_after") or None
        failed = []
        for video_id in config.get("video_ids", []):
            try:
                _run_single_archive(lease, storage, str(video_id))
            except Exception:
                # 逐条保留结果；租约丢失必须中止，普通素材失败不阻塞后面的条目。
                with _connection() as conn:
                    _require_lease(conn, lease)
                    if not viral_runtime_controls(conn)[0]:
                        raise
                    failed.append(str(video_id))
                    progress["failed_video_ids"] = failed
                    _checkpoint(conn, lease, progress)
                continue
            if not feature_after:
                with _connection() as conn:
                    record_content_stage(
                        conn, platform=lease.platform, video_id=str(video_id), stage="prepared"
                    )
                continue
            with _connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
                _require_lease(conn, lease)
                applied = conn.execute(
                    "UPDATE viral_videos v SET homepage_featured=1, collection_published=1, "
                    "homepage_featured_at=COALESCE(homepage_featured_at,now()) "
                    "WHERE platform=%s AND video_id=%s AND deleted_at IS NULL "
                    "AND homepage_intent_version=%s AND homepage_featured=0 "
                    "AND (homepage_ends_at IS NULL OR homepage_ends_at>now()) "
                    "AND NOT EXISTS (SELECT 1 FROM viral_video_visibility vis WHERE "
                    "vis.platform=v.platform AND vis.video_id=v.video_id "
                    "AND vis.status!='AVAILABLE') "
                    "AND EXISTS (SELECT 1 FROM viral_media_preparations m "
                    "WHERE m.platform=v.platform "
                    "AND m.video_id=v.video_id AND m.media_kind='video' AND m.status='SUCCEEDED' "
                    "AND m.storage_uri IS NOT NULL) "
                    "AND (COALESCE(v.cover_url,'')='' OR v.cover_key IS NOT NULL) "
                    "RETURNING platform, video_id",
                    (
                        lease.platform,
                        str(video_id),
                        int(feature_after.get("versions", {}).get(str(video_id), 0)),
                    ),
                ).fetchone()
                if applied is None:
                    continue
                record_content_stage(
                    conn, platform=lease.platform, video_id=str(video_id), stage="prepared"
                )
                record_content_stage(
                    conn, platform=lease.platform, video_id=str(video_id), stage="homepage"
                )
                conn.execute(
                    """INSERT INTO audit_logs(
                        id,actor_user_id,action,entity_type,entity_id,metadata_json)
                    VALUES(%s,%s,'viral_video.curation','viral_video',%s,%s)""",
                    (
                        str(uuid4()),
                        str(feature_after.get("actor_user_id") or ""),
                        f"{lease.platform}:{video_id}",
                        json.dumps(
                            {
                                "action": "feature",
                                "reason": str(feature_after.get("reason") or ""),
                                "request_id": str(feature_after.get("request_id") or ""),
                                "prepared_then_featured": True,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
        if failed:
            raise ViralSourceError(f"有 {len(failed)} 条素材准备失败，其他就绪视频已处理；可重试。")
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
                # 旧检查点可能已跳过来源写入，不能因为本次新建账单就宣称统计完整。
                stats_complete=not bool(progress.get("keywords") or progress.get("prepared")),
            )
            conn.execute(
                "UPDATE viral_refresh_tasks SET collection_config_json=%s WHERE id=%s",
                (json.dumps(config), lease.id),
            )
        client = viral_source_client_from_settings(conn)
        # 质量门槛按本轮开始时的设置执行；跑到一半改设置不影响这一轮的一致性。
        quality_rules = load_quality_rules(conn)
    entries = [ViralKeywordConfig.model_validate(item) for item in config.get("keywords", [])]
    if not entries:
        raise ViralSourceError("请先在管理后台配置采集关键词。")
    keywords_done = progress.setdefault("keywords", {})
    prepared = set(progress.setdefault("prepared", []))
    detailed = set(progress.setdefault("detailed", []))
    failed_video_ids = set(progress.setdefault("failed_video_ids", []))
    failures = 0
    with _keep_lease(lease) as check, collection_billing_context(config["billing_batch_id"]):
        for index, entry in enumerate(entries):
            check()
            step = str(index)
            if step not in keywords_done:
                with _connection() as conn:
                    _require_lease(conn, lease)
                    conn.execute(
                        "INSERT INTO viral_keyword_runs(batch_id,platform,keyword,status) "
                        "VALUES(%s,%s,%s,'RUNNING') ON CONFLICT(batch_id,platform,keyword) "
                        "DO UPDATE SET status='RUNNING',started_at=clock_timestamp(),"
                        "finished_at=NULL,failure_code=NULL,"
                        "attempt_count=viral_keyword_runs.attempt_count+1,video_count=NULL",
                        (config["billing_batch_id"], entry.platform, entry.keyword),
                    )
                try:
                    videos = (
                        client.douyin_search(keyword=entry.keyword, category=entry.category)
                        if entry.platform == "douyin"
                        else client.wechat_search(keyword=entry.keyword, category=entry.category)
                    )
                except Exception as cause:
                    with _connection() as conn:
                        _require_lease(conn, lease)
                        conn.execute(
                            "UPDATE viral_keyword_runs SET status='FAILED',"
                            "finished_at=clock_timestamp(),failure_code=%s "
                            "WHERE batch_id=%s AND platform=%s AND keyword=%s",
                            (
                                classify_collection_failure(cause),
                                config["billing_batch_id"],
                                entry.platform,
                                entry.keyword,
                            ),
                        )
                    failures += 1
                    continue
                # 先过质量门槛再取每词上限：上限应当数「合格的」，而不是让被拒的
                # 视频占掉名额、合格的反而被截掉。被拒的既不入池也不下载。
                videos = [video for video in videos if not quality_rules.rejects(video)][
                    : entry.limit if entry.limit is not None else int(config["limit"])
                ]
                keywords_done[step] = list(dict.fromkeys(video.video_id for video in videos))
                with _connection() as conn:
                    _require_lease(conn, lease)
                    conn.execute(
                        "UPDATE viral_keyword_runs SET status='SUCCEEDED',"
                        "finished_at=clock_timestamp(),failure_code=NULL,"
                        "video_count=%s WHERE batch_id=%s AND platform=%s AND keyword=%s",
                        (len(videos), config["billing_batch_id"], entry.platform, entry.keyword),
                    )
                    inserted = upsert_viral_videos(conn, videos, commit=False)
                    for collected in videos:
                        record_content_source(
                            conn,
                            batch_id=str(config["billing_batch_id"]),
                            source_kind="collection",
                            platform=entry.platform,
                            video_id=collected.video_id,
                            keyword=entry.keyword,
                            is_new=(entry.platform, collected.video_id) in inserted,
                        )
                    _checkpoint(conn, lease, progress)
            for video_id in keywords_done[step]:
                if video_id in prepared:
                    continue
                check()
                with _connection() as conn:
                    video = get_viral_video(conn, platform=entry.platform, video_id=video_id)
                    available = viral_video_availability(
                        conn, platform=entry.platform, video_id=video_id
                    )
                if video is None or available != "available":
                    # An administrator can remove a staged item while collecting.
                    # Its tombstone prevents both re-publication and re-download.
                    continue
                pipeline = ViralMediaPipeline(
                    client=client, storage=storage, shared=True, cancellation_check=check
                )
                try:
                    with video_billing_context(entry.platform, video_id):
                        cover = CoverEnricher(
                            storage=storage,
                            fetcher=UrlFetcher(max_bytes=10 * 1024 * 1024),
                            metered=True,
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
                            failed_video_ids.discard(video_id)
                            record_content_stage(
                                conn, platform=entry.platform, video_id=video_id, stage="prepared"
                            )
                            progress.setdefault("video_outcomes", {})[video_id] = {
                                "status": "SUCCEEDED",
                                "failure_code": None,
                                "title": video.title,
                            }
                            next_progress = {
                                **progress,
                                "prepared": sorted(next_prepared),
                                "failed_video_ids": sorted(failed_video_ids),
                            }
                            _checkpoint(conn, lease, next_progress)
                        prepared, progress = next_prepared, next_progress
                except Exception as cause:
                    failures += 1
                    progress.setdefault("video_outcomes", {})[video_id] = {
                        "status": "FAILED",
                        "failure_code": classify_collection_failure(cause),
                        "title": video.title,
                    }
                    failed_video_ids.add(video_id)
                    with _connection() as conn:
                        _require_lease(conn, lease)
                        progress = {**progress, "failed_video_ids": sorted(failed_video_ids)}
                        _checkpoint(conn, lease, progress)
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
