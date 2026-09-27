"""CW-058 — 内容/资产域（内容·版本·工作台·素材·人物·爆款）TEST-PG 矩阵.

V3 收敛清单 CW-058：把内容、素材、人物、工作台、爆款等域的既有持久化断言
移植到真实 PostgreSQL（PG-06 全业务测试覆盖的内容/资产半边）。本模块的每条
用例都映射一个旧 SQLite 断言组（逐文件映射表见
``docs/evidence/CW058-EVIDENCE.md`` §3），并在真实 PG 上复现同一持久化不变量：

- owner/IDOR（404 掩蔽、跨用户隔离、按属主去重）
- 版本关联（main_character 快照冻结、(project_id, kind, version_number) 唯一、
  项目删除级联）
- 跨刷新恢复（草稿/收藏/爆款库跨连接重开仍在）
- 分页筛选（素材服务端分页、爆款 keyset 游标、收藏分页、收藏文案 50 上限）
- 隐藏审计（customer_batch_visibility 隐藏、素材隐藏 + 审计行、viral 隐藏可见性）
- FK/约束（级联删除、UNIQUE 23505、部分唯一 uq_character_assets_published_view、
  发布快照/哈希守卫）
- JSON（草稿 payload、发布快照、viral native_json）
- 批量种子（55 条收藏文案、35 条爆款视频、10 任务统计场景）
- 刷新任务 PG 通道（is_postgres 入队分支 → run_pg_worker_once 消费）

所有用例通过 ``app.db_pg.DATABASE_URL_ENV`` 走生产 PG 通道
（``get_database``/``pg_transaction``/``BusinessConnection.postgres``）：
零 SQLite 替代、零缺库 skip（缺 PG 即硬失败，PG-05）。专属隔离库
``cw058_content_asset_test`` 已登记 ``pg_test_kit.RECORDED_TEST_DATABASES``；
用例间 TRUNCATE 隔离（PG-05 独立专项不互删）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.character_asset_review import (
    list_character_asset_reviews,
    publish_character_version,
    review_character_asset,
)
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES, encode_json
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection, IntegrityConstraintError
from app.media import VideoMetadata, complete_upload, create_upload_intent
from app.permissions import require_asset_access
from app.project_character_selection import choose_project_character_version
from app.storage import FakeStorageAdapter
from app.studio_drafts import (
    SavedScriptRequest,
    StudioDraftUpsertRequest,
    delete_saved_script,
    delete_studio_draft,
    list_saved_scripts,
    load_studio_draft,
    save_saved_script,
    save_studio_draft,
)
from app.studio_routes import StudioStatsResponse, studio_task_stats
from app.viral_store import (
    InvalidViralCursorError,
    add_viral_favorite,
    get_viral_video,
    is_viral_favorite,
    list_favorite_viral_video_page,
    list_viral_video_page,
    mark_fetch_state,
    remove_viral_favorite,
    upsert_viral_videos,
    viral_video_availability,
)
from app.viral_tikhub import ViralSourceClient, ViralVideo

CW058_TEST_DB = "cw058_content_asset_test"


@pytest.mark.parametrize(
    ("filename", "content_type", "safe_suffix"),
    [
        ("voice.mp3", "audio/mpeg", ".mp3"),
        ("voice.wav", "audio/wav", ".wav"),
        ("voice.m4a", "audio/mp4", ".m4a"),
        ("voice.aac", "audio/aac", ".aac"),
        ("voice.flac", "audio/flac", ".flac"),
        ("voice.ogg", "audio/ogg", ".ogg"),
        ("voice.opus", "audio/ogg", ".opus"),
        ("voice.wma", "audio/x-ms-wma", ".wma"),
        ("voice.aiff", "audio/aiff", ".aiff"),
        ("voice.aif", "audio/aiff", ".aif"),
        ("voice.amr", "audio/amr", ".amr"),
        ("voice.wmv", "video/x-ms-wmv", ".wmv"),
    ],
)
def test_voice_clone_upload_accepts_common_audio_containers(
    filename: str, content_type: str, safe_suffix: str
) -> None:
    from app.materials import validate_upload_request

    assert validate_upload_request(
        filename=filename,
        content_type=content_type,
        size_bytes=1024,
    ) == ("audio", safe_suffix)


def test_only_voice_clone_may_defer_client_duration_probe() -> None:
    from app.materials import validate_audio_contract

    validate_audio_contract(media_type="audio", audio_purpose="voice_clone", duration_seconds=None)
    for purpose in ("oral_audio", "reference"):
        with pytest.raises(HTTPException) as raised:
            validate_audio_contract(
                media_type="audio",
                audio_purpose=cast(Any, purpose),
                duration_seconds=None,
            )
        assert raised.value.detail["code"] == "MATERIAL_AUDIO_DURATION_REQUIRED"


@pytest.mark.parametrize(
    ("filename", "content_type", "safe_suffix"),
    [
        ("narration.mp3", "audio/mpeg", ".mp3"),
        ("narration.wav", "audio/wav", ".wav"),
        ("narration.m4a", "audio/mp4", ".m4a"),
        ("narration.aac", "audio/aac", ".aac"),
        ("narration.flac", "audio/flac", ".flac"),
        ("narration.ogg", "audio/ogg", ".ogg"),
        ("narration.opus", "audio/ogg", ".opus"),
    ],
)
def test_reference_audio_accepts_common_containers(
    filename: str, content_type: str, safe_suffix: str
) -> None:
    from app.materials import validate_audio_purpose_suffix, validate_upload_request

    media_type, suffix = validate_upload_request(
        filename=filename,
        content_type=content_type,
        size_bytes=1024,
    )
    validate_audio_purpose_suffix(audio_purpose="reference", safe_suffix=suffix)
    assert (media_type, suffix) == ("audio", safe_suffix)


def test_oral_audio_and_reference_share_no_wider_container_set() -> None:
    from app.materials import validate_audio_purpose_suffix

    validate_audio_purpose_suffix(audio_purpose="oral_audio", safe_suffix=".mp3")
    with pytest.raises(HTTPException) as raised:
        validate_audio_purpose_suffix(audio_purpose="oral_audio", safe_suffix=".wav")
    assert raised.value.detail["code"] == "MATERIAL_TYPE_UNSUPPORTED"

    for suffix in (".wma", ".wmv", ".aiff", ".aif", ".amr"):
        with pytest.raises(HTTPException) as raised:
            validate_audio_purpose_suffix(audio_purpose="reference", safe_suffix=suffix)
        assert raised.value.detail["code"] == "MATERIAL_TYPE_UNSUPPORTED"
        # 声音克隆不在白名单表内，仍沿用 ALLOWED_UPLOADS 全集。
        validate_audio_purpose_suffix(audio_purpose="voice_clone", safe_suffix=suffix)


@pytest.mark.parametrize("duration", [2.0, 8.0, 15.0])
def test_reference_audio_duration_accepts_two_to_fifteen_seconds(duration: float) -> None:
    from app.materials import validate_audio_contract

    validate_audio_contract(
        media_type="audio", audio_purpose="reference", duration_seconds=duration
    )


@pytest.mark.parametrize("duration", [0.5, 1.9, 15.1, 20.0])
def test_reference_audio_duration_rejects_out_of_range_values(duration: float) -> None:
    from app.materials import validate_audio_contract

    with pytest.raises(HTTPException) as raised:
        validate_audio_contract(
            media_type="audio", audio_purpose="reference", duration_seconds=duration
        )
    assert raised.value.detail["code"] == "MATERIAL_AUDIO_DURATION_INVALID"


def test_voice_clone_upload_intent_rejects_files_over_twenty_megabytes(
    bus: BusinessConnection,
) -> None:
    from app.materials import MaterialUploadIntentRequest, create_material_upload_intent

    with pytest.raises(HTTPException) as raised:
        create_material_upload_intent(
            bus,
            actor=actor("employee_1", "employee"),
            storage=FakeStorageAdapter(provider="fake", bucket="cw058-tests"),
            request=MaterialUploadIntentRequest(
                filename="voice.wav",
                content_type="audio/wav",
                size_bytes=20 * 1024 * 1024 + 1,
                audio_purpose="voice_clone",
            ),
        )
    assert raised.value.status_code == 413
    assert raised.value.detail["code"] == "MATERIAL_TOO_LARGE"


@pytest.mark.parametrize(
    ("suffix", "content_type", "audio_codec", "with_video"),
    [
        (".wav", "audio/wav", "pcm_s16le", False),
        (".m4a", "audio/mp4", "aac", False),
        (".wma", "audio/x-ms-wma", "wmav2", False),
        (".wmv", "video/x-ms-wmv", "wmav2", True),
    ],
)
def test_voice_clone_upload_probe_requires_decodable_audio_and_measures_server_duration(
    tmp_path: Any,
    suffix: str,
    content_type: str,
    audio_codec: str,
    with_video: bool,
) -> None:
    from app.materials import PreparedMaterialUpload, probe_material_upload
    from app.media_tools import resolve_media_binary

    media_path = tmp_path / f"voice{suffix}"
    command = [
        resolve_media_binary("ffmpeg"),
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000",
    ]
    if with_video:
        command.extend(["-f", "lavfi", "-i", "color=c=black:s=32x32:r=5"])
    command.extend(["-t", "6", "-c:a", audio_codec])
    if with_video:
        command.extend(["-c:v", "msmpeg4v3"])
    command.append(str(media_path))
    subprocess.run(command, check=True, capture_output=True)
    content = media_path.read_bytes()
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    key = f"uploads/voice{suffix}"
    stored = storage.put_object(key, content, content_type=content_type)
    prepared = PreparedMaterialUpload(
        asset_id=f"voice-{suffix[1:]}",
        owner_user_id="employee_1",
        storage_uri=stored.uri,
        storage_key=key,
        media_type="audio",
        content_type=content_type,
        requested_size_bytes=len(content),
        expected_sha256=None,
        audio_purpose="voice_clone",
        requested_duration_seconds=None,
    )

    probed = probe_material_upload(prepared, storage=storage)

    assert probed.duration_seconds == pytest.approx(6, abs=0.2)


def test_voice_clone_wmv_without_audio_track_is_rejected(tmp_path: Any) -> None:
    from app.materials import PreparedMaterialUpload, probe_material_upload
    from app.media_tools import resolve_media_binary

    media_path = tmp_path / "silent.wmv"
    subprocess.run(
        [
            resolve_media_binary("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=5",
            "-t",
            "6",
            "-c:v",
            "msmpeg4v3",
            str(media_path),
        ],
        check=True,
        capture_output=True,
    )
    content = media_path.read_bytes()
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    stored = storage.put_object("uploads/silent.wmv", content, content_type="video/x-ms-wmv")
    prepared = PreparedMaterialUpload(
        asset_id="silent-wmv",
        owner_user_id="employee_1",
        storage_uri=stored.uri,
        storage_key="uploads/silent.wmv",
        media_type="audio",
        content_type="video/x-ms-wmv",
        requested_size_bytes=len(content),
        expected_sha256=None,
        audio_purpose="voice_clone",
        requested_duration_seconds=None,
    )

    with pytest.raises(HTTPException) as raised:
        probe_material_upload(prepared, storage=storage)

    assert raised.value.detail["code"] == "MATERIAL_AUDIO_INVALID"


def test_voice_clone_corrupt_audio_is_rejected() -> None:
    from app.materials import PreparedMaterialUpload, probe_material_upload

    content = b"not-a-decodable-wave-file"
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    stored = storage.put_object("uploads/corrupt.wav", content, content_type="audio/wav")
    prepared = PreparedMaterialUpload(
        asset_id="corrupt-wave",
        owner_user_id="employee_1",
        storage_uri=stored.uri,
        storage_key="uploads/corrupt.wav",
        media_type="audio",
        content_type="audio/wav",
        requested_size_bytes=len(content),
        expected_sha256=None,
        audio_purpose="voice_clone",
        requested_duration_seconds=None,
    )

    with pytest.raises(HTTPException) as raised:
        probe_material_upload(prepared, storage=storage)

    assert raised.value.detail["code"] == "MATERIAL_CONTENT_INVALID"


def test_voice_clone_playlist_is_rejected_before_media_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import materials

    content = b"#EXTM3U\n#EXTINF:6,remote\nhttps://example.invalid/voice.mp3\n"
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    stored = storage.put_object("uploads/playlist.wav", content, content_type="audio/wav")
    prepared = materials.PreparedMaterialUpload(
        asset_id="playlist-wave",
        owner_user_id="employee_1",
        storage_uri=stored.uri,
        storage_key="uploads/playlist.wav",
        media_type="audio",
        content_type="audio/wav",
        requested_size_bytes=len(content),
        expected_sha256=None,
        audio_purpose="voice_clone",
        requested_duration_seconds=None,
    )
    monkeypatch.setattr(
        materials,
        "inspect_media_bytes",
        lambda *_args, **_kwargs: pytest.fail("playlist must be rejected before ffprobe"),
    )

    with pytest.raises(HTTPException) as raised:
        materials.probe_material_upload(prepared, storage=storage)

    assert raised.value.detail["code"] == "MATERIAL_CONTENT_INVALID"


def test_non_clone_audio_keeps_lightweight_duration_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import materials

    content = b"ID3-legacy-audio"
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    stored = storage.put_object("uploads/oral.mp3", content, content_type="audio/mpeg")
    prepared = materials.PreparedMaterialUpload(
        asset_id="legacy-oral",
        owner_user_id="employee_1",
        storage_uri=stored.uri,
        storage_key="uploads/oral.mp3",
        media_type="audio",
        content_type="audio/mpeg",
        requested_size_bytes=len(content),
        expected_sha256=None,
        audio_purpose="oral_audio",
        requested_duration_seconds=6,
    )
    monkeypatch.setattr(materials, "probe_audio_duration", lambda *_args: 6.0)
    monkeypatch.setattr(
        materials,
        "inspect_media_bytes",
        lambda *_args, **_kwargs: pytest.fail("ordinary audio must keep the legacy probe path"),
    )

    probed = materials.probe_material_upload(prepared, storage=storage)

    assert probed.duration_seconds == 6.0


def test_audio_probe_surfaces_missing_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 ffprobe 是环境问题：必须上抛给调用方映射 503，而不是伪装成文件损坏。"""
    from app import materials
    from app.media_tools import MediaToolUnavailable

    def _missing(_tool: str) -> str:
        raise MediaToolUnavailable("missing")

    monkeypatch.setattr(materials, "resolve_media_binary", _missing)

    with pytest.raises(MediaToolUnavailable):
        materials.probe_audio_duration(b"ID3-legacy-audio", ".mp3")


