"""Credit prices use integer arithmetic and immutable acceptance snapshots."""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from test_customer_center import (  # noqa: F401
    account,
    client,
    mutation,
    registration_client,
    registration_dsn,
    route_state,
    token_headers,
)

from app.customer_pricing import PricingConfig, recharge_credits, task_credits
from app.db_portable import BusinessConnection
from app.internal_billing import (
    InsufficientCreditsError,
    finalize_internal_billing,
    reserve_internal_billing,
)


@pytest.fixture()
def pricing_client(client, route_state, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    from app.admin_auth_routes import router as admin_router
    from app.customer_pricing_routes import router
    from app.generation_routes import router as generation_router
    from app.independent_routes import router as independent_router

    client.app.include_router(router)
    client.app.include_router(admin_router)
    client.app.include_router(independent_router)
    client.app.include_router(generation_router)
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE customer_credit_pricing SET version = 0, config_json = NULL")
        raw.execute("INSERT INTO runtime_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        for subject, unit, resolution in (
            ("video_generation_2k", "second", "2K"),
            ("context_ir", "call", None),
        ):
            raw.execute(
                "INSERT INTO operation_cost_rates (subject, kind, unit, resolution, "
                "unit_price_fen) VALUES (%s, 'upstream_cost', %s, %s, 5) ON CONFLICT "
                "(subject) DO NOTHING",
                (subject, unit, resolution),
            )
    return client


def admin_login(client, dsn):
    from uuid import uuid4

    from app.admin_auth_routes import hash_admin_password

    uid = str(uuid4())
    with psycopg.connect(dsn) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, "
            "'price_admin', 'Price Admin', 'admin')",
            (uid,),
        )
        raw.execute(
            "INSERT INTO admin_password_credentials (user_id, password_hash, "
            "credential_version, password_changed_at) VALUES (%s, %s, 1, "
            "CURRENT_TIMESTAMP)",
            (uid, hash_admin_password("Price-test-2026!")),
        )
    response = client.post(
        "/api/control/admin/session/password",
        json={"username": "price_admin", "password": "Price-test-2026!"},
    )
    assert response.status_code == 201, response.text
    return {"X-Admin-CSRF": response.json()["csrf_token"]}


def config(**changes):
    return PricingConfig.model_validate(
        {"video_768p": 3, "video_2k": 7, "oral": 11, "points_per_yuan": 100, **changes}
    )


def test_quote_is_sum_of_individual_task_reservations():
    assert task_credits(config(), "video_768p", 6, 4) == 72
    assert task_credits(config(), "video_2k", 6, 4) == 168
    assert task_credits(config(), "oral", 1, 2) == 22


def test_recharge_uses_integer_fen_and_never_float_rounding():
    assert recharge_credits(config(), 10001) == 10001
    assert recharge_credits(config(points_per_yuan=3), 101) == 3
    with pytest.raises(ValueError):
        recharge_credits(config(points_per_yuan=1), 1)


def test_discount_rounds_each_task_before_multiplying_quantity():
    discounted = config(discount_basis_points=8500, consumption_rounding="ceil")
    assert task_credits(discounted, "video_768p", 5, 2) == 26
    assert task_credits(discounted, "oral", 1) == 10
    assert (
        task_credits(config(discount_basis_points=8500, consumption_rounding="floor"), "oral", 1)
        == 9
    )
    assert task_credits(config(discount_basis_points=1), "oral", 1) == 1
    assert recharge_credits(discounted, 10000) == 10000


@pytest.mark.parametrize(
    "changes",
    [
        {"video_768p": -1},
        {"oral": -1},
        {"points_per_yuan": 0},
        {"oral": 1.5},
        {"oral": True},
        {"extra": 1},
    ],
)
def test_invalid_prices_rejected(changes):
    with pytest.raises(ValueError):
        config(**changes)


