"""折扣接线测试（CW-075 折扣模型 → 消耗侧计费/报价/充值结算）.

分组：
- A 组：纯函数单元（接口键目录 + 折扣合并取更优）——无 PG，本地全绿。
- B 组：接线实证（retail_snapshot 折后 / accept_operation 冻结快照 + 账目落 discount_rate /
  冻结后改配置不重算 / 报价折后 / 充值结算授予套餐权益）——共享库 pg_dsn。

PG 组沿 test_cw075_discount_model 先例：共享库 fixture、pg_test_kit 硬门、模块级套件锁，
只按 ``dwire-`` 前缀 DELETE；billing_tariffs 行在套件前后原样快照/还原（绝不改共享库的
长期配置）。
"""

from __future__ import annotations

import json
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

from app.billing_catalog import (
    INTERFACE_KEYS,
    SERVICE_INTERFACE,
    SERVICES,
    calculate_credits,
    interface_for_service,
    merge_customer_discount,
    retail_snapshot,
)
from app.db_pg import close_pg_pool
from app.db_portable import BusinessConnection
from app.discount_service import validate_discount_rate
from app.recharge_packages import (
    build_package_snapshot,
    create_package,
    grant_package_discount,
)
from app.usage_billing import accept_operation, finish_operation
from app.zpay_payments import confirm_recharge_payment

DB_NAME_SHARED = "customer_v3_test"
DWIRE = "dwire-"
TARIFF_SERVICE = "video_768p"
# accept_operation 测试用科目：非 video_*/oral——不触发 wallet_transactions.task_id
# 对 generation_tasks 的外键（真实链路里该 FK 由生成任务占位，本套件不建生成任务）。
OPERATION_SERVICE = "viral_data"
TARIFF_SERVICES = (TARIFF_SERVICE, OPERATION_SERVICE)


def _new_user(tag: str) -> str:
    return f"{DWIRE}{tag}-{uuid4().hex[:10]}"


def _clean_dwire(dsn: str) -> None:
    """删除全部 dwire- 作用域行（子表先于父表满足外键；只 DELETE）。

    ``billing_credit_lots.id`` 引用 ``wallet_transactions.id``，必须先删 lot
    再删流水行；本套件经 ``create_package`` 建的套餐是 ``rpkg-`` 前缀，订单侧
    按 user 前缀或 ``package_id LIKE 'rpkg-%'`` 兜底后一并回收套餐行。
    """
    like = DWIRE + "%"
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
            (like, "rpkg-%"),
        )
        conn.execute("DELETE FROM recharge_packages WHERE id LIKE %s", ("rpkg-%",))
        conn.execute("DELETE FROM users WHERE id LIKE %s", (like,))


def _seed_customer(dsn: str, user_id: str, *, credits: int = 100_000) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES (%s, %s, %s, 'customer') ON CONFLICT (id) DO NOTHING",
            (user_id, user_id, f"DWIRE {user_id}"),
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES (%s, %s, 0) ON CONFLICT (user_id) DO UPDATE SET "
            "available_credits = EXCLUDED.available_credits, reserved_credits = 0",
            (user_id, credits),
        )


