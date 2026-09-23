"""MATERIAL-PERF-A-20260917 — 批量素材预览授权接口（P0-2）PG 矩阵.

对应分析报告《素材库显示与页面切换性能根因分析与优化方案-2026-09-17》P0-2：
素材库网格当前对每个瓦片各发一次 ``POST /api/assets/{id}/download-url``（每页
6–24 条 = 同等数量的写库连接与审计往返）。本模块钉住新增批量通道
``POST /api/assets/download-urls`` 的安全与语义不变量：

- 一次写事务内逐资产复用与单资产端点完全相同的授权逻辑
  （require_not_auditor → require_asset_access → 审计 → 签名），审计行仍逐资产
  保留（口径不因批量而变稀）。
- 属主掩蔽与单端点一致：他属/缺失资产对该请求者返回逐条
  ``ASSET_NOT_FOUND``，不拖垮整批，也不泄漏存在性。
- 请求体大小上限 100（与素材列表 page_size 上限对齐），去重保序。
- auditor 整体 403（与单资产端点相同的角色门）。
- 响应附带 sha256/size_bytes/content_type，供前端省掉逐条
  ``GET /api/assets/{id}`` 元数据往返（字段本就可通过单资产端点读取，无新增暴露面）。

鉴权注入沿用 CW-058 先例：``BusinessDbDep`` 路由在配置 PG 后走客户会话通道
（CW-031 锁定开发身份头不再被接受），路由级测试覆写 ``get_business_db`` 提供
真实 PG 连接 + 指定 actor，被测不变量是批量授权/掩蔽语义而非会话鉴权本身。
存储在模块缝隙 ``app.rbac_routes.storage_for_asset`` 处替身（与
test_oral_domain.py 同一手法）。专属库 ``matperf_a_batch_urls_test``，用例间
TRUNCATE 隔离；零 SQLite 替代、零缺库 skip。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.customer_fence import get_business_db
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection
from app.storage import FakeStorageAdapter

MATPERF_TEST_DB = "matperf_a_batch_urls_test"
BATCH_URL = "/api/assets/download-urls"


@pytest.fixture(scope="module")
def matperf_dsn() -> Iterator[str]:
    """专属批量授权测试库：建库 → alembic head → 用完即删."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(MATPERF_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(MATPERF_TEST_DB)


@pytest.fixture()
def pg(matperf_dsn: str) -> Iterator[psycopg.Connection]:
    """autocommit 原生连接：播种用户/资产；用例间 TRUNCATE 隔离."""
    close_pg_pool()
    conn = psycopg.connect(matperf_dsn, autocommit=True)
    conn.execute("SET session_replication_role = replica")
    conn.execute("TRUNCATE users, assets, audit_logs CASCADE")
    conn.execute("SET session_replication_role = DEFAULT")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
                ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ],
        )
    try:
        yield conn
    finally:
        conn.close()
        close_pg_pool()


@pytest.fixture()
def lane_env(matperf_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """DATABASE_URL_ENV 指向专属库：pg_transaction 走生产 PG 通道."""
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, matperf_dsn)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    yield matperf_dsn
    close_pg_pool()


@pytest.fixture()
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorageAdapter:
    """模块缝隙存储替身：授权只读元数据，不触真实对象存储."""
    storage = FakeStorageAdapter(provider="fake", bucket="matperf-tests")
    monkeypatch.setattr("app.rbac_routes.storage_for_asset", lambda _conn, _uri: storage)
    return storage


class ScopedBusinessDb:
    """写端点身份替身（CW-058 ScopedTestDb 同款）：真实 PG 连接 + 指定 actor."""

    def __init__(self, bus: BusinessConnection, user_id: str, role: str) -> None:
        self._bus = bus
        self._user_id = user_id
        self._role = role

    @contextmanager
    def write(self) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        yield (
            self._bus,
            CurrentUser(  # type: ignore[arg-type]
                id=self._user_id,
                username=self._user_id,
                display_name=self._user_id,
                role=self._role,
            ),
        )


