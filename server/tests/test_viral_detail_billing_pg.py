"""爆款视频「查看详情」计费：独立科目 + 按付费账号去重台账.

TEST-PG：复用 test_customer_pricing 的夹具链（cw076_registration_test 专属库，
module 级建库 → alembic head → 每用例 TRUNCATE wallets/users）。去重台账复用
``billing_operations`` 既有的唯一键与索引，因此本模块不覆盖新迁移。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

import json
from datetime import UTC, datetime

import psycopg
import pytest
from test_customer_pricing import (  # noqa: F401
    account,
    client,
    registration_client,
    registration_dsn,
    route_state,
)

_DETAIL_SERVICE = "viral_detail"
_COPY_SERVICE = "viral_copy"
_COPY_TEXT = "别人转写好的共享文案"


@pytest.fixture()
def detail_client(client):
    """测试 app 是精简的，viral_routes 不在默认路由表里，必须显式挂上。"""
    from app.viral_routes import router as viral_routes_router

    client.app.include_router(viral_routes_router)
    yield client
    client.app.dependency_overrides.clear()


@pytest.fixture()
def copy_client(detail_client):
    """文案状态读接口在 viral_search_routes 里，需要再挂一个路由。"""
    from app.viral_search_routes import router as viral_search_router

    detail_client.app.include_router(viral_search_router)
    yield detail_client


def _seed_detail_video(
    raw,
    uid: str,
    *,
    platform: str = "douyin",
    video_id: str,
    tariff: int | None = 2,
    visibility: str | None = None,
) -> None:
    raw.execute(
        "INSERT INTO viral_videos "
        "(platform, video_id, title, author, comments, shares, collects, native_json) "
        "VALUES (%s, %s, 'T', 'A', 3, 4, 5, %s) ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (platform, video_id, json.dumps({})),
    )
    raw.execute(
        "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
    )
    raw.execute("DELETE FROM billing_tariffs WHERE service=%s", (_DETAIL_SERVICE,))
    if tariff is not None:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES(%s,true,%s)",
            (_DETAIL_SERVICE, tariff),
        )
    if visibility is not None:
        raw.execute(
            "INSERT INTO viral_video_visibility (platform, video_id, status, reason) "
            "VALUES (%s, %s, %s, 'detail-test') ON CONFLICT (platform, video_id) "
            "DO UPDATE SET status = excluded.status",
            (platform, video_id, visibility),
        )
    raw.commit()


def _open_detail(http_client, headers, *, platform: str = "douyin", video_id: str):
    return http_client.post(
        "/api/viral/videos/detail",
        headers=headers,
        json={"platform": platform, "videoId": video_id},
    )


def _wallet(raw, uid: str) -> tuple[int, int]:
    row = raw.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
    ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def _detail_operations(raw, source_id: str) -> list[tuple[str, int]]:
    rows = raw.execute(
        "SELECT state, charged_credits FROM billing_operations "
        "WHERE service=%s AND source_id=%s ORDER BY billing_round",
        (_DETAIL_SERVICE, source_id),
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]


def test_viral_detail_service_registered_in_billing_catalog() -> None:
    """viral_detail 科目：call/platform/viral，可客户收费、复用 viral_extract 折扣接口."""
    from app.billing_catalog import (
        INTERFACE_KEYS,
        SERVICE_INTERFACE,
        SERVICES,
        interface_for_service,
    )

    service = SERVICES["viral_detail"]
    assert service.name == "爆款视频详情"
    assert service.unit == "call"
    # 详情读的是已归档内容，没有按次供应商成本，provider 记平台本身。
    assert service.provider == "platform"
    assert service.module == "viral"
    assert service.customer_charge_allowed is True
    assert interface_for_service("viral_detail") == "viral_extract"
    assert SERVICE_INTERFACE["viral_detail"] in INTERFACE_KEYS


def test_detail_charges_once_then_replays_free(detail_client, route_state) -> None:
    """首次查看扣一次费（详情与统计字段同批返回），再次查看复用购买不再扣费."""
    headers, uid = account(detail_client, "detail_first")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-first")
    prepared = detail_client.post(
        "/api/viral/videos/detail",
        headers=headers,
        json={"platform": "douyin", "videoId": "v-detail-first"},
    )
    assert prepared.status_code == 200, prepared.text
    body = prepared.json()
    assert body["billing"] == {
        "charged": 2,
        "unit": "call",
        "deduped": False,
        "billable": True,
    }
    assert body["item"]["detailCharged"] is True
    # 交互统计字段与详情在同一个响应里下发，所以只算一次「查看详情」。
    assert body["item"]["comments"] == 3
    assert body["item"]["shares"] == 4
    assert body["item"]["collects"] == 5
    replay = detail_client.post(
        "/api/viral/videos/detail",
        headers=headers,
        json={"platform": "douyin", "videoId": "v-detail-first"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["billing"] == {
        "charged": 0,
        "unit": "call",
        "deduped": True,
        "billable": True,
    }
    assert replay.json()["item"]["detailCharged"] is True
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (48, 0)
        assert _detail_operations(raw, f"viral-detail:{uid}:douyin:v-detail-first") == [
            ("SUCCEEDED", 2)
        ]


def test_detail_is_fail_closed_without_tariff(detail_client, route_state) -> None:
    """资费未配置 → 503 且不扣费、台账不落行（与搜索/直链同一 fail-closed 口径）."""
    headers, uid = account(detail_client, "detail_unpriced")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-unpriced", tariff=None)
    response = _open_detail(detail_client, headers, video_id="v-detail-unpriced")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_DETAIL_UNPRICED"
    assert "尚未配置" in response.json()["detail"]["message"]
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _DETAIL_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_detail_unavailable_content_is_free(detail_client, route_state) -> None:
    """已下架内容不计费：客户端据 billable=false 与「已购买」区分展示."""
    headers, uid = account(detail_client, "detail_unavailable")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-gone", visibility="UNAVAILABLE")
    response = _open_detail(detail_client, headers, video_id="v-detail-gone")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["billing"] == {
        "charged": 0,
        "unit": "call",
        "deduped": False,
        "billable": False,
    }
    assert body["item"]["availability"] == "unavailable"
    assert body["item"]["detailCharged"] is False
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _DETAIL_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_detail_rejects_unknown_video_and_platform_without_charge(
    detail_client, route_state
) -> None:
    """读不到内容的请求不产生费用：缺视频 404、平台非法 400，钱包与台账都不动."""
    headers, uid = account(detail_client, "detail_missing")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-known")
    missing = _open_detail(detail_client, headers, video_id="v-detail-absent")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "VIRAL_VIDEO_NOT_FOUND"
    invalid = _open_detail(detail_client, headers, platform="kuaishou", video_id="v-detail-known")
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "VIRAL_PLATFORM_INVALID"
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _DETAIL_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_detail_purchase_is_shared_across_sub_account(detail_client, route_state) -> None:
    """去重键是付费账号而不是操作者：子账号买过，母账号再看不再扣费."""
    master_headers, master_id = account(detail_client, "detail_master")
    child_headers, child_id = account(detail_client, "detail_child")
    source_id = f"viral-detail:{master_id}:douyin:v-detail-shared"
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE users SET account_type='SUB', parent_user_id=%s WHERE id=%s",
            (master_id, child_id),
        )
        raw.execute(
            "UPDATE wallets SET available_credits=0, reserved_credits=0 WHERE user_id=%s",
            (child_id,),
        )
        _seed_detail_video(raw, master_id, video_id="v-detail-shared")
    child_view = _open_detail(detail_client, child_headers, video_id="v-detail-shared")
    assert child_view.status_code == 200, child_view.text
    assert child_view.json()["billing"]["charged"] == 2
    master_view = _open_detail(detail_client, master_headers, video_id="v-detail-shared")
    assert master_view.status_code == 200, master_view.text
    assert master_view.json()["billing"] == {
        "charged": 0,
        "unit": "call",
        "deduped": True,
        "billable": True,
    }
    with psycopg.connect(route_state) as raw:
        # 扣的是母账号钱包；子账号自己的余额保持为 0。
        assert _wallet(raw, master_id) == (48, 0)
        assert _wallet(raw, child_id) == (0, 0)
        assert _detail_operations(raw, source_id) == [("SUCCEEDED", 2)]


def test_detail_auditor_never_starts_a_charge(detail_client, route_state) -> None:
    """非客户角色不得产生新的客户扣费.

    客户会话闸门在进入业务路由前就拒掉 ``role != 'customer'`` 的会话（401），
    端点内的 ``require_not_auditor`` 是更深一层的兜底（与其余计费端点同一口径，
    API-Key 轨把角色钉死为 customer，因此该兜底当前不可达）。这里断言可观察的
    结果：换角色后既拿不到详情，也不会留下任何计费行。
    """
    headers, uid = account(detail_client, "detail_audit")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-audited")
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE users SET role='auditor' WHERE id=%s", (uid,))
    response = _open_detail(detail_client, headers, video_id="v-detail-audited")
    assert response.status_code == 401
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _DETAIL_SERVICE),
            ).fetchone()[0]
            == 0
        )


def _seed_featured_video(raw, *, video_id: str) -> None:
    """首页展示网格的完整可见条件：已归档封面 + 视频媒体就绪 + 已入集."""
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
        "VALUES ('douyin', %s, %s, 'A', '{}') ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (video_id, f"P1 {video_id}"),
    )
    raw.execute(
        "INSERT INTO viral_media_preparations "
        "(id, platform, video_id, media_kind, status, storage_uri) "
        "VALUES (%s, 'douyin', %s, 'video', 'SUCCEEDED', %s) "
        "ON CONFLICT (platform, video_id, media_kind) DO UPDATE SET "
        "status='SUCCEEDED', storage_uri=excluded.storage_uri",
        (f"mp-{video_id}", video_id, f"local://media/viral/{video_id}.mp4"),
    )
    raw.execute(
        "UPDATE viral_videos SET published_at=%s, collection_published=1, "
        "homepage_featured=1, cover_key='viral/covers/douyin/' || video_id "
        "WHERE platform='douyin' AND video_id=%s",
        (int(datetime.now(UTC).timestamp()), video_id),
    )


def test_list_and_free_detail_backfill_detail_charged(detail_client, route_state) -> None:
    """卡片状态回填：已购详情的视频在列表与免费详情里都带 detailCharged=true."""
    headers, uid = account(detail_client, "detail_backfill")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-bought")
        _seed_featured_video(raw, video_id="v-detail-bought")
        _seed_featured_video(raw, video_id="v-detail-fresh")
    prepared = _open_detail(detail_client, headers, video_id="v-detail-bought")
    assert prepared.status_code == 200, prepared.text
    listing = detail_client.get(
        "/api/viral/videos",
        headers=headers,
        params={"platform": "douyin", "sort": "hot", "limit": 50, "featured_only": "true"},
    )
    assert listing.status_code == 200, listing.text
    flags = {item["videoId"]: item["detailCharged"] for item in listing.json()["items"]}
    assert flags == {"v-detail-bought": True, "v-detail-fresh": False}
    free_detail = detail_client.get("/api/viral/videos/douyin/v-detail-bought", headers=headers)
    assert free_detail.status_code == 200, free_detail.text
    assert free_detail.json()["detailCharged"] is True
    # 免费读取口径不产生费用：只有首次「查看详情」那一行台账。
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (48, 0)
        assert _detail_operations(raw, f"viral-detail:{uid}:douyin:v-detail-bought") == [
            ("SUCCEEDED", 2)
        ]


def test_cache_reclaim_returns_server_side_truth(detail_client, route_state) -> None:
    """本地缓存回收判据由服务端回答：删除 / 下架 / 不再下发的回收，仍在发与收藏的保留.

    客户端手里只有翻过的页，拿分页结果当「还在下发」的依据会误删仍然有效的缓存，
    所以回收必须问服务端。该端点只读、不计费：回收发生在用户机器上，不该因此收费。
    """
    headers, uid = account(detail_client, "cache_reclaim")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="keep-live")
        _seed_featured_video(raw, video_id="keep-live")
        # 收藏不受首页窗口与归档状态影响：即使当前不在下发，也不能回收。
        _seed_detail_video(raw, uid, video_id="keep-favorite")
        _seed_featured_video(raw, video_id="drop-deleted")
        raw.execute(
            "UPDATE viral_videos SET deleted_at=CURRENT_TIMESTAMP "
            "WHERE platform='douyin' AND video_id='drop-deleted'"
        )
        _seed_featured_video(raw, video_id="drop-hidden")
        raw.execute(
            "INSERT INTO viral_video_visibility (platform, video_id, status) "
            "VALUES ('douyin', 'drop-hidden', 'HIDDEN')"
        )
    favorited = detail_client.put("/api/viral/favorites/douyin/keep-favorite", headers=headers)
    assert favorited.status_code == 200, favorited.text
    response = detail_client.post(
        "/api/viral/videos/cache-reclaim",
        headers=headers,
        json={
            "platform": "douyin",
            # 重复上报的条目按一次处理，客户端不必先去重。
            "videoIds": [
                "keep-live",
                "keep-favorite",
                "drop-deleted",
                "drop-hidden",
                "drop-unknown",
                "keep-live",
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "platform": "douyin",
        "reclaim": ["drop-deleted", "drop-hidden", "drop-unknown"],
    }
    invalid = detail_client.post(
        "/api/viral/videos/cache-reclaim",
        headers=headers,
        json={"platform": "kuaishou", "videoIds": ["keep-live"]},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "VIRAL_PLATFORM_INVALID"
    empty = detail_client.post(
        "/api/viral/videos/cache-reclaim",
        headers=headers,
        json={"platform": "douyin", "videoIds": []},
    )
    assert empty.status_code == 422
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            == 0
        )


# ---------------------------------------------------------------- 获取文案（viral_copy）


def _seed_copy_video(
    raw,
    uid: str,
    *,
    platform: str = "douyin",
    video_id: str,
    tariff: int | None = 2,
    visibility: str | None = None,
    cached: bool = True,
) -> None:
    """一条可获取文案的视频：内容池有行 + 账号有钱 + 共享缓存里有结果."""
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
        "VALUES (%s, %s, 'T', 'A', '{}') ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (platform, video_id),
    )
    raw.execute(
        "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
    )
    raw.execute("DELETE FROM billing_tariffs WHERE service=%s", (_COPY_SERVICE,))
    if tariff is not None:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES(%s,true,%s)",
            (_COPY_SERVICE, tariff),
        )
    if cached:
        raw.execute(
            "INSERT INTO viral_script_cache (platform, video_id, result_json) "
            "VALUES (%s, %s, %s) ON CONFLICT (platform, video_id) DO UPDATE "
            "SET result_json=excluded.result_json",
            (
                platform,
                video_id,
                json.dumps({"text": _COPY_TEXT, "duration_sec": 12.0}, ensure_ascii=False),
            ),
        )
    else:
        raw.execute(
            "DELETE FROM viral_script_cache WHERE platform=%s AND video_id=%s",
            (platform, video_id),
        )
    if visibility is not None:
        raw.execute(
            "INSERT INTO viral_video_visibility (platform, video_id, status, reason) "
            "VALUES (%s, %s, %s, 'copy-test') ON CONFLICT (platform, video_id) "
            "DO UPDATE SET status = excluded.status",
            (platform, video_id, visibility),
        )
    raw.commit()


def _claim_copy(http_client, headers, *, platform: str = "douyin", video_id: str):
    return http_client.post(
        "/api/viral/videos/copy/claim",
        headers=headers,
        json={"platform": platform, "videoId": video_id},
    )


def _copy_operations(raw, source_id: str) -> list[tuple[str, int]]:
    rows = raw.execute(
        "SELECT state, charged_credits FROM billing_operations "
        "WHERE service=%s AND source_id=%s ORDER BY billing_round",
        (_COPY_SERVICE, source_id),
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]


def test_viral_copy_service_registered_in_billing_catalog() -> None:
    """viral_copy 科目：call/platform/viral，可客户收费、复用 viral_extract 折扣接口."""
    from app.billing_catalog import (
        INTERFACE_KEYS,
        SERVICE_INTERFACE,
        SERVICES,
        interface_for_service,
    )

    service = SERVICES["viral_copy"]
    assert service.name == "爆款视频文案"
    assert service.unit == "call"
    # 文案在共享缓存里，复用别人的转写结果没有按次供应商成本，provider 记平台本身。
    assert service.provider == "platform"
    assert service.module == "viral"
    assert service.customer_charge_allowed is True
    assert interface_for_service("viral_copy") == "viral_extract"
    assert SERVICE_INTERFACE["viral_copy"] in INTERFACE_KEYS


def test_copy_claim_charges_once_then_replays_free(detail_client, route_state) -> None:
    """复用共享缓存同样付费：首次扣一次「获取文案」，再次获取只复用购买."""
    headers, uid = account(detail_client, "copy_first")
    with psycopg.connect(route_state) as raw:
        _seed_copy_video(raw, uid, video_id="v-copy-first")
    first = _claim_copy(detail_client, headers, video_id="v-copy-first")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["text"] == _COPY_TEXT
    assert body["updatedAt"]
    assert body["billing"] == {
        "charged": 2,
        "unit": "call",
        "deduped": False,
        "billable": True,
    }
    replay = _claim_copy(detail_client, headers, video_id="v-copy-first")
    assert replay.status_code == 200, replay.text
    # 秒回的是别人的转写结果，但正文本身仍是零售内容：第二次只复用购买，不再扣费。
    assert replay.json()["text"] == _COPY_TEXT
    assert replay.json()["billing"] == {
        "charged": 0,
        "unit": "call",
        "deduped": True,
        "billable": True,
    }
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (48, 0)
        assert _copy_operations(raw, f"viral-copy:{uid}:douyin:v-copy-first") == [("SUCCEEDED", 2)]


def test_copy_claim_miss_delivers_nothing_and_costs_nothing(detail_client, route_state) -> None:
    """未命中共享缓存：如实回 null 且分文不扣——没有交付就没有收费."""
    headers, uid = account(detail_client, "copy_miss")
    with psycopg.connect(route_state) as raw:
        _seed_copy_video(raw, uid, video_id="v-copy-miss", cached=False)
    response = _claim_copy(detail_client, headers, video_id="v-copy-miss")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "text": None,
        "updatedAt": None,
        "billing": {"charged": 0, "unit": "call", "deduped": False, "billable": True},
    }
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _COPY_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_copy_claim_is_fail_closed_without_tariff(detail_client, route_state) -> None:
    """资费未配置 → 503 且不交付、不扣费（与搜索/详情同一 fail-closed 口径）."""
    headers, uid = account(detail_client, "copy_unpriced")
    with psycopg.connect(route_state) as raw:
        _seed_copy_video(raw, uid, video_id="v-copy-unpriced", tariff=None)
    response = _claim_copy(detail_client, headers, video_id="v-copy-unpriced")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_COPY_UNPRICED"
    assert "尚未配置" in response.json()["detail"]["message"]
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _COPY_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_copy_claim_unavailable_content_is_free(detail_client, route_state) -> None:
    """已下架内容不计费，但缓存里的正文照给：内容早产出，此时再收钱不合理."""
    headers, uid = account(detail_client, "copy_gone")
    with psycopg.connect(route_state) as raw:
        _seed_copy_video(raw, uid, video_id="v-copy-gone", visibility="UNAVAILABLE")
    response = _claim_copy(detail_client, headers, video_id="v-copy-gone")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == _COPY_TEXT
    assert body["billing"] == {
        "charged": 0,
        "unit": "call",
        "deduped": False,
        "billable": False,
    }
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _COPY_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_copy_purchase_is_shared_across_sub_account(detail_client, route_state) -> None:
    """去重键是付费账号而不是操作者：子账号买过，母账号再取不再扣费."""
    master_headers, master_id = account(detail_client, "copy_master")
    child_headers, child_id = account(detail_client, "copy_child")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE users SET account_type='SUB', parent_user_id=%s WHERE id=%s",
            (master_id, child_id),
        )
        raw.execute(
            "UPDATE wallets SET available_credits=0, reserved_credits=0 WHERE user_id=%s",
            (child_id,),
        )
        _seed_copy_video(raw, master_id, video_id="v-copy-shared")
    child_claim = _claim_copy(detail_client, child_headers, video_id="v-copy-shared")
    assert child_claim.status_code == 200, child_claim.text
    assert child_claim.json()["billing"]["charged"] == 2
    master_claim = _claim_copy(detail_client, master_headers, video_id="v-copy-shared")
    assert master_claim.status_code == 200, master_claim.text
    assert master_claim.json()["billing"] == {
        "charged": 0,
        "unit": "call",
        "deduped": True,
        "billable": True,
    }
    with psycopg.connect(route_state) as raw:
        # 扣的是母账号钱包；子账号自己的余额保持为 0。
        assert _wallet(raw, master_id) == (48, 0)
        assert _wallet(raw, child_id) == (0, 0)
        assert _copy_operations(raw, f"viral-copy:{master_id}:douyin:v-copy-shared") == [
            ("SUCCEEDED", 2)
        ]


def test_copy_claim_rejects_unknown_video_and_platform_without_charge(
    detail_client, route_state
) -> None:
    """读不到内容的请求不产生费用：缺视频 404、平台非法 400，钱包与台账都不动."""
    headers, uid = account(detail_client, "copy_missing")
    with psycopg.connect(route_state) as raw:
        _seed_copy_video(raw, uid, video_id="v-copy-known")
    missing = _claim_copy(detail_client, headers, video_id="v-copy-absent")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "VIRAL_VIDEO_NOT_FOUND"
    invalid = _claim_copy(detail_client, headers, platform="kuaishou", video_id="v-copy-known")
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "VIRAL_PLATFORM_INVALID"
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _COPY_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_copy_status_never_leaks_text_and_reports_purchase(copy_client, route_state) -> None:
    """只读状态只回答「有没有 / 买没买过」：正文必须走计费端点，本端点不计费."""
    headers, uid = account(copy_client, "copy_status")
    with psycopg.connect(route_state) as raw:
        _seed_copy_video(raw, uid, video_id="v-copy-status")
    params = {"platform": "douyin", "videoId": "v-copy-status"}
    before = copy_client.get("/api/viral/search/copy", headers=headers, params=params)
    assert before.status_code == 200, before.text
    assert before.json()["available"] is True
    assert before.json()["updatedAt"]
    assert before.json()["purchased"] is False
    # 正文绝不由只读端点下发：拿到它就没有任何交付计费点了。
    assert "text" not in before.json()
    assert (
        _claim_copy(copy_client, headers, video_id="v-copy-status").json()["billing"]["charged"]
        == 2
    )
    after = copy_client.get("/api/viral/search/copy", headers=headers, params=params)
    assert after.json()["purchased"] is True
    miss = copy_client.get(
        "/api/viral/search/copy",
        headers=headers,
        params={"platform": "douyin", "videoId": "v-copy-unknown"},
    )
    assert miss.json() == {"available": False, "updatedAt": None, "purchased": False}
    with psycopg.connect(route_state) as raw:
        # 读状态分文不取：台账里只该有 claim 留下的那一行。
        assert _wallet(raw, uid) == (48, 0)
        assert _copy_operations(raw, f"viral-copy:{uid}:douyin:v-copy-status") == [("SUCCEEDED", 2)]
