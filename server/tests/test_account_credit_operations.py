"""Account operations use real administrator sessions and a PostgreSQL ledger."""

# ruff: noqa: F811
from uuid import uuid4

import psycopg
import pytest
from test_customer_pricing import (  # noqa: F401
    account,
    admin_login,
    client,
    mutation,
    pricing_client,
    registration_client,
    registration_dsn,
    route_state,
    token_headers,
)


@pytest.fixture()
def operations_client(pricing_client):
    from app.account_admin_routes import router as account_router
    from app.admin_customer_routes import router

    pricing_client.app.include_router(router)
    pricing_client.app.include_router(account_router)
    from app.account_migration_routes import router as migration_router

    pricing_client.app.include_router(migration_router)
    from app.credit_conversion import router as conversion_router

    pricing_client.app.include_router(conversion_router)
    return pricing_client


def test_h3_account_admin_contract_and_masking(operations_client, route_state):
    client = operations_client
    path = "/api/control/settings/h3-accounts"
    assert client.get(path).status_code == 401
    admin = admin_login(client, route_state)
    payload = {
        "name": "Synthetic account",
        "api_key": "synthetic-pool-secret",
        "concurrency_limit": 17,
        "enabled": True,
        "expected_version": 0,
        "confirm": True,
        "reason": "Configure test account",
    }
    headers = {**admin, "Idempotency-Key": str(uuid4())}
    oversized = client.put(
        path + "/test-a", headers=headers, json={**payload, "api_key": "x" * 8193}
    )
    assert oversized.status_code == 422 and "xxxx" not in oversized.text
    assert client.put(path + "/test-a", json=payload).status_code == 403
    saved = client.put(path + "/test-a", headers=headers, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["total_concurrency"] == 17
    assert payload["api_key"] not in saved.text
    replay = client.put(path + "/test-a", headers=headers, json=payload)
    assert replay.status_code == 200 and replay.json() == saved.json()
    payload.update(api_key="", expected_version=1, concurrency_limit=9)
    assert (
        client.put(
            path + "/test-a", headers={**admin, "Idempotency-Key": str(uuid4())}, json=payload
        ).json()["total_concurrency"]
        == 9
    )
    with psycopg.connect(route_state) as raw:
        audit = " ".join(row[0] for row in raw.execute("SELECT metadata_json FROM audit_logs"))
        assert "synthetic-pool-secret" not in audit
        raw.execute("UPDATE users SET role='observer' WHERE username='price_admin'")
    assert (
        client.put(
            path + "/test-a", headers={**admin, "Idempotency-Key": str(uuid4())}, json=payload
        ).status_code
        == 401
    )


def test_registered_account_is_listed_and_exported(operations_client, route_state):
    client = operations_client
    _, uid = account(client)
    admin_login(client, route_state)
    result = client.get("/api/control/customers", params={"username": "center_user"})
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 1
    row = result.json()["items"][0]
    assert row["user_id"] == uid and row["activation_code"] == "账号注册"
    exported = client.get("/api/control/customers.csv", params={"username": "center_user"})
    assert exported.status_code == 200 and "center_user" in exported.text


@pytest.mark.parametrize("source", ["FREE_GRANT", "CREDIT_COMPENSATION"])
def test_gift_and_compensation_are_zero_revenue_and_replay(operations_client, route_state, source):
    client = operations_client
    customer, uid = account(client)
    headers = {**admin_login(client, route_state), "Idempotency-Key": str(uuid4())}
    payload = {
        "confirm": True,
        "reason": "Internal approval only",
        "credits": 123,
        "source_document_type": source,
        "source_document_ref": "LOCAL-APPROVAL",
    }
    path = f"/api/control/customers/{uid}/adjustments"
    first = client.post(path, headers=headers, json=payload)
    assert first.status_code == 201, first.text
    assert client.post(path, headers=headers, json=payload).json() == first.json()
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT available_credits FROM wallets WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            == 123
        )
        order = conn.execute(
            "SELECT amount_fen, pricing_scope FROM recharge_orders WHERE user_id = %s", (uid,)
        ).fetchone()
        assert order == (0, "CUSTOMER_STANDARD")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM admin_adjustments WHERE target_user_id = %s", (uid,)
            ).fetchone()[0]
            == 1
        )
    ledger = client.get("/api/customer/wallet/transactions", headers=customer)
    assert ledger.status_code == 200, ledger.text
    assert ledger.json()["items"][0]["credit_source"] == source
    assert "Internal approval" not in ledger.text and "LOCAL-APPROVAL" not in ledger.text


