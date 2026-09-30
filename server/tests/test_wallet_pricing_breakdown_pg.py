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


# ---------------------------------------------------------------------------
# 批次积分去向（任务失败提示的数据来源）
# ---------------------------------------------------------------------------


def _seed_video_batch(raw, uid, batch_id, task_ids):
    """独立创作批次（无项目）+ 若干任务，任务 ID 同时是计费 operation 的 source_id。"""
    raw.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, idempotency_key,"
        " request_hash, request_snapshot_json, status, creation_kind)"
        " VALUES (%s, NULL, %s, %s, %s, '{}', 'QUEUED', 'independent')",
        (batch_id, uid, f"ik-{batch_id}", f"rh-{batch_id}"),
    )
    for task_id in task_ids:
        raw.execute(
            "INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, model, status,"
            " archive_status, quality_status)"
            " VALUES (%s, %s, 'I2V', 'metaso', 'MiniMax-H3', 'QUEUED', 'PENDING', 'PENDING')",
            (task_id, batch_id),
        )


def _seed_batch_with_mixed_outcomes(raw, uid):
    """b-1：成功 / 部分成功 / 失败 / 进行中各一条；b-2：只有一条进行中的任务。"""
    raw.execute("UPDATE wallets SET available_credits=500 WHERE user_id=%s", (uid,))
    raw.execute(
        "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
        "VALUES('video_768p',true,1,1)"
    )
    conn = BusinessConnection.postgres(raw)
    _seed_video_batch(raw, uid, "b-1", ["t-ok", "t-part", "t-fail", "t-open"])
    _seed_video_batch(raw, uid, "b-2", ["t-other"])
    for task_id, actual, succeeded in (
        ("t-ok", 8, True),
        ("t-part", 6, True),
        ("t-fail", 0, False),
    ):
        operation_id = accept_operation(
            conn, user_id=uid, service="video_768p", source_id=task_id, units=8
        )
        finish_operation(conn, operation_id=operation_id, units=actual, succeeded=succeeded)
    # 仍在生成：只有预扣，没有结算也没有退回，不应计入任何一个数。
    accept_operation(conn, user_id=uid, service="video_768p", source_id="t-open", units=8)
    accept_operation(conn, user_id=uid, service="video_768p", source_id="t-other", units=8)


def test_batch_credits_sum_settled_and_released_per_batch(pricing_client, route_state):
    from app.generation import batch_credits_by_batch

    _, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        _seed_batch_with_mixed_outcomes(raw, uid)

    with psycopg.connect(route_state) as raw:
        totals = batch_credits_by_batch(BusinessConnection.postgres(raw), ["b-1", "b-2", "b-none"])

    # 实扣 8 + 6；退回 部分成功剩下的 2 + 失败全额 8。进行中的预扣两边都不算。
    assert totals["b-1"].charged_credits == 14
    assert totals["b-1"].refunded_credits == 10
    # 只有进行中任务的批次没有落账，调用方按「零值」处理。
    assert "b-2" not in totals
    assert "b-none" not in totals
    assert batch_credits_by_batch(BusinessConnection.postgres(raw), []) == {}


def test_batch_detail_and_list_expose_credits(pricing_client, route_state):
    from app.auth import CurrentUser
    from app.generation import get_generation_batch, list_generation_batches

    _, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        _seed_batch_with_mixed_outcomes(raw, uid)
    actor = CurrentUser(id=uid, username="u", display_name="U", role="customer")

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        detail = get_generation_batch(conn, batch_id="b-1", actor=actor)
        page = list_generation_batches(conn, actor=actor, created_by_user_id=uid)

    assert (detail.credits.charged_credits, detail.credits.refunded_credits) == (14, 10)
    by_id = {item.id: item for item in page.items}
    assert (by_id["b-1"].credits.charged_credits, by_id["b-1"].credits.refunded_credits) == (14, 10)
    assert (by_id["b-2"].credits.charged_credits, by_id["b-2"].credits.refunded_credits) == (0, 0)


# ---------------------------------------------------------------------------
# 消费记录按计费周期合并（/api/customer/wallet/ledger）
# ---------------------------------------------------------------------------

LEDGER_PATH = "/api/customer/wallet/ledger"


