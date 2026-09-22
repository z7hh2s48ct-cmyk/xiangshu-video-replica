"""MATERIAL-UX-01-20260922 — 素材分组导航与批量维护（后端契约）PG 矩阵.

任务卡 docs/素材库改进工作任务清单-2026-09-22.md §1「分组与批量」：分组从
「逐条自由文本」升级为「可导航、可批量维护的轻实体」，列表支持多选批量操作。
本模块钉住后端契约：

- ``GET /api/studio/materials/groups``：对当前用户可见（scope）且未隐藏的候选
  素材按 ``COALESCE(NULLIF(group_override,''), base_group)`` 聚合计数，排序
  ``count DESC, name ASC``；contact sheet 同一张只计一条（派生五视图不重复出现）；
  他人素材不计入。
- ``GET /api/studio/materials?group=``：``None`` 不过滤；空串为「未分组」语义
  （当前数据模型 base_group 恒非空 → 空集返回，属预期语义保留）；非空 strip
  后精确匹配（支持中文组名）；page/total 同条件（分页一致性）。
- ``PATCH /api/studio/materials/bulk``：``{material_ids[≤100], update:{group?,hidden?}}``
  在单一写事务内逐条 owner 校验；越权/不存在/非法 ID 逐条 skipped；direct 成片
  （generation 来源）的 group 变更逐条 skipped（单条 409 的批量降级）；显式
  ``group: null`` 清除 override 回 base_group；``hidden`` 走 preserve_overrides
  语义（绝不覆盖 title/group）；一条汇总审计 ``studio.material.bulk_update``。
- 既有通道不回退：上传时选分组（upload-intent 的 group 字段）照常；单条 PATCH
  的 group 空串仍为 422（MATERIAL_GROUP_EMPTY）。

PG 约束（沿 CW-031 / CW-078）：不建新库，只用共享库 ``customer_v3_test``；模块级
持 ``shared_suite_lock``；全部数据用唯一 ``ux01-`` 前缀；**绝不 TRUNCATE 共享业务
表**，收尾只 ``DELETE`` ux01- 行；全部 PG 用例挂 CW-007 硬门（fixture 不可达即
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
UX01 = "ux01-"
EMP1 = f"{UX01}emp1"
EMP2 = f"{UX01}emp2"
AUDITOR = f"{UX01}auditor"
UX01_LIKE = f"{UX01}%"
MATERIALS_PATH = "/api/studio/materials"
GROUPS_PATH = f"{MATERIALS_PATH}/groups"
BULK_PATH = f"{MATERIALS_PATH}/bulk"


def _actor(user_id: str, role: str = "employee") -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 共享库 fixture + ux01- 前缀种子/清理（绝不新建 PG 库，绝不 TRUNCATE）
# ---------------------------------------------------------------------------


def _clean_ux01_rows(conn: psycopg.Connection) -> None:
    """删除全部 ux01- 作用域行（子表先于父表）；只 DELETE、绝不 TRUNCATE 共享表。"""
    conn.execute(
        "DELETE FROM audit_logs WHERE actor_user_id LIKE %s OR entity_id LIKE %s",
        (UX01_LIKE, UX01_LIKE),
    )
    conn.execute(
        "DELETE FROM studio_material_preferences WHERE user_id LIKE %s OR source_id LIKE %s",
        (UX01_LIKE, UX01_LIKE),
    )
    conn.execute(
        "DELETE FROM generation_tasks WHERE id LIKE %s OR batch_id LIKE %s", (UX01_LIKE, UX01_LIKE)
    )
    conn.execute("DELETE FROM generation_batches WHERE id LIKE %s", (UX01_LIKE,))
    conn.execute(
        "DELETE FROM character_assets WHERE id LIKE %s OR character_version_id LIKE %s",
        (UX01_LIKE, UX01_LIKE),
    )
    conn.execute("DELETE FROM character_versions WHERE id LIKE %s", (UX01_LIKE,))
    conn.execute("DELETE FROM character_personas WHERE id LIKE %s", (UX01_LIKE,))
    conn.execute("DELETE FROM oral_tasks WHERE id LIKE %s", (UX01_LIKE,))
    conn.execute(
        "DELETE FROM person_identities WHERE id LIKE %s OR owner_user_id LIKE %s",
        (UX01_LIKE, UX01_LIKE),
    )
    conn.execute(
        "DELETE FROM assets WHERE id LIKE %s OR created_by_user_id LIKE %s", (UX01_LIKE, UX01_LIKE)
    )
    conn.execute("DELETE FROM projects WHERE id LIKE %s", (UX01_LIKE,))
    conn.execute("DELETE FROM users WHERE id LIKE %s", (UX01_LIKE,))


def _clean_ux01(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        _clean_ux01_rows(conn)


def _seed_users(conn: psycopg.Connection) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                (EMP1, EMP1, "UX01 Employee One", "employee"),
                (EMP2, EMP2, "UX01 Employee Two", "employee"),
                (AUDITOR, AUDITOR, "UX01 Auditor", "auditor"),
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
        _clean_ux01(dsn)
        try:
            yield dsn
        finally:
            _clean_ux01(dsn)
            close_pg_pool()


@pytest.fixture()
def pg(pg_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    _clean_ux01_rows(conn)
    _seed_users(conn)
    try:
        yield conn
    finally:
        _clean_ux01_rows(conn)
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
# 断言辅助与素材种子
# ---------------------------------------------------------------------------


def _list_page(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get(MATERIALS_PATH, params=params)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _material_ids(client: TestClient, **params: Any) -> set[str]:
    return {str(item["id"]) for item in _list_page(client, **params)["items"]}


def _groups(client: TestClient) -> dict[str, int]:
    response = client.get(GROUPS_PATH)
    assert response.status_code == 200, response.text
    return {str(item["name"]): int(item["count"]) for item in response.json()["items"]}


def _seed_upload(
    conn: psycopg.Connection,
    asset_id: str,
    owner: str,
    *,
    content_type: str = "image/png",
    kind: str = "material_image",
    suffix: str = ".png",
) -> None:
    """「我的上传」候选：project_id NULL + material_* kind + 非空 sha256（ready）。"""
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json) "
        "VALUES (%s, NULL, %s, %s, %s, 2048, %s, %s, '{}')",
        (
            asset_id,
            kind,
            f"local://ux01/{asset_id}{suffix}",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            content_type,
            owner,
        ),
    )


def _seed_project_asset(
    conn: psycopg.Connection, asset_id: str, *, project_id: str, owner: str
) -> None:
    """「项目素材」候选：asset 挂在项目上且未被 generation_tasks 引用。"""
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (project_id, owner, f"UX01 {project_id}"),
    )
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json) "
        "VALUES (%s, %s, 'project_image', %s, %s, 4096, 'image/png', %s, '{}')",
        (
            asset_id,
            project_id,
            f"local://ux01/{asset_id}.png",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            owner,
        ),
    )


def _seed_direct_generation(
    conn: psycopg.Connection, *, task_id: str, batch_id: str, project_id: str, owner: str
) -> None:
    """直出成片（provider-hosted DIRECT、无本存档）：material id = ``generation:{task_id}``."""
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (project_id, owner, f"UX01 {project_id}"),
    )
    conn.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, idempotency_key, "
        "request_hash, request_snapshot_json, status) "
        "VALUES (%s, %s, %s, %s, 'ux01-hash', '{}', 'SUCCEEDED')",
        (batch_id, project_id, owner, f"{batch_id}-key"),
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, provider, model, status, archive_status, "
        "provider_result_url, completed_at) "
        "VALUES (%s, %s, 'metaso', 'MiniMax-H3', 'SUCCEEDED', 'DIRECT', %s, CURRENT_TIMESTAMP)",
        (task_id, batch_id, f"https://cdn.example.com/{task_id}.mp4"),
    )


def _seed_published_sheet(conn: psycopg.Connection, key: str, *, owner: str) -> tuple[str, str]:
    """播种 PUBLISHED 人物 contact sheet + 一条已发布五视图派生资产.

    返回 ``(contact_sheet_asset_id, derived_view_asset_id)``。列表/分组里同一张
    sheet 只应计一条：派生的五视图（人物素材分支）不得重复出现。
    """
    identity_id = f"{UX01}identity-{key}"
    persona_id = f"{UX01}persona-{key}"
    version_id = f"{UX01}version-{key}"
    sheet_asset_id = f"{UX01}sheet-{key}"
    view_asset_id = f"{UX01}view-{key}"
    for asset_id, asset_kind in (
        (sheet_asset_id, "character_contact_sheet"),
        (view_asset_id, "character_approved_image"),
    ):
        conn.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id, metadata_json) "
            "VALUES (%s, NULL, %s, %s, %s, 4096, 'image/png', %s, '{}')",
            (
                asset_id,
                asset_kind,
                f"local://ux01/{asset_id}.png",
                hashlib.sha256(asset_id.encode()).hexdigest(),
                owner,
            ),
        )
    conn.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, "
        "authorization_status, authorization_scope, source_quality_status, status, created_by) "
        "VALUES (%s, %s, %s, 'AUTHORIZED', '[]', 'PASSED', 'ACTIVE', %s)",
        (identity_id, owner, f"UX01 {key}", owner),
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
        (f"{UX01}character-asset-{key}", version_id, view_asset_id),
    )
    return sheet_asset_id, view_asset_id


# ===========================================================================
# A 组 — GET /groups 聚合（默认分组、override 迁移、hidden、scope、去重、排序）
# ===========================================================================


def test_groups_counts_default_buckets_scoped_to_the_user(pg, client) -> None:
    """默认分组计数按 base_group 聚合；他人素材与当前用户无关（scope 隔离）。"""
    _seed_upload(pg, "ux01-a1", EMP1)
    _seed_upload(pg, "ux01-a2", EMP1)
    _seed_upload(pg, "ux01-b1", EMP2)
    _seed_project_asset(pg, "ux01-p1", project_id="ux01-proj1", owner=EMP1)

    assert _groups(client) == {"我的上传": 2, "项目素材": 1}


def test_groups_override_moves_count_and_hidden_is_excluded(pg, client) -> None:
    """override 后计数随有效分组迁移；hidden 素材不计入任何分组。"""
    _seed_upload(pg, "ux01-a1", EMP1)
    _seed_upload(pg, "ux01-a2", EMP1)

    response = client.patch(f"{MATERIALS_PATH}/asset:ux01-a1", json={"group": "ux01-甲组"})
    assert response.status_code == 200, response.text
    assert _groups(client) == {"我的上传": 1, "ux01-甲组": 1}

    assert client.delete(f"{MATERIALS_PATH}/asset:ux01-a2").status_code == 204
    assert _groups(client) == {"ux01-甲组": 1}


def test_groups_orders_by_count_desc_then_name_asc(pg, client) -> None:
    """排序：count DESC 优先；同计数再按 name ASC（用 ASCII 组名锁定次要键）。"""
    for asset_id in ("ux01-a1", "ux01-a2", "ux01-a3", "ux01-a4", "ux01-a5"):
        _seed_upload(pg, asset_id, EMP1)
    _seed_project_asset(pg, "ux01-p1", project_id="ux01-proj1", owner=EMP1)
    for asset_id in ("ux01-a1", "ux01-a2"):
        assert (
            client.patch(
                f"{MATERIALS_PATH}/asset:{asset_id}", json={"group": "ux01-alpha"}
            ).status_code
            == 200
        )

    response = client.get(GROUPS_PATH)
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()["items"]] == [
        "我的上传",  # 3
        "ux01-alpha",  # 2
        "项目素材",  # 1
    ]


def test_groups_tie_is_broken_by_name_ascending(pg, client) -> None:
    _seed_upload(pg, "ux01-a1", EMP1)
    _seed_upload(pg, "ux01-a2", EMP1)
    assert (
        client.patch(f"{MATERIALS_PATH}/asset:ux01-a1", json={"group": "ux01-beta"}).status_code
        == 200
    )
    assert (
        client.patch(f"{MATERIALS_PATH}/asset:ux01-a2", json={"group": "ux01-alpha"}).status_code
        == 200
    )

    response = client.get(GROUPS_PATH)
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()["items"]] == ["ux01-alpha", "ux01-beta"]
    assert [item["count"] for item in response.json()["items"]] == [1, 1]


def test_groups_dedupe_contact_sheet_and_hide_its_derived_views(pg, client) -> None:
    """同一张 contact sheet 只计一条：派生的五视图（人物素材分支）不得重复出现。"""
    sheet_asset_id, view_asset_id = _seed_published_sheet(pg, "sheet1", owner=EMP1)

    assert _groups(client) == {"基础五视图": 1}
    listed = _material_ids(client)
    assert listed == {f"asset:{sheet_asset_id}"}
    assert f"asset:{view_asset_id}" not in listed


def test_groups_is_empty_for_a_scope_without_materials(pg) -> None:
    client = _build_client(pg, user_id=EMP2)
    assert _groups(client) == {}


# ===========================================================================
# B 组 — GET /materials?group= 精确筛选（None / 空串「未分组」/ 中文 / 分页）
# ===========================================================================


def test_group_filter_none_is_full_scan_and_nonempty_is_exact(pg, client) -> None:
    _seed_upload(pg, "ux01-a1", EMP1)
    _seed_upload(pg, "ux01-a2", EMP1)
    _seed_project_asset(pg, "ux01-p1", project_id="ux01-proj1", owner=EMP1)

    page = _list_page(client)
    assert page["total"] == 3
    assert len(page["items"]) == 3

    page = _list_page(client, group="我的上传")
    assert page["total"] == 2
    assert {item["id"] for item in page["items"]} == {"asset:ux01-a1", "asset:ux01-a2"}
    assert all(item["group"] == "我的上传" for item in page["items"])

    # 精确语义：前缀不匹配
    assert _list_page(client, group="我的")["total"] == 0

    # strip 后使用（与单条 PATCH 的 group 处理一致）
    assert _list_page(client, group="  我的上传  ")["total"] == 2


def test_group_filter_supports_chinese_and_unassigned_empty_semantics(pg, client) -> None:
    _seed_upload(pg, "ux01-a1", EMP1)
    assert (
        client.patch(
            f"{MATERIALS_PATH}/asset:ux01-a1", json={"group": "ux01-客户样板间"}
        ).status_code
        == 200
    )

    page = _list_page(client, group="ux01-客户样板间")
    assert page["total"] == 1
    assert page["items"][0]["group"] == "ux01-客户样板间"

    # 空串 = 「未分组」：当前数据模型 base_group 恒非空 → 空集返回（200，语义保留）
    empty = _list_page(client, group="")
    assert empty["items"] == []
    assert empty["total"] == 0
    blank = _list_page(client, group="   ")
    assert blank["items"] == []
    assert blank["total"] == 0


def test_group_filter_trims_legacy_whitespace_override(pg, client) -> None:
    """存量 override 带首尾空格时按 trim 后口径聚合与命中（幽灵分组回归）。"""
    _seed_upload(pg, "ux01-a1", EMP1)
    pg.execute(
        "INSERT INTO studio_material_preferences (user_id, source_type, source_id, "
        "group_override, hidden) VALUES (%s, 'asset', %s, %s, 0)",
        (EMP1, "ux01-a1", "  ux01-pad  "),
    )

    assert _groups(client) == {"ux01-pad": 1}
    assert _material_ids(client, group="ux01-pad") == {"asset:ux01-a1"}
    assert _list_page(client)["items"][0]["group"] == "ux01-pad"


def test_bulk_dedupes_repeated_ids_without_inflating_updated(pg, client) -> None:
    """重复 material_ids 去重后只计一次 updated，审计 requested 保持原值。"""
    _seed_upload(pg, "ux01-a1", EMP1)

    response = client.patch(
        f"{MATERIALS_PATH}/bulk",
        json={
            "material_ids": ["asset:ux01-a1", "asset:ux01-a1"],
            "update": {"group": "ux01-dedupe"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 1, "skipped": 0}
    assert _groups(client) == {"ux01-dedupe": 1}


def test_group_filter_keeps_page_and_total_consistent(pg, client) -> None:
    for asset_id in ("ux01-a1", "ux01-a2", "ux01-a3"):
        _seed_upload(pg, asset_id, EMP1)
    _seed_project_asset(pg, "ux01-p1", project_id="ux01-proj1", owner=EMP1)

    first = _list_page(client, group="我的上传", page=1, page_size=2)
    assert (first["page"], first["page_size"], first["total"], len(first["items"])) == (1, 2, 3, 2)
    second = _list_page(client, group="我的上传", page=2, page_size=2)
    assert (second["total"], len(second["items"])) == (3, 1)
    assert {item["id"] for item in first["items"]} | {item["id"] for item in second["items"]} == {
        "asset:ux01-a1",
        "asset:ux01-a2",
        "asset:ux01-a3",
    }


# ===========================================================================
# C 组 — PATCH /bulk 批量维护（分组、清除 override、隐藏 preserve、逐条跳过、审计）
# ===========================================================================


def test_bulk_sets_group_across_sources_and_writes_one_summary_audit(pg, client) -> None:
    """跨来源批量设置分组；一条汇总审计（不逐条写）。"""
    _seed_upload(pg, "ux01-a1", EMP1)
    _seed_project_asset(pg, "ux01-p1", project_id="ux01-proj1", owner=EMP1)

    response = client.patch(
        BULK_PATH,
        json={
            "material_ids": ["asset:ux01-a1", "asset:ux01-p1"],
            "update": {"group": "ux01-批量组"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 2, "skipped": 0}

    assert _groups(client) == {"ux01-批量组": 2}
    listed = _list_page(client, group="ux01-批量组")
    assert listed["total"] == 2
    assert all(item["group"] == "ux01-批量组" for item in listed["items"])

    audit = pg.execute(
        "SELECT metadata_json FROM audit_logs WHERE action = 'studio.material.bulk_update' "
        "AND actor_user_id = %s LIMIT 1",
        (EMP1,),
    ).fetchone()
    assert audit is not None
    assert json.loads(audit["metadata_json"]) == {
        "requested": 2,
        "updated": 2,
        "skipped": 0,
        "group_changed": True,
        "hidden_changed": False,
    }


def test_bulk_clears_group_override_back_to_base_group(pg, client) -> None:
    """显式 ``group: null`` 清除 override：有效分组回落到 base_group。"""
    _seed_upload(pg, "ux01-a1", EMP1)
    assert (
        client.patch(f"{MATERIALS_PATH}/asset:ux01-a1", json={"group": "ux01-临时组"}).status_code
        == 200
    )
    assert _groups(client) == {"ux01-临时组": 1}

    response = client.patch(
        BULK_PATH, json={"material_ids": ["asset:ux01-a1"], "update": {"group": None}}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 1, "skipped": 0}

    assert _groups(client) == {"我的上传": 1}
    page = _list_page(client)
    assert page["items"][0]["group"] == "我的上传"
    row = pg.execute(
        "SELECT group_override FROM studio_material_preferences "
        "WHERE user_id = %s AND source_id = %s",
        (EMP1, "ux01-a1"),
    ).fetchone()
    assert row is not None
    assert row["group_override"] is None


def test_bulk_hide_preserves_title_and_group_then_unhide_restores_visibility(pg, client) -> None:
    """批量隐藏走 preserve_overrides：绝不覆盖既有 title/group override。"""
    _seed_upload(pg, "ux01-a1", EMP1)
    material_id = "asset:ux01-a1"
    assert (
        client.patch(
            f"{MATERIALS_PATH}/{material_id}",
            json={"title": "自定义标题", "group": "ux01-保留组"},
        ).status_code
        == 200
    )

    hidden = client.patch(
        BULK_PATH, json={"material_ids": [material_id], "update": {"hidden": True}}
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {"updated": 1, "skipped": 0}

    row = pg.execute(
        "SELECT title_override, group_override, hidden FROM studio_material_preferences "
        "WHERE user_id = %s AND source_id = %s",
        (EMP1, "ux01-a1"),
    ).fetchone()
    assert row is not None
    assert row["title_override"] == "自定义标题"
    assert row["group_override"] == "ux01-保留组"
    assert int(row["hidden"]) == 1
    assert _material_ids(client) == set()
    assert _groups(client) == {}

    shown = client.patch(
        BULK_PATH, json={"material_ids": [material_id], "update": {"hidden": False}}
    )
    assert shown.status_code == 200, shown.text
    assert shown.json() == {"updated": 1, "skipped": 0}
    page = _list_page(client)
    assert page["items"][0]["title"] == "自定义标题"
    assert page["items"][0]["group"] == "ux01-保留组"
    assert page["items"][0]["hidden"] is False


def test_bulk_skips_foreign_missing_and_malformed_ids(pg, client) -> None:
    """逐条 owner 校验：越权/不存在/非法 ID 全部计入 skipped（不写他人偏好行）。"""
    _seed_upload(pg, "ux01-a1", EMP1)
    _seed_upload(pg, "ux01-b1", EMP2)

    response = client.patch(
        BULK_PATH,
        json={
            "material_ids": [
                "asset:ux01-a1",
                "asset:ux01-b1",  # 他人素材：越权
                "asset:ux01-missing",  # 不存在
                "not-a-material-id",  # 非法 ID
            ],
            "update": {"hidden": True},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 1, "skipped": 3}

    rows = pg.execute(
        "SELECT source_id, hidden FROM studio_material_preferences WHERE user_id = %s",
        (EMP1,),
    ).fetchall()
    assert [(row["source_id"], int(row["hidden"])) for row in rows] == [("ux01-a1", 1)]


def test_bulk_skips_direct_generation_group_but_allows_hide(pg, client) -> None:
    """direct 成片不支持分组（单条 409 的批量降级为逐条跳过）；hidden 不受限。"""
    _seed_direct_generation(
        pg, task_id="ux01-task1", batch_id="ux01-batch1", project_id="ux01-proj1", owner=EMP1
    )
    material_id = "generation:ux01-task1"

    grouped = client.patch(
        BULK_PATH, json={"material_ids": [material_id], "update": {"group": "ux01-批量组"}}
    )
    assert grouped.status_code == 200, grouped.text
    assert grouped.json() == {"updated": 0, "skipped": 1}

    page = _list_page(client)
    assert page["items"][0]["id"] == material_id
    assert page["items"][0]["group"] == "任务结果"

    hidden = client.patch(
        BULK_PATH, json={"material_ids": [material_id], "update": {"hidden": True}}
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {"updated": 1, "skipped": 0}


def test_bulk_requires_at_least_one_update_field(pg, client) -> None:
    _seed_upload(pg, "ux01-a1", EMP1)
    response = client.patch(BULK_PATH, json={"material_ids": ["asset:ux01-a1"], "update": {}})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "MATERIAL_UPDATE_EMPTY"


def test_bulk_rejects_blank_group(pg, client) -> None:
    _seed_upload(pg, "ux01-a1", EMP1)
    response = client.patch(
        BULK_PATH, json={"material_ids": ["asset:ux01-a1"], "update": {"group": "   "}}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "MATERIAL_GROUP_EMPTY"


def test_bulk_rejects_more_than_100_ids(pg, client) -> None:
    response = client.patch(
        BULK_PATH,
        json={
            "material_ids": [f"asset:ux01-{index}" for index in range(101)],
            "update": {"hidden": True},
        },
    )
    assert response.status_code == 422, response.text


def test_bulk_rejects_empty_ids(pg, client) -> None:
    response = client.patch(BULK_PATH, json={"material_ids": [], "update": {"hidden": True}})
    assert response.status_code == 422, response.text


def test_bulk_rejects_unknown_fields(pg, client) -> None:
    _seed_upload(pg, "ux01-a1", EMP1)
    unknown_update = client.patch(
        BULK_PATH, json={"material_ids": ["asset:ux01-a1"], "update": {"title": "x"}}
    )
    assert unknown_update.status_code == 422, unknown_update.text
    unknown_top = client.patch(
        BULK_PATH,
        json={"material_ids": ["asset:ux01-a1"], "update": {"hidden": True}, "extra": 1},
    )
    assert unknown_top.status_code == 422, unknown_top.text


def test_bulk_is_forbidden_for_auditors(pg) -> None:
    client = _build_client(pg, user_id=AUDITOR, role="auditor")
    response = client.patch(
        BULK_PATH, json={"material_ids": ["asset:ux01-a1"], "update": {"hidden": True}}
    )
    assert response.status_code == 403, response.text


def test_single_patch_keeps_blank_group_rejection(pg, client) -> None:
    """单条 PATCH 的既有语义不回退：group 空串 → 422 MATERIAL_GROUP_EMPTY。"""
    _seed_upload(pg, "ux01-a1", EMP1)
    response = client.patch(f"{MATERIALS_PATH}/asset:ux01-a1", json={"group": ""})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "MATERIAL_GROUP_EMPTY"


# ===========================================================================
# D 组 — 既有上传通道（upload-intent 的 group 字段）不回退
# ===========================================================================


def test_upload_intent_group_channel_still_assigns_group(pg, client) -> None:
    from app.materials import MaterialUploadIntentRequest, create_material_upload_intent
    from app.storage import FakeStorageAdapter

    bus = BusinessConnection.postgres(pg)
    storage = FakeStorageAdapter(provider="fake", bucket="ux01-tests")
    intent = create_material_upload_intent(
        bus,
        actor=_actor(EMP1),
        storage=storage,
        request=MaterialUploadIntentRequest(
            filename="ux01.png",
            content_type="image/png",
            size_bytes=1024,
            group="ux01-上传组",
        ),
    )
    assert intent.material_id.startswith("asset:")

    assert _groups(client) == {"ux01-上传组": 1}
    page = _list_page(client, group="ux01-上传组")
    assert page["total"] == 1
    assert page["items"][0]["id"] == intent.material_id


# ===========================================================================
# E 组 — 契约边界（OpenAPI 与请求模型；无 PG）
# ===========================================================================


def test_openapi_exposes_groups_bulk_and_group_filter_contracts() -> None:
    """前端按 OpenAPI 生成类型：新路径/schema 与 GET 的 group 参数必须在契约里。"""
    application = FastAPI()
    application.include_router(material_router)
    document = application.openapi()
    paths = document["paths"]

    groups_operation = paths[GROUPS_PATH]["get"]
    assert (
        groups_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/MaterialGroupsResponse"
    )
    bulk_operation = paths[BULK_PATH]["patch"]
    assert (
        bulk_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/MaterialBulkResult"
    )
    assert (
        bulk_operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/MaterialBulkRequest"
    )
    schemas = document["components"]["schemas"]
    assert "MaterialGroupItem" in schemas
    assert "MaterialBulkUpdate" in schemas

    group_param = next(
        param for param in paths[MATERIALS_PATH]["get"]["parameters"] if param["name"] == "group"
    )
    options = group_param["schema"].get("anyOf", [group_param["schema"]])
    assert 80 in [option.get("maxLength") for option in options]


def test_bulk_models_enforce_bounds_and_forbid_extra_fields() -> None:
    from pydantic import ValidationError

    from app.materials import MaterialBulkRequest, MaterialBulkUpdate

    with pytest.raises(ValidationError):
        MaterialBulkRequest(material_ids=[], update=MaterialBulkUpdate(hidden=True))
    with pytest.raises(ValidationError):
        MaterialBulkRequest(
            material_ids=[f"asset:{index}" for index in range(101)],
            update=MaterialBulkUpdate(hidden=True),
        )
    with pytest.raises(ValidationError):
        MaterialBulkRequest(material_ids=["asset:1"], update=MaterialBulkUpdate(group="x" * 81))
    with pytest.raises(ValidationError):
        MaterialBulkUpdate(title="nope")  # extra=forbid

    explicit_null = MaterialBulkRequest(
        material_ids=["asset:1"], update=MaterialBulkUpdate(group=None)
    )
    assert explicit_null.update.model_fields_set == {"group"}
    assert MaterialBulkUpdate().model_fields_set == set()
