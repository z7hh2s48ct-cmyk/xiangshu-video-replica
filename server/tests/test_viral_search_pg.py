"""爆款视频搜索 P1（服务端基座）：搜索接口 / 内容池 / 发现记录 / 计费.

TEST-PG：复用 test_customer_pricing 的夹具链（cw076_registration_test 专属库，
module 级建库 → alembic head → 每用例 TRUNCATE users/wallets 等）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

import json
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from test_customer_pricing import (  # noqa: F401
    account,
    client,
    pricing_client,
    registration_client,
    registration_dsn,
    route_state,
)
from test_usage_billing import credit_lot  # noqa: F401

from app.db_portable import BusinessConnection


def test_viral_search_discoveries_schema_present_at_head(route_state: str) -> None:
    """新迁移在链尾：发现记录表 9 列 + 五列唯一约束 + 两个查询索引，且无 FK."""
    with psycopg.connect(route_state) as pg:
        columns = pg.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='viral_search_discoveries' "
            "ORDER BY ordinal_position"
        ).fetchall()
        assert [row[0] for row in columns] == [
            "id",
            "user_id",
            "keyword",
            "platform",
            "video_id",
            "search_date",
            "searched_at",
            "created_at",
            "updated_at",
        ]
        unique = pg.execute(
            "SELECT array_agg(a.attname ORDER BY a.attname) FROM pg_index i "
            "JOIN pg_class c ON c.oid=i.indrelid "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ANY(i.indkey) "
            "WHERE c.relname='viral_search_discoveries' AND i.indisunique "
            "AND NOT i.indisprimary"
        ).fetchone()
        assert unique[0] == ["keyword", "platform", "search_date", "user_id", "video_id"]
        assert (
            pg.execute(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_name='viral_search_discoveries' AND constraint_type='FOREIGN KEY'"
            ).fetchone()[0]
            == 0
        )
        indexes = {
            row[0]
            for row in pg.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                "AND tablename='viral_search_discoveries'"
            ).fetchall()
        }
        assert {
            "ix_viral_search_discoveries_user_date",
            "ix_viral_search_discoveries_date",
        } <= indexes


def test_viral_search_service_registered_in_billing_catalog() -> None:
    """viral_search 科目：call/tikhub/viral，接口键复用 viral_extract（两条目录契约）."""
    from app.billing_catalog import (
        INTERFACE_KEYS,
        SERVICE_INTERFACE,
        SERVICES,
        interface_for_service,
    )

    service = SERVICES["viral_search"]
    assert service.name == "爆款视频搜索"
    assert service.unit == "call"
    assert service.provider == "tikhub"
    assert service.module == "viral"
    assert service.customer_charge_allowed is True
    assert interface_for_service("viral_search") == "viral_extract"
    assert SERVICE_INTERFACE["viral_search"] in INTERFACE_KEYS
    assert set(SERVICE_INTERFACE) == set(SERVICES)


def _viral_seed(platform: str = "douyin", video_id: str = "v-1"):
    from app.viral_tikhub import ViralVideo

    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category="",
        title=f"P1 {video_id}",
        author="作者",
        author_avatar=None,
        verified=False,
        cover_url="https://cdn.example/cover.jpg",
        duration_ms=15000,
        likes=10,
        comments=None,
        shares=None,
        collects=None,
        published_at=None,
        published_display=None,
        like_display=None,
    )


def test_upsert_keeps_existing_category_when_search_category_is_empty(
    route_state: str,
) -> None:
    """搜索条目（category=""）不得清空采集时代写入的分类；非空仍可覆盖."""
    from dataclasses import replace

    from app.viral_store import get_viral_video, upsert_viral_videos

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        upsert_viral_videos(conn, [replace(_viral_seed(), category="施工避坑")])
        upsert_viral_videos(conn, [_viral_seed()])
        kept = get_viral_video(conn, platform="douyin", video_id="v-1")
        assert kept is not None and kept.category == "施工避坑"
        upsert_viral_videos(conn, [replace(_viral_seed(), category="建房预算")])
        updated = get_viral_video(conn, platform="douyin", video_id="v-1")
        assert updated is not None and updated.category == "建房预算"


def test_mark_and_list_viral_discoveries_dedup_per_day(route_state: str) -> None:
    """同键 upsert 刷新 searched_at；不同关键词/日期各自成行；左联内容池带回视频."""
    from app.viral_store import (
        list_viral_discoveries,
        mark_viral_discoveries,
        upsert_viral_videos,
    )

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        upsert_viral_videos(conn, [_viral_seed()])
        mark_viral_discoveries(
            conn,
            user_id="u-1",
            keyword="农村建房",
            platform="douyin",
            video_ids=["v-1", "v-missing"],
            search_date="2026-09-22",
            searched_at="2026-09-22T01:00:00+00:00",
        )
        mark_viral_discoveries(
            conn,
            user_id="u-1",
            keyword="农村建房",
            platform="douyin",
            video_ids=["v-1"],
            search_date="2026-09-22",
            searched_at="2026-09-22T02:00:00+00:00",
        )
        mark_viral_discoveries(
            conn,
            user_id="u-1",
            keyword="农村建房",
            platform="douyin",
            video_ids=["v-1"],
            search_date="2026-09-23",
            searched_at="2026-09-23T01:00:00+00:00",
        )
        raw.commit()
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries WHERE user_id='u-1'"
            ).fetchone()[0]
            == 3
        )
        same_day = list_viral_discoveries(conn, user_id="u-1", search_date="2026-09-22")
        assert len(same_day) == 2
        refreshed = next(item for item in same_day if item.video is not None)
        assert refreshed.searched_at == "2026-09-22T02:00:00+00:00"
        assert refreshed.video is not None and refreshed.video.video_id == "v-1"
        missing = next(item for item in same_day if item.video is None)
        assert missing.video is None
        # 内容池条目缺失时，发现记录自身仍带有平台与视频 ID。
        assert (missing.platform, missing.video_id) == ("douyin", "v-missing")
        other_day = list_viral_discoveries(conn, user_id="u-1", search_date="2026-09-23")
        assert len(other_day) == 1


def test_cached_transcript_and_batch_lookup(route_state: str) -> None:
    """共享文案缓存只读查询：单条 / 批量 / 未命中 / 脏 JSON 一律安全."""
    from app.asr import TranscriptResult
    from app.script_from_audio import cached_transcript, cached_transcripts

    payload = json.dumps(
        {"text": "农村自建房避坑指南", "duration_sec": 15.0, "language": "zh"},
        ensure_ascii=False,
    )
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_script_cache WHERE video_id IN ('v-1', 'v-broken')")
        raw.execute(
            "INSERT INTO viral_script_cache (platform, video_id, result_json) "
            "VALUES ('douyin', 'v-1', %s), ('douyin', 'v-broken', 'not-json')",
            (payload,),
        )
        raw.commit()
        conn = BusinessConnection.postgres(raw)
        hit = cached_transcript(conn, platform="douyin", video_id="v-1")
        assert hit is not None
        assert hit.result == TranscriptResult("农村自建房避坑指南", 15.0, "zh")
        assert "T" in hit.updated_at  # ISO 时间串（GET /api/viral/search/copy 的 updatedAt）
        assert cached_transcript(conn, platform="douyin", video_id="v-404") is None
        assert cached_transcript(conn, platform="douyin", video_id="v-broken") is None
        batch = cached_transcripts(
            conn,
            [
                ("douyin", "v-1"),
                ("douyin", "v-404"),
                ("douyin", "v-1"),
                ("wechat_channels", "v-1"),
            ],
        )
        assert set(batch) == {("douyin", "v-1")}
        assert batch[("douyin", "v-1")].result.text == "农村自建房避坑指南"
        assert cached_transcripts(conn, []) == {}


class _SearchStub:
    """搜索数据源桩：只实现两个搜索面（duck typing 满足 ViralSourceClient 用法）."""

    def __init__(self, *, wechat=None):
        self.wechat = wechat
        self.calls: list[tuple[str, str, str | None, str]] = []

    def douyin_search_page(
        self, *, keyword, category="", sort_type="1", publish_time="7", cursor=None
    ):
        self.calls.append(("douyin", keyword, cursor, publish_time))
        from app.viral_tikhub import DouyinSearchPage

        return DouyinSearchPage(videos=[_viral_seed()], cursor=None, has_more=False)

    def wechat_search_page(
        self, *, keyword, category="", sort="hot", publish_time="week", cursor=None
    ):
        self.calls.append(("wechat", keyword, cursor, publish_time))
        return self.wechat


def test_search_date_shanghai_follows_shanghai_calendar() -> None:
    """发现日期按上海日历归属（UTC 23:30 已是上海次日）."""
    from app.viral_search import search_date_shanghai

    assert search_date_shanghai(datetime(2026, 9, 21, 23, 30, tzinfo=UTC)) == "2026-09-22"
    assert search_date_shanghai(datetime(2026, 9, 22, 16, 30, tzinfo=UTC)) == "2026-09-23"


def test_run_viral_search_platform_shapes() -> None:
    """两平台都按游标翻页：抖音透传不透明游标；视频号透传上游 cursor."""
    from app.viral_search import run_viral_search
    from app.viral_tikhub import DouyinSearchPage, WechatSearchPage

    douyin_cursor = '{"c":10,"s":"sid-1","b":""}'
    stub = _SearchStub()
    stub_page = DouyinSearchPage(videos=[_viral_seed()], cursor=douyin_cursor, has_more=True)
    stub.douyin_search_page = (  # type: ignore[method-assign]
        lambda *, keyword, category="", sort_type="1", publish_time="7", cursor=None: stub_page
    )
    douyin = run_viral_search(stub, keyword="农村建房", platform="douyin", cursor="{}")
    assert [video.video_id for video in douyin.items] == ["v-1"]
    assert douyin.cursor == douyin_cursor and douyin.has_more is True

    stub = _SearchStub(
        wechat=WechatSearchPage(
            videos=[_viral_seed("wechat_channels", "w-1")], cursor="c-2", has_more=True
        )
    )
    wechat = run_viral_search(stub, keyword="农村建房", platform="wechat_channels", cursor="c-1")
    assert [video.video_id for video in wechat.items] == ["w-1"]
    assert wechat.cursor == "c-2" and wechat.has_more is True
    assert stub.calls == [("wechat", "农村建房", "c-1", "week")]


def test_run_viral_search_maps_time_range_per_platform() -> None:
    """中立时间范围映射到各平台取值；缺省「一周」与历史行为一致."""
    from app.viral_search import run_viral_search
    from app.viral_tikhub import WechatSearchPage

    stub = _SearchStub()
    run_viral_search(stub, keyword="农村建房", platform="douyin")
    run_viral_search(stub, keyword="农村建房", platform="douyin", time_range="day")
    wechat_stub = _SearchStub(wechat=WechatSearchPage(videos=[], cursor=None, has_more=False))
    run_viral_search(wechat_stub, keyword="农村建房", platform="wechat_channels", time_range="all")
    assert [call[3] for call in wechat_stub.calls] == ["all"]
    assert stub.calls[0][3] == "7"
    assert stub.calls[1][3] == "1"


def test_archive_search_covers_threads_and_falls_back(monkeypatch) -> None:
    """封面归档：成功者换自有稳定地址；下载失败者保留源站链接且不抛错."""
    from dataclasses import replace

    from app import viral_media
    from app.viral_search import archive_search_covers

    class _FakeStorage:
        def __init__(self) -> None:
            self.puts: dict[str, tuple[bytes, str | None]] = {}

        def head_object(self, key):
            return None

        def put_object(self, key, data, *, content_type=None):
            self.puts[key] = (data, content_type)

    def fake_iter_fetch(self, url):
        if "bad.example" in url:
            raise viral_media.ViralMediaError("媒体地址无法解析")
        yield b"\xff\xd8\xff\xe0fakejpeg"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", fake_iter_fetch)
    good = _viral_seed()
    bad = replace(_viral_seed(video_id="v-2"), cover_url="https://bad.example/cover.jpg")
    storage = _FakeStorage()
    enriched = archive_search_covers(storage, [good, bad])
    assert enriched[0].cover_key == viral_media.viral_cover_key("douyin", "v-1")
    assert enriched[0].cover_url == "/api/viral/covers/douyin/v-1"
    assert enriched[1].cover_key is None
    assert enriched[1].cover_url == "https://bad.example/cover.jpg"
    assert storage.puts  # 至少一个封面已归档


def test_reserve_search_operation_rounds_and_replays(client, route_state) -> None:
    """计费轮次：PENDING/SUCCEEDED 幂等复用；FAILED 后重试才开新轮次重新预留."""
    from app.usage_billing import finish_operation
    from app.viral_search import reserve_search_operation

    _, uid = account(client, "search_reserve")

    def reserve() -> str:
        with psycopg.connect(route_state) as raw:
            return reserve_search_operation(
                BusinessConnection.postgres(raw),
                user_id=uid,
                source_id="viral-search:k1",
                request_fingerprint="douyin:农村建房:",
            )

    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    first = reserve()
    assert reserve() == first  # PENDING 幂等：同轮次复用同一单，不重复扣费
    with psycopg.connect(route_state) as raw:
        finish_operation(
            BusinessConnection.postgres(raw), operation_id=first, units=0, succeeded=False
        )
    second = reserve()  # FAILED（外呼失败已释放）后重试开新轮次
    assert second != first
    with psycopg.connect(route_state) as raw:
        finish_operation(
            BusinessConnection.postgres(raw), operation_id=second, units=1, succeeded=True
        )
    assert reserve() == second  # SUCCEEDED 不重复扣费
    with psycopg.connect(route_state) as raw:
        rounds = [
            row[0]
            for row in raw.execute(
                "SELECT billing_round FROM billing_operations "
                "WHERE source_id='viral-search:k1' ORDER BY billing_round"
            ).fetchall()
        ]
        assert rounds == [1, 2]
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (47, 0)


def test_persist_viral_search_writes_pool_discoveries_and_billing(client, route_state) -> None:
    """落库三件事：内容池 upsert + cover_key 回写 + 发现记录 + 计费结算."""
    from dataclasses import replace

    from app.viral_search import persist_viral_search, reserve_search_operation
    from app.viral_store import upsert_viral_videos

    _, uid = account(client, "search_persist")
    seeded = replace(_viral_seed(), cover_key="viral/covers/douyin/v-1.jpg")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
        conn = BusinessConnection.postgres(raw)
        # 先建立"已存在但 cover_key 为空"的池条目，强制 persist 走 update_viral_cover 回写路径
        # （_UPSERT_SQL 的 ON CONFLICT DO UPDATE 不更新 cover_key，只有 INSERT 新行才写）。
        upsert_viral_videos(conn, [_viral_seed()])
        operation = reserve_search_operation(
            conn, user_id=uid, source_id="viral-search:k2", request_fingerprint="douyin:自建房:"
        )
        persist_viral_search(
            conn,
            user_id=uid,
            source_id="viral-search:k2",
            keyword="自建房",
            platform="douyin",
            videos=[seeded],
            search_date="2026-09-22",
            searched_at="2026-09-22T01:00:00+00:00",
        )
        assert (
            raw.execute(
                "SELECT cover_key FROM viral_videos WHERE platform='douyin' AND video_id='v-1'"
            ).fetchone()[0]
            == "viral/covers/douyin/v-1.jpg"
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries "
                "WHERE user_id=%s AND keyword='自建房' AND search_date='2026-09-22'",
                (uid,),
            ).fetchone()[0]
            == 1
        )
        assert tuple(
            raw.execute(
                "SELECT state, charged_credits FROM billing_operations WHERE id=%s", (operation,)
            ).fetchone()
        ) == ("SUCCEEDED", 3)


@pytest.fixture()
def search_client(client):
    from app.viral_search_routes import router as viral_search_router

    client.app.include_router(viral_search_router)
    yield client
    client.app.dependency_overrides.clear()


def _use_search_stub(search_client, stub) -> None:
    from app.viral_search_routes import get_viral_search_source_client

    search_client.app.dependency_overrides[get_viral_search_source_client] = lambda: stub


class _FakeCoverStorage:
    """封面归档用的假存储（只记录 put，不落盘）."""

    def head_object(self, key):
        return None

    def put_object(self, key, data, *, content_type=None):
        pass


def _patch_search_infra(monkeypatch) -> None:
    from app import viral_media

    def fake_iter_fetch(self, url):
        yield b"\xff\xd8\xff\xe0fakejpeg"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", fake_iter_fetch)
    monkeypatch.setattr(
        "app.viral_search_routes.get_media_storage", lambda conn: _FakeCoverStorage()
    )


def test_search_requires_valid_idempotency_key(search_client) -> None:
    """缺/空 Idempotency-Key → 422 前缀化错误码（先例：viral_import_routes）."""
    _use_search_stub(search_client, _SearchStub())
    headers, _ = account(search_client, "search_key")
    response = search_client.post(
        "/api/viral/search", json={"keyword": "农村建房", "platform": "douyin"}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VIRAL_SEARCH_IDEMPOTENCY_KEY_REQUIRED"


def test_search_charges_persists_and_flags_copy(search_client, route_state, monkeypatch) -> None:
    """一次搜索：外呼 + 封面归档 + 内容池/发现落库 + 按次计费一次."""
    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _SearchStub())
    headers, uid = account(search_client, "search_flow")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
        raw.execute("DELETE FROM viral_script_cache WHERE platform='douyin' AND video_id='v-1'")
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-flow-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["videoId"] for item in body["items"]] == ["v-1"]
    assert body["items"][0]["hasCopy"] is False
    assert body["billing"] == {"charged": 3, "unit": "call"}
    assert body["hasMore"] is False and body["cursor"] is None
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_videos WHERE platform='douyin' AND video_id='v-1'"
            ).fetchone()[0]
            == 1
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries "
                "WHERE user_id=%s AND keyword='农村建房'",
                (uid,),
            ).fetchone()[0]
            == 1
        )
        assert (
            raw.execute(
                "SELECT state FROM billing_operations WHERE source_id=%s",
                (f"viral-search:{uid}:search-flow-1",),
            ).fetchone()[0]
            == "SUCCEEDED"
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (47, 0)


def test_search_flags_has_copy_on_cache_hit(search_client, route_state, monkeypatch) -> None:
    """共享文案缓存命中 → hasCopy=True；``hasCopy`` 是信息性标记，计费照常
    （原用 0 价种子凑 charged=0 的写法随「0 价=未定价」守卫（评审 M-1）作废）."""
    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _SearchStub())
    headers, uid = account(search_client, "search_copy")
    payload = json.dumps({"text": "已有文案", "duration_sec": 12.0}, ensure_ascii=False)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
        raw.execute(
            "INSERT INTO viral_script_cache (platform, video_id, result_json) "
            "VALUES ('douyin', 'v-1', %s) ON CONFLICT (platform, video_id) "
            "DO UPDATE SET result_json=excluded.result_json",
            (payload,),
        )
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-copy-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"][0]["hasCopy"] is True
    assert body["billing"] == {"charged": 3, "unit": "call"}


def test_search_upstream_failure_refunds_and_fails_closed(
    search_client, route_state, monkeypatch
) -> None:
    """外呼失败：计费单转 FAILED 并释放预留，返回 503 稳定错误码."""
    from app.viral_tikhub import ViralSourceError

    class _FailingStub:
        def douyin_search_page(
            self, *, keyword, category="", sort_type="1", publish_time="7", cursor=None
        ):
            raise ViralSourceError("爆款数据源暂时不可用")

    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _FailingStub())
    headers, uid = account(search_client, "search_fail")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-fail-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_SEARCH_UPSTREAM_FAILED"
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT state FROM billing_operations WHERE source_id=%s",
                (f"viral-search:{uid}:search-fail-1",),
            ).fetchone()[0]
            == "FAILED"
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (50, 0)


def test_search_insufficient_credits_fails_closed(search_client, route_state, monkeypatch) -> None:
    """额度不足：402 INSUFFICIENT_CREDITS，不扣预留、不落发现记录（设计 §5.2）."""
    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _SearchStub())
    headers, uid = account(search_client, "search_poor")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=1, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-poor-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "INSUFFICIENT_CREDITS"
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (1, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            == 0
        )


def test_search_empty_result_charges_once(search_client, route_state, monkeypatch) -> None:
    """0 结果：正常返回空列表，但仍完成一次计量（供应商成本已发生，设计 §7）."""
    _patch_search_infra(monkeypatch)

    class _EmptyStub:
        """返回空列表的数据源桩（0 结果是一次合法交付）."""

        def douyin_search_page(
            self, *, keyword, category="", sort_type="1", publish_time="7", cursor=None
        ):
            from app.viral_tikhub import DouyinSearchPage

            return DouyinSearchPage(videos=[], cursor=None, has_more=False)

    _use_search_stub(search_client, _EmptyStub())
    headers, uid = account(search_client, "search_empty")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-empty-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["billing"] == {"charged": 3, "unit": "call"}
    assert body["hasMore"] is False and body["cursor"] is None
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (47, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            == 0
        )


def test_search_replay_reuses_billing_round(search_client, route_state, monkeypatch) -> None:
    """同幂等键重放：同一计费单同一轮次，不重复扣费."""
    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _SearchStub())
    headers, uid = account(search_client, "search_replay")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    first = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-replay-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    second = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-replay-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["billing"] == first.json()["billing"]
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE source_id=%s",
                (f"viral-search:{uid}:search-replay-1",),
            ).fetchone()[0]
            == 1
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (47, 0)


class _RefreshStub:
    """刷新数据源桩：返回单条视频并回写一个更新的 cover."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def douyin_refresh(self, *, platform: str, video_id: str):
        self.calls.append((platform, video_id))
        from dataclasses import replace

        from app.viral_tikhub import ViralVideo

        return [_viral_seed(platform=platform, video_id=video_id)]


