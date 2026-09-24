"""MATERIAL-UX-05-20260922 — 标签底座（后端契约）PG 矩阵.

任务卡 docs/素材库改进工作任务清单-2026-09-22.md §2「标签底座」
（方案 §4.3 P1-1）。本模块钉住后端契约：

- ``MaterialItem.tags``：默认 ``[]``；PATCH 提交 tags 全量覆盖（含 ``[]`` 清空），
  未提交该字段时保留既有标签（与 title/group 的 COALESCE 语义一致）。
- 服务端规整：trim、去空串、按首次出现去重；单素材 >20 个或单条 >40 字符
  拒绝 422 ``MATERIAL_TAGS_INVALID``（可预期的显式失败优于静默截断）。
- ``GET /api/studio/materials?tag=``：JSONB 包含过滤（jsonb_exists），可与
  media_type / source / group 等既有筛选组合；未命中返回空而非报错。
- ``GET /api/studio/materials/tags``：当前用户可见素材的标签聚合计数
  （count DESC, tag ASC），owner 围栏与列表一致——他人标签不出现。

PG 约束（沿 CW-031 / CW-078 / UX-03）：不建新库，只用共享库 ``customer_v3_test``；
模块级持 ``shared_suite_lock``；全部数据用唯一 ``ux05-`` 前缀；**绝不 TRUNCATE 共享
业务表**，收尾只 ``DELETE`` ux05- 行；全部 PG 用例挂 CW-007 硬门（fixture 不可达即
fail，不 skip）。
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
UX05 = "ux05-"
EMP1 = f"{UX05}emp1"
EMP2 = f"{UX05}emp2"
UX05_LIKE = f"{UX05}%"
MATERIALS_PATH = "/api/studio/materials"
TAGS_PATH = "/api/studio/materials/tags"


def _actor(user_id: str, role: str = "employee") -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 共享库 fixture + ux05- 前缀种子/清理（绝不新建 PG 库，绝不 TRUNCATE）
# ---------------------------------------------------------------------------


def _clean_ux05_rows(conn: psycopg.Connection) -> None:
    conn.execute(
        "DELETE FROM audit_logs WHERE actor_user_id LIKE %s OR entity_id LIKE %s",
        (UX05_LIKE, UX05_LIKE),
    )
    conn.execute(
        "DELETE FROM studio_material_preferences WHERE user_id LIKE %s OR source_id LIKE %s",
        (UX05_LIKE, UX05_LIKE),
    )
    conn.execute(
        "DELETE FROM assets WHERE id LIKE %s OR created_by_user_id LIKE %s",
        (UX05_LIKE, UX05_LIKE),
    )
    conn.execute("DELETE FROM projects WHERE id LIKE %s", (UX05_LIKE,))
    conn.execute("DELETE FROM users WHERE id LIKE %s", (UX05_LIKE,))


def _clean_ux05(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        _clean_ux05_rows(conn)


def _seed_users(conn: psycopg.Connection) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                (EMP1, EMP1, "UX05 Employee One", "employee"),
                (EMP2, EMP2, "UX05 Employee Two", "employee"),
            ],
        )


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert database_name_of(dsn) == DB_NAME_SHARED, "本套件只准用共享库 customer_v3_test"
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        _clean_ux05(dsn)
        try:
            yield dsn
        finally:
            _clean_ux05(dsn)
            close_pg_pool()


@pytest.fixture()
def pg(pg_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    _clean_ux05_rows(conn)
    _seed_users(conn)
    try:
        yield conn
    finally:
        _clean_ux05_rows(conn)
        conn.close()
        close_pg_pool()


class _ScopedBusinessDb:
    def __init__(self, bus: BusinessConnection, actor: CurrentUser) -> None:
        self._bus = bus
        self._actor = actor

    @contextmanager
    def write(self) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        yield self._bus, self._actor


def _build_client(
    pg: psycopg.Connection, *, user_id: str = EMP1, role: str = "employee"
) -> TestClient:
    bus = BusinessConnection.postgres(pg)
    current = _actor(user_id, role)
    application = FastAPI()
    application.include_router(material_router)
    application.dependency_overrides[get_database] = lambda: bus
    application.dependency_overrides[get_current_user] = lambda: current
    application.dependency_overrides[get_business_db] = lambda: _ScopedBusinessDb(bus, current)
    return TestClient(application)


@pytest.fixture()
def client(pg: psycopg.Connection) -> TestClient:
    return _build_client(pg)


# ---------------------------------------------------------------------------
# 断言辅助与素材种子
# ---------------------------------------------------------------------------


def _list_page(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get(MATERIALS_PATH, params=params)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _items(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _list_page(client, **params)["items"])


def _seed_upload(
    conn: psycopg.Connection,
    asset_id: str,
    owner: str,
    *,
    content_type: str = "image/png",
    size_bytes: int = 2048,
) -> None:
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, NULL, 'material_image', %s, %s, %s, %s, %s, '{}', CURRENT_TIMESTAMP)",
        (
            asset_id,
            f"local://ux05/{asset_id}.png",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            size_bytes,
            content_type,
            owner,
        ),
    )


def _patch(client: TestClient, material_id: str, payload: dict[str, Any]) -> Any:
    response = client.patch(f"{MATERIALS_PATH}/{material_id}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 用例：MaterialItem.tags 与 PATCH 语义
# ---------------------------------------------------------------------------


def test_default_material_has_empty_tags(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_upload(pg, f"{UX05}plain", EMP1)
    items = _items(client)
    assert len(items) == 1
    assert items[0]["tags"] == []


def test_patch_sets_and_normalizes_tags(pg: psycopg.Connection, client: TestClient) -> None:
    asset_id = f"{UX05}norm"
    _seed_upload(pg, asset_id, EMP1)
    material_id = f"asset:{asset_id}"
    updated = _patch(client, material_id, {"tags": ["  庭院 ", "庭院", "", "外观"]})
    assert updated["tags"] == ["庭院", "外观"]


def test_patch_without_tags_field_preserves_existing(
    pg: psycopg.Connection, client: TestClient
) -> None:
    asset_id = f"{UX05}preserve"
    _seed_upload(pg, asset_id, EMP1)
    material_id = f"asset:{asset_id}"
    _patch(client, material_id, {"tags": ["庭院"]})
    updated = _patch(client, material_id, {"title": "新名称"})
    assert updated["tags"] == ["庭院"]


def test_patch_empty_list_clears_tags(pg: psycopg.Connection, client: TestClient) -> None:
    asset_id = f"{UX05}clear"
    _seed_upload(pg, asset_id, EMP1)
    material_id = f"asset:{asset_id}"
    _patch(client, material_id, {"tags": ["庭院"]})
    updated = _patch(client, material_id, {"tags": []})
    assert updated["tags"] == []


def test_patch_rejects_over_limit_tags(pg: psycopg.Connection, client: TestClient) -> None:
    asset_id = f"{UX05}limit"
    _seed_upload(pg, asset_id, EMP1)
    url = f"{MATERIALS_PATH}/asset:{asset_id}"
    too_many = client.patch(url, json={"tags": [f"t{i}" for i in range(21)]})
    assert too_many.status_code == 422
    assert too_many.json()["detail"]["code"] == "MATERIAL_TAGS_INVALID"
    too_long = client.patch(url, json={"tags": ["x" * 41]})
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["code"] == "MATERIAL_TAGS_INVALID"


# ---------------------------------------------------------------------------
# 用例：tag 过滤
# ---------------------------------------------------------------------------


def test_list_filter_by_tag(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_upload(pg, f"{UX05}tagged", EMP1)
    _seed_upload(pg, f"{UX05}untagged", EMP1)
    _patch(client, f"asset:{UX05}tagged", {"tags": ["庭院"]})

    assert [item["id"] for item in _items(client, tag="庭院")] == [f"asset:{UX05}tagged"]
    assert _items(client, tag="不存在") == []


def test_tag_filter_combines_with_media_type(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_upload(pg, f"{UX05}img", EMP1, content_type="image/png")
    _patch(client, f"asset:{UX05}img", {"tags": ["庭院"]})
    assert len(_items(client, tag="庭院", media_type="image")) == 1
    assert _items(client, tag="庭院", media_type="video") == []


# ---------------------------------------------------------------------------
# 用例：聚合端点
# ---------------------------------------------------------------------------


def test_tags_aggregation_counts_and_isolates_users(
    pg: psycopg.Connection, client: TestClient
) -> None:
    _seed_upload(pg, f"{UX05}agg-a", EMP1)
    _seed_upload(pg, f"{UX05}agg-b", EMP1)
    _seed_upload(pg, f"{UX05}agg-other", EMP2)
    _patch(client, f"asset:{UX05}agg-a", {"tags": ["庭院", "外观"]})
    _patch(client, f"asset:{UX05}agg-b", {"tags": ["庭院"]})
    _patch(_build_client(pg, user_id=EMP2), f"asset:{UX05}agg-other", {"tags": ["他人标签"]})

    response = client.get(TAGS_PATH)
    assert response.status_code == 200, response.text
    tags = cast(list[dict[str, Any]], response.json())
    assert tags == [
        {"tag": "庭院", "count": 2},
        {"tag": "外观", "count": 1},
    ]


# ---------------------------------------------------------------------------
# 用例：bulk 批量打标签
# ---------------------------------------------------------------------------


def test_bulk_update_tags(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_upload(pg, f"{UX05}bulk-a", EMP1)
    _seed_upload(pg, f"{UX05}bulk-b", EMP1)
    response = client.patch(
        f"{MATERIALS_PATH}/bulk",
        json={
            "material_ids": [f"asset:{UX05}bulk-a", f"asset:{UX05}bulk-b"],
            "update": {"tags": ["工地"]},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 2
    assert {item["id"] for item in _items(client, tag="工地")} == {
        f"asset:{UX05}bulk-a",
        f"asset:{UX05}bulk-b",
    }
