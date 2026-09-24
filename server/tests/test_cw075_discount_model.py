"""CW-075 折扣数据模型测试 — 纯单元 + PG service 读取 + 迁移结构矩阵.

分组：
- A 组：纯函数单元（折扣率校验 / 折后价 ceil 计算）——无 PG，本地全绿。
- B 组：discount_service PG 读取（活跃折扣 / 优先级互斥 / 有效期 / 接口范围）——共享库 pg_dsn。
- C 组：迁移 090 结构（customer_discounts 列集 + 快照列 + 形状 CHECK 未被收紧）——pg_dsn。
- D 组：HEAD 矩阵集成（083→090）在 test_cw056_supported_head_matrix.py、
  test_postgres_migrations.py。

PG 组沿 test_cw078_api_keys 先例：同步 psycopg + pg_test_kit 共享库 fixture（绝不新建库、
绝不 TRUNCATE，只按 cw075- 前缀 DELETE）。本地 Windows 因 pg_test_kit 模块级 ``import fcntl``
（POSIX-only）无法运行 B/C 组，以 CI-Linux 为准；A 组已用一次性探针在本地验证。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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

from app.db_pg import close_pg_pool
from app.discount_service import (
    DiscountRecord,
    calculate_discounted_price,
    get_active_discounts,
    get_best_discount,
    get_discount_rate,
    validate_discount_rate,
)

# ---------------------------------------------------------------------------
# 常量 + cw075- 前缀种子/清理（绝不新建 PG 库，绝不 TRUNCATE）
# ---------------------------------------------------------------------------

DB_NAME_SHARED = "customer_v3_test"
CW075 = "cw075-"


def _new_user(tag: str) -> str:
    return f"{CW075}{tag}-{uuid4().hex[:10]}"


def _clean_cw075(dsn: str) -> None:
    """删除全部 cw075- 作用域行（子表先于父表以满足外键；只 DELETE，绝不 TRUNCATE）。"""
    like = CW075 + "%"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM customer_discounts WHERE user_id LIKE %s", (like,))
        conn.execute("DELETE FROM users WHERE id LIKE %s", (like,))


def _seed_customer(dsn: str, user_id: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES (%s, %s, %s, 'customer') ON CONFLICT (id) DO NOTHING",
            (user_id, user_id, f"CW075 {user_id}"),
        )


def _seed_discount(
    dsn: str,
    user_id: str,
    *,
    rate: Decimal,
    priority: int,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    interfaces: list[str] | None = None,
    is_active: bool = True,
) -> str:
    """落一条 customer_discounts 配置行，返回其 id。valid_from 缺省取当前时刻。"""
    discount_id = f"{CW075}d-{uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_discounts "
            "(id, user_id, discount_rate, priority, valid_from, valid_until, "
            "applicable_interfaces, is_active) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                discount_id,
                user_id,
                rate,
                priority,
                valid_from or datetime.now(UTC),
                valid_until,
                json.dumps(list(interfaces or [])),
                is_active,
            ),
        )
    return discount_id


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    """共享库 DSN + CW-007 硬门 + 迁移到 head + 模块级套件锁（沿 CW-031/CW-078）。"""
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert dsn.rsplit("/", 1)[1] == DB_NAME_SHARED, "本套件只准用共享库 customer_v3_test"
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        _clean_cw075(dsn)
        try:
            yield dsn
        finally:
            _clean_cw075(dsn)
            close_pg_pool()


# ===========================================================================
# A 组：纯单元（无 PG）——折扣率校验 + 折后价 ceil 计算契约
# ===========================================================================


def test_validate_discount_rate_decimal_valid() -> None:
    rate = Decimal("0.8500")
    result = validate_discount_rate(rate)
    assert result == rate
    assert isinstance(result, Decimal)


def test_validate_discount_rate_float_valid() -> None:
    result = validate_discount_rate(0.85)
    assert result == Decimal("0.85")
    assert isinstance(result, Decimal)


def test_validate_discount_rate_str_valid() -> None:
    result = validate_discount_rate("0.8500")
    assert result == Decimal("0.8500")
    assert isinstance(result, Decimal)


def test_validate_discount_rate_boundary_min() -> None:
    assert validate_discount_rate(Decimal("0.0001")) == Decimal("0.0001")


def test_validate_discount_rate_boundary_max() -> None:
    assert validate_discount_rate(Decimal("1.0")) == Decimal("1.0")


def test_validate_discount_rate_zero_rejected() -> None:
    with pytest.raises(ValueError, match="discount_rate must be > 0"):
        validate_discount_rate(Decimal("0"))


def test_validate_discount_rate_negative_rejected() -> None:
    with pytest.raises(ValueError, match="discount_rate must be > 0"):
        validate_discount_rate(Decimal("-0.5"))


def test_validate_discount_rate_over_one_rejected() -> None:
    with pytest.raises(ValueError, match="discount_rate must be <= 1.0"):
        validate_discount_rate(Decimal("1.5"))


def test_validate_discount_rate_invalid_type_rejected() -> None:
    with pytest.raises(TypeError, match="discount_rate must be Decimal/float/str"):
        validate_discount_rate([0.85])


def test_calculate_discounted_price_no_discount() -> None:
    assert calculate_discounted_price(1000, None) == 1000


def test_calculate_discounted_price_85_percent() -> None:
    assert calculate_discounted_price(1000, Decimal("0.85")) == 850


def test_calculate_discounted_price_ceiling_rounding() -> None:
    # 1001 * 0.85 = 850.85 → ceil → 851（宁多收一分，不侵蚀平台）
    assert calculate_discounted_price(1001, Decimal("0.85")) == 851


def test_calculate_discounted_price_full_rate() -> None:
    assert calculate_discounted_price(1000, Decimal("1.0")) == 1000


def test_calculate_discounted_price_min_discount() -> None:
    # 10000 * 0.0001 = 1.0000 → ceil → 1
    assert calculate_discounted_price(10000, Decimal("0.0001")) == 1


def test_calculate_discounted_price_invalid_rate_rejected() -> None:
    with pytest.raises(ValueError, match="discount_rate must be > 0"):
        calculate_discounted_price(1000, Decimal("0"))


# ===========================================================================
# B 组：discount_service PG 读取（共享库 pg_dsn，CI-Linux-only）
# ===========================================================================


def test_get_active_discounts_empty(pg_dsn: str) -> None:
    """无折扣记录 → 空列表（走原价）。"""
    user_id = _new_user("empty")
    _seed_customer(pg_dsn, user_id)
    with psycopg.connect(pg_dsn) as conn:
        assert get_active_discounts(conn, user_id=user_id) == []
        assert get_discount_rate(conn, user_id=user_id) is None


def test_get_active_discounts_single(pg_dsn: str) -> None:
    """单个活跃折扣查询正确，DiscountRecord 字段落位。"""
    user_id = _new_user("single")
    _seed_customer(pg_dsn, user_id)
    _seed_discount(pg_dsn, user_id, rate=Decimal("0.8500"), priority=10)
    with psycopg.connect(pg_dsn) as conn:
        discounts = get_active_discounts(conn, user_id=user_id)
        rate = get_discount_rate(conn, user_id=user_id)
    assert len(discounts) == 1
    assert isinstance(discounts[0], DiscountRecord)
    assert discounts[0].discount_rate == Decimal("0.8500")
    assert discounts[0].priority == 10
    assert discounts[0].applicable_interfaces == ()  # 空数组 = 全部接口
    assert rate == Decimal("0.8500")


def test_get_best_discount_priority_ordering(pg_dsn: str) -> None:
    """多折扣源互斥取优先级最高（§2.3）：priority 大者胜出。"""
    user_id = _new_user("prio")
    _seed_customer(pg_dsn, user_id)
    _seed_discount(pg_dsn, user_id, rate=Decimal("0.9000"), priority=5)
    _seed_discount(pg_dsn, user_id, rate=Decimal("0.8000"), priority=10)
    with psycopg.connect(pg_dsn) as conn:
        best = get_best_discount(conn, user_id=user_id)
    assert best is not None
    assert best.priority == 10
    assert best.discount_rate == Decimal("0.8000")


def test_get_active_discounts_validity_period(pg_dsn: str) -> None:
    """有效期过滤：过期（valid_until <= now）与未来（valid_from > now）折扣都不返回。"""
    user_id = _new_user("valid")
    _seed_customer(pg_dsn, user_id)
    now = datetime.now(UTC)
    _seed_discount(
        pg_dsn,
        user_id,
        rate=Decimal("0.8500"),
        priority=10,
        valid_from=now - timedelta(days=10),
        valid_until=now,  # 到期时刻 = now，valid_until > now 为假 → 排除
    )
    _seed_discount(
        pg_dsn,
        user_id,
        rate=Decimal("0.9000"),
        priority=5,
        valid_from=now + timedelta(days=10),  # 尚未生效 → 排除
    )
    with psycopg.connect(pg_dsn) as conn:
        assert get_active_discounts(conn, user_id=user_id, at_time=now) == []


def test_get_active_discounts_inactive_excluded(pg_dsn: str) -> None:
    """软删（is_active=false）的折扣不返回。"""
    user_id = _new_user("inactive")
    _seed_customer(pg_dsn, user_id)
    _seed_discount(pg_dsn, user_id, rate=Decimal("0.8500"), priority=10, is_active=False)
    with psycopg.connect(pg_dsn) as conn:
        assert get_active_discounts(conn, user_id=user_id) == []


def test_get_active_discounts_interface_filtering(pg_dsn: str) -> None:
    """适用接口范围过滤：命中接口返回，未命中不返回。"""
    user_id = _new_user("iface")
    _seed_customer(pg_dsn, user_id)
    _seed_discount(
        pg_dsn,
        user_id,
        rate=Decimal("0.8500"),
        priority=10,
        interfaces=["video_generation", "oral"],
    )
    with psycopg.connect(pg_dsn) as conn:
        matched = get_active_discounts(conn, user_id=user_id, interface_key="video_generation")
        unmatched = get_active_discounts(conn, user_id=user_id, interface_key="image_generation")
    assert len(matched) == 1
    assert unmatched == []


def test_get_active_discounts_empty_interfaces_matches_all(pg_dsn: str) -> None:
    """空 applicable_interfaces（= 全部接口）对任意 interface_key 都命中。"""
    user_id = _new_user("alliface")
    _seed_customer(pg_dsn, user_id)
    _seed_discount(pg_dsn, user_id, rate=Decimal("0.7500"), priority=1, interfaces=[])
    with psycopg.connect(pg_dsn) as conn:
        matched = get_active_discounts(conn, user_id=user_id, interface_key="anything")
    assert len(matched) == 1


# ===========================================================================
# C 组：迁移 090 结构矩阵（共享库 pg_dsn，CI-Linux-only）
# ===========================================================================


def test_migration_090_creates_customer_discounts_columns(pg_dsn: str) -> None:
    """customer_discounts 列集齐（含 20260922T1200 套餐权益来源列）；
    applicable_interfaces 为 TEXT（维持 jsonb=0）。"""
    with psycopg.connect(pg_dsn) as conn:
        columns = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'customer_discounts'"
            ).fetchall()
        }
        user_index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'customer_discounts' "
            "AND indexname = 'idx_customer_discounts_user_active'"
        ).fetchone()
        priority_index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'customer_discounts' "
            "AND indexname = 'idx_customer_discounts_priority'"
        ).fetchone()
    assert set(columns) == {
        "id",
        "user_id",
        "discount_rate",
        "priority",
        "valid_from",
        "valid_until",
        "applicable_interfaces",
        "is_active",
        "created_at",
        "updated_at",
        "created_by_user_id",
        "source_recharge_order_id",
    }
    # R-A / cw056 §617：JSON 全存 TEXT，维持 head jsonb_columns=0 不变量。
    assert columns["applicable_interfaces"] == "text"
    assert columns["discount_rate"] == "numeric"
    assert user_index is not None
    assert priority_index is not None


def test_migration_090_adds_nullable_snapshot_columns(pg_dsn: str) -> None:
    """generation_tasks.discount_rate_snapshot + wallet_transactions.discount_rate 可空落地。"""
    with psycopg.connect(pg_dsn) as conn:
        snapshot = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'generation_tasks' AND column_name = 'discount_rate_snapshot'"
        ).fetchone()
        wallet = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'wallet_transactions' AND column_name = 'discount_rate'"
        ).fetchone()
    assert snapshot is not None and snapshot[0] == "numeric" and snapshot[1] == "YES"
    assert wallet is not None and wallet[0] == "numeric" and wallet[1] == "YES"


def test_migration_090_leaves_wallet_shape_check_compatible(pg_dsn: str) -> None:
    """形状 CHECK 兼容（§4）：090 不动 ck_wallet_transactions_shape，只加独立值域 CHECK。

    收紧 RESERVE/SETTLE 必填 discount_rate 归 CW-076——否则现有 reserve/finalize 流插入的
    NULL discount_rate 行会集体违反 CHECK。
    """
    with psycopg.connect(pg_dsn) as conn:
        shape = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_wallet_transactions_shape'"
        ).fetchone()
        rate_range = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_wallet_transactions_discount_rate_range'"
        ).fetchone()
    assert shape is not None
    # 形状 CHECK 未被 090 收紧：不提及 discount_rate，维持现有 reserve/finalize 流兼容。
    assert "discount_rate" not in str(shape[0])
    # 独立的 NULL 容忍值域 CHECK 落地。
    assert rate_range is not None
    assert "discount_rate" in str(rate_range[0])


def test_migration_20260912T1353_customer_discounts_rate_range_check(pg_dsn: str) -> None:
    """customer_discounts 值域 CHECK 拒绝 rate <= 0 或 > 1.0（DB 侧兜底）。"""
    user_id = _new_user("range")
    _seed_customer(pg_dsn, user_id)
    with psycopg.connect(pg_dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO customer_discounts "
            "(id, user_id, discount_rate, priority, applicable_interfaces, is_active) "
            "VALUES (%s, %s, %s, 0, '[]', true)",
            (f"{CW075}bad-{uuid4().hex[:8]}", user_id, Decimal("1.5")),
        )


# ===========================================================================
# D 组：HEAD 矩阵集成（083→090）
# 在 test_cw056_supported_head_matrix.py（HEAD_REVISION + HEAD_TABLE_NAMES 追加
# customer_discounts）与 test_postgres_migrations.py（HEAD_REVISION + downgrade 步数）中更新。
# counts/digest 是全局 post-linearization 不变量，随其余并行迁移由 integrator 一次性重算。
# ===========================================================================
