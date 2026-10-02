"""T16 / T18 / DEV-01 — two current device slots, credentials and unbind history,
the admin verification lane.

Fail-first tests for the frozen files ``server/app/customer_device_service.py``
and ``server/app/customer_device_routes.py`` (code checklist §3.2 / §3.3):
the device-slot half of the customer runtime. Revision 028 already proved the
slot invariants in the database (partial unique indexes); this task delivers
the application layer on top — device credential authentication, the
two-slot status view, unbinding with its preserved audit history, and the
third-device block that the second-device enroll flow (T17) will consult.
T18 adds the administrator fallback lane (§12.2 step 3 / §9.2 / §15):
the live-first-device rejection, the admin-verified approval once the first
device is released, the admin unbind and credential revocation lanes, and
the ``admin_device_events`` audit trail (revision 038).

Contract under test (task list §3 T16 / T18; dev doc §3.2 / §6.1 / §6.2 /
§9.2 / §12.2 / §13.2 / §15):

- the device credential is the long-lived secret returned once at bind time;
  it authenticates device-management requests via ``Authorization: Bearer``
  and only its keyed digest ever reaches the database;
- ``GET /api/customer/devices`` answers the two-slot status: slot 1 and slot
  2 each hold at most one currently ``BOUND`` device, plus the unbind history
  that outlives slot reuse (rows are never deleted — dev doc §3.2);
- ``DELETE /api/customer/devices/{id}`` unbinds one of the caller's own
  devices: the row flips to ``UNBOUND``, the slot becomes reusable, and any
  live session riding that device is revoked atomically (epoch bump + past
  lease + ``LOGOUT`` event);
- with both slots ``BOUND`` there is no free slot (``next_free_slot`` is
  ``None``) and PostgreSQL itself refuses a third ``BOUND`` row — the
  third-device block;
- stable error codes: 401 ``DEVICE_CREDENTIAL_REQUIRED`` /
  ``DEVICE_CREDENTIAL_INVALID`` / ``DEVICE_REVOKED``, 404 ``DEVICE_NOT_FOUND``
  (missing or foreign device — one answer, no IDOR oracle), 409
  ``DEVICE_ALREADY_UNBOUND``;
- the DELETE carries a mandatory ``Idempotency-Key`` (PR #47 Codex review
  P2): a client that lost the 204 retries with the same key + same target
  and replays the sealed 204 after the released target credential is matched;
  unauthenticated/cross-account probes never reach that envelope, while the
  same key against a different target in the same account answers
  409 ``IDEMPOTENCY_CONFLICT`` (400 ``IDEMPOTENCY_KEY_REQUIRED`` otherwise);
- T18 management lanes: ``GET /api/control/devices`` lists device metadata
  without keyed digests (§6.2), ``POST /api/control/device-pairings/{id}/approve``
  routes through the operator after verifying issuance record and activation
  fact when the first device is unavailable (§12.2 step 3), and
  ``POST /api/control/devices/{id}/unbind`` / ``revoke-credential`` are
  audited with the real acting administrator, a human-readable reason and an
  Idempotency-Key (dev doc §15: 真实 actor、原因、二次确认和审计存在);
- revision 038 creates the append-only ``admin_device_events`` table (the
  029 trigger precedent + the 036 TRUNCATE guard shared function).
"""

from __future__ import annotations

import base64
import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip
from psycopg.errors import CheckViolation, UniqueViolation

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
    mask_activation_code,
)
from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

# 链尾（alembic head）。038/037 的降级守卫用例断言「拒绝后 schema 停在 head」，
# 用的是这个常量——此前那两处把 head 字面量写在断言里，追加迁移时漏掉一处就会红
# 而没有任何自动化提示。B1 追加 20260923T1200 时正是被这两个断言抓到的，
# 因此顺手收敛成一个常量。
_HEAD_REVISION = "20261002T0800_alert_delivery_rules"

T16_DB_NAME = "t16_customer_devices_test"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY_V1 = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V2 = secrets.token_urlsafe(48)
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)
TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)  # admin-session HMAC key

ACTIVATE_PATH = "/api/customer/activate"
DEVICES_PATH = "/api/customer/devices"
ENROLL_PATH = "/api/customer/devices/enroll"
APPROVE_PATH = "/api/customer/device-pairings"
ADMIN_DEVICES_PATH = "/api/control/devices"
ADMIN_PAIRINGS_PATH = "/api/control/device-pairings"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"
AUTHORIZATION_HEADER = "Authorization"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

# Canonical Crockford-shaped codes (prefix + 4 groups x 7 characters).
FIRST_CODE = "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD"
SECOND_CODE = "XS04-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF"
# Dedicated to the service-level tests that skip the route_state fixture
# (and therefore run against whatever the routed tests last seeded).
SLOT_PROBE_CODE = "XS04-EEEEEEE-FFFFFFF-AAAAAAA-BBBBBBB"
THIRD_DEVICE_CODE = "XS04-FFFFFFF-AAAAAAA-BBBBBBB-CCCCCCC"

COUNTERS_TABLE = "security_rate_limit_counters"
FAILURES_TABLE = "security_auth_failures"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t16_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T16_DB_NAME}"


# ---------------------------------------------------------------------------
# Module-level units (no database) — always run
# ---------------------------------------------------------------------------


def test_keyed_digest_is_deterministic_and_key_sensitive() -> None:
    from app.customer_device_service import keyed_digest

    first = keyed_digest(b"a" * 32, "token-one")
    second = keyed_digest(b"a" * 32, "token-one")
    other_key = keyed_digest(b"b" * 32, "token-one")
    other_value = keyed_digest(b"a" * 32, "token-two")
    assert first == second, "same key and token must produce the same digest"
    assert first != other_key, "a different key version must change the digest"
    assert first != other_value, "a different token must change the digest"


def test_default_max_devices_constant_is_two() -> None:
    from app.customer_device_service import _DEFAULT_MAX_DEVICES

    assert _DEFAULT_MAX_DEVICES == 2


def test_enroll_openapi_contract_declares_both_response_shapes() -> None:
    """T28 (FE-01): the enroll route must declare its two response bodies in
    the OpenAPI contract — 202 while the pairing waits for approval, 201 with
    the one-time device credential. The client's generated types are cut from
    this contract ("the OpenAPI contract lands with the T28 client-type task",
    the route docstring), so an undeclared shape is a broken contract, not a
    cosmetic gap: the adapter could not type the pairing wait vs. the
    credential handoff without drifting into hand-written shapes."""
    from app.customer_device_routes import router as customer_device_router

    contract_app = FastAPI()
    contract_app.include_router(customer_device_router)
    schema = contract_app.openapi()

    responses = schema["paths"][ENROLL_PATH]["post"]["responses"]
    pending_ref = responses["202"]["content"]["application/json"]["schema"]["$ref"]
    consumed_ref = responses["201"]["content"]["application/json"]["schema"]["$ref"]
    assert pending_ref == "#/components/schemas/DeviceEnrollPendingResponse"
    assert consumed_ref == "#/components/schemas/DeviceEnrollConsumedResponse"
    # PR #55 review: the route only ever answers 201 or 202 — a phantom
    # default-200 entry (an artifact of FastAPI's decorator default colliding
    # with the explicit responses= declaration) would force every generated
    # client to handle an impossible untyped branch.
    assert "200" not in responses

    components = schema["components"]["schemas"]
    pending = components["DeviceEnrollPendingResponse"]
    consumed = components["DeviceEnrollConsumedResponse"]
    # The two live bodies: {pairing_request_id, status, expires_at,
    # request_id} while PENDING/APPROVED, {device_id, slot_no, device_token,
    # request_id} once consumed — every field is required either way.
    assert set(pending["required"]) == {
        "pairing_request_id",
        "status",
        "expires_at",
        "request_id",
    }
    assert set(consumed["required"]) == {
        "device_id",
        "slot_no",
        "device_token",
        "request_id",
    }
    assert set(pending["properties"]) == set(pending["required"])
    assert set(consumed["properties"]) == set(consumed["required"])


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def devices_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T16_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T16_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t16_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t16_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T16_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(devices_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, device_pairing_requests, "
            "admin_device_events, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            f"{COUNTERS_TABLE}, {FAILURES_TABLE} CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor User', 'auditor')"
        )
    yield devices_dsn
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router
    from app.admin_activation_routes import router as admin_activation_router
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_device_routes import router as admin_device_router
    from app.customer_device_routes import router as customer_device_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_device_router)
    app.include_router(admin_auth_router)
    app.include_router(admin_activation_router)
    app.include_router(admin_device_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    # Roomy budgets: these tests exercise the device routes, not the limiter.
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")
    monkeypatch.setattr("app.activation_code_routes.apply_anti_enumeration_delay", lambda: None)
    # The admin lanes must run on real admin sessions, never a dev identity
    # header shortcut (the T12 fixture precedent).
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


def _seed_issuable_code(code: str, *, code_id: str, batch_id: str) -> None:
    """Insert one OPEN batch + ISSUED code pair (T12 shapes)."""
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            f"VALUES ('{batch_id}', 'batch-{batch_id}', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
        )
        digest = compute_code_digest(code, key=TEST_KEY.encode("utf-8"))
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) "
            f"VALUES ('{code_id}', '{batch_id}', %s, 1, 'XS04-****', "
            "'ISSUED', '2026-01-01T00:00:00+00:00')",
            (digest,),
        )


def _activate(client: TestClient, code: str, fingerprint: str, key_suffix: str) -> object:
    return client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": fingerprint,
            "device_name": f"Device {key_suffix}",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: f"idem-{key_suffix}"},
    )


def _activated_customer(client: TestClient, *, code: str, fingerprint: str, suffix: str) -> dict:
    """Seed + activate one customer; assert success and return the payload."""
    _seed_issuable_code(code, code_id=f"code-{suffix}", batch_id=f"batch-{suffix}")
    response = _activate(client, code, fingerprint, suffix)
    assert response.status_code == 201, response.text
    payload = response.json()
    for field in ("user_id", "device_id", "device_token", "session_token"):
        assert isinstance(payload.get(field), str) and payload[field], payload
    return payload


def _bearer(token: str) -> dict[str, str]:
    return {AUTHORIZATION_HEADER: f"Bearer {token}"}


def _second_device_row(
    *,
    user_id: str,
    activation_code_id: str,
    device_id: str,
    slot_no: int,
) -> str:
    """Insert a second BOUND device directly (T17 enroll is out of scope).

    The credential digest is computed with the highest configured device
    key version, exactly like the activation transaction does. Returns the
    plaintext token the caller then presents.
    """
    from app.customer_device_service import highest_device_domain_key, keyed_digest

    token = secrets.token_urlsafe(32)
    version, key = highest_device_domain_key()
    digest = keyed_digest(key, token)
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES (%s, %s, %s, %s, 'Second Device', 'macos', %s, %s, %s, %s)",
            (
                device_id,
                activation_code_id,
                user_id,
                slot_no,
                keyed_digest(key, f"fp-second-{device_id}"),
                version,
                digest,
                version,
            ),
        )
    return token


# ---------------------------------------------------------------------------
# GET /api/customer/devices — the two-slot status view
# ---------------------------------------------------------------------------


def test_list_devices_requires_bearer_token(client: TestClient) -> None:
    response = client.get(DEVICES_PATH)
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"


def test_list_devices_rejects_malformed_authorization(client: TestClient) -> None:
    for header in ("Token abc", "Bearer", "Bearer   ", "Basic Zm9vOmJhcg=="):
        response = client.get(DEVICES_PATH, headers={AUTHORIZATION_HEADER: header})
        assert response.status_code == 401, header
        assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"


def test_list_devices_rejects_unknown_token(client: TestClient) -> None:
    response = client.get(DEVICES_PATH, headers=_bearer(secrets.token_urlsafe(32)))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_INVALID"


