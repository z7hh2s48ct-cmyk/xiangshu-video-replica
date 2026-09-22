"""充值套餐（管理员配置档位 + 赠送积分 + 消耗侧折扣权益）测试.

分组：
- A 组：纯函数单元（草稿校验 / 接口键归一 / 快照编解码）——无 PG，本地全绿。
- B 组：``app.recharge_packages`` 服务（CRUD + 权益授予语义）——共享库 pg_dsn。
- C 组：迁移结构（recharge_packages 表 / 订单两列 / CHECK 三支重写）——pg_dsn。

PG 组沿 test_cw075_discount_model 先例：共享库 fixture、pg_test_kit 硬门、模块级套件锁，
只按 ``rpkg-`` 前缀 DELETE（绝不新建库、绝不 TRUNCATE）。

D 组路由测试复用 test_customer_center 的专用库路由基座（与 test_customer_pricing 同款）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from pg_test_kit import (
    require_pg_or_explicit_skip,
    resolve_test_dsn,
    shared_suite_lock,
    upgrade_test_database_to_head,
)
from test_customer_center import (  # noqa: F401
    account,
    client,
    registration_client,
    registration_dsn,
    route_state,
)

from app.db_pg import close_pg_pool
from app.db_portable import BusinessConnection
from app.discount_service import get_active_discounts, get_best_discount
from app.recharge_packages import (
    PACKAGE_DISCOUNT_PRIORITY,
    PackageNotFound,
    PackageVersionConflict,
    RechargePackage,
    build_package_snapshot,
    create_package,
    grant_package_discount,
    list_packages,
    normalize_interfaces,
    parse_interfaces_json,
    parse_package_snapshot,
    read_package,
    update_package,
    validate_package_draft,
)

DB_NAME_SHARED = "customer_v3_test"
RPKG = "rpkg-"


def _new_user(tag: str) -> str:
    return f"{RPKG}{tag}-{uuid4().hex[:10]}"


def _new_order_no(tag: str) -> str:
    return f"{RPKG}order-{tag}-{uuid4().hex[:10]}"


def _clean_rpkg(dsn: str) -> None:
    """删除全部 rpkg- 作用域行（子表先于父表满足外键；只 DELETE）。

    ``billing_credit_lots.id`` 引用 ``wallet_transactions.id``，必须先删 lot
    再删流水行。套餐行统一以 ``rpkg-`` 前缀创建（create_package 契约）：订单侧
    按 user 前缀或 ``package_id`` 前缀兜底，避免跨套件订单引用 rpkg 套餐时留下
    外键孤儿（各套件共享库串行，兜底删除不影响并发正确性）。
    """
    like = RPKG + "%"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM customer_discounts WHERE user_id LIKE %s "
            "OR source_recharge_order_id LIKE %s",
            (like, like),
        )
        conn.execute("DELETE FROM billing_credit_lots WHERE user_id LIKE %s", (like,))
        conn.execute("DELETE FROM wallet_transactions WHERE user_id LIKE %s", (like,))
        # billing_operations 有不可变事实触发器（拒绝 DELETE）；session_replication_role
        # 仅放行本会话，不改共享库触发器定义。
        conn.execute("SET session_replication_role = replica")
        conn.execute("DELETE FROM billing_operations WHERE user_id LIKE %s", (like,))
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute("DELETE FROM wallets WHERE user_id LIKE %s", (like,))
        conn.execute(
            "DELETE FROM recharge_orders WHERE user_id LIKE %s OR package_id LIKE %s",
            (like, like),
        )
        conn.execute("DELETE FROM recharge_packages WHERE id LIKE %s", (like,))
        conn.execute("DELETE FROM users WHERE id LIKE %s", (like,))


def _seed_customer(dsn: str, user_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES (%s, %s, %s, 'customer') ON CONFLICT (id) DO NOTHING",
            (user_id, user_id, f"RPKG {user_id}"),
        )


def _seed_order(
    dsn: str,
    *,
    user_id: str,
    order_no: str,
    amount_fen: int = 199_800,
    credits: int | None = None,
    credit_pricing_snapshot_json: str | None = None,
    package_id: str | None = None,
    package_snapshot_json: str | None = None,
    step_fen: int = 1,
) -> str:
    """直接落一条 recharge_orders（绕过网关），返回其 id。

    ``credits=None``（缺省）按基础汇率换算（1 元 = 1 积分），与无快照订单的
    ``charged_unit_price_fen_snapshot=100`` 满足 CHECK 第一支；套餐订单由调用方
    显式传快照与 credits（快照三支校验）。``step_fen`` 供限额约束对照用。
    """
    if credits is None:
        credits = amount_fen // 100
    order_id = f"{RPKG}order-row-{uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO recharge_orders ("
            "id, user_id, merchant_order_no, provider, provider_trade_no, channel, status, "
            "pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
            "credit_pricing_snapshot_json, package_id, package_snapshot_json) "
            "VALUES (%s, %s, %s, 'zpay', NULL, 'alipay', 'PENDING', 'CUSTOMER_STANDARD', "
            "100, 100, 1, %s, %s, %s, %s, %s, %s)",
            (
                order_id,
                user_id,
                order_no,
                step_fen,
                amount_fen,
                credits,
                credit_pricing_snapshot_json,
                package_id,
                package_snapshot_json,
            ),
        )
    return order_id


def _seed_wallet(dsn: str, user_id: str, *, credits: int) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES (%s, %s, 0) ON CONFLICT (user_id) DO UPDATE SET available_credits = %s",
            (user_id, credits, credits),
        )


def _seed_discount(
    dsn: str,
    user_id: str,
    *,
    rate: Decimal,
    priority: int,
    interfaces: list[str] | None = None,
    source_order_id: str | None = None,
) -> str:
    discount_id = f"{RPKG}manual-{uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_discounts (id, user_id, discount_rate, priority, "
            "applicable_interfaces, is_active, source_recharge_order_id) "
            "VALUES (%s, %s, %s, %s, %s, true, %s)",
            (
                discount_id,
                user_id,
                rate,
                priority,
                json.dumps(list(interfaces or [])),
                source_order_id,
            ),
        )
    return discount_id


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    """共享库 DSN + CW-007 硬门 + 迁移到 head + 模块级套件锁（沿 CW-031/CW-075）。"""
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert dsn.rsplit("/", 1)[1] == DB_NAME_SHARED, "本套件只准用共享库 customer_v3_test"
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        _clean_rpkg(dsn)
        try:
            yield dsn
        finally:
            _clean_rpkg(dsn)
            close_pg_pool()


@pytest.fixture()
def pg_conn(pg_dsn: str) -> Iterator[BusinessConnection]:
    with psycopg.connect(pg_dsn) as raw:
        yield BusinessConnection.postgres(raw)


# ===========================================================================
# A 组：纯单元（无 PG）——草稿校验 / 接口键归一 / 快照编解码
# ===========================================================================


def test_validate_package_draft_minimal_no_benefit() -> None:
    """充值 500 到账 500、无折扣、无接口范围——最朴素档位必须合法。"""
    validate_package_draft(
        name="基础充值",
        amount_fen=50_000,
        credits=500,
        discount_rate=None,
        discount_interfaces=[],
    )


def test_validate_package_draft_bonus_and_discount() -> None:
    """充值 1998 到账 2000 积分 + 视频生成 9 折。"""
    validate_package_draft(
        name="超值套餐",
        amount_fen=199_800,
        credits=2_000,
        discount_rate="0.9",
        discount_interfaces=["video_generation"],
    )


def test_validate_package_draft_rejects_blank_name() -> None:
    with pytest.raises(ValueError, match="套餐名称"):
        validate_package_draft(
            name="   ",
            amount_fen=50_000,
            credits=500,
            discount_rate=None,
            discount_interfaces=[],
        )


@pytest.mark.parametrize("amount_fen", [0, -1, 2_147_483_648])
def test_validate_package_draft_rejects_bad_amount(amount_fen: int) -> None:
    with pytest.raises(ValueError, match="充值金额"):
        validate_package_draft(
            name="套餐",
            amount_fen=amount_fen,
            credits=500,
            discount_rate=None,
            discount_interfaces=[],
        )


@pytest.mark.parametrize("credits", [0, -5, 2_147_483_648])
def test_validate_package_draft_rejects_bad_credits(credits: int) -> None:
    with pytest.raises(ValueError, match="到账积分"):
        validate_package_draft(
            name="套餐",
            amount_fen=50_000,
            credits=credits,
            discount_rate=None,
            discount_interfaces=[],
        )


@pytest.mark.parametrize("rate", ["0", "-0.1", "1.2"])
def test_validate_package_draft_rejects_bad_rate(rate: str) -> None:
    with pytest.raises(ValueError):
        validate_package_draft(
            name="套餐",
            amount_fen=50_000,
            credits=500,
            discount_rate=rate,
            discount_interfaces=[],
        )


def test_validate_package_draft_rejects_interfaces_without_rate() -> None:
    """无折扣却勾选接口范围是配置笔误，必须显式拒绝而不是静默忽略。"""
    with pytest.raises(ValueError, match="折扣"):
        validate_package_draft(
            name="套餐",
            amount_fen=50_000,
            credits=500,
            discount_rate=None,
            discount_interfaces=["video_generation"],
        )


def test_validate_package_draft_rejects_unknown_interface() -> None:
    with pytest.raises(ValueError, match="接口"):
        validate_package_draft(
            name="套餐",
            amount_fen=50_000,
            credits=500,
            discount_rate="0.9",
            discount_interfaces=["video_generation", "not_a_real_key"],
        )


def test_normalize_interfaces_dedupes_and_keeps_order() -> None:
    assert normalize_interfaces(["oral", "video_generation", "oral"]) == [
        "oral",
        "video_generation",
    ]


def test_normalize_interfaces_empty() -> None:
    assert normalize_interfaces(None) == []
    assert normalize_interfaces([]) == []


def test_parse_interfaces_json_tolerates_garbage() -> None:
    assert parse_interfaces_json(None) == ()
    assert parse_interfaces_json("") == ()
    assert parse_interfaces_json("not-json") == ()
    assert parse_interfaces_json('{"a": 1}') == ()
    assert parse_interfaces_json('["video_generation"]') == ("video_generation",)


def test_package_snapshot_round_trip() -> None:
    package = RechargePackage(
        id="pkg-snap",
        name="超值套餐",
        amount_fen=199_800,
        credits=2_000,
        discount_rate=Decimal("0.9000"),
        discount_interfaces=("video_generation",),
        sort_order=1,
        is_active=True,
        version=3,
        created_at=None,
        updated_at=None,
    )
    snapshot = build_package_snapshot(package)
    assert snapshot["package_id"] == "pkg-snap"
    assert snapshot["amount_fen"] == 199_800
    assert snapshot["credits"] == 2_000
    assert snapshot["discount_rate"] == "0.9000"
    assert snapshot["discount_interfaces"] == ["video_generation"]
    restored = parse_package_snapshot(json.dumps(snapshot))
    assert restored is not None
    assert restored["credits"] == 2_000
    assert restored["discount_rate"] == "0.9000"


def test_parse_package_snapshot_null_and_garbage() -> None:
    assert parse_package_snapshot(None) is None
    assert parse_package_snapshot("") is None
    assert parse_package_snapshot("[]") is None
    assert parse_package_snapshot("not-json") is None


# ===========================================================================
# B 组：服务（共享库 pg_dsn，CI-Linux-only）
# ===========================================================================


def test_create_and_read_package(pg_dsn: str, pg_conn: BusinessConnection) -> None:
    actor = _new_user("actor")
    _seed_customer(pg_dsn, actor)
    created = create_package(
        pg_conn,
        name="入门档",
        amount_fen=50_000,
        credits=500,
        discount_rate=None,
        discount_interfaces=[],
        sort_order=2,
        is_active=True,
        actor_user_id=actor,
    )
    assert created.id.startswith(RPKG)
    assert created.credits == 500
    assert created.discount_rate is None
    assert created.version == 0
    read = read_package(pg_conn, created.id)
    assert read is not None
    assert read.name == "入门档"
    assert read.amount_fen == 50_000


def test_list_packages_orders_and_filters(pg_dsn: str, pg_conn: BusinessConnection) -> None:
    actor = _new_user("list")
    _seed_customer(pg_dsn, actor)
    first = create_package(
        pg_conn,
        name="排序一",
        amount_fen=10_000,
        credits=100,
        discount_rate=None,
        discount_interfaces=[],
        sort_order=1,
        is_active=True,
        actor_user_id=actor,
    )
    second = create_package(
        pg_conn,
        name="排序二",
        amount_fen=20_000,
        credits=200,
        discount_rate=None,
        discount_interfaces=[],
        sort_order=5,
        is_active=False,
        actor_user_id=actor,
    )
    active = [item.id for item in list_packages(pg_conn, active_only=True)]
    assert first.id in active
    assert second.id not in active
    everything = [item.id for item in list_packages(pg_conn)]
    assert everything.index(first.id) < everything.index(second.id)


def test_update_package_bumps_version(pg_dsn: str, pg_conn: BusinessConnection) -> None:
    actor = _new_user("upd")
    _seed_customer(pg_dsn, actor)
    created = create_package(
        pg_conn,
        name="原套餐",
        amount_fen=50_000,
        credits=500,
        discount_rate=None,
        discount_interfaces=[],
        sort_order=0,
        is_active=True,
        actor_user_id=actor,
    )
    updated = update_package(
        pg_conn,
        package_id=created.id,
        expected_version=created.version,
        name="改后套餐",
        amount_fen=199_800,
        credits=2_000,
        discount_rate="0.9",
        discount_interfaces=["video_generation"],
        sort_order=0,
        is_active=True,
        actor_user_id=actor,
    )
    assert updated.version == created.version + 1
    assert updated.credits == 2_000
    assert updated.discount_rate == Decimal("0.9000")
    assert updated.discount_interfaces == ("video_generation",)


def test_update_package_version_conflict(pg_dsn: str, pg_conn: BusinessConnection) -> None:
    actor = _new_user("conflict")
    _seed_customer(pg_dsn, actor)
    created = create_package(
        pg_conn,
        name="并发套餐",
        amount_fen=50_000,
        credits=500,
        discount_rate=None,
        discount_interfaces=[],
        sort_order=0,
        is_active=True,
        actor_user_id=actor,
    )
    with pytest.raises(PackageVersionConflict):
        update_package(
            pg_conn,
            package_id=created.id,
            expected_version=created.version + 3,
            name="并发套餐",
            amount_fen=50_000,
            credits=500,
            discount_rate=None,
            discount_interfaces=[],
            sort_order=0,
            is_active=True,
            actor_user_id=actor,
        )


def test_update_package_not_found(pg_conn: BusinessConnection) -> None:
    with pytest.raises(PackageNotFound):
        update_package(
            pg_conn,
            package_id=f"{RPKG}missing-{uuid4().hex[:8]}",
            expected_version=0,
            name="不存在",
            amount_fen=50_000,
            credits=500,
            discount_rate=None,
            discount_interfaces=[],
            sort_order=0,
            is_active=True,
            actor_user_id=None,
        )


def test_grant_package_discount_without_rate_is_noop(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """无权益套餐不得触碰已有权益（用户口径 2026-09-22）。"""
    user_id = _new_user("noop")
    _seed_customer(pg_dsn, user_id)
    _seed_discount(pg_dsn, user_id, rate=Decimal("0.8000"), priority=10)
    order_id = _seed_order(dsn=pg_dsn, user_id=user_id, order_no=_new_order_no("noop"))
    assert (
        grant_package_discount(
            pg_conn,
            user_id=user_id,
            source_recharge_order_id=order_id,
            discount_rate=None,
            discount_interfaces=(),
        )
        is None
    )
    discounts = get_active_discounts(pg_conn.raw, user_id=user_id)
    assert len(discounts) == 1
    assert discounts[0].discount_rate == Decimal("0.8000")


def test_grant_package_discount_inserts_permanent_row(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    user_id = _new_user("grant")
    _seed_customer(pg_dsn, user_id)
    order_id = _seed_order(dsn=pg_dsn, user_id=user_id, order_no=_new_order_no("grant"))
    granted = grant_package_discount(
        pg_conn,
        user_id=user_id,
        source_recharge_order_id=order_id,
        discount_rate="0.9000",
        discount_interfaces=("video_generation",),
    )
    assert granted is not None
    best = get_best_discount(pg_conn.raw, user_id=user_id)
    assert best is not None
    assert best.discount_rate == Decimal("0.9000")
    assert best.priority == PACKAGE_DISCOUNT_PRIORITY
    assert best.valid_until is None
    assert best.applicable_interfaces == ("video_generation",)
    row = pg_conn.raw.execute(
        "SELECT source_recharge_order_id FROM customer_discounts WHERE id = %s", (granted,)
    ).fetchone()
    assert row is not None and str(row[0]) == order_id


def test_grant_package_discount_recent_overrides_and_keeps_manual(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """带权益套餐最近覆盖同源旧行；手工折扣（无来源订单）不受影响。"""
    user_id = _new_user("override")
    _seed_customer(pg_dsn, user_id)
    manual_id = _seed_discount(pg_dsn, user_id, rate=Decimal("0.9500"), priority=5)
    first_order = _seed_order(dsn=pg_dsn, user_id=user_id, order_no=_new_order_no("ov1"))
    second_order = _seed_order(dsn=pg_dsn, user_id=user_id, order_no=_new_order_no("ov2"))
    first = grant_package_discount(
        pg_conn,
        user_id=user_id,
        source_recharge_order_id=first_order,
        discount_rate="0.9000",
        discount_interfaces=(),
    )
    second = grant_package_discount(
        pg_conn,
        user_id=user_id,
        source_recharge_order_id=second_order,
        discount_rate="0.8500",
        discount_interfaces=(),
    )
    assert first is not None and second is not None and first != second
    rows = pg_conn.raw.execute(
        "SELECT id, is_active FROM customer_discounts WHERE user_id = %s", (user_id,)
    ).fetchall()
    state = {str(row[0]): bool(row[1]) for row in rows}
    assert state[first] is False
    assert state[second] is True
    assert state[manual_id] is True
    best = get_best_discount(pg_conn.raw, user_id=user_id)
    assert best is not None and best.discount_rate == Decimal("0.8500")


def test_grant_package_discount_same_order_is_idempotent(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    user_id = _new_user("idem")
    _seed_customer(pg_dsn, user_id)
    order_id = _seed_order(dsn=pg_dsn, user_id=user_id, order_no=_new_order_no("idem"))
    first = grant_package_discount(
        pg_conn,
        user_id=user_id,
        source_recharge_order_id=order_id,
        discount_rate="0.9000",
        discount_interfaces=(),
    )
    second = grant_package_discount(
        pg_conn,
        user_id=user_id,
        source_recharge_order_id=order_id,
        discount_rate="0.9000",
        discount_interfaces=(),
    )
    assert first == second
    count = pg_conn.raw.execute(
        "SELECT COUNT(*) FROM customer_discounts WHERE source_recharge_order_id = %s",
        (order_id,),
    ).fetchone()
    assert count is not None and int(count[0]) == 1


def test_grant_package_discount_interface_scope(pg_dsn: str, pg_conn: BusinessConnection) -> None:
    user_id = _new_user("scope")
    _seed_customer(pg_dsn, user_id)
    order_id = _seed_order(dsn=pg_dsn, user_id=user_id, order_no=_new_order_no("scope"))
    grant_package_discount(
        pg_conn,
        user_id=user_id,
        source_recharge_order_id=order_id,
        discount_rate="0.9000",
        discount_interfaces=("video_generation",),
    )
    assert (
        get_best_discount(pg_conn.raw, user_id=user_id, interface_key="video_generation")
        is not None
    )
    assert get_best_discount(pg_conn.raw, user_id=user_id, interface_key="oral") is None


def test_grant_package_discount_serializes_on_user_advisory_lock(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """并发授予同一用户必须被用户级 advisory 锁串行化（M-1，评审修复）.

    持锁连接先占住 ``billing:customer-discount:<user>`` 事务锁 → 独立线程内的
    授予阻塞为 pg_locks 中未授予的 advisory waiter → 释放后授予完成且全用户
    仅一条 active 权益行（无锁实现下并发可产生两条 active，「最近覆盖」失效）。
    """
    user_id = _new_user("lock")
    _seed_customer(pg_dsn, user_id)
    order_id = _seed_order(dsn=pg_dsn, user_id=user_id, order_no=_new_order_no("lock"))
    lock_sql = "SELECT pg_advisory_xact_lock(hashtext('billing:customer-discount:' || %s))"
    completed = threading.Event()
    outcome: dict[str, object] = {}

    def _grant() -> None:
        try:
            with psycopg.connect(pg_dsn) as raw:
                outcome["granted"] = grant_package_discount(
                    BusinessConnection.postgres(raw),
                    user_id=user_id,
                    source_recharge_order_id=order_id,
                    discount_rate="0.9000",
                    discount_interfaces=(),
                )
        except Exception as exc:  # pragma: no cover - 异常由主线程断言兜底
            outcome["error"] = exc
        finally:
            completed.set()

    with psycopg.connect(pg_dsn) as holder:
        holder.execute(lock_sql, (user_id,))
        worker = threading.Thread(target=_grant, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10.0
        blocked = False
        while time.monotonic() < deadline:
            row = holder.execute(
                "SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND granted = false AND pid <> pg_backend_pid()"
            ).fetchone()
            if row is not None and int(row[0]) > 0:
                blocked = True
                break
            if completed.is_set():
                break
            time.sleep(0.05)
        assert blocked, "授予未被用户级 advisory 锁阻塞（并发可产生两条 active 权益）"
        assert not completed.is_set()
    worker.join(timeout=10)
    assert completed.is_set() and "error" not in outcome, outcome
    rows = pg_conn.raw.execute(
        "SELECT is_active FROM customer_discounts WHERE user_id = %s", (user_id,)
    ).fetchall()
    assert len(rows) == 1
    assert bool(rows[0][0]) is True


# ===========================================================================
# C 组：迁移结构（列集 + CHECK 三支重写）
# ===========================================================================


def test_migration_structure_columns(pg_conn: BusinessConnection) -> None:
    rows = pg_conn.raw.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND ("
        "(table_name = 'recharge_packages') OR "
        "(table_name = 'recharge_orders' AND column_name IN "
        "('package_id', 'package_snapshot_json')) OR "
        "(table_name = 'customer_discounts' AND column_name = 'source_recharge_order_id')"
        ") ORDER BY table_name, column_name"
    ).fetchall()
    found = {(str(row[0]), str(row[1])) for row in rows}
    assert ("recharge_packages", "id") in found
    assert ("recharge_packages", "amount_fen") in found
    assert ("recharge_packages", "credits") in found
    assert ("recharge_packages", "discount_rate") in found
    assert ("recharge_packages", "discount_interfaces") in found
    assert ("recharge_packages", "sort_order") in found
    assert ("recharge_packages", "is_active") in found
    assert ("recharge_packages", "version") in found
    assert ("recharge_packages", "created_by_user_id") in found
    assert ("recharge_orders", "package_id") in found
    assert ("recharge_orders", "package_snapshot_json") in found
    assert ("customer_discounts", "source_recharge_order_id") in found


def test_migration_credit_check_accepts_package_bonus(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """充值 1998 元到账 2000 积分（赠送）必须通过 CHECK，而不是等于汇率换算值。"""
    user_id = _new_user("check-ok")
    _seed_customer(pg_dsn, user_id)
    package = create_package(
        pg_conn,
        name="充值1998",
        amount_fen=199_800,
        credits=2_000,
        discount_rate="0.9",
        discount_interfaces=["video_generation"],
        sort_order=0,
        is_active=True,
        actor_user_id=user_id,
    )
    # _seed_order 走独立连接：套餐行必须先提交，否则外键不可见。
    pg_conn.raw.commit()
    snapshot = build_package_snapshot(package)
    order_id = _seed_order(
        dsn=pg_dsn,
        user_id=user_id,
        order_no=_new_order_no("check-ok"),
        amount_fen=199_800,
        credits=2_000,
        credit_pricing_snapshot_json=json.dumps({"version": 1, "points_per_yuan": 100}),
        package_id=package.id,
        package_snapshot_json=json.dumps(snapshot),
    )
    row = pg_conn.raw.execute(
        "SELECT credits, package_id FROM recharge_orders WHERE id = %s", (order_id,)
    ).fetchone()
    assert row is not None and int(row[0]) == 2_000 and str(row[1]) == package.id


def test_migration_credit_check_rejects_package_mismatch(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """订单 credits 与套餐快照不符必须被 CHECK 拒绝（防止赠送口径被改写）。"""
    user_id = _new_user("check-bad")
    _seed_customer(pg_dsn, user_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        _seed_order(
            dsn=pg_dsn,
            user_id=user_id,
            order_no=_new_order_no("check-bad"),
            amount_fen=199_800,
            credits=1_998,
            credit_pricing_snapshot_json=json.dumps({"version": 1, "points_per_yuan": 100}),
            package_id=None,
            package_snapshot_json=json.dumps(
                {"package_id": "pkg-x", "amount_fen": 199_800, "credits": 2_000}
            ),
        )


def test_migration_credit_check_still_enforces_exchange_rate(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """无套餐订单仍受汇率换算 CHECK 约束——重写不得放宽既有口径。"""
    user_id = _new_user("check-rate")
    _seed_customer(pg_dsn, user_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        _seed_order(
            dsn=pg_dsn,
            user_id=user_id,
            order_no=_new_order_no("check-rate"),
            amount_fen=199_800,
            credits=1_999,
            credit_pricing_snapshot_json=json.dumps({"version": 1, "points_per_yuan": 100}),
        )


def test_migration_amount_constraints_exempt_package_orders(
    pg_conn: BusinessConnection,
) -> None:
    """amount_price / amount_step 各加套餐豁免支，且保留原有口径分支."""
    definitions = {
        str(row[0]): str(row[1])
        for row in pg_conn.raw.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'recharge_orders'::regclass AND contype = 'c' "
            "AND conname IN ('ck_recharge_orders_amount_price', "
            "'ck_recharge_orders_amount_step')"
        ).fetchall()
    }
    assert set(definitions) == {
        "ck_recharge_orders_amount_price",
        "ck_recharge_orders_amount_step",
    }
    price = definitions["ck_recharge_orders_amount_price"]
    step = definitions["ck_recharge_orders_amount_step"]
    assert "package_snapshot_json IS NOT NULL" in price
    assert "package_snapshot_json IS NOT NULL" in step
    # 无套餐金额仍逐字沿用原约束（豁免不得整体删除口径）。
    assert "credit_pricing_snapshot_json IS NOT NULL" in price
    assert "charged_unit_price_fen_snapshot" in price
    assert "recharge_step_fen_snapshot" in step


def test_migration_amount_step_exempts_packages_but_not_custom_amounts(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """1998 元套餐对 10 元步长不整除可入账；同金额自定义单仍被步长约束拒绝."""
    user_id = _new_user("step-pkg")
    _seed_customer(pg_dsn, user_id)
    package = create_package(
        pg_conn,
        name="充值1998",
        amount_fen=199_800,
        credits=2_000,
        discount_rate=None,
        is_active=True,
        actor_user_id=user_id,
    )
    # _seed_order 走独立连接：套餐行必须先提交，否则外键不可见。
    pg_conn.raw.commit()
    order_id = _seed_order(
        dsn=pg_dsn,
        user_id=user_id,
        order_no=_new_order_no("step-pkg"),
        amount_fen=199_800,
        credits=2_000,
        package_id=package.id,
        package_snapshot_json=json.dumps(build_package_snapshot(package), ensure_ascii=False),
        step_fen=1_000,
    )
    assert order_id
    # 无套餐 + 汇率快照：199800 对 1000 步长不整除 ⇒ amount_step 拒绝。
    with pytest.raises(psycopg.errors.CheckViolation) as rejected:
        _seed_order(
            dsn=pg_dsn,
            user_id=user_id,
            order_no=_new_order_no("step-custom"),
            amount_fen=199_800,
            credits=1_998,
            credit_pricing_snapshot_json=json.dumps({"version": 1, "points_per_yuan": 100}),
            step_fen=1_000,
        )
    assert "ck_recharge_orders_amount_step" in str(rejected.value)


# ===========================================================================
# D 组：路由层（管理端 CRUD / 客户只读 / 套餐下单冻结快照）
# ===========================================================================


@pytest.fixture()
def package_client(client, route_state, monkeypatch):
    """专用库路由基座 + 管理端与套餐路由（沿 test_customer_pricing 先例）."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    from app.admin_auth_routes import router as admin_router
    from app.recharge_package_routes import router as package_router

    client.app.include_router(admin_router)
    client.app.include_router(package_router)
    with psycopg.connect(route_state) as raw:
        # 清空汇率配置：套餐单不得依赖 customer_credit_pricing。
        raw.execute("UPDATE customer_credit_pricing SET version = 0, config_json = NULL")
        raw.execute("INSERT INTO runtime_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    return client


def _admin_login(client, dsn) -> dict[str, str]:
    """建管理员并登录，返回写路由所需的 CSRF 头（镜像 test_customer_pricing）."""
    from app.admin_auth_routes import hash_admin_password

    uid = f"{RPKG}admin-{uuid4().hex[:10]}"
    with psycopg.connect(dsn) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, "
            "'rpkg_admin', 'RPKG Admin', 'admin')",
            (uid,),
        )
        raw.execute(
            "INSERT INTO admin_password_credentials (user_id, password_hash, "
            "credential_version, password_changed_at) VALUES (%s, %s, 1, "
            "CURRENT_TIMESTAMP)",
            (uid, hash_admin_password("RPKG-test-2026!")),
        )
    response = client.post(
        "/api/control/admin/session/password",
        json={"username": "rpkg_admin", "password": "RPKG-test-2026!"},
    )
    assert response.status_code == 201, response.text
    return {"X-Admin-CSRF": response.json()["csrf_token"]}