def seed_asset(
    pg: psycopg.Connection,
    asset_id: str,
    owner: str,
    *,
    kind: str = "material_image",
    size_bytes: int = 1024,
    sha256: str | None = None,
    content_type: str = "image/png",
) -> str:
    """播种可授权资产（size>0 且 sha256 非空，否则授权端点按未完成上传拒绝）."""
    digest = sha256 or hashlib.sha256(asset_id.encode()).hexdigest()
    storage = FakeStorageAdapter(provider="fake", bucket="matperf-tests")
    stored = storage.put_object(f"perf/{asset_id}", b"payload", content_type=content_type)
    pg.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json
        )
        VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, '{}')
        """,
        (asset_id, kind, stored.uri, digest, size_bytes, content_type, owner),
    )
    return digest


def batch_client(
    app: Any, pg: psycopg.Connection, user_id: str = "employee_1", role: str = "employee"
) -> TestClient:
    bus = BusinessConnection.postgres(pg)
    app.dependency_overrides[get_business_db] = lambda: ScopedBusinessDb(bus, user_id, role)
    return TestClient(app)


def post_batch(client: TestClient, asset_ids: list[str]) -> Any:
    return client.post(BATCH_URL, json={"asset_ids": asset_ids})


def test_batch_grants_each_owned_asset_with_metadata_and_per_asset_audit(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """属主批量授权：逐资产签名 URL + 元数据，审计行逐资产保留（口径不稀释）."""
    from app.main import app

    digest = seed_asset(pg, "mat_a1", "employee_1")
    seed_asset(pg, "mat_a2", "employee_1", kind="material_video", content_type="video/mp4")
    client = batch_client(app, pg)
    response = post_batch(client, ["mat_a1", "mat_a2"])
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["asset_id"] for item in items] == ["mat_a1", "mat_a2"]
    for item in items:
        assert item["error_code"] is None
        assert urlsplit(item["url"]).path.startswith("/api/assets/signed-objects/")
        # 签名参数齐全：expires/user_id/asset_id/sig（与单资产端点同一签名通道）。
        query = parse_qs(urlsplit(item["url"]).query)
        assert {"expires", "user_id", "asset_id", "sig"} <= set(query)
        assert query["asset_id"][0] == item["asset_id"]
        assert query["user_id"][0] == "employee_1"
    assert items[0]["sha256"] == digest
    assert items[0]["size_bytes"] == 1024
    assert items[0]["content_type"] == "image/png"
    assert items[1]["content_type"] == "video/mp4"
    rows = pg.execute(
        "SELECT entity_id FROM audit_logs WHERE action = 'asset.download_url.create' "
        "ORDER BY entity_id"
    ).fetchall()
    assert [row[0] for row in rows] == ["mat_a1", "mat_a2"]
    app.dependency_overrides.clear()


def test_grants_are_absolute_so_cross_origin_clients_can_load_them(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """授权地址必须是绝对地址：桌面端页面 origin 是 ``tauri://``，客户云版前端
    可与 API 分域名部署，站内相对地址会打到客户端自身而不是后端，``img``/
    ``video`` 只剩空预览框。地址由服务端签全，客户端零拼接——已发布的旧客户端
    无需升级即可恢复预览。"""
    from app.main import app

    seed_asset(pg, "mat_abs", "employee_1", kind="material_video", content_type="video/mp4")
    client = batch_client(app, pg)
    response = post_batch(client, ["mat_abs"])
    assert response.status_code == 200
    item = response.json()["items"][0]
    # 未配 PUBLIC_BASE_URL 时 api_base_url() 回退到桌面端本机地址。
    assert item["url"].startswith("http://127.0.0.1:8000/api/assets/signed-objects/")
    app.dependency_overrides.clear()


def test_public_base_url_drives_the_signed_origin(
    lane_env: str,
    pg: psycopg.Connection,
    fake_storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署后对外地址来自 PUBLIC_BASE_URL（与自有封面路由共用同一处配置）."""
    from app.main import app

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://studio.example.com")
    seed_asset(pg, "mat_cfg", "employee_1")
    client = batch_client(app, pg)
    item = post_batch(client, ["mat_cfg"]).json()["items"][0]
    assert item["url"].startswith("https://studio.example.com/api/assets/signed-objects/")
    app.dependency_overrides.clear()