def test_list_devices_shows_only_bound_slots_without_capacity_placeholders(
    client: TestClient,
) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-1", suffix="d1")
    response = client.get(DEVICES_PATH, headers=_bearer(customer["device_token"]))
    assert response.status_code == 200, response.text
    body = response.json()

    assert [slot["slot_no"] for slot in body["slots"]] == [1]
    slot_one = body["slots"][0]
    assert slot_one["device"] is not None
    assert slot_one["device"]["id"] == customer["device_id"]
    assert slot_one["device"]["status"] == "BOUND"
    assert slot_one["device"]["is_current"] is True
    assert slot_one["device"]["bound_at"]
    assert body["history"] == []


def test_list_devices_returns_pending_pairings_for_approval(client: TestClient) -> None:
    """The first device sees PENDING pairings in the devices view so the
    approval flow is reachable (T30 UI wiring: the pending list drives the
    approval card)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dvp-1", suffix="dvp1")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-dvp-1-second", key="idem-dvp1")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    response = client.get(DEVICES_PATH, headers=_bearer(customer["device_token"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["pending_pairings"]) == 1
    pending = body["pending_pairings"][0]
    assert pending["pairing_request_id"] == pairing_id
    assert pending["display_name"]
    assert pending["platform"]
    assert pending["created_at"]

    # A different customer's devices view must not see this pairing.
    other = _activated_customer(client, code=SECOND_CODE, fingerprint="fp-dvp-2", suffix="dvp2")
    response = client.get(DEVICES_PATH, headers=_bearer(other["device_token"]))
    assert response.status_code == 200, response.text
    assert response.json()["pending_pairings"] == []


# ---------------------------------------------------------------------------
# DELETE /api/customer/devices/{id} — unbind, history and slot reuse
# ---------------------------------------------------------------------------


def test_unbind_releases_slot_keeps_history_and_allows_reuse(
    client: TestClient,
) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-2", suffix="d2")
    device_id = customer["device_id"]
    token = customer["device_token"]

    response = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={**_bearer(token), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d2"},
    )
    assert response.status_code == 204, response.text

    # The history row survives: audit is append-only, never deleted.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, unbound_at FROM customer_devices WHERE id = %s",
            (device_id,),
        ).fetchone()
    assert row is not None, "the unbound history row must not be deleted"
    assert row[0] == "UNBOUND"
    assert row[1] is not None

    # The slot view reports slot 1 free and the row in history.
    listing = client.get(DEVICES_PATH, headers=_bearer(token))
    assert listing.status_code == 401
    assert listing.json()["detail"]["code"] == "DEVICE_REVOKED"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        slots, history = _raw_slots_and_history(conn, user_id=customer["user_id"])
    assert slots == {1: None, 2: None}
    assert [device["id"] for device in history] == [device_id]

    # Slot reuse: a fresh BOUND row on slot 1 must be accepted (the partial
    # unique index only guards *current* occupancy — DEV-01 No-Go).
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=str(uuid.uuid4()),
        slot_no=1,
    )


def test_unbind_revokes_session_atomically(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-3", suffix="d3")
    device_id = customer["device_id"]
    token = customer["device_token"]

    response = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={
            **_bearer(token),
            IDEMPOTENCY_KEY_HEADER: "idem-unbind-d3",
            REQUEST_ID_HEADER: "req-unbind-d3",
        },
    )
    assert response.status_code == 204, response.text

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        session = conn.execute(
            "SELECT session_epoch, lease_until FROM customer_session_state WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
        logout = conn.execute(
            "SELECT event, reason, request_id FROM customer_session_events "
            "WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (customer["user_id"],),
        ).fetchone()
    assert session is not None
    # Epoch bumped from 1 to 2 and the lease is already expired at read
    # time: the device that just lost its binding cannot ride the old
    # session. The revoked lease keeps the full PostgreSQL microsecond
    # precision (PR #47 Codex review P2) — it must not survive into the
    # future, so the assertion carries no forward allowance: the unbind
    # transaction committed before this read began, and the fixture PG
    # clock shares the host clock source.
    assert session[0] == 2
    lease_until = datetime.fromisoformat(str(session[1]))
    assert lease_until <= datetime.now(UTC), (
        f"revoked lease must already be expired, got {lease_until!s}"
    )
    assert logout is not None
    assert logout[0] == "LOGOUT"
    assert logout[1] == "device_unbound"
    assert logout[2] == "req-unbind-d3"


def test_delete_missing_device_answers_not_found(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-4", suffix="d4")
    response = client.delete(
        f"{DEVICES_PATH}/{uuid.uuid4()}",
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d4"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_delete_foreign_users_device_answers_not_found(client: TestClient) -> None:
    """IDOR: one device credential must never touch another customer's row."""
    first = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-5a", suffix="d5a")
    second = _activated_customer(client, code=SECOND_CODE, fingerprint="fp-dev-5b", suffix="d5b")
    response = client.delete(
        f"{DEVICES_PATH}/{second['device_id']}",
        headers={**_bearer(first["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d5a"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "DEVICE_NOT_FOUND"
    # The foreign device is untouched.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (second["device_id"],),
        ).fetchone()
    assert status is not None and status[0] == "BOUND"


def test_delete_already_unbound_answers_conflict(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-6", suffix="d6")
    device_id = customer["device_id"]
    # Unbind with the *other* device's credential would not exist here, so
    # unbind own device via a second bound device first? Simpler: a second
    # device holds the slot-2 binding and performs the delete of device 1,
    # then retries the same delete against the now-UNBOUND row.
    _code_id = _code_id_of_user(customer["user_id"])
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id,
        device_id=str(uuid.uuid4()),
        slot_no=2,
    )
    first_delete = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={**_bearer(second_token), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d6"},
    )
    assert first_delete.status_code == 204, first_delete.text
    # The retry burns a *fresh* key: the same key would legitimately replay
    # the sealed 204 (the idempotent-unbind contract below), which is not
    # what this test pins — it pins the fresh-unbind conflict answer.
    retry = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={**_bearer(second_token), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d6-retry"},
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["detail"]["code"] == "DEVICE_ALREADY_UNBOUND"


def test_unbind_other_device_keeps_own_slot_and_session(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-7", suffix="d7")
    second_device_id = str(uuid.uuid4())
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=second_device_id,
        slot_no=2,
    )
    # The activation session rides device 1; unbinding device 2 must not
    # disturb it (one live session per user, device-scoped revocation).
    response = client.delete(
        f"{DEVICES_PATH}/{second_device_id}",
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-unbind-d7",
        },
    )
    assert response.status_code == 204, response.text

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        own = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
        session = conn.execute(
            "SELECT session_epoch, device_id FROM customer_session_state WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
    assert own is not None and own[0] == "BOUND"
    assert session is not None
    assert session[0] == 1, "unbinding the other device must not bump the epoch"
    assert str(session[1]) == customer["device_id"]

    # The caller's own listing still shows slot 1 bound and slot 2 free,
    # with device 2 moved into history.
    listing = client.get(DEVICES_PATH, headers=_bearer(customer["device_token"]))
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["slots"][0]["device"] is not None
    assert len(body["slots"]) == 1
    assert [device["id"] for device in body["history"]] == [second_device_id]


def test_unbound_credential_answers_device_revoked(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-8", suffix="d8")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=str(uuid.uuid4()),
        slot_no=2,
    )
    # Unbind the second device using the first device's credential…
    device_ids = _device_ids_of_user(customer["user_id"])
    second_device_id = [i for i in device_ids if i != customer["device_id"]][0]
    response = client.delete(
        f"{DEVICES_PATH}/{second_device_id}",
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d8"},
    )
    assert response.status_code == 204, response.text
    # …then the second device's own credential must report DEVICE_REVOKED —
    # the client-side signal to wipe its stored credentials (§13.2).
    afterwards = client.get(DEVICES_PATH, headers=_bearer(second_token))
    assert afterwards.status_code == 401, afterwards.text
    assert afterwards.json()["detail"]["code"] == "DEVICE_REVOKED"


# ---------------------------------------------------------------------------
# Third-device block: both slots full
# ---------------------------------------------------------------------------


def test_next_free_slot_reports_availability(devices_dsn: str) -> None:
    from app.customer_device_service import next_free_slot

    close_pg_pool()
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            f"VALUES ('batch-slot', 'slot', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
        )
        digest = compute_code_digest(SLOT_PROBE_CODE, key=TEST_KEY.encode("utf-8"))
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) "
            "VALUES ('code-slot', 'batch-slot', %s, 1, 'XS04-****', "
            "'ISSUED', '2026-01-01T00:00:00+00:00')",
            (digest,),
        )
    try:
        with psycopg.connect(_t16_dsn()) as conn:
            assert next_free_slot(conn, "code-slot") == 1, "empty code: slot 1 is free"
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('slot_u', 'slot_u', 'Slot User', 'customer')"
            )
            _bind_raw(conn, "code-slot", "slot_u", "dev-slot-1", 1)
            assert next_free_slot(conn, "code-slot") == 2, "slot 1 taken: slot 2 is free"
            _bind_raw(conn, "code-slot", "slot_u", "dev-slot-2", 2)
            assert next_free_slot(conn, "code-slot") == 3, "no device capacity limit"
            # Releasing a slot frees it again for reuse.
            conn.execute(
                "UPDATE customer_devices SET status = 'UNBOUND', unbound_at = %s "
                "WHERE id = 'dev-slot-1'",
                (datetime.now(UTC).replace(microsecond=0).isoformat(),),
            )
            assert next_free_slot(conn, "code-slot") == 1, "unbound slot must be reusable"
    finally:
        close_pg_pool()


def test_third_bound_row_is_refused_by_the_database(devices_dsn: str) -> None:
    """The per-user device-limit floor is proven by PostgreSQL itself (086).

    CW-073 removed the hard-coded two-slot DB constraints (uq_customer_devices_slot,
    ck_customer_devices_slot_range); the third-device block now lives in the
    service layer and is answered with 409 ``DEVICE_SLOTS_FULL`` (locked by the
    API tests in this module).  What the database itself still refuses outright
    is a non-positive ``users.max_devices`` — the CHECK 086 installed.
    """
    close_pg_pool()
    try:
        with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('third_u', 'third_u', 'Third User', 'customer')"
            )
            # server_default 2: a fresh user sits at the historical two-device limit.
            seeded = conn.execute("SELECT max_devices FROM users WHERE id = 'third_u'").fetchone()
            assert seeded == (2,), seeded
            # The DB itself refuses a non-positive limit (ck_users_max_devices_positive).
            with pytest.raises(CheckViolation, match="ck_users_max_devices_positive"):
                conn.execute("UPDATE users SET max_devices = 0 WHERE id = 'third_u'")
    finally:
        close_pg_pool()