def _seed_ledger_scenario(raw, uid):
    """完成 / 部分成功 / 整笔退回 / 生成中各一条，最后一笔独立的充值入账。

    每个 asr 任务暂扣 8（1 积分/秒 × 8 秒）：done 实扣 8；part 实扣 6 退回 2；
    fail 整笔退回 8；open 只有暂扣。返回 ``{source_id: operation_id}``。
    """
    raw.execute("UPDATE wallets SET available_credits=500 WHERE user_id=%s", (uid,))
    raw.execute(
        "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
        "VALUES('asr',true,1,1)"
    )
    conn = BusinessConnection.postgres(raw)
    operations: dict[str, str] = {}
    for source, actual, succeeded in (("done", 8, True), ("part", 6, True), ("fail", 0, False)):
        operation_id = accept_operation(conn, user_id=uid, service="asr", source_id=source, units=8)
        finish_operation(conn, operation_id=operation_id, units=actual, succeeded=succeeded)
        operations[source] = operation_id
    operations["open"] = accept_operation(
        conn, user_id=uid, service="asr", source_id="open", units=8
    )
    raw.execute(
        "INSERT INTO recharge_orders (id, user_id, merchant_order_no, "
        "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits) "
        "VALUES ('ledger-order',%s,'ledger-merchant-1',100,100,1000,100,1000,10)",
        (uid,),
    )
    raw.execute(
        "INSERT INTO wallet_transactions(id,user_id,type,available_delta,reserved_delta,"
        "recharge_order_id,idempotency_key) VALUES('ledger-charge',%s,'CHARGE',10,0,"
        "'ledger-order','ledger-charge:1')",
        (uid,),
    )
    return operations


def _move_cycle_across_months(raw, operation_id):
    """把一条任务的暂扣摆到 1 月、退回摆到 3 月，用来造「时间筛选切在两笔之间」。

    账本行受触发器保护；隔离库里临时绕过它。
    """
    raw.execute("SET session_replication_role = replica")
    raw.execute(
        "UPDATE wallet_transactions SET created_at='2026-01-10T00:00:00+00:00' "
        "WHERE billing_operation_id=%s AND type='RESERVE'",
        (operation_id,),
    )
    raw.execute(
        "UPDATE wallet_transactions SET created_at='2026-03-10T00:00:00+00:00' "
        "WHERE billing_operation_id=%s AND type='RELEASE'",
        (operation_id,),
    )
    raw.execute("SET session_replication_role = DEFAULT")