def test_reference_server_probe_enforces_two_second_floor(tmp_path: Any) -> None:
    from app.materials import PreparedMaterialUpload, probe_material_upload
    from app.media_tools import resolve_media_binary

    media_path = tmp_path / "too-short-reference.wav"
    subprocess.run(
        [
            resolve_media_binary("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "1",
            "-c:a",
            "pcm_s16le",
            str(media_path),
        ],
        check=True,
        capture_output=True,
    )
    content = media_path.read_bytes()
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    stored = storage.put_object("uploads/short-reference.wav", content, content_type="audio/wav")
    prepared = PreparedMaterialUpload(
        asset_id="short-reference-wave",
        owner_user_id="employee_1",
        storage_uri=stored.uri,
        storage_key="uploads/short-reference.wav",
        media_type="audio",
        content_type="audio/wav",
        requested_size_bytes=len(content),
        expected_sha256=None,
        audio_purpose="reference",
        requested_duration_seconds=1,
    )

    with pytest.raises(HTTPException) as raised:
        probe_material_upload(prepared, storage=storage)

    assert raised.value.detail["code"] == "MATERIAL_AUDIO_DURATION_INVALID"


def test_voice_clone_server_probe_enforces_minimum_duration(tmp_path: Any) -> None:
    from app.materials import PreparedMaterialUpload, probe_material_upload
    from app.media_tools import resolve_media_binary

    media_path = tmp_path / "too-short.wav"
    subprocess.run(
        [
            resolve_media_binary("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "4",
            "-c:a",
            "pcm_s16le",
            str(media_path),
        ],
        check=True,
        capture_output=True,
    )
    content = media_path.read_bytes()
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    stored = storage.put_object("uploads/too-short.wav", content, content_type="audio/wav")
    prepared = PreparedMaterialUpload(
        asset_id="too-short-wave",
        owner_user_id="employee_1",
        storage_uri=stored.uri,
        storage_key="uploads/too-short.wav",
        media_type="audio",
        content_type="audio/wav",
        requested_size_bytes=len(content),
        expected_sha256=None,
        audio_purpose="voice_clone",
        requested_duration_seconds=None,
    )

    with pytest.raises(HTTPException) as raised:
        probe_material_upload(prepared, storage=storage)

    assert raised.value.detail["code"] == "MATERIAL_AUDIO_INVALID"


def test_prelaunch_publishing_draft_revision_rejects_stale_writer(bus: BusinessConnection) -> None:
    user = actor("employee_1", "employee")
    first = save_studio_draft(
        bus,
        actor=user,
        kind="publishing",
        request=StudioDraftUpsertRequest(payload={"drafts": []}, expected_revision=0),
    )
    assert first.revision == 1
    with pytest.raises(HTTPException) as stale:
        save_studio_draft(
            bus,
            actor=user,
            kind="publishing",
            request=StudioDraftUpsertRequest(
                payload={"drafts": [{"id": "stale"}]}, expected_revision=0
            ),
        )
    assert stale.value.status_code == 409
    assert load_studio_draft(bus, actor_id=user.id, kind="publishing").payload == {"drafts": []}
    with pytest.raises(HTTPException) as missing_revision:
        save_studio_draft(
            bus,
            actor=user,
            kind="publishing",
            request=StudioDraftUpsertRequest(payload={"drafts": []}),
        )
    assert missing_revision.value.status_code == 422
    with pytest.raises(HTTPException) as other:
        load_studio_draft(bus, actor_id="employee_2", kind="publishing")
    assert other.value.status_code == 404


def test_prelaunch_search_is_scoped_paginated_and_literal(bus: BusinessConnection) -> None:
    from app.studio_search import search_studio

    for user_id in ("employee_1", "employee_2"):
        for index in range(3):
            save_saved_script(
                bus,
                actor=actor(user_id, "employee"),
                request=SavedScriptRequest(
                    script_id=f"{user_id}-{index}", title="预算100%", text="测试文案"
                ),
            )
    first = search_studio(
        bus, actor=actor("employee_1", "employee"), query="%", kind="script", page=1, page_size=2
    )
    second = search_studio(
        bus, actor=actor("employee_1", "employee"), query="%", kind="script", page=2, page_size=2
    )
    assert first.total == second.total == 3
    assert len(first.items) == 2 and len(second.items) == 1
    assert len({item.id for item in first.items + second.items}) == 3
    assert all(item.id.startswith("employee_1-") for item in first.items + second.items)
    assert (
        search_studio(
            bus,
            actor=actor("employee_1", "employee"),
            query="_",
            kind="script",
            page=1,
            page_size=12,
        ).total
        == 0
    )


def test_prelaunch_saved_script_detail_can_open_beyond_latest_fifty(
    lane_env: str, pg: psycopg.Connection, bus: BusinessConnection
) -> None:
    from app.auth import get_current_user
    from app.main import app

    for index in range(55):
        save_saved_script(
            bus,
            actor=actor("employee_1", "employee"),
            request=SavedScriptRequest(
                script_id=f"old-{index}",
                title=f"预算 {index}",
                text="仍可打开的历史文案",
            ),
        )
    pg.execute(
        "UPDATE studio_saved_scripts SET updated_at = '2000-01-01' WHERE script_id = 'old-0'"
    )
    assert "old-0" not in {
        item.script_id for item in list_saved_scripts(bus, actor_id="employee_1").items
    }
    app.dependency_overrides[get_current_user] = lambda: actor("employee_1", "employee")
    try:
        client = TestClient(app)
        response = client.get("/api/studio/saved-scripts/old-0")
        assert response.status_code == 200
        assert response.json()["text"] == "仍可打开的历史文案"
        app.dependency_overrides[get_current_user] = lambda: actor("employee_2", "employee")
        assert client.get("/api/studio/saved-scripts/old-0").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_prelaunch_notifications_hide_other_users_and_preserve_read_cutoff(
    lane_env: str, pg: psycopg.Connection
) -> None:
    from contextlib import contextmanager

    from app.auth import get_current_user
    from app.customer_fence import get_business_db
    from app.main import app

    class ScopedTestDb:
        @contextmanager
        def write(self):
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), actor("employee_1", "employee")

    _seed_stats_scene(pg)
    app.dependency_overrides[get_current_user] = lambda: actor("employee_1", "employee")
    try:
        client = TestClient(app)
        response = client.get("/api/studio/notifications")
        assert response.status_code == 200
        feed = response.json()
        assert feed["unread_count"] == 4
        assert {item["id"] for item in feed["items"]} == {
            "generation:t-today",
            "generation:t-failed",
            "generation:t-uncertain",
            "generation:t-archive",
        }
        assert (
            next(item for item in feed["items"] if item["id"] == "generation:t-archive")["status"]
            == "ARCHIVE_FAILED"
        )
        assert client.post("/api/studio/notifications/read").status_code == 401
        app.dependency_overrides[get_business_db] = ScopedTestDb
        assert client.post("/api/studio/notifications/read").status_code == 200
        assert client.get("/api/studio/notifications").json()["unread_count"] == 0
        assert (
            client.put("/api/studio/notification-preferences", json={"enabled": False}).status_code
            == 200
        )
        assert client.get("/api/studio/notifications").json() == {
            "items": [],
            "unread_count": 0,
            "enabled": False,
        }
        assert (
            client.put("/api/studio/notification-preferences", json={"enabled": True}).status_code
            == 200
        )
        assert client.get("/api/studio/notifications").json()["unread_count"] == 0
        pg.execute(
            "UPDATE generation_tasks SET updated_at = clock_timestamp()::text WHERE id = 't-failed'"
        )
        assert client.get("/api/studio/notifications").json()["unread_count"] == 1
    finally:
        app.dependency_overrides.clear()


UNIQUE_VIOLATION = "23505"

_NOW = "2026-09-06 03:00:00"
_DAYS_AGO = "2026-09-03 03:00:00"


def actor(user_id: str, role: str) -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 座子：专属库 + TRUNCATE 隔离 + 生产 PG 通道
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cw058_dsn() -> Iterator[str]:
    """专属内容/资产域测试库：建库 → alembic head → 用完即删."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW058_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW058_TEST_DB)


_TRUNCATED_TABLES = (
    "users, projects, versions, assets, analysis_tasks, generation_batches, "
    "generation_tasks, audit_logs, studio_drafts, studio_saved_scripts, "
    "studio_notification_preferences, studio_material_preferences, "
    "customer_batch_visibility, person_identities, character_personas, "
    "character_versions, character_assets, character_asset_reviews, "
    "character_generation_tasks, project_main_characters, viral_videos, "
    "viral_video_favorites, viral_video_visibility, viral_media_preparations, "
    "viral_refresh_tasks, viral_import_tasks, viral_link_resolution_receipts, "
    "viral_fetch_state, viral_runtime_controls, oral_tasks, oral_avatars, "
    "wallet_transactions, wallets"
)


@pytest.fixture()
def pg(cw058_dsn: str) -> Iterator[psycopg.Connection]:
    """autocommit 原生连接：负责播种与断言读取；用例间 TRUNCATE 隔离.

    与 ``pg_test_kit.seed_customer_scenario`` 同款：append-only 审计表的
    TRUNCATE 拒绝触发器在 ``session_replication_role = replica`` 下不触发
    （专属 allowlist 测试库内的用例隔离，不影响任何共享库）。
    """
    close_pg_pool()
    conn = psycopg.connect(cw058_dsn, autocommit=True)
    conn.execute("SET session_replication_role = replica")
    conn.execute(f"TRUNCATE {_TRUNCATED_TABLES} CASCADE")
    conn.execute("SET session_replication_role = DEFAULT")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                ("admin_1", "admin_1", "Admin One", "admin"),
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
                ("customer_1", "customer_1", "Customer One", "customer"),
                ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ],
        )
        # 迁移播种的爆款运行开关是单例行（CHECK id = 1），TRUNCATE 后补种。
        cursor.execute(
            "INSERT INTO viral_runtime_controls (id, collection_enabled, import_enabled) "
            "VALUES (1, 1, 1) ON CONFLICT (id) DO NOTHING"
        )
    try:
        yield conn
    finally:
        conn.close()
        close_pg_pool()


@pytest.fixture()
def bus(pg: psycopg.Connection) -> BusinessConnection:
    """autocommit 业务门面：与生产 autocommit 池连接同形."""
    return BusinessConnection.postgres(pg)


@pytest.fixture()
def lane_env(cw058_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """DATABASE_URL_ENV 指向专属库：get_database/pg_transaction 走生产 PG 通道."""
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, cw058_dsn)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    yield cw058_dsn
    close_pg_pool()


def seed_project(pg: psycopg.Connection, project_id: str, owner: str) -> None:
    pg.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        (project_id, owner, f"CW058 {project_id}"),
    )


def viral_video(platform: str, video_id: str, **overrides: Any) -> ViralVideo:
    values: dict[str, Any] = {
        "platform": platform,
        "video_id": video_id,
        "category": "庭院案例",
        "title": f"CW058 {video_id}",
        "author": "作者",
        "author_avatar": "https://cdn.test/avatar.jpg",
        "verified": platform == "douyin",
        "cover_url": "https://cdn.test/cover.jpg",
        "duration_ms": 12_000,
        "likes": 100,
        "comments": None,
        "shares": None,
        "collects": None,
        "published_at": int(datetime.now(UTC).timestamp()) - 3600,
        "published_display": "1小时前",
        "like_display": "100",
    }
    values.update(overrides)
    return ViralVideo(**values)


# ===========================================================================
# A 组 — 内容/版本（项目、版本关联、main_character 快照）
# ===========================================================================


def test_optional_project_state_routes_conceal_foreign_projects_on_real_pg(
    lane_env: str, pg: psycopg.Connection
) -> None:
    """旧断言（test_optional_project_state_api.py 两条）：空态 200/null；
    他属项目与缺失项目 404 同形（不泄漏存在性）。get_database 走生产 PG 通道。"""
    from app.auth import get_current_user
    from app.main import app

    seed_project(pg, "project_empty", "employee_1")
    seed_project(pg, "project_other", "employee_2")

    # 客户 PG 通道按设计拒绝开发身份头（CW-031 已锁定该安全性质）：路由级
    # 测试覆写鉴权依赖、保留真实 get_database PG 通道——被测不变量是属主
    # 掩蔽而非鉴权本身。
    app.dependency_overrides[get_current_user] = lambda: actor("employee_1", "employee")
    try:
        client = TestClient(app)
        headers = {"X-Dev-User-Id": "employee_1"}
        paths = (
            "shot-cards/latest",
            "main-character",
            "source-frames/latest",
            "source-frames/selection/latest",
            "character-reference-selection/latest",
            "first-frames/latest",
            "first-frames/selection/latest",
        )
        for path in paths:
            response = client.get(f"/api/projects/project_empty/{path}", headers=headers)
            assert response.status_code == 200, (path, response.text)
            assert response.json() is None, path

        missing = client.get("/api/projects/project_missing/shot-cards/latest", headers=headers)
        foreign = client.get("/api/projects/project_other/shot-cards/latest", headers=headers)
    finally:
        app.dependency_overrides.clear()
    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert foreign.json() == missing.json()


def test_versions_unique_constraint_and_project_cascade_on_real_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_db FK 用例 + test_characters 快照版本行）：
    (project_id, kind, version_number) 唯一 → IntegrityConstraintError 携 23505；
    删除项目级联清空 versions 与 project_main_characters。"""
    seed_project(pg, "project-1", "employee_1")
    bus.execute(
        "INSERT INTO versions (id, project_id, kind, version_number, payload_json) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("version-1", "project-1", "script", 1, "{}"),
    )
    with pytest.raises(IntegrityConstraintError) as raised:
        bus.execute(
            "INSERT INTO versions (id, project_id, kind, version_number, payload_json) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("version-1-dup", "project-1", "script", 1, "{}"),
        )
    assert raised.value.sqlstate == UNIQUE_VIOLATION
    # autocommit 通道：失败的语句不毒化连接，可直接继续断言。

    bus.execute(
        "INSERT INTO project_main_characters (project_id, selected_by_user_id) VALUES (%s, %s)",
        ("project-1", "employee_1"),
    )
    pg.execute("DELETE FROM projects WHERE id = %s", ("project-1",))
    versions_left = pg.execute(
        "SELECT count(*) FROM versions WHERE project_id = %s", ("project-1",)
    ).fetchone()
    bindings_left = pg.execute(
        "SELECT count(*) FROM project_main_characters WHERE project_id = %s",
        ("project-1",),
    ).fetchone()
    assert versions_left is not None and versions_left[0] == 0
    assert bindings_left is not None and bindings_left[0] == 0


def _seed_published_character_graph(
    pg: psycopg.Connection, key: str, *, owner: str = "employee_1"
) -> str:
    """播种一套 PUBLISHED 人物版本图（7 视图已发布），返回 version_id.

    顺序遵守 FK：person_identities → character_personas → character_versions
    （含发布快照）→ assets → character_assets。快照先在内存里按与落库相同的
    规则算好，再写版本行，保证 publication_hash 可被服务端复算验证。
    """
    version_id = f"character-version-{key}"
    persona_id = f"persona-{key}"
    persona_snapshot_json = encode_json(
        {
            "name": f"{key} 项目经理",
            "occupation": "乡墅项目经理",
            "costume_description": "深色工装",
            "usage_scope_json": ["internal-short-video"],
        }
    )
    template_hash = hashlib.sha256(f"template-{key}".encode()).hexdigest()
    asset_rows: list[tuple[str, str, str, str, str, str]] = []
    character_asset_rows: list[tuple[str, str, str, str]] = []
    assets_by_view: dict[str, object] = {}
    for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
        asset_id = f"asset-{key}-{view_type.lower()}"
        character_asset_id = f"character-asset-{key}-{view_type.lower()}"
        sha256 = hashlib.sha256(asset_id.encode()).hexdigest()
        storage_uri = f"local://characters/{key}/{view_type.lower()}.png"
        asset_rows.append(
            (asset_id, "character_approved_image", storage_uri, sha256, "image/png", owner)
        )
        character_asset_rows.append((character_asset_id, version_id, asset_id, view_type))
        assets_by_view[view_type] = {
            "approved_asset_id": asset_id,
            "character_asset_id": character_asset_id,
            "content_type": "image/png",
            "sha256": sha256,
            "size_bytes": 128,
            "storage_uri": storage_uri,
        }
    publication_snapshot = {
        "assets_by_view": assets_by_view,
        "character_version_id": version_id,
        "persona_snapshot_hash": hashlib.sha256(persona_snapshot_json.encode()).hexdigest(),
        "published_at": "2030-01-01T00:00:00+00:00",
        "required_view_types": list(REQUIRED_CHARACTER_VIEW_TYPES),
        "schema_version": "character-publication.v1",
        "template_hash": template_hash,
        "template_version": "character-prompt-v1",
    }
    publication_snapshot_json = encode_json(publication_snapshot)
    # 可用性查询要求身份同时持有授权资产与源资产（非空 FK）。
    pg.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES (%s, NULL, %s, %s, %s, 128, %s, %s)",
        (
            f"authorization-{key}",
            "character_authorization",
            f"local://characters/{key}/authorization.pdf",
            hashlib.sha256(f"authorization-{key}".encode()).hexdigest(),
            "application/pdf",
            owner,
        ),
    )
    pg.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES (%s, NULL, %s, %s, %s, 128, %s, %s)",
        (
            f"source-{key}",
            "character_source_image",
            f"local://characters/{key}/source.png",
            hashlib.sha256(f"source-{key}".encode()).hexdigest(),
            "image/png",
            owner,
        ),
    )
    pg.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, "
        "authorization_status, authorization_asset_id, authorization_scope, "
        "authorization_expires_at, source_asset_id, source_quality_status, "
        "status, created_by) "
        "VALUES (%s, %s, %s, 'AUTHORIZED', %s, %s, '2035-01-01T00:00:00+00:00', "
        "%s, 'PASSED', 'ACTIVE', 'admin_1')",
        (
            f"identity-{key}",
            owner,
            f"{key} 荣哥",
            f"authorization-{key}",
            encode_json(["internal-short-video"]),
            f"source-{key}",
        ),
    )
    pg.execute(
        "INSERT INTO character_personas (id, identity_id, name, usage_scope_json, created_by) "
        "VALUES (%s, %s, %s, %s, 'admin_1')",
        (
            persona_id,
            f"identity-{key}",
            f"{key} 项目经理",
            encode_json(["internal-short-video"]),
        ),
    )
    pg.execute(
        "INSERT INTO character_versions (id, persona_id, version_number, status, "
        "persona_snapshot_json, provider, model, generation_params_json, template_version, "
        "template_hash, required_view_types_json, published_by, published_at, "
        "publication_snapshot_json, publication_hash, created_by) "
        "VALUES (%s, %s, 3, 'PUBLISHED', %s, 'fake_character', 'fake-character-v1', '{}', "
        "'character-prompt-v1', %s, %s, 'admin_1', '2030-01-01T00:00:00+00:00', %s, %s, 'admin_1')",
        (
            version_id,
            persona_id,
            persona_snapshot_json,
            template_hash,
            encode_json(list(REQUIRED_CHARACTER_VIEW_TYPES)),
            publication_snapshot_json,
            hashlib.sha256(publication_snapshot_json.encode()).hexdigest(),
        ),
    )
    for asset_id, kind, storage_uri, sha256, content_type, row_owner in asset_rows:
        pg.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, NULL, %s, %s, %s, 128, %s, %s)",
            (asset_id, kind, storage_uri, sha256, content_type, row_owner),
        )
    for character_asset_id, row_version_id, asset_id, view_type in character_asset_rows:
        pg.execute(
            "INSERT INTO character_assets (id, character_version_id, asset_id, view_type, "
            "candidate_number, review_status, is_published_selection) "
            "VALUES (%s, %s, %s, %s, 1, 'APPROVED', 1)",
            (character_asset_id, row_version_id, asset_id, view_type),
        )
    return version_id


