"""T20 / SES-03 — the customer session fencing verifier (dev doc §12.4).

Fail-first tests for the frozen file ``server/app/customer_auth.py`` (code
checklist §9.2): the transaction-scoped verifier every customer *write* route
must call inside its business transaction once T21 wires them up.

Contract under test (task list §12.3 SES-03; dev doc §12.3 / §12.4):

- ``verify_session_context`` resolves the presented session token to the
  single live ``customer_session_state`` row under a row lock and answers the
  minimal ``CustomerSessionContext`` (user, activation code, device, session
  id, epoch, lease) — nothing more ever leaves the verifier (§9.2);

- a token that no longer owns the row (unknown, or replaced by a switch /
  takeover) answers ``SESSION_REPLACED`` — the §12.3 rule that a stale
  device's write must never observe its epoch coming back;

- a matching token under a lapsed lease answers ``SESSION_EXPIRED`` and the
  session is never resurrected (§3.4);

- the verifier re-checks the *authority* chain inside the same transaction:
  a non-ACTIVE activation code or a released device answers SESSION_REPLACED
  even if the lease still looks alive (the revocation-propagation defence in
  depth — the suspend/revoke transaction normally pulls the lease first);

- the expected-binding parameters mirror §12.4's "compare user_id +
  device_id + session_id + session_epoch + lease inside the business
  transaction": identity changes and shortened leases answer SESSION_REPLACED;
  normal renewal of the same live session can accompany a slow request.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T20_FENCING_DB_NAME = "t20_customer_fencing_test"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY_V1 = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V2 = secrets.token_urlsafe(48)
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)

ACTIVATE_PATH = "/api/customer/activate"
LOGIN_PATH = "/api/customer/sessions/login"
SWITCH_PATH = "/api/customer/sessions/switch"
HEARTBEAT_PATH = "/api/customer/sessions/heartbeat"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
AUTHORIZATION_HEADER = "Authorization"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

FIRST_CODE = "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD"
SECOND_CODE = "XS04-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _fencing_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T20_FENCING_DB_NAME}"


@pytest.fixture(scope="module")
def fencing_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T20_FENCING_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T20_FENCING_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _fencing_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _fencing_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T20_FENCING_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(fencing_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, device_pairing_requests, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            "projects, audit_logs, customer_fencing_write_evidence, "
            "security_rate_limit_counters, security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
    yield fencing_dsn
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router
    from app.analysis_routes import router as analysis_router
    from app.character_reference_routes import router as character_reference_router
    from app.character_routes import router as character_router
    from app.customer_session_routes import router as customer_session_router
    from app.first_frame_routes import router as first_frame_router
    from app.generation_routes import router as generation_router
    from app.media_routes import router as media_router
    from app.rbac_routes import router as rbac_router
    from app.recharge_routes import router as recharge_router
    from app.simple_character_routes import router as simple_character_router
    from app.source_frame_routes import router as source_frame_router

    app = FastAPI()
    for r in (
        activation_code_router,
        customer_session_router,
        rbac_router,
        media_router,
        recharge_router,
        analysis_router,
        first_frame_router,
        source_frame_router,
        simple_character_router,
        character_router,
        character_reference_router,
        generation_router,
    ):
        app.include_router(r)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")

    # The migrated write routes' storage/provider/extractor dependencies read
    # get_database (a SQLite path) which is absent on the customer lane; the
    # gate tests only exercise the session snapshot 401 (before the route body),
    # so inert fakes are enough for dependency resolution.
    from app.character_identity_routes import get_character_storage
    from app.first_frame_routes import get_image_provider
    from app.media_routes import get_media_storage, get_video_probe
    from app.source_frame_routes import get_source_frame_extractor

    for dep in (
        get_media_storage,
        get_image_provider,
        get_character_storage,
        get_source_frame_extractor,
        get_video_probe,
    ):
        app.dependency_overrides[dep] = lambda: object()

    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Seed helpers (the T19 test precedents)
# ---------------------------------------------------------------------------


def _seed_issuable_code(code: str, *, code_id: str, batch_id: str) -> None:
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
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


def _activated_customer(client: TestClient, *, code: str, fingerprint: str, suffix: str) -> dict:
    _seed_issuable_code(code, code_id=f"code-{suffix}", batch_id=f"batch-{suffix}")
    response = client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": fingerprint,
            "device_name": f"Device {suffix}",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: f"idem-{suffix}"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    for field in ("user_id", "device_id", "device_token", "session_token"):
        assert isinstance(payload.get(field), str) and payload[field], payload
    return payload


def _second_device_row(
    *,
    user_id: str,
    activation_code_id: str,
    device_id: str,
    slot_no: int,
    display_name: str = "Second Device",
) -> str:
    from app.customer_device_service import highest_device_domain_key, keyed_digest

    token = secrets.token_urlsafe(32)
    version, key = highest_device_domain_key()
    digest = keyed_digest(key, token)
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES (%s, %s, %s, %s, %s, 'macos', %s, %s, %s, %s)",
            (
                device_id,
                activation_code_id,
                user_id,
                slot_no,
                display_name,
                keyed_digest(key, f"fp-second-{device_id}"),
                version,
                digest,
                version,
            ),
        )
    return token


def _bearer(token: str) -> dict[str, str]:
    return {AUTHORIZATION_HEADER: f"Bearer {token}"}


def _session_row() -> tuple[str, str, int, str]:
    """The live session row: (device_id, session_id, epoch, lease_until)."""
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT device_id, session_id, session_epoch, lease_until FROM customer_session_state"
        ).fetchone()
    assert row is not None, "expected exactly one session row"
    return str(row[0]), str(row[1]), int(row[2]), str(row[3])


def _expire_lease() -> None:
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = GREATEST("
            "(now() - interval '10 seconds'), "
            "created_at::timestamptz + interval '1 microsecond')::text"
        )


def _verify(token: str, **overrides: object) -> object:
    """Run the fencing verifier in one transaction (the T21 call shape)."""
    from app.customer_auth import verify_session_context

    with psycopg.connect(_fencing_dsn(), autocommit=False) as conn:
        try:
            context = verify_session_context(
                conn,
                presentation_session_token=token,
                **overrides,  # type: ignore[arg-type]
            )
        finally:
            conn.rollback()
    return context


def _verify_raises(token: str, **overrides: object) -> str:
    from app.customer_auth import SessionFencingError, verify_session_context

    with psycopg.connect(_fencing_dsn(), autocommit=False) as conn:
        try:
            verify_session_context(
                conn,
                presentation_session_token=token,
                **overrides,  # type: ignore[arg-type]
            )
        except SessionFencingError as error:
            return error.code
        finally:
            conn.rollback()
    raise AssertionError("expected SessionFencingError")


# ---------------------------------------------------------------------------
# The happy path — the minimal context
# ---------------------------------------------------------------------------


def test_verify_answers_the_minimal_session_context(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    device_id, session_id, epoch, _ = _session_row()

    context = _verify(customer["session_token"])
    assert context.user_id == customer["user_id"]
    assert context.activation_code_id == "code-a"
    assert context.device_id == device_id
    assert context.session_id == session_id
    assert context.session_epoch == epoch == 1
    assert context.lease_until
    # The frozen dataclass exposes exactly the six §9.2 fields — nothing else.
    assert set(type(context).__dataclass_fields__) == {
        "user_id",
        "activation_code_id",
        "device_id",
        "session_id",
        "session_epoch",
        "lease_until",
    }


# ---------------------------------------------------------------------------
# SESSION_REPLACED — a token that no longer owns the row
# ---------------------------------------------------------------------------


def test_verify_rejects_unknown_token(client: TestClient) -> None:
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    assert _verify_raises(secrets.token_urlsafe(32)) == "SESSION_REPLACED"


def test_verify_rejects_the_replaced_token_after_a_switch(client: TestClient) -> None:
    """§12.3/§12.4 core: after the switch commits, the old token is fenced out
    on every instance — the verifier answers SESSION_REPLACED even though the
    row still exists (a new epoch owns it)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = customer["device_token"]
    switched = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-fencing-switch",
        },
    )
    assert switched.status_code == 201, switched.text

    assert _verify_raises(customer["session_token"]) == "SESSION_REPLACED"
    # The new token verifies fine and carries the bumped epoch.
    context = _verify(switched.json()["session_token"])
    assert context.session_epoch == 2
    assert context.device_id == customer["device_id"]


