"""MATERIAL-UX-10 — 使用记录（单素材按需查询）PG 矩阵.

任务卡 docs/素材库改进工作任务清单-2026-09-22.md §3「使用记录」。本模块钉住：

- ``GET /api/studio/materials/{id}/usages``：返回引用该素材的任务
  （generation 经 batch 归属、oral 直属 owner），按创建时间倒序；
  ``total`` 为全量计数（LIMIT 20 截断时仍准确）。
- owner 围栏：他人素材 404（require_material 路径）。
- 跨用户任务引用场景不在本矩阵：候选集语义本就排除“被任何任务引用的
  upload 资产”，与任务归属无关（业务上任务只引用本人素材）。
- 无引用素材返回 ``{"total": 0, "items": []}``（前端显示“未被使用”）。

PG 约束（沿 CW-031 / CW-078 / UX-03）：共享库 ``customer_v3_test``；模块级
``shared_suite_lock``；数据用 ``ux10-`` 前缀；绝不 TRUNCATE；CW-007 硬门。
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
UX10 = "ux10-"
EMP1 = f"{UX10}emp1"
EMP2 = f"{UX10}emp2"
UX10_LIKE = f"{UX10}%"
MATERIALS_PATH = "/api/studio/materials"


def _actor(user_id: str, role: str = "employee") -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


def _clean_ux10_rows(conn: psycopg.Connection) -> None:
    conn.execute(
        "DELETE FROM audit_logs WHERE actor_user_id LIKE %s OR entity_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute(
        "DELETE FROM studio_material_preferences WHERE user_id LIKE %s OR source_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute(
        "DELETE FROM generation_tasks WHERE id LIKE %s OR batch_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute("DELETE FROM generation_batches WHERE id LIKE %s", (UX10_LIKE,))
    conn.execute("DELETE FROM oral_tasks WHERE id LIKE %s", (UX10_LIKE,))
    conn.execute(
        "DELETE FROM oral_avatars WHERE id LIKE %s OR owner_user_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute(
        "DELETE FROM person_identities WHERE id LIKE %s OR owner_user_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute(
        "DELETE FROM assets WHERE id LIKE %s OR created_by_user_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute("DELETE FROM projects WHERE id LIKE %s", (UX10_LIKE,))
    conn.execute("DELETE FROM users WHERE id LIKE %s", (UX10_LIKE,))


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert database_name_of(dsn) == DB_NAME_SHARED
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        with psycopg.connect(dsn, autocommit=True) as conn:
            _clean_ux10_rows(conn)
        try:
            yield dsn
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                _clean_ux10_rows(conn)
            close_pg_pool()


@pytest.fixture()
def pg(pg_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    _clean_ux10_rows(conn)
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                (EMP1, EMP1, "UX10 Employee One", "employee"),
                (EMP2, EMP2, "UX10 Employee Two", "employee"),
            ],
        )
    try:
        yield conn
    finally:
        _clean_ux10_rows(conn)
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


def _seed_material_asset(conn: psycopg.Connection, asset_id: str, owner: str) -> None:
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, NULL, 'material_video', %s, %s, 4096, 'video/mp4', %s, '{}', "
        "CURRENT_TIMESTAMP)",
        (
            asset_id,
            f"local://ux10/{asset_id}.mp4",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            owner,
        ),
    )


def _seed_generation_reference(
    conn: psycopg.Connection,
    owner: str,
    *,
    task_id: str,
    batch_id: str,
    asset_id: str,
) -> None:
    conn.execute(
        "INSERT INTO generation_batches (id, created_by_user_id, idempotency_key, "
        "request_hash, request_snapshot_json, status) VALUES (%s, %s, %s, %s, %s, "
        "'SUCCEEDED') ON CONFLICT (id) DO NOTHING",
        (
            batch_id,
            owner,
            f"ux10-idem-{batch_id}",
            f"ux10-hash-{batch_id}",
            "{}",
        ),
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, "
        "model, status, result_asset_id) VALUES (%s, %s, 'I2V', 'video_gen', "
        "'model-x', 'SUCCEEDED', %s) ON CONFLICT (id) DO NOTHING",
        (task_id, batch_id, asset_id),
    )


def _usages(client: TestClient, asset_id: str) -> dict[str, Any]:
    response = client.get(f"{MATERIALS_PATH}/asset:{asset_id}/usages")
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def test_unused_material_reports_zero(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_material_asset(pg, f"{UX10}lonely", EMP1)
    body = _usages(client, f"{UX10}lonely")
    assert body == {"total": 0, "items": []}


def test_generation_and_oral_references_listed(pg: psycopg.Connection, client: TestClient) -> None:
    asset_id = f"{UX10}referenced"
    _seed_material_asset(pg, asset_id, EMP1)

    # generation 引用（batch 归属 EMP1）
    _seed_generation_reference(
        pg,
        EMP1,
        task_id=f"{UX10}gtask",
        batch_id=f"{UX10}gbatch",
        asset_id=asset_id,
    )

    # oral 引用（oral_tasks 直属 EMP1）
    identity_id = f"{UX10}identity"
    avatar_id = f"{UX10}avatar"
    oral_id = f"{UX10}otask"
    pg.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, "
        "authorization_status, authorization_scope, source_quality_status, status, created_by) "
        "VALUES (%s, %s, 'UX10 口播人', 'AUTHORIZED', '[]', 'PASSED', 'ACTIVE', %s)",
        (identity_id, EMP1, EMP1),
    )
    pg.execute(
        "INSERT INTO oral_avatars (id, identity_id, owner_user_id, title, "
        "source_kind, source_asset_id, vendor_avatar_id, status) VALUES "
        "(%s, %s, %s, 'UX10 形象', 'IMAGE', %s, %s, 'READY') "
        "ON CONFLICT (id) DO NOTHING",
        (avatar_id, identity_id, EMP1, asset_id, avatar_id),
    )
    pg.execute(
        "INSERT INTO oral_tasks (id, owner_user_id, identity_id, avatar_id, mode, "
        "title, idempotency_key, estimated_cost_fen, status, result_asset_id) "
        "VALUES (%s, %s, %s, %s, 'AUDIO', 'UX10 口播', %s, 0, 'SUCCEEDED', %s) "
        "ON CONFLICT (id) DO NOTHING",
        (oral_id, EMP1, identity_id, avatar_id, f"ux10-idem-{oral_id}", asset_id),
    )

    body = _usages(client, asset_id)
    assert body["total"] == 2
    kinds = {item["kind"] for item in body["items"]}
    assert kinds == {"generation", "oral"}
    task_ids = {item["task_id"] for item in body["items"]}
    assert task_ids == {f"{UX10}gtask", f"{UX10}otask"}


def test_other_users_material_not_found(pg: psycopg.Connection) -> None:
    _seed_material_asset(pg, f"{UX10}theirs", EMP2)
    client = _build_client(pg, user_id=EMP1)
    response = client.get(f"{MATERIALS_PATH}/asset:{UX10}theirs/usages")
    assert response.status_code == 404