def test_overflow_and_invalid_quantity_rejected():
    with pytest.raises(ValueError):
        task_credits(config(video_2k=1_000_000), "video_2k", 15, 1000)
    with pytest.raises(ValueError):
        task_credits(config(), "oral", 1, 0)
    with pytest.raises(ValueError):
        recharge_credits(config(points_per_yuan=1_000_000), 2_147_483_647)


def seed_tasks(dsn, user_id, names, balance=100):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES ('price_project', %s, 'price')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
            "idempotency_key, request_hash, request_snapshot_json) VALUES "
            "('price_batch', 'price_project', %s, 'price_batch', 'hash', '{}')",
            (user_id,),
        )
        for name in names:
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, "
                "model, status, prompt_snapshot_json) VALUES (%s, 'price_batch', 'I2V', "
                "'metaso', 'MiniMax-H3', 'PENDING', '{\"resolution\":\"768P\"}')",
                (name,),
            )
        conn.execute(
            "UPDATE wallets SET available_credits = %s WHERE user_id = %s", (balance, user_id)
        )
        conn.execute(
            "UPDATE customer_credit_pricing SET version = 1, config_json = %s",
            (config().model_dump_json(),),
        )
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES ('video_768p',true,3)"
        )


def reserve(dsn, uid, key_id, task, seconds):
    with psycopg.connect(dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        conn.api_key_id = key_id
        conn.auth_source = "api_key"
        return reserve_internal_billing(conn, user_id=uid, task_id=task, seconds=seconds)


def test_rotation_inflight_keeps_original_price_and_account(client, route_state):
    headers, uid = account(client)
    a = mutation(client, "", headers, {"label": "A"}).json()
    b = mutation(client, "", headers, {"label": "B"}).json()
    seed_tasks(route_state, uid, ["task_a", "task_b"])
    reserve(route_state, uid, a["id"], "task_a", 10)
    rotated = mutation(client, "/" + a["id"] + "/rotate", headers).json()
    assert rotated["token_group_id"] == a["id"]
    assert client.get("/api/customer/wallet", headers=token_headers(a)).status_code == 401
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE customer_credit_pricing SET version = 2, config_json = %s",
            (config(video_768p=9).model_dump_json(),),
        )
        raw.execute(
            "UPDATE billing_tariffs SET unit_credits=9,version=2 WHERE service='video_768p'"
        )
        raw.execute(
            "UPDATE generation_tasks SET status = 'SUCCEEDED', actual_output_seconds=10, "
            "archive_status = "
            "'DIRECT', provider_result_url = 'https://example.com/test.mp4' WHERE id = "
            "'task_a'"
        )
        conn = BusinessConnection.postgres(raw)
        finalize_internal_billing(conn, task_id="task_a", outcome="success")
        finalize_internal_billing(conn, task_id="task_a", outcome="success")
    reserve(route_state, uid, b["id"], "task_b", 2)
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task_b'")
        finalize_internal_billing(
            BusinessConnection.postgres(raw), task_id="task_b", outcome="failed"
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s", (uid,)
            ).fetchone()
        ) == (70, 0)
        ledger = raw.execute(
            "SELECT type, reserved_delta, api_key_id, auth_source, pricing_snapshot_json "
            "FROM wallet_transactions WHERE task_id = 'task_a' ORDER BY type"
        ).fetchall()
        assert [r[1] for r in ledger] == [30, -30]
        assert all(r[2] == a["id"] and r[3] == "api_key" for r in ledger)
        assert ledger[0][4] == ledger[1][4]
    keys = client.get("/api/customer/api-keys", headers=headers).json()["items"]
    assert next(k for k in keys if k["id"] == rotated["id"])["total_consumed_credits"] == 30
    filtered = client.get(
        "/api/customer/wallet/transactions",
        headers=headers,
        params={"token_group_id": a["id"], "transaction_type": "SETTLE"},
    ).json()
    assert filtered["total"] == 1 and filtered["items"][0]["credential_version"] == 1
    assert filtered["items"][0]["credit_price_version"] == 1
    other_headers, other_uid = account(client, "other_price_account")
    other_key = mutation(client, "", other_headers, {"label": "other"}).json()
    assert (
        client.get(
            "/api/customer/wallet/transactions",
            headers=other_headers,
            params={"token_group_id": a["id"]},
        ).json()["total"]
        == 0
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "UPDATE wallet_transactions SET api_key_id = %s WHERE task_id = 'task_a'",
                (other_key["id"],),
            )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "UPDATE customer_api_keys SET token_group_id = %s, credential_version = "
                "99, revoked_at = CURRENT_TIMESTAMP WHERE id = %s",
                (a["id"], other_key["id"]),
            )


