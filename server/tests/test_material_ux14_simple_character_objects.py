"""MATERIAL-UX-14 — 一键人物对象单份化（approved 复用 generated 对象）专项测试.

验收（任务卡 §4）：
- 新建一键人物的存储对象数 12 → 7：1 源图 + 1 五视图合成图 + 5 generated，
  approved 不再为同一份 content 写第二个对象。
- approved assets 行保持独立主键（引用结构不变），但其 storage_uri / sha256 /
  size_bytes / content_type 与同一视图的 generated 行完全一致。
- 人物库展示、授权、引用（作参考/首帧）链路读取 approved 行仍能取回同一份
  字节，发布选择语义不变。
- 两条路径都覆盖：prepared（生产 worker 主路径，对象先写后入账）与
  non-prepared（同步兜底路径，函数内直写）；失败清理仍按 object_keys 生效。

PG 约束：共享库 ``customer_v3_test``（TEST_POSTGRESQL_URL，经
``pg_test_kit.resolve_test_dsn`` 读取）；模块级持 ``shared_suite_lock``；
全部数据用 ``ux14-`` 前缀；绝不 TRUNCATE 共享业务表，收尾只 DELETE ux14- 行。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import (
    database_name_of,
    require_pg_or_explicit_skip,
    resolve_test_dsn,
    shared_suite_lock,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES
from app.character_image_generation import deterministic_png
from app.db_pg import close_pg_pool
from app.db_portable import BusinessConnection
from app.media import storage_key_from_uri
from app.simple_character import (
    SimpleCharacterCreationResult,
    cleanup_deleted_character_objects,
    create_simple_character,
    delete_simple_character_identity,
    list_simple_library_page,
    prepare_simple_character_generation,
    regenerate_simple_character_contact_sheet,
    store_simple_character_publication,
)
from app.storage import FakeStorageAdapter, StorageBackendUnavailable, StoredObject

DB_NAME_SHARED = "customer_v3_test"
UX14 = "ux14-"
LIKE_UX14 = UX14 + "%"
BUCKET = "ux14-bucket"
DISPLAY_NAME = "UX14 单份化人物"
PERSONA_NAME = "UX14 单份化人设"


def _new_user(tag: str) -> str:
    return f"{UX14}{tag}-{uuid4().hex[:10]}"


def _actor(user_id: str) -> CurrentUser:
    return CurrentUser(
        id=user_id,
        username=user_id,
        display_name="UX14 Owner",
        role="employee",  # type: ignore[arg-type]
    )


def _seed_user(pg: psycopg.Connection, user_id: str) -> None:
    pg.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'employee')",
        (user_id, user_id, "UX14 Owner"),
    )


def _clean_ux14(dsn: str) -> None:
    """删除全部 ux14- 行（先子表后父表满足外键），绝不 TRUNCATE 共享表."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            DELETE FROM character_asset_reviews
            WHERE character_asset_id IN (
                SELECT view.id FROM character_assets AS view
                WHERE view.character_version_id IN (
                    SELECT version.id FROM character_versions AS version
                    WHERE version.persona_id IN (
                        SELECT persona.id FROM character_personas AS persona
                        WHERE persona.identity_id IN (
                            SELECT identity.id FROM person_identities AS identity
                            WHERE identity.owner_user_id LIKE %s
                        )
                    )
                )
            )
            """,
            (LIKE_UX14,),
        )
        conn.execute(
            """
            DELETE FROM character_assets
            WHERE character_version_id IN (
                SELECT version.id FROM character_versions AS version
                WHERE version.persona_id IN (
                    SELECT persona.id FROM character_personas AS persona
                    WHERE persona.identity_id IN (
                        SELECT identity.id FROM person_identities AS identity
                        WHERE identity.owner_user_id LIKE %s
                    )
                )
            )
            """,
            (LIKE_UX14,),
        )
        conn.execute(
            """
            DELETE FROM character_versions
            WHERE persona_id IN (
                SELECT persona.id FROM character_personas AS persona
                WHERE persona.identity_id IN (
                    SELECT identity.id FROM person_identities AS identity
                    WHERE identity.owner_user_id LIKE %s
                )
            )
            """,
            (LIKE_UX14,),
        )
        conn.execute(
            """
            DELETE FROM character_personas
            WHERE identity_id IN (
                SELECT identity.id FROM person_identities AS identity
                WHERE identity.owner_user_id LIKE %s
            )
            """,
            (LIKE_UX14,),
        )
        conn.execute("DELETE FROM person_identities WHERE owner_user_id LIKE %s", (LIKE_UX14,))
        conn.execute("DELETE FROM assets WHERE created_by_user_id LIKE %s", (LIKE_UX14,))
        conn.execute("DELETE FROM content_objects WHERE scope_owner LIKE %s", (LIKE_UX14,))
        conn.execute("DELETE FROM audit_logs WHERE actor_user_id LIKE %s", (LIKE_UX14,))
        conn.execute("DELETE FROM users WHERE id LIKE %s", (LIKE_UX14,))


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    """共享库 DSN + CW-007 硬门 + 迁移到 head + 模块级套件锁."""
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert database_name_of(dsn) == DB_NAME_SHARED, "本套件只准用共享库 customer_v3_test"
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        _clean_ux14(dsn)
        try:
            yield dsn
        finally:
            _clean_ux14(dsn)
            close_pg_pool()


@pytest.fixture()
def pg(pg_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()
        close_pg_pool()


@pytest.fixture()
def bus(pg: psycopg.Connection) -> BusinessConnection:
    """autocommit 业务门面：与生产 autocommit 池连接同形."""
    return BusinessConnection.postgres(pg)


class _FailingPutStorage(FakeStorageAdapter):
    """第 fail_at 次 put_object 抛 StorageBackendUnavailable（模拟存储抖动）."""

    def __init__(self, *, fail_at: int) -> None:
        super().__init__(provider="fake", bucket=BUCKET)
        self._fail_at = fail_at
        self.put_calls = 0

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        self.put_calls += 1
        if self.put_calls == self._fail_at:
            raise StorageBackendUnavailable("injected put failure")
        return super().put_object(key, content, content_type=content_type)


def _storage() -> FakeStorageAdapter:
    return FakeStorageAdapter(provider="fake", bucket=BUCKET)


def _assert_single_object_budget(storage: FakeStorageAdapter) -> None:
    """恰好 7 个对象：1 源图 + 1 合成图 + 5 generated；不得存在 approved 对象."""
    keys = set(storage._objects)
    assert len(keys) == 7
    assert len([key for key in keys if "/source/" in key]) == 1
    assert len([key for key in keys if "/contact-sheets/" in key]) == 1
    assert len([key for key in keys if "/generated/" in key]) == 5
    assert not [key for key in keys if "/approved/" in key]


def _assert_approved_rows_reuse_generated(conn: psycopg.Connection, version_id: str) -> None:
    """approved 行与 generated 行的存储三要素（uri/sha256/size/content_type）一致."""
    rows = conn.execute(
        """
        SELECT approved.kind AS approved_kind,
               approved.storage_uri AS approved_uri,
               approved.sha256 AS approved_sha256,
               approved.size_bytes AS approved_size,
               approved.content_type AS approved_content_type,
               generated.storage_uri AS generated_uri,
               generated.sha256 AS generated_sha256,
               generated.size_bytes AS generated_size,
               generated.content_type AS generated_content_type
        FROM assets AS approved
        JOIN assets AS generated
          ON generated.id = (approved.metadata_json::jsonb ->> 'generated_asset_id')
        WHERE approved.kind = 'character_approved_image'
          AND approved.metadata_json::jsonb ->> 'character_version_id' = %s
        """,
        (version_id,),
    ).fetchall()
    assert len(rows) == len(REQUIRED_CHARACTER_VIEW_TYPES)
    for row in rows:
        assert row["approved_kind"] == "character_approved_image"
        assert row["approved_uri"] == row["generated_uri"]
        assert row["approved_sha256"] == row["generated_sha256"]
        assert row["approved_size"] == row["generated_size"]
        assert row["approved_content_type"] == row["generated_content_type"]


def _assert_library_and_reference_chain_intact(
    conn: psycopg.Connection,
    bus: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: FakeStorageAdapter,
    result: SimpleCharacterCreationResult,
) -> None:
    """人物库展示 / 发布选择 / 引用下载链路读取 approved 行不变量."""
    approved_ids = [view.asset_id for view in result.views]
    assert len(approved_ids) == len(REQUIRED_CHARACTER_VIEW_TYPES)
    assert {view.view_type for view in result.views} == set(REQUIRED_CHARACTER_VIEW_TYPES)

    # 发布选择：character_assets 指向 approved 行且 is_published_selection=1。
    selections = conn.execute(
        "SELECT asset_id FROM character_assets "
        "WHERE character_version_id = %s AND is_published_selection = 1",
        (result.character_version_id,),
    ).fetchall()
    assert {str(row["asset_id"]) for row in selections} == set(approved_ids)

    # 引用（作参考/首帧）下载链路：approved 行的 storage_uri 可取回哈希一致的字节。
    asset_rows = conn.execute(
        "SELECT id, storage_uri, sha256, content_type FROM assets WHERE id = ANY(%s)",
        (approved_ids,),
    ).fetchall()
    assert len(asset_rows) == len(approved_ids)
    for row in asset_rows:
        content = storage.get_object(storage_key_from_uri(str(row["storage_uri"])))
        assert hashlib.sha256(content).hexdigest() == str(row["sha256"])

    # 人物库展示：最新已发布版本的 approved 选择即预览视图。
    page = list_simple_library_page(bus, actor=actor, limit=10, cursor=None, query="")
    assert page.total == 1
    entry = page.items[0]
    assert {view.view_type for view in entry.views} == set(REQUIRED_CHARACTER_VIEW_TYPES)
    assert {view.asset_id for view in entry.views} == set(approved_ids)

    # 授权链路：身份记录仍指向源图资产（一键流程 self-authorization 语义不变）。
    identity = conn.execute(
        "SELECT authorization_status, source_asset_id FROM person_identities WHERE id = %s",
        (result.identity_id,),
    ).fetchone()
    assert identity is not None
    assert str(identity["authorization_status"]) == "AUTHORIZED"
    assert identity["source_asset_id"] is not None


def _create_source_png(tag: bytes) -> bytes:
    return deterministic_png(tag, width=1024, height=1536)


# ---------------------------------------------------------------------------
# prepared 路径（生产 worker 主路径）：store_simple_character_publication
# ---------------------------------------------------------------------------


def test_prepared_path_writes_seven_objects_and_approved_reuses_generated(
    pg: psycopg.Connection, bus: BusinessConnection
) -> None:
    user_id = _new_user("prep")
    _seed_user(pg, user_id)
    actor = _actor(user_id)
    storage = _storage()
    source = _create_source_png(b"ux14-prep-source")

    generation = prepare_simple_character_generation(
        source_content=source,
        source_content_type="image/png",
        display_name=DISPLAY_NAME,
        image_provider=None,
    )
    publication = store_simple_character_publication(
        actor=actor,
        storage=storage,
        source_content=source,
        source_content_type="image/png",
        display_name=DISPLAY_NAME,
        generation=generation,
    )
    # 对象键恰好 7 个且唯一；approved 不再有自己的存储对象。
    assert len(publication.object_keys) == 7
    assert len(set(publication.object_keys)) == 7
    _assert_single_object_budget(storage)

    result = create_simple_character(
        bus,
        actor=actor,
        project_id=None,
        storage=storage,
        source_content=source,
        source_content_type="image/png",
        display_name=DISPLAY_NAME,
        persona_name=PERSONA_NAME,
        prepared_publication=publication,
    )
    # 入账阶段不再追加任何对象。
    _assert_single_object_budget(storage)
    _assert_approved_rows_reuse_generated(pg, result.character_version_id)
    _assert_library_and_reference_chain_intact(pg, bus, actor=actor, storage=storage, result=result)
    # 审计口径如实记录对象预算。
    audit = pg.execute(
        "SELECT metadata_json FROM audit_logs WHERE actor_user_id = %s "
        "AND action = 'simple_character.create'",
        (user_id,),
    ).fetchone()
    assert audit is not None


# ---------------------------------------------------------------------------
# non-prepared 路径（同步兜底）：create_simple_character 函数内直写
# ---------------------------------------------------------------------------


def test_non_prepared_path_writes_seven_objects(
    pg: psycopg.Connection, bus: BusinessConnection
) -> None:
    user_id = _new_user("direct")
    _seed_user(pg, user_id)
    actor = _actor(user_id)
    storage = _storage()
    source = _create_source_png(b"ux14-direct-source")

    result = create_simple_character(
        bus,
        actor=actor,
        project_id=None,
        storage=storage,
        source_content=source,
        source_content_type="image/png",
        display_name=DISPLAY_NAME,
        persona_name=PERSONA_NAME,
        image_provider=None,
    )
    _assert_single_object_budget(storage)
    _assert_approved_rows_reuse_generated(pg, result.character_version_id)
    _assert_library_and_reference_chain_intact(pg, bus, actor=actor, storage=storage, result=result)


def test_regenerate_path_also_writes_single_copy_per_view(
    pg: psycopg.Connection, bus: BusinessConnection
) -> None:
    """non-prepared 的另一消费方（重新生成五视图）同样单份化：+6 而非 +11."""
    user_id = _new_user("regen")
    _seed_user(pg, user_id)
    actor = _actor(user_id)
    storage = _storage()
    source = _create_source_png(b"ux14-regen-source")

    created = create_simple_character(
        bus,
        actor=actor,
        project_id=None,
        storage=storage,
        source_content=source,
        source_content_type="image/png",
        display_name=DISPLAY_NAME,
        persona_name=PERSONA_NAME,
        image_provider=None,
    )
    _assert_single_object_budget(storage)

    regenerated = regenerate_simple_character_contact_sheet(
        bus,
        actor=actor,
        identity_id=created.identity_id,
        storage=storage,
        source_content_override=source,
        image_provider=None,
    )
    # 新版本新增 1 合成图 + 5 generated；approved 不写第二个对象。
    keys = set(storage._objects)
    assert len(keys) == 13
    assert not [key for key in keys if "/approved/" in key]
    _assert_approved_rows_reuse_generated(pg, regenerated.character_version_id)
    _assert_library_and_reference_chain_intact(
        pg, bus, actor=actor, storage=storage, result=regenerated
    )


# ---------------------------------------------------------------------------
# 失败清理语义：仍是 object_keys → cleanup_publication_objects
# ---------------------------------------------------------------------------


def test_publication_failure_cleans_every_written_object(
    pg: psycopg.Connection, bus: BusinessConnection
) -> None:
    user_id = _new_user("cleanup")
    _seed_user(pg, user_id)
    actor = _actor(user_id)
    storage = _FailingPutStorage(fail_at=6)
    source = _create_source_png(b"ux14-cleanup-source")
    generation = prepare_simple_character_generation(
        source_content=source,
        source_content_type="image/png",
        display_name=DISPLAY_NAME,
        image_provider=None,
    )

    with pytest.raises(HTTPException) as excinfo:
        store_simple_character_publication(
            actor=actor,
            storage=storage,
            source_content=source,
            source_content_type="image/png",
            display_name=DISPLAY_NAME,
            generation=generation,
        )
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["code"] == "SIMPLE_CHARACTER_STORAGE_UNAVAILABLE"
    # 第 6 次 put 失败前已写入 5 个对象，全部被清理；数据库无任何残留。
    assert storage.put_calls == 6
    assert storage._objects == {}
    leftovers = pg.execute(
        "SELECT count(*) FROM assets WHERE created_by_user_id = %s", (user_id,)
    ).fetchone()
    assert leftovers is not None and leftovers[0] == 0


# ---------------------------------------------------------------------------
# 删除清理去重：approved 复用 generated 的 key 时按唯一对象计数
# ---------------------------------------------------------------------------


def test_delete_cleanup_dedupes_objects_shared_by_approved_and_generated_rows(
    pg: psycopg.Connection, bus: BusinessConnection
) -> None:
    """删除清理按对象身份去重：12 个资产行 → 7 个唯一对象。

    approved 行复用 generated 行的 key（MATERIAL-UX-14），按资产行逐个清理会把
    planned/deleted 计数虚高到 12；去重后与真实 COS 对象数一致，审计同步唯一口径。
    """
    user_id = _new_user("dedupe")
    _seed_user(pg, user_id)
    actor = _actor(user_id)
    storage = _storage()
    source = _create_source_png(b"ux14-dedupe-source")

    created = create_simple_character(
        bus,
        actor=actor,
        project_id=None,
        storage=storage,
        source_content=source,
        source_content_type="image/png",
        display_name=DISPLAY_NAME,
        persona_name=PERSONA_NAME,
        image_provider=None,
    )
    _assert_single_object_budget(storage)

    # 12 个资产行：1 源图 + 1 合成图 + 5 approved + 5 generated（metadata 引用）。
    asset_rows = pg.execute(
        "SELECT count(*) FROM assets WHERE created_by_user_id = %s", (user_id,)
    ).fetchone()
    assert asset_rows is not None and asset_rows[0] == 12

    plan = delete_simple_character_identity(
        bus,
        actor=actor,
        identity_id=created.identity_id,
        storage_for_uri=lambda _conn, _uri: storage,
    )
    identities = [(t.storage.provider, t.storage.bucket, t.key) for t in plan.targets]
    assert len(plan.targets) == 7
    assert len(set(identities)) == 7

    result = cleanup_deleted_character_objects(plan)
    assert result.deleted_count == 7
    assert result.failed_count == 0
    assert storage._objects == {}

    # 审计同步记录唯一对象数（planned），而不是资产行数。
    audit = pg.execute(
        "SELECT metadata_json FROM audit_logs WHERE actor_user_id = %s "
        "AND action = 'simple_character.delete'",
        (user_id,),
    ).fetchone()
    assert audit is not None
    metadata = json.loads(str(audit["metadata_json"]))
    assert metadata["deleted_asset_count"] == 12
    assert metadata["storage_cleanup_planned_count"] == 7
    assert metadata["shared_storage_object_count"] == 0