def _ledger(client, headers, **query):
    response = client.get(LEDGER_PATH, headers=headers, params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_ledger_requires_a_session(pricing_client):
    assert pricing_client.get(LEDGER_PATH).status_code == 401


def test_ledger_merges_each_billing_cycle_into_one_entry(pricing_client, route_state):
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        operations = _seed_ledger_scenario(raw, uid)

    body = _ledger(pricing_client, customer)

    # 按最新一笔记账倒序：入账 → 生成中 → 整笔退回 → 部分成功 → 已完成。
    assert [entry["outcome"] for entry in body["items"]] == [
        "POSTED",
        "PENDING",
        "FAILED",
        "PARTIAL",
        "COMPLETED",
    ]
    by_outcome = {entry["outcome"]: entry for entry in body["items"]}

    def amounts(entry):
        return (entry["reserved_credits"], entry["charged_credits"], entry["refunded_credits"])

    done = by_outcome["COMPLETED"]
    assert (done["kind"], done["key"]) == ("cycle", operations["done"])
    assert amounts(done) == (8, 8, 0)
    assert done["net_available_delta"] == -8
    assert [row["type"] for row in done["rows"]] == ["RESERVE", "SETTLE"]

    part = by_outcome["PARTIAL"]
    assert amounts(part) == (8, 6, 2)
    assert part["net_available_delta"] == -6
    # 周期内按资金走向排：暂扣 → 实扣 → 退回，而不是靠同一事务里相同的时间戳。
    assert [row["type"] for row in part["rows"]] == ["RESERVE", "SETTLE", "RELEASE"]

    failed = by_outcome["FAILED"]
    assert amounts(failed) == (8, 0, 8)
    # 整笔退回：对可用积分的净影响是 0，这正是用户想看到的「没花钱」。
    assert failed["net_available_delta"] == 0
    assert [row["type"] for row in failed["rows"]] == ["RESERVE", "RELEASE"]

    pending = by_outcome["PENDING"]
    assert amounts(pending) == (8, 0, 0)
    assert pending["net_available_delta"] == -8
    assert len(pending["rows"]) == 1

    posted = by_outcome["POSTED"]
    assert (posted["kind"], posted["key"]) == ("row", "ledger-charge")
    assert posted["net_available_delta"] == 10
    assert amounts(posted) == (0, 0, 0)

    assert body["total"] == 5
    assert body["counts"] == {
        "total": 5,
        "pending": 1,
        "completed": 1,
        "refunded": 2,
        "posted": 1,
    }
    assert "unit_cost_fen" not in json.dumps(body)


def test_ledger_pages_by_entry_and_never_splits_a_cycle(pricing_client, route_state):
    """回归：逐行分页会把暂扣与退回切到两页，第二页只剩一行「暂扣 -8」。"""
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        _seed_ledger_scenario(raw, uid)

    first = _ledger(pricing_client, customer, limit=2, offset=0)
    second = _ledger(pricing_client, customer, limit=2, offset=2)
    third = _ledger(pricing_client, customer, limit=2, offset=4)

    assert [entry["outcome"] for entry in first["items"]] == ["POSTED", "PENDING"]
    assert [entry["outcome"] for entry in second["items"]] == ["FAILED", "PARTIAL"]
    assert [entry["outcome"] for entry in third["items"]] == ["COMPLETED"]
    assert {page["total"] for page in (first, second, third)} == {5}
    # 第二页的两条各自带着完整的逐笔：整笔退回 2 笔、部分成功 3 笔，一笔都不缺。
    assert [len(entry["rows"]) for entry in second["items"]] == [2, 3]
    keys = [entry["key"] for page in (first, second, third) for entry in page["items"]]
    assert len(keys) == len(set(keys)) == 5


def test_ledger_outcome_filter_counts_entries_and_keeps_counts_stable(pricing_client, route_state):
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        _seed_ledger_scenario(raw, uid)

    refunded = _ledger(pricing_client, customer, outcome="refunded")
    assert {entry["outcome"] for entry in refunded["items"]} == {"FAILED", "PARTIAL"}
    assert refunded["total"] == 2
    # 结果筛选条上的数字不随筛选变：用户要看到「切到别的结果有几条」。
    assert refunded["counts"]["total"] == 5
    assert refunded["counts"]["pending"] == 1

    pending = _ledger(pricing_client, customer, outcome="pending")
    assert [entry["outcome"] for entry in pending["items"]] == ["PENDING"]
    assert pending["total"] == 1

    posted = _ledger(pricing_client, customer, outcome="posted")
    assert [entry["kind"] for entry in posted["items"]] == ["row"]

    bogus = pricing_client.get(LEDGER_PATH, headers=customer, params={"outcome": "bogus"})
    assert bogus.status_code == 422


def test_ledger_time_filter_cutting_a_cycle_still_returns_the_whole_cycle(
    pricing_client, route_state
):
    """筛选范围恰好只含退回、不含暂扣时，这条任务仍是完整的「已退回」，不是「生成中」。"""
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        operations = _seed_ledger_scenario(raw, uid)
        _move_cycle_across_months(raw, operations["fail"])

    body = _ledger(
        pricing_client,
        customer,
        started_at="2026-03-01T00:00:00+00:00",
        ended_at="2026-04-01T00:00:00+00:00",
    )

    assert [entry["key"] for entry in body["items"]] == [operations["fail"]]
    entry = body["items"][0]
    assert entry["outcome"] == "FAILED"
    assert [row["type"] for row in entry["rows"]] == ["RESERVE", "RELEASE"]
    assert (entry["reserved_credits"], entry["refunded_credits"]) == (8, 8)
    assert body["total"] == 1

    inverted = pricing_client.get(
        LEDGER_PATH,
        headers=customer,
        params={
            "started_at": "2026-04-01T00:00:00+00:00",
            "ended_at": "2026-03-01T00:00:00+00:00",
        },
    )
    assert inverted.status_code == 422
    naive = pricing_client.get(
        LEDGER_PATH, headers=customer, params={"started_at": "2026-04-01T00:00:00"}
    )
    assert naive.status_code == 422


def test_ledger_sub_account_summary_counts_entries_not_rows(pricing_client, route_state):
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        _seed_ledger_scenario(raw, uid)

    body = _ledger(pricing_client, customer, group_by_sub_account="true")

    # 母账号自己操作时 actor 就是自己：四条任务共 8 笔流水，摘要按条数 4，与列表对得上。
    [summary] = body["sub_account_summary"]
    assert summary["sub_account_id"] == uid
    assert summary["debit_total"] == 14
    assert summary["credit_total"] == 10
    assert summary["transaction_count"] == 4
    # 不带该参数时不返回摘要。
    assert _ledger(pricing_client, customer)["sub_account_summary"] is None
    # 摘要同样遵守结果筛选：只看生成中时，只剩那一条任务，没有实扣也没有退回。
    [filtered] = _ledger(pricing_client, customer, group_by_sub_account="true", outcome="pending")[
        "sub_account_summary"
    ]
    assert (filtered["debit_total"], filtered["credit_total"], filtered["transaction_count"]) == (
        0,
        0,
        1,
    )


def test_center_summary_reports_total_returned_credits(pricing_client, route_state):
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        _seed_ledger_scenario(raw, uid)

    response = pricing_client.get("/api/customer/center-summary", headers=customer)
    assert response.status_code == 200, response.text
    body = response.json()
    # 退回 = 部分成功剩下的 2 + 整笔退回的 8；暂扣与实扣不算退回。
    assert body["total_returned_credits"] == 10
    assert body["total_consumed_credits"] == 14


def _export_rows(client, headers, **query):
    import csv
    import io

    response = client.get("/api/customer/wallet/transactions/export", headers=headers, params=query)
    assert response.status_code == 200, response.text
    reader = csv.reader(io.StringIO(response.content.decode("utf-8-sig")))
    header, *rows = list(reader)
    return header, rows


def test_export_with_outcome_matches_the_ledger_entries(pricing_client, route_state):
    customer, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        operations = _seed_ledger_scenario(raw, uid)

    _, everything = _export_rows(pricing_client, customer)
    assert len(everything) == 9  # 2 + 3 + 2 + 1 + 入账

    header, refunded = _export_rows(pricing_client, customer, outcome="refunded")
    assert header[1] == "类型"
    # 「有退回」两条任务的全部逐笔：部分成功 3 笔 + 整笔退回 2 笔。
    assert sorted(row[1] for row in refunded) == sorted(
        ["RESERVE", "SETTLE", "RELEASE", "RESERVE", "RELEASE"]
    )
    _, posted = _export_rows(pricing_client, customer, outcome="posted")
    assert [row[1] for row in posted] == ["CHARGE"]

    # 与列表同口径：带时间筛选、只命中退回那一笔时，导出仍取整条任务的两笔。
    with psycopg.connect(route_state) as raw:
        _move_cycle_across_months(raw, operations["fail"])
    _, cut = _export_rows(
        pricing_client,
        customer,
        outcome="refunded",
        started_at="2026-03-01T00:00:00+00:00",
        ended_at="2026-04-01T00:00:00+00:00",
    )
    assert sorted(row[1] for row in cut) == ["RELEASE", "RESERVE"]


def test_merged_ledger_query_turns_jit_off_for_its_own_transaction_only(
    pricing_client, route_state
):
    """回归：JIT 会让这条合并查询在 1 万条任务的账本上慢 10 倍以上（每次重新编译）。

    关闭必须是事务级的：连接放回池里后，其他请求不该带着被改过的设置。
    """
    from app.customer_ledger import read_ledger_page

    _, uid = account(pricing_client)
    with psycopg.connect(route_state) as raw:
        _seed_ledger_scenario(raw, uid)
    with psycopg.connect(route_state) as raw:
        default = raw.execute("SHOW jit").fetchone()[0]
        page = read_ledger_page(
            raw,
            wallet_owner_id=uid,
            sub_account_id=None,
            token_group_id=None,
            auth_source=None,
            business=None,
            started_at=None,
            ended_at=None,
            outcome=None,
            limit=20,
            offset=0,
            group_by_sub_account=False,
        )
        assert page.total == 5
        assert raw.execute("SHOW jit").fetchone()[0] == "off"
        raw.commit()
        assert raw.execute("SHOW jit").fetchone()[0] == default
