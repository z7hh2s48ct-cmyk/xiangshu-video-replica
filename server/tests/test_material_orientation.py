"""MATERIAL-UX-08 — 方向筛选与宽高契约 PG 矩阵.

任务卡 docs/素材库改进工作任务清单-2026-09-22.md §2「宽高比数据与方向筛选」。
本模块钉住：

- ``MaterialItem.width/height/aspect_ratio``：从 ``assets.metadata_json`` 派生；
  缺字段的存量素材返回 ``null``（前端优雅降级）。
- ``GET /api/studio/materials?orientation=``：portrait/landscape/square 精确筛选
  （阈值 >1.05 / <0.95 / 其余方形，与前端一致）；缺宽高的素材不命中任何方向；
  可与 media_type 组合；非法值 422。

PG 约束（沿 CW-031 / CW-078 / UX-03）：共享库 ``customer_v3_test``；模块级
``shared_suite_lock``；数据用 ``ux08-`` 前缀；绝不 TRUNCATE；CW-007 硬门。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import (
    database_name_of,
    require_pg_or_explicit_skip,
    resolve_test_dsn,
    shared_suite_lock,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser, get_current_user, get_database
from app.customer_fence import get_business_db
from app.db_pg import close_pg_pool
from app.db_portable import BusinessConnection
from app.material_routes import router as material_router

DB_NAME_SHARED = "customer_v3_test"
UX08 = "ux08-"
EMP1 = f"{UX08}emp1"
UX08_LIKE = f"{UX08}%"
MATERIALS_PATH = "/api/studio/materials"


def _actor(user_id: str, role: str = "employee") -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


def _clean_ux08_rows(conn: psycopg.Connection) -> None:
    conn.execute(
        "DELETE FROM audit_logs WHERE actor_user_id LIKE %s OR entity_id LIKE %s",
        (UX08_LIKE, UX08_LIKE),
    )
    conn.execute(
        "DELETE FROM studio_material_preferences WHERE user_id LIKE %s OR source_id LIKE %s",
        (UX08_LIKE, UX08_LIKE),
    )
    conn.execute(
        "DELETE FROM assets WHERE id LIKE %s OR created_by_user_id LIKE %s",
        (UX08_LIKE, UX08_LIKE),
    )
    conn.execute("DELETE FROM users WHERE id LIKE %s", (UX08_LIKE,))


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert database_name_of(dsn) == DB_NAME_SHARED
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        with psycopg.connect(dsn, autocommit=True) as conn:
            _clean_ux08_rows(conn)
        try:
            yield dsn
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                _clean_ux08_rows(conn)
            close_pg_pool()


@pytest.fixture()
def pg(pg_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    _clean_ux08_rows(conn)
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            (EMP1, EMP1, "UX08 Employee", "employee"),
        )
    try:
        yield conn
    finally:
        _clean_ux08_rows(conn)
        conn.close()
        close_pg_pool()


class _ScopedBusinessDb:
    def __init__(self, bus: BusinessConnection, actor: CurrentUser) -> None:
        self._bus = bus
        self._actor = actor

    @contextmanager
    def write(self) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        yield self._bus, self._actor


@pytest.fixture()
def client(pg: psycopg.Connection) -> TestClient:
    bus = BusinessConnection.postgres(pg)
    current = _actor(EMP1)
    application = FastAPI()
    application.include_router(material_router)
    application.dependency_overrides[get_database] = lambda: bus
    application.dependency_overrides[get_current_user] = lambda: current
    application.dependency_overrides[get_business_db] = lambda: _ScopedBusinessDb(bus, current)
    return TestClient(application)


def _seed_upload(
    conn: psycopg.Connection,
    asset_id: str,
    owner: str,
    *,
    content_type: str = "image/png",
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, NULL, 'material_image', %s, %s, 2048, %s, %s, %s, CURRENT_TIMESTAMP)",
        (
            asset_id,
            f"local://ux08/{asset_id}.png",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            content_type,
            owner,
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )


def _seed_portrait_video(
    conn: psycopg.Connection, asset_id: str, owner: str, *, width: int, height: int
) -> None:
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, NULL, 'material_video', %s, %s, 20480, 'video/mp4', %s, %s, "
        "CURRENT_TIMESTAMP)",
        (
            asset_id,
            f"local://ux08/{asset_id}.mp4",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            owner,
            json.dumps(
                {
                    "width": width,
                    "height": height,
                    "aspect_ratio": round(width / height, 2),
                }
            ),
        ),
    )


def _items(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get(MATERIALS_PATH, params=params)
    assert response.status_code == 200, response.text
    return cast(list[dict[str, Any]], response.json()["items"])


def test_legacy_material_without_dimensions_exposes_nulls(
    pg: psycopg.Connection, client: TestClient
) -> None:
    _seed_upload(pg, f"{UX08}legacy", EMP1)
    items = _items(client)
    assert len(items) == 1
    assert items[0]["width"] is None
    assert items[0]["height"] is None
    assert items[0]["aspect_ratio"] is None


def test_material_with_dimensions_exposes_fields(
    pg: psycopg.Connection, client: TestClient
) -> None:
    _seed_upload(
        pg,
        f"{UX08}with-size",
        EMP1,
        metadata={"width": 1080, "height": 1920, "aspect_ratio": 0.56},
    )
    items = _items(client)
    assert items[0]["width"] == 1080
    assert items[0]["height"] == 1920
    assert items[0]["aspect_ratio"] == 0.56


def test_orientation_filter_hits_each_direction(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_portrait_video(pg, f"{UX08}portrait", EMP1, width=1080, height=1920)
    _seed_upload(
        pg,
        f"{UX08}landscape",
        EMP1,
        metadata={"width": 1920, "height": 1080, "aspect_ratio": 1.78},
    )
    _seed_upload(
        pg,
        f"{UX08}square",
        EMP1,
        metadata={"width": 1000, "height": 1000, "aspect_ratio": 1.0},
    )
    _seed_upload(pg, f"{UX08}no-size", EMP1)

    assert [item["id"] for item in _items(client, orientation="portrait")] == [
        f"asset:{UX08}portrait"
    ]
    assert [item["id"] for item in _items(client, orientation="landscape")] == [
        f"asset:{UX08}landscape"
    ]
    assert [item["id"] for item in _items(client, orientation="square")] == [f"asset:{UX08}square"]


def test_orientation_filter_combines_with_media_type(
    pg: psycopg.Connection, client: TestClient
) -> None:
    _seed_portrait_video(pg, f"{UX08}p-video", EMP1, width=1080, height=1920)
    _seed_upload(
        pg,
        f"{UX08}p-image",
        EMP1,
        metadata={"width": 1080, "height": 1920, "aspect_ratio": 0.56},
    )
    combined = _items(client, orientation="portrait", media_type="image")
    assert [item["id"] for item in combined] == [f"asset:{UX08}p-image"]


def test_orientation_without_dimensions_matches_nothing(
    pg: psycopg.Connection, client: TestClient
) -> None:
    _seed_upload(pg, f"{UX08}bare", EMP1)
    assert _items(client, orientation="portrait") == []
    assert _items(client, orientation="landscape") == []
    assert _items(client, orientation="square") == []


def test_orientation_invalid_value_rejected(client: TestClient) -> None:
    response = client.get(MATERIALS_PATH, params={"orientation": "diagonal"})
    assert response.status_code == 422