def test_batch_masks_foreign_and_missing_assets_per_item(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """他属/缺失资产逐条 ASSET_NOT_FOUND：不拖垮整批、不泄漏存在性（与单端点同形）."""
    from app.main import app

    seed_asset(pg, "mine", "employee_1")
    seed_asset(pg, "foreign", "employee_2")
    client = batch_client(app, pg)
    response = post_batch(client, ["mine", "foreign", "missing"])
    assert response.status_code == 200
    items = {item["asset_id"]: item for item in response.json()["items"]}
    assert items["mine"]["error_code"] is None
    assert urlsplit(items["mine"]["url"]).path.startswith("/api/assets/signed-objects/")
    assert items["foreign"]["url"] is None
    assert items["foreign"]["error_code"] == "ASSET_NOT_FOUND"
    assert items["missing"]["url"] is None
    assert items["missing"]["error_code"] == "ASSET_NOT_FOUND"
    # 失败项不写授权审计，成功项照常。
    rows = pg.execute(
        "SELECT entity_id FROM audit_logs WHERE action = 'asset.download_url.create'"
    ).fetchall()
    assert [row[0] for row in rows] == ["mine"]
    app.dependency_overrides.clear()


def test_batch_enforces_hundred_id_cap_and_rejects_empty(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """上限 100（与素材列表 page_size 上限对齐），空列表 422."""
    from app.main import app

    client = batch_client(app, pg)
    too_many = post_batch(client, [f"asset-{index}" for index in range(101)])
    assert too_many.status_code == 422
    assert client.post(BATCH_URL, json={"asset_ids": []}).status_code == 422
    assert client.post(BATCH_URL, json={}).status_code == 422
    app.dependency_overrides.clear()


def test_batch_deduplicates_while_preserving_request_order(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """重复 ID 去重保序：一个资产只签一次、审计一次."""
    from app.main import app

    seed_asset(pg, "dup_a", "employee_1")
    seed_asset(pg, "dup_b", "employee_1")
    client = batch_client(app, pg)
    response = post_batch(client, ["dup_b", "dup_a", "dup_b", "dup_a"])
    assert response.status_code == 200
    assert [item["asset_id"] for item in response.json()["items"]] == ["dup_b", "dup_a"]
    rows = pg.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action = 'asset.download_url.create'"
    ).fetchone()
    assert rows is not None and rows[0] == 2
    app.dependency_overrides.clear()


def test_batch_denies_auditor_like_single_endpoint(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """auditor 整体 403（角色门在批量入口一次执行，语义与单资产端点一致）."""
    from app.main import app

    seed_asset(pg, "audited", "employee_1")
    client = batch_client(app, pg, user_id="auditor_1", role="auditor")
    response = post_batch(client, ["audited"])
    assert response.status_code == 403
    rows = pg.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action = 'asset.download_url.create'"
    ).fetchone()
    assert rows is not None and rows[0] == 0
    app.dependency_overrides.clear()


def test_batch_incomplete_upload_reports_per_item_not_found_code(
    lane_env: str, pg: psycopg.Connection, fake_storage: FakeStorageAdapter
) -> None:
    """未完成上传（sha256 空 / size 0）逐条报 ASSET_UPLOAD_NOT_COMPLETE，不拖垮整批."""
    from app.main import app

    seed_asset(pg, "ready", "employee_1")
    seed_asset(pg, "pending", "employee_1", size_bytes=0, sha256="")
    client = batch_client(app, pg)
    response = post_batch(client, ["pending", "ready"])
    assert response.status_code == 200
    items = {item["asset_id"]: item for item in response.json()["items"]}
    assert items["pending"]["error_code"] == "ASSET_UPLOAD_NOT_COMPLETE"
    assert urlsplit(items["ready"]["url"]).path.startswith("/api/assets/signed-objects/")
    app.dependency_overrides.clear()