# ---------------------------------------------------------------------------
# SESSION_EXPIRED — a matching token under a lapsed lease
# ---------------------------------------------------------------------------


def test_verify_rejects_a_lapsed_lease(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    _expire_lease()
    assert _verify_raises(customer["session_token"]) == "SESSION_EXPIRED"


def test_verify_rejects_a_logged_out_session(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    out = client.post(
        "/api/customer/sessions/logout",
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-fencing-logout",
        },
    )
    assert out.status_code == 204, out.text
    assert _verify_raises(customer["session_token"]) == "SESSION_EXPIRED"


# ---------------------------------------------------------------------------
# Authority re-checks — the revocation-propagation defence in depth
# ---------------------------------------------------------------------------


def test_verify_rejects_a_suspended_code_even_with_a_live_lease(client: TestClient) -> None:
    """Defence in depth: the suspend transaction normally pulls the lease
    first (SES-03), but the verifier must refuse on the code status alone if
    the lease still looks alive (e.g. an operator flips the status manually)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE activation_codes SET status = 'SUSPENDED', suspended_at = %s "
            "WHERE id = 'code-a'",
            ("2026-08-23T00:00:00+00:00",),
        )

    assert _verify_raises(customer["session_token"]) == "SESSION_REPLACED"


def test_verify_rejects_a_revoked_code_even_with_a_live_lease(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE activation_codes SET status = 'REVOKED', revoked_at = %s WHERE id = 'code-a'",
            ("2026-08-23T00:00:00+00:00",),
        )

    assert _verify_raises(customer["session_token"]) == "SESSION_REPLACED"


def test_verify_rejects_a_released_device_even_with_a_live_lease(client: TestClient) -> None:
    """T16/T18 revoke the session when they release the device; the verifier
    still re-checks the device status itself (defence in depth)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_devices SET status = 'REVOKED', revoked_at = %s WHERE id = %s",
            ("2026-08-23T00:00:00+00:00", customer["device_id"]),
        )

    assert _verify_raises(customer["session_token"]) == "SESSION_REPLACED"


# ---------------------------------------------------------------------------
# Expected-binding parameters — the §12.4 in-transaction re-comparison
# ---------------------------------------------------------------------------