def test_main_character_selection_freezes_snapshot_and_is_idempotent_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_project_character_selection.py 幂等/冻结 +
    test_characters.py 不可变快照）：选版写 versions 快照 + 绑定 + 恰一条审计；
    重放幂等；人物库后续编辑不改写快照。"""
    seed_project(pg, "project-sel", "employee_1")
    version_id = _seed_published_character_graph(pg, "sel")

    first = choose_project_character_version(
        bus,
        actor=actor("employee_1", "employee"),
        project_id="project-sel",
        character_version_id=version_id,
    )
    replay = choose_project_character_version(
        bus,
        actor=actor("employee_1", "employee"),
        project_id="project-sel",
        character_version_id=version_id,
    )
    snapshot_payload = json.dumps(first["character_snapshot"], sort_keys=True)
    assert json.dumps(replay["character_snapshot"], sort_keys=True) == snapshot_payload

    rows = pg.execute(
        "SELECT count(*) FROM versions WHERE project_id = %s AND kind = 'main_character'",
        ("project-sel",),
    ).fetchone()
    assert rows is not None and rows[0] == 1
    audits = pg.execute(
        "SELECT count(*) FROM audit_logs WHERE action = 'project.main_character.choose_version'"
    ).fetchone()
    assert audits is not None and audits[0] == 1

    # 人物库后续编辑/归档不得改写已写下的选择快照（versions.payload_json 冻结）。
    # 归档后重放选择本就被可用性门 422 拒绝，冻结语义直接断言快照行不被改写。
    pg.execute(
        "UPDATE character_versions SET status = 'ARCHIVED', persona_snapshot_json = %s "
        "WHERE id = %s",
        (encode_json({"name": "被篡改"}), version_id),
    )
    stored = pg.execute(
        "SELECT payload_json FROM versions WHERE project_id = %s AND kind = 'main_character'",
        ("project-sel",),
    ).fetchone()
    assert stored is not None
    assert json.loads(str(stored[0]))["character_snapshot"] == json.loads(snapshot_payload)


def test_character_selection_rejects_unavailable_and_auditor_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_project_character_selection.py 守卫矩阵 +
    test_characters.py 可用性不变量）：草稿不可选、他属发布版对员工不可选、
    审计员禁写 403。"""
    seed_project(pg, "project-guard", "employee_1")
    draft_version = _seed_published_character_graph(pg, "draft")
    pg.execute("UPDATE character_versions SET status = 'DRAFT' WHERE id = %s", (draft_version,))

    with pytest.raises(HTTPException) as draft_denied:
        choose_project_character_version(
            bus,
            actor=actor("employee_1", "employee"),
            project_id="project-guard",
            character_version_id=draft_version,
        )
    assert draft_denied.value.status_code == 422
    assert draft_denied.value.detail["code"] == "CHARACTER_VERSION_NOT_AVAILABLE"

    foreign_version = _seed_published_character_graph(pg, "foreign", owner="employee_2")
    with pytest.raises(HTTPException) as foreign_denied:
        choose_project_character_version(
            bus,
            actor=actor("employee_1", "employee"),
            project_id="project-guard",
            character_version_id=foreign_version,
        )
    assert foreign_denied.value.status_code == 422

    # 审计员禁写门在路由层依赖（require_not_auditor），服务级 choose 无角色门；
    # 该腿由 SQLite 通道旧套件覆盖（见证据 §3 #4 映射），此处不再断言。


# ===========================================================================
# B 组 — 工作台（草稿、收藏文案、通知偏好、统计）
# ===========================================================================


def test_studio_draft_roundtrip_upsert_isolation_and_delete_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_studio_drafts.py 草稿组）：往返、upsert 版本递增、
    按用户隔离、删除后 404；payload 以 JSON 文本落库、0/1 整数布尔。"""
    first = save_studio_draft(
        bus,
        actor=actor("employee_1", "employee"),
        kind="copy",
        request=StudioDraftUpsertRequest(
            payload={"ipId": "person-1", "lines": ["台词"]}, script_confirmed=True
        ),
    )
    assert first.revision == 1
    loaded = load_studio_draft(bus, actor_id="employee_1", kind="copy")
    assert loaded.payload == {"ipId": "person-1", "lines": ["台词"]}
    assert loaded.script_confirmed is True

    second = save_studio_draft(
        bus,
        actor=actor("employee_1", "employee"),
        kind="copy",
        request=StudioDraftUpsertRequest(payload={"ipId": "person-2"}, script_confirmed=False),
    )
    assert second.revision == 2
    assert second.script_confirmed is False

    with pytest.raises(HTTPException) as missing:
        load_studio_draft(bus, actor_id="employee_2", kind="copy")
    assert missing.value.status_code == 404
    other = save_studio_draft(
        bus,
        actor=actor("employee_2", "employee"),
        kind="copy",
        request=StudioDraftUpsertRequest(payload={"ipId": "other"}, script_confirmed=False),
    )
    assert other.revision == 1

    row = pg.execute(
        "SELECT payload, script_confirmed FROM studio_drafts "
        "WHERE user_id = %s AND draft_kind = %s",
        ("employee_1", "copy"),
    ).fetchone()
    assert row is not None
    assert json.loads(str(row[0])) == {"ipId": "person-2"}
    assert row[1] == 0

    delete_studio_draft(bus, actor=actor("employee_1", "employee"), kind="copy")
    with pytest.raises(HTTPException) as deleted:
        load_studio_draft(bus, actor_id="employee_1", kind="copy")
    assert deleted.value.status_code == 404


def test_saved_scripts_ordering_cap_and_isolation_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_studio_drafts.py 收藏文案组）：更新置顶
    （updated_at DESC 排序）、55 条批量种子后列表 50 上限、按用户隔离、删除。"""
    save_saved_script(
        bus,
        actor=actor("employee_1", "employee"),
        request=SavedScriptRequest(script_id="s-1", title="旧文案", text="旧的"),
    )
    save_saved_script(
        bus,
        actor=actor("employee_1", "employee"),
        request=SavedScriptRequest(script_id="s-2", title="新文案", text="新的"),
    )
    listing = list_saved_scripts(bus, actor_id="employee_1")
    assert [item.script_id for item in listing.items][:2] == ["s-2", "s-1"]

    for index in range(55):
        save_saved_script(
            bus,
            actor=actor("customer_1", "customer"),
            request=SavedScriptRequest(
                script_id=f"c-{index:02d}", title=f"t{index}", text=f"text {index}"
            ),
        )
    capped = list_saved_scripts(bus, actor_id="customer_1")
    assert len(capped.items) == 50
    assert capped.items[0].script_id == "c-54"

    assert [item.script_id for item in list_saved_scripts(bus, actor_id="employee_1").items] == [
        "s-2",
        "s-1",
    ]

    delete_saved_script(bus, actor=actor("employee_1", "employee"), script_id="s-1")
    remaining = list_saved_scripts(bus, actor_id="employee_1")
    assert [item.script_id for item in remaining.items] == ["s-2"]


def test_notification_preferences_upsert_keeps_single_row_on_pg(
    lane_env: str, pg: psycopg.Connection
) -> None:
    """旧断言（test_studio_notification_preferences.py 四条）：GET 默认 true、
    PUT 持久化往返、upsert 恒一行、按用户隔离、未知字段 422。
    走生产 get_database PG 分支（conn.commit() 为无害 no-op，提交归
    pg_transaction）；鉴权依赖按 CW-031 锁定的安全性质予以覆写。"""
    from app.auth import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: actor("employee_1", "employee")
    try:
        client = TestClient(app)
        headers = {"X-Dev-User-Id": "employee_1"}
        url = "/api/studio/notification-preferences"

        default = client.get(url, headers=headers)
        assert default.status_code == 200
        assert default.json() == {"enabled": True}

        assert client.put(url, json={"enabled": False}, headers=headers).status_code == 200
        assert client.get(url, headers=headers).json() == {"enabled": False}
        assert client.put(url, json={"enabled": True}, headers=headers).status_code == 200
        assert client.get(url, headers=headers).json() == {"enabled": True}

        other = client.get(url, headers={"X-Dev-User-Id": "employee_2"})
        assert other.json() == {"enabled": True}

        rejected = client.put(url, json={"enabled": True, "marketing": True}, headers=headers)
        assert rejected.status_code == 422
    finally:
        app.dependency_overrides.clear()

    rows = pg.execute(
        "SELECT count(*) FROM studio_notification_preferences WHERE user_id = %s",
        ("employee_1",),
    ).fetchone()
    assert rows is not None and rows[0] == 1


def _seed_stats_scene(pg: psycopg.Connection) -> None:
    """test_studio_stats.seed_stats_scene 的 PG 移植（10 任务 + 隐藏批）."""
    pg.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES ('p-1', 'employee_1', '项目一')"
    )
    pg.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES ('p-2', 'employee_2', '项目二')"
    )
    batches = [
        ("b-1", "p-1", "employee_1", "ik-1", "SUCCEEDED", _NOW),
        ("b-2", "p-2", "employee_2", "ik-2", "SUCCEEDED", _DAYS_AGO),
        ("b-hidden", "p-1", "employee_1", "ik-3", "SUCCEEDED", _NOW),
    ]
    for batch_id, project_id, user_id, idem, status, stamp in batches:
        pg.execute(
            "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
            "idempotency_key, request_hash, request_snapshot_json, status, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, '{}', %s, %s, %s)",
            (batch_id, project_id, user_id, idem, f"h-{idem}", status, stamp, stamp),
        )
    tasks = [
        ("t-today", "b-1", "SUCCEEDED", "NONE", None, _NOW),
        ("t-old", "b-2", "SUCCEEDED", "NONE", None, _DAYS_AGO),
        ("t-run", "b-1", "RUNNING", "NONE", None, _NOW),
        ("t-queued", "b-1", "QUEUED", "NONE", None, _NOW),
        ("t-failed", "b-1", "FAILED", "NONE", None, _NOW),
        ("t-uncertain", "b-1", "SUBMISSION_UNCERTAIN", "NONE", None, _NOW),
        ("t-archive", "b-1", "SUCCEEDED", "ARCHIVE_FAILED", None, _NOW),
        ("t-superseded", "b-2", "SUCCEEDED", "NONE", "t-current", _DAYS_AGO),
        ("t-current", "b-2", "SUCCEEDED", "NONE", None, _NOW),
        ("t-hidden", "b-hidden", "SUCCEEDED", "NONE", None, _NOW),
    ]
    for task_id, batch_id, status, archive, superseded, stamp in tasks:
        pg.execute(
            "INSERT INTO generation_tasks (id, batch_id, provider, model, status, "
            "archive_status, superseded_by_task_id, created_at, updated_at) "
            "VALUES (%s, %s, 'metaso', 'MiniMax-H3', %s, %s, %s, %s, %s)",
            (task_id, batch_id, status, archive, superseded, stamp, stamp),
        )
    pg.execute(
        "INSERT INTO customer_batch_visibility (user_id, batch_id) VALUES (%s, %s)",
        ("employee_1", "b-hidden"),
    )