def test_refresh_requires_valid_idempotency_key(search_refresh_client) -> None:
    """缺/空 Idempotency-Key → 422 前缀化错误码."""
    headers, _ = account(search_refresh_client, "refresh_key")
    response = search_refresh_client.post(
        "/api/viral/search/refresh",
        json={"platform": "douyin", "videoId": "v-1"},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VIRAL_SEARCH_REFRESH_IDEMPOTENCY_KEY_REQUIRED"


def test_refresh_video_not_found(search_refresh_client, route_state) -> None:
    """视频不存在 → 404 NOT_FOUND."""
    _use_refresh_stub(search_refresh_client, _RefreshStub())
    headers, uid = account(search_refresh_client, "refresh_404")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search_refresh',true,2) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=2"
        )
    response = search_refresh_client.post(
        "/api/viral/search/refresh",
        headers={**headers, "Idempotency-Key": "refresh-404-1"},
        json={"platform": "douyin", "videoId": "v-nonexistent"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "VIRAL_VIDEO_NOT_FOUND"


def test_refresh_video_deleted(search_refresh_client, route_state) -> None:
    """视频已删除 → 403 FORBIDDEN_DELETED."""
    from app.viral_tikhub import ViralVideo

    _use_refresh_stub(search_refresh_client, _RefreshStub())
    headers, uid = account(search_refresh_client, "refresh_403")
    with psycopg.connect(route_state) as raw:
        # 先插入一条视频并标记为 deleted
        raw.execute(
            "INSERT INTO viral_videos (platform, video_id, title, author, deleted_at) "
            "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)",
            ("douyin", "v-deleted", "Deleted Video", "Author"),
        )
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search_refresh',true,2) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=2"
        )
    response = search_refresh_client.post(
        "/api/viral/search/refresh",
        headers={**headers, "Idempotency-Key": "refresh-403-1"},
        json={"platform": "douyin", "videoId": "v-deleted"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "VIRAL_VIDEO_FORBIDDEN_DELETED"


def test_refresh_charges_once_and_returns_updated_video(
    search_refresh_client, route_state, monkeypatch
) -> None:
    """正常刷新：扣费 1 单位，返回更新后的视频数据."""
    from dataclasses import replace

    _patch_refresh_infra(monkeypatch)
    _use_refresh_stub(search_refresh_client, _RefreshStub())
    headers, uid = account(search_refresh_client, "refresh_ok")
    with psycopg.connect(route_state) as raw:
        # 先插入一条视频
        raw.execute(
            "INSERT INTO viral_videos (platform, video_id, title, author, cover_url) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("douyin", "v-refresh", "Original Title", "Author", "https://old.com/cover.jpg"),
        )
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search_refresh',true,2) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=2"
        )
    response = search_refresh_client.post(
        "/api/viral/search/refresh",
        headers={**headers, "Idempotency-Key": "refresh-ok-1"},
        json={"platform": "douyin", "videoId": "v-refresh"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["video"]["videoId"] == "v-refresh"
    assert body["billing"] == {"charged": 2, "unit": "call"}
    # 验证钱包余额被扣除
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (48, 0)


def test_refresh_rejects_unsupported_platform_before_reserving(
    search_refresh_client, route_state
) -> None:
    """非抖音平台在预留计费前 422 拒绝（H-3：拿抖音搜索顶替会污染内容池并扣费）。"""
    _use_refresh_stub(search_refresh_client, _RefreshStub())
    headers, uid = account(search_refresh_client, "refresh_pf")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO viral_videos (platform, video_id, title, author) "
            "VALUES ('wechat_channels', 'v-wechat-pf', 'W', 'A')",
        )
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search_refresh',true,2) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=2"
        )
    response = search_refresh_client.post(
        "/api/viral/search/refresh",
        headers={**headers, "Idempotency-Key": "refresh-pf-1"},
        json={"platform": "wechat_channels", "videoId": "v-wechat-pf"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VIRAL_SEARCH_REFRESH_PLATFORM_UNSUPPORTED"
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations "
                "WHERE user_id=%s AND service='viral_search_refresh'",
                (uid,),
            ).fetchone()[0]
            == 0
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (50, 0)


def test_refresh_mismatched_upstream_result_never_pollutes_or_charges(
    search_refresh_client, route_state, monkeypatch
) -> None:
    """上游返回无关视频（video_id 不匹配）→ 503、释放预留、无关数据不入库（H-3）。"""

    class _MismatchedStub(_RefreshStub):
        def douyin_refresh(self, *, platform: str, video_id: str):
            return [_viral_seed(platform=platform, video_id="v-unrelated-9x")]

    _patch_refresh_infra(monkeypatch)
    _use_refresh_stub(search_refresh_client, _MismatchedStub())
    headers, uid = account(search_refresh_client, "refresh_mis")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO viral_videos (platform, video_id, title, author) "
            "VALUES ('douyin', 'v-mis-1', 'Original', 'Author')",
        )
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search_refresh',true,2) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=2"
        )
    response = search_refresh_client.post(
        "/api/viral/search/refresh",
        headers={**headers, "Idempotency-Key": "refresh-mis-1"},
        json={"platform": "douyin", "videoId": "v-mis-1"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_SEARCH_REFRESH_UPSTREAM_FAILED"
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT state FROM billing_operations WHERE user_id=%s "
                "AND service='viral_search_refresh'",
                (uid,),
            ).fetchone()[0]
            == "FAILED"
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_videos WHERE video_id='v-unrelated-9x'"
            ).fetchone()[0]
            == 0
        )