def test_native_payment_settings_work_with_admin_cookie(operations_client, route_state):
    client = operations_client
    admin = admin_login(client, route_state)
    path = "/api/control/settings/customer-payments"
    loaded = client.get(path)
    assert loaded.status_code == 200, loaded.text
    assert set(loaded.json()) == {
        "billing",
        "zpay",
        "wechat_native",
        "active_provider",
        "deployment",
    }
    payload = {
        "confirm": True,
        "reason": "local billing setup",
        "internal_base_unit_price_fen": 1000,
        "oral_unit_price_fen": 100,
        "min_recharge_fen": 20000,
        "recharge_step_fen": 1000,
    }
    updated = client.patch(
        path + "/billing", json=payload, headers={**admin, "Idempotency-Key": str(uuid4())}
    )
    assert updated.status_code == 200, updated.text
    assert client.get(path).json()["billing"]["min_recharge_fen"] == 20000


def test_admin_summary_never_exposes_secrets(operations_client, route_state):
    client = operations_client
    customer, uid = account(client)
    key = mutation(client, "", customer, {"label": "team-script"}).json()
    admin_login(client, route_state)
    response = client.get(f"/api/control/customers/{uid}/account-summary")
    assert response.status_code == 200, response.text
    assert response.json()["tokens"][0]["label"] == "team-script"
    assert key["plaintext"] not in response.text and "key_digest" not in response.text
    assert "password_hash" not in response.text


@pytest.mark.parametrize("receipt", ["valid", "unpaid", "mismatch", "revoked_admin"])
def test_original_order_reconcile_then_callback_credits_once(
    operations_client, route_state, monkeypatch, receipt
):
    from app import account_admin_routes
    from app.db_portable import BusinessConnection
    from app.payment_provider import DeploymentConfig, MerchantConfig, OrderQueryResult
    from app.zpay_payments import confirm_recharge_payment

    client = operations_client
    _, uid = account(client)
    admin = admin_login(client, route_state)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider, status, "
            "pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits) "
            "VALUES ('uc4-order', %s, 'UC4-ORDER', 'zpay', 'PENDING', 'CUSTOMER_STANDARD', "
            "1000, 1000, 10000, 1000, 20000, 20)",
            (uid,),
        )

    class Gateway:
        def load_merchant_config(self, conn):
            return MerchantConfig("zpay", {}, ("alipay",))

        def load_deployment_config(self):
            return DeploymentConfig("", "")

        def query_order(self, **kwargs):
            if receipt == "revoked_admin":
                with psycopg.connect(route_state) as raw:
                    raw.execute("UPDATE admin_sessions SET revoked_at = CURRENT_TIMESTAMP")
            return OrderQueryResult(
                receipt != "unpaid",
                "UC4-ORDER",
                "UC4-TRADE",
                19999 if receipt == "mismatch" else 20000,
                "alipay",
                "verified-local",
            )

    monkeypatch.setattr(account_admin_routes, "get_payment_provider", lambda name: Gateway())
    path = f"/api/control/customers/{uid}/recharge-orders/UC4-ORDER/reconcile"
    result = client.post(
        path,
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={"confirm": True, "reason": "original receipt check"},
    )
    if receipt != "valid":
        assert result.status_code in (401, 403, 409), result.text
        with psycopg.connect(route_state) as raw:
            assert (
                raw.execute(
                    "SELECT available_credits FROM wallets WHERE user_id = %s", (uid,)
                ).fetchone()[0]
                == 0
            )
            assert (
                raw.execute("SELECT status FROM recharge_orders WHERE id = 'uc4-order'").fetchone()[
                    0
                ]
                == "PENDING"
            )
        return
    assert result.status_code == 200, result.text
    with psycopg.connect(route_state) as raw:
        confirm_recharge_payment(
            BusinessConnection.postgres(raw),
            merchant_order_no="UC4-ORDER",
            provider_trade_no="UC4-TRADE",
            amount_fen=20000,
            channel="alipay",
            source_digest="callback",
            allowed_channels=("alipay",),
        )
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            == 20
        )
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM wallet_transactions WHERE recharge_order_id = 'uc4-order'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("role", ["customer", "auditor"])
def test_non_writers_cannot_mutate_account_credits(operations_client, route_state, role):
    client = operations_client
    customer, uid = account(client)
    admin = admin_login(client, route_state)
    with psycopg.connect(route_state) as conn:
        conn.execute("UPDATE users SET role = %s WHERE username = 'price_admin'", (role,))
    response = client.post(
        f"/api/control/customers/{uid}/adjustments",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": "must deny",
            "credits": 10,
            "source_document_type": "FREE_GRANT",
            "source_document_ref": "denied",
        },
    )
    assert response.status_code in (401, 403), response.text
    assert client.get("/api/customer/wallet", headers=customer).json()["available_credits"] == 0
    for suffix, payload in (
        ("provider", {"active_provider": "wechat_native"}),
        ("wechat-native", {"config": {"appid": "denied"}}),
    ):
        denied = client.patch(
            "/api/control/settings/customer-payments/" + suffix,
            headers={**admin, "Idempotency-Key": str(uuid4())},
            json={"confirm": True, "reason": "must deny", **payload},
        )
        assert denied.status_code in (401, 403), denied.text


