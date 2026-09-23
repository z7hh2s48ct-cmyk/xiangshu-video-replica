"""MATERIAL-THUMBS-B-20260917 / MATERIAL-UX-02-20260922 — 素材缩略图（视频与图片）PG 矩阵.

对应分析报告《素材库显示与页面切换性能根因分析与优化方案-2026-09-17》P0-3：
素材库网格的视频瓦片此前只能让浏览器经服务端代理流式拉原视频出首帧
（每页 6–24 路转发流）；MATERIAL-UX-02 起图片瓦片同链路派生单帧缩略图
（手机照片可达 10MB/张，网格不宜直拉原图）。本模块钉住缩略图链路的安全与
语义不变量：

- 抽帧：ffmpeg 取首帧（图片为单帧缩放）缩为 ≤480px JPEG（图片同时受
  ≤960px 宽度上界约束），缩略图对象键与原对象键同址派生
  （``<object_key>.thumb.jpg``），任何抽帧/存储失败都不得影响原对象可用性。
- 记录：缩略图键写入 ``assets.metadata_json``（零迁移；列表 CTE 全分支已带出
  metadata_json）。
- 列表：``MaterialItem.thumbnail_key`` 透出元数据中的缩略图键；无缩略图的
  历史/口播素材保持 None（前端降级为现状占位）。
- 批量授权：对视频与图片资产，``DownloadUrlItem.thumbnail_url`` 额外签出
  7 天有效的缩略图对象 URL（同一签名通道、同一属主校验；审计口径不变）。
  音频与其他类型一律 None，不新增暴露面。
- 按需派生：图片（image/*）整读源对象，不头探测——截断的 JPEG 会被 ffmpeg
  解出残缺画面并当作成功，落盘后永久固化。

抽帧用真实 ffmpeg（lavfi 生成测试视频/图片，与 test_cw058_content_asset_pg_matrix.py
同一手法）；存储在 FakeStorageAdapter 处替身；授权语义复用 MATERIAL-PERF-A 的
批量授权矩阵与专属 PG 库 ``matthumbs_test``。
"""

from __future__ import annotations

import json
import struct
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.material_thumbs import (
    extract_thumbnail_jpeg,
    store_video_thumbnail,
    thumbnail_key_for,
)

JPEG_MAGIC = b"\xff\xd8"


