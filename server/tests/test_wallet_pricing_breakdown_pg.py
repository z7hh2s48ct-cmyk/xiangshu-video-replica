"""用户端扣费依据端到端（PG）：真实账本行经客户端点后仍只带零售侧依据。

P0-3 验收：客户能在流水里看到「单价 × 用量、折扣、取整、预扣上限、轮次、操作人」，
而成本侧（``unit_cost_fen`` 等）在任何一层都不出现在响应里——即便冻结快照本身
被写入了成本键，白名单投影也把它挡在客户边界之外。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811

from __future__ import annotations

import json

import psycopg
from test_customer_pricing import (  # noqa: F401
    account,
    client,
    pricing_client,
    registration_client,
    registration_dsn,
    route_state,
)

from app.billing_catalog import retail_snapshot
from app.db_portable import BusinessConnection
from app.usage_billing import accept_operation, finish_operation

# 95 折 + 向下取整：与默认值不同的组合，才能证明行上的数字真的来自冻结快照。
DISCOUNTED_CONFIG = json.dumps(
    {"points_per_yuan": 100, "discount_basis_points": 9500, "consumption_rounding": "floor"}
)

# 成本侧字段：既不能出现在 pricing 里，也不能出现在行顶层。
COST_FIELDS = {"unit_cost_fen", "revenue_fen", "funding_json", "amount_fen", "lot_id"}

PRICING_FIELDS = {
    "service",
    "version",
    "unit",
    "units",
    "unit_credits",
    "unit_rounding",
    "discount_basis_points",
    "consumption_rounding",
    "credits",
    "enabled",
    "free_reason",
}


def test_ledger_rows_carry_the_frozen_pricing_basis(pricing_client, route_state):
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        raw.execute("UPDATE wallets SET available_credits=500 WHERE user_id=%s", (uid,))
        raw.execute(
            "UPDATE customer_credit_pricing SET config_json=%s WHERE id=1", (DISCOUNTED_CONFIG,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen,"
            "unit_rounding,version) VALUES('asr',true,2,7,'ceil',3)"
        )
        operation_id = accept_operation(
            conn, user_id=uid, service="asr", source_id="pricing-basis", units=3
        )
        finish_operation(conn, operation_id=operation_id, units=2, succeeded=True)

    response = pricing_client.get("/api/customer/wallet/transactions", headers=customer)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert {row["type"] for row in rows} == {"RESERVE", "SETTLE", "RELEASE"}
    assert "unit_cost_fen" not in response.text

    # 3 秒 × 2 积分 × 95% = 5.7；预扣按向上取整前的原值算，消费取整配置为向下 → 5。
    # unit_credits 是 numeric 列的原样文本（"2" 落库后读回即 "2.000000"），
    # 客户界面负责去尾零——接口保留冻结原值才可核对。
    expected_pricing = {
        "service": "asr",
        "version": 3,
        "unit": "second",
        "units": "3.000000",
        "unit_credits": "2.000000",
        "unit_rounding": "ceil",
        "discount_basis_points": 9500,
        "consumption_rounding": "floor",
        "credits": 5,
        "enabled": True,
        "free_reason": None,
    }
    for row in rows:
        # 同一笔业务的三行共享同一份冻结快照：客户核对时不依赖行序。
        assert set(row["pricing"]) == PRICING_FIELDS
        assert row["pricing"] == expected_pricing
        assert not set(row) & COST_FIELDS
        assert row["credit_price_version"] == 3
        assert row["billing_round"] == 1
        assert row["actor_user_id"] == uid
        assert row["service_name"] == "语音转写"
        # P1-7：同组三行携带同一配对态，客户界面据此把三笔归为一组。
        assert row["pair_state"] == "SETTLED"

    # 结算按实际用量 2 秒：2 × 2 × 95% = 3.8 → 向下取整 3；退回 5 - 3 = 2。
    settle = next(row for row in rows if row["type"] == "SETTLE")
    assert settle["reserved_delta"] == -3
    release = next(row for row in rows if row["type"] == "RELEASE")
    assert release["available_delta"] == 2
    reserve = next(row for row in rows if row["type"] == "RESERVE")
    assert reserve["reserved_delta"] == 5


def test_snapshot_cost_keys_and_legacy_rows_never_reach_the_customer(pricing_client, route_state):
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        raw.execute("UPDATE wallets SET available_credits=500 WHERE user_id=%s", (uid,))
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES('analysis',true,4,9)"
        )
        polluted = dict(retail_snapshot(conn, "analysis", 1))
        polluted["unit_cost_fen"] = "9"
        polluted["funding_json"] = [{"lot_id": None, "credits": 4, "amount_fen": "900"}]
        operation_id = accept_operation(
            conn,
            user_id=uid,
            service="analysis",
            source_id="pricing-basis-polluted",
            units=1,
            pricing_snapshot=polluted,
        )
        finish_operation(conn, operation_id=operation_id, units=1, succeeded=True)
        # 充值入账没有计价快照：投影必须是 None，而不是一个字段全空的壳对象。
        raw.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, "
            "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits) "
            "VALUES ('legacy-order',%s,'legacy-merchant-1',100,100,1000,100,2000,20)",
            (uid,),
        )
        raw.execute(
            "INSERT INTO wallet_transactions(id,user_id,type,available_delta,reserved_delta,"
            "recharge_order_id,idempotency_key) VALUES('legacy-charge',%s,'CHARGE',10,0,"
            "'legacy-order','legacy-charge:1')",
            (uid,),
        )

    response = pricing_client.get("/api/customer/wallet/transactions", headers=customer)
    assert response.status_code == 200, response.text
    assert "unit_cost_fen" not in response.text
    assert "amount_fen" not in response.text

    rows = response.json()["items"]
    legacy = next(row for row in rows if row["type"] == "CHARGE")
    assert legacy["pricing"] is None
    # 充值入账不参与计费配对：没有 operation 就没有组态。
    assert legacy["pair_state"] is None
    priced = next(row for row in rows if row["type"] == "RESERVE")
    assert set(priced["pricing"]) == PRICING_FIELDS
    assert priced["pricing"]["credits"] == 4


def test_pair_state_marks_a_settled_group_on_every_row(pricing_client, route_state):
    """P1-7 验收：三笔共享同一 operation 且组态一致——前端据此归组。

    同一计费周期的 RESERVE/SETTLE/RELEASE 都挂着同一个``billing_operation_id``，
    组态对每一行取同一个答案（SETTLED），这样即使分页把三行切开、或按类型
    筛选后只剩一行，界面仍知道它属于一组已结算的计费。
    """
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        raw.execute("UPDATE wallets SET available_credits=500 WHERE user_id=%s", (uid,))
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES('asr',true,2,7)"
        )
        operation_id = accept_operation(
            conn, user_id=uid, service="asr", source_id="pair-settled", units=3
        )
        finish_operation(conn, operation_id=operation_id, units=2, succeeded=True)

    response = pricing_client.get("/api/customer/wallet/transactions", headers=customer)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert {row["type"] for row in rows} == {"RESERVE", "SETTLE", "RELEASE"}
    assert {row["billing_operation_id"] for row in rows} == {operation_id}
    assert {row["pair_state"] for row in rows} == {"SETTLED"}


def test_pair_state_reads_released_for_a_fully_refunded_group(pricing_client, route_state):
    """失败任务：预扣全额退回，两行组态 RELEASED。"""
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        raw.execute("UPDATE wallets SET available_credits=500 WHERE user_id=%s", (uid,))
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES('analysis',true,4,9)"
        )
        operation_id = accept_operation(
            conn, user_id=uid, service="analysis", source_id="pair-released", units=2
        )
        finish_operation(conn, operation_id=operation_id, units=0, succeeded=False)

    response = pricing_client.get("/api/customer/wallet/transactions", headers=customer)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert {row["type"] for row in rows} == {"RESERVE", "RELEASE"}
    assert {row["pair_state"] for row in rows} == {"RELEASED"}
    reserve = next(row for row in rows if row["type"] == "RESERVE")
    release = next(row for row in rows if row["type"] == "RELEASE")
    assert reserve["reserved_delta"] == release["available_delta"] == 8


def test_pair_state_reads_pending_for_an_open_reservation(pricing_client, route_state):
    """进行中的预扣：组态 PENDING——前端显示「结算中」，不是永久冻结。"""
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        raw.execute("UPDATE wallets SET available_credits=500 WHERE user_id=%s", (uid,))
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES('analysis',true,4,9)"
        )
        accept_operation(conn, user_id=uid, service="analysis", source_id="pair-pending", units=2)

    response = pricing_client.get("/api/customer/wallet/transactions", headers=customer)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert [row["type"] for row in rows] == ["RESERVE"]
    assert rows[0]["pair_state"] == "PENDING"
