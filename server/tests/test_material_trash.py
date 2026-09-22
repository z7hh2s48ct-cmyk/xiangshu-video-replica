"""MATERIAL-UX-09 — 回收站（已移除视图与恢复）PG 矩阵.

任务卡 docs/素材库改进工作任务清单-2026-09-22.md §3「回收站」。本模块钉住：

- 默认列表（trashed 缺省 false）不含已移除素材（hidden=1）——既有行为不变。
- ``GET /api/studio/materials?trashed=true``：只返回已移除素材。
- 恢复 = 既有 ``PATCH /materials/{id} {"hidden": false}``：恢复后回到默认视图，
  且 title/group 偏好保留（preserve 语义）——素材回到原分组。
- 物理删除不在本任务范围（内容寻址引用计数闸门另行处理）。

PG 约束（沿 CW-031 / CW-078 / UX-03）：共享库 ``customer_v3_test``；模块级
``shared_suite_lock``；数据用 ``ux09-`` 前缀；绝不 TRUNCATE；CW-007 硬门。
"""

from __future__ import annotations

import hashlib
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
UX09 = "ux09-"
EMP1 = f"{UX09}emp1"
UX09_LIKE = f"{UX09}%"
MATERIALS_PATH = "/api/studio/materials"


def _actor(user_id: str, role: str = "employee") -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


def _clean_ux09_rows(conn: psycopg.Connection) -> None:
    conn.execute(
        "DELETE FROM audit_logs WHERE actor_user_id LIKE %s OR entity_id LIKE %s",
        (UX09_LIKE, UX09_LIKE),
    )
    conn.execute(
        "DELETE FROM studio_material_preferences WHERE user_id LIKE %s OR source_id LIKE %s",
        (UX09_LIKE, UX09_LIKE),
    )
    conn.execute(
        "DELETE FROM assets WHERE id LIKE %s OR created_by_user_id LIKE %s",
        (UX09_LIKE, UX09_LIKE),
    )
    conn.execute("DELETE FROM users WHERE id LIKE %s", (UX09_LIKE,))


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert database_name_of(dsn) == DB_NAME_SHARED
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        with psycopg.connect(dsn, autocommit=True) as conn:
            _clean_ux09_rows(conn)
        try:
            yield dsn
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                _clean_ux09_rows(conn)
            close_pg_pool()


@pytest.fixture()
def pg(pg_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    _clean_ux09_rows(conn)
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            (EMP1, EMP1, "UX09 Employee", "employee"),
        )
    try:
        yield conn
    finally:
        _clean_ux09_rows(conn)
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


def _seed_upload_with_group(
    conn: psycopg.Connection, asset_id: str, owner: str, *, group: str
) -> None:
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, NULL, 'material_image', %s, %s, 2048, 'image/png', %s, '{}', "
        "CURRENT_TIMESTAMP)",
        (
            asset_id,
            f"local://ux09/{asset_id}.png",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            owner,
        ),
    )
    conn.execute(
        "INSERT INTO studio_material_preferences (user_id, source_type, source_id, "
        "group_override, hidden, tags_json) VALUES (%s, 'asset', %s, %s, 0, '[]')",
        (owner, asset_id, group),
    )


def _remove(client: TestClient, asset_id: str) -> None:
    response = client.patch(f"{MATERIALS_PATH}/asset:{asset_id}", json={"hidden": True})
    assert response.status_code == 200, response.text


def _ids(client: TestClient, **params: Any) -> list[str]:
    response = client.get(MATERIALS_PATH, params=params)
    assert response.status_code == 200, response.text
    return [str(item["id"]) for item in cast(dict[str, Any], response.json())["items"]]


def test_trashed_view_and_restore_roundtrip(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_upload_with_group(pg, f"{UX09}kept", EMP1, group="庭院专题")
    _seed_upload_with_group(pg, f"{UX09}gone", EMP1, group="庭院专题")

    # 默认视图两件都在
    assert set(_ids(client)) == {f"asset:{UX09}kept", f"asset:{UX09}gone"}

    # 移除一件：默认视图不再出现；trashed=true 只出现这一件
    _remove(client, f"{UX09}gone")
    assert _ids(client) == [f"asset:{UX09}kept"]
    assert _ids(client, trashed=True) == [f"asset:{UX09}gone"]

    # 恢复：回到默认视图，原分组保留
    restore = client.patch(f"{MATERIALS_PATH}/asset:{UX09}gone", json={"hidden": False})
    assert restore.status_code == 200, restore.text
    assert restore.json()["hidden"] is False
    assert restore.json()["group"] == "庭院专题"
    assert set(_ids(client)) == {f"asset:{UX09}kept", f"asset:{UX09}gone"}
    assert _ids(client, trashed=True) == []


def test_trashed_view_counts_and_isolation(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_upload_with_group(pg, f"{UX09}mine", EMP1, group="我的上传")
    _remove(client, f"{UX09}mine")

    body = client.get(MATERIALS_PATH, params={"trashed": True}).json()
    assert body["total"] == 1

    # 分组聚合（未移除口径）不包含已移除素材的分组
    groups = client.get(f"{MATERIALS_PATH}/groups").json()
    assert all(item["count"] == 0 for item in groups["items"]) or all(
        item["name"] != "我的上传" for item in groups["items"]
    )
