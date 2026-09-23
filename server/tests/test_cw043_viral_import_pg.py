"""CW-043 Segment 4+ — 爆款导入 durable-queue 租约机制 TEST-PG 矩阵.

CW-043 审计实施：``CW043-PG-COVERAGE-MATRIX`` §5.1.1 #20 把旧
``test_viral_import.py`` 的 import 全矩阵标为 ⚠️部分（旧文件仅 SQLite，且多为
经 ``X-Dev-User-Id`` 的 TestClient 路由级重型集成）。CW-026 收敛后 PG lane 只认
客户会话 Bearer，路由级 dev-header 用例在 PG 上不可达；本模块改在真实 PG 上直接
验证 ``run_pg_worker_once`` 消费爆款导入队列所用的**持久队列租约机制**——这是
``test_cw030_worker_pg_matrix.py`` 唯一未覆盖的任务类（cw030 grep viral_import = 0）：

- 独占认领（``FOR UPDATE SKIP LOCKED`` + status 状态机：一个 PENDING 任务不会被
  两个 worker 重复认领）
- 过期租约回收 + attempt 递增（RUNNING 且 locked_until 过期 → 复位 PENDING 后被
  新 worker 重新认领，attempt 1→2，started_at 保留）
- 失败释放锁 + retryable 分级（普通异常默认可重试；终态 ViralImportError
  retryable=False 落 retryable=0）
- 陈旧租约失败是 no-op（attempt + locked_by 栅栏令牌：被回收后旧 worker 的迟到
  失败不得覆盖已恢复状态）
- 幂等键按属主唯一（``uq_viral_import_tasks_owner_idempotency`` —— enqueue 的
  ``ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING`` 重放去重所依赖的约束）

PG-lane 事实（CW-043 审计确认）：``viral_import_tasks`` 的 ``locked_until`` /
``updated_at`` / ``created_at`` 皆 ``sa.Text()``（迁移 078），认领/回收用 TEXT 词法
比较 ``locked_until <= now_text``，且 ``now_text`` 由 Python 侧
``datetime.now(UTC).strftime`` 生成——全程与会话 TimeZone 无关。故本模块用固定过去
文本 ``'2020-01-01 00:00:00'`` 制造确定性过期，不依赖 SQL ``now()``。

专属隔离库 ``cw043_viral_import_test`` 已登记 ``pg_test_kit.RECORDED_TEST_DATABASES``；
零 SQLite 替代、零缺库 skip（缺 PG 即硬失败，PG-05）；用例间 DELETE 复位隔离。
认领/失败经 ``pg_transaction`` 生产事务通道（与 ``run_pg_worker_once`` 同形）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any, cast

# 审计写入器在导入期即要求 HMAC key（enqueue 走 insert_audit）；先于 app 导入设置。
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-cw043-viral-import-matrix-tests-minimum-48-bytes-long",
)

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)
from psycopg.rows import dict_row

from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.viral_import import (
    ViralImportError,
    ViralImportLease,
    acquire_viral_import_task,
    fail_viral_import_task,
)

CW043_VIRAL_IMPORT_TEST_DB = "cw043_viral_import_test"


@pytest.mark.parametrize(
    "failure", [None, "upload", "tampered", "production", "superseded", "relocated"]
)
def test_cached_local_media_moves_to_cos_without_provider_call_or_losing_source(
    pg_state, tmp_path, monkeypatch, failure
):
    from fastapi import HTTPException

    from app.storage import FakeStorageAdapter, LocalStorageAdapter
    from app.viral_media_preparation import ViralMediaLeaseLost, ViralMediaPreparation

    monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "0")
    old = LocalStorageAdapter(root=tmp_path, bucket="local-private")
    content = b"verified historical video"
    original, _ = ViralMediaPreparation(storage=old).fetch(
        platform="douyin",
        video_id="legacy-migrate",
        kind="video",
        prepare=lambda key, check: old.put_object(key, content, content_type="video/mp4"),
    )
    before = _rows(
        pg_state, "SELECT * FROM viral_media_preparations WHERE video_id='legacy-migrate'"
    )[0]
    cloud = FakeStorageAdapter(provider="cos", bucket="current")
    if failure == "upload":

        def fail_upload(*args, **kwargs):
            raise RuntimeError("cloud unavailable")

        monkeypatch.setattr(cloud, "put_file", fail_upload)
    elif failure == "tampered":
        _exec(
            pg_state,
            "UPDATE viral_media_preparations SET storage_uri='local://local-private/unrelated.mp4' "
            "WHERE video_id='legacy-migrate'",
        )
    elif failure == "production":
        monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "1")
    elif failure == "relocated":
        import shutil

        restored = tmp_path.parent / f"{tmp_path.name}-restored"
        shutil.copytree(tmp_path, restored)
        monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(restored))
    elif failure == "superseded":
        original_put = cloud.put_file

        def concurrent_update(*args, **kwargs):
            stored = original_put(*args, **kwargs)
            _exec(
                pg_state,
                "UPDATE viral_media_preparations SET status='RUNNING',"
                "attempt=attempt+1 WHERE video_id='legacy-migrate'",
            )
            return stored

        monkeypatch.setattr(cloud, "put_file", concurrent_update)
    coordinator = ViralMediaPreparation(storage=cloud)
    if failure in {"upload", "production", "superseded"}:
        with pytest.raises((RuntimeError, HTTPException, ViralMediaLeaseLost)):
            coordinator.cached(platform="douyin", video_id="legacy-migrate", kind="video")
    else:
        result = coordinator.cached(platform="douyin", video_id="legacy-migrate", kind="video")
        if failure == "tampered":
            assert result is None
        else:
            assert result is not None and result.uri.startswith("cos://current/")
            assert cloud.get_object(result.key) == content
            assert (
                coordinator.cached(platform="douyin", video_id="legacy-migrate", kind="video")
                == result
            )
    after = _rows(
        pg_state, "SELECT * FROM viral_media_preparations WHERE video_id='legacy-migrate'"
    )[0]
    if failure in {"upload", "production"}:
        assert after["storage_uri"] == before["storage_uri"]
        assert after["cache_scope"] == before["cache_scope"]
    elif failure == "superseded":
        assert after["status"] == "RUNNING"
        assert after["attempt"] == before["attempt"] + 1
        assert after["storage_uri"] == before["storage_uri"]
    assert old.get_object(original.key) == content


def test_shared_media_preparation_cross_connection_cache(pg_state: str, tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    started, release = Event(), Event()
    calls = []

    def prepare(key, check):
        calls.append(key)
        started.set()
        assert release.wait(5)
        check()
        return storage.put_object(key, b"video", content_type="video/mp4")

    def fetch():
        return ViralMediaPreparation(storage=storage, poll_seconds=0.01).fetch(
            platform="wechat_channels", video_id="same-video", kind="video", prepare=prepare
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetch)
        assert started.wait(5)
        second = pool.submit(fetch)
        release.set()
        a, b = first.result(), second.result()
    assert len(calls) == 1
    assert a[0].uri == b[0].uri
    assert sorted([a[1], b[1]]) == [False, True]
    row = _rows(pg_state, "SELECT * FROM viral_media_preparations")[0]
    assert row["status"] == "SUCCEEDED"
    assert row["storage_uri"] == a[0].uri


@pytest.mark.parametrize("stage", ["upload", "publish"])
def test_expired_shared_attempt_cannot_publish_or_delete_successor(pg_state, stage):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaLeaseLost, ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    old = ViralMediaPreparation(storage=storage)
    paused, release = Event(), Event()
    old_keys = []

    def prepare_old(key, check):
        old_keys.append(key)
        stored = storage.put_object(key, b"old-video", content_type="video/mp4")
        if stage == "upload":
            paused.set()
            assert release.wait(5)
        return stored

    publish = old._publish

    def pause_publish(lease, stored):
        paused.set()
        assert release.wait(5)
        publish(lease, stored)

    if stage == "publish":
        old._publish = pause_publish
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            old.fetch,
            platform="wechat_channels",
            video_id="lease-video",
            kind="video",
            prepare=prepare_old,
        )
        assert paused.wait(5)
        _exec(pg_state, "UPDATE viral_media_preparations SET locked_until='2020-01-01 00:00:00'")
        newer, hit = ViralMediaPreparation(storage=storage).fetch(
            platform="wechat_channels",
            video_id="lease-video",
            kind="video",
            prepare=lambda key, check: storage.put_object(
                key, b"new-video", content_type="video/mp4"
            ),
        )
        release.set()
        with pytest.raises(ViralMediaLeaseLost):
            future.result()
    assert not hit
    assert newer.key != old_keys[0]
    assert storage.get_object(newer.key) == b"new-video"
    row = _rows(pg_state, "SELECT * FROM viral_media_preparations")[0]
    assert row["status"] == "SUCCEEDED" and row["storage_uri"] == newer.uri
    assert row["attempt"] == 2


def test_preview_and_import_share_one_object_across_every_import(pg_state, monkeypatch):
    """DEDUP-CAS-20260916：导入不再为每个项目各写一份私有副本。

    旧断言固定的是「每次导入一份 attempt 私有副本」（`new.stored.key !=
    old.stored.key != other.stored.key`）。那正是本次要消除的重复：同一平台视频
    导入 N 个项目就存 N 份完整视频。新不变量更强——三个导入（同属主两次 +
    他属主一次）必须指向**同一个**对象，且下载只发生一次。
    """
    from test_viral_media import _video

    from app import viral_media
    from app.storage import FakeStorageAdapter
    from app.viral_import import (
        ViralImportWork,
        discard_viral_import_outcome,
        perform_viral_import_task,
    )

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    video = _video("douyin", _playback_version=1)
    calls = []

    def stream(self, url):
        calls.append(url)
        yield b"\x00\x00\x00\x18ftypisom" + b"video"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", stream)
    preview = viral_media.ViralMediaPipeline(client=None, storage=storage, shared=True).fetch(
        video, prefer="video"
    )
    objects_before_imports = len(storage._objects)

    def work(owner, attempt):
        return ViralImportWork(
            lease=ViralImportLease(
                id=f"task-{owner}",
                worker_id=f"worker-{attempt}",
                owner_user_id=owner,
                project_id=f"project-{owner}",
                platform=video.platform,
                video_id=video.video_id,
                purpose="replica",
                attempt=attempt,
            ),
            video=video,
            client=None,
            storage=storage,
            prefer="video",
        )

    old = perform_viral_import_task(work("u1", 1))
    new = perform_viral_import_task(work("u1", 2))
    other = perform_viral_import_task(work("u2", 1))
    discard_viral_import_outcome(storage, outcome=old, actor_id="u1")
    assert len(calls) == 1
    # One shared object serves every import, including the other user's.
    assert new.stored.key == old.stored.key == other.stored.key
    assert len(storage._objects) == objects_before_imports
    assert storage.head_object(new.stored.key) and storage.head_object(other.stored.key)
    assert (
        _rows(pg_state, "SELECT storage_uri FROM viral_media_preparations")[0]["storage_uri"]
        == preview.storage_uri
    )
    # Discarding an outcome must not remove the shared bytes any more.
    assert storage.head_object(old.stored.key) is not None


def test_broken_copy_backend_cannot_fail_an_import(pg_state, monkeypatch):
    """DEDUP-CAS-20260916：导入不再调用 copy_object，故副本故障不再能拖垮导入。

    旧版本用 ``BrokenCopy.copy_object`` 抛错来证明「副本失败不会污染共享媒体」——
    那条路径如今根本不存在。保留同一个故障注入，断言的是更强的事实：整个导入
    过程一次 copy 都不发起，共享媒体依旧是缓存命中。
    """
    from test_viral_media import _video

    from app import viral_media
    from app.storage import FakeStorageAdapter
    from app.viral_import import ViralImportWork, perform_viral_import_task

    _exec(pg_state, "DELETE FROM viral_media_preparations")

    class BrokenCopy(FakeStorageAdapter):
        def copy_object(self, source_key, destination_key):
            raise RuntimeError("project storage copy failed")

    storage = BrokenCopy(provider="cos", bucket="shared")
    video = _video("douyin", _playback_version=1)
    calls = []

    def stream(self, url):
        calls.append(url)
        yield b"\x00\x00\x00\x18ftypisom"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", stream)
    work = ViralImportWork(
        lease=ViralImportLease(
            id="copy-fails",
            worker_id="worker",
            owner_user_id="u1",
            project_id="project",
            platform=video.platform,
            video_id=video.video_id,
            purpose="replica",
            attempt=1,
        ),
        video=video,
        client=None,
        storage=storage,
        prefer="video",
    )
    viral_media.ViralMediaPipeline(client=None, storage=storage, shared=True).fetch(
        video, prefer="video"
    )
    outcome = perform_viral_import_task(work)
    assert outcome.stored.key
    cached = viral_media.ViralMediaPipeline(client=None, storage=storage, shared=True).fetch(
        video, prefer="video"
    )
    assert cached.cache_hit and len(calls) == 1


def test_changed_storage_namespace_and_missing_object_prepare_again(pg_state):
    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storages = [
        FakeStorageAdapter(provider="cos", bucket="shared", key_prefix=prefix)
        for prefix in ("first", "second")
    ]
    results = []
    for storage in storages:
        coordinator = ViralMediaPreparation(storage=storage)

        def prepare(key, check):
            return storage.put_object(key, b"video", content_type="video/mp4")

        first, hit = coordinator.fetch(
            platform="wechat_channels", video_id="scope", kind="video", prepare=prepare
        )
        assert not hit
        storage.delete_object(first.key)
        recovered, hit = coordinator.fetch(
            platform="wechat_channels", video_id="scope", kind="video", prepare=prepare
        )
        assert not hit and recovered.key != first.key
        results.append(recovered)
    assert results[0].uri != results[1].uri


def test_waiter_does_not_reclaim_when_running_snapshot_becomes_succeeded(pg_state, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    started, release = Event(), Event()
    owner, waiter = ViralMediaPreparation(storage=storage), ViralMediaPreparation(storage=storage)

    def prepare(key, check):
        started.set()
        assert release.wait(5)
        return storage.put_object(key, b"video", content_type="video/mp4")

    cached = waiter._cached

    def interleave(row, kind):
        if row["status"] == "RUNNING":
            release.set()
            future.result(timeout=5)
            return None
        return cached(row, kind)

    monkeypatch.setattr(waiter, "_cached", interleave)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            owner.fetch, platform="wechat_channels", video_id="race", kind="video", prepare=prepare
        )
        assert started.wait(5)
        result, hit = waiter.fetch(
            platform="wechat_channels",
            video_id="race",
            kind="video",
            prepare=lambda key, check: pytest.fail("completed media downloaded again"),
        )
    assert hit and result.uri == future.result()[0].uri
    assert _rows(pg_state, "SELECT attempt FROM viral_media_preparations")[0]["attempt"] == 1


@pytest.mark.parametrize("interval_days", [1, 7])
def test_schedule_is_unique_and_does_not_repeat_until_due(pg_state, interval_days):
    import json
    from concurrent.futures import ThreadPoolExecutor

    from app.viral_collection import enqueue_due_viral_collections

    _exec(pg_state, "DELETE FROM viral_refresh_tasks")
    _exec(pg_state, "DELETE FROM viral_runtime_controls")
    keywords = [{"platform": "wechat_channels", "category": "预算", "keyword": "建房预算"}]
    _exec(
        pg_state,
        "INSERT INTO viral_runtime_controls"
        "(id,collection_enabled,import_enabled,keywords_json,collection_interval_days) "
        "VALUES(1,1,1,%s,%s)",
        (json.dumps(keywords), interval_days),
    )

    def tick():
        with pg_transaction() as raw:
            enqueue_due_viral_collections(BusinessConnection.postgres(raw))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tick(), range(2)))
    assert len(_rows(pg_state, "SELECT * FROM viral_refresh_tasks")) == 1
    assert _rows(
        pg_state,
        f"SELECT next_collection_at BETWEEN now()+interval '{interval_days * 24 - 1} hours' "
        f"AND now()+interval '{interval_days * 24 + 1} hours' AS future "
        "FROM viral_runtime_controls",
    )[0]["future"]
    _exec(pg_state, "UPDATE viral_refresh_tasks SET status='SUCCEEDED'")
    tick()
    assert _rows(pg_state, "SELECT status FROM viral_refresh_tasks")[0]["status"] == "SUCCEEDED"
    _exec(
        pg_state, "UPDATE viral_runtime_controls SET next_collection_at=now()-interval '1 minute'"
    )
    tick()
    assert _rows(pg_state, "SELECT status FROM viral_refresh_tasks")[0]["status"] == "PENDING"
    _exec(
        pg_state,
        "UPDATE viral_refresh_tasks SET status='FAILED',retryable=1,retry_count=2,"
        "updated_at='2020-01-01 00:00:00'",
    )
    tick()
    assert _rows(pg_state, "SELECT status FROM viral_refresh_tasks")[0]["status"] == "FAILED"
    _exec(pg_state, "DELETE FROM viral_runtime_controls")


def test_customer_cached_only_path_never_starts_preparation(pg_state, monkeypatch):
    from test_viral_media import _video

    from app.storage import FakeStorageAdapter
    from app.viral_media import UrlFetcher, ViralMediaPipeline
    from app.viral_media_preparation import ViralMediaBusy

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    monkeypatch.setattr(
        UrlFetcher, "iter_fetch", lambda *args: pytest.fail("customer click fetched source")
    )
    with pytest.raises(ViralMediaBusy):
        ViralMediaPipeline(client=None, storage=storage, shared=True, cached_only=True).fetch(
            _video("douyin"), prefer="video"
        )
    assert _rows(pg_state, "SELECT * FROM viral_media_preparations") == []


# 固定“已过期”时刻：acquire 用 TEXT 词法比较 locked_until <= now_text，
# 该值恒小于任何真实 now（2026+），确定性触发回收且与会话 TimeZone 无关。
_EXPIRED = "2020-01-01 00:00:00"
# 固定“仍持有”时刻：恒大于真实 now，代表未过期的活动租约。
_HELD = "2999-01-01 00:00:00"
_SEED_STAMP = "2026-09-06 03:00:00"


# ---------------------------------------------------------------------------
# 座子：专属库 + DATABASE_URL_ENV 指向它（pg_transaction 走生产 PG 通道）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def import_dsn() -> Iterator[str]:
    """专属爆款导入队列测试库：建库 → alembic head → 用完即删."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW043_VIRAL_IMPORT_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW043_VIRAL_IMPORT_TEST_DB)


