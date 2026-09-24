"""CW-029 — reuse the billing core and close charge/payment gaps (TEST-PG).

V3 收敛清单 CW-029 remaining work: ``把任务结果重复/乱序、未知提交、取消、
客户隐藏记录、支付回调验签与跨用户订单全部迁成 TEST-PG 多连接矩阵；对每
用例核对 available+reserved+ledger 差额为 0 并证明历史价格快照不可被现价
改写``.

The billing core (``reserve/settle/release`` + ``billing_round`` idempotency
in ``internal_billing.py``, the ZPay callback confirmation in
``zpay_payments.py``) is REUSED untouched. This module re-hosts the payment
and task-status matrix on PostgreSQL with multiple connections — the
equivalent suite (``tests/test_payments.py``) still runs on the legacy
internal SQLite lane, which is exactly the gap CW-029 closes.

Excluded per spec: real ZPay/provider callbacks (CW-050), historical
migration rewrites, client-side display amounts as evidence.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-cw029-billing-matrix-minimum-48-bytes-1234567",
)

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip

from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.zpay import sign_zpay_params

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

CW029_DB_NAME = "cw029_billing_pg_test"

MERCHANT_KEY = "merchant-secret-cw029"
ORDER_NO = "CW29-ORDER-1"
SECOND_ORDER_NO = "CW29-ORDER-2"
OTHER_ORDER_NO = "CW29-ORDER-3"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _cw029_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{CW029_DB_NAME}"


def _insert_pending_order(
    conn: psycopg.Connection,
    *,
    order_id: str,
    user_id: str,
    order_no: str,
    amount_fen: int = 10000,
    credits: int = 10,
) -> None:
    conn.execute(
        """
        INSERT INTO recharge_orders (
            id, user_id, merchant_order_no, provider, status, pricing_scope,
            channel, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
            min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits
        ) VALUES (%s, %s, %s, 'zpay', 'PENDING', 'CUSTOMER_STANDARD',
            'alipay', 1000, 1000, 10000, 1000, %s, %s)
        """,
        (order_id, user_id, order_no, amount_fen, credits),
    )


def _seed_task_chain(conn: psycopg.Connection, *, task_id: str, user_id: str) -> None:
    """A minimal generation chain the billing core can reserve/finalize on."""
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        (f"proj-{task_id}", user_id, "CW-029"),
    )
    conn.execute(
        "INSERT INTO generation_batches ("
        " id, project_id, created_by_user_id, idempotency_key, "
        " request_hash, request_snapshot_json"
        ") VALUES (%s, %s, %s, %s, 'hash', '{}')",
        (f"batch-{task_id}", f"proj-{task_id}", user_id, f"key-{task_id}"),
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, model, status) "
        "VALUES (%s, %s, 'I2V', 'fake_h3', 'MiniMax-H3', 'PENDING')",
        (task_id, f"batch-{task_id}"),
    )


@pytest.fixture(scope="module")
def cw029_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{CW029_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{CW029_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _cw029_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _cw029_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{CW029_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(cw029_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_cw029_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE wallet_transactions, recharge_orders, wallets, users, "
            "projects, generation_batches, generation_tasks, audit_logs, "
            "provider_settings, runtime_settings CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        for user in ("user_1", "user_2"):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES (%s, %s, %s, 'customer')",
                (user, user, user.title()),
            )
            conn.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES (%s, 100, 0)",
                (user,),
            )
    yield cw029_dsn
    close_pg_pool()


@pytest.fixture()
def payment_app(route_state: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """The ZPay callback surface on the customer PG lane (signature-gated)."""
    from app.payment_routes import router as payment_router

    app = FastAPI()
    app.include_router(payment_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://callback.example.com")
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")
    # The signed merchant config the notify route loads (the T22 fixture pattern).
    settings_key = os.environ["VIDEO_REPLICA_SETTINGS_KEY"]
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_z', 'admin_z', 'Admin Z', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
        encrypted = (
            Fernet(settings_key.encode("ascii"))
            .encrypt(
                b'{"pid":"merchant-123","key":"'
                + MERCHANT_KEY.encode("ascii")
                + b'","enabled_channels":"alipay,wxpay"}'
            )
            .decode("ascii")
        )
        conn.execute(
            "INSERT INTO provider_settings (provider, encrypted_config, "
            "updated_by_user_id, created_at, updated_at) VALUES (%s, %s, 'admin_z', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT(provider) DO UPDATE SET "
            "encrypted_config = excluded.encrypted_config",
            ("zpay", encrypted),
        )
        _insert_pending_order(conn, order_id="order_1", user_id="user_1", order_no=ORDER_NO)
        _insert_pending_order(conn, order_id="order_2", user_id="user_1", order_no=SECOND_ORDER_NO)
        _insert_pending_order(conn, order_id="order_3", user_id="user_2", order_no=OTHER_ORDER_NO)
    yield app
    app.dependency_overrides.clear()


@pytest.fixture()
def client(payment_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(payment_app) as test_client:
        yield test_client


def signed_notify(
    *,
    order_no: str = ORDER_NO,
    trade_no: str = "cw29-trade-1",
    money: str = "100.00",
    trade_status: str = "TRADE_SUCCESS",
    pid: str = "merchant-123",
    channel: str = "alipay",
    key: str = MERCHANT_KEY,
) -> dict[str, str]:
    params: dict[str, str] = {
        "pid": pid,
        "name": "CW-029 recharge",
        "money": money,
        "out_trade_no": order_no,
        "trade_no": trade_no,
        "trade_status": trade_status,
        "type": channel,
    }
    params["sign"] = sign_zpay_params(params, key)
    params["sign_type"] = "MD5"
    return params


def four_ledger_snapshot(dsn: str, user_id: str) -> tuple[int, int, int, int]:
    """(available, reserved, ledger rows, Σ reserved_delta).

    The CW-029 invariant pair, asserted per case: available always equals
    the seeded 100 plus Σ available_delta, and once a billing round reaches
    its terminal row the reserved column AND Σ reserved_delta are both back
    to zero (the round nets to zero across the ledger).
    """
    with psycopg.connect(dsn) as conn:
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT count(*), COALESCE(SUM(reserved_delta), 0) "
            "FROM wallet_transactions WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    assert wallet is not None and rows is not None
    return int(wallet[0]), int(wallet[1]), int(rows[0]), int(rows[1])


def order_status(dsn: str, order_no: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT status FROM recharge_orders WHERE merchant_order_no = %s",
            (order_no,),
        ).fetchone()
    return None if row is None else str(row[0])


# ---------------------------------------------------------------------------
# The payment callback matrix (TEST-PG multi-connection)
# ---------------------------------------------------------------------------


def test_paid_notify_credits_once_and_duplicate_out_of_order_is_idempotent(
    client: TestClient, route_state: str
) -> None:
    before = four_ledger_snapshot(route_state, "user_1")

    first = client.get("/api/payments/zpay/notify", params=signed_notify())
    assert first.status_code == 200 and first.text == "success", first.text
    after_first = four_ledger_snapshot(route_state, "user_1")
    assert after_first[0] == before[0] + 10, "the order snapshot credits exactly 10"
    assert after_first[2] == before[2] + 1, "exactly one ledger row"
    assert order_status(route_state, ORDER_NO) == "PAID"

    # Duplicate arrival (the same signed callback replayed by the provider):
    # success, but zero additional booking.
    duplicate = client.get("/api/payments/zpay/notify", params=signed_notify())
    assert duplicate.status_code == 200 and duplicate.text == "success"
    assert four_ledger_snapshot(route_state, "user_1") == after_first

    # Out-of-order arrival with a DIFFERENT trade number on a paid order:
    # refused, no second credit.
    mismatch = client.get(
        "/api/payments/zpay/notify", params=signed_notify(trade_no="cw29-trade-2")
    )
    assert mismatch.status_code == 409, mismatch.text
    assert four_ledger_snapshot(route_state, "user_1") == after_first


def test_forged_callbacks_have_zero_side_effects(client: TestClient, route_state: str) -> None:
    baseline = four_ledger_snapshot(route_state, "user_1")

    forged_cases = [
        signed_notify(key="forged-key"),  # bad signature
        signed_notify(pid="merchant-999"),  # wrong merchant id
        signed_notify(trade_status="TRADE_CLOSED"),  # non-success status
        signed_notify(money="1.00"),  # amount does not match the order
        signed_notify(channel="bitcoin"),  # disallowed channel
        signed_notify(order_no="CW29-UNKNOWN"),  # unknown order
    ]
    for params in forged_cases:
        response = client.get("/api/payments/zpay/notify", params=params)
        assert response.status_code in (400, 404, 409), (params, response.text)
        assert response.text == "failure"

    assert four_ledger_snapshot(route_state, "user_1") == baseline, (
        "a forged callback must leave wallet and ledger untouched"
    )
    assert order_status(route_state, ORDER_NO) == "PENDING"


def test_provider_trade_no_cannot_credit_two_users_orders(
    client: TestClient, route_state: str
) -> None:
    first = client.get("/api/payments/zpay/notify", params=signed_notify())
    assert first.status_code == 200

    # The same provider trade number presented for ANOTHER user's order.
    cross = client.get(
        "/api/payments/zpay/notify",
        params=signed_notify(order_no=OTHER_ORDER_NO),
    )
    assert cross.status_code == 409, cross.text
    assert order_status(route_state, OTHER_ORDER_NO) == "PENDING"
    user2 = four_ledger_snapshot(route_state, "user_2")
    assert user2[0] == 100 and user2[2] == 0, "the cross-user order must not credit"


def test_customer_closed_order_keeps_lineage_and_late_callback_reconciles(
    client: TestClient, route_state: str
) -> None:
    # The customer "delete" (close) marks the unpaid order CLOSED without
    # erasing the row — the route's audited outcome, mirrored here.
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "UPDATE recharge_orders SET status = 'CLOSED' WHERE merchant_order_no = %s",
            (ORDER_NO,),
        )
    before = four_ledger_snapshot(route_state, "user_1")

    late = client.get("/api/payments/zpay/notify", params=signed_notify())
    assert late.status_code == 200 and late.text == "success"
    assert order_status(route_state, ORDER_NO) == "PAID", (
        "the closed order keeps its lineage and settles the late callback"
    )
    after = four_ledger_snapshot(route_state, "user_1")
    assert after[0] == before[0] + 10 and after[2] == before[2] + 1


def test_order_price_snapshot_survives_runtime_price_change(
    client: TestClient, route_state: str
) -> None:
    # The operator raises the unit price AFTER the order was created: the
    # pending order keeps its own frozen snapshot and credits 10, not the
    # recalculated amount.
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute("UPDATE runtime_settings SET internal_base_unit_price_fen = 5000 WHERE id = 1")
        order = conn.execute(
            "SELECT charged_unit_price_fen_snapshot, credits FROM recharge_orders "
            "WHERE merchant_order_no = %s",
            (ORDER_NO,),
        ).fetchone()
    assert order is not None
    assert int(order[0]) == 1000 and int(order[1]) == 10, "snapshot frozen at creation"

    response = client.get("/api/payments/zpay/notify", params=signed_notify())
    assert response.status_code == 200
    with psycopg.connect(route_state) as conn:
        row = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = 'user_1'"
        ).fetchone()
    assert row is not None and int(row[0]) == 110, "credited per the historical snapshot"


# ---------------------------------------------------------------------------
# The task-result matrix (reserve / settle / release / dangling sweep)
# ---------------------------------------------------------------------------


def _seed_task(dsn: str, task_id: str, user_id: str = "user_1") -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('video_768p',true,1) ON CONFLICT(service) DO NOTHING"
        )
        _seed_task_chain(conn, task_id=task_id, user_id=user_id)


def _reserve(dsn: str, task_id: str, seconds: int = 2) -> None:
    with pg_transaction() as raw:
        from app.internal_billing import reserve_internal_billing

        reserve_internal_billing(
            BusinessConnection.postgres(raw),
            user_id="user_1",
            task_id=task_id,
            billing_round=1,
            seconds=seconds,
        )


def _finalize(dsn: str, task_id: str, outcome: str) -> object:
    from app.internal_billing import finalize_internal_billing

    with pg_transaction() as raw:
        return finalize_internal_billing(
            BusinessConnection.postgres(raw),
            task_id=task_id,
            outcome=outcome,  # type: ignore[arg-type]
        )


def test_task_result_duplicate_and_out_of_order_finalization_is_idempotent(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    _seed_task(route_state, "task_dup")
    _reserve(route_state, "task_dup", seconds=2)
    reserved = four_ledger_snapshot(route_state, "user_1")
    assert reserved == (98, 2, 1, 2), "a live round holds the reservation"

    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes, content_type, "
            " created_by_user_id"
            ") VALUES ('result_dup', 'proj-task_dup', 'video', "
            " 'cos://bucket/result.mp4', 'sha', 12, 'video/mp4', 'user_1')"
        )
        conn.execute(
            "UPDATE generation_tasks "
            "SET status = 'SUCCEEDED', actual_output_seconds=2, archive_status = 'ARCHIVED', "
            "    result_asset_id = 'result_dup' WHERE id = 'task_dup'"
        )
    # Duplicate result arrival: the second finalize returns the recorded
    # terminal type and adds NO second ledger row.
    first = _finalize(route_state, "task_dup", "success")
    settled = four_ledger_snapshot(route_state, "user_1")
    replay = _finalize(route_state, "task_dup", "success")
    # available 98 == 100 seeded + Σavailable_delta(-2); the round's
    # reserved column AND Σreserved_delta both returned to zero.
    assert settled == (98, 0, 2, 0), "reserve+settle cycle nets to zero"
    assert getattr(replay, "transaction_type") == getattr(first, "transaction_type") == "SETTLE"
    assert four_ledger_snapshot(route_state, "user_1") == settled

    # Out-of-order: a (late) failure result AFTER the terminal SETTLE is a
    # no-op — it must not add a RELEASE row on top.
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task_dup'")
    late = _finalize(route_state, "task_dup", "failed")
    assert getattr(late, "transaction_type") == "SETTLE"
    assert four_ledger_snapshot(route_state, "user_1") == settled


def test_task_cancellation_releases_once_and_unknown_submission_stays_frozen(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    _seed_task(route_state, "task_cancel")
    _reserve(route_state, "task_cancel", seconds=2)
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute("UPDATE generation_tasks SET status = 'CANCELLED' WHERE id = 'task_cancel'")
    released = _finalize(route_state, "task_cancel", "failed")
    assert getattr(released, "transaction_type") == "RELEASE"
    after_release = four_ledger_snapshot(route_state, "user_1")
    assert after_release == (100, 0, 2, 0), "cancel cycle nets to zero"
    replay = _finalize(route_state, "task_cancel", "failed")
    assert getattr(replay, "transaction_type") == "RELEASE"
    assert four_ledger_snapshot(route_state, "user_1") == after_release

    # Unknown submission: a claimed task whose provider outcome never came
    # back keeps its reservation frozen — the sweep excludes active tasks, so
    # nothing is settled or released behind the worker's back.
    _seed_task(route_state, "task_unknown")
    _reserve(route_state, "task_unknown", seconds=3)
    frozen = four_ledger_snapshot(route_state, "user_1")
    assert frozen[0] == 97 and frozen[1] == 3

    from app.internal_billing import reconcile_dangling_billing_reservations

    with pg_transaction() as raw:
        reconciliation = reconcile_dangling_billing_reservations(BusinessConnection.postgres(raw))
    assert reconciliation.scanned == 0, "an active task is not a dangling candidate"
    assert four_ledger_snapshot(route_state, "user_1") == frozen