@pytest.mark.parametrize("credits", [1.5, True, 0, -1])
def test_admin_credit_input_is_strict_positive_integer(operations_client, route_state, credits):
    client = operations_client
    _, uid = account(client)
    admin = admin_login(client, route_state)
    response = client.post(
        f"/api/control/customers/{uid}/adjustments",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": "integer check",
            "credits": credits,
            "source_document_type": "FREE_GRANT",
            "source_document_ref": "input",
        },
    )
    assert response.status_code in (400, 422), response.text


@pytest.mark.parametrize("suffix", ["recharge-orders", "wallet-transactions"])
def test_native_account_ledger_reads_without_control_proxy(operations_client, route_state, suffix):
    client = operations_client
    _, uid = account(client)
    admin = admin_login(client, route_state)
    grant = client.post(
        f"/api/control/customers/{uid}/adjustments",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": "native account ledger",
            "credits": 25,
            "source_document_type": "FREE_GRANT",
            "source_document_ref": "native-read",
        },
    )
    assert grant.status_code == 201, grant.text
    result = client.get(f"/api/control/customers/{uid}/{suffix}", params={"limit": 3, "offset": 0})
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["user_id"] == uid
    assert "password_hash" not in result.text


def test_concurrent_grant_payment_and_token_consumption_conserve_account_ledger(
    operations_client, route_state
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from test_customer_pricing import reserve, seed_tasks

    from app.db_portable import BusinessConnection
    from app.internal_billing import finalize_internal_billing
    from app.zpay_payments import confirm_recharge_payment

    client = operations_client
    customer, uid = account(client)
    key = mutation(client, "", customer, {"label": "concurrent worker"}).json()
    admin = admin_login(client, route_state)
    seed_tasks(route_state, uid, ["concurrent-credit-task"], balance=0)

    def grant(credits, reference):
        result = client.post(
            f"/api/control/customers/{uid}/adjustments",
            headers={**admin, "Idempotency-Key": str(uuid4())},
            json={
                "confirm": True,
                "reason": "concurrent account accounting",
                "credits": credits,
                "source_document_type": "FREE_GRANT",
                "source_document_ref": reference,
            },
        )
        assert result.status_code == 201, result.text

    grant(100, "opening-balance")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider, status, "
            "pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits) "
            "VALUES ('concurrent-order', %s, 'CONCURRENT-ORDER', 'zpay', 'PENDING', "
            "'CUSTOMER_STANDARD', 1000, 1000, 10000, 1000, 20000, 20)",
            (uid,),
        )

    barrier = Barrier(3, timeout=15)

    def change(kind):
        barrier.wait()
        if kind == "gift":
            grant(25, "concurrent-gift")
        elif kind == "payment":
            with psycopg.connect(route_state) as raw:
                confirm_recharge_payment(
                    BusinessConnection.postgres(raw),
                    merchant_order_no="CONCURRENT-ORDER",
                    provider_trade_no="CONCURRENT-TRADE",
                    amount_fen=20000,
                    channel="alipay",
                    source_digest="local-verified-callback",
                    allowed_channels=("alipay",),
                )
        else:
            reserve(route_state, uid, key["id"], "concurrent-credit-task", 20)
            with psycopg.connect(route_state) as raw:
                raw.execute(
                    "UPDATE generation_tasks SET status = 'SUCCEEDED', "
                    "actual_output_seconds=20, archive_status = "
                    "'DIRECT', provider_result_url = 'https://example.com/test.mp4' "
                    "WHERE id = 'concurrent-credit-task'"
                )
                finalize_internal_billing(
                    BusinessConnection.postgres(raw),
                    task_id="concurrent-credit-task",
                    outcome="success",
                )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(change, ("gift", "payment", "token")))
    with psycopg.connect(route_state) as raw:
        assert raw.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s", (uid,)
        ).fetchone() == (85, 0)
        assert raw.execute(
            "SELECT SUM(available_delta), SUM(reserved_delta) FROM wallet_transactions "
            "WHERE user_id = %s",
            (uid,),
        ).fetchone() == (85, 0)
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM wallet_transactions WHERE user_id = %s AND type = 'CHARGE'",
                (uid,),
            ).fetchone()[0]
            == 3
        )
    listed = client.get("/api/customer/api-keys", headers=customer).json()["items"]
    assert next(item for item in listed if item["id"] == key["id"])["total_consumed_credits"] == 60