def test_authentication_accepts_retained_key_versions(client: TestClient) -> None:
    """A credential issued under a retained key version still authenticates.

    The activation transaction signs with the *highest* configured version
    (V2 here); authentication must probe every configured version so a V1
    credential stays valid through a rotation window (PR #44 review P1
    precedent on the fingerprint dimension).
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-9", suffix="d9")
    token = customer["device_token"]

    # Sanity: the token authenticates while both versions are configured…
    response = client.get(DEVICES_PATH, headers=_bearer(token))
    assert response.status_code == 200, response.text

    # …and the digest stored for it is the V2 one.
    from app.customer_device_service import keyed_digest

    v2_digest = keyed_digest(TEST_FINGERPRINT_KEY_V2.encode("utf-8"), token)
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        stored = conn.execute(
            "SELECT token_digest, token_key_version FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
    assert stored is not None
    assert str(stored[0]) == v2_digest
    assert int(stored[1]) == 2

    # Drop V2 from the environment: only V1 remains configured, and the V2
    # credential no longer resolves (rotation must retire, not break, old
    # keys — the probe list simply no longer contains the V2 digest).
    previous = os.environ.get("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2")
    try:
        os.environ.pop("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", None)
        demoted = client.get(DEVICES_PATH, headers=_bearer(token))
        assert demoted.status_code == 401, demoted.text
        assert demoted.json()["detail"]["code"] == "DEVICE_CREDENTIAL_INVALID"
    finally:
        if previous is not None:
            os.environ["VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2"] = previous


# ---------------------------------------------------------------------------
# Review-locked contracts: fail-closed 503s and the REVOKED credential
# ---------------------------------------------------------------------------


def test_device_routes_fail_closed_without_pg_runtime(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """No PG runtime → 503, never a partial or SQLite-lane answer."""
    from app.customer_device_routes import router as customer_device_router

    app = FastAPI()
    app.include_router(customer_device_router)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    close_pg_pool()
    with TestClient(app) as fresh_client:
        response = fresh_client.get(DEVICES_PATH, headers=_bearer("any-token"))
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "DEVICE_SERVICE_UNAVAILABLE"


def test_unconfigured_device_keys_answer_service_unavailable(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """Review P2: key misconfiguration must answer 503, never a 401.

    A 401 here would trick the client into wiping perfectly valid stored
    credentials (the §13.2 client contract) because of an operator-side
    configuration failure — the activation-route precedent answers 503 for
    the same class of failure.
    """
    from app.customer_device_routes import router as customer_device_router

    app = FastAPI()
    app.include_router(customer_device_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", raising=False)
    close_pg_pool()
    try:
        with TestClient(app) as fresh_client:
            response = fresh_client.get(DEVICES_PATH, headers=_bearer("any-token"))
    finally:
        close_pg_pool()
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "DEVICE_SERVICE_UNAVAILABLE"


def test_revoked_status_credential_answers_device_revoked(client: TestClient) -> None:
    """A REVOKED (not just UNBOUND) row also reports DEVICE_REVOKED."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-10", suffix="d10")
    token = customer["device_token"]
    # Flip the row to REVOKED directly (the admin revocation lane is T18).
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_devices SET status = 'REVOKED', revoked_at = %s WHERE id = %s",
            (
                datetime.now(UTC).replace(microsecond=0).isoformat(),
                customer["device_id"],
            ),
        )
    response = client.get(DEVICES_PATH, headers=_bearer(token))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_REVOKED"


# ---------------------------------------------------------------------------
# Idempotent unbind (PR #47 Codex review P2): the sealed-204 recovery
# ---------------------------------------------------------------------------


def test_unbind_requires_idempotency_key(client: TestClient) -> None:
    """The DELETE without an Idempotency-Key answers 400, before anything."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-11", suffix="d11")
    response = client.delete(
        f"{DEVICES_PATH}/{customer['device_id']}", headers=_bearer(customer["device_token"])
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    # The check precedes the credential layer and any envelope write: the
    # device row is untouched and no envelope was created for the attempt.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
        envelopes = conn.execute(
            "SELECT count(*) FROM customer_idempotency_envelopes WHERE operation = 'device_unbind'",
        ).fetchone()
    assert status is not None and status[0] == "BOUND"
    assert envelopes is not None and int(envelopes[0]) == 0


def test_unbind_replays_sealed_204_after_response_loss(client: TestClient) -> None:
    """The lost-204 recovery: same key + same target replays the sealed 204.

    The caller unbound its *own* session-riding device, so the credential it
    retries with is no longer live but still resolves to that historical row.
    The account-scoped recovery must not re-execute the unbind
    (one row flip, one epoch bump, one LOGOUT event).
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-12", suffix="d12")
    device_id = customer["device_id"]
    token = customer["device_token"]
    key = "idem-unbind-replay-d12"
    first = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={
            **_bearer(token),
            IDEMPOTENCY_KEY_HEADER: key,
            REQUEST_ID_HEADER: "req-replay-d12",
        },
    )
    assert first.status_code == 204, first.text
    assert REPLAY_HEADER not in first.headers

    # The retry rides the same released credential and the same key. Matching
    # that historical row proves the account/target scope, but the replay must
    # not expose the original request id.
    retry = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={**_bearer(token), IDEMPOTENCY_KEY_HEADER: key},
    )
    assert retry.status_code == 204, retry.text
    assert retry.headers.get(REPLAY_HEADER) == "true"
    assert retry.headers.get(REQUEST_ID_HEADER) is None

    # The replay re-executed nothing: exactly one row flip, one epoch bump
    # (1 -> 2, not 3) and one LOGOUT event.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, unbound_at, revoked_at FROM customer_devices WHERE id = %s",
            (device_id,),
        ).fetchone()
        session = conn.execute(
            "SELECT session_epoch FROM customer_session_state WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
        logouts = conn.execute(
            "SELECT count(*) FROM customer_session_events WHERE user_id = %s AND event = 'LOGOUT'",
            (customer["user_id"],),
        ).fetchone()
    assert row is not None and row[0] == "UNBOUND" and row[2] is None
    assert session is not None and session[0] == 2
    assert logouts is not None and int(logouts[0]) == 1


def test_unbind_same_key_different_target_isolated_by_scope(client: TestClient) -> None:
    """T45 scopes an unbind key to its authenticated account and target."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-13", suffix="d13")
    second_device_id = str(uuid.uuid4())
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=second_device_id,
        slot_no=2,
    )
    key = "idem-unbind-conflict-d13"
    headers = {**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: key}
    first = client.delete(f"{DEVICES_PATH}/{second_device_id}", headers=headers)
    assert first.status_code == 204, first.text

    # The same client-generated key may safely be reused for a different
    # authenticated target because the target is part of the envelope scope.
    second = client.delete(f"{DEVICES_PATH}/{customer['device_id']}", headers=headers)
    assert second.status_code == 204, second.text
    assert second.headers.get(REPLAY_HEADER) is None

    # Both independent submissions took effect.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        statuses = conn.execute(
            "SELECT id, status FROM customer_devices WHERE id = ANY(%s) ORDER BY id",
            ([second_device_id, customer["device_id"]],),
        ).fetchall()
    assert len(statuses) == 2
    assert {str(row[1]) for row in statuses} == {"UNBOUND"}


def test_unbind_idempotency_key_is_isolated_by_target_device(client: TestClient) -> None:
    """T45 S-2: two accounts may safely reuse the same client key."""
    first = _activated_customer(
        client,
        code=FIRST_CODE,
        fingerprint="fp-unbind-scope-a",
        suffix="unbind-scope-a",
    )
    second = _activated_customer(
        client,
        code=SECOND_CODE,
        fingerprint="fp-unbind-scope-b",
        suffix="unbind-scope-b",
    )
    key = "shared-client-generated-key"

    first_response = client.delete(
        f"{DEVICES_PATH}/{first['device_id']}",
        headers={**_bearer(first["device_token"]), IDEMPOTENCY_KEY_HEADER: key},
    )
    second_response = client.delete(
        f"{DEVICES_PATH}/{second['device_id']}",
        headers={**_bearer(second["device_token"]), IDEMPOTENCY_KEY_HEADER: key},
    )

    assert first_response.status_code == 204
    assert second_response.status_code == 204


def test_unbind_replay_cannot_be_probed_without_the_owning_credential(
    client: TestClient,
) -> None:
    """T45 S-2: a foreign or missing credential cannot discover a sealed 204."""
    owner = _activated_customer(
        client,
        code=FIRST_CODE,
        fingerprint="fp-unbind-owner",
        suffix="unbind-owner",
    )
    other = _activated_customer(
        client,
        code=SECOND_CODE,
        fingerprint="fp-unbind-other",
        suffix="unbind-other",
    )
    key = "shared-replay-probe-key"
    first = client.delete(
        f"{DEVICES_PATH}/{owner['device_id']}",
        headers={**_bearer(owner["device_token"]), IDEMPOTENCY_KEY_HEADER: key},
    )
    assert first.status_code == 204

    unauthenticated = client.delete(
        f"{DEVICES_PATH}/{owner['device_id']}",
        headers={IDEMPOTENCY_KEY_HEADER: key},
    )
    foreign = client.delete(
        f"{DEVICES_PATH}/{owner['device_id']}",
        headers={**_bearer(other["device_token"]), IDEMPOTENCY_KEY_HEADER: key},
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"
    assert foreign.status_code == 404
    assert foreign.json()["detail"]["code"] == "DEVICE_NOT_FOUND"
    assert REPLAY_HEADER not in unauthenticated.headers
    assert REPLAY_HEADER not in foreign.headers
    assert REQUEST_ID_HEADER not in unauthenticated.headers
    assert REQUEST_ID_HEADER not in foreign.headers


# ---------------------------------------------------------------------------
# T17 / DEV-02 — second-device enroll, one-shot pairing and approval
# ---------------------------------------------------------------------------


def _enroll(
    client: TestClient,
    *,
    code: str,
    fingerprint: str,
    key: str,
    name: str = "Second Device",
    platform: str = "macos",
) -> object:
    return client.post(
        ENROLL_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": fingerprint,
            "device_name": name,
            "device_platform": platform,
        },
        headers={IDEMPOTENCY_KEY_HEADER: key},
    )


def _approve(client: TestClient, token: str, pairing_id: str) -> object:
    return client.post(f"{APPROVE_PATH}/{pairing_id}/approve", headers=_bearer(token))


def _dismiss(client: TestClient, token: str, pairing_id: str) -> object:
    return client.delete(f"{APPROVE_PATH}/{pairing_id}", headers=_bearer(token))


def _pairing_row(conn: psycopg.Connection, pairing_id: str) -> tuple | None:
    return conn.execute(
        "SELECT status, candidate_fingerprint_hmac, expires_at, approved_at, "
        "approved_by_device_id, consumed_at, consumed_device_id "
        "FROM device_pairing_requests WHERE id = %s",
        (pairing_id,),
    ).fetchone()


def _expire_pairing(pairing_id: str) -> None:
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE device_pairing_requests SET expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = %s",
            (pairing_id,),
        )


def _count_rows(sql: str) -> int:
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_enroll_requires_idempotency_key(client: TestClient) -> None:
    """The enroll carries a mandatory Idempotency-Key (dev doc §6.3)."""
    response = client.post(
        ENROLL_PATH,
        json={
            "activation_code": FIRST_CODE,
            "device_fingerprint": "fp-pair-none",
            "device_name": "Second Device",
            "device_platform": "macos",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_enroll_creates_pending_pairing_request(client: TestClient) -> None:
    """Step 1+2 of the §12.2 contract: a PENDING request bound to the digest."""
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-1", suffix="p1")
    response = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-1-second", key="idem-p1")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    pairing_id = body["pairing_request_id"]
    assert pairing_id
    assert body["expires_at"]

    from app.customer_device_service import keyed_digest

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None
    assert row[0] == "PENDING"
    assert row[1] == keyed_digest(TEST_FINGERPRINT_KEY_V2.encode("utf-8"), "fp-pair-1-second")
    assert row[3] is None and row[5] is None


def test_enroll_pending_retry_returns_same_pairing(client: TestClient) -> None:
    """A retried enroll (same key + same body) reuses the active request."""
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-2", suffix="p2")
    first = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-2-second", key="idem-p2")
    assert first.status_code == 202, first.text
    second = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-2-second", key="idem-p2")
    assert second.status_code == 202, second.text
    assert second.json()["pairing_request_id"] == first.json()["pairing_request_id"]
    # The PENDING branch never seals an envelope: nothing was spent.
    assert _count_rows("SELECT COUNT(*) FROM device_pairing_requests") == 1
    assert (
        _count_rows(
            "SELECT COUNT(*) FROM customer_idempotency_envelopes WHERE operation = 'device_enroll'"
        )
        == 0
    )


def test_enroll_pending_status_polls_do_not_spend_rate_limit_budget(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """The 0.1.12 waiting screen auto-polls enroll for its pairing outcome.

    A device that already holds an active (unexpired) pairing is polling,
    not probing codes: those polls must not spend the shared ACT-08
    activation budgets, or the default 5-per-window code budget 429s the
    poll within seconds and breaks the auto-consume promise — while a
    stranger's fresh attempts on the same code stay budgeted, so the free
    lane is not an enumeration bypass.
    """
    from app.activation_code_routes import router as activation_code_router
    from app.customer_device_routes import router as customer_device_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_device_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    # The deployment-default code budget (5 per window) with a roomy IP
    # budget, so the assertion pins the code dimension exactly.
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "5")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")
    monkeypatch.setattr("app.activation_code_routes.apply_anti_enumeration_delay", lambda: None)

    with TestClient(app) as tight_client:
        _activated_customer(tight_client, code=FIRST_CODE, fingerprint="fp-poll-1", suffix="pl1")
        first = _enroll(
            tight_client, code=FIRST_CODE, fingerprint="fp-poll-1-second", key="idem-pl1"
        )
        assert first.status_code == 202, first.text

        # Twelve polls with distinct keys (each a fresh budgeted attempt
        # before the fix) must all stay 202 and answer the same pairing row.
        for attempt in range(12):
            poll = _enroll(
                tight_client,
                code=FIRST_CODE,
                fingerprint="fp-poll-1-second",
                key=f"poll-key-{attempt}",
            )
            assert poll.status_code == 202, poll.text
            assert poll.json()["pairing_request_id"] == first.json()["pairing_request_id"]

        # Fresh fingerprints keep consuming the shared budget: the customer
        # activation plus the first enroll spent 2 of 5, so exactly 3 more
        # succeed and the next is 429.
        succeeded = 0
        blocked = None
        for attempt in range(8):
            response = _enroll(
                tight_client,
                code=FIRST_CODE,
                fingerprint=f"fp-poll-stranger-{attempt}",
                key=f"stranger-key-{attempt}",
            )
            if response.status_code == 429:
                blocked = response
                break
            succeeded += 1
        assert blocked is not None, "the stranger lane must still be rate limited"
        assert succeeded == 3, "polls must not have pre-spent the code budget"
        assert blocked.json()["detail"]["code"] == "RATE_LIMITED"


def test_enroll_rejects_unknown_code_unified(client: TestClient) -> None:
    """Anti-enumeration: unknown and unusable codes share one 400 answer."""
    response = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-3", key="idem-p3")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "PAIRING_UNAVAILABLE"


def test_enroll_rejects_unactivated_code_unified(client: TestClient) -> None:
    """An ISSUED (never activated) code answers the same unified 400."""
    _seed_issuable_code(code=SECOND_CODE, code_id="code-p4", batch_id="batch-p4")
    response = _enroll(client, code=SECOND_CODE, fingerprint="fp-pair-4", key="idem-p4")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "PAIRING_UNAVAILABLE"


def test_enroll_rejects_already_bound_fingerprint(client: TestClient) -> None:
    """A fingerprint holding a current binding cannot enroll again."""
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-5", suffix="p5")
    response = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-5", key="idem-p5")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "PAIRING_UNAVAILABLE"
    assert _count_rows("SELECT COUNT(*) FROM device_pairing_requests") == 0


def test_third_device_can_request_pairing(client: TestClient) -> None:
    """Existing bound devices do not cap new pairing requests."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-6", suffix="p6")
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=str(uuid.uuid4()),
        slot_no=2,
    )
    response = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-6-third", key="idem-p6")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "PENDING"
    assert _count_rows("SELECT COUNT(*) FROM device_pairing_requests") == 1