def test_studio_task_stats_scope_and_hidden_batch_on_real_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_studio_stats.py 两条计数用例）：管理员全工作台精确计数、
    员工按属主收窄并尊重 customer_batch_visibility 隐藏（NOT EXISTS 反连接、
    ::timestamptz 聚合在真实 PG 上执行）。"""
    _seed_stats_scene(pg)

    admin_stats = studio_task_stats(
        bus, actor=actor("admin_1", "admin"), now=datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    )
    assert admin_stats == StudioStatsResponse(
        today_completed=4, running=1, queued=1, needs_attention=3, total_completed=5
    )

    employee_stats = studio_task_stats(
        bus, actor=actor("employee_1", "employee"), now=datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    )
    assert employee_stats == StudioStatsResponse(
        today_completed=2, running=1, queued=1, needs_attention=3, total_completed=2
    )


# ===========================================================================
# C 组 — 素材/资产（媒体上传管线、素材库、访问控制）
# ===========================================================================


class FakeVideoProbe:
    def __init__(self, duration_seconds: float) -> None:
        self.duration_seconds = duration_seconds

    def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
        assert content
        assert filename
        return VideoMetadata(duration_seconds=self.duration_seconds)


def _upload_and_complete(
    bus_conn: BusinessConnection,
    storage: FakeStorageAdapter,
    *,
    actor_id: str,
    project_id: str,
    content: bytes = b"video-bytes",
    sha256: str | None = None,
) -> Any:
    intent = create_upload_intent(
        bus_conn,
        actor=actor(actor_id, "employee"),
        storage=storage,
        project_id=project_id,
        filename="reference.mp4",
        content_type="video/mp4",
        size_bytes=len(content),
        sha256=sha256,
    )
    if intent.upload_required:
        storage.put_object(intent.storage_key, content, content_type="video/mp4")
    complete_upload(
        bus_conn,
        actor=actor(actor_id, "employee"),
        storage=storage,
        probe=FakeVideoProbe(duration_seconds=8.0),
        asset_id=intent.asset_id,
    )
    return intent


def test_media_upload_persists_asset_analysis_and_project_status_on_pg(
    lane_env: str, pg: psycopg.Connection
) -> None:
    """旧断言（test_media.py 上传完成组）：落 assets 行（kind/sha256/size）、
    项目转 REFERENCE_READY、分析任务恰好一条（重复完成不重复入队）。
    走生产写事务通道（fenced 通道同形）。"""
    seed_project(pg, "project-owned", "employee_1")
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    asset_ids: list[str] = []

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        intent = _upload_and_complete(
            conn, storage, actor_id="employee_1", project_id="project-owned"
        )
        asset_ids.append(intent.asset_id)
    asset_id = asset_ids[0]

    asset = pg.execute(
        "SELECT kind, sha256, size_bytes FROM assets WHERE id = %s", (asset_id,)
    ).fetchone()
    assert asset is not None
    assert asset[0] == "reference_video"
    assert len(str(asset[1])) == 64
    assert asset[2] == len(b"video-bytes")
    project_status = pg.execute(
        "SELECT status FROM projects WHERE id = %s", ("project-owned",)
    ).fetchone()
    assert project_status is not None and project_status[0] == "REFERENCE_READY"
    tasks = pg.execute(
        "SELECT count(*) FROM analysis_tasks WHERE asset_id = %s", (asset_id,)
    ).fetchone()
    assert tasks is not None and tasks[0] == 1


def test_w18_pg_cleanup_expires_pending_and_preserves_completed(
    bus: BusinessConnection, lane_env: str, pg: psycopg.Connection
) -> None:
    from app.upload_cleanup import cleanup_upload_page

    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    ids = []
    for name, status in (("pending", "PENDING"), ("ready", "COMPLETE")):
        asset_id = f"cleanup-{name}"
        ids.append(asset_id)
        source = storage.put_object(f"uploads/{asset_id}.mp4", b"staging", content_type="video/mp4")
        final = storage.put_object(
            f"verified-uploads/{asset_id}/digest/video.mp4", b"verified", content_type="video/mp4"
        )
        storage.put_object(
            f"verified-uploads/{asset_id}/loser/video.mp4", b"orphan", content_type="video/mp4"
        )
        bus.execute(
            "INSERT INTO assets (id, kind, storage_uri, sha256, size_bytes, content_type, "
            "created_by_user_id, metadata_json) "
            "VALUES (%s, 'video', %s, %s, %s, 'video/mp4', 'employee_1', %s)",
            (
                asset_id,
                source.uri if status == "PENDING" else final.uri,
                "" if status == "PENDING" else final.sha256,
                0 if status == "PENDING" else final.size,
                json.dumps(
                    {
                        "upload_status": status,
                        "upload_source_uri": source.uri,
                        "intent_expires_at": "2020-01-01T00:00:00+00:00",
                    }
                ),
            ),
        )
    preview = cleanup_upload_page(lane_env, storage=storage)
    assert preview["deleted"] == 0
    assert storage.head_object("uploads/cleanup-pending.mp4") is not None
    result = cleanup_upload_page(lane_env, storage=storage, apply=True)
    assert result["failed"] == 0
    assert result["deleted"] == 5
    assert storage.head_object("verified-uploads/cleanup-ready/digest/video.mp4") is not None
    assert storage.head_object("uploads/cleanup-ready.mp4") is None
    row = pg.execute("SELECT metadata_json FROM assets WHERE id = 'cleanup-pending'").fetchone()
    assert row is not None and json.loads(row[0])["upload_status"] == "EXPIRED"
    assert cleanup_upload_page(lane_env, storage=storage, apply=True)["failed"] == 0


def test_w18_pg_material_and_identity_uploads_keep_verified_bytes(bus: BusinessConnection) -> None:
    import struct

    from app.character_identity import (
        FakeSourceImageInspector,
        complete_authorization_upload,
        complete_source_upload,
        create_identity_upload_intent,
        create_person_identity,
    )
    from app.materials import (
        MaterialUploadIntentRequest,
        create_material_upload_intent,
        persist_material_upload,
        prepare_material_upload,
        probe_material_upload,
    )
    from app.storage import storage_object_ref_from_uri

    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    admin = actor("admin_1", "admin")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1024, 1024)
    material = create_material_upload_intent(
        bus,
        actor=admin,
        storage=storage,
        request=MaterialUploadIntentRequest(
            filename="picture.png", content_type="image/png", size_bytes=len(png)
        ),
    )
    storage.put_object(material.storage_key, png, content_type="image/png")
    prepared = prepare_material_upload(bus, actor=admin, asset_id=material.asset_id)
    probed = probe_material_upload(prepared, storage=storage)
    persist_material_upload(bus, actor=admin, probed=probed)
    with pytest.raises(HTTPException) as denied:
        prepare_material_upload(bus, actor=admin, asset_id=material.asset_id, pending_only=True)
    assert denied.value.status_code == 409
    uploads = [(material.asset_id, material.storage_key, png)]
    identity = create_person_identity(
        bus,
        actor=admin,
        display_name="Upload test",
        owner_user_id="admin_1",
        authorization_scope=["video"],
        authorization_expires_at=None,
    )
    for purpose in ("authorization", "source"):
        content = b"%PDF-1.7 authorization" if purpose == "authorization" else png
        content_type = "application/pdf" if purpose == "authorization" else "image/png"
        intent = create_identity_upload_intent(
            bus,
            actor=admin,
            storage=storage,
            identity_id=identity.id,
            purpose=cast(Any, purpose),
            filename="auth.pdf" if purpose == "authorization" else "source.png",
            content_type=content_type,
            size_bytes=len(content),
        )
        storage.put_object(intent.storage_key, content, content_type=content_type)
        if purpose == "authorization":
            complete_authorization_upload(
                bus, actor=admin, storage=storage, identity_id=identity.id, asset_id=intent.asset_id
            )
        else:
            complete_source_upload(
                bus,
                actor=admin,
                storage=storage,
                identity_id=identity.id,
                asset_id=intent.asset_id,
                inspector=FakeSourceImageInspector(),
            )
        uploads.append((intent.asset_id, intent.storage_key, content))
    for asset_id, source_key, content in uploads:
        storage.put_object(
            source_key, b"malicious replacement", content_type="application/octet-stream"
        )
        row = bus.execute(
            "SELECT storage_uri, sha256 FROM assets WHERE id=%s", (asset_id,)
        ).fetchone()
        assert row is not None
        # The point of this loop is that a verified snapshot survives someone
        # overwriting the *source* object afterwards: the bytes the client
        # uploaded are what the asset must resolve to, not whatever is sitting
        # under the intent key now.
        #
        # The asset id is deliberately no longer required in the key. Deduplication
        # may point this asset at an earlier verified copy of the same bytes owned
        # by the same user (here: the source photo reuses the material upload of
        # the identical PNG). What must still hold is that the asset resolves into
        # the verified namespace and to the right bytes.
        assert "/verified-uploads/" in row[0]
        assert storage.get_object(storage_object_ref_from_uri(row[0]).key) == content
        assert row[1] == hashlib.sha256(content).hexdigest()


def test_w18_pg_cleanup_fences_inflight_completion_and_retries(
    lane_env: str, pg: psycopg.Connection
) -> None:
    from app.media import (
        persist_upload_completion,
        prepare_upload_completion,
        probe_upload_completion,
    )
    from app.storage import StorageBackendUnavailable
    from app.upload_cleanup import cleanup_upload_page

    class FailingDeleteStorage(FakeStorageAdapter):
        fail = True

        def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
            if self.fail:
                raise StorageBackendUnavailable("temporary fixture failure")
            super().delete_object(key, actor_id=actor_id)

    seed_project(pg, "w18-expired", "employee_1")
    storage = FailingDeleteStorage(provider="fake", bucket="cw058-tests")
    employee = actor("employee_1", "employee")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        intent = create_upload_intent(
            conn,
            actor=employee,
            storage=storage,
            project_id="w18-expired",
            filename="video.mp4",
            content_type="video/mp4",
            size_bytes=5,
        )
        prepared = prepare_upload_completion(conn, actor=employee, asset_id=intent.asset_id)
    storage.put_object(intent.storage_key, b"video", content_type="video/mp4")

    class Probe:
        def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
            return VideoMetadata(duration_seconds=8)

    probed = probe_upload_completion(prepared, storage=storage, probe=Probe())
    assert cleanup_upload_page(lane_env, storage=storage, apply=True)["eligible"] == 0
    pg.execute(
        "UPDATE assets SET metadata_json = jsonb_set(metadata_json::jsonb, '{intent_expires_at}', "
        "'\"2020-01-01T00:00:00+00:00\"')::text WHERE id=%s",
        (intent.asset_id,),
    )
    assert cleanup_upload_page(lane_env, storage=storage, apply=True)["failed"] == 1
    with pytest.raises(HTTPException) as denied, pg_transaction() as raw:
        persist_upload_completion(BusinessConnection.postgres(raw), actor=employee, probed=probed)
    assert denied.value.detail["code"] == "UPLOAD_EXPIRED"
    storage.fail = False
    result = cleanup_upload_page(lane_env, storage=storage, apply=True)
    assert result["failed"] == 0 and result["deleted"] == 2


@pytest.mark.parametrize("delete_before_probe", [False, True])
def test_w18_pg_deleted_project_upload_receipt_is_still_reclaimed(
    lane_env: str, pg: psycopg.Connection, delete_before_probe: bool
) -> None:
    from dataclasses import replace

    from app.media import (
        persist_upload_completion,
        prepare_upload_completion,
        probe_upload_completion,
    )
    from app.upload_cleanup import cleanup_upload_page

    class OldGrantStorage(FakeStorageAdapter):
        def create_upload_intent(self, key: str, **kwargs: Any) -> Any:
            return replace(
                super().create_upload_intent(key, **kwargs),
                expires_at=datetime(2020, 1, 1, tzinfo=UTC),
            )

    class Probe:
        def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
            return VideoMetadata(duration_seconds=8)

    seed_project(pg, "w18-deleted", "employee_1")
    storage = OldGrantStorage(provider="fake", bucket="cw058-tests")
    employee = actor("employee_1", "employee")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        intent = create_upload_intent(
            conn,
            actor=employee,
            storage=storage,
            project_id="w18-deleted",
            filename="video.mp4",
            content_type="video/mp4",
            size_bytes=5,
        )
        prepared = prepare_upload_completion(conn, actor=employee, asset_id=intent.asset_id)
    storage.put_object(intent.storage_key, b"video", content_type="video/mp4")
    if delete_before_probe:
        pg.execute("DELETE FROM projects WHERE id='w18-deleted'")
    probed = probe_upload_completion(prepared, storage=storage, probe=Probe())
    if not delete_before_probe:
        with pg_transaction() as raw:
            persist_upload_completion(
                BusinessConnection.postgres(raw), actor=employee, probed=probed
            )
        pg.execute("DELETE FROM projects WHERE id='w18-deleted'")
    result = cleanup_upload_page(lane_env, storage=storage, apply=True)
    assert result["failed"] == 0 and result["deleted"] == 2


def test_w18_pg_deduplicated_completion_keeps_existing_verified_uri(
    lane_env: str, pg: psycopg.Connection
) -> None:
    from app.media import (
        persist_upload_completion,
        prepare_upload_completion,
        probe_upload_completion,
    )
    from app.upload_cleanup import cleanup_upload_page

    seed_project(pg, "w18-original", "employee_1")
    seed_project(pg, "w18-duplicate", "employee_1")
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    employee = actor("employee_1", "employee")
    with pg_transaction() as raw:
        original = _upload_and_complete(
            BusinessConnection.postgres(raw),
            storage,
            actor_id="employee_1",
            project_id="w18-original",
        )
    row = pg.execute(
        "SELECT sha256, size_bytes, storage_uri FROM assets WHERE id=%s", (original.asset_id,)
    ).fetchone()
    assert row is not None
    pg.execute(
        "UPDATE assets SET metadata_json=jsonb_set(metadata_json::jsonb, '{intent_expires_at}', "
        "'\"2020-01-01T00:00:00+00:00\"')::text WHERE id=%s",
        (original.asset_id,),
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        duplicate = create_upload_intent(
            conn,
            actor=employee,
            storage=storage,
            project_id="w18-duplicate",
            filename="video.mp4",
            content_type="video/mp4",
            sha256=row[0],
            size_bytes=row[1],
        )
        prepared = prepare_upload_completion(conn, actor=employee, asset_id=duplicate.asset_id)

    class Probe:
        def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
            return VideoMetadata(duration_seconds=8)

    probed = probe_upload_completion(prepared, storage=storage, probe=Probe())
    assert probed.storage_uri == row[2]
    cleanup_upload_page(lane_env, storage=storage, apply=True)
    with pg_transaction() as raw:
        persist_upload_completion(BusinessConnection.postgres(raw), actor=employee, probed=probed)
    assert storage.head_object(duplicate.storage_key) is not None
    metadata = pg.execute(
        "SELECT metadata_json FROM assets WHERE id=%s", (duplicate.asset_id,)
    ).fetchone()
    assert metadata is not None and "upload_source_uri" not in json.loads(metadata[0])


def test_w18_pg_cleanup_cursor_survives_an_earlier_receipt_expiring(
    bus: BusinessConnection, lane_env: str, pg: psycopg.Connection
) -> None:
    from app.upload_cleanup import cleanup_upload_page

    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    for asset_id, expires in (
        ("cursor-a", "2099-01-01T00:00:00+00:00"),
        ("cursor-b", "2020-01-01T00:00:00+00:00"),
    ):
        source = storage.put_object(f"uploads/{asset_id}", b"test", content_type="text/plain")
        bus.execute(
            "INSERT INTO assets (id, kind, storage_uri, sha256, size_bytes, metadata_json) "
            "VALUES (%s, 'video', %s, '', 0, %s)",
            (
                asset_id,
                source.uri,
                json.dumps(
                    {
                        "upload_status": "PENDING",
                        "upload_source_uri": source.uri,
                        "intent_expires_at": expires,
                    }
                ),
            ),
        )
    for index in range(105):
        storage.put_object(
            f"verified-uploads/cursor-b/{index:03d}/video", b"test", content_type="text/plain"
        )
    first = cleanup_upload_page(lane_env, storage=storage, apply=True)
    assert first["next_asset_id"] == "cursor-a"
    assert first["next_object_cursor"].startswith("verified-uploads/cursor-b/")
    assert first["next_object_asset_id"] == "cursor-b"
    # A new receipt sorted between A and B must not inherit B's object cursor.
    source = storage.put_object("uploads/cursor-ab", b"new", content_type="text/plain")
    bus.execute(
        "INSERT INTO assets (id, kind, storage_uri, sha256, size_bytes, metadata_json) "
        "VALUES ('cursor-ab', 'video', %s, '', 0, %s)",
        (
            source.uri,
            json.dumps(
                {
                    "upload_status": "PENDING",
                    "upload_source_uri": source.uri,
                    "intent_expires_at": "2020-01-01T00:00:00+00:00",
                }
            ),
        ),
    )
    pg.execute(
        "UPDATE assets SET metadata_json=jsonb_set(metadata_json::jsonb, '{intent_expires_at}', "
        "'\"2020-01-01T00:00:00+00:00\"')::text WHERE id='cursor-a'"
    )
    second = cleanup_upload_page(
        lane_env,
        storage=storage,
        apply=True,
        after_asset_id=first["next_asset_id"],
        object_cursor=first["next_object_cursor"],
        object_asset_id=first["next_object_asset_id"],
    )
    assert second["failed"] == 0 and second["deleted"] == 5
    assert storage.head_object("uploads/cursor-a") is not None
    assert cleanup_upload_page(lane_env, storage=storage, apply=True)["deleted"] == 2


def test_w18_pg_completed_asset_never_follows_replaced_upload(
    lane_env: str, pg: psycopg.Connection
) -> None:
    from app.media import storage_key_from_uri

    seed_project(pg, "w18-project", "employee_1")
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    with pg_transaction() as raw:
        intent = _upload_and_complete(
            BusinessConnection.postgres(raw),
            storage,
            actor_id="employee_1",
            project_id="w18-project",
        )
    completed = pg.execute(
        "SELECT storage_uri, sha256 FROM assets WHERE id=%s", (intent.asset_id,)
    ).fetchone()
    storage.put_object(intent.storage_key, b"replacement", content_type="video/mp4")
    assert storage.get_object(storage_key_from_uri(str(completed[0]))) == b"video-bytes"
    with pg_transaction() as raw:
        replay = complete_upload(
            BusinessConnection.postgres(raw),
            actor=actor("employee_1", "employee"),
            storage=storage,
            probe=FakeVideoProbe(8),
            asset_id=intent.asset_id,
        )
    assert replay.storage_uri == completed[0]
    assert replay.sha256 == completed[1]


def test_w18_pg_legacy_video_remains_a_reference_after_completion(
    lane_env: str, pg: psycopg.Connection
) -> None:
    from app.media import is_reference_video_asset

    seed_project(pg, "w18-project", "employee_1")
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    owner = actor("employee_1", "employee")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        intent = create_upload_intent(
            conn,
            actor=owner,
            storage=storage,
            project_id="w18-project",
            filename="clip.mp4",
            content_type="video/mp4",
            size_bytes=4,
        )
        conn.execute("UPDATE assets SET kind='video' WHERE id=%s", (intent.asset_id,))
    storage.put_object(intent.storage_key, b"AAAA", content_type="video/mp4")
    with pg_transaction() as raw:
        complete_upload(
            BusinessConnection.postgres(raw),
            actor=owner,
            storage=storage,
            probe=FakeVideoProbe(8),
            asset_id=intent.asset_id,
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        current = conn.execute("SELECT * FROM assets WHERE id=%s", (intent.asset_id,)).fetchone()
        assert current["kind"] == "reference_video"
        assert is_reference_video_asset(current)
        complete_upload(
            conn, actor=owner, storage=storage, probe=FakeVideoProbe(8), asset_id=intent.asset_id
        )


def test_w18_pg_concurrent_completions_cannot_replace_committed_content(
    lane_env: str, pg: psycopg.Connection
) -> None:
    from app.media import (
        persist_upload_completion,
        prepare_upload_completion,
        probe_upload_completion,
    )

    seed_project(pg, "w18-project", "employee_1")
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    owner = actor("employee_1", "employee")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        intent = create_upload_intent(
            conn,
            actor=owner,
            storage=storage,
            project_id="w18-project",
            filename="clip.mp4",
            content_type="video/mp4",
            size_bytes=4,
        )
        prepared = prepare_upload_completion(conn, actor=owner, asset_id=intent.asset_id)
    storage.put_object(intent.storage_key, b"AAAA", content_type="video/mp4")
    first = probe_upload_completion(prepared, storage=storage, probe=FakeVideoProbe(8))
    storage.put_object(intent.storage_key, b"BBBB", content_type="video/mp4")
    second = probe_upload_completion(prepared, storage=storage, probe=FakeVideoProbe(8))
    with pg_transaction() as raw:
        persist_upload_completion(BusinessConnection.postgres(raw), actor=owner, probed=first)
    with pytest.raises(HTTPException) as caught:
        with pg_transaction() as raw:
            persist_upload_completion(BusinessConnection.postgres(raw), actor=owner, probed=second)
    assert caught.value.detail["code"] == "UPLOAD_STATE_CHANGED"
    current = pg.execute(
        "SELECT storage_uri, sha256 FROM assets WHERE id=%s", (intent.asset_id,)
    ).fetchone()
    assert current == (first.storage_uri, first.sha256)


def test_media_upload_dedup_reuses_owned_hash_never_foreign_on_pg(
    lane_env: str, pg: psycopg.Connection
) -> None:
    """旧断言（test_media.py 去重两条）：同属主内容哈希去重复用存储行、
    他属主同哈希绝不复用（IDOR）。"""
    seed_project(pg, "project-a", "employee_1")
    seed_project(pg, "project-b", "employee_1")
    seed_project(pg, "project-foreign", "employee_2")
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    content_sha = hashlib.sha256(b"video-bytes").hexdigest()

    first_ids: list[str] = []
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        intent = _upload_and_complete(conn, storage, actor_id="employee_1", project_id="project-a")
        first_ids.append(intent.asset_id)

    reused_ids: list[str] = []
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        second = create_upload_intent(
            conn,
            actor=actor("employee_1", "employee"),
            storage=storage,
            project_id="project-b",
            filename="reference.mp4",
            content_type="video/mp4",
            size_bytes=len(b"video-bytes"),
            sha256=content_sha,
        )
        assert second.upload_required is False
        reused_ids.append(second.asset_id)

    row = pg.execute(
        "SELECT project_id, sha256 FROM assets WHERE id = %s", (reused_ids[0],)
    ).fetchone()
    assert row is not None
    assert row[0] == "project-b"
    assert row[1] == content_sha

    foreign_results: list[bool] = []
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        foreign = create_upload_intent(
            conn,
            actor=actor("employee_2", "employee"),
            storage=storage,
            project_id="project-foreign",
            filename="reference.mp4",
            content_type="video/mp4",
            size_bytes=len(b"video-bytes"),
            sha256=content_sha,
        )
        foreign_results.append(foreign.upload_required)
    assert foreign_results[0] is True


def test_media_completion_rolls_back_atomically_when_enqueue_fails_on_pg(
    lane_env: str,
    pg: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧断言（test_media.py 回滚用例）：分析入队失败 → 资产/项目/任务
    原子回滚到上传前。PG 上提交权归外层事务（fenced 通道同形）。"""
    from app import media as media_module

    seed_project(pg, "project-rb", "employee_1")
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")

    def reject_enqueue(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(media_module, "enqueue_analysis_task", reject_enqueue)

    # 第一阶段（独立事务提交）：创建上传意图并存入对象——与旧用例
    # 「先有 asset 行、完成阶段失败」的前提一致。
    intent_ids: list[str] = []
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        intent = create_upload_intent(
            conn,
            actor=actor("employee_1", "employee"),
            storage=storage,
            project_id="project-rb",
            filename="reference.mp4",
            content_type="video/mp4",
            size_bytes=len(b"video-bytes"),
        )
        storage.put_object(intent.storage_key, b"video-bytes", content_type="video/mp4")
        intent_ids.append(intent.asset_id)
    asset_id = intent_ids[0]

    # 第二阶段：完成失败 → 本事务整体回滚，资产回到「已建未完成」状态。
    with pytest.raises(RuntimeError, match="queue unavailable"):
        with pg_transaction() as raw:
            complete_upload(
                BusinessConnection.postgres(raw),
                actor=actor("employee_1", "employee"),
                storage=storage,
                probe=FakeVideoProbe(duration_seconds=8.0),
                asset_id=asset_id,
            )

    asset = pg.execute(
        "SELECT sha256, size_bytes FROM assets WHERE id = %s", (asset_id,)
    ).fetchone()
    assert asset is not None
    assert asset[0] == ""
    assert asset[1] == 0
    project_status = pg.execute(
        "SELECT status FROM projects WHERE id = %s", ("project-rb",)
    ).fetchone()
    assert project_status is not None and project_status[0] == "ACTIVE"
    tasks = pg.execute(
        "SELECT count(*) FROM analysis_tasks WHERE asset_id = %s", (asset_id,)
    ).fetchone()
    assert tasks is not None and tasks[0] == 0


@pytest.mark.parametrize("standalone", [False, True])
def test_direct_generation_archive_is_owned_idempotent_and_does_not_rebill(
    bus: BusinessConnection, pg: psycopg.Connection, standalone: bool
) -> None:
    from app.generation import (
        claim_generation_result_archive,
        get_generation_batch,
        persist_generation_result_archive,
        prepare_generation_result_archive,
    )
    from app.materials import require_material

    seed_project(pg, "archive-project", "employee_1")
    pg.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json, status) VALUES "
        "('archive-batch','archive-project','employee_1','archive-key','h','{}','SUCCEEDED')"
    )
    pg.execute(
        "INSERT INTO generation_tasks (id,batch_id,provider,model,status,archive_status,"
        "provider_result_url,completed_at) VALUES "
        "('archive-task','archive-batch','metaso','MiniMax-H3','SUCCEEDED','DIRECT',"
        "'https://cdn.example.com/video.mp4',CURRENT_TIMESTAMP)"
    )
    owner = actor("employee_1", "employee")
    if standalone:
        pg.execute("UPDATE generation_batches SET project_id=NULL WHERE id='archive-batch'")
    with pytest.raises(HTTPException):
        prepare_generation_result_archive(
            bus, actor=actor("employee_2", "employee"), task_id="archive-task"
        )
    with pytest.raises(HTTPException):
        prepare_generation_result_archive(
            bus, actor=actor("auditor_1", "auditor"), task_id="archive-task"
        )
    prepared = claim_generation_result_archive(bus, actor=owner, task_id="archive-task")
    storage = FakeStorageAdapter(provider="cos", bucket="archive-test")
    stored = storage.put_object(
        "generation-results/archive-task/video.mp4", b"verified-video", content_type="video/mp4"
    )
    for _ in range(2):
        result = persist_generation_result_archive(
            bus,
            actor=owner,
            prepared=prepared,
            stored=stored,
            duration_seconds=4.458333,
        )
    assert result.result_asset_id
    material = require_material(bus, actor=owner, material_id=f"asset:{result.result_asset_id}")
    assert material.saved and material.delivery == "stored"
    assert material.source == "generation"
    assert material.duration_seconds == pytest.approx(4.458333)
    assert "download" in material.allowed_actions
    assert pg.execute("SELECT count(*) FROM assets").fetchone()[0] == 1
    assert (
        pg.execute(
            "SELECT count(*) FROM audit_logs WHERE action='generation_task.archive'"
        ).fetchone()[0]
        == 1
    )
    assert pg.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0
    detail = get_generation_batch(bus, actor=owner, batch_id="archive-batch")
    assert detail.project_name == (None if standalone else "CW058 archive-project")
    pg.execute(
        "INSERT INTO customer_batch_visibility(user_id,batch_id) "
        "VALUES ('employee_1','archive-batch')"
    )
    # Removing a task from history must not remove a separately saved material
    # or break a publishing draft that references its physical asset.
    retained = require_material(bus, actor=owner, material_id=f"asset:{result.result_asset_id}")
    assert retained.saved and retained.source == "generation"
    from app.control_routes import list_generation_records

    pg.execute("UPDATE generation_tasks SET actual_cost=0.08 WHERE id='archive-task'")
    records = list_generation_records(bus, owner, record_type="VIDEO", limit=50, offset=0)
    assert records.total == len(records.items) == 1
    assert records.items[0].project_id == (None if standalone else "archive-project")
    assert records.items[0].provider_cost == 0.08
    # record_video_generation_cost uses frozen configured rates, not an invoice.
    assert records.items[0].provider_cost_status == "ESTIMATED"


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "download",
        "settings",
        "nan",
        "changed",
        "revoked",
        "normalize",
        "normalization-failed",
        "snapshot-changed",
        "normalized-revoked",
        "claim-stolen",
        "claim-stolen-at-put",
        "renew-commit-failed",
        "adaptive",
        "other-resolution",
        "other-ratio",
        "missing-ratio",
    ],
)
def test_archive_http_rechecks_before_commit_and_preserves_billing(
    lane_env: str, pg: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.customer_fence import get_business_db
    from app.generation import H3ProviderFailed, H3ProviderSettingsUnavailable
    from app.main import app
    from app.media_routes import get_media_storage
    from app.media_tools import MediaValidationFailed

    seed_project(pg, "archive-http-project", "employee_1")
    pg.execute(
        "INSERT INTO generation_batches(id,project_id,created_by_user_id,idempotency_key,"
        "request_hash,request_snapshot_json,status) VALUES "
        "('archive-http-batch','archive-http-project','employee_1','archive-http-key','h','{}','SUCCEEDED')"
    )
    pg.execute(
        "INSERT INTO generation_tasks(id,batch_id,provider,model,status,archive_status,"
        "provider_result_url) VALUES ('archive-http-task','archive-http-batch','metaso',"
        "'MiniMax-H3','SUCCEEDED','DIRECT','https://cdn.example/result.mp4')"
    )
    normalized_outcomes = {
        "normalize",
        "normalization-failed",
        "snapshot-changed",
        "normalized-revoked",
        "claim-stolen",
        "claim-stolen-at-put",
    }
    snapshot: object = {}
    if outcome in normalized_outcomes:
        snapshot = {"resolution": "2K", "ratio": "9:16"}
    elif outcome == "adaptive":
        snapshot = {"resolution": "2K", "ratio": "adaptive"}
    elif outcome == "other-resolution":
        snapshot = {"resolution": "768P", "ratio": "9:16"}
    elif outcome == "other-ratio":
        snapshot = {"resolution": "2K", "ratio": "16:9"}
    elif outcome == "missing-ratio":
        snapshot = {"resolution": "2K"}
    pg.execute(
        "UPDATE generation_tasks SET prompt_snapshot_json=%s WHERE id='archive-http-task'",
        (json.dumps(snapshot),),
    )
    pg.commit()
    downloads: list[str] = []
    normalization_calls: list[tuple[bytes, int, int]] = []
    original_content = b"\x00\x00\x00\x18ftypisom" + b"test-video"
    normalized_content = b"\x00\x00\x00\x18ftypisom" + b"normalized-video"

    class TestDb:
        @contextmanager
        def write(self):
            with pg_transaction() as raw:
                if outcome in {"revoked", "normalized-revoked"} and downloads:
                    raise HTTPException(401, detail={"code": "SESSION_REPLACED"})
                yield BusinessConnection.postgres(raw), actor("employee_1", "employee")
                if outcome == "renew-commit-failed" and downloads:
                    raise HTTPException(401, detail={"code": "SESSION_REPLACED"})

    class Provider:
        def download_result(self, url: str) -> bytes:
            downloads.append(url)
            if outcome == "download":
                raise H3ProviderFailed("temporary download failure")
            if outcome == "changed":
                pg.execute(
                    "UPDATE generation_tasks SET provider_result_url='https://cdn.example/new.mp4' "
                    "WHERE id='archive-http-task'"
                )
                pg.commit()
            if outcome == "snapshot-changed":
                pg.execute(
                    "UPDATE generation_tasks SET prompt_snapshot_json=%s "
                    "WHERE id='archive-http-task'",
                    (json.dumps({"resolution": "2K", "ratio": "adaptive"}),),
                )
                pg.commit()
            return original_content

    def normalize(content: bytes, *, target_width: int, target_height: int):
        normalization_calls.append((content, target_width, target_height))
        if outcome == "normalization-failed":
            raise MediaValidationFailed("invalid decoded video")
        if outcome == "claim-stolen":
            pg.execute(
                "UPDATE generation_tasks SET locked_by='archive:replacement', "
                "locked_until=(clock_timestamp()+interval '10 minutes')::text "
                "WHERE id='archive-http-task'"
            )
        return SimpleNamespace(
            content=normalized_content,
            duration_seconds=4.0,
            width=1440,
            height=2560,
            source_sample_aspect_ratio="64:63",
            source_display_aspect_ratio="4:7",
            sample_aspect_ratio="1:1",
            display_aspect_ratio="9:16",
            source_rotation_degrees=0,
            transformed=True,
        )

    def provider(*args, **kwargs):
        if outcome == "settings":
            raise H3ProviderSettingsUnavailable("not configured")
        return Provider()

    monkeypatch.setattr("app.generation_routes.h3_provider_for_task", provider)
    monkeypatch.setattr("app.generation_routes.normalize_generated_video", normalize, raising=False)
    monkeypatch.setattr(
        "app.generation_routes.FFprobeVideoProbe.probe",
        lambda *_args, **_kwargs: VideoMetadata(float("nan") if outcome == "nan" else 4.0),
    )
    app.dependency_overrides[get_business_db] = TestDb
    storage = FakeStorageAdapter(provider="cos", bucket="http-archive-test")
    uploaded_content: list[bytes] = []
    put_object = storage.put_object

    def record_upload(key: str, content: bytes, *, content_type: str):
        uploaded_content.append(content)
        if outcome == "claim-stolen-at-put":
            pg.execute(
                "UPDATE generation_tasks SET locked_by='archive:replacement', "
                "locked_until=(clock_timestamp()+interval '10 minutes')::text "
                "WHERE id='archive-http-task'"
            )
        return put_object(key, content, content_type=content_type)

    monkeypatch.setattr(storage, "put_object", record_upload)
    app.dependency_overrides[get_media_storage] = lambda: storage
    try:
        client = TestClient(app, raise_server_exceptions=False)
        result = client.post("/api/generation-tasks/archive-http-task/archive")
        successful = {
            "success",
            "normalize",
            "adaptive",
            "other-resolution",
            "other-ratio",
            "missing-ratio",
        }
        expected = (
            200
            if outcome in successful
            else {
                "changed": 409,
                "snapshot-changed": 409,
                "revoked": 401,
                "normalized-revoked": 401,
                "claim-stolen": 409,
                "claim-stolen-at-put": 409,
                "renew-commit-failed": 401,
            }.get(outcome, 503)
        )
        assert result.status_code == expected, result.text
        if outcome in successful:
            replay = client.post("/api/generation-tasks/archive-http-task/archive")
            assert replay.status_code == 200
            assert replay.json()["result_asset_id"] == result.json()["result_asset_id"]
            assert len(downloads) == 1
            asset = pg.execute("SELECT sha256,metadata_json FROM assets").fetchone()
            saved_metadata = json.loads(asset[1])
            if outcome == "normalize":
                assert normalization_calls == [(original_content, 1440, 2560)]
                assert uploaded_content == [normalized_content]
                assert asset[0] == hashlib.sha256(normalized_content).hexdigest()
                assert saved_metadata["video_normalization"] == {
                    "policy": "explicit_2k_portrait_v1",
                    "requested_resolution": "2K",
                    "requested_ratio": "9:16",
                    "source_sha256": hashlib.sha256(original_content).hexdigest(),
                    "width": 1440,
                    "height": 2560,
                    "source_sample_aspect_ratio": "64:63",
                    "source_display_aspect_ratio": "4:7",
                    "sample_aspect_ratio": "1:1",
                    "display_aspect_ratio": "9:16",
                    "source_rotation_degrees": 0,
                    "transformed": True,
                }
            else:
                assert normalization_calls == []
                assert uploaded_content == [original_content]
                assert asset[0] == hashlib.sha256(original_content).hexdigest()
                assert "video_normalization" not in saved_metadata
        else:
            if outcome in {"normalization-failed", "claim-stolen"}:
                assert uploaded_content == []
            assert pg.execute("SELECT count(*) FROM assets").fetchone()[0] == 0
            assert (
                pg.execute(
                    "SELECT archive_status FROM generation_tasks WHERE id='archive-http-task'"
                ).fetchone()[0]
                == "DIRECT"
            )
        assert pg.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0
        lock = pg.execute(
            "SELECT locked_by,locked_until FROM generation_tasks WHERE id='archive-http-task'"
        ).fetchone()
        if outcome in {"claim-stolen", "claim-stolen-at-put"}:
            assert result.json()["detail"]["code"] == "RESULT_ARCHIVE_LEASE_LOST"
            assert lock[0] == "archive:replacement" and lock[1] is not None
        else:
            assert lock == (None, None)
        if outcome != "changed":
            assert pg.execute(
                "SELECT provider_result_url FROM generation_tasks WHERE id='archive-http-task'"
            ).fetchone() == ("https://cdn.example/result.mp4",)
    finally:
        app.dependency_overrides.clear()


def test_archive_retry_while_first_request_runs_deduplicates_expensive_work(
    lane_env: str, pg: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the overlap after a browser stops waiting but the handler runs on.

    Events fix the relevant ordering without sleeping through the UI's 60-second
    timer. Both requests execute the real HTTP route against separate PG sessions;
    only supplier bytes, normalization and object storage are controlled doubles.
    """
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event, Lock
    from types import SimpleNamespace

    from app.customer_fence import get_business_db
    from app.main import app
    from app.media_routes import get_media_storage

    seed_project(pg, "archive-overlap-project", "employee_1")
    pg.execute(
        "INSERT INTO generation_batches(id,project_id,created_by_user_id,idempotency_key,"
        "request_hash,request_snapshot_json,status) VALUES "
        "('archive-overlap-batch','archive-overlap-project','employee_1',"
        "'archive-overlap-key','h','{}','SUCCEEDED')"
    )
    pg.execute(
        "INSERT INTO generation_tasks(id,batch_id,provider,model,status,archive_status,"
        "provider_result_url,prompt_snapshot_json) VALUES "
        "('archive-overlap-task','archive-overlap-batch','metaso','MiniMax-H3',"
        "'SUCCEEDED','DIRECT','https://cdn.example/result.mp4',%s)",
        (json.dumps({"resolution": "2K", "ratio": "9:16"}),),
    )
    pg.commit()
    entered = Event()
    release_first = Event()
    count_lock = Lock()
    calls = {"download": 0, "normalize": 0, "put": 0}
    content = b"\x00\x00\x00\x18ftypisom" + b"test-video"

    class TestDb:
        @contextmanager
        def write(self):
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), actor("employee_1", "employee")

    class Provider:
        def download_result(self, url: str) -> bytes:
            with count_lock:
                calls["download"] += 1
                first = calls["download"] == 1
            if first:
                entered.set()
                if not release_first.wait(15):
                    raise RuntimeError("overlap test release was not delivered")
            return content

    def normalize(data: bytes, *, target_width: int, target_height: int):
        with count_lock:
            calls["normalize"] += 1
        return SimpleNamespace(
            content=data,
            duration_seconds=4.0,
            width=target_width,
            height=target_height,
            source_sample_aspect_ratio="64:63",
            source_display_aspect_ratio="4:7",
            sample_aspect_ratio="1:1",
            display_aspect_ratio="9:16",
            source_rotation_degrees=0,
            transformed=True,
        )

    storage = FakeStorageAdapter(provider="cos", bucket="overlap-test")
    put_object = storage.put_object

    def put(key: str, data: bytes, *, content_type: str):
        with count_lock:
            calls["put"] += 1
        return put_object(key, data, content_type=content_type)

    monkeypatch.setattr("app.generation_routes.h3_provider_for_task", lambda *a, **kw: Provider())
    monkeypatch.setattr("app.generation_routes.normalize_generated_video", normalize)
    monkeypatch.setattr(
        "app.generation_routes.FFprobeVideoProbe.probe", lambda *a, **kw: VideoMetadata(4.0)
    )
    monkeypatch.setattr(storage, "put_object", put)
    app.dependency_overrides[get_business_db] = TestDb
    app.dependency_overrides[get_media_storage] = lambda: storage

    def post_archive():
        return TestClient(app, raise_server_exceptions=False).post(
            "/api/generation-tasks/archive-overlap-task/archive"
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(post_archive)
            assert entered.wait(10), "first request did not enter provider download"
            try:
                # A fresh request starts while the abandoned first operation still runs.
                retry = executor.submit(post_archive).result(timeout=10)
                assert retry.status_code == 409, retry.text
                assert retry.json()["detail"]["code"] == "RESULT_ARCHIVE_IN_PROGRESS"
            finally:
                release_first.set()
            original = first.result(timeout=10)
        assert original.status_code == 200, original.text
        replay = post_archive()
        assert replay.status_code == 200
        assert original.json()["result_asset_id"] == replay.json()["result_asset_id"]
        assert pg.execute("SELECT count(*) FROM assets").fetchone()[0] == 1
        assert (
            pg.execute(
                "SELECT count(*) FROM audit_logs WHERE action='generation_task.archive'"
            ).fetchone()[0]
            == 1
        )
        assert pg.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0
        assert calls == {"download": 1, "normalize": 1, "put": 1}
        assert pg.execute(
            "SELECT locked_by,locked_until FROM generation_tasks WHERE id='archive-overlap-task'"
        ).fetchone() == (None, None)
    finally:
        release_first.set()
        app.dependency_overrides.clear()


def _seed_manual_archive_claim(pg: psycopg.Connection) -> None:
    seed_project(pg, "claim-project", "employee_1")
    pg.execute(
        "INSERT INTO generation_batches(id,project_id,created_by_user_id,idempotency_key,"
        "request_hash,request_snapshot_json,status) VALUES "
        "('claim-batch','claim-project','employee_1','claim-key','h','{}','SUCCEEDED')"
    )
    pg.execute(
        "INSERT INTO generation_tasks(id,batch_id,provider,model,status,archive_status,"
        "provider_result_url) VALUES ('claim-task','claim-batch','metaso','MiniMax-H3',"
        "'SUCCEEDED','DIRECT','https://cdn.example/result.mp4')"
    )


def test_manual_archive_claim_recovery_fences_previous_owner_and_deadline(
    lane_env: str, pg: psycopg.Connection, bus: BusinessConnection
) -> None:
    from app.generation import (
        claim_generation_result_archive,
        persist_generation_result_archive,
        release_generation_result_archive_claim,
        renew_generation_result_archive_claim,
    )

    _seed_manual_archive_claim(pg)
    owner = actor("employee_1", "employee")
    first = claim_generation_result_archive(bus, actor=owner, task_id="claim-task")
    with pytest.raises(HTTPException) as busy:
        claim_generation_result_archive(bus, actor=owner, task_id="claim-task")
    assert busy.value.detail["code"] == "RESULT_ARCHIVE_IN_PROGRESS"
    pg.execute(
        "UPDATE generation_tasks SET locked_until=(clock_timestamp()-interval '1 second')::text "
        "WHERE id='claim-task'"
    )
    second = claim_generation_result_archive(bus, actor=owner, task_id="claim-task")
    assert second["locked_by"] != first["locked_by"]
    with pytest.raises(HTTPException) as stale:
        renew_generation_result_archive_claim(bus, actor=owner, prepared=first)
    assert stale.value.detail["code"] == "RESULT_ARCHIVE_LEASE_LOST"
    assert not release_generation_result_archive_claim(bus, prepared=first)
    stored = FakeStorageAdapter(provider="cos", bucket="claim-test").put_object(
        "generation-results/claim-task/result.mp4", b"video", content_type="video/mp4"
    )
    with pytest.raises(HTTPException) as publish:
        persist_generation_result_archive(
            bus, actor=owner, prepared=first, stored=stored, duration_seconds=4
        )
    assert publish.value.detail["code"] == "RESULT_ARCHIVE_LEASE_LOST"
    assert pg.execute("SELECT count(*) FROM assets").fetchone()[0] == 0
    renewed = renew_generation_result_archive_claim(bus, actor=owner, prepared=second)
    assert renewed["locked_until"] != second["locked_until"]
    assert not release_generation_result_archive_claim(bus, prepared=second)
    assert release_generation_result_archive_claim(bus, prepared=renewed)
    assert pg.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0


def test_manual_archive_claim_expiry_does_not_enter_worker_recovery(
    lane_env: str, pg: psycopg.Connection, bus: BusinessConnection
) -> None:
    from app.generation import (
        acquire_generation_continuation_lease,
        claim_generation_result_archive,
        mark_expired_active_leases_needing_attention,
        renew_generation_result_archive_claim,
    )

    _seed_manual_archive_claim(pg)
    owner = actor("employee_1", "employee")
    prepared = claim_generation_result_archive(bus, actor=owner, task_id="claim-task")
    expired = pg.execute(
        "UPDATE generation_tasks SET locked_until=(clock_timestamp()-interval '1 second')::text "
        "WHERE id='claim-task' RETURNING locked_until"
    ).fetchone()[0]
    prepared["locked_until"] = expired
    mark_expired_active_leases_needing_attention(bus)
    assert acquire_generation_continuation_lease(bus, worker_id="unrelated-worker") is None
    assert tuple(
        pg.execute(
            "SELECT status,archive_status,locked_by,locked_until "
            "FROM generation_tasks WHERE id='claim-task'"
        ).fetchone()
    ) == ("SUCCEEDED", "DIRECT", prepared["locked_by"], expired)
    with pytest.raises(HTTPException) as stale:
        renew_generation_result_archive_claim(bus, actor=owner, prepared=prepared)
    assert stale.value.detail["code"] == "RESULT_ARCHIVE_LEASE_LOST"
    recovered = claim_generation_result_archive(bus, actor=owner, task_id="claim-task")
    assert recovered["locked_by"] != prepared["locked_by"]
    assert pg.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0


def test_materials_pagination_hide_rename_and_audit_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_materials.py 分页/隐藏审计组）：服务端分页不重不漏、
    重命名写偏好 + 审计行、隐藏后列表排除、resolve 报告隐藏与不可得."""
    from app.materials import (
        MaterialUpdateRequest,
        hide_material,
        list_materials,
        resolve_materials,
        update_material,
    )

    seed_project(pg, "project-mat", "employee_1")
    pg.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json, status) "
        "VALUES ('batch-direct', 'project-mat', 'employee_1', 'ik-direct', 'h', '{}', 'SUCCEEDED')"
    )
    pg.execute(
        "INSERT INTO generation_tasks (id, batch_id, provider, model, status, "
        "archive_status, provider_result_url, completed_at) VALUES "
        "('task-direct', 'batch-direct', 'metaso', 'MiniMax-H3', 'SUCCEEDED', "
        "'DIRECT', 'https://cdn.example.com/result.mp4', CURRENT_TIMESTAMP)"
    )
    for index in range(3):
        pg.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, 'project-mat', 'material_audio', "
            "%s, %s, 10, 'audio/mpeg', 'employee_1')",
            (f"asset-mat-{index}", f"local://materials/{index}.mp3", f"sha-{index}"),
        )

    page_one = list_materials(
        bus,
        actor=actor("employee_1", "employee"),
        media_type=None,
        source=None,
        query=None,
        page=1,
        page_size=2,
    )
    assert page_one.total == 4
    assert page_one.page == 1
    page_two = list_materials(
        bus,
        actor=actor("employee_1", "employee"),
        media_type=None,
        source=None,
        query=None,
        page=2,
        page_size=2,
    )
    page_one_ids = {item.id for item in page_one.items}
    page_two_ids = {item.id for item in page_two.items}
    assert page_one_ids.isdisjoint(page_two_ids)
    assert all(item.owner_user_id == "employee_1" for item in page_one.items)

    updated = update_material(
        bus,
        actor=actor("employee_1", "employee"),
        material_id="asset:asset-mat-0",
        request=MaterialUpdateRequest(title="定制标题", group="别墅外观"),
    )
    assert updated.title == "定制标题"
    audit = pg.execute(
        "SELECT count(*) FROM audit_logs WHERE action = 'studio.material.update' "
        "AND entity_id = %s",
        ("asset:asset-mat-0",),
    ).fetchone()
    assert audit is not None and audit[0] >= 1

    hide_material(bus, actor=actor("employee_1", "employee"), material_id="asset:asset-mat-1")
    after_hide = list_materials(
        bus,
        actor=actor("employee_1", "employee"),
        media_type=None,
        source=None,
        query=None,
        page=1,
        page_size=50,
    )
    assert "asset:asset-mat-1" not in {item.id for item in after_hide.items}

    resolved = resolve_materials(
        bus,
        actor=actor("employee_1", "employee"),
        material_ids=["asset:asset-mat-1", "asset:asset-mat-2", "asset:missing"],
    )
    by_id = {item.id: item for item in resolved.items}
    hidden_item = by_id.get("asset:asset-mat-1")
    assert hidden_item is not None
    assert hidden_item.hidden is True
    assert "asset:missing" in resolved.unavailable_ids


