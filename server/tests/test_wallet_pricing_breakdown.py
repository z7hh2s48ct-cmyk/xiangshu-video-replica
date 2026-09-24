"""用户端计费依据：冻结快照的零售侧白名单投影（BILLING-OBS-20260922 P0-3）。

客户流水此前只透出快照里的 ``version``（``credit_price_version``），单价、用量、
折扣、取整规则全部留在服务端——客户看到「-4 积分」却无法核对这个数是怎么来的。
本投影把快照中**零售侧**字段摊开给客户；成本侧字段（``unit_cost_fen``、
``revenue_fen``、funding 明细）永远不越过客户边界，因此这里是白名单而非黑名单。
"""

from __future__ import annotations

from decimal import Decimal

from app.wallet_routes import WalletTransactionPricing, WalletTransactionResponse, pricing_breakdown


def test_retail_basis_is_projected_and_cost_fields_never_cross_the_boundary():
    snapshot = {
        "service": "asr",
        "version": 3,
        "unit": "second",
        "units": "3.000000",
        "enabled": True,
        "unit_credits": "2",
        "unit_rounding": "ceil",
        "credits": 6,
        "discount_basis_points": 9500,
        "consumption_rounding": "floor",
        "points_per_yuan": 100,
        "free_reason": None,
        # 成本侧：历史快照里可能残留这些键，投影必须整条丢掉。
        "unit_cost_fen": "7",
        "revenue_fen": "14",
        "funding_json": [{"lot_id": None, "credits": 6}],
    }

    pricing = pricing_breakdown(snapshot)

    assert isinstance(pricing, WalletTransactionPricing)
    assert pricing.model_dump() == {
        "service": "asr",
        "version": 3,
        "unit": "second",
        "units": "3.000000",
        "unit_credits": "2",
        "unit_rounding": "ceil",
        "discount_basis_points": 9500,
        "consumption_rounding": "floor",
        "credits": 6,
        "enabled": True,
        "free_reason": None,
    }


def test_decimal_and_integer_snapshot_values_normalize_to_json_scalars():
    pricing = pricing_breakdown(
        {
            "service": "oral",
            "version": Decimal("4"),
            "unit": "second",
            "units": Decimal("12.500000"),
            "unit_credits": Decimal("2.5"),
            "credits": Decimal("32"),
            "discount_basis_points": Decimal("8000"),
            "enabled": False,
            "free_reason": "disabled",
        }
    )

    assert pricing is not None
    assert pricing.version == 4
    assert pricing.units == "12.500000"
    assert pricing.unit_credits == "2.5"
    assert pricing.credits == 32
    assert pricing.discount_basis_points == 8000
    assert pricing.unit_rounding is None
    assert pricing.consumption_rounding is None
    assert pricing.enabled is False
    assert pricing.free_reason == "disabled"


def test_absent_or_unusable_snapshot_degrades_to_none_instead_of_junk():
    assert pricing_breakdown(None) is None
    assert pricing_breakdown({}) is None
    assert pricing_breakdown([]) is None
    assert pricing_breakdown("{}") is None

    partial = pricing_breakdown({"service": "analysis"})
    assert partial is not None
    assert partial.service == "analysis"
    assert partial.version is None
    assert partial.credits is None
    assert partial.units is None

    junk = pricing_breakdown(
        {
            "service": "analysis",
            # 历史行里出现过的脏值：既不能 500，也不能伪装成合法取值。
            "version": "not-a-number",
            "credits": True,
            "units": "   ",
            "unit_rounding": "round-half-up",
            "consumption_rounding": "nearest",
            "enabled": "yes",
            "free_reason": 17,
        }
    )
    assert junk is not None
    assert junk.model_dump() == {
        "service": "analysis",
        "version": None,
        "unit": None,
        "units": None,
        "unit_credits": None,
        "unit_rounding": None,
        "discount_basis_points": None,
        "consumption_rounding": None,
        "credits": None,
        "enabled": None,
        "free_reason": None,
    }


def test_response_model_defaults_to_no_pricing_for_rows_without_a_snapshot():
    row = WalletTransactionResponse(
        id="tx-legacy",
        user_id="u1",
        type="CHARGE",
        available_delta=10,
        reserved_delta=0,
        recharge_order_id="order-1",
        task_id=None,
        billing_round=None,
        created_at="2026-01-01 00:00:00+00:00",
    )

    assert row.pricing is None
    assert row.model_dump()["pricing"] is None