def test_enroll_consumes_approved_pairing_binds_slot2(client: TestClient) -> None:
    """The full §12.2 flow: enroll, approve, enroll again -> credentials.

    The second device binds slot 2 and the chain grows by exactly one
    customer_devices row — no recharge order, no wallet transaction, no
    new user (DEV-02 No-Go: the second device never re-charges).
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-7", suffix="p7")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-7-second", key="idem-p7")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    approval = _approve(client, customer["device_token"], pairing_id)
    assert approval.status_code == 200, approval.text

    consume = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-7-second", key="idem-p7")
    assert consume.status_code == 201, consume.text
    credentials = consume.json()
    assert credentials["device_id"]
    assert credentials["slot_no"] == 2
    assert credentials["device_token"]

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
        device = conn.execute(
            "SELECT slot_no, status, user_id, display_name, platform "
            "FROM customer_devices WHERE id = %s",
            (credentials["device_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "CONSUMED"
    assert row[5] is not None and str(row[6]) == credentials["device_id"]
    assert device is not None
    assert device[0] == 2 and device[1] == "BOUND"
    assert str(device[2]) == customer["user_id"]
    assert device[3] == "Second Device" and device[4] == "macos"

    # No second charge: the pairing chain touches no money tables.
    assert _count_rows("SELECT COUNT(*) FROM recharge_orders") == 1
    assert _count_rows("SELECT COUNT(*) FROM wallet_transactions") == 1
    assert _count_rows("SELECT COUNT(*) FROM users WHERE role = 'customer'") == 1


def test_enroll_consume_response_loss_replays_credentials(client: TestClient) -> None:
    """Lost 201: the same key replays the sealed credentials (§12.2 step 6)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-8", suffix="p8")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-8-second", key="idem-p8")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    assert _approve(client, customer["device_token"], pairing_id).status_code == 200

    first = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-8-second", key="idem-p8")
    assert first.status_code == 201, first.text
    replay = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-8-second", key="idem-p8")
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json() == first.json()

    # One binding, one CONSUMED pairing — the replay re-occupied nothing.
    assert _count_rows("SELECT COUNT(*) FROM customer_devices") == 2
    assert _count_rows("SELECT COUNT(*) FROM device_pairing_requests") == 1


def test_enroll_same_key_different_body_conflicts(client: TestClient) -> None:
    """The spent key answers 409 against a different request body."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-9", suffix="p9")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-9-second", key="idem-p9")
    assert enroll.status_code == 202, enroll.text
    approved = _approve(client, customer["device_token"], enroll.json()["pairing_request_id"])
    assert approved.status_code == 200, approved.text
    consumed = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-9-second", key="idem-p9")
    assert consumed.status_code == 201, consumed.text

    conflict = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-pair-9-second",
        key="idem-p9",
        name="A Different Name",
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_pairing_allocates_distinct_available_slots(client: TestClient) -> None:
    """Two approved candidates race for slot 2: exactly one binds (§12.2-5)."""
    import threading

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-10", suffix="p10")
    pairing_ids: list[str] = []
    for index in (1, 2):
        enroll = _enroll(
            client,
            code=FIRST_CODE,
            fingerprint=f"fp-pair-10-rival-{index}",
            key=f"idem-p10-{index}",
            name=f"Rival {index}",
        )
        assert enroll.status_code == 202, enroll.text
        pairing_id = enroll.json()["pairing_request_id"]
        assert _approve(client, customer["device_token"], pairing_id).status_code == 200
        pairing_ids.append(pairing_id)

    barrier = threading.Barrier(2)
    results: list[tuple[int, str]] = []
    results_lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        response = _enroll(
            client,
            code=FIRST_CODE,
            fingerprint=f"fp-pair-10-rival-{index}",
            key=f"idem-p10-{index}",
            name=f"Rival {index}",
        )
        with results_lock:
            results.append((response.status_code, response.text))

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a concurrent enroll worker hung"

    statuses = sorted(status for status, _ in results)
    assert statuses == [201, 201], results

    assert _count_rows("SELECT COUNT(*) FROM customer_devices WHERE status = 'BOUND'") == 3
    consumed = _count_rows("SELECT COUNT(*) FROM device_pairing_requests WHERE status = 'CONSUMED'")
    assert consumed == 2


def test_enroll_expired_pending_flips_and_creates_new(client: TestClient) -> None:
    """A lapsed PENDING request is lazily expired, not revived."""
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-11", suffix="p11")
    first = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-11-second", key="idem-p11")
    assert first.status_code == 202, first.text
    old_pairing_id = first.json()["pairing_request_id"]
    _expire_pairing(old_pairing_id)

    fresh = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-11-second", key="idem-p11")
    assert fresh.status_code == 202, fresh.text
    new_pairing_id = fresh.json()["pairing_request_id"]
    assert new_pairing_id != old_pairing_id

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        old_row = _pairing_row(conn, old_pairing_id)
        new_row = _pairing_row(conn, new_pairing_id)
    assert old_row is not None and old_row[0] == "EXPIRED"
    assert new_row is not None and new_row[0] == "PENDING"


def test_enroll_approved_then_expired_restarts(client: TestClient) -> None:
    """An approval is time-boxed: a lapsed APPROVED pairing restarts fresh."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-12", suffix="p12")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-12-second", key="idem-p12")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    assert _approve(client, customer["device_token"], pairing_id).status_code == 200
    _expire_pairing(pairing_id)

    again = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-12-second", key="idem-p12")
    assert again.status_code == 202, again.text
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None and row[0] == "EXPIRED"
    assert row[3] is not None  # the lapsed approval stays visible in the audit


def test_pairing_consumption_allocates_third_slot_and_replays(client: TestClient) -> None:
    """A newly occupied slot does not block an approved pairing; replay is stable."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-13", suffix="p13")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-13-second", key="idem-p13")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    assert _approve(client, customer["device_token"], pairing_id).status_code == 200

    # Slot 2 gets taken by another binding before the consume retry.
    rival_device_id = str(uuid.uuid4())
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=rival_device_id,
        slot_no=2,
    )
    created = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-13-second", key="idem-p13")
    assert created.status_code == 201, created.text
    assert created.json()["slot_no"] == 3
    replay = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-13-second", key="idem-p13")
    assert replay.json() == created.json()
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None and row[0] == "CONSUMED"
    assert _count_rows("SELECT COUNT(*) FROM customer_devices WHERE status = 'BOUND'") == 3


def test_approve_requires_bearer_token(client: TestClient) -> None:
    response = client.post(f"{APPROVE_PATH}/{str(uuid.uuid4())}/approve")
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"


def test_approve_happy_path_records_approver(client: TestClient) -> None:
    """Step 3: the first bound device approves; the lineage is recorded."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-14", suffix="p14")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-14-second", key="idem-p14")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    response = _approve(client, customer["device_token"], pairing_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pairing_request_id"] == pairing_id
    assert body["status"] == "APPROVED"

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None
    assert row[0] == "APPROVED"
    assert row[3] is not None
    assert row[4] is not None and str(row[4]) == customer["device_id"]


def test_bound_device_can_dismiss_invalid_pairing_request(client: TestClient) -> None:
    customer = _activated_customer(
        client,
        code=FIRST_CODE,
        fingerprint="fp-pair-dismiss",
        suffix="dismiss",
    )
    enroll = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-pair-dismiss-second",
        key="idem-pair-dismiss",
    )
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    response = _dismiss(client, customer["device_token"], pairing_id)

    assert response.status_code == 204, response.text
    assert _dismiss(client, customer["device_token"], pairing_id).status_code == 204
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        pairing = _pairing_row(conn, pairing_id)
        audit = conn.execute(
            "SELECT action, entity_id FROM audit_logs "
            "WHERE actor_user_id = %s AND action = 'customer.device_pairing.dismissed'",
            (customer["user_id"],),
        ).fetchone()
    assert pairing is not None and pairing[0] == "EXPIRED"
    assert audit == ("customer.device_pairing.dismissed", pairing_id)