def test_reconcile_releases_stale_refresh_reservations(client, route_state) -> None:
    """崩溃残留的 viral_search_refresh PENDING 预留必须被 30 分钟窗口释放（H-3）。"""
    from app.db_portable import BusinessConnection
    from app.usage_billing import accept_operation, reconcile_operations

    headers, uid = account(client, "refresh_rec")
    del headers
    source_id = f"viral-refresh:{uid}:crash-1"
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search_refresh',true,2) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=2"
        )
        conn = BusinessConnection.postgres(raw)
        accept_operation(
            conn,
            user_id=uid,
            service="viral_search_refresh",
            source_id=source_id,
            units=1,
        )
        # 把预留行推到 30 分钟窗口之外，模拟「预留已提交、交付事务未发生」的崩溃
        # 现场。billing_operations 有不可变事实触发器，沿 dwire 套件先例仅在本
        # 会话放行 UPDATE，不改共享库触发器定义。
        raw.execute("SET session_replication_role = replica")
        raw.execute(
            "UPDATE billing_operations SET created_at = now() - interval '31 minutes' "
            "WHERE service='viral_search_refresh' AND source_id=%s AND user_id=%s",
            (source_id, uid),
        )
        raw.execute("SET session_replication_role = DEFAULT")
        assert reconcile_operations(conn) >= 1
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (50, 0)
        assert (
            raw.execute(
                "SELECT state FROM billing_operations WHERE service='viral_search_refresh' "
                "AND source_id=%s AND user_id=%s",
                (source_id, uid),
            ).fetchone()[0]
            == "FAILED"
        )


