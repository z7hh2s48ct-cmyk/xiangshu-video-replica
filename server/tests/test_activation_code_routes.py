"""T13 / ACT-05 — first-activation API contract tests (fail-first, red before green).

PG integration cases over a dedicated migrated database (skip without the
fixture): the customer activation endpoint ``POST /api/customer/activate``
must create the whole first-activation chain atomically (customer user,
wallet, activation fact, slot-1 device, PAID ``provider=activation_code``
order, unique CHARGE, session state with epoch=1 and a 90-second lease) or
leave nothing behind.

Contract under test (activation-code dev doc §12.1 / §13.1 / §13.2):

- ``Idempotency-Key`` is mandatory for the state-changing activation write;
- the unified 400 ``ACTIVATION_UNAVAILABLE`` never distinguishes unknown,
  malformed, undelivered, expired, suspended, revoked or already-active
  codes (anti-enumeration, ACT-08 groundwork);
- a fingerprint already bound to a live customer answers 409
  unified ``ACTIVATION_UNAVAILABLE`` (one account never redeems a second main code);
- the same Idempotency-Key with a different request body answers 409
  ``IDEMPOTENCY_CONFLICT``; with the same body it replays the stored
  response without re-billing;
- the SQLite lane fails closed with 503 (customer runtime is PG-only);
- plaintext codes, device tokens and session tokens never reach the logs —
  only digests do (T11 principle extended to credentials).
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
    generate_activation_code,
    mask_activation_code,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY = secrets.token_urlsafe(48)  # device-fingerprint HMAC key
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)  # 32 bytes, never a real secret

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"

ACTIVATE_PATH = "/api/customer/activate"

FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"


def _b64key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


T13_DB_NAME = "t13_customer_activation_test"


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t13_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T13_DB_NAME}"


@pytest.fixture(scope="module")
def activation_pg_dsn() -> Iterator[str]:
    """Dedicated migrated database with a seed operator for batch rows."""
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T13_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T13_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t13_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_t13_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
    try:
        yield _t13_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T13_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def clean_state(activation_pg_dsn: str) -> Iterator[str]:
    """Per-test isolation: truncate every table the activation chain touches."""
    close_pg_pool()
    with psycopg.connect(activation_pg_dsn, autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            "security_rate_limit_counters, security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
    yield activation_pg_dsn
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, clean_state: str) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router

    app = FastAPI()
    app.include_router(activation_code_router)
    monkeypatch.setenv(DATABASE_URL_ENV, clean_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY", _b64key(TEST_ENVELOPE_AEAD_KEY)
    )
    # T15 / ACT-08: the limiter now guards the activate endpoint. These T13
    # contract cases (including the 100-way ACT-06 burst from one client
    # address) exercise activation semantics, not abuse control — raise the
    # budgets far above any traffic the suite can generate.
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


def _insert_batch(
    conn: psycopg.Connection,
    batch_id: str,
    *,
    face_value_fen: int = 1500,
    unit_price_fen: int = 1000,
    credits: int = 100,
    activation_expires_at: str = FUTURE_EXPIRY,
) -> None:
    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        "quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN', 'admin_u')",
        (
            batch_id,
            f"批次 {batch_id}",
            face_value_fen,
            unit_price_fen,
            credits,
            1,
            activation_expires_at,
        ),
    )


def _insert_code(
    conn: psycopg.Connection,
    *,
    code_id: str,
    batch_id: str,
    plaintext: str,
    status: str = "ISSUED",
    activation_expires_at: str = FUTURE_EXPIRY,
) -> None:
    # 027's CHECK requires activation_expires_at > created_at, so an
    # already-expired batch is staged by back-dating created_at after the
    # insert instead of writing a past expiry directly. Naive timestamps
    # (the T12 admin shape) coerce to UTC first.
    _insert_batch(conn, batch_id)
    expires = datetime.fromisoformat(activation_expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        backdated = (datetime.fromisoformat(activation_expires_at) - timedelta(days=1)).isoformat()
        conn.execute(
            "UPDATE activation_code_batches "
            "SET activation_expires_at = %s, created_at = %s WHERE id = %s",
            (activation_expires_at, backdated, batch_id),
        )
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    # 027 couples bound_user_id with activated_at: a SUSPENDED / REVOKED /
    # ACTIVE code is a previously activated code, so it carries a bound user.
    # The seed user is an employee — the assertions count customer users only.
    bound_user_id = None
    if status in {"ACTIVE", "SUSPENDED", "REVOKED"}:
        bound_user_id = "seed-bound-user"
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('seed-bound-user', 'seed-bound-user', 'Seed', 'employee') "
            "ON CONFLICT (id) DO NOTHING"
        )
    status_columns = {
        "GENERATED": (None, None, None, None, None),
        "ISSUED": (now, None, None, None, None),
        "ACTIVE": (now, now, None, None, None),
        "SUSPENDED": (now, now, now, None, None),
        "REVOKED": (now, now, None, now, None),
        "EXPIRED": (now, None, None, None, now),
    }
    issued_at, activated_at, suspended_at, revoked_at, expired_at = status_columns[status]
    conn.execute(
        "INSERT INTO activation_codes "
        "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
        "bound_user_id, issued_at, activated_at, suspended_at, revoked_at, expired_at) "
        "VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            code_id,
            batch_id,
            compute_code_digest(plaintext, key=TEST_KEY.encode()),
            mask_activation_code(plaintext),
            status,
            bound_user_id,
            issued_at,
            activated_at,
            suspended_at,
            revoked_at,
            expired_at,
        ),
    )


def _activate(
    client: TestClient,
    *,
    code: str,
    key: str | None = None,
    fingerprint: str = "fp-primary-device",
    device_name: str = "我的工作台",
    platform: str = "windows",
    request_id: str | None = None,
) -> object:
    headers = {IDEMPOTENCY_KEY_HEADER: key or f"key-{secrets.token_hex(8)}"}
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    return client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": fingerprint,
            "device_name": device_name,
            "device_platform": platform,
        },
        headers=headers,
    )


def _row(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> tuple | None:
    return conn.execute(sql, params).fetchone()


def _count(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# ---------------------------------------------------------------------------
# The atomic happy path (ACT-05 exit gate)
# ---------------------------------------------------------------------------


def test_activate_creates_full_chain_atomically(client: TestClient, clean_state: str) -> None:
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-1", batch_id="batch-1", plaintext=plaintext)

    response = _activate(client, code=plaintext, key="key-primary")
    assert response.status_code == 201, response.text
    payload = response.json()
    # The one-time response carries the server-generated identity and the
    # credentials whose digests are the only thing the database keeps.
    assert payload["username"].startswith("customer-")
    assert payload["device_token"]
    assert payload["session_token"]
    assert payload["session_lease_expires_at"]
    assert response.headers.get(REQUEST_ID_HEADER)

    with psycopg.connect(clean_state) as conn:
        # customer user + role
        user = _row(
            conn,
            "SELECT id, username, role FROM users WHERE username = %s",
            (payload["username"],),
        )
        assert user is not None
        assert user[2] == "customer"
        user_id = user[0]

        # wallet funded exactly once with the batch credits
        wallet = _row(
            conn,
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
            (user_id,),
        )
        assert wallet is not None
        assert wallet[0] == 100
        assert wallet[1] == 0

        # one activation fact chained to the code, user and first-charge order
        activation = _row(
            conn,
            "SELECT code_id, user_id, first_device_id, recharge_order_id "
            "FROM activation_code_activations WHERE code_id = %s",
            ("code-1",),
        )
        assert activation is not None
        assert activation[1] == user_id
        assert activation[2] is not None
        order_id = activation[3]

        # slot-1 device bound with digests only
        device = _row(
            conn,
            "SELECT slot_no, status, token_digest, fingerprint_hmac "
            "FROM customer_devices WHERE id = %s",
            (activation[2],),
        )
        assert device is not None
        assert device[0] == 1
        assert device[1] == "BOUND"
        assert device[2]
        assert device[3]
        assert payload["device_token"] != device[2]

        # PAID first-charge order with the frozen commercial snapshot:
        # credits x frozen unit price, priced at/above the internal base
        # (PRICE-01); the amount follows the ledger formula
        # ``credits * charged_unit_price_fen_snapshot`` that revision 022/026
        # enforces on every recharge order.
        order = _row(
            conn,
            "SELECT provider, status, pricing_scope, amount_fen, credits, "
            "charged_unit_price_fen_snapshot, provider_trade_no "
            "FROM recharge_orders WHERE id = %s",
            (order_id,),
        )
        assert order is not None
        assert order[0] == "activation_code"
        assert order[1] == "PAID"
        assert order[2] == "CUSTOMER_STANDARD"
        assert order[3] == 100000
        assert order[4] == 100
        assert order[5] == 1000
        assert order[6] is None

        # exactly one CHARGE, keyed by the activation order
        charge = _row(
            conn,
            "SELECT type, available_delta, reserved_delta, recharge_order_id, "
            "idempotency_key FROM wallet_transactions WHERE recharge_order_id = %s",
            (order_id,),
        )
        assert charge is not None
        assert charge[0] == "CHARGE"
        assert charge[1] == 100
        assert charge[2] == 0
        assert charge[4] == f"activation_code:charge:{order_id}"

        # the code is ACTIVE and bound to the new user
        code_row = _row(
            conn, "SELECT status, bound_user_id FROM activation_codes WHERE id = %s", ("code-1",)
        )
        assert code_row is not None
        assert code_row[0] == "ACTIVE"
        assert code_row[1] == user_id

        # session state: epoch=1 with a ~90 second lease on the server clock
        session = _row(
            conn,
            "SELECT activation_code_id, device_id, session_epoch, token_digest, lease_until "
            "FROM customer_session_state WHERE user_id = %s",
            (user_id,),
        )
        assert session is not None
        assert session[0] == "code-1"
        assert session[1] == activation[2]
        assert session[2] == 1
        assert session[3]
        assert payload["session_token"] != session[3]
        lease = datetime.fromisoformat(session[4])
        server_now = conn.execute("SELECT now()").fetchone()[0].replace(tzinfo=UTC)
        assert timedelta(seconds=60) <= (lease - server_now) <= timedelta(seconds=120)

        # audit events recorded
        events = _count(
            conn,
            "SELECT COUNT(*) FROM activation_code_events "
            "WHERE code_id = %s AND event = 'ACTIVATED'",
            ("code-1",),
        )
        assert events == 1


def test_activate_uses_server_time_and_request_id(client: TestClient, clean_state: str) -> None:
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-1", batch_id="batch-1", plaintext=plaintext)
    response = _activate(client, code=plaintext, key="key-req", request_id="req-42")
    assert response.status_code == 201, response.text
    assert response.headers.get(REQUEST_ID_HEADER) == "req-42"
    with psycopg.connect(clean_state) as conn:
        event = _row(
            conn,
            "SELECT request_id FROM activation_code_events "
            "WHERE code_id = %s AND event = 'ACTIVATED'",
            ("code-1",),
        )
        assert event is not None and event[0] == "req-42"


# ---------------------------------------------------------------------------
# Write contract: the Idempotency-Key is mandatory
# ---------------------------------------------------------------------------


def test_activate_requires_idempotency_key(client: TestClient, clean_state: str) -> None:
    response = client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": generate_activation_code(),
            "device_fingerprint": "fp-x",
            "device_name": "d",
            "device_platform": "macos",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_activate_rejects_blank_idempotency_key(client: TestClient, clean_state: str) -> None:
    response = _activate(client, code=generate_activation_code(), key="   ")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ---------------------------------------------------------------------------
# Unified 400 ACTIVATION_UNAVAILABLE (anti-enumeration)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        "malformed",
        "unknown",
        "generated",
        "expired",
        "suspended",
        "revoked",
        "already_active",
    ],
)
def test_activate_unavailable_is_unified(
    client: TestClient, clean_state: str, scenario: str
) -> None:
    plaintext = generate_activation_code()
    expired = "2020-01-01T00:00:00+00:00"
    with psycopg.connect(clean_state) as conn:
        if scenario in {"generated", "expired", "suspended", "revoked", "already_active"}:
            status = {
                "generated": "GENERATED",
                "expired": "ISSUED",
                "suspended": "SUSPENDED",
                "revoked": "REVOKED",
                "already_active": "ACTIVE",
            }[scenario]
            _insert_code(
                conn,
                code_id=f"code-{scenario}",
                batch_id=f"batch-{scenario}",
                plaintext=plaintext,
                status=status,
                activation_expires_at=expired if scenario == "expired" else FUTURE_EXPIRY,
            )
    code = plaintext if scenario != "malformed" else "XS04-not-a-real-code"
    response = _activate(client, code=code)
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "ACTIVATION_UNAVAILABLE"
    # The message must not leak which sub-state the code was in.
    for marker in ("suspended", "revoked", "expired", "active", "not found", "unknown"):
        assert marker not in detail["message"].lower()

    if scenario != "malformed" and scenario != "unknown":
        with psycopg.connect(clean_state) as conn:
            # A rejected attempt must leave no user, order or ledger residue.
            assert _count(conn, "SELECT COUNT(*) FROM users WHERE role = 'customer'") == 0
            assert (
                _count(
                    conn,
                    "SELECT COUNT(*) FROM recharge_orders WHERE provider = 'activation_code'",
                )
                == 0
            )
            assert _count(conn, "SELECT COUNT(*) FROM wallet_transactions") == 0


# ---------------------------------------------------------------------------
# Unified rejection: one account never redeems a second main code
# ---------------------------------------------------------------------------


def test_activate_second_code_with_same_fingerprint_conflicts(
    client: TestClient, clean_state: str
) -> None:
    first_code = generate_activation_code()
    second_code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-1", batch_id="batch-1", plaintext=first_code)
        _insert_code(conn, code_id="code-2", batch_id="batch-2", plaintext=second_code)

    first = _activate(client, code=first_code, fingerprint="fp-same", key="key-first")
    assert first.status_code == 201, first.text

    second = _activate(client, code=second_code, fingerprint="fp-same", key="key-second")
    assert second.status_code == 400, second.text
    assert second.json()["detail"]["code"] == "ACTIVATION_UNAVAILABLE"

    with psycopg.connect(clean_state) as conn:
        # Only the first activation chain exists.
        assert _count(conn, "SELECT COUNT(*) FROM activation_code_activations") == 1
        assert (
            _count(conn, "SELECT COUNT(*) FROM recharge_orders WHERE provider = 'activation_code'")
            == 1
        )
        assert (
            _row(conn, "SELECT status FROM activation_codes WHERE id = %s", ("code-2",))[0]
            == "ISSUED"
        )


# ---------------------------------------------------------------------------
# Idempotency envelope: replay and conflict
# ---------------------------------------------------------------------------


def test_activate_same_key_same_body_replays_without_rebilling(
    client: TestClient, clean_state: str
) -> None:
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-1", batch_id="batch-1", plaintext=plaintext)

    first = _activate(client, code=plaintext, key="key-replay")
    assert first.status_code == 201, first.text
    replay = _activate(client, code=plaintext, key="key-replay")
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json()["username"] == first.json()["username"]
    assert replay.json()["device_token"] == first.json()["device_token"]
    assert replay.json()["session_token"] == first.json()["session_token"]

    with psycopg.connect(clean_state) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM activation_code_activations") == 1
        assert _count(conn, "SELECT COUNT(*) FROM users WHERE role = 'customer'") == 1
        assert _count(conn, "SELECT COUNT(*) FROM wallet_transactions WHERE type = 'CHARGE'") == 1
        assert _count(conn, "SELECT COUNT(*) FROM customer_session_state") == 1


def test_activate_same_key_different_body_conflicts(client: TestClient, clean_state: str) -> None:
    first_code = generate_activation_code()
    second_code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-1", batch_id="batch-1", plaintext=first_code)
        _insert_code(conn, code_id="code-2", batch_id="batch-2", plaintext=second_code)

    first = _activate(client, code=first_code, key="key-conflict")
    assert first.status_code == 201, first.text

    second = _activate(client, code=second_code, key="key-conflict")
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert second.headers.get(REPLAY_HEADER) is None

    with psycopg.connect(clean_state) as conn:
        assert (
            _row(conn, "SELECT status FROM activation_codes WHERE id = %s", ("code-2",))[0]
            == "ISSUED"
        )
        assert _count(conn, "SELECT COUNT(*) FROM activation_code_activations") == 1


# ---------------------------------------------------------------------------
# Fail-closed lanes and log hygiene
# ---------------------------------------------------------------------------


def test_activate_sqlite_lane_fails_closed(
    monkeypatch: pytest.MonkeyPatch, clean_state: str
) -> None:
    from app.activation_code_routes import router as activation_code_router

    app = FastAPI()
    app.include_router(activation_code_router)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    with TestClient(app) as sqlite_client:
        response = _activate(sqlite_client, code=generate_activation_code())
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "ACTIVATION_SERVICE_UNAVAILABLE"


def test_activate_never_logs_plaintext_code_or_credentials(
    client: TestClient, clean_state: str, caplog: pytest.LogCaptureFixture
) -> None:
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-1", batch_id="batch-1", plaintext=plaintext)
    with caplog.at_level(logging.DEBUG, logger="app.activation_code_routes"):
        response = _activate(client, code=plaintext, key="key-logs")
        assert response.status_code == 201, response.text
    payload = response.json()
    for secret in (plaintext, payload["device_token"], payload["session_token"]):
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# PR-review regressions: naive expiry, envelope shape, recovery window
# ---------------------------------------------------------------------------


def test_naive_batch_expiry_answers_unified_400_not_500(
    client: TestClient, clean_state: str
) -> None:
    """PR review P2: T12 accepts naive batch-expiry timestamps; an expired
    naive batch must still answer the unified 400 (never a TypeError 500)."""
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-naive",
            batch_id="batch-naive",
            plaintext=plaintext,
            # Naive past expiry: same shape the T12 admin API would have stored.
            activation_expires_at="2020-01-01T00:00:00",
        )

    response = _activate(client, code=plaintext, key="key-naive-expired")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "ACTIVATION_UNAVAILABLE"


def test_envelope_row_shape_after_success(client: TestClient, clean_state: str) -> None:
    """PR review P3: the completed envelope carries the sealed ciphertext,
    the key version and the recovery deadline — and never a purged marker."""
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-env", batch_id="batch-env", plaintext=plaintext)

    response = _activate(client, code=plaintext, key="key-envelope-shape")
    assert response.status_code == 201, response.text

    with psycopg.connect(clean_state) as conn:
        envelope = _row(
            conn,
            "SELECT operation, ciphertext, key_version, recovery_expires_at, purged_at "
            "FROM customer_idempotency_envelopes WHERE key_digest IS NOT NULL",
        )
        assert envelope is not None
        assert envelope[0] == "activate"
        assert envelope[1]  # sealed ciphertext present
        assert envelope[2] == 1  # key version recorded
        assert envelope[3] is not None  # recovery deadline present
        assert envelope[4] is None  # never purged at completion (T14 owns purge)


def test_recovery_window_env_override(
    client: TestClient,
    clean_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR review P3: VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS sets
    the window; the stored deadline follows it (default is 24 h)."""
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-win", batch_id="batch-win", plaintext=plaintext)

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS", "60")
    response = _activate(client, code=plaintext, key="key-window")
    assert response.status_code == 201, response.text

    # recovery_expires_at is a Text column holding an ISO string (revision
    # 029), so the window is checked client-side after parsing.
    with psycopg.connect(clean_state) as conn:
        row = _row(conn, "SELECT recovery_expires_at FROM customer_idempotency_envelopes")
        assert row is not None
        deadline = datetime.fromisoformat(str(row[0]))
        remaining = deadline - datetime.now(UTC)
        assert timedelta(seconds=0) < remaining <= timedelta(seconds=60)