def test_verify_enforces_the_expected_session_id(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    _, session_id, _, _ = _session_row()
    assert (
        _verify_raises(customer["session_token"], expected_session_id=f"{session_id}-stale")
        == "SESSION_REPLACED"
    )
    # The true value still passes.
    context = _verify(customer["session_token"], expected_session_id=session_id)
    assert context.session_id == session_id


def test_verify_enforces_the_expected_epoch(client: TestClient) -> None:
    """A request that observed epoch 1 and re-checks after a switch sees the
    epoch has moved — SESSION_REPLACED, no business write may follow."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = customer["device_token"]
    switched = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-fencing-epoch",
        },
    )
    assert switched.status_code == 201, switched.text

    # The stale epoch-1 expectation is fenced out; the fresh epoch passes.
    assert _verify_raises(customer["session_token"], expected_session_epoch=1) == (
        "SESSION_REPLACED"
    )
    context = _verify(switched.json()["session_token"], expected_session_epoch=2)
    assert context.session_epoch == 2


def test_verify_enforces_the_expected_device_id(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    assert (
        _verify_raises(customer["session_token"], expected_device_id="device-somewhere-else")
        == "SESSION_REPLACED"
    )
    context = _verify(customer["session_token"], expected_device_id=customer["device_id"])
    assert context.device_id == customer["device_id"]


def test_verify_enforces_the_expected_user_id(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    assert (
        _verify_raises(customer["session_token"], expected_user_id="someone-else")
        == "SESSION_REPLACED"
    )
    context = _verify(customer["session_token"], expected_user_id=customer["user_id"])
    assert context.user_id == customer["user_id"]


# ---------------------------------------------------------------------------
# No-Go red lines
# ---------------------------------------------------------------------------


def test_verify_never_leaks_the_token_or_digest(client: TestClient) -> None:
    """The context carries no credential material (§9.2 minimal context)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    context = _verify(customer["session_token"])
    fields = type(context).__dataclass_fields__.values()
    blob = " ".join(str(getattr(context, field.name)) for field in fields)
    assert customer["session_token"] not in blob
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        digest = conn.execute("SELECT token_digest FROM customer_session_state").fetchone()
    assert str(digest[0]) not in blob


# ---------------------------------------------------------------------------
# PR #52 review regression locks (chatgpt-codex-connector)
# ---------------------------------------------------------------------------


def test_verify_accepts_the_matching_lease_snapshot(client: TestClient) -> None:
    """PR #52 P2 happy path: a caller that supplies the exact expected lease
    snapshot still verifies."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    context = _verify(customer["session_token"])
    assert context.lease_until
    re_checked = _verify(customer["session_token"], expected_lease_until=context.lease_until)
    assert re_checked.session_epoch == 1


def test_verify_fences_a_lease_snapshot_that_was_shortened(client: TestClient) -> None:
    """PR #52 P2: §12.4 re-compares the *complete* snapshot including the
    lease — a caller whose expected lease no longer matches the row (a lease
    pulled back by logout/revocation between the preliminary auth step and
    the business transaction) is fenced SESSION_REPLACED even though the
    epoch is unchanged."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    context = _verify(customer["session_token"])
    original_lease = context.lease_until
    # A shortened but still live lease must be fenced even before expiry.
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state SET lease_until = (now() + interval '30 seconds')"
        )
    assert (
        _verify_raises(customer["session_token"], expected_lease_until=original_lease)
        == "SESSION_REPLACED"
    )


