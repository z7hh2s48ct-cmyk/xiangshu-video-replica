"""DEDUP-CAS-20260916 — 内容寻址登记表（content_objects）PG 矩阵.

本模块验证去重方案的两个安全支点，它们是整个方案得以共享字节的前提：

1. **属主是身份的一部分。** ``scope='user'`` 的登记行以属主为键，所以一个客户的
   上传**不可能**满足另一个客户的查找。这是最容易写错、也最致命的一处：少了属主
   维度，`(sha256, size)` 命中就变成跨用户信息泄露。唯一性用两条**部分唯一索引**
   表达（``uq_content_objects_user`` / ``uq_content_objects_global``），而不是一条
   UNIQUE 约束 —— PG 视唯一索引中的 NULL 互不相等，单条以 ``scope_owner`` 为键的
   约束会让 ``global`` 行彻底失去唯一性。

2. **删除由引用计数裁决，不由 ``storage_uri`` 字符串比较裁决。**
   ``project_id <> %s`` 在引用方 ``project_id IS NULL`` 时求值为 NULL 而非 TRUE，
   于是素材库/人物资料这类用户级资产会被判为「无共享引用」，其对象被删掉，而别的
   资产还在用它。``object_referenced_by_another_asset`` 用 ``id NOT IN`` 表达同一
   意图，没有这个 NULL 语义陷阱。

另外验证两阶段回收：置零只**排期**（``reclaim_after``），真正删字节的是
``reclaim_expired_content_objects``，它在锁内重读计数，因此宽限期内新建立的引用
不会被销毁；``pinned`` 行（爆款共享缓存这类由资产图之外持有的字节）永不被回收。

专属隔离库 ``dedup_cas_test`` 已登记 ``pg_test_kit.RECORDED_TEST_DATABASES``；
零 SQLite 替代、零缺库 skip（缺 PG 即硬失败），用例间 DELETE 复位隔离。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)
from psycopg.rows import dict_row

from app import content_store
from app.auth import CurrentUser
from app.character_identity import (
    complete_authorization_upload,
    create_identity_upload_intent,
    create_person_identity,
)
from app.db_pg import close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.materials import (
    MaterialUploadIntentRequest,
    ProbedMaterialUpload,
    create_material_upload_intent,
    persist_material_upload,
    prepare_material_upload,
)
from app.storage import FakeStorageAdapter

DEDUP_CAS_TEST_DB = "dedup_cas_test"

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64

_CLEANUP_ORDER = ("assets", "person_identities", "content_objects", "projects", "users")


@pytest.fixture(scope="module")
def cas_dsn() -> Iterator[str]:
    """专属内容寻址测试库：建库 → alembic head → 用完即删."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(DEDUP_CAS_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(DEDUP_CAS_TEST_DB)


