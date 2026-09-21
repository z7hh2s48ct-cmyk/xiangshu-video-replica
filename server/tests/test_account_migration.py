"""Authenticated legacy account upgrade preserves identity, wallet and history."""

# ruff: noqa: F811
import os
from uuid import uuid4

import psycopg
import pytest
from test_account_credit_operations import (  # noqa: F401
    account,
    admin_login,
    client,
    mutation,
    operations_client,
    pricing_client,
    registration_client,
    registration_dsn,
    route_state,
    token_headers,
)


def legacy_account(client, dsn):
    from app.activation_code_routes import router
    from app.activation_code_service import compute_code_digest, generate_activation_code

    client.app.include_router(router)
    admin = admin_login(client, dsn)
    code = generate_activation_code()
    with psycopg.connect(dsn) as conn:
        admin_uid = conn.execute("SELECT id FROM users WHERE username = 'price_admin'").fetchone()[
            0
        ]
        conn.execute(
            "INSERT INTO activation_code_batches (id, name, "
            "face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id)"
            " VALUES ('legacy-batch', 'legacy', 100000, 1000, 100, 1, "
            "'2099-01-01T00:00:00+00:00', 'OPEN', %s)",
            (admin_uid,),
        )
        conn.execute(
            "INSERT INTO activation_codes (id, batch_id, code_digest, "
            "digest_key_version, masked_code, status, issued_at) VALUES "
            "('legacy-code', 'legacy-batch', %s, 1, 'local masked code', "
            "'ISSUED', CURRENT_TIMESTAMP)",
            (
                compute_code_digest(
                    code, key=os.environ["VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY"].encode()
                ),
            ),
        )
    result = client.post(
        "/api/customer/activate",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "activation_code": code,
            "device_fingerprint": str(uuid4()),
            "device_name": "legacy local",
            "device_platform": "windows",
        },
    )
    assert result.status_code == 201, result.text
    return (
        {"Authorization": "Bearer " + result.json()["session_token"]},
        result.json()["user_id"],
        admin,
    )


def test_legacy_password_setup_preserves_wallet_and_original_session(
    operations_client, route_state
):
    client = operations_client
    headers, uid, _ = legacy_account(client, route_state)
    path = "/api/customer/account/password"
    info = client.get(path, headers=headers)
    assert info.status_code == 200, info.text
    assert info.json()["has_password"] is False
    retry = {**headers, "Idempotency-Key": str(uuid4())}
    payload = {"username": "legacy_named", "password": "sixsix"}
    result = client.post(path, headers=retry, json=payload)
    assert result.status_code == 200, result.text
    assert client.post(path, headers=retry, json=payload).json() == result.json()
    login = client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid4())},
        json={**payload, "device_fingerprint": str(uuid4())},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user_id"] == uid
    for auth in (headers, {"Authorization": "Bearer " + login.json()["session_token"]}):
        wallet = client.get("/api/customer/wallet", headers=auth)
        assert wallet.status_code == 200 and wallet.json()["available_credits"] == 100
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM activation_code_activations WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            == 1
        )
        stored = conn.execute(
            "SELECT password_hash, registration_source FROM users WHERE id = %s", (uid,)
        ).fetchone()
        assert stored[0] != "sixsix" and stored[1] == "activation_code"


def test_existing_password_and_api_key_cannot_use_legacy_setup(operations_client):
    client = operations_client
    headers, _ = account(client)
    result = client.post(
        "/api/customer/account/password",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"username": "changed_name", "password": "sixsix"},
    )
    assert result.status_code == 409, result.text
    key = mutation(client, "", headers).json()
    assert (
        client.get("/api/customer/account/password", headers=token_headers(key)).status_code == 401
    )


@pytest.mark.parametrize(
    "mode,numerator,denominator,expected",
    [("keep", 1, 1, 100), ("convert", 3, 2, 150), ("convert", 1, 3, 33)],
)
def test_legacy_balance_policy_preview_and_once_only_conversion(
    operations_client, route_state, mode, numerator, denominator, expected
):
    client = operations_client
    customer, uid, admin = legacy_account(client, route_state)
    policy_path = "/api/control/settings/legacy-credit-policy"
    policy = client.get(policy_path)
    assert policy.status_code == 200, policy.text
    saved = client.put(
        policy_path,
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": "legacy points policy",
            "expected_version": policy.json()["version"],
            "mode": mode,
            "numerator": numerator,
            "denominator": denominator,
        },
    )
    assert saved.status_code == 200, saved.text
    path = f"/api/control/customers/{uid}/credit-conversion"
    preview = client.get(path)
    assert preview.status_code == 200, preview.text
    assert preview.json()["after_credits"] == expected
    body = {
        "confirm": True,
        "reason": "legacy conversion approved",
        "expected_balance": 100,
        "expected_version": saved.json()["version"],
    }
    headers = {**admin, "Idempotency-Key": str(uuid4())}
    result = client.post(path, headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert client.post(path, headers=headers, json=body).json() == result.json()
    assert (
        client.post(path, headers={**admin, "Idempotency-Key": str(uuid4())}, json=body).status_code
        == 409
    )
    wallet = client.get("/api/customer/wallet", headers=customer)
    assert wallet.json()["available_credits"] == expected
    admin_ledger = client.get(f"/api/control/customers/{uid}/wallet-transactions")
    assert admin_ledger.status_code == 200, admin_ledger.text
    if expected != 100:
        assert any(row["type"] == "CONVERSION" for row in admin_ledger.json()["items"])
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT SUM(available_delta) FROM wallet_transactions WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            == expected
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM wallet_credit_conversions WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            == 1
        )