def test_verify_judges_the_lease_on_the_post_lock_clock(client: TestClient) -> None:
    """PR #52 P2: the lease is judged on clock_timestamp() — the actual
    wall-clock time after the row lock — not the transaction-start now(). A
    write that began while the lease was still valid but waited on the
    FOR UPDATE until after it expired is fenced SESSION_EXPIRED (a stale
    now() would authorise it)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    token = customer["session_token"]
    # Shorten the lease to ~2 s so the ~3.5 s lock wait crosses it.
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state SET lease_until = (now() + interval '2 seconds')::text"
        )
    lock_taken = threading.Event()
    holder_done = threading.Event()

    def holder() -> None:
        # Take the session row lock first and hold it past the lease expiry.
        with psycopg.connect(_fencing_dsn(), autocommit=False) as conn:
            conn.execute("SELECT user_id FROM customer_session_state FOR UPDATE")
            lock_taken.set()
            time.sleep(3.5)
            conn.commit()
            holder_done.set()

    def verifier() -> None:
        assert lock_taken.wait(timeout=5)
        code = _verify_raises(token)
        assert code == "SESSION_EXPIRED"

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=verifier)
    t1.start()
    t2.start()
    t1.join(timeout=8)
    t2.join(timeout=8)
    assert holder_done.is_set()


def test_code_status_gate_locks_the_code_through_establishment(
    client: TestClient,
) -> None:
    """PR #52 P1: the code-status gate locks the activation code row FOR
    UPDATE through establishment, so an administrator suspend/revoke (which
    locks the code row first, then the riding session) serializes against a
    concurrent session establishment — a suspended code can never end up
    with a live session. Without the lock, the suspend's UPDATE completes
    immediately instead of waiting for the gate."""
    from app.customer_session_routes import _require_active_code

    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    lock_held = threading.Event()
    suspend_complete = threading.Event()
    suspend_elapsed: list[float] = []
    thread_errors: list[BaseException] = []

    def gate_holder() -> None:
        # The login/switch route holds this lock inside the business
        # transaction, from the gate through login_session to commit.
        try:
            with psycopg.connect(_fencing_dsn(), autocommit=False) as conn:
                _require_active_code(conn, "code-a")  # FOR UPDATE held here
                lock_held.set()
                time.sleep(0.4)
                conn.commit()
        except BaseException as exc:  # pragma: no cover - surfaces the thread error
            thread_errors.append(exc)

    def suspender() -> None:
        try:
            assert lock_held.wait(timeout=5)
            start = time.monotonic()
            with psycopg.connect(_fencing_dsn(), autocommit=False) as conn:
                # Any write to the code row must block on the gate's FOR UPDATE;
                # a no-op assignment avoids the status-shape CHECK so the test
                # isolates the lock semantics.
                conn.execute(
                    "UPDATE activation_codes SET masked_code = masked_code WHERE id = 'code-a'"
                )
                conn.commit()
            suspend_elapsed.append(time.monotonic() - start)
            suspend_complete.set()
        except BaseException as exc:  # pragma: no cover - surfaces the thread error
            thread_errors.append(exc)

    t1 = threading.Thread(target=gate_holder)
    t2 = threading.Thread(target=suspender)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not thread_errors, thread_errors
    assert suspend_complete.is_set()
    # The suspend's UPDATE must have waited on the gate's FOR UPDATE lock
    # (≈ the 0.4 s hold); without the lock it would complete immediately.
    assert suspend_elapsed[0] >= 0.3
    # The gate's read saw the code still ACTIVE (nothing changed it).
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        row = conn.execute("SELECT status FROM activation_codes WHERE id = 'code-a'").fetchone()
    assert str(row[0]) == "ACTIVE"


# ---------------------------------------------------------------------------
# T21 wiring — the customer business write route (SES-04, plan C)
# ---------------------------------------------------------------------------

PROJECTS_PATH = "/api/projects"


def _business_login(client: TestClient, customer: dict, key: str) -> str:
    resp = client.post(
        LOGIN_PATH,
        json={},
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: key},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["session_token"])


def test_successful_project_owner_authorization_persists_digest_evidence(
    client: TestClient,
) -> None:
    """A successful protected owner operation commits only a bounded digest pair."""
    from app.auth import CurrentUser, get_database
    from app.permissions import require_project_access

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    token = _business_login(client, customer, "idem-login-a")
    resp = client.post(PROJECTS_PATH, json={"name": "Customer Project"}, headers=_bearer(token))
    assert resp.status_code == 201, resp.text
    assert resp.json()["owner_user_id"] == customer["user_id"]
    project_id = str(resp.json()["id"])

    app = FastAPI()

    @app.post("/authorized-write")
    def authorized_write(
        conn: BusinessConnection = Depends(get_database),
    ) -> None:
        require_project_access(
            conn,
            actor=CurrentUser(
                id=customer["user_id"],
                username=customer["user_id"],
                display_name="Customer",
                role="customer",
            ),
            project_id=project_id,
            action="project.rename",
        )
        conn.execute(
            "UPDATE projects SET name = %s WHERE id = %s",
            ("Customer Project Renamed", project_id),
        )

    with TestClient(app) as authorized_client:
        authorized = authorized_client.post("/authorized-write")
    assert authorized.status_code == 200, authorized.text
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT owner_user_id, name FROM projects WHERE id = %s",
            (project_id,),
        ).fetchone()
        evidence = conn.execute(
            "SELECT resource_type, actor_digest, owner_digest "
            "FROM customer_authorization_evidence WHERE resource_type = 'project'"
        ).fetchone()
    assert str(row[0]) == customer["user_id"]
    assert str(row[1]) == "Customer Project Renamed"
    assert evidence is not None
    assert str(evidence[0]) == "project"
    assert str(evidence[1]) == str(evidence[2])
    assert customer["user_id"] not in repr(evidence)


def test_other_customer_cannot_touch_a_foreign_project(client: TestClient) -> None:
    """Two-code IDOR: B cannot distinguish A's project from a missing project."""
    alice = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    bob = _activated_customer(client, code=SECOND_CODE, fingerprint="fp-b", suffix="b")
    alice_token = _business_login(client, alice, "idem-login-alice")
    created = client.post(
        PROJECTS_PATH, json={"name": "Alice Project"}, headers=_bearer(alice_token)
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    bob_token = _business_login(client, bob, "idem-login-bob")
    missing = client.patch(
        f"{PROJECTS_PATH}/00000000-0000-4000-8000-000000000000/name",
        json={"name": "Hijack"},
        headers=_bearer(bob_token),
    )
    renamed = client.patch(
        f"{PROJECTS_PATH}/{project_id}/name",
        json={"name": "Hijack"},
        headers=_bearer(bob_token),
    )
    assert missing.status_code == 404, missing.text
    assert renamed.status_code == missing.status_code, renamed.text
    assert renamed.content == missing.content
    assert renamed.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        row = conn.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()
        denial = conn.execute(
            "SELECT actor_user_id, entity_id, metadata_json FROM audit_logs "
            "WHERE action = 'security.project_denied'"
        ).fetchone()
    assert str(row[0]) == "Alice Project"
    assert denial is not None
    assert (str(denial[0]), str(denial[1])) == (bob["user_id"], project_id)
    assert json.loads(str(denial[2])) == {"attempted_action": "project.rename"}


def test_cluster_probe_detects_heartbeat_from_a_displaced_session_epoch(
    client: TestClient,
) -> None:
    """A heartbeat that commits after a newer session epoch is a P1 even
    though its old epoch has only one device event."""
    from scripts import check_ops_alerts as alerts

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    old_device_id, old_session_id, old_epoch, _ = _session_row()
    second_token = customer["device_token"]
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_session_events "
            "(id, event, user_id, activation_code_id, device_id, session_id, "
            "session_epoch, actor_user_id, request_id) "
            "VALUES ('heartbeat-before-switch-t37', 'HEARTBEAT', %s, 'code-a', %s, %s, %s, %s, "
            "'heartbeat-before-switch-t37')",
            (customer["user_id"], old_device_id, old_session_id, old_epoch, customer["user_id"]),
        )

    switched = client.post(
        SWITCH_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-displaced-heartbeat-switch",
        },
    )
    assert switched.status_code == 201, switched.text
    assert switched.json()["session_epoch"] == old_epoch + 1

    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        before_regression = alerts.collect_observations(conn)
        assert (
            next(item for item in before_regression if item.name == "double_online").observed_count
            == 0
        )
        # ``CURRENT_TIMESTAMP`` is rendered as TEXT in the connection's local
        # timezone. The probe must compare it as an instant, not lexically
        # against a UTC cutoff.
        conn.execute("SET TIME ZONE 'America/Los_Angeles'")
        conn.execute(
            "INSERT INTO customer_session_events "
            "(id, event, user_id, activation_code_id, device_id, session_id, "
            "session_epoch, actor_user_id, request_id) "
            "VALUES ('heartbeat-after-switch-t37', 'HEARTBEAT', %s, 'code-a', %s, %s, %s, %s, "
            "'heartbeat-after-switch-t37')",
            (customer["user_id"], old_device_id, old_session_id, old_epoch, customer["user_id"]),
        )
        observations = alerts.collect_observations(conn)

    assert next(item for item in observations if item.name == "double_online").observed_count == 1