@pytest.fixture()
def pg_state(import_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """DATABASE_URL_ENV 指向专属库：``pg_transaction`` 走生产 PG 事务通道."""
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, import_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield import_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# 原语播种 helper（worker-identical 行，无路由机制）
# ---------------------------------------------------------------------------

# 叶子优先 DELETE：viral_import_tasks 的 project_id/owner_user_id 是 FK。
_CLEANUP_ORDER = ("viral_import_tasks", "projects", "users")


def _exec(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(sql, params)


def _rows(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as pg:
        return [dict(row) for row in pg.execute(sql, params).fetchall()]


def _one(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(sql, params).fetchone()
        return None if row is None else row[0]


def _truncate(dsn: str) -> None:
    for table in _CLEANUP_ORDER:
        _exec(dsn, f"DELETE FROM {table}")


def _seed_base(dsn: str) -> None:
    _truncate(dsn)
    _exec(
        dsn,
        "INSERT INTO users (id, username, display_name, role) VALUES"
        " ('u1', 'u1', 'User One', 'employee'),"
        " ('u2', 'u2', 'User Two', 'employee')",
    )
    _exec(
        dsn,
        "INSERT INTO projects (id, name, owner_user_id) VALUES"
        " ('proj-1', 'CW043 Viral Import', 'u1')",
    )


def _seed_import_task(
    dsn: str,
    *,
    task_id: str,
    owner: str = "u1",
    project_id: str | None = "proj-1",
    platform: str = "douyin",
    video_id: str = "vid-1",
    purpose: str = "replica",
    idempotency_key: str | None = None,
    status: str = "PENDING",
    attempt: int = 0,
    locked_by: str | None = None,
    locked_until: str | None = None,
) -> None:
    _exec(
        dsn,
        "INSERT INTO viral_import_tasks (id, owner_user_id, project_id, platform,"
        " video_id, purpose, idempotency_key, request_hash, request_json, status,"
        " attempt, locked_by, locked_until, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '{}', %s, %s, %s, %s, %s, %s)",
        (
            task_id,
            owner,
            project_id,
            platform,
            video_id,
            purpose,
            idempotency_key or f"ik-{task_id}",
            f"rh-{task_id}",
            status,
            attempt,
            locked_by,
            locked_until,
            _SEED_STAMP,
            _SEED_STAMP,
        ),
    )


def _import_row(dsn: str, task_id: str) -> dict[str, Any]:
    return _rows(
        dsn,
        "SELECT status, attempt, locked_by, locked_until, retryable, error_code,"
        " started_at FROM viral_import_tasks WHERE id = %s",
        (task_id,),
    )[0]


def _expire_lease(dsn: str, task_id: str) -> None:
    _exec(
        dsn,
        "UPDATE viral_import_tasks SET locked_until = %s WHERE id = %s",
        (_EXPIRED, task_id),
    )


def _acquire(worker_id: str) -> ViralImportLease | None:
    with pg_transaction() as raw:
        return acquire_viral_import_task(BusinessConnection.postgres(raw), worker_id=worker_id)


def _fail(lease: ViralImportLease, cause: Exception) -> None:
    with pg_transaction() as raw:
        fail_viral_import_task(BusinessConnection.postgres(raw), lease=lease, cause=cause)


def test_import_commit_acknowledgement_loss_preserves_published_project_copy(pg_state, monkeypatch):
    from contextlib import contextmanager

    from app import generation_worker as worker
    from app.storage import FakeStorageAdapter
    from app.viral_import import ViralImportOutcome

    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="commit-receipt")
    storage = FakeStorageAdapter(provider="cos", bucket="project")
    stored = storage.put_object("project/attempt-1.mp4", b"project-video", content_type="video/mp4")
    outcome = ViralImportOutcome(stored=stored, media_kind="video", duration_seconds=5)
    monkeypatch.setattr(worker, "prepare_viral_import_task", lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, "perform_viral_import_task", lambda *_: outcome)
    original = worker.pg_transaction
    lost = False

    @contextmanager
    def commit_then_disconnect():
        nonlocal lost
        with original() as raw:
            yield raw
            published = (
                raw.execute(
                    "SELECT status FROM viral_import_tasks WHERE id='commit-receipt'"
                ).fetchone()[0]
                == "SUCCEEDED"
            )
        if published and not lost:
            lost = True
            raise OSError("committed transaction acknowledgement lost")

    monkeypatch.setattr(worker, "pg_transaction", commit_then_disconnect)
    assert worker.run_pg_worker_once(worker_id="commit-worker", storage=storage, max_tasks=1) == 1
    assert lost
    assert _import_row(pg_state, "commit-receipt")["status"] == "SUCCEEDED"
    assert storage.get_object(stored.key) == b"project-video"
    assert _one(pg_state, "SELECT count(*) FROM assets WHERE storage_uri=%s", (stored.uri,)) == 1


# ===========================================================================
# A. 独占认领（FOR UPDATE SKIP LOCKED + 状态机）
# ===========================================================================


def test_acquire_viral_import_lease_is_exclusive_on_real_pg(pg_state: str) -> None:
    """一个 PENDING 导入任务只被一个 worker 认领：worker-a 得租约（attempt 0→1、
    status→RUNNING、locked_by 落定），worker-b 在真实 PG 的 ``FOR UPDATE SKIP
    LOCKED`` + ``status='PENDING'`` 状态机下拿不到同一任务（返回 None）。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1")

    first = _acquire("worker-a")
    assert first is not None
    assert first.id == "vi-1"
    assert first.worker_id == "worker-a"
    assert first.attempt == 1
    assert first.owner_user_id == "u1"
    assert first.project_id == "proj-1"

    second = _acquire("worker-b")
    assert second is None

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "RUNNING"
    assert row["locked_by"] == "worker-a"
    assert int(row["attempt"]) == 1
    # started_at 首次认领即落定。
    assert row["started_at"] is not None


def test_expired_viral_import_lease_is_reclaimed_and_attempt_increments_on_real_pg(
    pg_state: str,
) -> None:
    """worker-a 持有的租约过期（locked_until 过去）后，acquire 的首段 UPDATE 把它
    复位 PENDING（保留 attempt），次段 UPDATE 由 worker-b 重新认领：attempt 1→2、
    locked_by 换到 worker-b、status 仍 RUNNING。这是 PG lane 崩溃 worker 的任务
    回收路径。"""
    _seed_base(pg_state)
    _seed_import_task(
        pg_state,
        task_id="vi-1",
        status="RUNNING",
        attempt=1,
        locked_by="worker-a",
        locked_until=_HELD,
    )
    _expire_lease(pg_state, "vi-1")

    reacquired = _acquire("worker-b")
    assert reacquired is not None
    assert reacquired.id == "vi-1"
    assert reacquired.attempt == 2

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "RUNNING"
    assert row["locked_by"] == "worker-b"
    assert int(row["attempt"]) == 2


# ===========================================================================
# B. 失败释放锁 + retryable 分级
# ===========================================================================


def test_fail_viral_import_task_releases_lease_and_marks_retryable_on_real_pg(
    pg_state: str,
) -> None:
    """普通异常（非 ViralImportError）→ status FAILED、retryable=1（默认可重试）、
    error_code=VIRAL_IMPORT_FAILED、locked_by/locked_until 释放为 NULL。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1")
    lease = _acquire("worker-a")
    assert lease is not None

    _fail(lease, RuntimeError("provider transport dropped"))

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "FAILED"
    assert int(row["retryable"]) == 1
    assert row["error_code"] == "VIRAL_IMPORT_FAILED"
    assert row["locked_by"] is None
    assert row["locked_until"] is None


def test_terminal_viral_import_error_marks_non_retryable_on_real_pg(pg_state: str) -> None:
    """终态 ViralImportError(retryable=False) → status FAILED、retryable=0（不再
    自动重试）、error_code 取异常携带的业务码。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1")
    lease = _acquire("worker-a")
    assert lease is not None

    _fail(
        lease,
        ViralImportError(
            409, "VIRAL_IMPORT_PROJECT_CHANGED", "目标项目归属已变化。", retryable=False
        ),
    )

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "FAILED"
    assert int(row["retryable"]) == 0
    assert row["error_code"] == "VIRAL_IMPORT_PROJECT_CHANGED"
    assert row["locked_by"] is None


# ===========================================================================
# C. 栅栏令牌：陈旧租约的迟到失败不得覆盖已恢复状态
# ===========================================================================


def test_stale_viral_import_lease_failure_is_a_noop_on_real_pg(pg_state: str) -> None:
    """worker-a 认领后租约过期、被 worker-b 回收（attempt→2）。worker-a 迟到的
    fail 携带陈旧栅栏（locked_by=worker-a、attempt=1），其 WHERE 与库中现值
    （worker-b、attempt=2）不匹配 → 0 行更新（no-op），任务保持 worker-b 的
    RUNNING/attempt=2 不被覆盖。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1")

    stale_lease = _acquire("worker-a")
    assert stale_lease is not None
    assert stale_lease.attempt == 1

    # 租约过期 → worker-b 回收（attempt 2、locked_by worker-b）。
    _expire_lease(pg_state, "vi-1")
    fresh_lease = _acquire("worker-b")
    assert fresh_lease is not None
    assert fresh_lease.attempt == 2

    # 陈旧 worker-a 的迟到失败：栅栏不匹配 → no-op。
    _fail(stale_lease, RuntimeError("late failure from the stale worker"))

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "RUNNING", "陈旧失败不得覆盖已回收的 RUNNING 状态"
    assert row["locked_by"] == "worker-b"
    assert int(row["attempt"]) == 2


# ===========================================================================
# D. 幂等键按属主唯一（enqueue ON CONFLICT 重放去重所依赖的约束）
# ===========================================================================


def test_viral_import_idempotency_key_is_unique_per_owner_on_real_pg(pg_state: str) -> None:
    """``uq_viral_import_tasks_owner_idempotency``：同属主重复幂等键触发唯一违例
    （enqueue 的 ``ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING`` 重放
    去重即依赖它）；不同属主可复用同一幂等键（约束按属主范围）。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1", owner="u1", idempotency_key="shared-key")

    with pytest.raises(psycopg.IntegrityError):
        _seed_import_task(pg_state, task_id="vi-2", owner="u1", idempotency_key="shared-key")

    # 另一属主复用同一幂等键：约束是 (owner_user_id, idempotency_key) 复合唯一。
    _seed_import_task(pg_state, task_id="vi-3", owner="u2", idempotency_key="shared-key")

    assert cast(int, _one(pg_state, "SELECT count(*) FROM viral_import_tasks")) == 2