def test_default_payment_saves_merchant_atomically_before_callback_is_ready(
    operations_client, route_state, monkeypatch
):
    from app.db_portable import BusinessConnection
    from app.settings import SettingsRepository

    client = operations_client
    admin = admin_login(client, route_state)
    path = "/api/control/settings/customer-payments"
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    payload = {
        "confirm": True,
        "reason": "保存默认通道",
        "active_provider": "zpay",
        "zpay": {"pid": "local-test-pid", "key": "local-test-key", "enabled_channels": ["wxpay"]},
    }
    headers = {**admin, "Idempotency-Key": str(uuid4())}
    saved = client.patch(path + "/provider", json=payload, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["active_provider"] == "zpay"
    assert saved.json()["deployment"]["ready"] is False
    assert "local-test-key" not in saved.text
    assert client.patch(path + "/provider", json=payload, headers=headers).json() == saved.json()
    assert client.get(path).json()["deployment"]["ready"] is False

    with psycopg.connect(route_state) as raw:
        repo = SettingsRepository(BusinessConnection.postgres(raw))
        assert repo.load_zpay_config()["pid"] == "local-test-pid"
        assert repo.load_zpay_config()["key"] == "local-test-key"
        before = raw.execute("SELECT count(*) FROM audit_logs").fetchone()[0]
    # A malformed merchant change does not partly update settings or the default.
    invalid = {**payload, "zpay": {**payload["zpay"], "pid": "   "}}
    failed = client.patch(
        path + "/provider", json=invalid, headers={**admin, "Idempotency-Key": str(uuid4())}
    )
    assert failed.status_code == 422
    with psycopg.connect(route_state) as raw:
        repo = SettingsRepository(BusinessConnection.postgres(raw))
        assert repo.load_zpay_config()["pid"] == "local-test-pid"
        assert raw.execute("SELECT count(*) FROM audit_logs").fetchone()[0] == before

    # Empty secret input retains the saved key; missing callback still blocks checkout.
    kept = client.patch(
        path + "/provider",
        json={**payload, "zpay": {**payload["zpay"], "key": ""}},
        headers={**admin, "Idempotency-Key": str(uuid4())},
    )
    assert kept.status_code == 200
    customer, _uid = account(client)
    checkout = client.post(
        "/api/customer/recharge-orders",
        headers={**customer, "Idempotency-Key": str(uuid4())},
        json={"amount_fen": 10000},
    )
    assert checkout.status_code == 503
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://payments.example.test")
    assert client.get(path).json()["deployment"]["ready"] is True


def test_payment_channel_setup_preserves_secrets_and_order_provider(
    operations_client, route_state, monkeypatch
):
    import base64
    import secrets

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app import recharge_routes
    from app.db_portable import BusinessConnection
    from app.payment_provider import (
        DeploymentConfig,
        MerchantConfig,
        PaymentCodeResult,
        PaymentFormResult,
    )
    from app.settings import SettingsRepository

    client = operations_client
    customer, uid = account(client)
    admin = admin_login(client, route_state)
    path = "/api/control/settings/customer-payments"
    assert client.get(path).json()["active_provider"] == "zpay"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://payments.example.test")

    def patch(suffix, payload, key=None):
        return client.patch(
            path + suffix,
            headers={**admin, "Idempotency-Key": key or str(uuid4())},
            json={"confirm": True, "reason": "local payment verification", **payload},
        )

    assert patch("/provider", {"active_provider": "wechat_native"}).status_code == 422
    private_key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    api_key = secrets.token_hex(16)
    config = {
        "appid": "test-app",
        "mchid": "test-merchant",
        "serial_no": "0123ABCD",
        "api_v3_key": api_key,
        "private_key": private_key,
    }
    saved = patch("/wechat-native", {"config": config})
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert api_key not in saved.text and "BEGIN PRIVATE KEY" not in saved.text
    kept = patch(
        "/wechat-native", {"config": {"appid": "updated-app", "api_v3_key": "", "private_key": ""}}
    )
    assert kept.status_code == 200, kept.text
    with psycopg.connect(route_state) as raw:
        repo = SettingsRepository(BusinessConnection.postgres(raw))
        retained = repo.load_wechat_native_config()
        assert retained["private_key"] == private_key.strip() and retained["api_v3_key"] == api_key
        assert retained["appid"] == "updated-app"
        encrypted = raw.execute(
            "SELECT encrypted_config FROM provider_settings WHERE provider='wechat_native'"
        ).fetchone()[0]
        assert api_key not in encrypted and private_key not in encrypted

    calls = []

    class FakeGateway:
        def __init__(self, name):
            self.name = name

        def load_merchant_config(self, conn):
            return MerchantConfig(provider=self.name, raw={}, allowed_channels=("wxpay",))

        def load_deployment_config(self):
            return DeploymentConfig(
                notify_url="https://payments.example.test/notify", return_url=""
            )

        def create_payment_form(self, **kwargs):
            assert self.name == "zpay"
            return PaymentFormResult(
                gateway_url="https://payments.example.test/pay", method="POST", form_fields={}
            )

        def create_payment_code(self, **kwargs):
            calls.append((self.name, kwargs["merchant_order_no"]))
            url = (
                "weixin://wxpay/bizpayurl?pr=local-test"
                if self.name == "wechat_native"
                else "https://payments.example.test/qr.png"
            )
            return PaymentCodeResult(qr_image_url=url, payment_url=url, provider_order_no=None)

    monkeypatch.setattr(recharge_routes, "get_payment_provider", FakeGateway)
    order_path = "/api/customer/recharge-orders"

    def order(key):
        response = client.post(
            order_path, headers={**customer, "Idempotency-Key": key}, json={"amount_fen": 10000}
        )
        assert response.status_code == 201, response.text
        return response.json()

    old_key = str(uuid4())
    old = order(old_key)
    switch_key = str(uuid4())
    switched = patch("/provider", {"active_provider": "wechat_native"}, switch_key)
    assert switched.status_code == 200, switched.text
    assert (
        patch("/provider", {"active_provider": "wechat_native"}, switch_key).json()
        == switched.json()
    )
    assert order(old_key) == old
    new = order(str(uuid4()))
    assert new["gateway_url"] == "" and new["form_fields"] == {}
    blocked = patch("/wechat-native", {"config": {"mchid": "different-merchant"}})
    assert blocked.status_code == 422
    with psycopg.connect(route_state) as raw:
        assert (
            SettingsRepository(BusinessConnection.postgres(raw)).load_wechat_native_config()[
                "mchid"
            ]
            == "test-merchant"
        )

    assert (
        client.post(
            order_path + "/" + old["order_no"] + "/payment-code", headers=customer
        ).status_code
        == 200
    )
    qr = client.post(order_path + "/" + new["order_no"] + "/payment-code", headers=customer)
    assert qr.status_code == 200, qr.text
    assert qr.json()["payment_url"].startswith("weixin://")
    assert base64.b64decode(qr.json()["qr_image_url"].split(",", 1)[1]).startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    replay = client.post(order_path + "/" + new["order_no"] + "/payment-code", headers=customer)
    assert replay.json() == qr.json()
    assert calls == [("zpay", old["order_no"]), ("wechat_native", new["order_no"])]
    # Closing in the customer UI does not close the gateway payment. A late
    # callback can still settle this order and must retain its merchant keys.
    closed = client.delete(order_path + "/" + new["order_no"], headers=customer)
    assert closed.status_code == 204, closed.text
    blocked_after_close = patch(
        "/wechat-native",
        {"config": {"appid": "replacement-app", "mchid": "different-merchant"}},
    )
    assert blocked_after_close.status_code == 422, blocked_after_close.text
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT status FROM recharge_orders WHERE merchant_order_no=%s",
                (new["order_no"],),
            ).fetchone()[0]
            == "CLOSED"
        )
        assert (
            SettingsRepository(BusinessConnection.postgres(raw)).load_wechat_native_config()
            == retained
        )
        rows = raw.execute(
            "SELECT merchant_order_no, provider FROM recharge_orders WHERE user_id=%s", (uid,)
        ).fetchall()
        assert dict(rows) == {old["order_no"]: "zpay", new["order_no"]: "wechat_native"}
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='payment.provider.update'"
            ).fetchone()[0]
            == 1
        )
        audit = " ".join(row[0] for row in raw.execute("SELECT metadata_json FROM audit_logs"))
        assert api_key not in audit and "BEGIN PRIVATE KEY" not in audit