def test_activation_code_reset_rotates_code_without_unbinding_current_device(
    client: TestClient,
) -> None:
    customer = _activated_customer(
        client,
        code=FIRST_CODE,
        fingerprint="fp-code-reset",
        suffix="code-reset",
    )
    missing_key = client.post(
        "/api/customer/activation-code/reset",
        headers=_bearer(customer["session_token"]),
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    device_credential_attempt = client.post(
        "/api/customer/activation-code/reset",
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-code-reset-device-token",
        },
    )
    assert device_credential_attempt.status_code == 401

    secondary_id = "device-code-reset-secondary"
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-code-reset",
        device_id=secondary_id,
        slot_no=2,
    )
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state SET device_id = %s WHERE user_id = %s",
            (secondary_id, customer["user_id"]),
        )
    secondary_attempt = client.post(
        "/api/customer/activation-code/reset",
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-code-reset-secondary",
        },
    )
    assert secondary_attempt.status_code == 403
    assert secondary_attempt.json()["detail"]["code"] == "PRIMARY_DEVICE_REQUIRED"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state SET device_id = %s WHERE user_id = %s",
            (customer["device_id"], customer["user_id"]),
        )
        conn.execute("DELETE FROM customer_devices WHERE id = %s", (secondary_id,))
    reset_headers = {
        **_bearer(customer["session_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-code-reset-primary",
    }
    response = client.post(
        "/api/customer/activation-code/reset",
        headers=reset_headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    new_code = payload["activation_code"]
    assert new_code != FIRST_CODE
    assert payload["masked_code"] == mask_activation_code(new_code)
    assert response.headers["cache-control"] == "no-store"

    replay = client.post("/api/customer/activation-code/reset", headers=reset_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json() == payload
    assert replay.headers.get(REPLAY_HEADER) == "true"

    other_key = client.post(
        "/api/customer/activation-code/reset",
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-code-reset-different",
        },
    )
    assert other_key.status_code == 409, other_key.text
    assert other_key.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    # The current device remains authorized, while only the newly issued
    # activation code can start a future pairing request.
    devices = client.get(DEVICES_PATH, headers=_bearer(customer["device_token"]))
    assert devices.status_code == 200, devices.text
    old_attempt = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-code-reset-old",
        key="idem-code-reset-old",
    )
    assert old_attempt.status_code == 400, old_attempt.text
    new_attempt = _enroll(
        client,
        code=new_code,
        fingerprint="fp-code-reset-new",
        key="idem-code-reset-new",
    )
    assert new_attempt.status_code == 202, new_attempt.text

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        code_row = conn.execute(
            "SELECT code_digest, masked_code, status FROM activation_codes "
            "WHERE bound_user_id = %s",
            (customer["user_id"],),
        ).fetchone()
        audit = conn.execute(
            "SELECT action FROM audit_logs "
            "WHERE actor_user_id = %s AND action = 'customer.activation_code.rotated'",
            (customer["user_id"],),
        ).fetchone()
    assert code_row == (
        compute_code_digest(new_code, key=TEST_KEY.encode()),
        payload["masked_code"],
        "ACTIVE",
    )
    assert audit == ("customer.activation_code.rotated",)


def test_approve_missing_or_foreign_pairing_not_found(client: TestClient) -> None:
    """Missing, random and cross-code approvals all answer one 404 (IDOR)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-15", suffix="p15")
    other = _activated_customer(client, code=SECOND_CODE, fingerprint="fp-pair-15b", suffix="p15b")

    missing = _approve(client, customer["device_token"], str(uuid.uuid4()))
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "PAIRING_NOT_FOUND"

    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-15-second", key="idem-p15")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    # A device bound to another code's customer must not approve this one.
    foreign = _approve(client, other["device_token"], pairing_id)
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["detail"]["code"] == "PAIRING_NOT_FOUND"

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None and row[0] == "PENDING"


def test_approve_repeated_returns_current_state(client: TestClient) -> None:
    """The state machine is the idempotency: re-approving answers APPROVED."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-16", suffix="p16")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-16-second", key="idem-p16")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    first = _approve(client, customer["device_token"], pairing_id)
    assert first.status_code == 200, first.text
    second = _approve(client, customer["device_token"], pairing_id)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()


def test_approve_after_consumption_conflicts(client: TestClient) -> None:
    """CONSUMED is terminal: the one-shot request cannot be re-approved."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-17", suffix="p17")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-17-second", key="idem-p17")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    assert _approve(client, customer["device_token"], pairing_id).status_code == 200
    consumed = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-17-second", key="idem-p17")
    assert consumed.status_code == 201, consumed.text

    response = _approve(client, customer["device_token"], pairing_id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PAIRING_ALREADY_CONSUMED"


def test_approve_expired_conflicts(client: TestClient) -> None:
    """A lapsed request flips EXPIRED and refuses the approval."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-18", suffix="p18")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-18-second", key="idem-p18")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    _expire_pairing(pairing_id)

    response = _approve(client, customer["device_token"], pairing_id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PAIRING_EXPIRED"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None and row[0] == "EXPIRED"


def test_approve_self_approval_rejected(client: TestClient) -> None:
    """Defensive: a pairing row naming the approver's own digest is refused."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-19", suffix="p19")
    code_id = _code_id_of_user(customer["user_id"])
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        fingerprint_row = conn.execute(
            "SELECT fingerprint_hmac FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
        assert fingerprint_row is not None
        conn.execute(
            "INSERT INTO device_pairing_requests "
            "(id, activation_code_id, candidate_fingerprint_hmac, "
            " candidate_fingerprint_key_version, display_name, platform, status, expires_at) "
            "VALUES (%s, %s, %s, 2, 'Ghost Device', 'windows', 'PENDING', %s)",
            (
                str(uuid.uuid4()),
                code_id,
                fingerprint_row[0],
                FUTURE_EXPIRY,
            ),
        )
        pairing_id = conn.execute(
            "SELECT id FROM device_pairing_requests WHERE activation_code_id = %s",
            (code_id,),
        ).fetchone()
    assert pairing_id is not None

    response = _approve(client, customer["device_token"], str(pairing_id[0]))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PAIRING_SELF_APPROVAL"


def test_pairing_approval_not_transferable(client: TestClient) -> None:
    """The approval binds the candidate digest: another device cannot use it."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-20", suffix="p20")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-20-second", key="idem-p20")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    assert _approve(client, customer["device_token"], pairing_id).status_code == 200

    # A different fingerprint enrolling the same code gets its own PENDING
    # request — it must not consume the approved pairing of the candidate.
    stranger = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-pair-20-stranger",
        key="idem-p20-stranger",
        name="Stranger Device",
    )
    assert stranger.status_code == 202, stranger.text
    assert stranger.json()["pairing_request_id"] != pairing_id

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        approved_row = _pairing_row(conn, pairing_id)
    assert approved_row is not None and approved_row[0] == "APPROVED"
    assert _count_rows("SELECT COUNT(*) FROM customer_devices") == 1


# ---------------------------------------------------------------------------
# PR #49 Codex review fixes — regression locks
# ---------------------------------------------------------------------------


def test_approve_revoked_credential_mid_flight_rejected(client: TestClient) -> None:
    """P1 fix: a credential released after authentication cannot approve.

    The route's ``_authenticate`` snapshot is unlocked; the approval
    transaction must re-lock and re-validate the approver row as ``BOUND``.
    Simulating the mid-flight unbind: the binding is released directly, then
    the (stale) authenticated device object is fed to the service layer —
    exactly the object a winning unbind race would leave behind.
    """
    from app.customer_device_service import AuthenticatedDevice, approve_pairing_request

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-21", suffix="p21")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-21-second", key="idem-p21")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    # The unbind commits *after* the route authenticated the bearer token.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_devices SET status = 'UNBOUND', unbound_at = %s WHERE id = %s",
            ("2026-08-23T00:00:00+00:00", customer["device_id"]),
        )

    stale_approver = AuthenticatedDevice(
        id=customer["device_id"],
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        slot_no=1,
        display_name="First Device",
        platform="windows",
    )
    with psycopg.connect(_t16_dsn()) as conn:
        with conn.transaction():
            outcome = approve_pairing_request(
                conn,
                pairing_id=pairing_id,
                approver_device=stale_approver,
                server_now=datetime.now(UTC),
            )
    assert outcome == "revoked"

    # The pairing row was left untouched: no approval from a released device.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None and row[0] == "PENDING" and row[3] is None


def test_approve_rejects_non_first_device_while_first_bound(client: TestClient) -> None:
    """PR #49 GitHub Codex review P1 (approval scope): §12.2 step 3 — the
    approval belongs to the *first* currently-bound device
    (``activation_code_activations.first_device_id``), never to every device
    sharing the activation code. While the first device is alive and bound,
    the slot-2 device cannot approve a candidate; the first device's own
    approval still works; and the authorization precedes the state machine
    (re-approving an already-APPROVED pairing stays the first device's lane).
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-23", suffix="p23")
    # A fresh PENDING pairing while slot 2 is still free.
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-23-candidate", key="idem-p23")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    # Bind slot 2 directly (the T17 enroll flow is not what this test pins).
    slot2_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=str(uuid.uuid4()),
        slot_no=2,
    )

    # The slot-2 device is not the first device: 403, and the pairing row
    # stays PENDING (the refusal names the lane, no enumeration oracle — the
    # caller is a legitimate device of this very code).
    refused = _approve(client, slot2_token, pairing_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "PAIRING_APPROVER_FORBIDDEN"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None and row[0] == "PENDING" and row[3] is None

    # The first device approves the very same pairing without friction.
    approved = _approve(client, customer["device_token"], pairing_id)
    assert approved.status_code == 200, approved.text

    # The authorization precedes the state machine: an already-APPROVED
    # pairing is still not re-approvable by the slot-2 device (the 403, not
    # the first device's idempotent 200 already-approved answer).
    refused_again = _approve(client, slot2_token, pairing_id)
    assert refused_again.status_code == 403, refused_again.text
    assert refused_again.json()["detail"]["code"] == "PAIRING_APPROVER_FORBIDDEN"


def test_approve_rejects_slot2_after_first_device_unbound(client: TestClient) -> None:
    """PR #49 GitHub Codex review P1 (approval scope): once the original
    first device is unbound while slot 2 stays bound, the surviving slot-2
    device must not inherit the approval — §12.2 step 3 routes that lane to
    the administrator verification (the T18 control API, §6.1), not down to
    the remaining device.
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-24", suffix="p24")
    # The full six-step flow binds slot 2 (the first device approves).
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-24-second", key="idem-p24")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    assert _approve(client, customer["device_token"], pairing_id).status_code == 200
    second = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-24-second", key="idem-p24")
    assert second.status_code == 201, second.text
    slot2_token = second.json()["device_token"]

    # Release the original first device (self-unbind via its own credential).
    unbind = client.delete(
        f"{DEVICES_PATH}/{customer['device_id']}",
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-p24-unbind"},
    )
    assert unbind.status_code == 204, unbind.text

    # A fresh candidate enrolls (slot 1 is free again).
    candidate = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-24-third", key="idem-p24b")
    assert candidate.status_code == 202, candidate.text
    new_pairing_id = candidate.json()["pairing_request_id"]

    # The surviving slot-2 device is NOT the first device: 403, the pairing
    # stays PENDING — the administrator verification (T18) is the only lane.
    refused = _approve(client, slot2_token, new_pairing_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "PAIRING_APPROVER_FORBIDDEN"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, new_pairing_id)
    assert row is not None and row[0] == "PENDING" and row[3] is None


def test_enroll_consume_after_key_rotation_keeps_pairing_version(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 fix: consumption copies the pairing row's digest *and* its version.

    A rotation between the 202 and the 201 must not mislabel the stored
    fingerprint: the digest was keyed under V1 when the request was created,
    and the ``customer_devices`` row must record (V1 digest, version 1) —
    not the stale digest stamped with the new highest version.
    """
    from app.customer_device_service import keyed_digest

    # Only V1 is configured while the pairing request is created.
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2")
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-22", suffix="p22")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-22-second", key="idem-p22")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    # The rotation window opens: V2 joins the configuration as the highest.
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    assert _approve(client, customer["device_token"], pairing_id).status_code == 200
    consume = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-22-second", key="idem-p22")
    assert consume.status_code == 201, consume.text

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT fingerprint_hmac, fingerprint_key_version FROM customer_devices WHERE id = %s",
            (consume.json()["device_id"],),
        ).fetchone()
    assert row is not None
    assert int(row[1]) == 1, "the stored version must match the digest's key version"
    assert str(row[0]) == keyed_digest(TEST_FINGERPRINT_KEY_V1.encode("utf-8"), "fp-pair-22-second")


def test_approve_lapsed_approved_flips_expired_and_restarts(client: TestClient) -> None:
    """P2 fix: a lapsed APPROVED flips to EXPIRED on the approve path too.

    Without the flip the dead row would keep occupying the partial-unique
    active index until some later enroll touched it — the approve endpoint
    itself must release the occupancy while preserving the approval lineage.
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-pair-23", suffix="p23")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-23-second", key="idem-p23")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    assert _approve(client, customer["device_token"], pairing_id).status_code == 200
    _expire_pairing(pairing_id)

    response = _approve(client, customer["device_token"], pairing_id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PAIRING_EXPIRED"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None and row[0] == "EXPIRED"
    assert row[3] is not None  # the lapsed approval stays visible in the audit

    # The released occupancy admits a fresh request for the same candidate.
    again = _enroll(client, code=FIRST_CODE, fingerprint="fp-pair-23-second", key="idem-p23")
    assert again.status_code == 202, again.text
    new_pairing_id = again.json()["pairing_request_id"]
    assert new_pairing_id != pairing_id
    assert _approve(client, customer["device_token"], new_pairing_id).status_code == 200


def _insert_pairing_row(
    *,
    pairing_id: str,
    activation_code_id: str,
    fingerprint_digest: str,
    status: str,
    expires_at: str = FUTURE_EXPIRY,
    approved_at: str | None = None,
    approved_by_device_id: str | None = None,
    approved_by_admin_user_id: str | None = None,
    consumed_at: str | None = None,
    consumed_device_id: str | None = None,
) -> None:
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO device_pairing_requests "
            "(id, activation_code_id, candidate_fingerprint_hmac, "
            " candidate_fingerprint_key_version, display_name, platform, status, "
            " expires_at, approved_at, approved_by_device_id, "
            " approved_by_admin_user_id, consumed_at, consumed_device_id) "
            "VALUES (%s, %s, %s, 1, 'Shape Probe', 'windows', %s, %s, %s, %s, %s, %s, %s)",
            (
                pairing_id,
                activation_code_id,
                fingerprint_digest,
                status,
                expires_at,
                approved_at,
                approved_by_device_id,
                approved_by_admin_user_id,
                consumed_at,
                consumed_device_id,
            ),
        )


