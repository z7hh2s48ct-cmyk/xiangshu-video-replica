"""生成记录成片预览端点（方案 P2-2）的真实 PG 泳道套件。

钉住管理端「查看成片」端点与记录列表 `has_preview` 的边界：记录 → 产物资产的
解析（视频任务行 / 结果版本候选池 / 人物表 result_json）、`content` 的 Range 与
审计、审计员的内容访问拒绝、列表字段。缩略图不在本文件：它由 control_routes 的
签名直连地址端点提供，对应用例在 test_external_calls_pg.py。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-admin-gen-media-tests-minimum-48-bytes-long",
)

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.admin_auth_routes import AdminActor, get_admin_actor
from app.admin_generation_routes import router as admin_generation_router
from app.auth import CurrentUser
from app.control_auth import get_control_route_user
from app.control_routes import router as control_router
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.storage import STORAGE_ROOT_ENV

MEDIA_DB_NAME = "admin_gen_media_test"

# 清单覆盖本轮读写涉及的每张表；CASCADE 负责清掉引用它们的行。
_RESET_TABLES = (
    "audit_logs, external_call_logs, wallet_transactions, assets, versions, "
    "generation_tasks, generation_batches, first_frame_tasks, "
    "character_sheet_tasks, person_identities, projects, users"
)

_VIDEO_PAYLOAD = b"fake-mp4-bytes-for-range-tests"


def _pg_dsn() -> str:
    return os.environ.get(
        "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
    )


@pytest.fixture(scope="module")
def media_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(MEDIA_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(MEDIA_DB_NAME)


@pytest.fixture()
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """本地存储根：端点经 storage_for_asset 解析 ``local://`` URI 时读它。"""
    monkeypatch.setenv(STORAGE_ROOT_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture()
def seeded(media_dsn: str) -> str:
    """清库并重建最小依赖（管理员、客户、项目、人物、视频批次）。"""
    with psycopg.connect(media_dsn, autocommit=True) as conn:
        # append-only 审计触发器拒绝 TRUNCATE；隔离夹具库内按 P1-4 先例
        # 临时关闭触发器再清。
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {_RESET_TABLES} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('u-1', 'u-1', 'Owner', 'user'), "
            "('admin_u', 'admin_u', 'Admin', 'admin')"
        )
        conn.execute("INSERT INTO projects (id, name, owner_user_id) VALUES ('p-1', 'P', 'u-1')")
        # 人物表任务对人物身份有外键：最小依赖链。
        conn.execute(
            "INSERT INTO person_identities ("
            " id, owner_user_id, display_name, authorization_status,"
            " source_quality_status, status, created_by"
            ") VALUES ('ident-1', 'u-1', '首帧人物', 'AUTHORIZED', 'PASSED', 'ACTIVE', 'u-1')"
        )
        conn.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash,"
            " request_snapshot_json, status"
            ") VALUES ('b-1', 'p-1', 'u-1', 'ik-b1', 'rh-b1', '{}', 'QUEUED')"
        )
    return media_dsn


def _write_object(root: Path, key: str, content: bytes) -> None:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _insert_asset(media_dsn: str, asset_id: str, storage_uri: str, *, content_type: str) -> None:
    with psycopg.connect(media_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes, content_type"
            ") VALUES (%s, 'p-1', 'generated_video', %s, %s, 0, %s)",
            (asset_id, storage_uri, "a" * 64, content_type),
        )