def test_search_unpriced_fails_closed(search_client, route_state) -> None:
    """资费缺失或停用时搜索必须 503 拒绝，不得 0 积分免费外呼（H-4）。"""
    _use_search_stub(search_client, _SearchStub())
    headers, uid = account(search_client, "search_unpriced")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute("DELETE FROM billing_tariffs WHERE service='viral_search'")
    missing = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-unpriced-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert missing.status_code == 503
    assert missing.json()["detail"]["code"] == "VIRAL_SEARCH_UNPRICED"
    # 三种原因的处置完全不同（去配置 / 去启用 / 去改单价），文案必须分得开：
    # 笼统报「未配置」会让已经配过的人反复检查同一个地方。
    assert "尚未配置" in missing.json()["detail"]["message"]
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',false,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=false, unit_credits=3"
        )
    disabled = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-unpriced-2"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert disabled.status_code == 503
    assert disabled.json()["detail"]["code"] == "VIRAL_SEARCH_UNPRICED"
    assert "已停用" in disabled.json()["detail"]["message"]
    with psycopg.connect(route_state) as raw:
        # 评审 M-1：管理端 tariff 路径允许写入 enabled=true + unit_credits=0，
        # calculate_credits 对 0 价按免费放行——守卫必须把 0 价与缺失同罪。
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,0) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=0"
        )
    zero_priced = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-unpriced-3"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert zero_priced.status_code == 503
    assert zero_priced.json()["detail"]["code"] == "VIRAL_SEARCH_UNPRICED"
    # 这条最容易被误判成「我明明配了」：界面填 0 也算配过。文案必须点破是单价问题。
    assert "单价为 0" in zero_priced.json()["detail"]["message"]
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations "
                "WHERE user_id=%s AND service='viral_search'",
                (uid,),
            ).fetchone()[0]
            == 0
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (50, 0)