def test_request_scoped_pg_reads_persist_denials_after_dependency_rollback(
    client: TestClient,
) -> None:
    from app.auth import CurrentUser, get_database
    from app.permissions import require_project_access

    alice = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    bob = _activated_customer(client, code=SECOND_CODE, fingerprint="fp-b", suffix="b")
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            ("project-read-denial", alice["user_id"], "Alice Read Boundary"),
        )

    app = FastAPI()

    @app.get("/denied-read")
    def denied_read(
        conn: BusinessConnection = Depends(get_database),
    ) -> None:
        require_project_access(
            conn,
            actor=CurrentUser(
                id=bob["user_id"],
                username=bob["user_id"],
                display_name="Bob",
                role="customer",
            ),
            project_id="project-read-denial",
            action="project.read",
        )

    with TestClient(app) as read_client:
        response = read_client.get("/denied-read")

    # T45 C-2 intentionally makes a foreign project indistinguishable from a
    # missing one while retaining the durable denial audit server-side.
    assert response.status_code == 404
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        denial_count = int(
            conn.execute(
                "SELECT count(*) FROM audit_logs "
                "WHERE action = 'security.project_denied' "
                "AND entity_id = 'project-read-denial'"
            ).fetchone()[0]
        )
    assert denial_count == 1


def test_unknown_session_token_cannot_create_a_project(client: TestClient) -> None:
    """The early snapshot gate 401s a session token that owns no live row, and
    no project lands. (The snapshot-vs-transaction epoch race itself is locked
    at the fenced_pg_transaction helper level.)"""
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    resp = client.post(
        PROJECTS_PATH,
        json={"name": "Stale"},
        headers=_bearer(secrets.token_urlsafe(32)),
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["code"] == "SESSION_REPLACED"
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        row = conn.execute("SELECT count(*) FROM projects WHERE name = 'Stale'").fetchone()
    assert int(row[0]) == 0


# ---------------------------------------------------------------------------
# SES-04 gate — every migrated write route answers 401 for a session token
# that owns no live row (proves the route is wired through the fenced path)
# ---------------------------------------------------------------------------

_GARBAGE_TOKEN = "no-such-session-token"

_GATED_WRITE_ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("post", "/api/projects", {"name": "x"}),
    ("patch", "/api/projects/p-nonexistent/name", {"name": "x"}),
    (
        "post",
        "/api/assets/upload-intent",
        {
            "project_id": "p-nonexistent",
            "filename": "a.mp4",
            "content_type": "video/mp4",
            "size_bytes": 1,
        },
    ),
    ("post", "/api/assets/a-nonexistent/complete", None),
    ("post", "/api/recharge-orders", {"amount_fen": 100}),
    ("post", "/api/projects/p-nonexistent/analysis", {"asset_id": "a-nonexistent"}),
    ("put", "/api/analysis/v-nonexistent/shots", {"shots": []}),
    ("post", "/api/projects/p-nonexistent/first-frames/generate", {"model": "x", "quantity": 1}),
    (
        "post",
        "/api/projects/p-nonexistent/first-frames/confirm",
        {"first_frame_asset_id": "a-nonexistent"},
    ),
    ("post", "/api/projects/p-nonexistent/source-frames/extract", {"asset_id": "a-nonexistent"}),
    (
        "post",
        "/api/projects/p-nonexistent/source-frames/confirm",
        {"source_frame_asset_id": "a-nonexistent"},
    ),
    ("patch", "/api/simple-characters/identities/i-nonexistent/name", {"display_name": "x"}),
    ("post", "/api/simple-characters/identities/i-nonexistent/regenerate-contact-sheet", None),
    ("delete", "/api/simple-characters/identities/i-nonexistent", None),
    (
        "post",
        "/api/projects/p-nonexistent/character-reference-selection",
        {"selected_asset_ids": []},
    ),
    ("put", "/api/projects/p-nonexistent/main-character", {"character_id": "c-nonexistent"}),
    ("post", "/api/projects/p-nonexistent/scripts", {"text": "x", "source": "custom"}),
    ("post", "/api/projects/p-nonexistent/prompts/compile", {"script_version_id": "v-nonexistent"}),
    ("post", "/api/projects/p-nonexistent/prompts/revise", {"text": "x"}),
    ("post", "/api/projects/p-nonexistent/prompts/v-nonexistent/lock", None),
    ("post", "/api/projects/p-nonexistent/generation-batches", None),
    ("patch", "/api/generation-batches/b-nonexistent/name", {"display_name": "x"}),
    ("delete", "/api/generation-batches/b-nonexistent", None),
    ("post", "/api/generation-batches/b-nonexistent/regenerate", None),
    ("post", "/api/generation-tasks/t-nonexistent/retry", None),
    ("post", "/api/generation-tasks/t-nonexistent/archive", None),
    ("post", "/api/generation-tasks/t-nonexistent/regenerate", None),
    ("post", "/api/generation-tasks/t-nonexistent/reconcile", None),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _GATED_WRITE_ROUTES,
    ids=[f"{m}-{p}" for m, p, _ in _GATED_WRITE_ROUTES],
)
def test_every_migrated_write_route_is_fenced(
    client: TestClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    """SES-04: a session token that owns no live row is refused 401 at the
    snapshot gate on every migrated write route — none of them fall through to
    the internal lane or reach their business logic."""
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    kwargs: dict[str, object] = {"headers": _bearer(_GARBAGE_TOKEN)}
    if body is not None:
        kwargs["json"] = body
    resp = client.request(method, path, **kwargs)
    assert resp.status_code == 401, (method, path, resp.text)
    code = resp.json()["detail"]["code"]
    assert code in {"SESSION_REPLACED", "SESSION_TOKEN_REQUIRED"}, (method, path, code)


# ---------------------------------------------------------------------------
# T21 wiring — fenced_pg_transaction (SES-04)
# ---------------------------------------------------------------------------


def _snapshot_for(customer: dict) -> object:
    """Build the early CustomerSessionSnapshot the route dependency would."""
    from app.customer_fence import CustomerSessionSnapshot

    device_id, session_id, epoch, lease = _session_row()
    return CustomerSessionSnapshot(
        token=customer["session_token"],
        expected_user_id=customer["user_id"],
        expected_device_id=device_id,
        expected_session_id=session_id,
        expected_session_epoch=epoch,
        expected_lease_until=lease,
    )


@pytest.mark.parametrize("replace_session", [False, True])
def test_batch_hide_preserves_active_task_and_honors_session_fence(
    client: TestClient,
    replace_session: bool,
) -> None:
    from fastapi import HTTPException

    from app.customer_fence import BusinessDb
    from app.generation_routes import delete_generation_batch_record

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES ('p-hide', %s, 'Project')",
            (customer["user_id"],),
        )
        conn.execute(
            "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
            "idempotency_key, request_hash, request_snapshot_json) "
            "VALUES ('b-hide', 'p-hide', %s, 'key', 'hash', '{}')",
            (customer["user_id"],),
        )
        conn.execute(
            "INSERT INTO generation_tasks (id, batch_id, provider, model, status) "
            "VALUES ('t-hide', 'b-hide', 'apilio', 'h3', 'RUNNING')"
        )
        conn.execute(
            "UPDATE wallets SET available_credits = 9, reserved_credits = 1 WHERE user_id = %s",
            (customer["user_id"],),
        )
        conn.execute(
            "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
            "reserved_delta, task_id, billing_round, idempotency_key) "
            "VALUES ('tx-hide', %s, 'RESERVE', -1, 1, 't-hide', 1, 'reserve:t-hide:1')",
            (customer["user_id"],),
        )
        retained_tables = (
            "generation_batches",
            "generation_tasks",
            "wallets",
            "wallet_transactions",
        )
        before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in retained_tables
        }
        if replace_session:
            conn.execute("UPDATE customer_session_state SET session_epoch = session_epoch + 1")
    db = BusinessDb(snapshot=snapshot, authorization=None, dev_user_id=None)
    if replace_session:
        with pytest.raises(HTTPException) as error:
            delete_generation_batch_record("b-hide", db)
        assert error.value.status_code == 401
        assert error.value.detail["code"] == "SESSION_REPLACED"
    else:
        for _ in range(2):
            response = client.delete(
                "/api/generation-batches/b-hide", headers=_bearer(customer["session_token"])
            )
            assert response.status_code == 204, response.text
        listed = client.get("/api/generation-batches", headers=_bearer(customer["session_token"]))
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"] == []
    with psycopg.connect(_fencing_dsn()) as conn:
        assert {
            table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in retained_tables
        } == before
        assert (
            conn.execute("SELECT status FROM generation_tasks WHERE id = 't-hide'").fetchone()[0]
            == "RUNNING"
        )
        assert (
            conn.execute("SELECT count(*) FROM generation_batches WHERE id = 'b-hide'").fetchone()[
                0
            ]
            == 1
        )
        expected = 0 if replace_session else 1
        assert (
            conn.execute("SELECT count(*) FROM customer_batch_visibility").fetchone()[0] == expected
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_logs WHERE action = 'generation_batch.hide'"
            ).fetchone()[0]
            == expected
        )


