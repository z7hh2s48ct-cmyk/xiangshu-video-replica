"""MATERIAL-UX-03-20260922 — 素材浏览与信息补全（后端契约）PG 矩阵.

任务卡 docs/素材库改进工作任务清单-2026-09-22.md §1「浏览与信息补全」
（方案 §4.2 P0-3 排序 + 卡片信息、P0-5 对象筛选与归属显示）。本模块钉住后端契约：

- ``GET /api/studio/materials?sort=``：``created_desc``（默认，保持既有行为不变）/
  ``created_asc`` / ``title_asc`` / ``size_desc``；非法值 422；``size_desc`` 对
  ``size_bytes IS NULL``（direct 成片）稳定排在最后（NULLS LAST）；排序与分页组合
  语义正确（第 2 页不与第 1 页重叠、并集等于全集）。
- ``GET /api/studio/materials?person_id= / project_id=``：按对象维度精确筛选；
  与 scope（owner 围栏）叠加——越权对象 ID 返回空而非他人数据；两参数可组合。
- ``MaterialItem.person_name / project_title``：从既有 CTE 派生
  （人物分支 = ``person_identities.display_name``；项目/成片分支 = ``projects.name``）；
  无对应对象的分支为 ``None``（如「我的上传」）。

PG 约束（沿 CW-031 / CW-078 / UX-01）：不建新库，只用共享库 ``customer_v3_test``；
模块级持 ``shared_suite_lock``；全部数据用唯一 ``ux03-`` 前缀；**绝不 TRUNCATE 共享
业务表**，收尾只 ``DELETE`` ux03- 行；全部 PG 用例挂 CW-007 硬门（fixture 不可达即
fail，不 skip）。
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
UX03 = "ux03-"
EMP1 = f"{UX03}emp1"
EMP2 = f"{UX03}emp2"
UX03_LIKE = f"{UX03}%"
MATERIALS_PATH = "/api/studio/materials"


def _actor(user_id: str, role: str = "employee") -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 共享库 fixture + ux03- 前缀种子/清理（绝不新建 PG 库，绝不 TRUNCATE）
# ---------------------------------------------------------------------------


def _clean_ux03_rows(conn: psycopg.Connection) -> None:
    """删除全部 ux03- 作用域行（子表先于父表）；只 DELETE、绝不 TRUNCATE 共享表。"""
    conn.execute(
        "DELETE FROM audit_logs WHERE actor_user_id LIKE %s OR entity_id LIKE %s",
        (UX03_LIKE, UX03_LIKE),
    )
    conn.execute(
        "DELETE FROM studio_material_preferences WHERE user_id LIKE %s OR source_id LIKE %s",
        (UX03_LIKE, UX03_LIKE),
    )
    conn.execute(
        "DELETE FROM generation_tasks WHERE id LIKE %s OR batch_id LIKE %s", (UX03_LIKE, UX03_LIKE)
    )
    conn.execute("DELETE FROM generation_batches WHERE id LIKE %s", (UX03_LIKE,))
    conn.execute(
        "DELETE FROM character_assets WHERE id LIKE %s OR character_version_id LIKE %s",
        (UX03_LIKE, UX03_LIKE),
    )
    conn.execute("DELETE FROM character_versions WHERE id LIKE %s", (UX03_LIKE,))
    conn.execute("DELETE FROM character_personas WHERE id LIKE %s", (UX03_LIKE,))
    conn.execute("DELETE FROM oral_tasks WHERE id LIKE %s", (UX03_LIKE,))
    conn.execute(
        "DELETE FROM oral_avatars WHERE id LIKE %s OR owner_user_id LIKE %s",
        (UX03_LIKE, UX03_LIKE),
    )
    conn.execute(
        "DELETE FROM person_identities WHERE id LIKE %s OR owner_user_id LIKE %s",
        (UX03_LIKE, UX03_LIKE),
    )
    conn.execute(
        "DELETE FROM assets WHERE id LIKE %s OR created_by_user_id LIKE %s", (UX03_LIKE, UX03_LIKE)
    )
    conn.execute("DELETE FROM projects WHERE id LIKE %s", (UX03_LIKE,))
    conn.execute("DELETE FROM users WHERE id LIKE %s", (UX03_LIKE,))


def _clean_ux03(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        _clean_ux03_rows(conn)


def _seed_users(conn: psycopg.Connection) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                (EMP1, EMP1, "UX03 Employee One", "employee"),
                (EMP2, EMP2, "UX03 Employee Two", "employee"),
            ],
        )


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    """共享库 DSN + CW-007 硬门 + 迁移到 head + 模块级套件锁（沿 CW-078）。"""
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert database_name_of(dsn) == DB_NAME_SHARED, "本套件只准用共享库 customer_v3_test"
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        _clean_ux03(dsn)
        try:
            yield dsn
        finally:
            _clean_ux03(dsn)
            close_pg_pool()


@pytest.fixture()
def pg(pg_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    _clean_ux03_rows(conn)
    _seed_users(conn)
    try:
        yield conn
    finally:
        _clean_ux03_rows(conn)
        conn.close()
        close_pg_pool()


class _ScopedBusinessDb:
    """把 ``db.write()`` 固定为（共享连接，指定 actor）——业务写围栏另测。"""

    def __init__(self, bus: BusinessConnection, actor: CurrentUser) -> None:
        self._bus = bus
        self._actor = actor

    @contextmanager
    def write(self) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        yield self._bus, self._actor


def _build_client(
    pg: psycopg.Connection, *, user_id: str = EMP1, role: str = "employee"
) -> TestClient:
    """只挂素材路由的子集 app，覆写鉴权/连接依赖以隔离被测单元。"""
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
# 断言辅助与素材种子（created_at / size_bytes / title 参数化以验证排序）
# ---------------------------------------------------------------------------


def _list_page(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get(MATERIALS_PATH, params=params)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _items(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _list_page(client, **params)["items"])


def _ids(client: TestClient, **params: Any) -> list[str]:
    return [str(item["id"]) for item in _items(client, **params)]


def _seed_upload(
    conn: psycopg.Connection,
    asset_id: str,
    owner: str,
    *,
    content_type: str = "image/png",
    kind: str = "material_image",
    size_bytes: int = 2048,
    created_at: str | None = None,
    original_filename: str | None = None,
) -> None:
    """「我的上传」候选：project_id NULL + material_* kind + 非空 sha256（ready）。"""
    metadata = json.dumps(
        {"original_filename": original_filename} if original_filename else {},
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, "
        "COALESCE(%s::timestamptz, CURRENT_TIMESTAMP))",
        (
            asset_id,
            kind,
            f"local://ux03/{asset_id}.png",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            size_bytes,
            content_type,
            owner,
            metadata,
            created_at,
        ),
    )


def _seed_project_asset(
    conn: psycopg.Connection,
    asset_id: str,
    *,
    project_id: str,
    owner: str,
    project_name: str | None = None,
    size_bytes: int = 4096,
    created_at: str | None = None,
) -> None:
    """「项目素材」候选：asset 挂在项目上且未被 generation_tasks 引用；base_title=project.name。"""
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        (project_id, owner, project_name or f"UX03 {project_id}"),
    )
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, %s, 'project_image', %s, %s, %s, 'image/png', %s, '{}', "
        "COALESCE(%s::timestamptz, CURRENT_TIMESTAMP))",
        (
            asset_id,
            project_id,
            f"local://ux03/{asset_id}.png",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            size_bytes,
            owner,
            created_at,
        ),
    )


def _seed_identity(conn: psycopg.Connection, key: str, *, owner: str, display_name: str) -> str:
    identity_id = f"{UX03}identity-{key}"
    conn.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, "
        "authorization_status, authorization_scope, source_quality_status, status, created_by) "
        "VALUES (%s, %s, %s, 'AUTHORIZED', '[]', 'PASSED', 'ACTIVE', %s) "
        "ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name",
        (identity_id, owner, display_name, owner),
    )
    return identity_id


def _seed_published_sheet(
    conn: psycopg.Connection,
    key: str,
    *,
    owner: str,
    display_name: str,
    created_at: str | None = None,
) -> tuple[str, str, str]:
    """播种 PUBLISHED 人物 contact sheet + 一条已发布五视图派生资产.

    返回 ``(identity_id, contact_sheet_asset_id, derived_view_asset_id)``。
    人物分支 person_name 应派生为 ``display_name``。
    """
    identity_id = _seed_identity(conn, key, owner=owner, display_name=display_name)
    persona_id = f"{UX03}persona-{key}"
    version_id = f"{UX03}version-{key}"
    sheet_asset_id = f"{UX03}sheet-{key}"
    view_asset_id = f"{UX03}view-{key}"
    for asset_id, asset_kind in (
        (sheet_asset_id, "character_contact_sheet"),
        (view_asset_id, "character_approved_image"),
    ):
        conn.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id, metadata_json, created_at) "
            "VALUES (%s, NULL, %s, %s, %s, 4096, 'image/png', %s, '{}', "
            "COALESCE(%s::timestamptz, CURRENT_TIMESTAMP))",
            (
                asset_id,
                asset_kind,
                f"local://ux03/{asset_id}.png",
                hashlib.sha256(asset_id.encode()).hexdigest(),
                owner,
                created_at,
            ),
        )
    conn.execute(
        "INSERT INTO character_personas (id, identity_id, name, appearance_constraints_json, "
        "usage_scope_json, created_by) VALUES (%s, %s, %s, '{}', '[]', %s)",
        (persona_id, identity_id, f"{key} 项目经理", owner),
    )
    snapshot = json.dumps(
        {"schema_version": "character-publication.v1", "contact_sheet_asset_id": sheet_asset_id}
    )
    conn.execute(
        "INSERT INTO character_versions (id, persona_id, version_number, status, "
        "persona_snapshot_json, generation_params_json, required_view_types_json, "
        "publication_snapshot_json, publication_hash) "
        "VALUES (%s, %s, 1, 'PUBLISHED', '{}', '{}', '[]', %s, %s)",
        (version_id, persona_id, snapshot, hashlib.sha256(snapshot.encode()).hexdigest()),
    )
    conn.execute(
        "INSERT INTO character_assets (id, character_version_id, asset_id, view_type, "
        "candidate_number, review_status, is_published_selection) "
        "VALUES (%s, %s, %s, 'FRONT_FULL', 1, 'APPROVED', 1)",
        (f"{UX03}character-asset-{key}", version_id, view_asset_id),
    )
    return identity_id, sheet_asset_id, view_asset_id


def _seed_oral(
    conn: psycopg.Connection,
    *,
    task_id: str,
    asset_id: str,
    owner: str,
    identity_id: str,
    title: str,
    created_at: str | None = None,
) -> None:
    """「口播成片」候选：oral_tasks SUCCEEDED + result_asset.

    base_title=oral.title，person_name=identity.display_name。
    """
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, NULL, 'oral_video', %s, %s, 8192, 'video/mp4', %s, '{}', "
        "COALESCE(%s::timestamptz, CURRENT_TIMESTAMP))",
        (
            asset_id,
            f"local://ux03/{asset_id}.mp4",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            owner,
            created_at,
        ),
    )
    avatar_id = f"{UX03}avatar-{task_id}"
    conn.execute(
        "INSERT INTO oral_avatars (id, identity_id, owner_user_id, title, vendor_avatar_id, "
        "status, source_kind, source_asset_id) "
        "VALUES (%s, %s, %s, %s, %s, 'READY', 'VIDEO', %s) "
        "ON CONFLICT (id) DO NOTHING",
        (avatar_id, identity_id, owner, f"{task_id} avatar", f"vendor-{avatar_id}", asset_id),
    )
    conn.execute(
        "INSERT INTO oral_tasks (id, owner_user_id, identity_id, avatar_id, mode, title, "
        "status, estimated_cost_fen, idempotency_key, request_hash, result_asset_id) "
        "VALUES (%s, %s, %s, %s, 'TTS', %s, 'SUCCEEDED', 0, %s, 'ux03-hash', %s)",
        (task_id, owner, identity_id, avatar_id, title, f"{task_id}-key", asset_id),
    )


def _seed_direct_generation(
    conn: psycopg.Connection,
    *,
    task_id: str,
    batch_id: str,
    project_id: str,
    owner: str,
    project_name: str | None = None,
) -> None:
    """直出成片（provider-hosted DIRECT、无本存档，size_bytes NULL）.

    id=``generation:{task_id}``。
    """
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        (project_id, owner, project_name or f"UX03 {project_id}"),
    )
    conn.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, idempotency_key, "
        "request_hash, request_snapshot_json, status) "
        "VALUES (%s, %s, %s, %s, 'ux03-hash', '{}', 'SUCCEEDED')",
        (batch_id, project_id, owner, f"{batch_id}-key"),
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, provider, model, status, archive_status, "
        "provider_result_url, completed_at) "
        "VALUES (%s, %s, 'metaso', 'MiniMax-H3', 'SUCCEEDED', 'DIRECT', %s, CURRENT_TIMESTAMP)",
        (task_id, batch_id, f"https://cdn.example.com/{task_id}.mp4"),
    )


# ===========================================================================
# A 组 — sort 排序（默认不回归 / 四种排序 / NULLS LAST / 非法值 / 分页组合）
# ===========================================================================


def test_default_sort_is_created_desc_unchanged(pg, client) -> None:
    """缺省 sort 保持既有 created_at DESC 行为（回归护栏）。"""
    _seed_upload(pg, "ux03-old", EMP1, created_at="2026-01-01T00:00:00Z")
    _seed_upload(pg, "ux03-mid", EMP1, created_at="2026-02-01T00:00:00Z")
    _seed_upload(pg, "ux03-new", EMP1, created_at="2026-03-01T00:00:00Z")
    ids = _ids(client)
    assert ids == ["asset:ux03-new", "asset:ux03-mid", "asset:ux03-old"]
    # 显式 created_desc 与缺省一致
    assert _ids(client, sort="created_desc") == ids


def test_sort_created_asc_reverses(pg, client) -> None:
    _seed_upload(pg, "ux03-old", EMP1, created_at="2026-01-01T00:00:00Z")
    _seed_upload(pg, "ux03-new", EMP1, created_at="2026-03-01T00:00:00Z")
    assert _ids(client, sort="created_asc") == ["asset:ux03-old", "asset:ux03-new"]


def test_sort_title_asc_uses_effective_title(pg, client) -> None:
    """title_asc 按有效标题（项目素材 base_title=project.name）升序。"""
    _seed_project_asset(
        pg, "ux03-pb", project_id="ux03-proj-b", owner=EMP1, project_name="Beta 别墅"
    )
    _seed_project_asset(
        pg, "ux03-pa", project_id="ux03-proj-a", owner=EMP1, project_name="Alpha 别墅"
    )
    titles = [item["title"] for item in _items(client, sort="title_asc")]
    assert titles == sorted(titles)
    assert titles[0].startswith("Alpha")


def test_sort_size_desc_nulls_last(pg, client) -> None:
    """size_desc 大→小；size_bytes IS NULL 的 direct 成片稳定排在最后。"""
    _seed_upload(pg, "ux03-small", EMP1, size_bytes=1000)
    _seed_upload(pg, "ux03-big", EMP1, size_bytes=9999)
    _seed_direct_generation(
        pg,
        task_id="ux03-task-null",
        batch_id="ux03-batch-null",
        project_id="ux03-proj-null",
        owner=EMP1,
    )
    ids = _ids(client, sort="size_desc")
    assert ids[0] == "asset:ux03-big"
    assert ids[1] == "asset:ux03-small"
    assert ids[-1] == "generation:ux03-task-null"  # NULL size 落最后


def test_sort_invalid_value_rejected(pg, client) -> None:
    response = client.get(MATERIALS_PATH, params={"sort": "bogus"})
    assert response.status_code == 422


def test_sort_with_pagination_is_consistent(pg, client) -> None:
    """排序 + 分页：两页不重叠、并集等于全集、整体有序。"""
    for i in range(5):
        _seed_upload(pg, f"ux03-page-{i}", EMP1, created_at=f"2026-0{i + 1}-01T00:00:00Z")
    page1 = _ids(client, sort="created_asc", page=1, page_size=2)
    page2 = _ids(client, sort="created_asc", page=2, page_size=2)
    assert len(page1) == 2 and len(page2) == 2
    assert set(page1).isdisjoint(page2)
    full = _ids(client, sort="created_asc", page=1, page_size=24)
    assert page1 + page2 == full[:4]


# ===========================================================================
# B 组 — person_id / project_id 对象筛选
# ===========================================================================


def test_filter_by_project_id(pg, client) -> None:
    _seed_project_asset(pg, "ux03-in", project_id="ux03-proj-in", owner=EMP1)
    _seed_project_asset(pg, "ux03-out", project_id="ux03-proj-out", owner=EMP1)
    ids = _ids(client, project_id="ux03-proj-in")
    assert ids == ["asset:ux03-in"]


def test_filter_by_person_id_character(pg, client) -> None:
    identity_id, sheet_id, _view = _seed_published_sheet(
        pg, "zhang", owner=EMP1, display_name="张工"
    )
    _seed_upload(pg, "ux03-plain", EMP1)  # 无人物归属
    ids = _ids(client, person_id=identity_id)
    assert f"asset:{sheet_id}" in ids
    assert "asset:ux03-plain" not in ids


def test_person_and_project_filter_combine(pg, client) -> None:
    _seed_project_asset(pg, "ux03-pa", project_id="ux03-proj-a", owner=EMP1)
    identity_id, sheet_id, _view = _seed_published_sheet(pg, "li", owner=EMP1, display_name="李工")
    # project 筛选不含人物素材；person 筛选不含项目素材
    assert _ids(client, project_id="ux03-proj-a") == ["asset:ux03-pa"]
    assert f"asset:{sheet_id}" in _ids(client, person_id=identity_id)
    assert "asset:ux03-pa" not in _ids(client, person_id=identity_id)


def test_cross_user_object_filter_returns_empty(pg, client) -> None:
    """越权对象 ID（他人项目/人物）返回空而非他人数据（scope 与筛选叠加）。"""
    _seed_project_asset(pg, "ux03-emp2-proj-asset", project_id="ux03-emp2-proj", owner=EMP2)
    identity_id, _sheet, _view = _seed_published_sheet(
        pg, "emp2-person", owner=EMP2, display_name="王工"
    )
    emp1_client = _build_client(pg, user_id=EMP1)
    assert _ids(emp1_client, project_id="ux03-emp2-proj") == []
    assert _ids(emp1_client, person_id=identity_id) == []


# ===========================================================================
# C 组 — person_name / project_title 派生
# ===========================================================================


def test_project_title_derived_for_project_asset(pg, client) -> None:
    _seed_project_asset(
        pg, "ux03-pa", project_id="ux03-proj-a", owner=EMP1, project_name="云顶别墅"
    )
    item = next(i for i in _items(client) if i["id"] == "asset:ux03-pa")
    assert item["project_title"] == "云顶别墅"
    assert item["person_name"] is None


def test_person_name_derived_for_character(pg, client) -> None:
    identity_id, sheet_id, _view = _seed_published_sheet(
        pg, "zhang", owner=EMP1, display_name="张工"
    )
    item = next(i for i in _items(client) if i["id"] == f"asset:{sheet_id}")
    assert item["person_name"] == "张工"
    assert item["person_id"] == identity_id
    assert item["project_title"] is None


def test_person_name_derived_for_oral(pg, client) -> None:
    identity_id = _seed_identity(pg, "oral-person", owner=EMP1, display_name="赵主播")
    _seed_oral(
        pg,
        task_id="ux03-oral-1",
        asset_id="ux03-oral-asset-1",
        owner=EMP1,
        identity_id=identity_id,
        title="口播·云顶",
    )
    item = next(i for i in _items(client) if i["id"] == "asset:ux03-oral-asset-1")
    assert item["person_name"] == "赵主播"
    assert item["person_id"] == identity_id


def test_upload_has_no_object_names(pg, client) -> None:
    """「我的上传」分支既无 project_title 也无 person_name（均为 None，前端需容错）。"""
    _seed_upload(pg, "ux03-plain", EMP1)
    item = next(i for i in _items(client) if i["id"] == "asset:ux03-plain")
    assert item["person_name"] is None
    assert item["project_title"] is None


def test_project_title_derived_for_direct_generation(pg, client) -> None:
    _seed_direct_generation(
        pg,
        task_id="ux03-task-1",
        batch_id="ux03-batch-1",
        project_id="ux03-proj-1",
        owner=EMP1,
        project_name="滨江别墅",
    )
    item = next(i for i in _items(client) if i["id"] == "generation:ux03-task-1")
    assert item["project_title"] == "滨江别墅"
    assert item["person_name"] is None