def test_two_tokens_share_atomic_wallet_without_overdraft(client, route_state):
    headers, uid = account(client)
    keys = [mutation(client, "", headers, {"label": n}).json()["id"] for n in ("A", "B")]
    seed_tasks(route_state, uid, ["race_a", "race_b"], balance=100)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE customer_credit_pricing SET config_json = %s",
            (config(video_768p=4).model_dump_json(),),
        )

    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE billing_tariffs SET unit_credits=4 WHERE service='video_768p'")

    def attempt(args):
        key, task = args
        try:
            reserve(route_state, uid, key, task, 20)
            return True
        except InsufficientCreditsError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, zip(keys, ("race_a", "race_b"))))
    assert sorted(results) == [False, True]
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s", (uid,)
            ).fetchone()
        ) == (20, 80)


def test_admin_price_publication_drives_authenticated_api_billing(pricing_client, route_state):
    from uuid import uuid4

    client = pricing_client
    headers, uid = account(client)
    key = mutation(client, "", headers, {"label": "generation"}).json()
    admin_headers = admin_login(client, route_state)
    payload = {
        "expected_version": 0,
        "config": config().model_dump(),
        "reason": "测试客户积分价格",
        "confirm": True,
    }
    write_headers = {**admin_headers, "Idempotency-Key": str(uuid4())}
    path = "/api/control/settings/customer-pricing"
    result = client.put(path, headers=write_headers, json=payload)
    assert result.status_code == 200, result.text
    assert client.put(path, headers=write_headers, json=payload).json() == result.json()
    assert (
        client.put(
            path, headers={**admin_headers, "Idempotency-Key": str(uuid4())}, json=payload
        ).status_code
        == 409
    )
    assert (
        client.put(path, headers={"Idempotency-Key": str(uuid4())}, json=payload).status_code == 403
    )
    public = client.get("/api/customer/pricing", headers=token_headers(key))
    assert public.status_code == 200
    expected_public = result.json()
    expected_public["prices"] = [
        price for price in expected_public["prices"] if price["configurable"]
    ]
    assert public.json() == expected_public
    assert "upstream" not in public.text and "Price Admin" not in public.text
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE wallets SET available_credits = 100 WHERE user_id = %s", (uid,))
    quote = client.get(
        "/api/generation/price-quote?resolution=2K&duration_seconds=6&quantity=1",
        headers=token_headers(key),
    )
    assert quote.status_code == 200, quote.text
    assert quote.json()["estimated_credits"] == 42
    assert (
        client.get(
            "/api/generation/price-quote?quantity=2147483647", headers=token_headers(key)
        ).status_code
        == 422
    )
    request = {
        "mode": "t2v",
        "prompt_text": "乡墅庭院",
        "ratio": "16:9",
        "output_duration_seconds": 6,
        "resolution": "2K",
        "quantity": 1,
        "provider": "fake_h3",
        "idempotency_key": str(uuid4()),
    }
    created = client.post("/api/independent/video-tasks", headers=token_headers(key), json=request)
    assert created.status_code == 201, created.text
    assert (
        client.post(
            "/api/independent/video-tasks", headers=token_headers(key), json=request
        ).status_code
        == 201
    )
    request["idempotency_key"] = str(uuid4())
    software = client.post("/api/independent/video-tasks", headers=headers, json=request)
    assert software.status_code == 201, software.text
    with psycopg.connect(route_state) as raw:
        row = raw.execute(
            "SELECT user_id, api_key_id, reserved_delta, auth_source FROM "
            "wallet_transactions WHERE type = 'RESERVE' AND auth_source = 'api_key'"
        ).fetchone()
        assert tuple(row) == (uid, key["id"], 42, "api_key")
        software_row = raw.execute(
            "SELECT user_id, api_key_id, reserved_delta, auth_source FROM "
            "wallet_transactions WHERE type = 'RESERVE' AND auth_source = 'session'"
        ).fetchone()
        assert tuple(software_row) == (uid, None, 42, "session")
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action = 'customer_pricing.update'"
            ).fetchone()[0]
            == 1
        )