def _count_projects() -> int:
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        row = conn.execute("SELECT count(*) FROM projects").fetchone()
    return int(row[0])


def test_fenced_transaction_yields_the_context_and_commits(client: TestClient) -> None:
    """SES-04 happy path: a valid snapshot passes the in-transaction
    re-verification, the business write commits with the session context."""
    from app.customer_fence import fenced_pg_transaction

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        assert ctx.user_id == customer["user_id"]
        assert ctx.session_epoch == 1
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            ("p-happy", customer["user_id"], "Happy Project"),
        )
    assert _count_projects() == 1


def test_successful_fenced_write_commits_expected_and_verified_epoch_evidence(
    client: TestClient,
) -> None:
    from app.customer_fence import fenced_pg_transaction
    from app.ops_metrics import bind_request_context

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)
    with bind_request_context(
        request_id="req-fencing-evidence-match",
        method="POST",
        route="/api/projects",
    ):
        with fenced_pg_transaction(snapshot, record_write_evidence=True) as (conn, _ctx):
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
                ("p-evidence-match", customer["user_id"], "Evidence Match"),
            )

    with psycopg.connect(_fencing_dsn()) as conn:
        row = conn.execute(
            "SELECT request_id, length(subject_digest), expected_session_epoch, "
            "verified_session_epoch FROM customer_fencing_write_evidence "
            "WHERE request_id = 'req-fencing-evidence-match'"
        ).fetchone()
    assert row is not None
    assert (str(row[0]), int(row[1]), int(row[2]), int(row[3])) == (
        "req-fencing-evidence-match",
        64,
        1,
        1,
    )