def test_refresh_unpriced_fails_closed(search_refresh_client, route_state) -> None:
    """资费缺失时刷新同样 fail-closed：不留「免费外呼供应商」的口子（H-4）。"""
    _use_refresh_stub(search_refresh_client, _RefreshStub())
    headers, uid = account(search_refresh_client, "refresh_unpriced")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO viral_videos (platform, video_id, title, author) "
            "VALUES ('douyin', 'v-unpriced', 'T', 'A')",
        )
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute("DELETE FROM billing_tariffs WHERE service='viral_search_refresh'")
    response = search_refresh_client.post(
        "/api/viral/search/refresh",
        headers={**headers, "Idempotency-Key": "refresh-unpriced-1"},
        json={"platform": "douyin", "videoId": "v-unpriced"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_SEARCH_REFRESH_UNPRICED"
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations "
                "WHERE user_id=%s AND service='viral_search_refresh'",
                (uid,),
            ).fetchone()[0]
            == 0
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (50, 0)


@pytest.fixture()
def search_refresh_client(client):
    from app.viral_search_refresh_routes import router as viral_search_refresh_router

    client.app.include_router(viral_search_refresh_router)
    yield client
    client.app.dependency_overrides.clear()


def _use_refresh_stub(refresh_client, stub) -> None:
    from app.viral_search_refresh_routes import get_viral_search_refresh_source_client

    refresh_client.app.dependency_overrides[get_viral_search_refresh_source_client] = lambda: stub


def _patch_refresh_infra(monkeypatch) -> None:
    from app import viral_media

    def fake_iter_fetch(self, url):
        yield b"\xff\xd8\xff\xe0fakejpeg"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", fake_iter_fetch)
    monkeypatch.setattr(
        "app.viral_search_refresh_routes.get_media_storage", lambda conn: _FakeCoverStorage()
    )


# Task 9: 文案与发现读接口（GET /api/viral/search/copy + GET /api/viral/search/discoveries）


def test_copy_returns_cached_transcript_and_null_on_miss(search_client, route_state) -> None:
    """GET /search/copy：命中返回 text+updatedAt（只读、免费），未命中双 null."""
    headers, _ = account(search_client, "copy_read")
    payload = json.dumps({"text": "已缓存文案", "duration_sec": 12.0}, ensure_ascii=False)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO viral_script_cache (platform, video_id, result_json) "
            "VALUES ('douyin', 'v-copy-1', %s) ON CONFLICT (platform, video_id) "
            "DO UPDATE SET result_json=excluded.result_json",
            (payload,),
        )
    hit = search_client.get(
        "/api/viral/search/copy",
        headers=headers,
        params={"platform": "douyin", "videoId": "v-copy-1"},
    )
    assert hit.status_code == 200, hit.text
    assert hit.json()["text"] == "已缓存文案"
    assert hit.json()["updatedAt"]
    miss = search_client.get(
        "/api/viral/search/copy",
        headers=headers,
        params={"platform": "douyin", "videoId": "v-copy-absent"},
    )
    assert miss.status_code == 200
    assert miss.json() == {"text": None, "updatedAt": None}