def test_pairing_status_shape_rejects_malformed_rows(route_state: str) -> None:
    """P2 fix: the four-state CHECK rejects half-written approval columns."""
    _seed_issuable_code(FIRST_CODE, code_id="code-shape", batch_id="batch-shape")

    # PENDING carrying an approved_at: no transition column is allowed.
    with pytest.raises(CheckViolation):
        _insert_pairing_row(
            pairing_id="pairing-shape-pending",
            activation_code_id="code-shape",
            fingerprint_digest="digest-shape",
            status="PENDING",
            approved_at="2026-08-23T00:00:00+00:00",
        )

    # EXPIRED with a lone approved_at (no approved_by_device_id): the
    # approval columns must arrive as a pair.
    with pytest.raises(CheckViolation):
        _insert_pairing_row(
            pairing_id="pairing-shape-expired",
            activation_code_id="code-shape",
            fingerprint_digest="digest-shape",
            status="EXPIRED",
            approved_at="2026-08-23T00:00:00+00:00",
        )


def test_pairing_active_unique_and_terminal_reuse(route_state: str) -> None:
    """The partial unique index: one active row per (code, digest), terminal reuse."""
    _seed_issuable_code(FIRST_CODE, code_id="code-uniq", batch_id="batch-uniq")
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES ('dev-uniq', 'code-uniq', 'admin_u', 1, 'Uniq Probe', 'windows', "
            "'fp-uniq', 1, 'tok-uniq', 1)"
        )

    _insert_pairing_row(
        pairing_id="pairing-uniq-1",
        activation_code_id="code-uniq",
        fingerprint_digest="digest-uniq",
        status="PENDING",
    )
    with pytest.raises(UniqueViolation):
        _insert_pairing_row(
            pairing_id="pairing-uniq-2",
            activation_code_id="code-uniq",
            fingerprint_digest="digest-uniq",
            status="PENDING",
        )

    # EXPIRED is terminal: the same (code, digest) may start a fresh row.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE device_pairing_requests SET status = 'EXPIRED' WHERE id = 'pairing-uniq-1'"
        )
    _insert_pairing_row(
        pairing_id="pairing-uniq-3",
        activation_code_id="code-uniq",
        fingerprint_digest="digest-uniq",
        status="PENDING",
    )

    # APPROVED is active: it still blocks a second active row.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE device_pairing_requests SET status = 'APPROVED', "
            "approved_at = '2026-08-23T00:00:00+00:00', "
            "approved_by_device_id = 'dev-uniq' WHERE id = 'pairing-uniq-3'"
        )
    with pytest.raises(UniqueViolation):
        _insert_pairing_row(
            pairing_id="pairing-uniq-4",
            activation_code_id="code-uniq",
            fingerprint_digest="digest-uniq",
            status="PENDING",
        )

    # CONSUMED is terminal too: the candidate may pair again afterwards.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE device_pairing_requests SET status = 'CONSUMED', "
            "consumed_at = '2026-08-23T00:00:00+00:00', "
            "consumed_device_id = 'dev-uniq' WHERE id = 'pairing-uniq-3'"
        )
    _insert_pairing_row(
        pairing_id="pairing-uniq-5",
        activation_code_id="code-uniq",
        fingerprint_digest="digest-uniq",
        status="PENDING",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _code_id_of_user(user_id: str) -> str:
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT d.activation_code_id FROM customer_devices d "
            "WHERE d.user_id = %s ORDER BY d.created_at LIMIT 1",
            (user_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _device_ids_of_user(user_id: str) -> list[str]:
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT id FROM customer_devices WHERE user_id = %s ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _raw_slots_and_history(conn: psycopg.Connection, *, user_id: str) -> tuple[dict, list]:
    """Service-free view for asserting on rows directly."""
    rows = conn.execute(
        "SELECT slot_no, id, status FROM customer_devices WHERE user_id = %s ORDER BY created_at",
        (user_id,),
    ).fetchall()
    slots: dict[int, str | None] = {1: None, 2: None}
    history: list[dict] = []
    for slot_no, device_id, status in rows:
        if str(status) == "BOUND":
            slots[int(slot_no)] = str(device_id)
        else:
            history.append({"id": str(device_id), "status": str(status)})
    return slots, history


def _bind_raw(
    conn: psycopg.Connection,
    code_id: str,
    user_id: str,
    device_id: str,
    slot_no: int,
) -> None:
    """Insert one BOUND device row directly with V1-keyed digests.

    The insert runs in its own savepoint so the UniqueViolation cases of
    the third-device tests leave the surrounding transaction usable.
    """
    from app.customer_device_service import keyed_digest

    key_v1 = TEST_FINGERPRINT_KEY_V1.encode("utf-8")
    with conn.transaction():
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES (%s, %s, %s, %s, 'Raw Device', 'windows', %s, 1, %s, 1)",
            (
                device_id,
                code_id,
                user_id,
                slot_no,
                keyed_digest(key_v1, f"fp-raw-{device_id}"),
                keyed_digest(key_v1, f"tok-raw-{device_id}"),
            ),
        )


# ---------------------------------------------------------------------------
# T18 — administrator device operations (approve / unbind / revoke)
# ---------------------------------------------------------------------------


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    """Exchange a real admin session cookie + CSRF header (the T12 pattern)."""
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _admin_write(
    client: TestClient,
    headers: dict[str, str],
    path: str,
    *,
    key: str | None = None,
    reason: str = "客服核验工单 #1001",
    confirm: bool = True,
) -> object:
    all_headers = dict(headers)
    all_headers[IDEMPOTENCY_KEY_HEADER] = key or f"key-{uuid.uuid4()}"
    return client.post(
        path,
        json={"confirm": confirm, "reason": reason},
        headers=all_headers,
    )


def _admin_event_row(event: str) -> tuple | None:
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        return conn.execute(
            "SELECT event, admin_user_id, target_user_id, device_id, "
            "pairing_request_id, activation_code_id, reason, request_id "
            "FROM admin_device_events WHERE event = %s",
            (event,),
        ).fetchone()


def test_admin_device_list_gates_and_hides_secrets(client: TestClient) -> None:
    """§6.2 / §15: the device list sits behind the admin session gate, the
    auditor may read it, and keyed digests never leave the store."""
    unauthenticated = client.get(ADMIN_DEVICES_PATH)
    assert unauthenticated.status_code == 401

    customer = _activated_customer(
        client, code=FIRST_CODE, fingerprint="fp-admin-list", suffix="alist"
    )
    listed = client.get(ADMIN_DEVICES_PATH, headers=_admin_session(client))
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["device_id"] == customer["device_id"]
    assert item["status"] == "BOUND"
    assert item["slot_no"] == 1
    assert item["username"]
    assert item["activation_code"] == "XS04-****"
    assert item["online"] is True
    assert listed.json()["summary"]["bound"] == 1
    assert listed.json()["summary"]["online"] == 1
    serialized = str(listed.json())
    assert "fingerprint" not in serialized
    assert "token_digest" not in serialized

    # The auditor is read-only, and this is the read path (§15).
    client.cookies.clear()
    auditor_view = client.get(ADMIN_DEVICES_PATH, headers=_admin_session(client, "auditor_u"))
    assert auditor_view.status_code == 200
    assert len(auditor_view.json()["items"]) == 1


def test_admin_device_list_does_not_report_unbound_device_online(client: TestClient) -> None:
    customer = _activated_customer(
        client, code=FIRST_CODE, fingerprint="fp-admin-offline", suffix="aoffline"
    )
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_devices SET status='UNBOUND', unbound_at=now() WHERE id=%s",
            (customer["device_id"],),
        )

    listed = client.get(ADMIN_DEVICES_PATH, headers=_admin_session(client))

    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["online"] is False
    assert listed.json()["summary"]["online"] == 0


def test_admin_device_history_cards_share_customer_scope_and_stable_pagination(
    client: TestClient,
) -> None:
    customer = _activated_customer(
        client, code=FIRST_CODE, fingerprint="fp-history", suffix="history"
    )
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-history",
        device_id="history-current-second",
        slot_no=2,
    )
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE users SET display_name='历史设备公司' WHERE id=%s", (customer["user_id"],)
        )
        for index in range(53):
            conn.execute(
                """
                INSERT INTO customer_devices
                (id,activation_code_id,user_id,slot_no,display_name,platform,status,
                 fingerprint_hmac,fingerprint_key_version,token_digest,token_key_version,
                 bound_at,unbound_at,created_at,last_active_at)
                SELECT %s,activation_code_id,user_id,1,'历史电脑','windows','UNBOUND',
                       %s,1,%s,1,(now()-interval '2 hours')::text,
                       (now()-interval '1 hour')::text,(now()-interval '3 hours')::text,
                       (now()-interval '90 minutes')::text
                FROM customer_devices WHERE id=%s
                """,
                (
                    f"history-{index:03d}",
                    f"synthetic-fingerprint-{index}",
                    f"synthetic-digest-{index}",
                    customer["device_id"],
                ),
            )
    headers = _admin_session(client)
    params = {"keyword": "历史设备公司", "user_id": customer["user_id"], "limit": 50}
    first = client.get(ADMIN_DEVICES_PATH, headers=headers, params=params).json()
    second = client.get(ADMIN_DEVICES_PATH, headers=headers, params={**params, "offset": 50}).json()
    assert first["total"] == second["total"] == 55
    assert len(first["items"]) == 50 and len(second["items"]) == 5
    assert {row["device_id"] for row in first["items"]}.isdisjoint(
        {row["device_id"] for row in second["items"]}
    )
    assert first["summary"] == second["summary"]
    assert first["summary"]["online_customers"] == 1
    assert first["summary"]["at_slot_limit"] == 1
    assert first["summary"]["frequent_swaps_24h"] == 1
    history = next(row for row in first["items"] if row["status"] == "UNBOUND")
    assert history["company_name"] == "历史设备公司"
    assert datetime.fromisoformat(history["first_bound_at"]) < datetime.fromisoformat(
        history["bound_at"]
    )
    assert history["last_active_at"] and not history["online"]
    assert history["session_id"] is None and history["session_epoch"] is None
    for attention, total in [
        ("online", 1),
        ("offline", 54),
        ("at_slot_limit", 55),
        ("frequent_swaps_24h", 55),
    ]:
        filtered = client.get(
            ADMIN_DEVICES_PATH, headers=headers, params={**params, "attention": attention}
        ).json()
        assert filtered["total"] == total
        assert filtered["summary"] == first["summary"]
    platform = client.get(
        ADMIN_DEVICES_PATH, headers=headers, params={**params, "platform": "macos"}
    ).json()
    assert platform["total"] == 1 and platform["summary"]["online_customers"] == 0
    assert platform["summary"]["at_slot_limit"] == 1
    empty = client.get(
        ADMIN_DEVICES_PATH, headers=headers, params={**params, "keyword": "别的公司"}
    ).json()
    assert empty["total"] == 0 and all(value == 0 for value in empty["summary"].values())
    assert (
        client.get(
            ADMIN_DEVICES_PATH, headers=headers, params={**params, "attention": "invalid"}
        ).status_code
        == 422
    )