def test_new_account_is_not_eligible_for_legacy_conversion(operations_client, route_state):
    client = operations_client
    _, uid = account(client)
    admin_login(client, route_state)
    response = client.get(f"/api/control/customers/{uid}/credit-conversion")
    assert response.status_code == 409, response.text


@pytest.mark.parametrize(
    "blocker", ["reserved", "new_charge", "pending_order", "stale_balance", "stale_policy"]
)
def test_conversion_rejects_unsettled_mixed_or_stale_wallet(
    operations_client, route_state, blocker
):
    client = operations_client
    _, uid, admin = legacy_account(client, route_state)
    path = f"/api/control/customers/{uid}/credit-conversion"
    preview = client.get(path).json()
    with psycopg.connect(route_state) as conn:
        if blocker == "reserved":
            conn.execute("UPDATE wallets SET reserved_credits = 1 WHERE user_id = %s", (uid,))
        elif blocker == "pending_order":
            conn.execute(
                "INSERT INTO recharge_orders (id, user_id, merchant_order_no,"
                " provider, status, pricing_scope, "
                "base_unit_price_fen_snapshot, "
                "charged_unit_price_fen_snapshot, min_recharge_fen_snapshot, "
                "recharge_step_fen_snapshot, amount_fen, credits) VALUES (%s,"
                " %s, %s, 'zpay', 'PENDING', 'CUSTOMER_STANDARD', 1000, 1000,"
                " 10000, 1000, 20000, 20)",
                (str(uuid4()), uid, str(uuid4())),
            )
    if blocker == "new_charge":
        gift = client.post(
            f"/api/control/customers/{uid}/adjustments",
            headers={**admin, "Idempotency-Key": str(uuid4())},
            json={
                "confirm": True,
                "reason": "mixed wallet gift",
                "credits": 1,
                "source_document_type": "FREE_GRANT",
                "source_document_ref": "guard-gift",
            },
        )
        assert gift.status_code == 201, gift.text
    response = client.post(
        path,
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": "check conversion guard",
            "expected_balance": 99
            if blocker == "stale_balance"
            else 100 + int(blocker == "new_charge"),
            "expected_version": preview["policy"]["version"] + int(blocker == "stale_policy"),
        },
    )
    assert response.status_code == 409, response.text
    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (uid,)
        ).fetchone()[0] == 100 + int(blocker == "new_charge")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM wallet_credit_conversions WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            == 0
        )


def test_conversion_rejects_concurrent_second_request(operations_client, route_state):
    from concurrent.futures import ThreadPoolExecutor

    client = operations_client
    _, uid, admin = legacy_account(client, route_state)
    path = f"/api/control/customers/{uid}/credit-conversion"
    preview = client.get(path).json()
    body = {
        "confirm": True,
        "reason": "concurrent conversion check",
        "expected_balance": 100,
        "expected_version": preview["policy"]["version"],
    }

    def convert(_):
        return client.post(
            path, headers={**admin, "Idempotency-Key": str(uuid4())}, json=body
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(convert, range(2))) == [200, 409]
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM wallet_credit_conversions WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(psycopg.Error):
            conn.execute("DELETE FROM wallet_credit_conversions WHERE user_id = %s", (uid,))


def test_conversion_ignores_wechat_orders_past_the_settlement_window(
    operations_client, route_state
):
    """窗口外的微信订单不能追溯性污染钱包：付款已不可能、回调重试已耗尽，
    补单清扫已查过——它不再是「可补付订单」，不再阻塞积分转换。"""
    client = operations_client
    _, uid, _admin = legacy_account(client, route_state)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider, "
            "status, channel, pricing_scope, base_unit_price_fen_snapshot, "
            "charged_unit_price_fen_snapshot, min_recharge_fen_snapshot, "
            "recharge_step_fen_snapshot, amount_fen, credits, prepay_id, code_url, "
            "created_at) VALUES (%s, %s, %s, 'wechat_native', 'CLOSED', 'wxpay', "
            "'CUSTOMER_STANDARD', 1000, 1000, 10000, 1000, 20000, 20, "
            "'wx-prepay-lapsed', 'weixin://wxpay/bizpayurl?pr=lapsed', %s)",
            (str(uuid4()), uid, str(uuid4()), "2026-01-01 00:00:00+00"),
        )
        conn.commit()

    preview = client.get(f"/api/control/customers/{uid}/credit-conversion")
    assert preview.status_code == 200, preview.text