def test_discoveries_list_today_and_validate_date(search_client, route_state) -> None:
    """GET /search/discoveries：默认当天（上海时区），左联内容池；坏日期 422."""
    from app.viral_search import search_date_shanghai
    from app.viral_store import upsert_viral_videos
    from app.viral_tikhub import PLATFORM_WECHAT, ViralVideo

    headers, uid = account(search_client, "discoveries_read")
    today = search_date_shanghai()
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_search_discoveries WHERE user_id=%s", (uid,))
        upsert_viral_videos(
            BusinessConnection.postgres(raw),
            [
                ViralVideo(
                    platform=PLATFORM_WECHAT,
                    video_id="doc-disc-1",
                    category="",
                    title="视频号样本",
                    author="作者",
                    author_avatar=None,
                    verified=False,
                    cover_url=None,
                    duration_ms=0,
                    likes=0,
                    comments=None,
                    shares=None,
                    collects=None,
                    published_at=None,
                    published_display=None,
                    like_display=None,
                    native={"export_id": "exp-1", "object_nonce_id": "nonce-1"},
                )
            ],
            commit=False,
        )
        raw.execute(
            "INSERT INTO viral_search_discoveries "
            "(id, user_id, keyword, platform, video_id, search_date, searched_at) VALUES "
            "('disc-1', %s, '农村建房', 'wechat_channels', 'doc-disc-1', %s, "
            "'2026-09-22T02:00:00+00:00'), "
            "('disc-2', %s, '农村建房', 'douyin', 'v-not-stored', %s, "
            "'2026-09-22T01:00:00+00:00')",
            (uid, today, uid, today),
        )
    response = search_client.get("/api/viral/search/discoveries", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["date"] == today
    assert body["total"] == 2
    assert [item["videoId"] for item in body["items"]] == ["doc-disc-1", "v-not-stored"]
    assert body["items"][0]["video"]["title"] == "视频号样本"
    assert body["items"][0]["video"]["hasCopy"] is False
    assert body["items"][1]["video"] is None
    empty = search_client.get(
        "/api/viral/search/discoveries", headers=headers, params={"date": "2000-01-01"}
    )
    assert empty.status_code == 200
    assert empty.json() == {"date": "2000-01-01", "total": 0, "items": []}
    bad = search_client.get(
        "/api/viral/search/discoveries", headers=headers, params={"date": "2026/09/22"}
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "VIRAL_SEARCH_DATE_INVALID"


# ---------------------------------------------------------------------------
# P2：源站直链下发（POST /api/viral/videos/source）
# 视频号必须**同批**下发 full_url 与 decode_key（密钥每次请求都会变），
# 且该端点与搜索一样受 fail-closed 资费守卫约束。
# ---------------------------------------------------------------------------

_SOURCE_SERVICE = "viral_search_refresh"


@pytest.fixture()
def source_client(client):
    """测试 app 是精简的，viral_routes 不在默认路由表里，必须显式挂上。"""
    from app.viral_routes import router as viral_routes_router

    client.app.include_router(viral_routes_router)
    yield client
    client.app.dependency_overrides.clear()


class _WechatDetailStub:
    """只暴露路由真正读取的两个字段，避免把上游 DTO 的全字段搬进测试。"""

    def __init__(self, full_url: str | None, decode_key: str | None) -> None:
        self.full_url = full_url
        self.decode_key = decode_key


class _SourceStub:
    """直链数据源桩：抖音回精确匹配的视频，视频号回带 decode_key 的详情。"""

    def __init__(
        self,
        *,
        play_url: str | None = "https://cdn.example/douyin.mp4",
        full_url: str | None = "https://cdn.example/wechat.mp4",
        decode_key: str | None = "1789473271",
    ) -> None:
        self.play_url = play_url
        self.full_url = full_url
        self.decode_key = decode_key

    def douyin_refresh(self, *, platform: str, video_id: str):
        from dataclasses import replace

        return [
            replace(
                _viral_seed(platform=platform, video_id=video_id),
                play_url=self.play_url,
            )
        ]

    def wechat_video_detail(self, *, export_id: str, object_nonce_id: str | None = None):
        return _WechatDetailStub(self.full_url, self.decode_key)


def _use_source_stub(http_client, stub) -> None:
    from app.viral_routes import get_viral_source_client

    http_client.app.dependency_overrides[get_viral_source_client] = lambda: stub


def _seed_source_video(raw, uid, *, platform: str, video_id: str, tariff: int | None = 2) -> None:
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
        "VALUES (%s, %s, 'T', 'A', %s)",
        (
            platform,
            video_id,
            json.dumps({"export_id": "exp-1", "object_nonce_id": "nonce-1"}),
        ),
    )
    raw.execute(
        "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
    )
    raw.execute("DELETE FROM billing_tariffs WHERE service=%s", (_SOURCE_SERVICE,))
    if tariff is not None:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES(%s,true,%s)",
            (_SOURCE_SERVICE, tariff),
        )