def test_merchant_identity_can_change_once_settlement_window_has_passed(
    operations_client, route_state
):
    """口径：支付窗（2h）+ 微信通知重推上限（24h）之内，订单阻塞商户身份变更；
    窗口之外不再阻塞——付款已不可能、回调重试已耗尽、补单清扫已查过，变更
    无从搁浅任何资金。原判据把 CLOSED 永久计入，只会把商户号锁死。"""
    import secrets

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    client = operations_client
    admin = admin_login(client, route_state)
    path = "/api/control/settings/customer-payments"
    private_key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    config = {
        "appid": "drain-app",
        "mchid": "drain-merchant",
        "serial_no": "0123ABCD",
        "api_v3_key": secrets.token_hex(16),
        "private_key": private_key,
    }
    saved = client.patch(
        path + "/wechat-native",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={"confirm": True, "reason": "drain window setup", "config": config},
    )
    assert saved.status_code == 200, saved.text

    # 一笔三天前创建、已关闭的微信订单：远在支付窗与回调重推期之外。
    with psycopg.connect(route_state) as raw:
        uid = raw.execute("SELECT id FROM users ORDER BY created_at LIMIT 1").fetchone()[0]
        raw.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider, "
            "status, channel, pricing_scope, base_unit_price_fen_snapshot, "
            "charged_unit_price_fen_snapshot, min_recharge_fen_snapshot, "
            "recharge_step_fen_snapshot, amount_fen, credits, prepay_id, code_url, "
            "created_at) VALUES (%s, %s, %s, 'wechat_native', 'CLOSED', 'wxpay', "
            "'CUSTOMER_STANDARD', 1000, 1000, 10000, 1000, 20000, 20, "
            "'wx-prepay-drained', 'weixin://wxpay/bizpayurl?pr=drained', %s)",
            (str(uuid4()), uid, str(uuid4()), "2026-01-01 00:00:00+00"),
        )
        raw.commit()

    switched = client.patch(
        path + "/wechat-native",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": "rotate merchant after drain",
            "config": {**config, "appid": "next-app", "mchid": "next-merchant"},
        },
    )
    assert switched.status_code == 200, switched.text
    from app.db_portable import BusinessConnection
    from app.settings import SettingsRepository

    with psycopg.connect(route_state) as raw:
        assert (
            SettingsRepository(BusinessConnection.postgres(raw)).load_wechat_native_config()[
                "mchid"
            ]
            == "next-merchant"
        )