def make_test_video(duration_seconds: float = 1.0) -> bytes:
    """用 lavfi 生成一段真实可解码的测试视频（同 CW-058 手法）."""
    from app.media_tools import resolve_media_binary

    with tempfile.TemporaryDirectory(prefix="matthumbs-") as directory:
        media_path = Path(directory) / "source.mp4"
        subprocess.run(
            [
                resolve_media_binary("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=duration={duration_seconds}:size=1280x720:rate=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(media_path),
            ],
            check=True,
            capture_output=True,
        )
        return media_path.read_bytes()


def make_test_image(suffix: str = "png", *, width: int = 480, height: int = 854) -> bytes:
    """用 lavfi 生成一张真实可解码的测试图片（默认 480x854 竖屏比例）."""
    from app.media_tools import resolve_media_binary

    with tempfile.TemporaryDirectory(prefix="matthumbs-") as directory:
        media_path = Path(directory) / f"source.{suffix}"
        subprocess.run(
            [
                resolve_media_binary("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=duration=1:size={width}x{height}:rate=1",
                "-frames:v",
                "1",
                str(media_path),
            ],
            check=True,
            capture_output=True,
        )
        return media_path.read_bytes()


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """读 JPEG SOF 段解析宽高（尺寸断言用，无第三方依赖）."""
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        index += 2 + struct.unpack(">H", data[index + 2 : index + 4])[0]
    raise AssertionError("JPEG 缺少 SOF 段")


def test_thumbnail_key_is_deterministic_and_adjacent() -> None:
    assert (
        thumbnail_key_for("materials/employee_1/a/original.mp4")
        == "materials/employee_1/a/original.mp4.thumb.jpg"
    )
    assert (
        thumbnail_key_for("generation-results/t1/abc.mp4")
        == "generation-results/t1/abc.mp4.thumb.jpg"
    )


def test_extract_thumbnail_produces_small_jpeg() -> None:
    content = make_test_video()
    thumb = extract_thumbnail_jpeg(content)
    assert thumb is not None
    assert thumb.startswith(JPEG_MAGIC)
    # 缩略图必须显著小于原视频，否则网格批量下发失去意义。
    assert len(thumb) < 200_000


def test_extract_thumbnail_supports_still_images() -> None:
    """MATERIAL-UX-02：图片素材（PNG/JPEG）复用同一抽帧（单帧缩放），
    输出 JPEG 缩略图——网格瓦片因此不必直拉原图."""
    for suffix in ("png", "jpg"):
        image = make_test_image(suffix)
        thumb = extract_thumbnail_jpeg(image)
        assert thumb is not None
        assert thumb.startswith(JPEG_MAGIC)
        assert len(thumb) < 200_000


def test_image_thumbnail_caps_width_for_wide_sources() -> None:
    """图片专用抽帧同时约束宽高：五视图类超宽合成图（2000×400）在只限高
    策略下会原样落盘 2000px 宽的瓦片缩略图，网格一页 24 张时下载量被放大；
    竖屏图不受宽度约束影响（输出与通用抽帧一致）."""
    from app.material_thumbs import THUMBNAIL_MAX_WIDTH, extract_image_thumbnail_jpeg

    wide = make_test_image("jpg", width=2000, height=400)
    thumb = extract_image_thumbnail_jpeg(wide)
    assert thumb is not None
    width, _height = jpeg_dimensions(thumb)
    assert width <= THUMBNAIL_MAX_WIDTH

    portrait = make_test_image("jpg")
    portrait_thumb = extract_image_thumbnail_jpeg(portrait)
    assert portrait_thumb is not None
    assert jpeg_dimensions(portrait_thumb) == (270, 480)


def test_on_demand_image_derivation_reads_whole_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """图片按需派生必须整读源对象：JPEG 扫描流被截断后 ffmpeg 仍会解出
    残缺画面并当作成功（评审实测 22MiB 图取前 8MiB 即产出"半张图"），一旦
    落盘就永久固化。头探测阈值调小到半个文件，回退旧策略即失败."""
    from app import material_thumbs
    from app.material_thumbs import ensure_thumbnail_object, extract_image_thumbnail_jpeg
    from app.storage import FakeStorageAdapter

    content = make_test_image("jpg")
    monkeypatch.setattr(material_thumbs, "_HEAD_PROBE_BYTES", len(content) // 2)
    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    original_key = "materials/employee_1/legacy/photo.jpg"
    storage.put_object(original_key, content, content_type="image/jpeg")
    thumb_key = thumbnail_key_for(original_key)

    assert ensure_thumbnail_object(storage, thumb_key) is True
    assert storage.get_object(thumb_key) == extract_image_thumbnail_jpeg(content)


def test_on_demand_image_derivation_caps_width() -> None:
    """超宽合成图的按需派生同样受宽度上界约束（只限高时 2000×400 会
    原样落盘 2000px 宽）."""
    from app.material_thumbs import THUMBNAIL_MAX_WIDTH, ensure_thumbnail_object
    from app.storage import FakeStorageAdapter

    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    original_key = "materials/employee_1/wide/contact-sheet.jpg"
    storage.put_object(
        original_key,
        make_test_image("jpg", width=2000, height=400),
        content_type="image/jpeg",
    )
    thumb_key = thumbnail_key_for(original_key)

    assert ensure_thumbnail_object(storage, thumb_key) is True
    stored = storage.get_object(thumb_key)
    width, _height = jpeg_dimensions(stored)
    assert width <= THUMBNAIL_MAX_WIDTH


def test_extract_thumbnail_tolerates_garbage_and_returns_none() -> None:
    assert extract_thumbnail_jpeg(b"not a video at all") is None


def test_store_video_thumbnail_puts_adjacent_object_and_returns_key() -> None:
    from app.storage import FakeStorageAdapter

    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    original_key = "generation-results/task-1/digest.mp4"
    storage.put_object(original_key, make_test_video(), content_type="video/mp4")
    key = store_video_thumbnail(storage, original_key, make_test_video())
    assert key == thumbnail_key_for(original_key)
    stored = storage.get_object(key)
    assert stored is not None and stored.startswith(JPEG_MAGIC)


def test_derives_thumbnail_on_demand_for_a_legacy_video() -> None:
    """历史素材按需补齐：抽帧只发生在上传写入点，#145 之前入库的视频永远没有
    缩略图，靠回填脚本跑批既要额外运维、又补不了将来抽帧失败的那些。改成第一次
    有人看到它时现场派生——派生键是确定性的，重复请求天然幂等。"""
    from app.material_thumbs import ensure_thumbnail_object
    from app.storage import FakeStorageAdapter

    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    original_key = "materials/employee_1/legacy/clip.mp4"
    storage.put_object(original_key, make_test_video(), content_type="video/mp4")
    thumb_key = thumbnail_key_for(original_key)
    assert storage.head_object(thumb_key) is None

    assert ensure_thumbnail_object(storage, thumb_key) is True
    stored = storage.get_object(thumb_key)
    assert stored is not None and stored.startswith(JPEG_MAGIC)


def test_on_demand_derivation_is_idempotent_and_does_not_reread_the_source() -> None:
    """已存在直接返回：多张瓦片同时请求同一条历史视频不会重复抽帧."""
    from app.material_thumbs import ensure_thumbnail_object
    from app.storage import FakeStorageAdapter

    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    original_key = "materials/employee_1/legacy/clip.mp4"
    storage.put_object(original_key, make_test_video(), content_type="video/mp4")
    thumb_key = thumbnail_key_for(original_key)
    assert ensure_thumbnail_object(storage, thumb_key) is True
    first = storage.get_object(thumb_key)

    reads: list[str] = []
    original_get = storage.get_object
    storage.get_object = lambda key: (reads.append(key), original_get(key))[1]  # type: ignore[method-assign]
    assert ensure_thumbnail_object(storage, thumb_key) is True
    assert original_key not in reads
    storage.get_object = original_get  # type: ignore[method-assign]
    assert storage.get_object(thumb_key) == first


def test_on_demand_derivation_reports_failure_without_raising() -> None:
    """源对象缺失或不可解码只是没有缩略图，绝不能拖垮这次预览请求."""
    from app.material_thumbs import ensure_thumbnail_object
    from app.storage import FakeStorageAdapter

    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    assert ensure_thumbnail_object(storage, "materials/x/missing.mp4.thumb.jpg") is False
    storage.put_object("materials/x/bad.mp4", b"garbage", content_type="video/mp4")
    assert ensure_thumbnail_object(storage, "materials/x/bad.mp4.thumb.jpg") is False


def test_store_video_thumbnail_never_breaks_on_bad_content() -> None:
    from app.storage import FakeStorageAdapter

    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    key = store_video_thumbnail(storage, "materials/x/a.mp4", b"garbage")
    assert key is None


# ---------------------------------------------------------------------------
# PG 部分：列表透出 metadata_json.thumbnail_key + 批量授权签发缩略图 URL
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from urllib.parse import parse_qs, unquote, urlsplit  # noqa: E402

import psycopg  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pg_test_kit import (  # noqa: E402
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser  # noqa: E402
from app.customer_fence import get_business_db  # noqa: E402
from app.db_pg import DATABASE_URL_ENV, close_pg_pool  # noqa: E402
from app.db_portable import BusinessConnection  # noqa: E402
from app.storage import FakeStorageAdapter  # noqa: E402

MATTHUMBS_TEST_DB = "matthumbs_test"
BATCH_URL = "/api/assets/download-urls"
DAY = 86_400


@pytest.fixture(scope="module")
def matthumbs_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(MATTHUMBS_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(MATTHUMBS_TEST_DB)


@pytest.fixture()
def pg(matthumbs_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(matthumbs_dsn, autocommit=True)
    conn.execute("SET session_replication_role = replica")
    conn.execute("TRUNCATE users, assets, audit_logs CASCADE")
    conn.execute("SET session_replication_role = DEFAULT")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
            ],
        )
    try:
        yield conn
    finally:
        conn.close()
        close_pg_pool()


@pytest.fixture()
def lane_env(matthumbs_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, matthumbs_dsn)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    yield matthumbs_dsn
    close_pg_pool()


@pytest.fixture()
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorageAdapter:
    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    monkeypatch.setattr("app.rbac_routes.storage_for_asset", lambda _conn, _uri: storage)
    return storage


class ScopedBusinessDb:
    def __init__(self, bus: BusinessConnection, user_id: str, role: str) -> None:
        self._bus = bus
        self._user_id = user_id
        self._role = role

    @contextmanager
    def write(self):
        from app.auth import CurrentUser

        yield (
            self._bus,
            CurrentUser(  # type: ignore[arg-type]
                id=self._user_id,
                username=self._user_id,
                display_name=self._user_id,
                role=self._role,
            ),
        )


def seed_video_asset(
    pg: psycopg.Connection,
    asset_id: str,
    owner: str,
    *,
    thumbnail_key: str | None,
) -> str:
    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    stored = storage.put_object(f"perf/{asset_id}.mp4", b"mp4-bytes", content_type="video/mp4")
    if thumbnail_key:
        storage.put_object(thumbnail_key, b"\xff\xd8thumb", content_type="image/jpeg")
    metadata = {"thumbnail_key": thumbnail_key} if thumbnail_key else {}
    digest = hashlib.sha256(asset_id.encode()).hexdigest()
    pg.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json
        )
        VALUES (%s, NULL, 'material_video', %s, %s, 1024, 'video/mp4', %s, %s)
        """,
        (asset_id, stored.uri, digest, owner, json.dumps(metadata)),
    )
    return digest


def seed_material_asset(
    pg: psycopg.Connection,
    asset_id: str,
    owner: str,
    *,
    kind: str,
    content_type: str,
    object_suffix: str,
    thumbnail_key: str | None,
) -> str:
    """种子化一条非视频素材（图片/音频），供授权边界用例使用."""
    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    stored = storage.put_object(
        f"perf/{asset_id}{object_suffix}", b"payload-bytes", content_type=content_type
    )
    if thumbnail_key:
        storage.put_object(thumbnail_key, b"\xff\xd8thumb", content_type="image/jpeg")
    metadata = {"thumbnail_key": thumbnail_key} if thumbnail_key else {}
    digest = hashlib.sha256(asset_id.encode()).hexdigest()
    pg.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json
        )
        VALUES (%s, NULL, %s, %s, %s, 1024, %s, %s, %s)
        """,
        (asset_id, kind, stored.uri, digest, content_type, owner, json.dumps(metadata)),
    )
    return digest


def batch_client(app, pg: psycopg.Connection, user_id: str = "employee_1") -> TestClient:
    bus = BusinessConnection.postgres(pg)
    app.dependency_overrides[get_business_db] = lambda: ScopedBusinessDb(bus, user_id, "employee")
    return TestClient(app)


@pytest.fixture()
def cos_storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorageAdapter:
    """云端后端替身：provider="cos" 触发缩略图直连分支."""
    storage = FakeStorageAdapter(provider="cos", bucket="matthumbs-cos")
    monkeypatch.setattr("app.rbac_routes.storage_for_asset", lambda _conn, _uri: storage)
    return storage


def test_cloud_thumbnail_is_served_straight_from_object_storage(
    lane_env: str, pg: psycopg.Connection, cos_storage: FakeStorageAdapter
) -> None:
    """云端缩略图直连对象存储：派生小图不再经应用服务器逐字节转发。

    素材库一页 24 张瓦片，走代理等于 24 次穿透 worker 且响应是 ``no-store``，
    翻页/重渲染会全量重拉。直连把字节搬运交给对象存储，应用服务器只签名。
    """
    from app.main import app

    cos_storage.put_object(
        "perf/cloud_video.mp4.thumb.jpg", b"\xff\xd8thumb", content_type="image/jpeg"
    )
    seed_video_asset(
        pg, "cloud_video", "employee_1", thumbnail_key="perf/cloud_video.mp4.thumb.jpg"
    )
    client = batch_client(app, pg)
    response = client.post("/api/assets/download-urls", json={"asset_ids": ["cloud_video"]})
    assert response.status_code == 200
    thumb_url = response.json()["items"][0]["thumbnail_url"]
    assert thumb_url is not None
    # 直连地址由存储后端签发，不再落在应用的代理路由上。
    assert "/api/assets/signed-objects/" not in thumb_url
    assert thumb_url.startswith("cos://matthumbs-cos/")
    app.dependency_overrides.clear()


def test_local_thumbnail_still_uses_the_application_proxy(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """本地存储签出的是 ``local://`` 伪协议，浏览器加载不了，必须继续走代理."""
    from app.main import app

    seed_video_asset(
        pg, "local_video", "employee_1", thumbnail_key="perf/local_video.mp4.thumb.jpg"
    )
    client = batch_client(app, pg)
    response = client.post("/api/assets/download-urls", json={"asset_ids": ["local_video"]})
    thumb_url = response.json()["items"][0]["thumbnail_url"]
    assert thumb_url is not None
    assert "/api/assets/signed-objects/" in thumb_url
    app.dependency_overrides.clear()


def test_batch_signs_seven_day_thumbnail_url_for_videos_with_thumb(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    import time

    from app.main import app

    seed_video_asset(
        pg, "thumb_video", "employee_1", thumbnail_key="perf/thumb_video.mp4.thumb.jpg"
    )
    seed_video_asset(pg, "legacy_video", "employee_1", thumbnail_key=None)
    client = batch_client(app, pg)
    response = client.post(BATCH_URL, json={"asset_ids": ["thumb_video", "legacy_video"]})
    assert response.status_code == 200
    items = {item["asset_id"]: item for item in response.json()["items"]}
    # 带缩略图键的视频：额外签出 7 天有效的缩略图对象 URL（同一签名通道）。
    thumb_url = items["thumb_video"]["thumbnail_url"]
    assert thumb_url is not None
    assert "/api/assets/signed-objects/perf%2Fthumb_video.mp4.thumb.jpg" in thumb_url or (
        "thumb_video.mp4.thumb.jpg" in thumb_url
    )
    query = parse_qs(urlsplit(thumb_url).query)
    assert {"expires", "user_id", "asset_id", "sig"} <= set(query)
    assert query["asset_id"][0] == "thumb_video"
    expires = int(query["expires"][0])
    assert 6 * DAY <= expires - int(time.time()) <= 8 * DAY
    assert urlsplit(items["thumb_video"]["url"]).path.startswith("/api/assets/signed-objects/")
    # 历史无缩略图视频：照签确定性派生键，首次加载时由 signed-objects 现场补齐，
    # 因此不再返回 None（前端不必为「视频却没有封面」单独降级）。
    assert items["legacy_video"]["url"] is not None
    legacy_thumb = items["legacy_video"]["thumbnail_url"]
    assert legacy_thumb is not None
    assert "legacy_video.mp4.thumb.jpg" in unquote(legacy_thumb)
    app.dependency_overrides.clear()


def test_batch_masks_foreign_video_and_omits_its_thumbnail(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    from app.main import app

    seed_video_asset(pg, "foreign_video", "employee_2", thumbnail_key="perf/f.mp4.thumb.jpg")
    client = batch_client(app, pg)
    response = client.post(BATCH_URL, json={"asset_ids": ["foreign_video"]})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["error_code"] == "ASSET_NOT_FOUND"
    assert item["thumbnail_url"] is None
    app.dependency_overrides.clear()


def test_batch_signs_thumbnail_url_for_image_materials(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """MATERIAL-UX-02：图片素材同样签出缩略图 URL——网格瓦片不再直拉原图；
    无键历史图片照签确定性派生键，首次加载时由 signed-objects 现场补齐."""
    from app.main import app

    seed_material_asset(
        pg,
        "thumb_image",
        "employee_1",
        kind="material_image",
        content_type="image/png",
        object_suffix=".png",
        thumbnail_key="perf/thumb_image.png.thumb.jpg",
    )
    seed_material_asset(
        pg,
        "legacy_image",
        "employee_1",
        kind="material_image",
        content_type="image/jpeg",
        object_suffix=".jpg",
        thumbnail_key=None,
    )
    client = batch_client(app, pg)
    response = client.post(BATCH_URL, json={"asset_ids": ["thumb_image", "legacy_image"]})
    assert response.status_code == 200
    items = {item["asset_id"]: item for item in response.json()["items"]}
    thumb_url = items["thumb_image"]["thumbnail_url"]
    assert thumb_url is not None
    assert "thumb_image.png.thumb.jpg" in thumb_url
    legacy_thumb = items["legacy_image"]["thumbnail_url"]
    assert legacy_thumb is not None
    assert "legacy_image.jpg.thumb.jpg" in unquote(legacy_thumb)
    app.dependency_overrides.clear()


def test_batch_omits_thumbnail_for_audio_materials(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """音频不签缩略图：声音没有画面，即使元数据残留一个键也不签——
    放宽图片时绝不能顺手把音频带进来（不新增暴露面）."""
    from app.main import app

    seed_material_asset(
        pg,
        "voice_track",
        "employee_1",
        kind="material_audio",
        content_type="audio/mpeg",
        object_suffix=".mp3",
        thumbnail_key="perf/voice_track.mp3.thumb.jpg",
    )
    client = batch_client(app, pg)
    response = client.post(BATCH_URL, json={"asset_ids": ["voice_track"]})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["url"] is not None
    assert item["thumbnail_url"] is None
    app.dependency_overrides.clear()


def test_material_item_exposes_thumbnail_key_from_metadata() -> None:
    from app.materials import material_item

    class Row(dict):
        __getattr__ = dict.__getitem__

    row = Row(
        {
            "source_type": "asset",
            "source_id": "asset-1",
            "owner_user_id": "employee_1",
            "asset_id": "asset-1",
            "generation_task_id": None,
            "project_id": None,
            "person_id": None,
            "person_name": None,
            "project_title": None,
            "tags_json": [],
            "title_override": None,
            "base_title": "成片",
            "base_group": "任务结果",
            "source": "generation",
            "content_type": "video/mp4",
            "size_bytes": 2048,
            "metadata_json": json.dumps(
                {"thumbnail_key": "generation-results/t/abc.mp4.thumb.jpg"}
            ),
            "created_at": "2026-09-17 10:00:00",
            "status": "ready",
            "delivery": "stored",
            "media_type": "video",
            "hidden": 0,
            "group_override": None,
            "character_views_json": "[]",
        }
    )
    item = material_item(row)
    assert item.thumbnail_key == "generation-results/t/abc.mp4.thumb.jpg"
    no_thumb = material_item(Row({**row, "metadata_json": "{}"}))
    assert no_thumb.thumbnail_key is None


# ---------------------------------------------------------------------------
# 上传链路：图片素材同走 probe 抽帧 + attach 记键（MATERIAL-UX-02）
# ---------------------------------------------------------------------------


def employee_actor() -> CurrentUser:
    return CurrentUser(  # type: ignore[arg-type]
        id="employee_1", username="employee_1", display_name="Employee One", role="employee"
    )


@pytest.mark.parametrize(
    ("width", "height"),
    [(480, 854), (2000, 400)],
    ids=["portrait", "wide"],
)
def test_image_upload_probe_derives_bounded_thumbnail_jpeg(
    lane_env: str, pg: psycopg.Connection, width: int, height: int
) -> None:
    """真实 PNG 走完整上传探测链路：probe 产出可用的 JPEG 缩略图字节，与视频
    同一写入点、同一降级语义（失败只损失缩略图）；竖屏（手机照主场景）与
    超宽（五视图类）都在 ≤960px 宽度上界内."""
    from app import materials
    from app.material_thumbs import THUMBNAIL_MAX_WIDTH

    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    content = make_test_image(width=width, height=height)
    bus = BusinessConnection.postgres(pg)
    intent = materials.create_material_upload_intent(
        bus,
        actor=employee_actor(),
        storage=storage,
        request=materials.MaterialUploadIntentRequest(
            filename="picture.png", content_type="image/png", size_bytes=len(content)
        ),
    )
    storage.put_object(intent.storage_key, content, content_type="image/png")
    prepared = materials.prepare_material_upload(
        bus, actor=employee_actor(), asset_id=intent.asset_id
    )
    probed = materials.probe_material_upload(prepared, storage=storage)
    assert probed.duration_seconds is None
    assert probed.thumbnail_jpeg is not None
    assert probed.thumbnail_jpeg.startswith(JPEG_MAGIC)
    thumb_width, _thumb_height = jpeg_dimensions(probed.thumbnail_jpeg)
    assert thumb_width <= THUMBNAIL_MAX_WIDTH


def test_image_probe_survives_thumbnail_failure(lane_env: str, pg: psycopg.Connection) -> None:
    """抽帧失败只损失缩略图：magic 合法但字节截断的图片仍能完成上传探测，
    上传结果不受影响（降级不阻塞原链路）."""
    from app import materials

    storage = FakeStorageAdapter(provider="fake", bucket="matthumbs-tests")
    content = make_test_image()[:64]
    bus = BusinessConnection.postgres(pg)
    intent = materials.create_material_upload_intent(
        bus,
        actor=employee_actor(),
        storage=storage,
        request=materials.MaterialUploadIntentRequest(
            filename="truncated.png", content_type="image/png", size_bytes=len(content)
        ),
    )
    storage.put_object(intent.storage_key, content, content_type="image/png")
    prepared = materials.prepare_material_upload(
        bus, actor=employee_actor(), asset_id=intent.asset_id
    )
    probed = materials.probe_material_upload(prepared, storage=storage)
    assert probed.thumbnail_jpeg is None


def test_attach_records_thumbnail_key_for_image_assets(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """图片持久化后 attach 同链路把缩略图落到最终对象旁并记键，
    列表与批量授权的透出据此工作（真实 JPEG，来自 ffmpeg 单帧缩放）."""
    from app.material_thumbs import extract_image_thumbnail_jpeg
    from app.materials import attach_video_thumbnail

    seed_material_asset(
        pg,
        "attach_image",
        "employee_1",
        kind="material_image",
        content_type="image/png",
        object_suffix=".png",
        thumbnail_key=None,
    )
    thumb = extract_image_thumbnail_jpeg(make_test_image())
    assert thumb is not None
    attach_video_thumbnail(
        BusinessConnection.postgres(pg),
        asset_id="attach_image",
        thumbnail_jpeg=thumb,
        storage=fake_storage,
    )
    stored = fake_storage.get_object("perf/attach_image.png.thumb.jpg")
    assert stored is not None and stored.startswith(JPEG_MAGIC)
    row = pg.execute("SELECT metadata_json FROM assets WHERE id = %s", ("attach_image",)).fetchone()
    assert json.loads(row["metadata_json"])["thumbnail_key"] == ("perf/attach_image.png.thumb.jpg")