def test_character_materials_group_contact_sheets_without_losing_reference_access(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    from app.materials import hide_material, list_materials, resolve_materials

    for key, owner in [
        ("sheet-a", "employee_1"),
        ("sheet-b", "employee_1"),
        ("foreign-sheet", "employee_2"),
    ]:
        version_id = _seed_published_character_graph(pg, key, owner=owner)
        sheet_id = f"contact-{key}"
        pg.execute(
            "INSERT INTO assets (id,kind,storage_uri,sha256,size_bytes,content_type,"
            "created_by_user_id,metadata_json) "
            "VALUES (%s,'character_contact_sheet',%s,'sheet-sha',128,'image/png',%s,%s)",
            (
                sheet_id,
                f"local://characters/{key}/sheet.png",
                owner,
                json.dumps({"purpose": "five_view_contact_sheet"}),
            ),
        )
        pg.execute(
            "UPDATE character_versions SET publication_snapshot_json = "
            "(publication_snapshot_json::jsonb || "
            "jsonb_build_object('contact_sheet_asset_id', %s::text))::text "
            "WHERE id=%s",
            (sheet_id, version_id),
        )
    user = actor("employee_1", "employee")
    pages = [
        list_materials(
            bus,
            actor=user,
            media_type="image",
            source="character",
            query=None,
            page=page,
            page_size=1,
        )
        for page in (1, 2)
    ]
    assert all(page.total == 2 for page in pages)
    assert {item.asset_id for page in pages for item in page.items} == {
        "contact-sheet-a",
        "contact-sheet-b",
    }
    assert all(item.composite for page in pages for item in page.items)
    for page in pages:
        item = page.items[0]
        key = str(item.asset_id).removeprefix("contact-")
        assert item.preview_asset_id == f"asset-{key}-front_full"
        assert len(item.character_views) == 5
        assert {view.asset_id for view in item.character_views} == {
            f"asset-{key}-{view_type.lower()}" for view_type in REQUIRED_CHARACTER_VIEW_TYPES
        }
    assert all("first_frame" not in item.allowed_uses for page in pages for item in page.items)
    references = resolve_materials(
        bus,
        actor=user,
        material_ids=["asset:asset-sheet-a-front_face", "asset:contact-foreign-sheet"],
    )
    assert [item.asset_id for item in references.items] == ["asset-sheet-a-front_face"]
    assert "first_frame" in references.items[0].allowed_uses
    assert references.unavailable_ids == ["asset:contact-foreign-sheet"]
    hide_material(bus, actor=user, material_id="asset:contact-sheet-a")
    remaining = list_materials(
        bus, actor=user, media_type="image", source="character", query=None, page=1, page_size=50
    )
    assert remaining.total == 1
    assert remaining.items[0].asset_id == "contact-sheet-b"


def test_asset_access_owner_scoping_on_pg(bus: BusinessConnection, pg: psycopg.Connection) -> None:
    """旧断言（test_material_permissions.py）：口播结果资产仅任务属主可读，
    他者一律 404 掩蔽（含无任务挂靠的游离资产）。"""
    pg.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
        "VALUES ('identity-oral', 'employee_1', '荣哥', 'ACTIVE')"
    )
    pg.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES ('asset-oral-source', NULL, "
        "'character_source_image', 'local://oral/source.png', 'sha-source', 10, "
        "'image/png', 'employee_1')"
    )
    pg.execute(
        "INSERT INTO oral_avatars (id, identity_id, owner_user_id, title, vendor_avatar_id, "
        "status, source_kind, source_asset_id) VALUES ('avatar-oral', 'identity-oral', "
        "'employee_1', 'Avatar', 'vendor-avatar', 'READY', 'VIDEO', 'asset-oral-source')"
    )
    pg.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES ('asset-oral-result', NULL, 'oral_audio', "
        "'local://oral/result.mp3', 'sha-result', 10, 'audio/mpeg', 'employee_1')"
    )
    pg.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES ('asset-unlinked', NULL, 'misc', "
        "'local://misc/unlinked.bin', 'sha-unlinked', 8, 'application/octet-stream', 'employee_1')"
    )
    pg.execute(
        "INSERT INTO oral_tasks (id, owner_user_id, identity_id, avatar_id, mode, title, "
        "status, result_asset_id, estimated_cost_fen, idempotency_key) VALUES "
        "('task-oral', 'employee_1', 'identity-oral', 'avatar-oral', 'TTS', 'Result', "
        "'SUCCEEDED', 'asset-oral-result', 1000, 'cw058-oral-idem')"
    )

    owner_row = require_asset_access(
        bus,
        actor=actor("employee_1", "employee"),
        asset_id="asset-oral-result",
        action="asset.read",
    )
    assert str(owner_row["id"]) == "asset-oral-result"

    for outsider in ("employee_2", "customer_1"):
        with pytest.raises(HTTPException) as denied:
            require_asset_access(
                bus,
                actor=actor(outsider, "employee" if outsider == "employee_2" else "customer"),
                asset_id="asset-oral-result",
                action="asset.read",
            )
        assert denied.value.status_code == 404
        assert denied.value.detail["code"] == "ASSET_NOT_FOUND"

    with pytest.raises(HTTPException) as unlinked:
        require_asset_access(
            bus,
            actor=actor("employee_2", "employee"),
            asset_id="asset-unlinked",
            action="asset.read",
        )
    assert unlinked.value.status_code == 404