def test_wechat_credential_self_check_reports_without_saving(
    operations_client, route_state, monkeypatch
):
    """自检按钮：真实调一次微信验证三件套（不落库、不动共享缓存），配错在
    这里暴露而不是在第一个真实客户下单时。"""
    import secrets as secrets_module

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app import wechat_native_client as wnc

    client = operations_client
    admin = admin_login(client, route_state)
    path = "/api/control/settings/customer-payments/wechat-native/self-check"

    # 未配置：不触网即返回失败
    probe = client.post(path, headers={**admin, "Idempotency-Key": str(uuid4())})
    assert probe.status_code == 200, probe.text
    body = probe.json()
    assert body["ok"] is False and body["code"] == "WECHAT_CONFIG_INVALID"

    private_key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    saved = client.patch(
        "/api/control/settings/customer-payments/wechat-native",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": "self-check setup",
            "config": {
                "appid": "check-app",
                "mchid": "check-merchant",
                "serial_no": "CHECKSERIAL01",
                "api_v3_key": secrets_module.token_hex(16),
                "private_key": private_key,
            },
        },
    )
    assert saved.status_code == 200, saved.text

    calls: list[str] = []

    class StubManager:
        def check_credentials(self, merchant):
            calls.append(merchant.mchid)
            if calls[-1] == "bad-merchant":
                raise wnc.WeChatNativeError(
                    "WeChat API request failed (HTTP 401 code=SIGN_ERROR message=签名错误)"
                )
            return 2

    monkeypatch.setattr(wnc, "PlatformCertificateManager", StubManager)

    probe = client.post(path, headers={**admin, "Idempotency-Key": str(uuid4())})
    assert probe.status_code == 200, probe.text
    body = probe.json()
    assert body == {
        "ok": True,
        "code": None,
        "message": "商户凭据有效：签名被微信接受，平台证书解密成功。",
        "platform_certificates": 2,
    }, body

    # 微信侧拒绝（如序列号与私钥不匹配）：错误体里的微信错误码原样透出
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE provider_settings SET encrypted_config = encrypted_config WHERE "
            "provider='wechat_native'"
        )
    from app.db_portable import BusinessConnection
    from app.settings import SettingsRepository

    with psycopg.connect(route_state) as raw:
        admin_uid = raw.execute(
            "SELECT id FROM users WHERE role='admin' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
    with psycopg.connect(route_state) as raw:
        repo = SettingsRepository(BusinessConnection.postgres(raw))
        config = repo.load_wechat_native_config()
        repo.save_wechat_native_config(
            {**config, "mchid": "bad-merchant"}, actor_user_id=str(admin_uid)
        )

    probe = client.post(path, headers={**admin, "Idempotency-Key": str(uuid4())})
    assert probe.status_code == 200, probe.text
    body = probe.json()
    assert body["ok"] is False and body["code"] == "WECHAT_SELF_CHECK_FAILED"
    assert "SIGN_ERROR" in body["message"], body