def test_expired_recovery_window_refuses_replay(client: TestClient, clean_state: str) -> None:
    """PR review P3: once the recovery window has passed, the same key can no
    longer re-issue the one-time credentials — 409, not a silent replay."""
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-late", batch_id="batch-late", plaintext=plaintext)

    first = _activate(client, code=plaintext, key="key-late")
    assert first.status_code == 201, first.text

    # Back-date both timestamps so the CHECK (recovery > created) still holds
    # while the recovery deadline sits in the past.
    expired = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    created = (datetime.now(UTC) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    with psycopg.connect(clean_state, autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_idempotency_envelopes SET recovery_expires_at = %s, created_at = %s",
            (expired, created),
        )

    late = _activate(client, code=plaintext, key="key-late")
    assert late.status_code == 409, late.text
    assert late.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert late.headers.get(REPLAY_HEADER) is None


def test_same_key_whitespace_padding_replays(client: TestClient, clean_state: str) -> None:
    """PR review P3: the request hash freezes the normalized body, so a retry
    that only differs in surrounding whitespace still replays the stored
    response instead of conflicting on the spent key."""
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-pad", batch_id="batch-pad", plaintext=plaintext)

    first = _activate(client, code=plaintext, key="key-padding", fingerprint="fp-pad")
    assert first.status_code == 201, first.text

    padded = client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": f"  {plaintext}  ",
            "device_fingerprint": " fp-pad ",
            "device_name": "  我的工作台  ",
            "device_platform": " windows ",
        },
        headers={IDEMPOTENCY_KEY_HEADER: "key-padding"},
    )
    assert padded.status_code == 201, padded.text
    assert padded.headers.get(REPLAY_HEADER) == "true"
    assert padded.json()["device_token"] == first.json()["device_token"]