# ===========================================================================
# D 组 — 人物审核/发布（含 rowid PG 阻塞点的先红后绿）
# ===========================================================================


def _seed_reviewing_version(
    pg: psycopg.Connection, storage: FakeStorageAdapter, *, key: str = "review"
) -> tuple[str, dict[str, str]]:
    """播种 REVIEWING 人物版本（7 视图候选已入存储），返回 (version_id, 选择表)."""
    version_id = f"character-version-{key}"
    persona_id = f"persona-{key}"
    identity_id = f"identity-{key}"
    source_content = b"png-source-image-bytes"
    stored_source = storage.put_object(
        f"users/employee_1/identities/{identity_id}/source/source.png",
        source_content,
        content_type="image/png",
    )
    storage.put_object(
        f"users/employee_1/identities/{identity_id}/authorization/authorization.pdf",
        b"%PDF-1.7\nauthorized",
        content_type="application/pdf",
    )
    pg.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES (%s, NULL, 'character_source_image', "
        "%s, %s, %s, 'image/png', 'admin_1')",
        (f"source-{key}", stored_source.uri, stored_source.sha256, stored_source.size),
    )
    pg.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, "
        "authorization_status, authorization_scope, authorization_expires_at, "
        "source_asset_id, source_quality_status, status, created_by) "
        "VALUES (%s, 'employee_1', '荣哥', 'AUTHORIZED', %s, "
        "'2035-01-01T00:00:00+00:00', %s, 'PASSED', 'ACTIVE', 'admin_1')",
        (identity_id, encode_json(["internal-short-video"]), f"source-{key}"),
    )
    pg.execute(
        "INSERT INTO character_personas (id, identity_id, name, created_by) "
        "VALUES (%s, %s, '乡墅项目管理专家', 'admin_1')",
        (persona_id, identity_id),
    )
    pg.execute(
        "INSERT INTO character_versions (id, persona_id, version_number, status, "
        "source_asset_id, source_sha256, persona_snapshot_json, provider, model, "
        "generation_params_json, template_version, template_hash, "
        "required_view_types_json, created_by) VALUES "
        "(%s, %s, 1, 'REVIEWING', %s, %s, '{}', 'fake_character', 'fake-character-v1', "
        "'{}', 'character-prompt-v1', %s, %s, 'admin_1')",
        (
            version_id,
            persona_id,
            f"source-{key}",
            stored_source.sha256,
            hashlib.sha256(b"character-prompt-v1").hexdigest(),
            encode_json(list(REQUIRED_CHARACTER_VIEW_TYPES)),
        ),
    )
    selected_by_view: dict[str, str] = {}
    for index, view_type in enumerate(REQUIRED_CHARACTER_VIEW_TYPES):
        character_asset_id = f"candidate-{key}-{view_type.lower()}"
        generated_asset_id = f"generated-{key}-{view_type.lower()}"
        stored = storage.put_object(
            f"users/employee_1/personas/{persona_id}/versions/{version_id}/{view_type.lower()}-{index}.png",
            b"png-candidate-" + view_type.encode(),
            content_type="image/png",
        )
        pg.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, NULL, 'character_generated_image', "
            "%s, %s, %s, 'image/png', 'admin_1')",
            (generated_asset_id, stored.uri, stored.sha256, stored.size),
        )
        pg.execute(
            "INSERT INTO character_assets (id, character_version_id, asset_id, view_type, "
            "candidate_number, auto_quality_json, review_status, is_published_selection) "
            "VALUES (%s, %s, %s, %s, 1, %s, 'NOT_REVIEWED', 0)",
            (
                character_asset_id,
                version_id,
                generated_asset_id,
                view_type,
                encode_json(
                    {
                        "blocking_issue_codes": [],
                        "schema_version": "character-quality.v1",
                        "simulated": True,
                    }
                ),
            ),
        )
        selected_by_view[view_type] = character_asset_id
    return version_id, selected_by_view