def test_admin_approve_rejects_live_first_device(client: TestClient) -> None:
    """§12.2 step 3: a live first device owns the approval — the admin lane
    fails closed with 403 and leaves no audit row behind."""
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-ap1", suffix="ap1")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-adm-ap1-2nd", key="idem-ap1")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    refused = _admin_write(
        client, _admin_session(client), f"{ADMIN_PAIRINGS_PATH}/{pairing_id}/approve"
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "PAIRING_FIRST_DEVICE_AVAILABLE"

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None and row[0] == "PENDING"
    assert _admin_event_row("PAIRING_ADMIN_APPROVED") is None


def test_admin_activation_list_includes_pending_pairings(client: TestClient) -> None:
    customer = _activated_customer(
        client, code=FIRST_CODE, fingerprint="fp-adm-list-pair", suffix="alistpair"
    )
    enroll = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-adm-list-pair-new",
        key="idem-adm-list-pair",
        name="重装后的电脑",
        platform="windows",
    )
    assert enroll.status_code == 202, enroll.text

    listed = client.get("/api/control/activation-codes", headers=_admin_session(client))

    assert listed.status_code == 200, listed.text
    code_id = _code_id_of_user(customer["user_id"])
    item = next(item for item in listed.json()["items"] if item["code_id"] == code_id)
    assert item["pending_pairings"] == [
        {
            "pairing_request_id": enroll.json()["pairing_request_id"],
            "display_name": "重装后的电脑",
            "platform": "windows",
            "status": "PENDING",
            "created_at": item["pending_pairings"][0]["created_at"],
            "expires_at": item["pending_pairings"][0]["expires_at"],
        }
    ]


def test_admin_replaces_bound_device_and_candidate_finishes_automatically(
    client: TestClient,
) -> None:
    customer = _activated_customer(
        client, code=FIRST_CODE, fingerprint="fp-adm-replace-old", suffix="replace"
    )
    enroll = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-adm-replace-new",
        key="idem-adm-replace-new",
        name="重装后的电脑",
        platform="windows",
    )
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]
    headers = {
        **_admin_session(client),
        IDEMPOTENCY_KEY_HEADER: "idem-admin-replace",
    }
    payload = {
        "replace_device_id": customer["device_id"],
        "confirm": True,
        "reason": "客户重装系统，替换无法登录的旧设备",
    }

    replaced = client.post(
        f"{ADMIN_PAIRINGS_PATH}/{pairing_id}/replace-device",
        json=payload,
        headers=headers,
    )
    replayed = client.post(
        f"{ADMIN_PAIRINGS_PATH}/{pairing_id}/replace-device",
        json=payload,
        headers=headers,
    )

    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["status"] == "APPROVED"
    assert replaced.json()["replaced_device_id"] == customer["device_id"]
    assert replayed.status_code == 200, replayed.text
    assert replayed.headers.get(REPLAY_HEADER) == "true"

    consumed = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-adm-replace-new",
        key="idem-adm-replace-consume",
        name="重装后的电脑",
        platform="windows",
    )
    assert consumed.status_code == 201, consumed.text
    new_device_id = consumed.json()["device_id"]
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        old_device = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
        first_device = conn.execute(
            "SELECT first_device_id FROM activation_code_activations WHERE code_id = %s",
            (_code_id_of_user(customer["user_id"]),),
        ).fetchone()
        events = conn.execute(
            "SELECT event, count(*) FROM admin_device_events GROUP BY event ORDER BY event"
        ).fetchall()
    assert old_device is not None and old_device[0] == "UNBOUND"
    assert first_device is not None and str(first_device[0]) == new_device_id
    assert dict(events)["DEVICE_ADMIN_UNBOUND"] == 1
    assert dict(events)["PAIRING_ADMIN_APPROVED"] == 1

    future = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-adm-replace-future",
        key="idem-adm-replace-future",
    )
    assert future.status_code == 202, future.text
    approved = _approve(
        client, consumed.json()["device_token"], future.json()["pairing_request_id"]
    )
    assert approved.status_code == 200, approved.text


def test_admin_approve_opens_after_first_device_release(client: TestClient) -> None:
    """The fallback lane: first device released → the admin approval lands
    with its lineage and one audit row (the T18 DoD: 真实 actor、原因、
    二次确认和审计存在)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-ap2", suffix="ap2")
    release = client.delete(
        f"{DEVICES_PATH}/{customer['device_id']}",
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-ap2-rel"},
    )
    assert release.status_code == 204, release.text

    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-adm-ap2-2nd", key="idem-ap2")
    assert enroll.status_code == 202, enroll.text
    pairing_id = enroll.json()["pairing_request_id"]

    approved = _admin_write(
        client,
        _admin_session(client),
        f"{ADMIN_PAIRINGS_PATH}/{pairing_id}/approve",
        key="adm-ap2",
        reason="客服核验发放记录后批准",
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "APPROVED"
    assert body["outcome"] == "approved"

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, approved_at, approved_by_device_id, approved_by_admin_user_id "
            "FROM device_pairing_requests WHERE id = %s",
            (pairing_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "APPROVED"
    assert row[1] is not None
    assert row[2] is None
    assert str(row[3]) == "admin_u"

    event = _admin_event_row("PAIRING_ADMIN_APPROVED")
    assert event is not None
    assert str(event[1]) == "admin_u"
    assert str(event[2]) == customer["user_id"]
    assert event[3] is None  # the approval proves the pairing, not a device
    assert str(event[4]) == pairing_id
    assert str(event[5]) == _code_id_of_user(customer["user_id"])
    assert str(event[6]) == "客服核验发放记录后批准"
    assert str(event[7]) == body["request_id"]

    # A second approval (different key) is idempotent: the state stays and
    # the outcome names it, without a second audit row.
    again = _admin_write(
        client, _admin_session(client), f"{ADMIN_PAIRINGS_PATH}/{pairing_id}/approve"
    )
    assert again.status_code == 200, again.text
    assert again.json()["outcome"] == "already_approved"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM admin_device_events WHERE event = 'PAIRING_ADMIN_APPROVED'"
        ).fetchone()
    assert count is not None and int(count[0]) == 1


def test_admin_approve_outcomes(client: TestClient) -> None:
    """404 unknown pairing, 409 expired (lazily flipped), 409 consumed."""
    headers = _admin_session(client)
    missing = _admin_write(client, headers, f"{ADMIN_PAIRINGS_PATH}/{uuid.uuid4()}/approve")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "PAIRING_NOT_FOUND"

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-ap3", suffix="ap3")
    release = client.delete(
        f"{DEVICES_PATH}/{customer['device_id']}",
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-ap3-rel"},
    )
    assert release.status_code == 204, release.text
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-adm-ap3-2nd", key="idem-ap3")
    pairing_id = enroll.json()["pairing_request_id"]

    _expire_pairing(pairing_id)
    expired_key = "idem-ap3-expired"
    expired = _admin_write(
        client, headers, f"{ADMIN_PAIRINGS_PATH}/{pairing_id}/approve", key=expired_key
    )
    assert expired.status_code == 409, expired.text
    assert expired.json()["detail"]["code"] == "PAIRING_EXPIRED"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM device_pairing_requests WHERE id = %s", (pairing_id,)
        ).fetchone()
    assert status is not None and status[0] == "EXPIRED"
    # The deferred 409 replay lock (session review P3): the same key must
    # answer the same 409 — the snapshot layer recorded the error response
    # while committing the lazy flip, so the replay must not re-execute.
    replayed_expired = _admin_write(
        client, headers, f"{ADMIN_PAIRINGS_PATH}/{pairing_id}/approve", key=expired_key
    )
    assert replayed_expired.status_code == 409, replayed_expired.text
    assert replayed_expired.headers.get(REPLAY_HEADER) == "true"
    assert replayed_expired.json() == expired.json()

    consumed_pairing = str(uuid.uuid4())
    _insert_pairing_row(
        pairing_id=consumed_pairing,
        activation_code_id=_code_id_of_user(customer["user_id"]),
        fingerprint_digest="digest-ap3-consumed",
        status="CONSUMED",
        approved_at="2026-01-01T00:00:00+00:00",
        approved_by_admin_user_id="admin_u",
        consumed_at="2026-01-01T00:01:00+00:00",
        consumed_device_id=customer["device_id"],
    )
    consumed = _admin_write(client, headers, f"{ADMIN_PAIRINGS_PATH}/{consumed_pairing}/approve")
    assert consumed.status_code == 409, consumed.text
    assert consumed.json()["detail"]["code"] == "PAIRING_ALREADY_CONSUMED"


def test_admin_pairing_verification_view(client: TestClient) -> None:
    """The verification view carries the full §12.2 step-3 evidence: the
    masked code, its delivery records (connector review P2 — who delivered,
    on which channel, to which recipient), the activation fact and the
    first-device status behind the admin_lane summary."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-vv", suffix="vv")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-adm-vv-2nd", key="idem-vv")
    pairing_id = enroll.json()["pairing_request_id"]
    code_id = _code_id_of_user(customer["user_id"])
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_deliveries "
            "(id, code_id, channel, external_order_ref, recipient_ref, "
            "delivered_by_user_id, delivered_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                "del-vv-1",
                code_id,
                "manual",
                "T18-VV-ORDER",
                "customer-vv@example.com",
                "admin_u",
                "2026-01-02T00:00:00+00:00",
            ),
        )

    view = client.get(f"{ADMIN_PAIRINGS_PATH}/{pairing_id}", headers=_admin_session(client))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["pairing"]["id"] == pairing_id
    assert body["pairing"]["activation_code_id"] == code_id
    assert body["pairing"]["status"] == "PENDING"
    assert body["code"]["code_id"] == code_id
    assert body["code"]["masked_code"]  # masked evidence, never plaintext
    assert body["activation"]["user_id"] == customer["user_id"]
    assert body["first_device"]["device_id"] == customer["device_id"]
    assert body["first_device"]["status"] == "BOUND"
    # The first device is still bound — the fallback lane stays closed.
    assert body["admin_lane"] == "CLOSED_FIRST_DEVICE_BOUND"
    assert len(body["deliveries"]) == 1
    delivery = body["deliveries"][0]
    assert delivery["channel"] == "manual"
    assert delivery["external_order_ref"] == "T18-VV-ORDER"
    assert delivery["recipient_ref"] == "customer-vv@example.com"
    assert delivery["delivered_by_user_id"] == "admin_u"

    missing = client.get(f"{ADMIN_PAIRINGS_PATH}/{uuid.uuid4()}", headers=_admin_session(client))
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "PAIRING_NOT_FOUND"