def test_successful_fenced_read_does_not_record_committed_write_evidence(
    client: TestClient,
) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    token = _business_login(client, customer, "idem-login-evidence-read")

    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        before = int(
            conn.execute("SELECT count(*) FROM customer_fencing_write_evidence").fetchone()[0]
        )
    response = client.get("/api/customer/recharge-orders", headers=_bearer(token))
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        after = int(
            conn.execute("SELECT count(*) FROM customer_fencing_write_evidence").fetchone()[0]
        )

    assert response.status_code == 200, response.text
    assert after == before


def test_fencing_evidence_failure_rolls_back_the_business_write(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import customer_fence

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)

    def evidence_unavailable(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("evidence unavailable")

    monkeypatch.setattr(
        customer_fence,
        "_record_fencing_commit_evidence",
        evidence_unavailable,
    )
    with pytest.raises(RuntimeError, match="evidence unavailable"):
        with customer_fence.fenced_pg_transaction(
            snapshot,
            record_write_evidence=True,
        ) as (conn, _ctx):
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
                ("p-evidence-failure", customer["user_id"], "Evidence Failure"),
            )

    assert _count_projects() == 0


def test_regressed_verifier_leaves_a_durable_committed_stale_write_fact(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from app import customer_fence
    from app.ops_metrics import bind_request_context

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)
    real_verify = customer_fence.verify_session_context

    def regressed_verify(conn: psycopg.Connection, **kwargs: object) -> object:
        verified = real_verify(conn, **kwargs)
        return replace(verified, session_epoch=verified.session_epoch + 1)

    monkeypatch.setattr(customer_fence, "verify_session_context", regressed_verify)
    with bind_request_context(
        request_id="req-fencing-evidence-mismatch",
        method="POST",
        route="/api/projects",
    ):
        with customer_fence.fenced_pg_transaction(
            snapshot,
            record_write_evidence=True,
        ) as (conn, _ctx):
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
                ("p-evidence-mismatch", customer["user_id"], "Evidence Mismatch"),
            )

    with psycopg.connect(_fencing_dsn()) as conn:
        row = conn.execute(
            "SELECT expected_session_epoch, verified_session_epoch "
            "FROM customer_fencing_write_evidence "
            "WHERE request_id = 'req-fencing-evidence-mismatch'"
        ).fetchone()
        project_count = conn.execute(
            "SELECT count(*) FROM projects WHERE id = 'p-evidence-mismatch'"
        ).fetchone()[0]
    assert row is not None and (int(row[0]), int(row[1])) == (1, 2)
    assert int(project_count) == 1