def _insert_video_task(
    media_dsn: str, task_id: str, *, status: str, result_asset_id: str | None
) -> None:
    with psycopg.connect(media_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO generation_tasks ("
            " id, batch_id, generation_mode, provider, model, status, archive_status,"
            " quality_status, result_asset_id"
            ") VALUES (%s, 'b-1', 'I2V', 'metaso', 'MiniMax-H3', %s, 'DIRECT',"
            " 'PENDING', %s)",
            (task_id, status, result_asset_id),
        )


def _insert_first_frame_task(media_dsn: str, task_id: str, *, version_id: str) -> None:
    with psycopg.connect(media_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO versions ("
            " id, project_id, kind, version_number, payload_json, created_by_user_id"
            ") VALUES (%s, 'p-1', 'first_frame_candidates', 1, %s, 'u-1')",
            (version_id, '{"candidates": [{"asset_id": "a-ff"}]}'),
        )
        conn.execute(
            "INSERT INTO first_frame_tasks ("
            " id, created_by_user_id, project_id, idempotency_key, request_hash,"
            " request_json, status, result_version_id"
            ") VALUES (%s, 'u-1', 'p-1', %s, %s, '{}', 'SUCCEEDED', %s)",
            (task_id, f"ik-{task_id}", f"rh-{task_id}", version_id),
        )


def _insert_character_sheet_task(media_dsn: str, task_id: str, *, asset_id: str) -> None:
    with psycopg.connect(media_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO character_sheet_tasks ("
            " id, created_by_user_id, identity_id, idempotency_key, request_hash,"
            " request_json, operation, source_storage_uri, source_content_type,"
            " source_sha256, source_size_bytes, status, result_json"
            ") VALUES (%s, 'u-1', 'ident-1', %s, %s, '{}', 'CREATE',"
            " 'fake://uploads/source.png', 'image/png', %s, 9, 'SUCCEEDED', %s)",
            (
                task_id,
                f"ik-{task_id}",
                f"rh-{task_id}",
                "f" * 64,
                f'{{"contact_sheet_asset_id": "{asset_id}"}}',
            ),
        )


def _seed_video_media(media_dsn: str, root: Path) -> None:
    """一段可预览的视频记录：任务行 + 资产行 + 本地对象。"""
    _insert_asset(
        media_dsn,
        "a-media",
        "local://local-private/projects/p-1/gen/t-media.mp4",
        content_type="video/mp4",
    )
    _insert_video_task(media_dsn, "t-media", status="SUCCEEDED", result_asset_id="a-media")
    _write_object(root, "projects/p-1/gen/t-media.mp4", _VIDEO_PAYLOAD)


def _actor(role: str) -> AdminActor:
    return AdminActor(
        user_id="admin_u",
        username="admin_u",
        display_name="Admin",
        role=role,
        is_super_admin=False,
        auth_method="password",
        session_id="sess-1",
        session_expires_at="2030-01-01T00:00:00Z",
        last_activity_at="2030-01-01T00:00:00Z",
    )


def _current_user(role: str) -> CurrentUser:
    return CurrentUser(id="admin_u", username="admin_u", display_name="Admin", role=role)  # type: ignore[arg-type]


def _client(seeded: str, monkeypatch: pytest.MonkeyPatch, role: str) -> TestClient:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, seeded)
    app = FastAPI()
    app.include_router(admin_generation_router)
    app.include_router(control_router)
    app.dependency_overrides[get_admin_actor] = lambda: _actor(role)
    app.dependency_overrides[get_control_route_user] = lambda: _current_user(role)
    return TestClient(app)


@pytest.fixture()
def api(seeded: str, monkeypatch: pytest.MonkeyPatch, media_root: Path) -> Iterator[TestClient]:
    with _client(seeded, monkeypatch, "admin") as test_client:
        yield test_client
    close_pg_pool()


@pytest.fixture()
def api_auditor(
    seeded: str, monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> Iterator[TestClient]:
    with _client(seeded, monkeypatch, "auditor") as test_client:
        yield test_client
    close_pg_pool()


def _content_url(record_type: str, record_id: str) -> str:
    return f"/api/control/generation-records/{record_type}/{record_id}/content"


def _audit_count(media_dsn: str, entity_id: str) -> int:
    row = (
        psycopg.connect(media_dsn)
        .execute(
            "SELECT count(*) FROM audit_logs"
            " WHERE action = 'generation_record.content_view' AND entity_id = %s",
            (entity_id,),
        )
        .fetchone()
    )
    assert row is not None
    return int(row[0])


def test_content_streams_result_and_writes_audit(
    api: TestClient, seeded: str, media_root: Path
) -> None:
    _seed_video_media(seeded, media_root)

    response = api.get(_content_url("VIDEO", "t-media"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == _VIDEO_PAYLOAD
    assert response.headers["content-disposition"].startswith("inline")
    # 客户内容每次查看都留痕（与 external_call.response_view 同一口径）。
    assert _audit_count(seeded, "t-media") == 1


def test_content_supports_range_requests(api: TestClient, seeded: str, media_root: Path) -> None:
    _seed_video_media(seeded, media_root)

    response = api.get(_content_url("VIDEO", "t-media"), headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-3/{len(_VIDEO_PAYLOAD)}"
    assert response.content == _VIDEO_PAYLOAD[:4]


def _seed_direct_result(
    dsn: str, *, url: str = "https://cdn.example.test/result.mp4?signature=private"
) -> None:
    _insert_video_task(dsn, "t-direct", status="SUCCEEDED", result_asset_id=None)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE generation_tasks SET provider_result_url=%s WHERE id='t-direct'", (url,)
        )


def test_direct_success_no_asset_uses_safe_proxy_and_range(api, seeded, monkeypatch) -> None:
    from app.admin_generation_routes import UrlFetcher

    _seed_direct_result(seeded)
    payload = b"\x00\x00\x00\x18ftypisomsynthetic-video"
    urls = []
    monkeypatch.setattr(UrlFetcher, "fetch", lambda self, url: urls.append(url) or payload)
    page = api.get("/api/control/generation-records?record_type=VIDEO").json()
    assert page["items"][0]["has_preview"] is True
    assert "signature" not in str(page)
    result = api.get(_content_url("VIDEO", "t-direct"), headers={"Range": "bytes=4-7"})
    assert result.status_code == 206 and result.content == b"ftyp"
    assert result.headers["cache-control"] == "private, no-store"
    assert "signature" not in str(result.headers)
    assert urls == ["https://cdn.example.test/result.mp4?signature=private"]
    assert _audit_count(seeded, "t-direct") == 1
    with psycopg.connect(seeded) as conn:
        assert "signature" not in str(
            conn.execute(
                "SELECT metadata_json FROM audit_logs WHERE entity_id='t-direct'"
            ).fetchall()
        )
        assert (
            conn.execute(
                "SELECT result_asset_id FROM generation_tasks WHERE id='t-direct'"
            ).fetchone()[0]
            is None
        )


def test_expired_direct_result_is_localized_and_audited(api, seeded, monkeypatch) -> None:
    from app.admin_generation_routes import UrlFetcher, ViralMediaError

    _seed_direct_result(seeded)

    def expired(self, url):
        raise ViralMediaError("private signed URL expired")

    monkeypatch.setattr(UrlFetcher, "fetch", expired)
    result = api.get(_content_url("VIDEO", "t-direct"))
    assert result.status_code == 502
    assert result.json()["detail"]["code"] == "MEDIA_DIRECT_RESULT_UNAVAILABLE"
    assert "过期" in result.json()["detail"]["message"]
    assert "private" not in result.text and "signature" not in result.text
    assert _audit_count(seeded, "t-direct") == 1


def test_direct_result_refuses_private_host_before_connection(api, seeded, monkeypatch) -> None:
    import socket

    from app.viral_media import UrlFetcher

    _seed_direct_result(seeded, url="https://127.0.0.1/private")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    called = []
    monkeypatch.setattr(
        UrlFetcher, "_connection_factory", lambda *a: called.append(a), raising=False
    )
    result = api.get(_content_url("VIDEO", "t-direct"))
    assert result.status_code == 502 and called == []


def test_auditor_cannot_read_direct_result_thumbnail_or_cost(
    api_auditor, seeded, monkeypatch
) -> None:
    from app.admin_generation_routes import UrlFetcher

    _seed_direct_result(seeded)
    monkeypatch.setattr(UrlFetcher, "fetch", lambda *a: pytest.fail("auditor must not fetch media"))
    assert api_auditor.get(_content_url("VIDEO", "t-direct")).status_code == 403
    assert (
        api_auditor.get("/api/control/generation-records/VIDEO/t-direct/thumbnail").status_code
        == 403
    )
    item = api_auditor.get("/api/control/generation-records?record_type=VIDEO").json()["items"][0]
    assert item["has_preview"] is False and item["provider_cost"] is None
    assert item["provider_message"] is None and item["error_message"] is None
    assert _audit_count(seeded, "t-direct") == 0


def test_missing_record_and_missing_result_return_404(api: TestClient, seeded: str) -> None:
    missing = api.get(_content_url("VIDEO", "no-such-task"))
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "GENERATION_RECORD_NOT_FOUND"

    # 任务存在但没有归档产物（失败/历史记录）：与「记录不存在」区分。
    _insert_video_task(seeded, "t-plain", status="SUCCEEDED", result_asset_id=None)
    no_result = api.get(_content_url("VIDEO", "t-plain"))
    assert no_result.status_code == 404
    assert no_result.json()["detail"]["code"] == "MEDIA_RESULT_UNAVAILABLE"
    # 拒绝发生在写审计之前：不产生 content_view 留痕。
    assert _audit_count(seeded, "t-plain") == 0


def test_auditor_is_denied_media(api_auditor: TestClient, seeded: str, media_root: Path) -> None:
    """审计员能看记录列表，但不能看客户生成内容（原片拒绝）。"""
    _seed_video_media(seeded, media_root)

    content = api_auditor.get(_content_url("VIDEO", "t-media"))
    assert content.status_code == 403
    assert content.json()["detail"]["code"] == "GENERATION_RECORD_MEDIA_FORBIDDEN"
    assert _audit_count(seeded, "t-media") == 0


def test_first_frame_content_reads_candidate_pool(
    api: TestClient, seeded: str, media_root: Path
) -> None:
    _insert_asset(
        seeded,
        "a-ff",
        "local://local-private/projects/p-1/first-frames/a-ff.png",
        content_type="image/png",
    )
    _insert_first_frame_task(seeded, "ff-1", version_id="v-ff")
    _write_object(media_root, "projects/p-1/first-frames/a-ff.png", b"fake-png")

    response = api.get(_content_url("FIRST_FRAME_IMAGE", "ff-1"))
    assert response.status_code == 200
    assert response.content == b"fake-png"


def test_character_sheet_content_reads_result_json(
    api: TestClient, seeded: str, media_root: Path
) -> None:
    _insert_asset(
        seeded,
        "a-cs",
        "local://local-private/projects/p-1/characters/a-cs.png",
        content_type="image/png",
    )
    _insert_character_sheet_task(seeded, "cs-1", asset_id="a-cs")
    _write_object(media_root, "projects/p-1/characters/a-cs.png", b"fake-png")

    response = api.get(_content_url("CHARACTER_SHEET_IMAGE", "cs-1"))
    assert response.status_code == 200
    assert response.content == b"fake-png"


def test_unsupported_record_type_is_rejected(api: TestClient) -> None:
    # 拆解没有媒体产物：路径参数直接拒绝，不给「碰巧 404」的模糊结果。
    response = api.get(_content_url("ANALYSIS", "an-1"))
    assert response.status_code == 422


def test_generation_records_list_exposes_has_preview(
    api: TestClient, seeded: str, media_root: Path
) -> None:
    _seed_video_media(seeded, media_root)
    _insert_video_task(seeded, "t-plain", status="SUCCEEDED", result_asset_id=None)

    response = api.get(
        "/api/control/generation-records", params={"record_type": "VIDEO", "limit": 50}
    )
    assert response.status_code == 200
    items = {item["record_id"]: item for item in response.json()["items"]}
    assert items["t-media"]["has_preview"] is True
    assert items["t-plain"]["has_preview"] is False


@pytest.mark.parametrize(
    "kind,status",
    [
        ("VIDEO", "FAILED"),
        ("VIDEO", "SUBMISSION_UNCERTAIN"),
        ("FIRST_FRAME_IMAGE", "FAILED"),
        ("FIRST_FRAME_IMAGE", "SUBMISSION_UNCERTAIN"),
        ("CHARACTER_SHEET_IMAGE", "FAILED"),
        ("CHARACTER_SHEET_IMAGE", "SUBMISSION_UNCERTAIN"),
        ("ORAL_VIDEO", "FAILED"),
        ("ORAL_VIDEO", "SUBMISSION_UNCERTAIN"),
        ("ORAL_VIDEO", "ARCHIVE_FAILED"),
        ("ORAL_VIDEO", "CANCELLED"),
    ],
)
def test_terminal_response_persistence_to_business_and_technical_display(api, seeded, kind, status):
    import json

    from app.external_calls import external_call_context, record_external_call

    task_id = "matrix-task"
    if kind == "VIDEO":
        _insert_video_task(seeded, task_id, status=status, result_asset_id=None)
        table = "generation_tasks"
    elif kind == "FIRST_FRAME_IMAGE":
        _insert_first_frame_task(seeded, task_id, version_id="matrix-version")
        table = "first_frame_tasks"
    elif kind == "CHARACTER_SHEET_IMAGE":
        _insert_character_sheet_task(seeded, task_id, asset_id="matrix-asset")
        table = "character_sheet_tasks"
    else:
        with psycopg.connect(seeded, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO oral_avatars(id,identity_id,owner_user_id,title,status,"
                "source_kind,source_asset_id) VALUES('matrix-avatar','ident-1','u-1',"
                "'测试分身','READY','VIDEO','synthetic-source')"
            )
            conn.execute(
                "INSERT INTO oral_tasks(id,owner_user_id,identity_id,avatar_id,mode,"
                "title,estimated_cost_fen,idempotency_key,status) "
                "VALUES(%s,'u-1','ident-1','matrix-avatar','TTS','测试口播',1,'matrix-ik',%s)",
                (task_id, status),
            )
        table = "oral_tasks"
    with psycopg.connect(seeded, autocommit=True) as conn:
        conn.execute(f"UPDATE {table} SET status=%s WHERE id=%s", (status, task_id))
        if kind != "ORAL_VIDEO":
            code = "PROVIDER_TERMINAL" if kind == "VIDEO" else "IMAGE_TASK_PROVIDER_FAILED"
            conn.execute(f"UPDATE {table} SET error_code=%s WHERE id=%s", (code, task_id))
    with external_call_context(kind, task_id, attempt=1):
        record_external_call(
            provider="synthetic-provider",
            endpoint="poll",
            method="GET",
            url="https://fixture.invalid/poll?token=secret-token",
            outcome="PROVIDER_ERROR",
            http_status=200,
            response_body=json.dumps(
                {
                    "status": "FAILED",
                    "error": {"code": "moderation", "message": "content policy violation"},
                    "debug": "https://fixture.invalid/result?signature=secret-sign",
                }
            ),
        )
    with psycopg.connect(seeded) as conn:
        persisted = conn.execute(
            "SELECT outcome,provider_message,response_body,created_at IS NOT NULL "
            "FROM external_call_logs WHERE task_id=%s",
            (task_id,),
        ).fetchone()
        assert persisted[0] == "PROVIDER_ERROR" and persisted[1] == "content policy violation"
        assert "secret-sign" not in persisted[2] and persisted[3] is True
    page = api.get("/api/control/generation-records", params={"record_type": kind}).json()
    item = next(row for row in page["items"] if row["record_id"] == task_id)
    assert item["provider_message"] == "content policy violation"
    assert item["failure_category"] and item["failure_owner"] and item["advice"]
    assert "secret-sign" not in str(item) and "secret-token" not in str(item)
    calls = api.get(f"/api/control/generation-records/{kind}/{task_id}/calls").json()
    assert calls["items"][0]["provider_message"] == "content policy violation"
    assert calls["items"][0]["has_response_body"] is True