def test_admin_unbind_releases_device_and_riding_session(client: TestClient) -> None:
    """The admin unbind mirrors the customer lane (§9.2) with the actor
    recorded as the administrator and one audit row."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-un1", suffix="un1")
    device_id = customer["device_id"]

    unbound = _admin_write(
        client,
        _admin_session(client),
        f"{ADMIN_DEVICES_PATH}/{device_id}/unbind",
        key="adm-un1",
        reason="客户手机丢失，客服解绑",
    )
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["status"] == "UNBOUND"

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, unbound_at, revoked_at FROM customer_devices WHERE id = %s",
            (device_id,),
        ).fetchone()
        session = conn.execute(
            "SELECT session_epoch, lease_until FROM customer_session_state WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
        logout = conn.execute(
            "SELECT event, actor_user_id, reason FROM customer_session_events "
            "WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (customer["user_id"],),
        ).fetchone()
    assert row is not None and row[0] == "UNBOUND" and row[1] is not None and row[2] is None
    assert session is not None and session[0] == 2
    assert datetime.fromisoformat(str(session[1])) <= datetime.now(UTC)
    assert logout is not None and logout[0] == "LOGOUT"
    assert str(logout[1]) == "admin_u"
    assert str(logout[2]) == "客户手机丢失，客服解绑"

    event = _admin_event_row("DEVICE_ADMIN_UNBOUND")
    assert event is not None
    assert str(event[1]) == "admin_u"
    assert str(event[2]) == customer["user_id"]
    assert str(event[3]) == device_id
    assert event[4] is None
    assert str(event[6]) == "客户手机丢失，客服解绑"
    assert str(event[7]) == unbound.json()["request_id"]

    afterwards = client.get(DEVICES_PATH, headers=_bearer(customer["device_token"]))
    assert afterwards.status_code == 401
    assert afterwards.json()["detail"]["code"] == "DEVICE_REVOKED"


def test_admin_revoke_credential_marks_revoked(client: TestClient) -> None:
    """The stronger release: ``REVOKED`` keeps the 028 shape (revoked_at set,
    unbound_at NULL), the session dies and the audit names the lane."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-rv1", suffix="rv1")
    device_id = customer["device_id"]

    revoked = _admin_write(
        client,
        _admin_session(client),
        f"{ADMIN_DEVICES_PATH}/{device_id}/revoke-credential",
        key="adm-rv1",
        reason="设备疑似泄露，撤销凭据",
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "REVOKED"

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, unbound_at, revoked_at FROM customer_devices WHERE id = %s",
            (device_id,),
        ).fetchone()
        session = conn.execute(
            "SELECT session_epoch FROM customer_session_state WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
    assert row is not None and row[0] == "REVOKED"
    assert row[1] is None and row[2] is not None
    assert session is not None and session[0] == 2

    event = _admin_event_row("DEVICE_CREDENTIAL_REVOKED")
    assert event is not None
    assert str(event[1]) == "admin_u"
    assert str(event[3]) == device_id
    assert str(event[6]) == "设备疑似泄露，撤销凭据"

    afterwards = client.get(DEVICES_PATH, headers=_bearer(customer["device_token"]))
    assert afterwards.status_code == 401
    assert afterwards.json()["detail"]["code"] == "DEVICE_REVOKED"


def test_admin_unbind_outcomes(client: TestClient) -> None:
    """404 unknown device, 409 when the device is not currently bound."""
    headers = _admin_session(client)
    missing = _admin_write(client, headers, f"{ADMIN_DEVICES_PATH}/{uuid.uuid4()}/unbind")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "DEVICE_NOT_FOUND"

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-un2", suffix="un2")
    device_id = customer["device_id"]
    first = _admin_write(client, headers, f"{ADMIN_DEVICES_PATH}/{device_id}/unbind")
    assert first.status_code == 200, first.text
    second = _admin_write(client, headers, f"{ADMIN_DEVICES_PATH}/{device_id}/unbind")
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "DEVICE_ALREADY_RELEASED"


def test_admin_write_contract_enforced(client: TestClient) -> None:
    """§15 / §9.3: key, confirmation and reason are mandatory; the auditor
    is read-only; an unauthenticated caller never reaches the business."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-wc", suffix="wc")
    path = f"{ADMIN_DEVICES_PATH}/{customer['device_id']}/unbind"
    headers = _admin_session(client)

    no_key = client.post(path, json={"confirm": True, "reason": "r"}, headers=headers)
    assert no_key.status_code == 400
    assert no_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    unconfirmed = _admin_write(client, headers, path, confirm=False)
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    no_reason = _admin_write(client, headers, path, reason="   ")
    assert no_reason.status_code == 400
    assert no_reason.json()["detail"]["code"] == "REASON_REQUIRED"

    client.cookies.clear()
    auditor = _admin_write(client, _admin_session(client, "auditor_u"), path)
    assert auditor.status_code == 403
    assert auditor.json()["detail"]["code"] == "AUDITOR_READ_ONLY"

    client.cookies.clear()
    unauthenticated = _admin_write(client, {}, path)
    assert unauthenticated.status_code == 401

    # None of the refusals touched the device or wrote an audit row.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
    assert status is not None and status[0] == "BOUND"
    assert _admin_event_row("DEVICE_ADMIN_UNBOUND") is None


def test_admin_unbind_idempotent_replay_and_conflict(client: TestClient) -> None:
    """Same key + same body replays the stored response; the same key against
    a different body is a conflict (the revision 031 snapshot semantics)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-idm", suffix="idm")
    path = f"{ADMIN_DEVICES_PATH}/{customer['device_id']}/unbind"
    headers = _admin_session(client)
    key = "adm-idem-idm"

    first = _admin_write(client, headers, path, key=key)
    assert first.status_code == 200, first.text
    assert REPLAY_HEADER not in first.headers

    # The device is already released; without the snapshot layer this retry
    # would answer 409 — the replay must answer the stored 200.
    retry = _admin_write(client, headers, path, key=key)
    assert retry.status_code == 200, retry.text
    assert retry.headers.get(REPLAY_HEADER) == "true"
    assert retry.json() == first.json()

    conflict = _admin_write(client, headers, path, key=key, reason="另一个原因")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_pairing_verification_view_shows_lane_state(client: TestClient) -> None:
    """The operator's verification view (§12.2 step 3): issuance record,
    activation fact, first-device status and the admin lane summary."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-vw", suffix="vw")
    enroll = _enroll(client, code=FIRST_CODE, fingerprint="fp-adm-vw-2nd", key="idem-vw")
    pairing_id = enroll.json()["pairing_request_id"]

    view = client.get(f"{ADMIN_PAIRINGS_PATH}/{pairing_id}", headers=_admin_session(client))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["pairing"]["status"] == "PENDING"
    assert body["pairing"]["activation_code_id"] == _code_id_of_user(customer["user_id"])
    assert body["code"]["status"] == "ACTIVE"
    assert body["code"]["masked_code"]
    assert body["activation"]["user_id"] == customer["user_id"]
    assert body["activation"]["first_device_id"] == customer["device_id"]
    assert body["first_device"]["status"] == "BOUND"
    assert body["admin_lane"] == "CLOSED_FIRST_DEVICE_BOUND"

    release = client.delete(
        f"{DEVICES_PATH}/{customer['device_id']}",
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-vw-rel"},
    )
    assert release.status_code == 204, release.text
    reopened = client.get(f"{ADMIN_PAIRINGS_PATH}/{pairing_id}", headers=_admin_session(client))
    assert reopened.status_code == 200
    assert reopened.json()["first_device"]["status"] == "UNBOUND"
    assert reopened.json()["admin_lane"] == "OPEN"

    missing = client.get(f"{ADMIN_PAIRINGS_PATH}/{uuid.uuid4()}", headers=_admin_session(client))
    assert missing.status_code == 404


def test_admin_device_events_append_only_and_no_truncate(client: TestClient) -> None:
    """Revision 038: UPDATE, DELETE and TRUNCATE on the audit table are all
    refused by PostgreSQL itself (the 029 / 036 trigger precedents)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-adm-ao", suffix="ao")
    unbound = _admin_write(
        client,
        _admin_session(client),
        f"{ADMIN_DEVICES_PATH}/{customer['device_id']}/unbind",
    )
    assert unbound.status_code == 200, unbound.text

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE admin_device_events SET reason = 'tampered'")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM admin_device_events")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("TRUNCATE admin_device_events")
        survivors = conn.execute("SELECT count(*) FROM admin_device_events").fetchone()
    assert survivors is not None and int(survivors[0]) == 1


def test_admin_device_events_downgrade_guard(route_state: str) -> None:
    """Revision 038's downgrade refuses once audit rows exist (the 037
    guard precedent): the operator lineage must survive any rollback."""
    from alembic import command
    from alembic.config import Config

    _seed_issuable_code(FIRST_CODE, code_id="code-038-guard", batch_id="batch-038-guard")
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        # A plain SQL row — the guard tests the migration constraints, not the
        # application-layer key configuration (route_state sets no device
        # fingerprint key env; the digests below are inert literals).
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES ('device-038-guard', 'code-038-guard', 'admin_u', 1, 'Guard', 'windows', "
            "'fp-038-guard', 1, 'token-038-guard', 1)"
        )
        conn.execute(
            "INSERT INTO admin_device_events "
            "(id, event, admin_user_id, target_user_id, device_id, "
            " activation_code_id, reason, request_id) "
            "VALUES ('event-038-guard', 'DEVICE_ADMIN_UNBOUND', 'admin_u', 'admin_u', "
            "'device-038-guard', 'code-038-guard', 'guard', 'req-038-guard')"
        )

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t16_dsn().replace("postgresql://", "postgresql+psycopg://")
    )

    with pytest.raises(RuntimeError, match="cannot downgrade 038_admin_device_operations"):
        command.downgrade(config, "037_device_pairing_requests")
    with psycopg.connect(_t16_dsn()) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == _HEAD_REVISION


# ---------------------------------------------------------------------------
# Migration 033 downgrade guard (runs last: it cycles the fixture schema)
# ---------------------------------------------------------------------------


def test_forged_fingerprint_is_caught_by_pairing_risk_control(
    client: TestClient,
) -> None:
    """CW-009/S4: 伪造（陌生）指纹不得直接产出 BOUND 设备。

    A forged fingerprint enrolling the code must land in its own PENDING
    pairing request (binding the candidate digest) and never touch the
    existing binding; risk control sees the request instead of a device.
    """
    customer = _activated_customer(
        client, code=FIRST_CODE, fingerprint="fp-cw09-owner", suffix="cw09fp"
    )
    forged = _enroll(
        client,
        code=FIRST_CODE,
        fingerprint="fp-cw09-forged",
        key="idem-cw09-forged",
        name="Forged Device",
    )
    assert forged.status_code == 202, forged.text
    pairing_id = forged.json()["pairing_request_id"]

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = _pairing_row(conn, pairing_id)
    assert row is not None
    status, candidate_digest = row[0], row[1]
    assert status == "PENDING"
    assert isinstance(candidate_digest, str) and len(candidate_digest) > 0
    # The digest must not be the raw fingerprint.
    assert candidate_digest != "fp-cw09-forged"
    # No new BOUND device: only the owner's original binding exists.
    assert _count_rows("SELECT COUNT(*) FROM customer_devices") == 1
    assert customer["device_token"]  # owner session untouched


def test_pairing_downgrade_refuses_once_rows_exist(route_state: str) -> None:
    """P2 fix: the 033 downgrade guards the pairing approval lineage.

    ``device_pairing_requests`` rows are the audit evidence of who approved
    which second device — once any row exists, the downgrade must refuse
    loudly (the 027/028/032 guard precedent, the 032 test pattern). The test
    runs last in this module and restores the fixture to head afterwards.
    """
    from alembic import command
    from alembic.config import Config

    _seed_issuable_code(FIRST_CODE, code_id="code-guard", batch_id="batch-guard")
    _insert_pairing_row(
        pairing_id="pairing-guard",
        activation_code_id="code-guard",
        fingerprint_digest="digest-guard",
        status="PENDING",
    )

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t16_dsn().replace("postgresql://", "postgresql+psycopg://")
    )

    with pytest.raises(RuntimeError, match="cannot downgrade 037_device_pairing_requests"):
        command.downgrade(config, "032_security_rate_limits")
    # The refusal left the schema untouched at head. The whole downgrade
    # chain runs in one transaction (env.py: no transaction_per_migration),
    # so 039/038's already-executed downgrades roll back with 037's refusal —
    # the version stays at the current head.
    with psycopg.connect(_t16_dsn()) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == _HEAD_REVISION

    # An emptied table downgrades symmetrically, and upgrading back restores
    # the schema for any rerun of this module. Revision 038 added the
    # admin_device_events → device_pairing_requests FK, so both tables must
    # empty together (and behind the replica role — 036/038 refuse bare
    # TRUNCATEs of the append-only audit tables).
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute("TRUNCATE admin_device_events, device_pairing_requests")
        conn.execute("SET session_replication_role = DEFAULT")
    command.downgrade(config, "032_security_rate_limits")
    command.upgrade(config, "head")