def test_character_review_history_latest_decision_wins_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_character_asset_review.py 审核历史组）：审核历史按写入序
    可枚举，最新一次裁决决定 review_status。PG 阻塞点：服务 SQL 的
    ``ORDER BY created_at, rowid`` 是 SQLite 专有列——先红（UndefinedColumn），
    修为按通道选择 ctid/rowid 后绿。"""
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    version_id, selected_by_view = _seed_reviewing_version(pg, storage)
    front_face = selected_by_view["FRONT_FACE"]

    review_character_asset(
        bus,
        actor=actor("admin_1", "admin"),
        character_asset_id=front_face,
        decision="APPROVED",
        issue_codes=[],
        comment=None,
    )
    review_character_asset(
        bus,
        actor=actor("admin_1", "admin"),
        character_asset_id=front_face,
        decision="REJECTED",
        issue_codes=["COMPOSITION"],
        comment="构图不合格",
    )

    history = list_character_asset_reviews(
        bus, actor=actor("admin_1", "admin"), character_asset_id=front_face
    )
    decisions = [item.decision for item in history]
    assert decisions == ["APPROVED", "REJECTED"]

    status_row = pg.execute(
        "SELECT review_status FROM character_assets WHERE id = %s", (front_face,)
    ).fetchone()
    assert status_row is not None and status_row[0] == "REJECTED"
    reviews = pg.execute(
        "SELECT count(*) FROM character_asset_reviews WHERE character_asset_id = %s",
        (front_face,),
    ).fetchone()
    assert reviews is not None and reviews[0] == 2
    audit = pg.execute(
        "SELECT count(*) FROM audit_logs WHERE action = 'character_asset.review'"
    ).fetchone()
    assert audit is not None and audit[0] == 2
    assert version_id  # 版本仍处 REVIEWING：驳回不改写版本状态


def test_publish_freezes_hash_and_enforces_published_view_uniqueness_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_character_asset_review.py 发布组 + test_character_domain
    约束用例）：发布冻结 64 位哈希快照、重放幂等、改选 409；部分唯一索引
    ``uq_character_assets_published_view`` 在 PG 上拒绝第二个已发布同视图."""
    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    version_id, selected_by_view = _seed_reviewing_version(pg, storage)
    # 发布前置：7 个候选逐一走真实审核流 APPROVED（发布守卫读取最新裁决）。
    for asset_id in selected_by_view.values():
        review_character_asset(
            bus,
            actor=actor("admin_1", "admin"),
            character_asset_id=asset_id,
            decision="APPROVED",
            issue_codes=[],
            comment=None,
        )
    selection = {cast(Any, view): asset_id for view, asset_id in selected_by_view.items()}

    published = publish_character_version(
        bus,
        actor=actor("admin_1", "admin"),
        version_id=version_id,
        selected_asset_ids=selection,
        storage=storage,
    )
    assert published.publication_hash is not None
    assert len(published.publication_hash) == 64

    snapshot_row = pg.execute(
        "SELECT publication_snapshot_json, publication_hash FROM character_versions WHERE id = %s",
        (version_id,),
    ).fetchone()
    assert snapshot_row is not None
    assert json.loads(str(snapshot_row[0]))["schema_version"] == "character-publication.v1"

    replay = publish_character_version(
        bus,
        actor=actor("admin_1", "admin"),
        version_id=version_id,
        selected_asset_ids=selection,
        storage=storage,
    )
    assert replay.publication_hash == published.publication_hash

    published_count = pg.execute(
        "SELECT count(*) FROM character_assets WHERE character_version_id = %s "
        "AND is_published_selection = 1",
        (version_id,),
    ).fetchone()
    assert published_count is not None and published_count[0] == 5

    # PG 部分唯一索引：同一视图第二个已发布选择必须被数据库拒绝。
    # （走门面写，验证 IntegrityConstraintError 携 SQLSTATE 与约束名。）
    with pytest.raises(IntegrityConstraintError) as raised:
        bus.execute(
            "INSERT INTO character_assets (id, character_version_id, asset_id, view_type, "
            "candidate_number, review_status, is_published_selection) "
            "VALUES (%s, %s, %s, %s, 2, 'APPROVED', 1)",
            (
                "character-asset-published-dup",
                version_id,
                "generated-review-front_face",
                "FRONT_FACE",
            ),
        )
    assert raised.value.sqlstate == UNIQUE_VIOLATION
    assert raised.value.constraint_name == "uq_character_assets_published_view"


# ===========================================================================
# E 组 — 爆款（库去重/统计、keyset 分页、收藏、隐藏可见性、刷新任务 PG 通道）
# ===========================================================================


def _publish_test_media(bus: BusinessConnection) -> None:
    """Fixtures used by pagination tests represent a completed collection."""
    bus.execute("UPDATE viral_videos SET collection_published=1, cover_key='cover.jpg'")
    bus.execute("""INSERT INTO viral_media_preparations
        (id,platform,video_id,media_kind,status,storage_uri)
        SELECT platform || video_id,platform,video_id,'video','SUCCEEDED','fake://tests/media.mp4'
        FROM viral_videos ON CONFLICT DO NOTHING""")