def test_source_requires_valid_idempotency_key(source_client) -> None:
    """缺 Idempotency-Key → 422 前缀化错误码，且不预留计费。"""
    headers, _ = account(source_client, "source_key")
    response = source_client.post(
        "/api/viral/videos/source",
        headers=headers,
        json={"platform": "douyin", "videoId": "v-source-1"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VIRAL_SOURCE_IDEMPOTENCY_KEY_REQUIRED"


def test_source_wechat_returns_decode_key_and_charges_once(source_client, route_state) -> None:
    """视频号直链与 decode_key 同批返回，并按 viral_search_refresh 扣一次费。"""
    _use_source_stub(source_client, _SourceStub())
    headers, uid = account(source_client, "source_wechat")
    with psycopg.connect(route_state) as raw:
        _seed_source_video(raw, uid, platform="wechat_channels", video_id="v-source-wc")
    response = source_client.post(
        "/api/viral/videos/source",
        headers={**headers, "Idempotency-Key": "source-wc-1"},
        json={"platform": "wechat_channels", "videoId": "v-source-wc"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fullUrl"] == "https://cdn.example/wechat.mp4"
    # 密钥必须和直链一起来，否则客户端无法在本地解密（决策 #17）。
    assert body["decodeKey"] == "1789473271"
    assert body["billing"] == {"charged": 2, "unit": "call"}
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (48, 0)
        assert (
            raw.execute(
                "SELECT state FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _SOURCE_SERVICE),
            ).fetchone()[0]
            == "SUCCEEDED"
        )


def test_source_douyin_returns_play_url_without_decode_key(source_client, route_state) -> None:
    """抖音不需要密钥：decodeKey 必须为 None，而不是给个空串让客户端误以为要解密。"""
    _use_source_stub(source_client, _SourceStub())
    headers, uid = account(source_client, "source_douyin")
    with psycopg.connect(route_state) as raw:
        _seed_source_video(raw, uid, platform="douyin", video_id="v-source-dy")
    response = source_client.post(
        "/api/viral/videos/source",
        headers={**headers, "Idempotency-Key": "source-dy-1"},
        json={"platform": "douyin", "videoId": "v-source-dy"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["fullUrl"] == "https://cdn.example/douyin.mp4"
    assert response.json()["decodeKey"] is None


def test_source_releases_reservation_when_upstream_omits_decode_key(
    source_client, route_state
) -> None:
    """上游没给密钥 → 503 并释放预留，绝不「扣了费却没交付可用直链」。"""
    _use_source_stub(source_client, _SourceStub(decode_key=None))
    headers, uid = account(source_client, "source_nodecode")
    with psycopg.connect(route_state) as raw:
        _seed_source_video(raw, uid, platform="wechat_channels", video_id="v-source-nokey")
    response = source_client.post(
        "/api/viral/videos/source",
        headers={**headers, "Idempotency-Key": "source-nokey-1"},
        json={"platform": "wechat_channels", "videoId": "v-source-nokey"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_SOURCE_UPSTREAM_FAILED"
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (50, 0)
        assert (
            raw.execute(
                "SELECT state FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _SOURCE_SERVICE),
            ).fetchone()[0]
            == "FAILED"
        )


def test_source_rejects_unpriced_service_before_reserving(source_client, route_state) -> None:
    """资费未配置 → 503 且不预留：真实上游成本不能免费放行（H-4 fail-closed）。"""
    _use_source_stub(source_client, _SourceStub())
    headers, uid = account(source_client, "source_unpriced")
    with psycopg.connect(route_state) as raw:
        _seed_source_video(raw, uid, platform="douyin", video_id="v-source-np", tariff=None)
    response = source_client.post(
        "/api/viral/videos/source",
        headers={**headers, "Idempotency-Key": "source-np-1"},
        json={"platform": "douyin", "videoId": "v-source-np"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_SOURCE_UNPRICED"
    assert "尚未配置" in response.json()["detail"]["message"]
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _SOURCE_SERVICE),
            ).fetchone()[0]
            == 0
        )