def test_fenced_transaction_fences_a_stale_snapshot_and_leaves_no_write(
    client: TestClient,
) -> None:
    """SES-04 §11.1: a request that passed the early snapshot but whose
    session was switched before the business transaction is fenced 401
    SESSION_REPLACED and its write never lands."""
    from fastapi import HTTPException

    from app.customer_fence import fenced_pg_transaction

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)
    # The switch bumps the session epoch after the snapshot was taken.
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute("UPDATE customer_session_state SET session_epoch = session_epoch + 1")
    with pytest.raises(HTTPException) as ei:
        with fenced_pg_transaction(snapshot) as (conn, _ctx):
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
                ("p-stale", customer["user_id"], "Stale Project"),
            )
    assert ei.value.status_code == 401
    assert ei.value.detail["code"] == "SESSION_REPLACED"
    assert _count_projects() == 0


def test_fenced_transaction_rolls_back_a_failed_business_write(client: TestClient) -> None:
    """SES-04: a business error inside the fenced transaction rolls the whole
    transaction back — no partial write survives."""
    from app.customer_fence import fenced_pg_transaction

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)
    with pytest.raises(RuntimeError):
        with fenced_pg_transaction(snapshot) as (conn, _ctx):
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
                ("p-rollback", customer["user_id"], "Rollback Project"),
            )
            raise RuntimeError("boom")
    assert _count_projects() == 0


def test_fenced_transaction_records_local_metrics_and_audit_log(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from fastapi import HTTPException

    from app.customer_fence import fenced_pg_transaction
    from app.ops_metrics import (
        bind_request_context,
        render_metrics_document_for_tests,
        reset_metrics_for_tests,
    )

    # This assertion is about one rejected transaction, not the cumulative
    # process total left by earlier fencing tests in the same worker.
    reset_metrics_for_tests()

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute("UPDATE customer_session_state SET session_epoch = session_epoch + 1")

    with caplog.at_level(logging.INFO, logger="app.customer_fence"):
        with pytest.raises(HTTPException) as ei:
            with bind_request_context(
                request_id="req-fenced",
                method="POST",
                route="/api/projects",
            ):
                with fenced_pg_transaction(snapshot) as (conn, _ctx):
                    conn.execute(
                        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
                        ("p-never", customer["user_id"], "Never Commit"),
                    )

    assert ei.value.status_code == 401
    assert ei.value.detail["code"] == "SESSION_REPLACED"
    text = render_metrics_document_for_tests(is_ready=True)
    assert 'video_replica_customer_fencing_rejects_total{code="SESSION_REPLACED"} 1' in text
    assert "video_replica_fencing_lock_wait_seconds_count 1" in text
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["event"] == "customer_session_fenced"
    assert payload["request_id"] == "req-fenced"
    assert payload["route"] == "/api/projects"
    assert payload["result_code"] == "SESSION_REPLACED"
    assert isinstance(payload["lock_wait_ms"], int)
    assert customer["session_token"] not in caplog.text


def test_fencing_audit_uses_the_matched_route_template(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from types import SimpleNamespace

    from app.customer_fence import _log_fencing_reject_audit
    from app.ops_metrics import bind_request_context

    raw_path = "/api/projects/attacker-controlled-object/scripts"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": raw_path,
            "headers": [],
            "route": SimpleNamespace(path="/api/projects/{project_id}/scripts"),
        }
    )

    with caplog.at_level(logging.INFO, logger="app.customer_fence"):
        with bind_request_context(
            request_id="req-route-template",
            method="POST",
            route=raw_path,
            request=request,
        ):
            _log_fencing_reject_audit("SESSION_REPLACED", 0.001)

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["route"] == "/api/projects/{project_id}/scripts"
    assert "attacker-controlled-object" not in caplog.text


def test_fenced_transaction_keeps_original_401_when_metrics_and_audit_fail(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from fastapi import HTTPException

    import app.customer_fence as customer_fence

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    snapshot = _snapshot_for(customer)
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        conn.execute("UPDATE customer_session_state SET session_epoch = session_epoch + 1")

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("postgresql://operator:secret-password@db/private")

    monkeypatch.setattr(customer_fence.ops_metrics, "record_fencing_reject", _boom)
    monkeypatch.setattr(customer_fence, "_record_fencing_failure_audit", _boom)
    monkeypatch.setattr(customer_fence, "_log_fencing_reject_audit", _boom)

    with caplog.at_level(logging.WARNING, logger="app.customer_fence"):
        with pytest.raises(HTTPException) as ei:
            with customer_fence.fenced_pg_transaction(snapshot) as (conn, _ctx):
                conn.execute(
                    "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
                    ("p-mask", customer["user_id"], "Mask Failure"),
                )

    assert ei.value.status_code == 401
    assert ei.value.detail["code"] == "SESSION_REPLACED"
    assert "secret-password" not in caplog.text
    assert "postgresql://" not in caplog.text


def test_cluster_probe_accepts_parallel_device_heartbeats(client: TestClient) -> None:
    from scripts import check_ops_alerts as alerts

    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    other = _second_device_row(
        user_id=customer["user_id"], activation_code_id="code-a", device_id="device-b", slot_no=2
    )
    login = client.post(
        SWITCH_PATH, json={}, headers={**_bearer(other), IDEMPOTENCY_KEY_HEADER: "parallel-probe"}
    )
    assert login.status_code == 201
    for token in (customer["session_token"], login.json()["session_token"]):
        assert client.post(HEARTBEAT_PATH, headers=_bearer(token)).status_code == 200
    with psycopg.connect(_fencing_dsn(), autocommit=True) as conn:
        observations = alerts.collect_observations(conn)
    assert next(item for item in observations if item.name == "double_online").observed_count == 0
