"""爆款文案交付门禁：任务接口不下发正文，正文只按 claim 回执交付.

TEST-PG：复用 test_customer_pricing 的夹具链（module 级建库 → alembic head →
每用例清理）。设计 §5.4-1：转写完成后正文躺在 ``script_from_audio_tasks``
里，但两个任务读取接口对爆款任务一律脱敏——交付与计费的唯一入口是
``POST /api/viral/videos/copy/claim``（首取扣费、复看免费），否则读任务
就等于绕开「获取文案」费的免费旁路。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

import json

import psycopg
import pytest
from test_customer_pricing import (  # noqa: F401
    account,
    client,
    registration_client,
    registration_dsn,
    route_state,
)

_COPY_SERVICE = "viral_copy"
_COPY_TEXT = "任务接口里必须被门禁住的一段共享文案"


@pytest.fixture()
def gate_client(client):
    """任务路由与「获取文案」路由都不在精简 app 的默认路由表里，显式挂上."""
    from app.script_from_audio_routes import router as script_router
    from app.viral_routes import router as viral_router

    client.app.include_router(viral_router)
    client.app.include_router(script_router)
    yield client
    client.app.dependency_overrides.clear()


def _seed_viral_copy_video(raw, uid: str, *, video_id: str) -> None:
    """一条可获取文案的视频：内容池有行 + 账号有钱 + 共享缓存里有结果."""
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
        "VALUES ('douyin', %s, 'T', 'A', '{}') ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (video_id,),
    )
    raw.execute(
        "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
    )
    raw.execute("DELETE FROM billing_tariffs WHERE service=%s", (_COPY_SERVICE,))
    raw.execute(
        "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES(%s,true,2)",
        (_COPY_SERVICE,),
    )
    raw.execute(
        "INSERT INTO viral_script_cache (platform, video_id, result_json) "
        "VALUES ('douyin', %s, %s) ON CONFLICT (platform, video_id) DO UPDATE "
        "SET result_json=excluded.result_json",
        (video_id, json.dumps({"text": _COPY_TEXT, "duration_sec": 12.0}, ensure_ascii=False)),
    )


def _seed_task(
    raw,
    uid: str,
    *,
    task_id: str,
    project_id: str,
    video_id: str | None,
) -> None:
    """直接落一条 SUCCEEDED 任务行：转写结果在任务里，正文是否下发由接口门禁决定."""
    raw.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '文案门禁项目')",
        (project_id, uid),
    )
    request_payload: dict[str, object] = {"source_asset_id": "asset-1"}
    if video_id is not None:
        request_payload["viral_source"] = ["douyin", video_id]
    raw.execute(
        "INSERT INTO script_from_audio_tasks ("
        "id, project_id, source_asset_id, created_by_user_id, idempotency_key, "
        "request_hash, request_json, status, result_json, completed_at, "
        "created_at, updated_at) VALUES ("
        "%s, %s, 'asset-1', %s, 'idem-1', 'hash-1', %s, 'SUCCEEDED', %s, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            task_id,
            project_id,
            uid,
            json.dumps(request_payload),
            json.dumps(
                {"text": _COPY_TEXT, "duration_sec": 12.5, "language": "zh"},
                ensure_ascii=False,
            ),
        ),
    )


def _wallet(raw, uid: str) -> tuple[int, int]:
    row = raw.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
    ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def test_enqueue_cache_hit_bills_asr_per_request_and_gates_response(
    gate_client, route_state
) -> None:
    """命中共享缓存的入队直接完成：转写费按次照收（复用结果省的是平台成本，不是
    用户手里的内容价值，test_cw030 已拍板），响应仍不带正文——交付与计费走 claim 回执.
    """
    headers, uid = account(gate_client, "gate_enqueue")
    project_id = "proj-gate-enqueue"
    asset_id = "asset-gate-enqueue"
    video_id = "v-gate-enqueue"
    with psycopg.connect(route_state) as raw:
        _seed_viral_copy_video(raw, uid, video_id=video_id)
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES('asr',true,2)"
        )
        raw.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '文案门禁项目')",
            (project_id, uid),
        )
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, %s, 'reference_video', %s, %s, "
            "1024, 'video/mp4', %s)",
            (asset_id, project_id, f"cos://bucket/{asset_id}.mp4", "a" * 64, uid),
        )
        raw.execute(
            "INSERT INTO viral_import_tasks (id, owner_user_id, project_id, source_asset_id, "
            "platform, video_id, purpose, idempotency_key, request_hash, request_json, status) "
            "VALUES ('imp-gate-enqueue', %s, %s, %s, 'douyin', %s, 'copy', 'idem-imp', "
            "'hash-imp', '{}', 'SUCCEEDED')",
            (uid, project_id, asset_id, video_id),
        )
        raw.commit()

    created = gate_client.post(
        f"/api/projects/{project_id}/script-from-audio",
        headers=headers,
        json={"source_asset_id": asset_id, "idempotency_key": "idem-gate-enqueue"},
    )
    assert created.status_code == 202, created.text
    body = created.json()
    assert body["status"] == "SUCCEEDED"
    assert body["copy_claim_required"] is True
    assert body["viral_source"] == {"platform": "douyin", "video_id": video_id}
    assert body["result"]["text"] is None

    with psycopg.connect(route_state) as raw:
        operation = raw.execute(
            "SELECT state, charged_credits, actual_units FROM billing_operations "
            "WHERE service='asr' AND source_id=%s",
            (body["id"],),
        ).fetchone()
        assert operation is not None
        assert str(operation[0]) == "SUCCEEDED"
        # 缓存时长 12.0s × 2 积分/秒：按次计费口径（test_cw030 已拍板）。
        assert int(operation[1]) == 24
        assert float(operation[2]) == 12.0
        assert _wallet(raw, uid) == (26, 0)


def test_viral_task_hides_text_until_copy_claimed(gate_client, route_state) -> None:
    """爆款任务：两个任务接口都不给正文，claim 是唯一交付口；GET 本身零计费."""
    headers, uid = account(gate_client, "gate_viral")
    task_id = "task-gate-viral"
    project_id = "proj-gate-viral"
    video_id = "v-gate-viral"
    with psycopg.connect(route_state) as raw:
        _seed_viral_copy_video(raw, uid, video_id=video_id)
        _seed_task(raw, uid, task_id=task_id, project_id=project_id, video_id=video_id)
        raw.commit()

    body = gate_client.get(f"/api/script-from-audio-tasks/{task_id}", headers=headers).json()
    assert body["status"] == "SUCCEEDED"
    assert body["copy_claim_required"] is True
    assert body["viral_source"] == {"platform": "douyin", "video_id": video_id}
    assert body["result"] is not None
    # 正文不下发，但时长/语言等非正文元数据照常回填，客户端等待态不受影响。
    assert body["result"]["text"] is None
    assert body["result"]["duration_sec"] == 12.5

    latest = gate_client.get(
        f"/api/projects/{project_id}/script-from-audio-tasks/latest", headers=headers
    ).json()
    assert latest["id"] == task_id
    assert latest["copy_claim_required"] is True
    assert latest["result"]["text"] is None

    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_content_usage_events WHERE video_id=%s AND kind='copy'",
                (video_id,),
            ).fetchone()[0]
            == 0
        )

    claim = gate_client.post(
        "/api/viral/videos/copy/claim",
        headers=headers,
        json={"platform": "douyin", "videoId": video_id},
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["text"] == _COPY_TEXT
    assert claim.json()["billing"]["charged"] == 2
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_content_usage_events WHERE video_id=%s AND kind='copy'",
                (video_id,),
            ).fetchone()[0]
            == 1
        )

    # 已购复看：任务接口依旧不下发正文（口径统一），claim 免费重放.
    replay = gate_client.post(
        "/api/viral/videos/copy/claim",
        headers=headers,
        json={"platform": "douyin", "videoId": video_id},
    )
    assert replay.json()["billing"]["deduped"] is True
    assert replay.json()["text"] == _COPY_TEXT
    again = gate_client.get(f"/api/script-from-audio-tasks/{task_id}", headers=headers).json()
    assert again["result"]["text"] is None
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_content_usage_events WHERE video_id=%s AND kind='copy'",
                (video_id,),
            ).fetchone()[0]
            == 2
        )


def test_homepage_http_returns_server_clock_and_next_window_boundary(gate_client, route_state):
    from datetime import datetime

    headers, uid = account(gate_client, "gate_home_clock")
    with psycopg.connect(route_state) as raw:
        _seed_viral_copy_video(raw, uid, video_id="clock-video")
        raw.execute(
            "UPDATE viral_videos SET homepage_featured=1,"
            "homepage_ends_at=date_trunc('second',now()+interval '1 day')"
            "+interval '0.123450 seconds' "
            "WHERE video_id='clock-video'"
        )
        raw.execute(
            "INSERT INTO viral_media_preparations"
            " (id,platform,video_id,media_kind,status,storage_uri) "
            "VALUES('clock-media','douyin','clock-video','video','SUCCEEDED','fake://clock.mp4')"
        )
    reply = gate_client.get("/api/viral/videos?featured_only=true", headers=headers)
    assert reply.status_code == 200, reply.text
    result = reply.json()
    assert len(result["items"]) == 1
    # SQL JSON removes trailing fractional zeros; ISO timestamps permit either
    # precision. Compare timezone-aware instants, including the persisted value.
    item_end = datetime.fromisoformat(result["items"][0]["homepageEndsAt"])
    server_time = datetime.fromisoformat(result["serverTime"])
    boundary = datetime.fromisoformat(result["nextChangeAt"])
    assert "T" in result["nextChangeAt"] and "T" in result["serverTime"]
    assert item_end == boundary
    assert server_time.utcoffset() is not None and boundary.utcoffset() is not None
    with psycopg.connect(route_state) as raw:
        persisted_end, database_now = raw.execute(
            "SELECT homepage_ends_at,clock_timestamp() FROM viral_videos "
            "WHERE video_id='clock-video'"
        ).fetchone()
    assert boundary == persisted_end
    assert abs((server_time - database_now).total_seconds()) < 10
    assert server_time < boundary


def test_plain_task_still_returns_text(gate_client, route_state) -> None:
    """普通上传任务不属于爆款零售链路：正文照常随任务接口下发."""
    headers, uid = account(gate_client, "gate_plain")
    task_id = "task-gate-plain"
    project_id = "proj-gate-plain"
    with psycopg.connect(route_state) as raw:
        _seed_task(raw, uid, task_id=task_id, project_id=project_id, video_id=None)
        raw.commit()

    body = gate_client.get(f"/api/script-from-audio-tasks/{task_id}", headers=headers).json()
    assert body["status"] == "SUCCEEDED"
    assert body["copy_claim_required"] is False
    assert body["viral_source"] is None
    assert body["result"]["text"] == _COPY_TEXT

    latest = gate_client.get(
        f"/api/projects/{project_id}/script-from-audio-tasks/latest", headers=headers
    ).json()
    assert latest["result"]["text"] == _COPY_TEXT


def test_viral_task_text_is_never_leaked_by_latest_for_other_project(
    gate_client, route_state
) -> None:
    """latest 按项目隔离：别的项目查不到这条任务，也不会顺带漏出正文."""
    headers, uid = account(gate_client, "gate_latest")
    task_id = "task-gate-latest"
    project_id = "proj-gate-latest"
    other_project = "proj-gate-other"
    with psycopg.connect(route_state) as raw:
        _seed_task(raw, uid, task_id=task_id, project_id=project_id, video_id="v-gate-latest")
        raw.commit()

    assert (
        gate_client.get(
            f"/api/projects/{other_project}/script-from-audio-tasks/latest",
            headers=headers,
        ).status_code
        == 404
    )