def test_admin_recharge_package_crud_route(package_client, route_state) -> None:
    """管理端：创建 → 幂等重放 → 无会话 403 → 列表 → 乐观锁 409 → 审计."""
    from app.admin_write_contract import REPLAY_HEADER

    client = package_client
    admin = _admin_login(client, route_state)
    path = "/api/control/settings/recharge-packages"
    draft = {
        "name": "充值1998",
        "amount_fen": 199_800,
        "credits": 2_000,
        "discount_rate": "0.9",
        "discount_interfaces": ["video_generation"],
        "sort_order": 0,
        "is_active": True,
        "reason": "配置 1998 档位（视频生成 9 折）",
        "confirm": True,
    }
    write_headers = {**admin, "Idempotency-Key": str(uuid4())}
    created = client.post(path, headers=write_headers, json=draft)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"].startswith(RPKG)
    assert body["discount_rate"] == "0.9000"
    assert body["discount_interfaces"] == ["video_generation"]
    assert body["version"] == 0 and body["is_active"] is True
    replayed = client.post(path, headers=write_headers, json=draft)
    assert replayed.status_code == 201 and replayed.json() == body
    assert replayed.headers[REPLAY_HEADER] == "true"
    unauthorized = client.post(path, headers={"Idempotency-Key": str(uuid4())}, json=draft)
    assert unauthorized.status_code == 403
    listing = client.get(path, headers=admin)
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [body["id"]]
    update = {**draft, "name": "充值1998（9折）", "expected_version": 0}
    updated = client.put(
        f"{path}/{body['id']}",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json=update,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 1
    assert updated.json()["name"] == "充值1998（9折）"
    stale = client.put(
        f"{path}/{body['id']}",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json=update,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "RECHARGE_PACKAGE_VERSION_CONFLICT"
    with psycopg.connect(route_state) as raw:
        actions = sorted(
            str(row[0])
            for row in raw.execute(
                "SELECT action FROM audit_logs WHERE entity_type = 'recharge_package'"
            ).fetchall()
        )
    assert actions == ["recharge_package.create", "recharge_package.update"]


def test_customer_recharge_packages_route_lists_active_only(package_client, route_state) -> None:
    """客户侧：只读启用档位、停用行不可见、响应 no-store."""
    client = package_client
    headers, user_id = account(client)
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        active = create_package(
            conn,
            name="充值500",
            amount_fen=50_000,
            credits=500,
            discount_rate=None,
            sort_order=0,
            is_active=True,
            actor_user_id=user_id,
        )
        disabled = create_package(
            conn,
            name="下架档位",
            amount_fen=100_000,
            credits=1_000,
            discount_rate="0.9",
            discount_interfaces=["video_generation"],
            sort_order=1,
            is_active=False,
            actor_user_id=user_id,
        )
    listing = client.get("/api/customer/recharge-packages", headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.headers["cache-control"] == "no-store"
    items = listing.json()["items"]
    assert [item["id"] for item in items] == [active.id]
    assert items[0]["discount_rate"] is None
    assert items[0]["amount_fen"] == 50_000 and items[0]["credits"] == 500
    assert disabled.id not in listing.text


def test_customer_package_order_freezes_snapshot_and_rejects_conflicts(
    package_client, route_state
) -> None:
    """套餐下单：credits/权益冻结快照；金额不符/停用 422；同 key 换套餐 409."""
    from types import SimpleNamespace

    from app.payment_provider import DeploymentConfig, MerchantConfig, PaymentFormResult
    from app.recharge_routes import REPLAY_HEADER, get_zpay_provider

    client = package_client
    headers, user_id = account(client)
    provider = SimpleNamespace(
        name="zpay",
        load_merchant_config=lambda conn: MerchantConfig("zpay", {}, ("alipay",)),
        load_deployment_config=lambda: DeploymentConfig(
            "https://example.test/notify", "https://example.test/return"
        ),
        create_payment_form=lambda **kwargs: PaymentFormResult(
            "https://example.test/pay", "POST", {}
        ),
    )
    client.app.dependency_overrides[get_zpay_provider] = lambda: provider
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        first = create_package(
            conn,
            name="充值1998",
            amount_fen=199_800,
            credits=2_000,
            discount_rate="0.9",
            discount_interfaces=["video_generation"],
            sort_order=0,
            is_active=True,
            actor_user_id=user_id,
        )
        second = create_package(
            conn,
            name="充值2998",
            amount_fen=299_800,
            credits=3_100,
            discount_rate=None,
            sort_order=1,
            is_active=True,
            actor_user_id=user_id,
        )
    path = "/api/customer/recharge-orders"
    key = str(uuid4())
    created = client.post(
        path,
        headers={**headers, "Idempotency-Key": key},
        json={"amount_fen": 199_800, "package_id": first.id},
    )
    assert created.status_code == 201, created.text
    assert created.json()["credits"] == 2_000
    with psycopg.connect(route_state) as raw:
        row = raw.execute(
            "SELECT amount_fen, credits, package_id, package_snapshot_json, "
            "credit_pricing_snapshot_json FROM recharge_orders WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        assert row is not None
        # 套餐单不依赖汇率配置：无汇率快照同样可下单（迁移第三支已解耦）。
        assert (row[0], row[1], row[2], row[4]) == (199_800, 2_000, first.id, None)
        snapshot = json.loads(str(row[3]))
        assert snapshot == {
            "package_id": first.id,
            "name": "充值1998",
            "amount_fen": 199_800,
            "credits": 2_000,
            "discount_rate": "0.9000",
            "discount_interfaces": ["video_generation"],
        }
    replayed = client.post(
        path,
        headers={**headers, "Idempotency-Key": key},
        json={"amount_fen": 199_800, "package_id": first.id},
    )
    assert replayed.status_code == 201 and replayed.json() == created.json()
    assert replayed.headers[REPLAY_HEADER] == "true"
    mismatch = client.post(
        path,
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"amount_fen": 199_799, "package_id": first.id},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "RECHARGE_PACKAGE_AMOUNT_MISMATCH"
    swapped = client.post(
        path,
        headers={**headers, "Idempotency-Key": key},
        json={"amount_fen": 299_800, "package_id": second.id},
    )
    assert swapped.status_code == 409
    assert swapped.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE recharge_packages SET is_active = false WHERE id = %s", (first.id,))
    hidden = client.post(
        path,
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"amount_fen": 199_800, "package_id": first.id},
    )
    assert hidden.status_code == 422
    assert hidden.json()["detail"]["code"] == "RECHARGE_PACKAGE_NOT_FOUND"
    with psycopg.connect(route_state) as raw:
        orders = raw.execute(
            "SELECT COUNT(*) FROM recharge_orders WHERE user_id = %s", (user_id,)
        ).fetchone()
        assert orders is not None and int(orders[0]) == 1


def test_customer_package_order_below_effective_minimum_is_rejected(
    package_client, route_state
) -> None:
    """套餐金额低于生效起充额：422 且不落订单（H-1，评审修复）.

    管理端可配置任意档位（含低于起充额的 50 元档）；客户下单必须被路由层
    拒绝为 ``RECHARGE_PACKAGE_BELOW_MINIMUM``，而不是撞数据库 CHECK 变 500。
    """
    from types import SimpleNamespace

    from app.payment_provider import DeploymentConfig, MerchantConfig, PaymentFormResult
    from app.recharge_routes import get_zpay_provider

    client = package_client
    headers, user_id = account(client)
    provider = SimpleNamespace(
        name="zpay",
        load_merchant_config=lambda conn: MerchantConfig("zpay", {}, ("alipay",)),
        load_deployment_config=lambda: DeploymentConfig(
            "https://example.test/notify", "https://example.test/return"
        ),
        create_payment_form=lambda **kwargs: PaymentFormResult(
            "https://example.test/pay", "POST", {}
        ),
    )
    client.app.dependency_overrides[get_zpay_provider] = lambda: provider
    with psycopg.connect(route_state) as raw:
        low = create_package(
            BusinessConnection.postgres(raw),
            name="五十元档",
            amount_fen=5_000,
            credits=55,
            discount_rate=None,
            discount_interfaces=[],
            sort_order=0,
            is_active=True,
            actor_user_id=user_id,
        )
    response = client.post(
        "/api/customer/recharge-orders",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"amount_fen": 5_000, "package_id": low.id},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "RECHARGE_PACKAGE_BELOW_MINIMUM"
    with psycopg.connect(route_state) as raw:
        orders = raw.execute(
            "SELECT COUNT(*) FROM recharge_orders WHERE user_id = %s", (user_id,)
        ).fetchone()
        assert orders is not None and int(orders[0]) == 0