def test_admin_discount_and_rounding_drive_quotes(pricing_client, route_state):
    """BILLING-OBS P1-3：全局折扣与消费取整保存后要真实进入报价口径。"""
    from uuid import uuid4

    client = pricing_client
    headers, uid = account(client)
    key = mutation(client, "", headers, {"label": "discount"}).json()
    admin_headers = admin_login(client, route_state)
    payload = {
        "expected_version": 0,
        "config": config(discount_basis_points=8500, consumption_rounding="floor").model_dump(),
        "reason": "全科目 85 折并向下取整",
        "confirm": True,
    }
    path = "/api/control/settings/customer-pricing"
    result = client.put(
        path, headers={**admin_headers, "Idempotency-Key": str(uuid4())}, json=payload
    )
    assert result.status_code == 200, result.text
    assert result.json()["config"]["discount_basis_points"] == 8500
    assert result.json()["config"]["consumption_rounding"] == "floor"
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE wallets SET available_credits = 100 WHERE user_id = %s", (uid,))
        # 扩展模式恒开放：h3_extended_modes_enabled 列已随
        # 20260923T0000_open_h3_extended_modes 迁移 DROP，无需再翻开关。
    quote = client.get(
        "/api/generation/price-quote?resolution=2K&duration_seconds=6&quantity=1",
        headers=token_headers(key),
    )
    assert quote.status_code == 200, quote.text
    # 2K 6 秒：7 × 6 = 42 积分，85 折后 35.7，向下取整为 35（默认配置下为 42）。
    assert quote.json()["estimated_credits"] == 35


def test_recharge_freezes_exchange_and_replays_without_granting_unpaid_points(
    pricing_client, route_state
):
    from types import SimpleNamespace
    from uuid import uuid4

    from app.payment_provider import DeploymentConfig, MerchantConfig, PaymentFormResult
    from app.recharge_routes import get_zpay_provider

    client = pricing_client
    headers, uid = account(client)
    # Only the external payment form is a stub. Authentication, order and wallet are real PG.
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
        raw.execute(
            "UPDATE customer_credit_pricing SET version = 1, config_json = %s",
            (config().model_dump_json(),),
        )
    write_headers = {**headers, "Idempotency-Key": str(uuid4())}
    created = client.post(
        "/api/customer/recharge-orders", headers=write_headers, json={"amount_fen": 10000}
    )
    assert created.status_code == 201, created.text
    assert created.json()["credits"] == 10000
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE customer_credit_pricing SET version = 2, config_json = %s",
            (config(points_per_yuan=200).model_dump_json(),),
        )
    assert (
        client.post(
            "/api/customer/recharge-orders", headers=write_headers, json={"amount_fen": 10000}
        ).json()
        == created.json()
    )
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            raw.execute("SELECT credit_pricing_snapshot_json FROM recharge_orders").fetchone()[0]
            == '{"version": 1, "points_per_yuan": 100}'
        )