# ---------------------------------------------------------------------------
# PR #44 review regressions: CORS preflight, rotation-window fingerprints,
# cross-version envelope scope, installed session triggers
# ---------------------------------------------------------------------------


def test_cors_preflight_permits_idempotency_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #44 review P1: the WebView/browser client must pass the mandatory
    Idempotency-Key (and X-Request-Id) through the CORS preflight, and read
    the replay/request-id response headers on the actual response."""
    from app.main import app as main_app

    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(
        "VIDEO_REPLICA_DATABASE_URL",
        os.environ.get(
            "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
        ),
    )
    with TestClient(main_app) as main_client:
        preflight = main_client.options(
            ACTIVATE_PATH,
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": ("idempotency-key, x-request-id, content-type"),
            },
        )
        assert preflight.status_code == 200, preflight.text
        allowed = preflight.headers.get("access-control-allow-headers", "").lower()
        assert "idempotency-key" in allowed
        assert "x-request-id" in allowed

        # The expose headers live on the actual (non-preflight) response — a
        # validation error still passes through the CORS middleware, so the
        # status does not matter, only the readable header contract.
        actual = main_client.post(
            ACTIVATE_PATH,
            headers={"Origin": "http://localhost:5173", IDEMPOTENCY_KEY_HEADER: "key-cors"},
        )
        assert actual.status_code == 422, actual.text
    exposed = actual.headers.get("access-control-expose-headers", "").lower()
    assert "x-idempotent-replay" in exposed
    assert "x-request-id" in exposed


def test_fingerprint_rotation_window_blocks_second_activation(
    client: TestClient,
    clean_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #44 review P1: during a rotation window (V1 retained, V2 added) a
    fingerprint bound under V1 must still be recognized — the same physical
    device may never redeem a second code for a second user and first charge."""
    first_code = generate_activation_code()
    second_code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-rot-1", batch_id="batch-rot-1", plaintext=first_code)
        _insert_code(conn, code_id="code-rot-2", batch_id="batch-rot-2", plaintext=second_code)

    first = _activate(client, code=first_code, fingerprint="fp-rotating", key="key-rot-a")
    assert first.status_code == 201, first.text

    # Rotation: V2 is configured while V1 stays retained.
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", secrets.token_urlsafe(48))
    second = _activate(client, code=second_code, fingerprint="fp-rotating", key="key-rot-b")
    assert second.status_code == 400, second.text
    assert second.json()["detail"]["code"] == "ACTIVATION_UNAVAILABLE"

    with psycopg.connect(clean_state) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM users WHERE role = 'customer'") == 1
        assert _count(conn, "SELECT COUNT(*) FROM wallet_transactions WHERE type = 'CHARGE'") == 1
        assert (
            _row(conn, "SELECT status FROM activation_codes WHERE id = %s", ("code-rot-2",))[0]
            == "ISSUED"
        )


def test_envelope_scope_recovery_survives_key_rotation(
    client: TestClient,
    clean_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #44 review P1: a retry created before the rotation (envelope scope
    under the V1 fingerprint digest) must still replay after V2 is added —
    the one-time credentials stay recoverable across the rotation window."""
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-scope", batch_id="batch-scope", plaintext=plaintext)

    first = _activate(client, code=plaintext, fingerprint="fp-scope", key="key-scope")
    assert first.status_code == 201, first.text

    # Rotate both the device-domain HMAC and the envelope AEAD keys; V1 stays
    # retained so the sealed envelope remains decryptable.
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", secrets.token_urlsafe(48))
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY_V2",
        _b64key(secrets.token_bytes(32)),
    )

    replay = _activate(client, code=plaintext, fingerprint="fp-scope", key="key-scope")
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json()["device_token"] == first.json()["device_token"]
    assert replay.json()["session_token"] == first.json()["session_token"]

    with psycopg.connect(clean_state) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM customer_idempotency_envelopes") == 1
        assert _count(conn, "SELECT COUNT(*) FROM wallet_transactions WHERE type = 'CHARGE'") == 1