@pytest.fixture()
def pg_state(cas_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """DATABASE_URL_ENV 指向专属库：``pg_transaction`` 走生产 PG 事务通道."""
    close_pg_pool()
    monkeypatch.setenv("VIDEO_REPLICA_DATABASE_URL", cas_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    _seed(cas_dsn)
    yield cas_dsn
    _truncate(cas_dsn)
    close_pg_pool()


def _exec(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(sql, params)


def _rows(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as pg:
        return [dict(row) for row in pg.execute(sql, params).fetchall()]


def _one(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(sql, params).fetchone()
        return None if row is None else row[0]


def _seed(dsn: str) -> None:
    _truncate(dsn)
    _exec(
        dsn,
        "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
        ("owner_a", "owner_a", "Owner A", "employee"),
    )
    _exec(
        dsn,
        "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
        ("owner_b", "owner_b", "Owner B", "employee"),
    )
    _exec(
        dsn,
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        ("project_a", "owner_a", "CAS project A"),
    )
    _exec(
        dsn,
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        ("project_b", "owner_b", "CAS project B"),
    )


def _truncate(dsn: str) -> None:
    for table in _CLEANUP_ORDER:
        _exec(dsn, f"DELETE FROM {table}")


def _with_conn(dsn: str, fn: Any) -> Any:
    """在一个生产事务通道里跑 fn(conn)。"""
    with pg_transaction() as raw:
        return fn(BusinessConnection.postgres(raw))


def _seed_asset(
    conn: BusinessConnection,
    *,
    asset_id: str,
    project_id: str | None,
    storage_uri: str,
    sha256: str,
    size_bytes: int,
    content_object_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json, content_object_id
        ) VALUES (%s, %s, 'reference_video', %s, %s, %s, 'video/mp4', NULL, '{}', %s)
        """,
        (asset_id, project_id, storage_uri, sha256, size_bytes, content_object_id),
    )


def _retain(
    conn: BusinessConnection,
    *,
    sha256: str = _DIGEST_A,
    size: int = 1024,
    scope: str = "user",
    owner: str | None = "owner_a",
    key: str | None = None,
    pinned: bool = False,
) -> tuple[content_store.ContentObject, bool]:
    return content_store.retain_content_object(
        conn,
        sha256=sha256,
        size_bytes=size,
        content_type="video/mp4",
        provider="fake",
        bucket="cas",
        object_key=key or content_store.content_object_key(sha256, ".mp4"),
        scope=scope,  # type: ignore[arg-type]
        owner_user_id=owner,
        pinned=pinned,
    )


# ---------------------------------------------------------------------------
# 1. CAS 键构造（纯静态，无需 PG）
# ---------------------------------------------------------------------------


def test_cas_key_is_sharded_and_normalises_suffix() -> None:
    """两级分片避免单目录对象数爆炸；后缀带不带点都收敛到同一形态."""
    key = content_store.content_object_key(_DIGEST_A, ".mp4")
    assert key == f"content/aa/aa/{_DIGEST_A}.mp4"
    assert content_store.content_object_key(_DIGEST_A, "mp4") == key
    assert content_store.content_object_key(_DIGEST_B, ".m4a").endswith(f"{_DIGEST_B}.m4a")


def test_cas_key_rejects_a_digest_too_short_to_shard() -> None:
    with pytest.raises(ValueError, match="too short to shard"):
        content_store.content_object_key("ab", ".mp4")


# ---------------------------------------------------------------------------
# 2. 属主隔离（本模块最关键的断言）
# ---------------------------------------------------------------------------


def test_user_scope_never_resolves_another_owners_bytes(pg_state: str) -> None:
    """同哈希同大小的两个属主必须各自持有登记行，且互不命中。

    这条断言是「去重不等于信息泄露」的机器化表达：缺少属主维度时，owner_b 的查找
    会返回 owner_a 的行，于是 owner_b 的项目会直接引用 owner_a 的私密素材。
    """
    same_bytes = dict(sha256=_DIGEST_A, size=2048)

    def seed(conn: BusinessConnection) -> tuple[str, str, str]:
        a, hit_a = _retain(conn, owner="owner_a", **same_bytes)
        b, hit_b = _retain(conn, owner="owner_b", **same_bytes)
        miss = content_store.find_content_object(
            conn,
            sha256=_DIGEST_A,
            size_bytes=2048,
            provider="fake",
            bucket="cas",
            scope="user",
            owner_user_id="owner_a",
        )
        assert miss is not None
        owner_b_lookup = content_store.find_content_object(
            conn,
            sha256=_DIGEST_A,
            size_bytes=2048,
            provider="fake",
            bucket="cas",
            scope="user",
            owner_user_id="owner_b",
        )
        assert owner_b_lookup is not None
        # 各自命中自己那一行，绝不交叉。
        assert miss.id == a.id
        assert owner_b_lookup.id == b.id
        assert a.id != b.id
        assert not hit_a and not hit_b
        return a.id, b.id, owner_b_lookup.id

    row_a, row_b, looked_up = _with_conn(pg_state, seed)
    assert row_a != row_b == looked_up
    assert _one(pg_state, "SELECT count(*) FROM content_objects") == 2
    assert sorted(
        r["scope_owner"] for r in _rows(pg_state, "SELECT scope_owner FROM content_objects")
    ) == ["owner_a", "owner_b"]


def test_user_scope_deduplicates_within_one_owner(pg_state: str) -> None:
    """同一属主的重复内容才允许复用，且第二次是命中而非新建."""

    def run(conn: BusinessConnection) -> tuple[bool, bool, str, str]:
        first, hit_first = _retain(conn)
        second, hit_second = _retain(conn)
        return hit_first, hit_second, first.id, second.id

    hit_first, hit_second, first_id, second_id = _with_conn(pg_state, run)
    assert hit_first is False
    assert hit_second is True
    assert first_id == second_id
    assert _one(pg_state, "SELECT count(*) FROM content_objects") == 1
    assert _one(pg_state, "SELECT ref_count FROM content_objects") == 2


def test_global_scope_is_shared_across_owners(pg_state: str) -> None:
    """``global`` 域（平台采集的公开内容）允许跨用户共享，且只留一行."""

    def run(conn: BusinessConnection) -> tuple[bool, str, str]:
        first, _ = _retain(conn, sha256=_DIGEST_B, scope="global", owner=None)
        second, hit = _retain(conn, sha256=_DIGEST_B, scope="global", owner=None)
        return hit, first.id, second.id

    hit, first_id, second_id = _with_conn(pg_state, run)
    assert hit is True
    assert first_id == second_id
    assert _one(pg_state, "SELECT count(*) FROM content_objects") == 1
    assert _one(pg_state, "SELECT scope_owner FROM content_objects") is None


def test_user_scope_requires_an_owner(pg_state: str) -> None:
    """``user`` 域缺属主必须直接失败，而不是退化成一条无主共享行."""

    def run(conn: BusinessConnection) -> None:
        with pytest.raises(ValueError, match="requires an owner_user_id"):
            _retain(conn, owner=None)

    _with_conn(pg_state, run)
    assert _one(pg_state, "SELECT count(*) FROM content_objects") == 0


# ---------------------------------------------------------------------------
# 3. 引用计数与回收
# ---------------------------------------------------------------------------


def test_release_schedules_reclaim_only_at_zero(pg_state: str) -> None:
    """减引用只改计数；归零才排期，且排期时间来自传入的宽限期."""

    def run(conn: BusinessConnection) -> tuple[int, Any, int, Any]:
        content, _ = _retain(conn)
        second, _ = _retain(conn)
        assert second.id == content.id
        remaining_one = content_store.release_content_object(conn, content.id)
        scheduled_while_referenced = content_store.find_content_object_by_id(conn, content.id)
        assert scheduled_while_referenced is not None
        remaining_zero = content_store.release_content_object(conn, content.id)
        scheduled = content_store.find_content_object_by_id(conn, content.id)
        assert scheduled is not None
        return (
            remaining_one,
            scheduled_while_referenced.reclaim_after,
            remaining_zero,
            scheduled.reclaim_after,
        )

    remaining_one, no_schedule, remaining_zero, scheduled = _with_conn(pg_state, run)
    assert remaining_one == 1
    assert no_schedule is None, "仍被引用时不得排期回收"
    assert remaining_zero == 0
    assert scheduled is not None, "归零后必须排期回收"


def test_release_is_idempotent_on_an_already_released_row(pg_state: str) -> None:
    """重复释放不得把计数压到负数（删除资产必须可重放）."""

    def run(conn: BusinessConnection) -> tuple[int, int, int]:
        content, _ = _retain(conn)
        content_store.release_content_object(conn, content.id)
        again = content_store.release_content_object(conn, content.id)
        third = content_store.release_content_object(conn, content.id)
        return again, third, content_store.find_content_object_by_id(conn, content.id).ref_count  # type: ignore[union-attr]

    again, third, final_count = _with_conn(pg_state, run)
    assert (again, third, final_count) == (0, 0, 0)


def test_release_of_an_unknown_row_reports_minus_one(pg_state: str) -> None:
    """登记行已消失不是错误：调用方据此保持幂等."""

    def run(conn: BusinessConnection) -> int:
        return content_store.release_content_object(conn, "does-not-exist")

    assert _with_conn(pg_state, run) == -1


def test_pinned_rows_are_never_scheduled_or_reclaimed(pg_state: str) -> None:
    """``pinned`` 字节由资产图之外持有（爆款共享缓存），删光项目也不得回收."""
    storage = FakeStorageAdapter(provider="fake", bucket="cas")

    def run(conn: BusinessConnection) -> tuple[content_store.ContentObject, int, dict[str, int]]:
        content, _ = _retain(conn, sha256=_DIGEST_B, scope="global", owner=None, pinned=True)
        conn.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, metadata_json, content_object_id) "
            "VALUES ('asset-pinned', 'project_a', 'reference_video', %s, %s, %s, "
            "'video/mp4', '{}', %s)",
            (content.storage_uri, _DIGEST_B, 1024, content.id),
        )
        released = content_store.release_assets_content_objects(
            conn,
            asset_ids=["asset-pinned"],
            reclaim_delay_hours=0,
        )
        after = content_store.find_content_object_by_id(conn, content.id)
        assert after is not None
        sweep = content_store.reclaim_expired_content_objects(conn, storage)
        return after, released, sweep

    after, released, sweep = _with_conn(pg_state, run)
    assert after.ref_count == 0
    assert after.reclaim_after is None, "pinned 行不得排期回收"
    assert released == 0, "pinned 行不得被记为已排期回收"
    assert sweep == {"scanned": 0, "deleted": 0, "skipped": 0, "failed": 0}
    assert _one(pg_state, "SELECT count(*) FROM content_objects") == 1


def test_reclaim_respects_grace_and_skips_rows_rereferenced_in_the_window(
    pg_state: str,
) -> None:
    """宽限期内的新引用必须赢过回收 —— 这是两阶段回收存在的全部理由."""
    storage = FakeStorageAdapter(provider="fake", bucket="cas")

    def run(conn: BusinessConnection) -> tuple[dict[str, int], dict[str, int], int, Any]:
        content, _ = _retain(conn, sha256=_DIGEST_B, scope="global", owner=None)
        stored = storage.put_object(content.object_key, b"bytes", content_type="video/mp4")
        assert stored.sha256

        # 尚未到期的行：不回收。
        released = content_store.release_content_object(conn, content.id, reclaim_delay_hours=24)
        assert released == 0
        too_early = content_store.reclaim_expired_content_objects(conn, storage)

        # 归零且已过宽限：回收。
        content_store.release_content_object(conn, content.id, reclaim_delay_hours=0)
        # 先模拟「宽限期内又被引用」：重新取一次引用必须清掉排期。
        content_store.retain_existing_content_object(conn, content.id)
        reref_then_sweep = content_store.reclaim_expired_content_objects(conn, storage)
        survived = content_store.find_content_object_by_id(conn, content.id)
        assert survived is not None
        return too_early, reref_then_sweep, survived.ref_count, survived.reclaim_after

    too_early, reref_then_sweep, ref_count, reclaim_after = _with_conn(pg_state, run)
    assert too_early["deleted"] == 0
    assert reref_then_sweep["deleted"] == 0, "宽限期内重建的引用必须阻止回收"
    assert ref_count == 1
    assert reclaim_after is None
    assert _one(pg_state, "SELECT count(*) FROM content_objects") == 1


def test_reclaim_deletes_bytes_once_the_window_closes(pg_state: str) -> None:
    """宽限期届满且无引用时，字节与登记行一起消失."""
    storage = FakeStorageAdapter(provider="fake", bucket="cas")

    def run(conn: BusinessConnection) -> tuple[dict[str, int], bool, bool]:
        content, _ = _retain(conn, sha256=_DIGEST_B, scope="global", owner=None)
        storage.put_object(content.object_key, b"bytes", content_type="video/mp4")
        content_store.release_content_object(conn, content.id, reclaim_delay_hours=0)
        sweep = content_store.reclaim_expired_content_objects(conn, storage)
        return (
            sweep,
            content_store.find_content_object_by_id(conn, content.id) is None,
            storage.head_object(content.object_key) is None,
        )

    sweep, row_gone, object_gone = _with_conn(pg_state, run)
    assert sweep["deleted"] == 1
    assert row_gone and object_gone


def test_reclaim_skips_objects_owned_by_another_backend(pg_state: str) -> None:
    """对象不在本适配器的 provider/bucket 上时留给那边的清扫负责."""
    other = FakeStorageAdapter(provider="cos", bucket="elsewhere")

    def run(conn: BusinessConnection) -> dict[str, int]:
        content, _ = _retain(conn, sha256=_DIGEST_B, scope="global", owner=None)
        content_store.release_content_object(conn, content.id, reclaim_delay_hours=0)
        return content_store.reclaim_expired_content_objects(conn, other)

    sweep = _with_conn(pg_state, run)
    assert sweep["skipped"] == 1 and sweep["deleted"] == 0
    assert _one(pg_state, "SELECT count(*) FROM content_objects") == 1


# ---------------------------------------------------------------------------
# 4. 共享引用判定（rbac_routes NULL 缺陷的回归）
# ---------------------------------------------------------------------------


def test_null_project_asset_counts_as_a_live_reference(pg_state: str) -> None:
    """``project_id IS NULL`` 的资产是**活引用**，必须阻止对象被删。

    回归目标：``rbac_routes.delete_project`` 原先写 ``project_id <> %s``；
    对 ``project_id IS NULL`` 的引用方（素材库、人物资料正是这类用户级资产），
    ``NULL <> 'p'`` 求值为 NULL 而非 TRUE，该行于是不匹配，判定退化为
    「无共享引用」→ 删掉对象 → 另一处引用直接失效。

    本用例同时钉住修复后的 ``id NOT IN`` 语义：引用方是 NULL 项目资产时，
    检查必须返回 True。
    """
    shared_uri = "fake://cas/shared/object.mp4"

    def run(conn: BusinessConnection) -> tuple[bool, bool]:
        _seed_asset(
            conn,
            asset_id="asset-in-project",
            project_id="project_a",
            storage_uri=shared_uri,
            sha256=_DIGEST_A,
            size_bytes=10,
        )
        # 用户级资产：没有项目（素材库 / 人物资料就是这个形态）。
        _seed_asset(
            conn,
            asset_id="asset-no-project",
            project_id=None,
            storage_uri=shared_uri,
            sha256=_DIGEST_A,
            size_bytes=10,
        )
        referenced_other = content_store.object_referenced_by_another_asset(
            conn,
            storage_uri=shared_uri,
            excluding_asset_ids=["asset-in-project"],
        )
        referenced_only_self = content_store.object_referenced_by_another_asset(
            conn,
            storage_uri=shared_uri,
            excluding_asset_ids=["asset-in-project", "asset-no-project"],
        )
        return referenced_other, referenced_only_self

    referenced_other, referenced_only_self = _with_conn(pg_state, run)
    assert referenced_other is True, "NULL 项目资产必须被认作共享引用"
    assert referenced_only_self is False, "排除全部持有者后才是可删"


def test_legacy_null_comparison_would_have_missed_the_null_reference(pg_state: str) -> None:
    """把缺陷本身钉住：旧写法在同样数据上返回 0 行。

    没有这条对照用例，修复看起来「只是换了个写法」；有了它，``NULL <> %s`` 的
    漏判是被证明出来的，而不是被描述的。
    """
    shared_uri = "fake://cas/shared/legacy.mp4"

    def run(conn: BusinessConnection) -> tuple[int, bool]:
        _seed_asset(
            conn,
            asset_id="legacy-null",
            project_id=None,
            storage_uri=shared_uri,
            sha256=_DIGEST_A,
            size_bytes=10,
        )
        legacy = conn.execute(
            "SELECT 1 FROM assets WHERE storage_uri = %s AND project_id <> %s LIMIT 1",
            (shared_uri, "project_a"),
        ).fetchone()
        return (0 if legacy is None else 1), content_store.object_referenced_by_another_asset(
            conn,
            storage_uri=shared_uri,
            excluding_asset_ids=["legacy-null"],
        )

    legacy_hits, helper_hits = _with_conn(pg_state, run)
    # 排除自身后旧写法本应返回 0 行；这里故意不排除自身，验证 NULL 语义本身。
    assert legacy_hits == 0, "旧写法看不见 project_id IS NULL 的行"
    assert helper_hits is False


def test_content_registry_presence_blocks_a_legacy_object_delete(pg_state: str) -> None:
    """登记行本身也是一处引用，即便它没有任何 assets 行指向."""

    def run(conn: BusinessConnection) -> tuple[bool, bool]:
        content, _ = _retain(conn, sha256=_DIGEST_B, scope="global", owner=None)
        present = content_store.object_referenced_by_content_registry(
            conn,
            provider="fake",
            bucket="cas",
            object_key=content.object_key,
        )
        excluding_self = content_store.object_referenced_by_content_registry(
            conn,
            provider="fake",
            bucket="cas",
            object_key=content.object_key,
            excluding_content_object_id=content.id,
        )
        return present, excluding_self

    present, excluding_self = _with_conn(pg_state, run)
    assert present is True
    assert excluding_self is False


# ---------------------------------------------------------------------------
# 5. 唯一索引确实生效（并发写入的收敛点）
# ---------------------------------------------------------------------------


def test_unique_indexes_reject_a_hand_written_duplicate(pg_state: str) -> None:
    """绕过 API 直接写重复行也必须被两条部分唯一索引挡住."""

    def run(conn: BusinessConnection) -> tuple[int, int]:
        _retain(conn, sha256=_DIGEST_A, owner="owner_a")
        _retain(conn, sha256=_DIGEST_B, scope="global", owner=None)
        user_dupe = 0
        global_dupe = 0
        for sql, params in (
            (
                "INSERT INTO content_objects (id, sha256, size_bytes, provider, bucket, "
                "object_key, scope, scope_owner) VALUES ('dupe-user', %s, 1024, 'fake', "
                "'cas', 'x', 'user', 'owner_a')",
                (_DIGEST_A,),
            ),
            (
                "INSERT INTO content_objects (id, sha256, size_bytes, provider, bucket, "
                "object_key, scope, scope_owner) VALUES ('dupe-global', %s, 1024, 'fake', "
                "'cas', 'y', 'global', NULL)",
                (_DIGEST_B,),
            ),
        ):
            try:
                conn.execute(sql, params)
            except Exception:  # noqa: BLE001 - 约束名随驱动版本而异，只关心是否被拒
                if "dupe-user" in sql:
                    user_dupe += 1
                else:
                    global_dupe += 1
        return user_dupe, global_dupe

    user_dupe, global_dupe = _with_conn(pg_state, run)
    assert user_dupe == 1, "user 域重复行必须被拒"
    assert global_dupe == 1, "global 域重复行必须被拒（单条 UNIQUE 约束会漏掉这条）"


def test_scope_owner_check_constraint_rejects_incoherent_rows(pg_state: str) -> None:
    """``user`` 无属主、``global`` 有属主都必须被 CHECK 拒绝."""

    def run(conn: BusinessConnection) -> tuple[int, int]:
        rejected = [0, 0]
        for index, (scope, owner) in enumerate((("user", None), ("global", "owner_a"))):
            try:
                conn.execute(
                    "INSERT INTO content_objects (id, sha256, size_bytes, provider, bucket, "
                    "object_key, scope, scope_owner) VALUES (%s, %s, 1024, 'fake', 'cas', "
                    "'k', %s, %s)",
                    (f"bad-{index}", _DIGEST_A, scope, owner),
                )
            except Exception:  # noqa: BLE001
                rejected[index] += 1
        return rejected[0], rejected[1]

    user_rejected, global_rejected = _with_conn(pg_state, run)
    assert user_rejected == 1
    assert global_rejected == 1


def test_regex_guard_bytes_do_not_leak_into_metadata_snapshot(pg_state: str) -> None:
    """审计快照只带前缀与计数，绝不带完整哈希或对象键."""

    def run(conn: BusinessConnection) -> str:
        content, _ = _retain(conn, sha256=_DIGEST_A)
        return content_store.content_object_metadata(content)

    snapshot = _with_conn(pg_state, run)
    assert _DIGEST_A not in snapshot
    assert _DIGEST_A[:12] in snapshot
    assert "content/aa/aa" not in snapshot


def test_reclaim_delay_default_is_the_approved_grace_window() -> None:
    """回收宽限期是评审拍板的 24 小时，改动需显式改这个常量."""
    assert content_store.RECLAIM_DELAY_HOURS == 24
    assert timedelta(hours=content_store.RECLAIM_DELAY_HOURS) == timedelta(hours=24)


def test_registry_check_can_exclude_an_own_registration(pg_state: str) -> None:
    """自身的登记行不能被算作「仍有引用」，否则字节永不回收.

    人物身份清理要在删除前判断这些字节是否还被别人用着。它自己的登记行在
    release 之后仍留在表里（只有 sweeper 才删行），所以如果不排除自己，
    一旦人物上传接入内容寻址，每个身份都会被自己判成「共享」——姿态安全
    （不会误删），但字节永久泄漏。
    """

    def run(conn: BusinessConnection) -> None:
        content, _ = _retain(conn, key="content/ab/cd/own.mp4")
        kwargs = {"provider": "fake", "bucket": "cas", "object_key": "content/ab/cd/own.mp4"}
        assert content_store.object_referenced_by_content_registry(conn, **kwargs), (
            "登记行存在时必须判定为被引用"
        )
        assert not content_store.object_referenced_by_content_registry(
            conn, excluding_content_object_id=content.id, **kwargs
        ), "排除自身之后不应再算作引用"

    _with_conn(pg_state, run)


# ---------------------------------------------------------------------------
# 8. A2 素材库上传去重 —— 用户最初的问题就落在这个入口
# ---------------------------------------------------------------------------


def _material_payload() -> tuple[bytes, str]:
    import hashlib

    payload = b"\x89PNG\r\n\x1a\n" + b"cas-material-payload" * 24
    return payload, hashlib.sha256(payload).hexdigest()


def _actor(owner: str) -> CurrentUser:
    return CurrentUser(id=owner, username=owner, display_name=owner, role="employee")


def _material_request(payload: bytes, digest: str, filename: str) -> MaterialUploadIntentRequest:
    return MaterialUploadIntentRequest(
        filename=filename,
        content_type="image/png",
        size_bytes=len(payload),
        sha256=digest,
    )


def _upload_material_once(
    conn: BusinessConnection,
    *,
    owner: str,
    storage: FakeStorageAdapter,
    payload: bytes,
    digest: str,
) -> Any:
    """走完「建意图 → 传输 → 落库」，把字节登记进 content_objects."""
    actor = _actor(owner)
    first = create_material_upload_intent(
        conn,
        actor=actor,
        storage=storage,
        request=_material_request(payload, digest, "shot.png"),
    )
    assert first.upload_required is True, "首次上传必须走真实传输"
    storage.put_object(first.storage_key, payload, content_type="image/png")
    prepared = prepare_material_upload(conn, actor=actor, asset_id=first.asset_id)
    probed = ProbedMaterialUpload(
        prepared=prepared,
        storage_uri=prepared.storage_uri,
        sha256=digest,
        size_bytes=len(payload),
    )
    persist_material_upload(conn, actor=actor, probed=probed, storage=storage)
    return first


def test_material_upload_reuses_bytes_within_one_owner(pg_state: str) -> None:
    """同一属主重复上传同一文件：第二次直接建成，前端无需再传一次."""

    def run(conn: BusinessConnection) -> None:
        storage = FakeStorageAdapter(provider="fake", bucket="cas")
        payload, digest = _material_payload()
        first = _upload_material_once(
            conn, owner="owner_a", storage=storage, payload=payload, digest=digest
        )

        second = create_material_upload_intent(
            conn,
            actor=_actor("owner_a"),
            storage=storage,
            request=_material_request(payload, digest, "shot-again.png"),
        )
        assert second.upload_required is False, "同一属主的相同字节应直接复用"
        assert second.asset_id != first.asset_id, "复用的是字节，不是资产行"
        assert second.storage_key == first.storage_key

        rows = conn.execute(
            "SELECT id, storage_uri, content_object_id FROM assets "
            "WHERE sha256 = %s ORDER BY created_at",
            (digest,),
        ).fetchall()
        assert len(rows) == 2
        assert len({str(row["storage_uri"]) for row in rows}) == 1, "两次上传必须指向同一份字节"

        registered = content_store.find_content_object_by_id(
            conn, str(rows[0]["content_object_id"])
        )
        assert registered is not None
        assert registered.ref_count == 2, "两条资产各持一个引用"

    _with_conn(pg_state, run)


def test_material_upload_never_reuses_another_owners_bytes(pg_state: str) -> None:
    """跨用户绝不能命中：素材常常是私人影像、声音或肖像."""

    def run(conn: BusinessConnection) -> None:
        storage = FakeStorageAdapter(provider="fake", bucket="cas")
        payload, digest = _material_payload()
        first = _upload_material_once(
            conn, owner="owner_a", storage=storage, payload=payload, digest=digest
        )

        other = create_material_upload_intent(
            conn,
            actor=_actor("owner_b"),
            storage=storage,
            request=_material_request(payload, digest, "same-bytes.png"),
        )
        assert other.upload_required is True, (
            "跨用户命中会把甲方的私有素材暴露给乙方，是必须挡住的红线"
        )
        assert other.storage_key != first.storage_key
        assert other.storage_key is not None
        assert other.storage_key.startswith("materials/owner_b/")

    _with_conn(pg_state, run)


# ---------------------------------------------------------------------------
# 9. 阶段 1：存量回填
# ---------------------------------------------------------------------------


def test_backfill_registers_existing_bytes_without_moving_them(pg_state: str) -> None:
    """存量回填：登记已有字节但不搬迁，让改造前的老素材也能被复用."""

    def run(conn: BusinessConnection) -> None:
        _seed_asset(
            conn,
            asset_id="legacy_a",
            project_id="project_a",
            storage_uri="fake://cas/legacy/a.mp4",
            sha256=_DIGEST_A,
            size_bytes=1024,
        )
        _seed_asset(
            conn,
            asset_id="legacy_b",
            project_id="project_a",
            storage_uri="fake://cas/legacy/b.mp4",
            sha256=_DIGEST_A,
            size_bytes=1024,
        )

        preview = content_store.backfill_content_objects(conn, apply=False)
        assert preview["scanned"] == 2
        assert preview["registered"] == 1, "同一份内容只应登记一条"
        assert conn.execute("SELECT count(*) FROM content_objects").fetchone()[0] == 0, (
            "预览模式不得写库"
        )

        applied = content_store.backfill_content_objects(conn, apply=True)
        assert applied["linked"] == 2
        assert applied["registered"] == 1
        assert applied["reused"] == 1

        rows = conn.execute(
            "SELECT content_object_id, storage_uri FROM assets WHERE id IN ('legacy_a', 'legacy_b')"
        ).fetchall()
        assert len({str(row["content_object_id"]) for row in rows}) == 1, "两条资产共用一个登记行"
        # 键保留原位：本阶段不做物理搬迁
        assert {str(row["storage_uri"]) for row in rows} == {
            "fake://cas/legacy/a.mp4",
            "fake://cas/legacy/b.mp4",
        }

        again = content_store.backfill_content_objects(conn, apply=True)
        assert again["scanned"] == 0, "回填必须幂等：已回填的资产会被跳过"

    _with_conn(pg_state, run)


def test_backfill_keeps_each_owner_in_their_own_scope(pg_state: str) -> None:
    """两个用户持有相同字节，回填后仍是两条登记行，绝不合并."""

    def run(conn: BusinessConnection) -> None:
        _seed_asset(
            conn,
            asset_id="own_a",
            project_id="project_a",
            storage_uri="fake://cas/legacy/owner-a.mp4",
            sha256=_DIGEST_A,
            size_bytes=1024,
        )
        _seed_asset(
            conn,
            asset_id="own_b",
            project_id="project_b",
            storage_uri="fake://cas/legacy/owner-b.mp4",
            sha256=_DIGEST_A,
            size_bytes=1024,
        )
        result = content_store.backfill_content_objects(conn, apply=True)
        assert result["registered"] == 2, "跨用户合并会把私人素材串到别人名下"

        owners = conn.execute(
            "SELECT scope_owner, ref_count FROM content_objects ORDER BY scope_owner"
        ).fetchall()
        assert [str(row["scope_owner"]) for row in owners] == ["owner_a", "owner_b"]
        assert [int(row["ref_count"]) for row in owners] == [1, 1]

    _with_conn(pg_state, run)


# A minimal PDF that satisfies the authorization content validator.
_AUTHORIZATION_PDF = b"%PDF-1.7 authorization"


def _identity_admin(owner: str) -> CurrentUser:
    """Identity uploads are admin-gated, unlike material uploads."""
    return CurrentUser(id=owner, username=owner, display_name=owner, role="admin")


def _new_identity(conn: BusinessConnection, owner: str) -> str:
    identity = create_person_identity(
        conn,
        actor=_identity_admin(owner),
        display_name=f"identity {owner}",
        owner_user_id=owner,
        authorization_scope=["video"],
        authorization_expires_at=None,
    )
    return str(identity.id)


def _complete_authorization_once(
    conn: BusinessConnection,
    *,
    owner: str,
    storage: FakeStorageAdapter,
    identity_id: str,
) -> str:
    """Upload and complete one authorization file, returning its asset id."""
    intent = create_identity_upload_intent(
        conn,
        actor=_identity_admin(owner),
        storage=storage,
        identity_id=identity_id,
        purpose="authorization",
        filename="auth.pdf",
        content_type="application/pdf",
        size_bytes=len(_AUTHORIZATION_PDF),
    )
    storage.put_object(intent.storage_key, _AUTHORIZATION_PDF, content_type="application/pdf")
    complete_authorization_upload(
        conn,
        actor=_identity_admin(owner),
        storage=storage,
        identity_id=identity_id,
        asset_id=intent.asset_id,
    )
    return str(intent.asset_id)


def _asset_uris(conn: BusinessConnection, asset_ids: list[str]) -> set[str]:
    rows = conn.execute(
        "SELECT storage_uri, content_object_id FROM assets WHERE id = ANY(%s)",
        (asset_ids,),
    ).fetchall()
    return {str(row["storage_uri"]) for row in rows}


def test_authorization_upload_reuses_bytes_within_one_owner(pg_state: str) -> None:
    """同一属主重复上传同一份授权书：第二份改指已有字节，不再写第二份。"""

    def run(conn: BusinessConnection) -> None:
        storage = FakeStorageAdapter(provider="fake", bucket="cas")
        identity_id = _new_identity(conn, "owner_a")
        first = _complete_authorization_once(
            conn, owner="owner_a", storage=storage, identity_id=identity_id
        )
        second = _complete_authorization_once(
            conn, owner="owner_a", storage=storage, identity_id=identity_id
        )
        assert first != second, "复用的是字节，不是资产行"
        assert len(_asset_uris(conn, [first, second])) == 1, (
            "同一属主的同一份授权书必须指向同一份字节"
        )
        # Reuse must not write a second *verified* copy: that key embeds the
        # asset id, so without the dedup check every upload lands on a fresh key.
        # The pending object of the second upload is still there — the client
        # transferred it — but it is never promoted to a second verified copy.
        verified_keys = [
            key
            for key in storage._objects  # noqa: SLF001
            if key.startswith("verified-uploads/")
        ]
        assert len(verified_keys) == 1, "复用时不该再写一份 verified 字节"

        registered_id = conn.execute(
            "SELECT content_object_id FROM assets WHERE id = %s", (second,)
        ).fetchone()
        assert registered_id is not None
        registered = content_store.find_content_object_by_id(
            conn, str(registered_id["content_object_id"])
        )
        assert registered is not None
        assert registered.ref_count == 2, "两份资产各持一个引用"

    _with_conn(pg_state, run)


def test_authorization_upload_never_reuses_another_owners_bytes(pg_state: str) -> None:
    """跨用户绝不能命中：授权书含身份信息，哈希相同也必须各自存一份。"""

    def run(conn: BusinessConnection) -> None:
        storage = FakeStorageAdapter(provider="fake", bucket="cas")
        identity_a = _new_identity(conn, "owner_a")
        identity_b = _new_identity(conn, "owner_b")
        first = _complete_authorization_once(
            conn, owner="owner_a", storage=storage, identity_id=identity_a
        )
        other = _complete_authorization_once(
            conn, owner="owner_b", storage=storage, identity_id=identity_b
        )
        assert len(_asset_uris(conn, [first, other])) == 2, (
            "跨用户命中会把甲方的身份材料暴露给乙方，是必须挡住的红线"
        )
        owners = conn.execute(
            "SELECT scope_owner, ref_count FROM content_objects ORDER BY scope_owner"
        ).fetchall()
        assert [str(row["scope_owner"]) for row in owners] == ["owner_a", "owner_b"]
        assert [int(row["ref_count"]) for row in owners] == [1, 1]

    _with_conn(pg_state, run)


@pytest.mark.parametrize("source_purpose", ["voice_clone", "oral_audio"])
def test_reused_voice_sample_preserves_probed_duration(pg_state: str, source_purpose: str) -> None:
    import hashlib
    import json

    def run(conn: BusinessConnection) -> None:
        storage = FakeStorageAdapter(provider="fake", bucket="cas")
        payload = b"ID3-test-voice-sample"
        digest = hashlib.sha256(payload).hexdigest()
        request = MaterialUploadIntentRequest(
            filename="voice.mp3",
            content_type="audio/mpeg",
            size_bytes=len(payload),
            sha256=digest,
            audio_purpose=source_purpose,
            duration_seconds=30,
        )
        first = create_material_upload_intent(
            conn, actor=_actor("owner_a"), storage=storage, request=request
        )
        storage.put_object(first.storage_key, payload, content_type="audio/mpeg")
        prepared = prepare_material_upload(conn, actor=_actor("owner_a"), asset_id=first.asset_id)
        persist_material_upload(
            conn,
            actor=_actor("owner_a"),
            storage=storage,
            probed=ProbedMaterialUpload(
                prepared=prepared,
                storage_uri=prepared.storage_uri,
                sha256=digest,
                size_bytes=len(payload),
                duration_seconds=30.2,
            ),
        )
        # Even a plausible client duration is not the trusted duration.
        reused = create_material_upload_intent(
            conn,
            actor=_actor("owner_a"),
            storage=storage,
            request=request.model_copy(
                update={"audio_purpose": "voice_clone", "duration_seconds": 30.8}
            ),
        )
        assert reused.upload_required is False
        row = conn.execute(
            "SELECT metadata_json FROM assets WHERE id = %s", (reused.asset_id,)
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        assert metadata["audio_duration_verified"] is True
        assert metadata["duration_seconds"] == 30.2
        assert metadata["audio_purpose"] == "voice_clone"

    _with_conn(pg_state, run)


def test_reuse_cannot_relabel_long_audio_as_voice_sample(pg_state: str) -> None:
    import hashlib

    from fastapi import HTTPException

    def run(conn: BusinessConnection) -> None:
        storage = FakeStorageAdapter(provider="fake", bucket="cas")
        payload = b"ID3-test-long-speech"
        digest = hashlib.sha256(payload).hexdigest()
        request = MaterialUploadIntentRequest(
            filename="speech.mp3",
            content_type="audio/mpeg",
            size_bytes=len(payload),
            sha256=digest,
            audio_purpose="oral_audio",
            duration_seconds=300,
        )
        first = create_material_upload_intent(
            conn, actor=_actor("owner_a"), storage=storage, request=request
        )
        storage.put_object(first.storage_key, payload, content_type="audio/mpeg")
        prepared = prepare_material_upload(conn, actor=_actor("owner_a"), asset_id=first.asset_id)
        persist_material_upload(
            conn,
            actor=_actor("owner_a"),
            storage=storage,
            probed=ProbedMaterialUpload(
                prepared=prepared,
                storage_uri=prepared.storage_uri,
                sha256=digest,
                size_bytes=len(payload),
                duration_seconds=300,
            ),
        )
        with pytest.raises(HTTPException):
            create_material_upload_intent(
                conn,
                actor=_actor("owner_a"),
                storage=storage,
                request=request.model_copy(
                    update={"audio_purpose": "voice_clone", "duration_seconds": 30}
                ),
            )
        assert conn.execute("SELECT ref_count FROM content_objects").fetchone()["ref_count"] == 1

    _with_conn(pg_state, run)