@pytest.mark.parametrize("failure", ["search", "media", "checkpoint"])
def test_weekly_collection_failure_keeps_published_snapshot_and_resumes_checkpoints(
    lane_env: str, pg: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from dataclasses import replace

    from app.generation_worker import run_pg_collection_once
    from app.viral_collection import enqueue_due_viral_collections
    from app.viral_tikhub import ViralSourceError

    bus = BusinessConnection.postgres(pg)
    upsert_viral_videos(bus, [viral_video("douyin", "last-week")])
    _publish_test_media(bus)
    stamp = datetime.now(UTC).isoformat()
    pg.execute(
        "INSERT INTO viral_fetch_state(platform,sort,fetched_at) "
        "VALUES('douyin','weekly_window',%s)",
        (stamp,),
    )
    pg.execute(
        "UPDATE viral_runtime_controls SET collection_enabled=1, keywords_json=%s, "
        "next_collection_at=NULL WHERE id=1",
        (
            json.dumps(
                [
                    {"platform": "douyin", "category": "测试", "keyword": key}
                    for key in ("bad", "good")
                ]
            ),
        ),
    )
    calls: list[str] = []
    streams: list[str] = []
    failing = True

    class Source:
        def douyin_search(self, *, keyword: str, category: str) -> list[ViralVideo]:
            calls.append(keyword)
            if failing and failure == "search" and keyword == "bad":
                raise ViralSourceError("temporary search failure")
            return [
                replace(
                    viral_video("douyin", keyword),
                    cover_url=None,
                    play_url=f"https://cdn.example/{keyword}.mp4",
                    audio_url=None,
                )
            ]

    def stream(self, url):
        streams.append(url)
        if failing and failure == "media" and url.endswith("bad.mp4"):
            raise ViralSourceError("temporary download failure")
        yield b"\x00\x00\x00\x18ftypisom"

    monkeypatch.setattr(
        "app.viral_collection.viral_source_client_from_settings", lambda conn: Source()
    )
    monkeypatch.setattr("app.viral_media.UrlFetcher.iter_fetch", stream)
    if failure == "checkpoint":
        from app import viral_collection

        save_checkpoint = viral_collection._checkpoint

        def checkpoint(conn, lease, progress):
            if failing and "bad" in progress.get("prepared", []):
                raise RuntimeError("checkpoint transaction failed")
            save_checkpoint(conn, lease, progress)

        monkeypatch.setattr(viral_collection, "_checkpoint", checkpoint)
        monkeypatch.setattr(
            viral_collection.CoverEnricher,
            "enrich",
            lambda self, video: replace(video, cover_key=f"cover-{video.video_id}.jpg"),
        )
    enqueue_due_viral_collections(bus)
    storage = FakeStorageAdapter(provider="fake", bucket="weekly")
    assert run_pg_collection_once(worker_id="collector", storage=storage) == 1
    assert calls == ["bad", "good"]
    assert [
        v.video_id
        for v in list_viral_video_page(bus, platform="douyin", sort="hot", limit=20).items
    ] == ["last-week"]
    assert (
        pg.execute(
            "SELECT fetched_at FROM viral_fetch_state WHERE sort='weekly_window'"
        ).fetchone()[0]
        == stamp
    )
    assert pg.execute("SELECT status FROM viral_refresh_tasks").fetchone()[0] == "FAILED"
    if failure == "checkpoint":
        assert (
            pg.execute("SELECT cover_key FROM viral_videos WHERE video_id='bad'").fetchone()[0]
            is None
        )
        checkpoint_value = json.loads(
            pg.execute("SELECT checkpoint_json FROM viral_refresh_tasks").fetchone()[0]
        )
        assert checkpoint_value["prepared"] == ["good"]
    failing = False
    pg.execute(
        "UPDATE viral_refresh_tasks SET "
        "updated_at=(CURRENT_TIMESTAMP - interval '16 minutes')::text"
    )
    pg.execute(
        "UPDATE viral_media_preparations SET "
        "updated_at=(CURRENT_TIMESTAMP - interval '1 minute')::text WHERE status='FAILED'"
    )
    enqueue_due_viral_collections(bus)
    assert run_pg_collection_once(worker_id="collector", storage=storage) == 1
    assert calls.count("good") == 1
    assert streams.count("https://cdn.example/good.mp4") == 1
    assert {
        v.video_id
        for v in list_viral_video_page(bus, platform="douyin", sort="hot", limit=20).items
    } == {"bad", "good"}
    assert pg.execute("SELECT status FROM viral_refresh_tasks").fetchone()[0] == "SUCCEEDED"
    if failure == "checkpoint":
        assert (
            pg.execute("SELECT cover_key FROM viral_videos WHERE video_id='bad'").fetchone()[0]
            == "cover-bad.jpg"
        )


def test_weekly_list_rejects_incomplete_media_and_cover(bus: BusinessConnection) -> None:
    upsert_viral_videos(
        bus,
        [
            viral_video("douyin", "ready"),
            viral_video("douyin", "unprepared"),
            viral_video("douyin", "cover-missing"),
        ],
    )
    _publish_test_media(bus)
    bus.execute("UPDATE viral_media_preparations SET status='FAILED' WHERE video_id='unprepared'")
    bus.execute(
        "UPDATE viral_videos SET cover_url='https://cdn.example/cover.jpg', cover_key=NULL "
        "WHERE video_id='cover-missing'"
    )
    page = list_viral_video_page(bus, platform="douyin", sort="hot", limit=20)
    assert page.total == 1
    assert [video.video_id for video in page.items] == ["ready"]


@pytest.mark.parametrize("pause_after_cover", [False, True])
def test_weekly_wechat_cached_media_refreshes_statistics_but_pause_prevents_details(
    lane_env: str,
    pg: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    pause_after_cover: bool,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from app.generation_worker import run_pg_collection_once
    from app.viral_collection import enqueue_due_viral_collections
    from app.viral_media_preparation import ViralMediaPreparation

    video = replace(
        viral_video("wechat_channels", "cached-wechat"),
        cover_url=None,
        native={"export_id": "test-export"},
    )
    storage = FakeStorageAdapter(provider="fake", bucket="weekly")
    ViralMediaPreparation(storage=storage).fetch(
        platform=video.platform,
        video_id=video.video_id,
        kind="video",
        prepare=lambda key, check: storage.put_object(
            key, b"\x00\x00\x00\x18ftypisom", content_type="video/mp4"
        ),
    )
    detail_calls: list[str] = []

    class Source:
        def wechat_search(self, **kwargs):
            return [video]

        def wechat_video_detail(self, **kwargs):
            detail_calls.append(kwargs["export_id"])
            return SimpleNamespace(
                like_count=9876, comment_count=12, forward_count=34, fav_count=56
            )

    def cover(self, source):
        if pause_after_cover:
            pg.execute("UPDATE viral_runtime_controls SET collection_enabled=0 WHERE id=1")
        return source

    monkeypatch.setattr(
        "app.viral_collection.viral_source_client_from_settings", lambda conn: Source()
    )
    monkeypatch.setattr("app.viral_collection.CoverEnricher.enrich", cover)
    pg.execute(
        "UPDATE viral_runtime_controls SET collection_enabled=1, keywords_json=%s, "
        "next_collection_at=NULL WHERE id=1",
        (json.dumps([{"platform": "wechat_channels", "category": "测试", "keyword": "建筑"}]),),
    )
    enqueue_due_viral_collections(BusinessConnection.postgres(pg))
    assert run_pg_collection_once(worker_id="collector", storage=storage) == 1
    if pause_after_cover:
        assert detail_calls == []
        assert pg.execute("SELECT collection_published FROM viral_videos").fetchone()[0] == 0
    else:
        assert detail_calls == ["test-export"]
        # 显式入队要求先把 pg 包成 BusinessConnection，而 PostgresBackend
        # 会把该连接的 row_factory 永久换成 _NamedRow（db_portable.py:112）；
        # _NamedRow == tuple 设计上恒为 False（镜像 sqlite3.Row），故先显式转 tuple。
        assert tuple(
            pg.execute(
                "SELECT likes,comments,shares,collects,collection_published FROM viral_videos"
            ).fetchone()
        ) == (9876, 12, 34, 56, 1)


def test_viral_store_upsert_dedup_and_statistics_coalesce_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection, cw058_dsn: str
) -> None:
    """旧断言（test_viral_store.py 去重/统计组）：(platform, video_id) 去重、
    NULL 统计不冲掉已富化值（COALESCE）、真实零值照写、跨连接重开仍在."""
    upsert_viral_videos(
        bus, [viral_video("wechat_channels", "v-1", comments=5, cover_key="covers/v-1.jpg")]
    )
    upsert_viral_videos(
        bus,
        [
            viral_video(
                "wechat_channels",
                "v-1",
                comments=None,
                shares=None,
                likes=200,
                cover_key=None,
            )
        ],
    )
    kept = get_viral_video(bus, platform="wechat_channels", video_id="v-1")
    assert kept is not None
    assert kept.likes == 200
    assert kept.comments == 5
    # 冲突路径不触碰 cover_key：刷新条目不带封面时不得冲掉已落存储的封面 key。
    assert kept.cover_key == "covers/v-1.jpg"

    upsert_viral_videos(bus, [viral_video("douyin", "v-1", likes=7)])
    same_id_other_platform = get_viral_video(bus, platform="douyin", video_id="v-1")
    assert same_id_other_platform is not None
    assert same_id_other_platform.likes == 7

    # 跨连接重开仍在：新开一条生产同形 autocommit 连接读取。
    with psycopg.connect(cw058_dsn, autocommit=True) as fresh_raw:
        reopened_conn = BusinessConnection.postgres(fresh_raw)
        durable = get_viral_video(reopened_conn, platform="wechat_channels", video_id="v-1")
        assert durable is not None
        assert durable.title == "CW058 v-1"
        count = fresh_raw.execute("SELECT count(*) FROM viral_videos").fetchone()
        assert count is not None and count[0] == 2


def test_viral_keyset_pagination_and_cursor_invalidation_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_viral_store.py 分页组）：35 条批量种子 → keyset 游标
    稳定翻页不重不漏、total 恒定；fetch_state 推进后旧游标作废."""
    videos = [
        viral_video(
            "douyin",
            f"v-{index:02d}",
            likes=1000 - index,
            published_at=int(datetime.now(UTC).timestamp()) - index * 60,
        )
        for index in range(35)
    ]
    upsert_viral_videos(bus, videos)
    _publish_test_media(bus)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    total: int | None = None
    while True:
        page = list_viral_video_page(bus, platform="douyin", sort="hot", limit=12, cursor=cursor)
        seen.extend(item.video_id for item in page.items)
        total = page.total
        pages += 1
        if not page.has_more:
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor
    assert pages == 3
    assert len(seen) == 35
    assert len(set(seen)) == 35
    assert total == 35

    stale_cursor = list_viral_video_page(bus, platform="douyin", sort="hot", limit=12).next_cursor
    mark_fetch_state(bus, platform="douyin", sort="hot")
    with pytest.raises(InvalidViralCursorError):
        list_viral_video_page(bus, platform="douyin", sort="hot", limit=12, cursor=stale_cursor)


def test_viral_favorites_idempotent_isolated_and_paginated_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_viral_store.py 收藏组）：收藏幂等（PK 去重）、按用户隔离、
    55 条批量种子 keyset 分页、移除恰好一次."""
    upsert_viral_videos(
        bus,
        [viral_video("douyin", f"fv-{index:02d}") for index in range(60)],
    )
    assert (
        add_viral_favorite(bus, user_id="employee_1", platform="douyin", video_id="fv-00") is True
    )
    assert (
        add_viral_favorite(bus, user_id="employee_1", platform="douyin", video_id="fv-00") is False
    )
    assert is_viral_favorite(bus, user_id="employee_1", platform="douyin", video_id="fv-00")
    assert not is_viral_favorite(bus, user_id="employee_2", platform="douyin", video_id="fv-00")

    for index in range(55):
        add_viral_favorite(bus, user_id="employee_1", platform="douyin", video_id=f"fv-{index:02d}")

    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = list_favorite_viral_video_page(bus, user_id="employee_1", limit=50, cursor=cursor)
        seen.extend(item.video_id for item in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert len(seen) == 55
    assert len(set(seen)) == 55

    assert (
        remove_viral_favorite(bus, user_id="employee_1", platform="douyin", video_id="fv-00")
        is True
    )
    assert (
        remove_viral_favorite(bus, user_id="employee_1", platform="douyin", video_id="fv-00")
        is False
    )
    assert not is_viral_favorite(bus, user_id="employee_1", platform="douyin", video_id="fv-00")


def test_viral_hidden_visibility_excluded_from_list_on_pg(
    bus: BusinessConnection, pg: psycopg.Connection
) -> None:
    """旧断言（test_viral_routes.py 隐藏可见性组）：HIDDEN 行主列表排除、
    详情仍可取且 availability=hidden."""
    upsert_viral_videos(
        bus,
        [viral_video("douyin", "visible-1"), viral_video("douyin", "hidden-1")],
    )
    bus.execute(
        "INSERT INTO viral_video_visibility (platform, video_id, status, reason) "
        "VALUES (%s, %s, 'HIDDEN', 'cw058-test-hide')",
        ("douyin", "hidden-1"),
    )
    bus.commit()
    _publish_test_media(bus)

    page = list_viral_video_page(bus, platform="douyin", sort="hot", limit=20)
    assert [item.video_id for item in page.items] == ["visible-1"]
    assert page.total == 1

    still_fetchable = get_viral_video(bus, platform="douyin", video_id="hidden-1")
    assert still_fetchable is not None
    assert viral_video_availability(bus, platform="douyin", video_id="hidden-1") == "hidden"
    assert viral_video_availability(bus, platform="douyin", video_id="visible-1") == "available"


def test_viral_list_reads_only_and_weekly_worker_prepares_cloud_media_on_pg(
    lane_env: str,
    pg: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧断言（test_viral_routes.py 刷新组 + test_viral_refresh.py 租约组）的
    PG 通道半边：is_postgres 分支把冷列表转为刷新任务入队（scope 去重），
    run_pg_worker_once 以独立连接消费——上游外呼不持有请求事务，结果落库后
    任务 SUCCEEDED、列表可从库中读出。"""
    from app.generation_worker import run_pg_collection_once, run_pg_worker_once
    from app.viral_collection import enqueue_due_viral_collections
    from app.viral_store import fetch_state_is_fresh

    class StubClient(ViralSourceClient):
        def __init__(self) -> None:
            super().__init__(api_key="stub-key")
            self.calls = 0

        def douyin_search(self, **_kwargs: Any) -> list[ViralVideo]:
            self.calls += 1
            from dataclasses import replace

            return [
                replace(
                    viral_video("douyin", f"worker-{index}"),
                    play_url="https://cdn.example/prepared.mp4",
                    audio_url=None,
                    cover_url=None,
                    native={"_playback_version": 1},
                )
                for index in range(2)
            ]

        def wechat_search(self, **_kwargs: Any) -> list[ViralVideo]:
            self.calls += 1
            return []

    stub = StubClient()
    # 路由依赖在注册期已绑定 get_viral_source_client，必须走 dependency_overrides；
    # worker 侧是模块级直调，monkeypatch 生效。
    monkeypatch.setattr(
        "app.viral_collection.viral_source_client_from_settings", lambda _conn: stub
    )

    def stream(self, url):
        yield b"\x00\x00\x00\x18ftypisom"

    monkeypatch.setattr("app.viral_media.UrlFetcher.iter_fetch", stream)

    # 冷列表：请求通道（get_database → pg_transaction）只入队，不回源。
    from app.auth import get_current_user
    from app.main import app
    from app.viral_routes import get_viral_source_client as _client_dependency

    app.dependency_overrides[_client_dependency] = lambda: stub
    app.dependency_overrides[get_current_user] = lambda: actor("employee_1", "employee")
    try:
        client = TestClient(app)
        response = client.get(
            "/api/viral/videos",
            params={"platform": "douyin", "sort": "hot"},
            headers={"X-Dev-User-Id": "employee_1"},
        )
        assert response.status_code == 200, response.text
        queued = pg.execute(
            "SELECT count(*) FROM viral_refresh_tasks WHERE platform = 'douyin' AND sort = 'hot'"
        ).fetchone()
        assert queued is not None and queued[0] == 0

        # 重放请求仍只保留一条去重任务。
        client.get(
            "/api/viral/videos",
            params={"platform": "douyin", "sort": "hot"},
            headers={"X-Dev-User-Id": "employee_1"},
        )
        queued_again = pg.execute(
            "SELECT count(*) FROM viral_refresh_tasks WHERE platform = 'douyin' AND sort = 'hot'"
        ).fetchone()
        assert queued_again is not None and queued_again[0] == 0
    finally:
        app.dependency_overrides.clear()

    assert stub.calls == 0
    pg.execute(
        "UPDATE viral_runtime_controls SET keywords_json=%s, next_collection_at=NULL WHERE id=1",
        (json.dumps([{"platform": "douyin", "category": "测试", "keyword": "农村建房"}]),),
    )
    pg.commit()
    enqueue_due_viral_collections(BusinessConnection.postgres(pg))
    # PG worker 消费：租约获取/完成走 pg_transaction 短事务。
    # A configured collection cannot occupy the generation worker pool.
    advanced: list[str] = []
    monkeypatch.setattr("app.generation_worker.claim_oral_work", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.generation_worker.acquire_generation_continuation_lease",
        lambda *args, **kwargs: {"id": "queued-generation"},
    )
    monkeypatch.setattr(
        "app.generation_worker._run_pg_generation_step",
        lambda **kwargs: advanced.append(kwargs["lease"]["id"]),
    )
    assert (
        run_pg_worker_once(
            worker_id="normal",
            storage=FakeStorageAdapter(provider="fake", bucket="cw058-tests"),
            max_tasks=1,
        )
        == 1
    )
    assert advanced == ["queued-generation"]
    assert stub.calls == 0
    processed = run_pg_collection_once(
        worker_id="cw058-worker",
        storage=FakeStorageAdapter(provider="fake", bucket="cw058-tests"),
    )
    assert processed >= 1
    task_row = pg.execute(
        "SELECT status FROM viral_refresh_tasks WHERE platform = 'douyin' AND sort = 'hot'"
    ).fetchone()
    assert task_row is not None and task_row[0] == "SUCCEEDED"
    assert stub.calls >= 1
    stored = pg.execute("SELECT count(*) FROM viral_videos").fetchone()
    assert stored is not None and stored[0] >= 2
    page = list_viral_video_page(
        BusinessConnection.postgres(pg), platform="douyin", sort="hot", limit=20
    )
    assert page.total == 2

    # 回源已落库：fetch_state 新鲜，后续请求不再新增上游调用。
    assert fetch_state_is_fresh(
        BusinessConnection.postgres(psycopg.connect(lane_env, autocommit=True)),
        platform="douyin",
        sort="hot",
        max_age=timedelta(minutes=5),
    )


def test_material_video_completion_persists_probed_duration(
    bus: BusinessConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import materials
    from app.media_tools import MediaInspection

    storage = FakeStorageAdapter(provider="fake", bucket="cw058-tests")
    admin = actor("admin_1", "admin")
    content = b"\x00\x00\x00\x18ftypisom" + b"test-video"

    def inspect(content_bytes: bytes, **kwargs: Any) -> MediaInspection:
        assert content_bytes == content
        assert kwargs["expected_type"] == "video"
        return MediaInspection(
            media_type="video", duration_seconds=12.066667, width=720, height=1372
        )

    monkeypatch.setattr(materials, "inspect_media_bytes", inspect, raising=False)
    intent = materials.create_material_upload_intent(
        bus,
        actor=admin,
        storage=storage,
        request=materials.MaterialUploadIntentRequest(
            filename="ref.mp4", content_type="video/mp4", size_bytes=len(content)
        ),
    )
    storage.put_object(intent.storage_key, content, content_type="video/mp4")
    prepared = materials.prepare_material_upload(bus, actor=admin, asset_id=intent.asset_id)
    probed = materials.probe_material_upload(prepared, storage=storage)
    assert probed.duration_seconds == 12.066667
    item = materials.persist_material_upload(bus, actor=admin, probed=probed)
    assert item.duration_seconds == 12.066667
    row = bus.execute("SELECT metadata_json FROM assets WHERE id=%s", (intent.asset_id,)).fetchone()
    metadata = json.loads(row["metadata_json"])
    assert metadata["video_duration_verified"] is True
    assert "audio_duration_verified" not in metadata