def _read_tariff_rows(dsn: str) -> dict[str, tuple[object, ...] | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return {
            service: conn.execute(
                "SELECT enabled, unit_credits, unit_rounding, version FROM billing_tariffs "
                "WHERE service = %s",
                (service,),
            ).fetchone()
            for service in TARIFF_SERVICES
        }


def _restore_tariff_rows(dsn: str, previous: dict[str, tuple[object, ...] | None]) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for service, row in previous.items():
            if row is None:
                conn.execute("DELETE FROM billing_tariffs WHERE service = %s", (service,))
                continue
            conn.execute(
                "UPDATE billing_tariffs SET enabled = %s, unit_credits = %s, "
                "unit_rounding = %s, version = %s WHERE service = %s",
                (row[0], row[1], row[2], row[3], service),
            )


def _seed_enabled_tariffs(dsn: str, *, unit_credits: int = 100) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for service in TARIFF_SERVICES:
            conn.execute(
                "INSERT INTO billing_tariffs (service, enabled, unit_credits, unit_rounding) "
                "VALUES (%s, true, %s, 'ceil') ON CONFLICT (service) DO UPDATE SET "
                "enabled = true, unit_credits = EXCLUDED.unit_credits, unit_rounding = 'ceil'",
                (service, unit_credits),
            )


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    """共享库 DSN + 迁移到 head + 套件锁 + 费率行原样还原（沿 CW-031/CW-075）。"""
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert dsn.rsplit("/", 1)[1] == DB_NAME_SHARED, "本套件只准用共享库 customer_v3_test"
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        previous = _read_tariff_rows(dsn)
        _clean_dwire(dsn)
        _seed_enabled_tariffs(dsn)
        try:
            yield dsn
        finally:
            _clean_dwire(dsn)
            _restore_tariff_rows(dsn, previous)
            close_pg_pool()


@pytest.fixture()
def pg_conn(pg_dsn: str) -> Iterator[BusinessConnection]:
    with psycopg.connect(pg_dsn) as raw:
        yield BusinessConnection.postgres(raw)


def _seed_package_with_discount(
    pg_conn: BusinessConnection,
    *,
    user_id: str,
    rate: str = "0.9",
    interfaces: list[str] | None = None,
) -> str:
    """授予一笔套餐权益（走真实服务路径：建套餐 → 落订单 → 授予）。"""
    package = create_package(
        pg_conn,
        name="测试套餐",
        amount_fen=199_800,
        credits=2_000,
        discount_rate=rate,
        discount_interfaces=list(interfaces or []),
        sort_order=0,
        is_active=True,
        actor_user_id=None,
    )
    order_id = f"{DWIRE}order-{uuid4().hex[:12]}"
    pg_conn.raw.execute(
        "INSERT INTO recharge_orders ("
        "id, user_id, merchant_order_no, provider, provider_trade_no, channel, status, "
        "pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
        "credit_pricing_snapshot_json, package_id, package_snapshot_json) "
        "VALUES (%s, %s, %s, 'zpay', NULL, 'alipay', 'PENDING', 'CUSTOMER_STANDARD', "
        "100, 100, 1, 1, 199800, 2000, %s, %s, %s)",
        (
            order_id,
            user_id,
            f"{DWIRE}mch-{uuid4().hex[:10]}",
            json.dumps({"version": 1, "points_per_yuan": 100}),
            package.id,
            json.dumps(build_package_snapshot(package)),
        ),
    )
    granted = grant_package_discount(
        pg_conn,
        user_id=user_id,
        source_recharge_order_id=order_id,
        discount_rate=package.discount_rate,
        discount_interfaces=package.discount_interfaces,
    )
    assert granted is not None
    return granted


# ===========================================================================
# A 组：纯单元（无 PG）——接口键目录 + 折扣合并
# ===========================================================================


def test_interface_for_service_maps_video_resolutions() -> None:
    assert interface_for_service("video_768p") == "video_generation"
    assert interface_for_service("video_2k") == "video_generation"
    assert interface_for_service("oral") == "oral"


def test_interface_directory_covers_every_customer_chargeable_service() -> None:
    for name, service in SERVICES.items():
        key = interface_for_service(name)
        assert key
        if service.customer_charge_allowed:
            assert key in INTERFACE_KEYS, f"{name} 的接口键 {key} 不在可配置目录中"


def test_interface_for_service_rejects_unknown_service() -> None:
    with pytest.raises(ValueError, match="未知计费科目"):
        interface_for_service("not_a_service")


def test_service_interface_covers_every_service() -> None:
    assert set(SERVICE_INTERFACE) == set(SERVICES)


def test_merge_customer_discount_without_rate_keeps_platform() -> None:
    assert merge_customer_discount(10_000, None) == 10_000
    assert merge_customer_discount(9_000, None) == 9_000


def test_merge_customer_discount_package_stronger_wins() -> None:
    assert merge_customer_discount(10_000, Decimal("0.9000")) == 9_000
    assert merge_customer_discount(10_000, Decimal("0.8500")) == 8_500


def test_merge_customer_discount_platform_stronger_wins() -> None:
    """平台侧已有更强折扣时不能被套餐的弱折扣顶掉（取更优 = bp 更小）。"""
    assert merge_customer_discount(8_000, Decimal("0.9500")) == 8_000


def test_merge_customer_discount_rejects_invalid_rate() -> None:
    with pytest.raises(ValueError):
        merge_customer_discount(10_000, Decimal("1.5"))


def test_validate_discount_rate_accepts_four_decimal_rate() -> None:
    assert validate_discount_rate(Decimal("0.8500")) == Decimal("0.8500")


# ===========================================================================
# B 组：接线实证（共享库 pg_dsn，CI-Linux-only）
# ===========================================================================


def test_retail_snapshot_without_user_keeps_platform_price(
    pg_conn: BusinessConnection,
) -> None:
    snapshot = retail_snapshot(pg_conn, TARIFF_SERVICE, 8)
    assert snapshot["discount_basis_points"] == 10_000
    assert snapshot["discount_rate"] is None
    assert snapshot["discount_source"] is None
    assert snapshot["credits"] == 800


def test_retail_snapshot_applies_package_discount(pg_dsn: str, pg_conn: BusinessConnection) -> None:
    user_id = _new_user("snap")
    _seed_customer(pg_dsn, user_id)
    _seed_package_with_discount(pg_conn, user_id=user_id, rate="0.9")
    snapshot = retail_snapshot(pg_conn, TARIFF_SERVICE, 8, user_id=user_id)
    assert snapshot["discount_basis_points"] == 9_000
    assert snapshot["discount_rate"] == "0.9000"
    assert snapshot["discount_source"] == "recharge_package"
    assert snapshot["credits"] == 720


def test_retail_snapshot_ignores_discount_for_other_interface(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    user_id = _new_user("iface")
    _seed_customer(pg_dsn, user_id)
    _seed_package_with_discount(pg_conn, user_id=user_id, rate="0.9", interfaces=["oral"])
    video = retail_snapshot(pg_conn, TARIFF_SERVICE, 8, user_id=user_id)
    oral = retail_snapshot(pg_conn, "oral", 8, user_id=user_id)
    assert video["credits"] == 800 and video["discount_rate"] is None
    assert oral["discount_basis_points"] == 9_000


def test_accept_operation_freezes_discount_and_ledger(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """预留按折后价冻结，账目落 discount_rate；改配置后结算不重算（R-D）。"""
    user_id = _new_user("accept")
    _seed_customer(pg_dsn, user_id)
    discount_id = _seed_package_with_discount(pg_conn, user_id=user_id, rate="0.9")
    operation_id = accept_operation(
        pg_conn,
        user_id=user_id,
        service=OPERATION_SERVICE,
        source_id=f"{DWIRE}src-{uuid4().hex[:8]}",
        units=8,
    )
    operation = pg_conn.raw.execute(
        "SELECT pricing_snapshot_json, reserved_credits FROM billing_operations WHERE id = %s",
        (operation_id,),
    ).fetchone()
    assert operation is not None and int(operation[1]) == 720
    snapshot = json.loads(str(operation[0]))
    assert snapshot["discount_basis_points"] == 9_000
    assert snapshot["discount_rate"] == "0.9000"
    ledger = pg_conn.raw.execute(
        "SELECT discount_rate FROM wallet_transactions WHERE billing_operation_id = %s "
        "AND type = 'RESERVE'",
        (operation_id,),
    ).fetchone()
    assert ledger is not None and Decimal(str(ledger[0])) == Decimal("0.9000")

    # 折扣失效（模拟管理员改配置）后按冻结快照结算，仍扣折后价。
    pg_conn.raw.execute(
        "UPDATE customer_discounts SET is_active = false WHERE id = %s", (discount_id,)
    )
    charged = finish_operation(pg_conn, operation_id=operation_id, units=8, succeeded=True)
    assert charged == 720
    settle = pg_conn.raw.execute(
        "SELECT discount_rate FROM wallet_transactions WHERE billing_operation_id = %s "
        "AND type = 'SETTLE'",
        (operation_id,),
    ).fetchone()
    assert settle is not None and Decimal(str(settle[0])) == Decimal("0.9000")


def test_accept_operation_without_discount_charges_full_price(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    user_id = _new_user("full")
    _seed_customer(pg_dsn, user_id)
    operation_id = accept_operation(
        pg_conn,
        user_id=user_id,
        service=OPERATION_SERVICE,
        source_id=f"{DWIRE}src-{uuid4().hex[:8]}",
        units=8,
    )
    row = pg_conn.raw.execute(
        "SELECT reserved_credits FROM billing_operations WHERE id = %s", (operation_id,)
    ).fetchone()
    assert row is not None and int(row[0]) == 800
    ledger = pg_conn.raw.execute(
        "SELECT discount_rate FROM wallet_transactions WHERE billing_operation_id = %s",
        (operation_id,),
    ).fetchone()
    assert ledger is not None and ledger[0] is None


def test_discount_uses_master_wallet_owner_for_sub_account(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """子账号消费走母账号钱包，也走母账号的套餐权益（T2.10）。"""
    master = _new_user("master")
    _seed_customer(pg_dsn, master)
    child = _new_user("child")
    _seed_customer(pg_dsn, child, credits=0)
    pg_conn.raw.execute(
        "UPDATE users SET account_type = 'SUB', parent_user_id = %s WHERE id = %s",
        (master, child),
    )
    _seed_package_with_discount(pg_conn, user_id=master, rate="0.9")
    operation_id = accept_operation(
        pg_conn,
        user_id=child,
        service=OPERATION_SERVICE,
        source_id=f"{DWIRE}src-{uuid4().hex[:8]}",
        units=8,
    )
    operation = pg_conn.raw.execute(
        "SELECT reserved_credits FROM billing_operations WHERE id = %s", (operation_id,)
    ).fetchone()
    assert operation is not None and int(operation[0]) == 720


def test_generation_price_quote_is_discounted(pg_dsn: str, pg_conn: BusinessConnection) -> None:
    from app.generation import generation_price_quote

    user_id = _new_user("quote")
    _seed_customer(pg_dsn, user_id)
    _seed_package_with_discount(pg_conn, user_id=user_id, rate="0.9")
    quote = generation_price_quote(
        pg_conn, resolution="768P", duration_seconds=8, quantity=2, user_id=user_id
    )
    assert quote.estimated_credits == 1_440
    assert quote.discount_rate == Decimal("0.9000")
    assert quote.discount_source == "recharge_package"
    plain = generation_price_quote(pg_conn, resolution="768P", duration_seconds=8, quantity=2)
    assert plain.discount_rate is None


def test_confirm_recharge_grants_package_discount(pg_dsn: str, pg_conn: BusinessConnection) -> None:
    """充值结算成功即授予套餐权益（同一事务）；重复结算不重复授予。"""
    user_id = _new_user("settle")
    _seed_customer(pg_dsn, user_id, credits=0)
    package = create_package(
        pg_conn,
        name="结算套餐",
        amount_fen=199_800,
        credits=2_000,
        discount_rate="0.9",
        discount_interfaces=["video_generation"],
        sort_order=0,
        is_active=True,
        actor_user_id=None,
    )
    order_no = f"{DWIRE}mch-{uuid4().hex[:10]}"
    order_id = f"{DWIRE}order-{uuid4().hex[:12]}"
    pg_conn.raw.execute(
        "INSERT INTO recharge_orders ("
        "id, user_id, merchant_order_no, provider, provider_trade_no, channel, status, "
        "pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
        "credit_pricing_snapshot_json, package_id, package_snapshot_json) "
        "VALUES (%s, %s, %s, 'zpay', NULL, 'alipay', 'PENDING', 'CUSTOMER_STANDARD', "
        "100, 100, 1, 1, 199800, 2000, %s, %s, %s)",
        (
            order_id,
            user_id,
            order_no,
            json.dumps({"version": 1, "points_per_yuan": 100}),
            package.id,
            json.dumps(build_package_snapshot(package)),
        ),
    )
    trade_no = f"{DWIRE}trade-{uuid4().hex[:8]}"
    confirm_recharge_payment(
        pg_conn,
        merchant_order_no=order_no,
        provider_trade_no=trade_no,
        amount_fen=199_800,
        channel="alipay",
        source_digest="dwire-local",
        allowed_channels=("alipay",),
    )
    wallet = pg_conn.raw.execute(
        "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
    ).fetchone()
    assert wallet is not None and int(wallet[0]) == 2_000
    snapshot = retail_snapshot(pg_conn, TARIFF_SERVICE, 8, user_id=user_id)
    assert snapshot["discount_basis_points"] == 9_000
    count = pg_conn.raw.execute(
        "SELECT COUNT(*) FROM customer_discounts WHERE source_recharge_order_id = %s",
        (order_id,),
    ).fetchone()
    assert count is not None and int(count[0]) == 1

    confirm_recharge_payment(
        pg_conn,
        merchant_order_no=order_no,
        provider_trade_no=trade_no,
        amount_fen=199_800,
        channel="alipay",
        source_digest="dwire-local-replay",
        allowed_channels=("alipay",),
    )
    wallet_after = pg_conn.raw.execute(
        "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
    ).fetchone()
    assert wallet_after is not None and int(wallet_after[0]) == 2_000
    recount = pg_conn.raw.execute(
        "SELECT COUNT(*) FROM customer_discounts WHERE source_recharge_order_id = %s",
        (order_id,),
    ).fetchone()
    assert recount is not None and int(recount[0]) == 1


def test_retail_snapshot_platform_stronger_discount_reports_effective_rate(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """平台口径强于客户权益：快照归因平台生效值，不虚报套餐来源（M-2，评审修复）.

    平台 8 折强于套餐 9.5 折：discount_basis_points 取 8000（原有行为），
    discount_rate 必须落生效值 "0.8000"（而非客户记录原值 "0.9500"），
    discount_source 不得归因为 recharge_package——账目与展示须与实收一致。
    配置改动在同一连接事务内还原（共享库长期配置不可残留）。
    """
    user_id = _new_user("pstrong")
    _seed_customer(pg_dsn, user_id)
    _seed_package_with_discount(pg_conn, user_id=user_id, rate="0.95")
    previous = pg_conn.raw.execute(
        "SELECT version, config_json FROM customer_credit_pricing WHERE id = 1"
    ).fetchone()
    assert previous is not None
    pg_conn.raw.execute(
        "UPDATE customer_credit_pricing SET config_json = %s WHERE id = 1",
        (json.dumps({"points_per_yuan": 100, "discount_basis_points": 8000}),),
    )
    try:
        snapshot = retail_snapshot(pg_conn, TARIFF_SERVICE, 8, user_id=user_id)
        assert snapshot["discount_basis_points"] == 8_000
        assert snapshot["discount_rate"] == "0.8000"
        assert snapshot["discount_source"] is None
        assert snapshot["credits"] == 640
    finally:
        pg_conn.raw.execute(
            "UPDATE customer_credit_pricing SET version = %s, config_json = %s WHERE id = 1",
            (previous[0], previous[1]),
        )


def test_confirm_recharge_invalid_snapshot_rate_keeps_wallet_credit(
    pg_dsn: str, pg_conn: BusinessConnection
) -> None:
    """快照折扣非法（漂移）时结算不得回滚已支付入账；权益跳过（L-1，评审修复）.

    资金入账必须落定（钱包 +credits），非法快照折扣只跳过权益授予，
    不阻断结算事务，也不产生 customer_discounts 行。
    """
    user_id = _new_user("drift")
    _seed_customer(pg_dsn, user_id, credits=0)
    package = create_package(
        pg_conn,
        name="漂移套餐",
        amount_fen=199_800,
        credits=2_000,
        discount_rate="0.9",
        discount_interfaces=[],
        sort_order=0,
        is_active=True,
        actor_user_id=None,
    )
    order_no = f"{DWIRE}mch-{uuid4().hex[:10]}"
    order_id = f"{DWIRE}order-{uuid4().hex[:12]}"
    drifted_snapshot = {
        "package_id": package.id,
        "name": package.name,
        "amount_fen": 199_800,
        "credits": 2_000,
        "discount_rate": "1.5",  # 漂移：越界折扣率
        "discount_interfaces": [],
    }
    pg_conn.raw.execute(
        "INSERT INTO recharge_orders ("
        "id, user_id, merchant_order_no, provider, provider_trade_no, channel, status, "
        "pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
        "credit_pricing_snapshot_json, package_id, package_snapshot_json) "
        "VALUES (%s, %s, %s, 'zpay', NULL, 'alipay', 'PENDING', 'CUSTOMER_STANDARD', "
        "100, 100, 1, 1, 199800, 2000, %s, %s, %s)",
        (
            order_id,
            user_id,
            order_no,
            json.dumps({"version": 1, "points_per_yuan": 100}),
            package.id,
            json.dumps(drifted_snapshot),
        ),
    )
    confirm_recharge_payment(
        pg_conn,
        merchant_order_no=order_no,
        provider_trade_no=f"{DWIRE}trade-{uuid4().hex[:8]}",
        amount_fen=199_800,
        channel="alipay",
        source_digest="dwire-drift",
        allowed_channels=("alipay",),
    )
    wallet = pg_conn.raw.execute(
        "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
    ).fetchone()
    assert wallet is not None and int(wallet[0]) == 2_000
    count = pg_conn.raw.execute(
        "SELECT COUNT(*) FROM customer_discounts WHERE source_recharge_order_id = %s",
        (order_id,),
    ).fetchone()
    assert count is not None and int(count[0]) == 0


def test_credits_from_snapshot_keeps_frozen_discount() -> None:
    """快照回放契约：settle 用冻结的 discount_basis_points 重算，不受当前配置影响。"""
    from app.billing_catalog import credits_from_snapshot

    snapshot = {
        "service": TARIFF_SERVICE,
        "unit": "second",
        "enabled": True,
        "unit_credits": "100",
        "unit_rounding": "ceil",
        "discount_basis_points": 8_500,
        "consumption_rounding": "ceil",
    }
    assert credits_from_snapshot(snapshot, 8) == 680
    assert calculate_credits(None, 8, discount_basis_points=8_500) == 0
