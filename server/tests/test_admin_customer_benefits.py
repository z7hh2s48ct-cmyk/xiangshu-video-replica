"""管理端客户权益：代客开通套餐（线下已付款）与手工折扣.

钉住的契约（2026-09-27 用户确认口径）：

- 开通套餐 = 积分 + 折扣权益都给，落账与客户自助买套餐同形：PAID 套餐订单（冻结快照）
  + CHARGE + 钱包入账 + 套餐权益行 + ``OFFLINE_PAYMENT`` 审计行，全部同一事务；
- 套餐权益沿用「最近覆盖」，无权益套餐不触碰已有权益；
- 停用 / 版本已变 / 低于最低充值额 / 不存在的套餐一律拒绝且不留任何账本痕迹；
- 手工折扣优先于套餐折扣（方案 A），同一客户只保留一条生效的手工折扣；
- 写契约与调账一致：幂等重放不重复入账，审计员只读，管理员不能给自己开通/设折扣。

复用 test_admin_customer_routes 的 T23 专用库与种子数据（customer_u 余额 50）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from decimal import Decimal

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_admin_customer_routes import (  # noqa: F401
    CUSTOMER_USER_ID,
    IDEMPOTENCY_KEY_HEADER,
    REPLAY_HEADER,
    TEST_ADMIN_SESSION_KEY,
    _admin_session,
    _fetch_all,
    _fetch_one,
    _t23_dsn,
    _wallet_balance,
    adjustments_dsn,
    route_state,
)

from app.admin_auth_routes import ADMIN_SESSION_HMAC_KEY_ENV
from app.customer_benefits import MANUAL_DISCOUNT_PRIORITY
from app.db_pg import DATABASE_URL_ENV
from app.db_portable import BusinessConnection
from app.discount_service import get_best_discount
from app.recharge_packages import PACKAGE_DISCOUNT_PRIORITY
from app.usage_billing import accept_operation, finish_operation

GRANT_PATH = f"/api/control/customers/{CUSTOMER_USER_ID}/package-grants"
DISCOUNTS_PATH = f"/api/control/customers/{CUSTOMER_USER_ID}/discounts"


@pytest.mark.parametrize(
    "kind", ["none", "global", "manual", "recharge_package", "global_over_manual"]
)
def test_read_quote_source_matches_actual_settlement(client, kind):
    admin = _admin_session(client)
    if kind in {"manual", "global_over_manual"}:
        assert _create_discount(client, admin).status_code == 201
    if kind == "recharge_package":
        assert _grant(client, admin, _seed_package(interfaces=[])).status_code == 201
    with psycopg.connect(_t23_dsn()) as raw:
        raw.execute(
            "UPDATE customer_credit_pricing SET config_json=%s WHERE id=1",
            (
                json.dumps(
                    {
                        "points_per_yuan": 100000,
                        "discount_basis_points": 7000
                        if kind in {"global", "global_over_manual"}
                        else 10000,
                    }
                ),
            ),
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_rounding) "
            "VALUES ('asr',true,2.5,'exact') ON CONFLICT(service) DO UPDATE "
            "SET enabled=true,unit_credits=2.5,unit_rounding='exact'"
        )
    before_wallet, before_ledger = _wallet_balance(CUSTOMER_USER_ID), _ledger_counts()
    response = client.get(
        "/api/control/billing/quote",
        headers=admin,
        params={
            "service": "asr",
            "units": "0.4",
            "user_id": CUSTOMER_USER_ID,
        },
    )
    assert response.status_code == 200, response.text
    quote = response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert quote["discount_kind"] == ("global" if kind == "global_over_manual" else kind)
    assert Decimal(quote["credits"]) == 1
    assert Decimal(quote["final_unit_credits"]) >= 2
    assert 0 < Decimal(quote["unit_nominal_fen"]) < 1
    assert quote["cost_fen"] is None and quote["gross_fen"] is None
    assert _wallet_balance(CUSTOMER_USER_ID) == before_wallet
    assert _ledger_counts() == before_ledger
    with psycopg.connect(_t23_dsn()) as raw:
        conn = BusinessConnection.postgres(raw)
        operation = accept_operation(
            conn,
            user_id=CUSTOMER_USER_ID,
            service="asr",
            source_id="quote-settlement",
            units=Decimal("0.4"),
        )
        finish_operation(conn, operation_id=operation, units=Decimal("0.4"), succeeded=True)
        row = raw.execute(
            "SELECT -reserved_delta FROM wallet_transactions "
            "WHERE billing_operation_id=%s AND type='SETTLE'",
            (operation,),
        ).fetchone()
        assert row is not None and Decimal(row[0]) == Decimal(quote["credits"])


def test_auditor_can_read_quote_but_cannot_post_or_change_discounts(client):
    auditor = _admin_session(client, actor="auditor_u")
    assert (
        client.get("/api/control/billing/quote?service=oral&units=1", headers=auditor).status_code
        == 200
    )
    assert (
        client.post(
            "/api/control/billing/quote",
            headers=auditor,
            json={"service": "oral", "units": 1, "confirm": True, "reason": "probe"},
        ).status_code
        == 403
    )
    assert _create_discount(client, auditor).status_code == 403
    assert _ledger_counts() == (0, 0, 0)


def test_legacy_post_quote_retains_write_contract_and_idempotency(client):
    admin = _admin_session(client)
    data = {"service": "oral", "units": 1, "confirm": True, "reason": "legacy quote"}
    assert client.post("/api/control/billing/quote", headers=admin, json=data).status_code == 400
    headers = {**admin, IDEMPOTENCY_KEY_HEADER: "legacy-quote-contract"}
    first = client.post("/api/control/billing/quote", headers=headers, json=data)
    second = client.post("/api/control/billing/quote", headers=headers, json=data)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers[REPLAY_HEADER] == "true"
    assert _ledger_counts() == (0, 0, 0)


def test_quantized_zero_discount_keeps_existing_manual_discount(client):
    admin = _admin_session(client)
    first = _create_discount(client, admin)
    assert first.status_code == 201
    rejected = _create_discount(client, admin, rate="0.000001")
    assert rejected.status_code == 422, rejected.text
    rows = _discount_rows()
    assert len(rows) == 1 and rows[0][0] == first.json()["discount"]["id"]
    assert rows[0][3] is True
    assert _best_rate() == Decimal("0.8")


def test_discount_list_uses_database_clock_for_effective_state(client):
    admin = _admin_session(client)
    for identity, active, start, end in [
        ("effective", True, "-1 day", None),
        ("pending", True, "1 day", "2 days"),
        ("expired", True, "-2 days", "-1 day"),
        ("disabled", False, "-1 day", None),
    ]:
        with psycopg.connect(_t23_dsn()) as raw:
            raw.execute(
                "INSERT INTO customer_discounts"
                "(id,user_id,discount_rate,priority,is_active,valid_from,valid_until) "
                "VALUES (%s,%s,0.8,200,%s,clock_timestamp()+%s::interval,"
                "CASE WHEN %s::text IS NULL THEN NULL ELSE clock_timestamp()+%s::interval END)",
                (identity, CUSTOMER_USER_ID, active, start, end, end),
            )
    response = client.get(DISCOUNTS_PATH, headers=admin)
    assert response.status_code == 200, response.text
    assert {row["id"]: row["state"] for row in response.json()["items"]} == {
        name: name for name in ["effective", "pending", "expired", "disabled"]
    }


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[TestClient]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_benefit_routes import router as benefit_router
    from app.billing_routes import router as billing_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(benefit_router)
    app.include_router(billing_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def _seed_package(
    *,
    amount_fen: int = 199_800,
    credits: int = 2_000,
    discount_rate: str | None = "0.9000",
    interfaces: list[str] | None = None,
    is_active: bool = True,
    version: int = 0,
) -> str:
    package_id = f"rpkg-{uuid.uuid4().hex}"
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO recharge_packages (id, name, amount_fen, credits, discount_rate, "
            "discount_interfaces, is_active, version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                package_id,
                f"套餐 {package_id[-4:]}",
                amount_fen,
                credits,
                discount_rate,
                json.dumps(["video_generation"] if interfaces is None else interfaces),
                is_active,
                version,
            ),
        )
    return package_id


def _grant(
    client: TestClient,
    headers: dict[str, str],
    package_id: str,
    *,
    version: int = 0,
    ref: str = "BANK-20260927-001",
    key: str | None = None,
    user_path: str = GRANT_PATH,
) -> object:
    return client.post(
        user_path,
        headers={**headers, IDEMPOTENCY_KEY_HEADER: key or f"grant-{uuid.uuid4()}"},
        json={
            "confirm": True,
            "reason": "客户对公转账已到账",
            "package_id": package_id,
            "package_version": version,
            "source_document_ref": ref,
        },
    )


def _create_discount(
    client: TestClient,
    headers: dict[str, str],
    *,
    rate: str = "0.8000",
    interfaces: list[str] | None = None,
    valid_until: str | None = None,
    key: str | None = None,
) -> object:
    payload: dict[str, object] = {
        "confirm": True,
        "reason": "大客户年框价",
        "discount_rate": rate,
        "applicable_interfaces": interfaces or [],
    }
    if valid_until is not None:
        payload["valid_until"] = valid_until
    return client.post(
        DISCOUNTS_PATH,
        headers={**headers, IDEMPOTENCY_KEY_HEADER: key or f"discount-{uuid.uuid4()}"},
        json=payload,
    )


def _discount_rows(user_id: str = CUSTOMER_USER_ID) -> list[tuple]:
    return _fetch_all(
        "SELECT id, discount_rate, priority, is_active, source_recharge_order_id "
        "FROM customer_discounts WHERE user_id = %s ORDER BY created_at, id",
        (user_id,),
    )


def _best_rate(interface_key: str = "video_generation") -> Decimal | None:
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        now_row = conn.execute("SELECT clock_timestamp()").fetchone()
        assert now_row is not None
        best = get_best_discount(
            conn, user_id=CUSTOMER_USER_ID, interface_key=interface_key, at_time=now_row[0]
        )
    return None if best is None else best.discount_rate


def _ledger_counts() -> tuple[int, int, int]:
    row = _fetch_one(
        "SELECT (SELECT count(*) FROM recharge_orders WHERE package_id IS NOT NULL), "
        "(SELECT count(*) FROM admin_adjustments), "
        "(SELECT count(*) FROM customer_discounts)"
    )
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


def test_manual_priority_outranks_package_priority() -> None:
    assert MANUAL_DISCOUNT_PRIORITY > PACKAGE_DISCOUNT_PRIORITY


# ---------------------------------------------------------------------------
# 代客开通套餐
# ---------------------------------------------------------------------------


def test_package_grant_writes_order_ledger_wallet_discount_and_audit(client: TestClient) -> None:
    admin = _admin_session(client)
    package_id = _seed_package()

    response = _grant(client, admin, package_id)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["amount_fen"] == 199_800
    assert payload["credits"] == 2_000
    assert payload["wallet_balance_after"] == 2_050
    assert payload["discount_rate"] == "0.9000"
    assert payload["source_document_type"] == "OFFLINE_PAYMENT"
    order_id = payload["order_id"]

    order = _fetch_one(
        "SELECT provider, status, amount_fen, credits, package_id, package_snapshot_json "
        "FROM recharge_orders WHERE id = %s",
        (order_id,),
    )
    assert order is not None
    assert order[:5] == ("admin_adjustment", "PAID", 199_800, 2_000, package_id)
    snapshot = json.loads(str(order[5]))
    assert snapshot["credits"] == 2_000 and snapshot["discount_rate"] == "0.9000"

    charges = _fetch_all(
        "SELECT type, available_delta FROM wallet_transactions WHERE recharge_order_id = %s",
        (order_id,),
    )
    assert charges == [("CHARGE", 2_000)]
    assert _wallet_balance(CUSTOMER_USER_ID) == (2_050, 0)

    discounts = _discount_rows()
    assert len(discounts) == 1
    assert discounts[0][0] == payload["discount_id"]
    assert discounts[0][1:] == (Decimal("0.9000"), PACKAGE_DISCOUNT_PRIORITY, True, order_id)
    assert _best_rate() == Decimal("0.9000")

    audit = _fetch_all(
        "SELECT recharge_order_id, admin_user_id, source_document_type, source_document_ref "
        "FROM admin_adjustments WHERE target_user_id = %s",
        (CUSTOMER_USER_ID,),
    )
    assert audit == [(order_id, "admin_u", "OFFLINE_PAYMENT", "BANK-20260927-001")]


def test_newer_package_grant_overrides_previous_package_discount(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _grant(client, admin, _seed_package(discount_rate="0.9000"))
    second = _grant(client, admin, _seed_package(discount_rate="0.8000"))
    assert first.status_code == 201 and second.status_code == 201

    rows = {row[0]: row for row in _discount_rows()}
    assert rows[first.json()["discount_id"]][3] is False
    assert rows[second.json()["discount_id"]][3] is True
    assert _best_rate() == Decimal("0.8000")


def test_package_without_discount_keeps_existing_benefits(client: TestClient) -> None:
    admin = _admin_session(client)
    with_discount = _grant(client, admin, _seed_package(discount_rate="0.9000"))
    plain = _grant(client, admin, _seed_package(discount_rate=None, interfaces=[]))

    assert plain.status_code == 201, plain.text
    assert plain.json()["discount_id"] is None
    rows = _discount_rows()
    assert len(rows) == 1 and rows[0][0] == with_discount.json()["discount_id"]
    assert rows[0][3] is True
    assert _wallet_balance(CUSTOMER_USER_ID) == (4_050, 0)


@pytest.mark.parametrize(
    ("seed", "version", "status", "code"),
    [
        ({"is_active": False}, 0, 409, "RECHARGE_PACKAGE_INACTIVE"),
        ({"version": 3}, 2, 409, "RECHARGE_PACKAGE_VERSION_CONFLICT"),
        ({"amount_fen": 5_000, "credits": 5}, 0, 422, "RECHARGE_PACKAGE_BELOW_MINIMUM"),
        (None, 0, 404, "RECHARGE_PACKAGE_NOT_FOUND"),
    ],
)
def test_ungrantable_packages_leave_no_ledger_trace(
    client: TestClient,
    seed: dict[str, object] | None,
    version: int,
    status: int,
    code: str,
) -> None:
    admin = _admin_session(client)
    package_id = "rpkg-missing" if seed is None else _seed_package(**seed)  # type: ignore[arg-type]

    response = _grant(client, admin, package_id, version=version)

    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code
    assert _ledger_counts() == (0, 0, 0)
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


def test_package_grant_requires_voucher_reference(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _grant(client, admin, _seed_package(), ref="   ")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PACKAGE_GRANT_VALIDATION_FAILED"
    assert _ledger_counts() == (0, 0, 0)


def test_package_grant_replay_does_not_double_credit(client: TestClient) -> None:
    admin = _admin_session(client)
    package_id = _seed_package()
    first = _grant(client, admin, package_id, key="grant-replay")
    replay = _grant(client, admin, package_id, key="grant-replay")

    assert first.status_code == 201 and replay.status_code == 201
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json()["order_id"] == first.json()["order_id"]
    assert _wallet_balance(CUSTOMER_USER_ID) == (2_050, 0)
    assert _ledger_counts() == (1, 1, 1)


def test_admin_cannot_grant_package_or_discount_to_self(client: TestClient) -> None:
    admin = _admin_session(client)
    package = _grant(
        client,
        admin,
        _seed_package(),
        user_path="/api/control/customers/admin_u/package-grants",
    )
    discount = client.post(
        "/api/control/customers/admin_u/discounts",
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "self-discount"},
        json={"confirm": True, "reason": "自助", "discount_rate": "0.5"},
    )

    for response in (package, discount):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "ADMIN_SELF_SERVICE_FORBIDDEN"
    assert _ledger_counts() == (0, 0, 0)


def test_auditor_reads_discounts_but_cannot_write(client: TestClient) -> None:
    auditor = _admin_session(client, actor="auditor_u")
    grant = _grant(client, auditor, _seed_package())
    discount = _create_discount(client, auditor)

    assert grant.status_code == 403 and discount.status_code == 403
    assert client.get(DISCOUNTS_PATH, headers=auditor).status_code == 200
    assert _ledger_counts() == (0, 0, 0)


# ---------------------------------------------------------------------------
# 手工折扣
# ---------------------------------------------------------------------------


def test_manual_discount_replaces_previous_manual_and_can_be_deactivated(
    client: TestClient,
) -> None:
    admin = _admin_session(client)
    first = _create_discount(client, admin, rate="0.8", interfaces=["video_generation"])
    second = _create_discount(client, admin, rate="0.7")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_id = first.json()["discount"]["id"]
    second_body = second.json()
    assert second_body["replaced_discount_ids"] == [first_id]
    assert second_body["discount"]["priority"] == MANUAL_DISCOUNT_PRIORITY
    assert second_body["discount"]["source"] == "manual"

    listing = client.get(DISCOUNTS_PATH, headers=admin).json()["items"]
    assert [(item["id"], item["is_active"]) for item in listing] == [
        (second_body["discount"]["id"], True),
        (first_id, False),
    ]

    deactivate = client.post(
        f"{DISCOUNTS_PATH}/{second_body['discount']['id']}/deactivate",
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "deactivate-1"},
        json={"confirm": True, "reason": "年框到期"},
    )
    assert deactivate.status_code == 200, deactivate.text
    assert deactivate.json()["was_active"] is True
    assert _best_rate() is None

    actions = [
        row[0]
        for row in _fetch_all(
            "SELECT action FROM audit_logs WHERE entity_type = 'customer_discount' "
            "ORDER BY created_at"
        )
    ]
    assert actions == [
        "customer_discount.create",
        "customer_discount.create",
        "customer_discount.deactivate",
    ]


def test_manual_discount_outranks_package_discount(client: TestClient) -> None:
    admin = _admin_session(client)
    grant = _grant(client, admin, _seed_package(discount_rate="0.9000"))
    assert grant.status_code == 201
    # 手工价比套餐差也照样生效：管理员对单客户定的价是最终口径（方案 A）。
    manual = _create_discount(client, admin, rate="0.95")
    assert manual.status_code == 201
    assert _best_rate() == Decimal("0.9500")

    # 之后再开通的套餐也不会顶掉手工折扣，只替换旧的套餐权益。
    later = _grant(client, admin, _seed_package(discount_rate="0.8000"))
    assert later.status_code == 201
    assert _best_rate() == Decimal("0.9500")

    client.post(
        f"{DISCOUNTS_PATH}/{manual.json()['discount']['id']}/deactivate",
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "deactivate-manual"},
        json={"confirm": True, "reason": "恢复套餐价"},
    )
    assert _best_rate() == Decimal("0.8000")


@pytest.mark.parametrize(
    ("rate", "interfaces", "valid_until"),
    [
        ("0", None, None),
        ("1.5", None, None),
        ("0.8", ["no_such_interface"], None),
        ("0.8", None, "2000-01-01T00:00:00+00:00"),
    ],
)
def test_manual_discount_rejects_invalid_input(
    client: TestClient, rate: str, interfaces: list[str] | None, valid_until: str | None
) -> None:
    admin = _admin_session(client)
    response = _create_discount(
        client, admin, rate=rate, interfaces=interfaces, valid_until=valid_until
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "INVALID_CUSTOMER_DISCOUNT"
    assert _discount_rows() == []


def test_manual_discount_with_future_expiry_is_listed(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_discount(client, admin, valid_until="2099-01-01T00:00:00+00:00")
    assert response.status_code == 201, response.text
    assert response.json()["discount"]["valid_until"].startswith("2099-01-01")


def test_package_discount_cannot_be_deactivated_by_hand(client: TestClient) -> None:
    admin = _admin_session(client)
    grant = _grant(client, admin, _seed_package())
    package_discount_id = grant.json()["discount_id"]

    refused = client.post(
        f"{DISCOUNTS_PATH}/{package_discount_id}/deactivate",
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "deactivate-package"},
        json={"confirm": True, "reason": "误操作"},
    )
    missing = client.post(
        f"{DISCOUNTS_PATH}/no-such-discount/deactivate",
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "deactivate-missing"},
        json={"confirm": True, "reason": "误操作"},
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "CUSTOMER_DISCOUNT_NOT_MANUAL"
    assert missing.status_code == 404
    assert _best_rate() == Decimal("0.9000")
