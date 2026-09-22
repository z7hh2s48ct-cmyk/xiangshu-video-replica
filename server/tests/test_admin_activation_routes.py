"""T12 / ACT-04 — admin activation-code management API.

PG integration cases over a dedicated migrated database (skip without the
fixture): RBAC/CSRF/session gating, the write contract (reason, confirmation,
Idempotency-Key, request id), batch creation, generation + AEAD export
creation, the one-time audited download, delivery, suspension, resume,
revocation, listing, and the admin write idempotency semantics (same key +
same request replays the stored response, same key + different request is
rejected, concurrent same-key writers serialize).

No-Go red lines locked here: unauthenticated writes, auditor writes and
missing confirmation are rejected; no plaintext code ever lands in the
database, the idempotency snapshots or the logs — plaintext lives only in
the one-time download response handed to the operator (T11 principle).
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    ACTIVATION_EXPORT_AEAD_KEY_ENV,
    compute_code_digest,
)
from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

TEST_KEY = secrets.token_urlsafe(48)  # admin-session HMAC key, never a real secret
TEST_CODE_HMAC_KEY = secrets.token_urlsafe(48)  # str env value, never a real secret
TEST_EXPORT_AEAD_KEY = secrets.token_bytes(32)  # exactly 32 bytes, never a real secret

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"
BATCH_CREATION_ENV = "VIDEO_REPLICA_ALLOW_ACTIVATION_BATCH_CREATION"


def test_zero_price_initial_seconds_require_explicit_grant_confirmation() -> None:
    from app.admin_activation_routes import BatchCreateRequest, _validate_batch_payload

    payload = BatchCreateRequest(
        name="免费体验秒数",
        face_value_fen=0,
        credits=600,
        quantity=1,
        activation_expires_at="2099-01-01T00:00:00+00:00",
        reason="活动赠送，未收款",
        confirm=True,
        confirm_grant=True,
    )
    _validate_batch_payload(payload)
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="400"):
        _validate_batch_payload(payload.model_copy(update={"confirm_grant": False}))


def _b64key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


T12_DB_NAME = "t12_admin_activation_test"


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t12_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T12_DB_NAME}"


@pytest.fixture(scope="module")
def activation_pg_dsn() -> Iterator[str]:
    """Dedicated migrated database with operator seed users."""
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T12_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T12_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t12_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_t12_dsn(), autocommit=True) as conn:
        for user_id, role in (("admin_u", "admin"), ("auditor_u", "auditor")):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (user_id, user_id, user_id.replace("_", " ").title(), role),
            )
    try:
        yield _t12_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T12_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def clean_state(activation_pg_dsn: str) -> Iterator[str]:
    """Per-test isolation: truncate the admin activation tables + sessions."""
    close_pg_pool()
    with psycopg.connect(activation_pg_dsn, autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
        conn.execute("SET session_replication_role = replica")
        # device_pairing_requests (revision 037) references customer_devices
        # and activation_codes: it must ride the same TRUNCATE, or PostgreSQL
        # refuses to truncate the referenced tables. admin_device_events
        # (revision 038) references device_pairing_requests / customer_devices
        # / activation_codes / users for the same reason.
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, device_pairing_requests, "
            "admin_device_events, "
            "customer_devices, "
            "activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "security_rate_limit_counters, security_auth_failures"
        )
        conn.execute("SET session_replication_role = DEFAULT")
    yield activation_pg_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, clean_state: str) -> Iterator[FastAPI]:
    from app.admin_activation_routes import router as admin_activation_router
    from app.admin_auth_routes import router as admin_auth_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_activation_router)
    monkeypatch.setenv(DATABASE_URL_ENV, clean_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_CODE_HMAC_KEY)
    monkeypatch.setenv(ACTIVATION_EXPORT_AEAD_KEY_ENV, _b64key(TEST_EXPORT_AEAD_KEY))
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    # M2 review H2: batch creation is refused until PRICE-01 freezes (the
    # production default). These cases exercise the minting business logic,
    # so the suite opts in explicitly; the kill-switch itself has its own
    # tests below.
    monkeypatch.setenv(BATCH_CREATION_ENV, "true")
    yield app


@pytest.fixture()
def client(admin_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(admin_app) as test_client:
        yield test_client


def _exchange(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    return _exchange(client)


@pytest.fixture()
def auditor_headers(client: TestClient) -> dict[str, str]:
    return _exchange(client, "auditor_u")


def _write_headers(base: dict[str, str], *, key: str | None = None) -> dict[str, str]:
    headers = dict(base)
    headers[IDEMPOTENCY_KEY_HEADER] = key or f"key-{uuid.uuid4()}"
    return headers


def _create_batch(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "2026 渠道批次",
    quantity: int = 10,
    key: str | None = None,
    **overrides: object,
) -> object:
    payload: dict[str, object] = {
        "name": name,
        "face_value_fen": 1500,
        "credits": 100,
        "quantity": quantity,
        "activation_expires_at": "2099-01-01T00:00:00+00:00",
        "confirm": True,
        "reason": "渠道备货",
    }
    payload.update(overrides)
    return client.post(
        "/api/control/activation-code-batches",
        json=payload,
        headers=_write_headers(headers, key=key),
    )


def _generate(
    client: TestClient,
    headers: dict[str, str],
    batch_id: str,
    *,
    quantity: int,
    auto_issue: bool = False,
    key: str | None = None,
) -> object:
    return client.post(
        f"/api/control/activation-code-batches/{batch_id}/generate",
        json={
            "quantity": quantity,
            "auto_issue": auto_issue,
            "confirm": True,
            "reason": "生成批次码",
        },
        headers=_write_headers(headers, key=key),
    )


def _row(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> tuple | None:
    return conn.execute(sql, params).fetchone()


# ---------------------------------------------------------------------------
# Authentication, RBAC and the admin write contract
# ---------------------------------------------------------------------------


def test_create_batch_requires_admin_session(client: TestClient) -> None:
    response = client.post(
        "/api/control/activation-code-batches",
        json={"confirm": True, "reason": "x"},
        headers={IDEMPOTENCY_KEY_HEADER: "key-no-session"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "ADMIN_SESSION_INVALID"


def test_create_batch_rejects_auditor_writer(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    response = _create_batch(client, auditor_headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUDITOR_READ_ONLY"


def test_initial_grant_response_matches_frozen_price(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    response = _create_batch(
        client, admin_headers, face_value_fen=0, credits=600, confirm_grant=True
    )
    assert response.status_code == 201, response.text
    with psycopg.connect(clean_state) as conn:
        row = conn.execute(
            "SELECT unit_price_fen_snapshot FROM activation_code_batches WHERE id=%s",
            (response.json()["batch_id"],),
        ).fetchone()
    assert response.json()["unit_price_fen_snapshot"] == row[0]
    assert row[0] > 0


def test_batch_creation_writes_an_audit_row(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    """批次铸造必须留审计行：只写业务表等于"凭空造码"无人可追溯.

    与 archive / reveal 同口径（audit_logs 里的 admin.activation_code.* 动作），
    审计中心因此能用统一查询回答"谁在什么时候按什么理由造了这一批码"。
    批次行本身只回答"造了什么"。
    """
    response = _create_batch(client, admin_headers, quantity=7)
    assert response.status_code == 201, response.text
    batch_id = response.json()["batch_id"]

    with psycopg.connect(clean_state) as conn:
        rows = conn.execute(
            "SELECT actor_user_id, metadata_json FROM audit_logs "
            "WHERE action = 'admin.activation_code_batch.created' "
            "AND entity_id = %s",
            (batch_id,),
        ).fetchall()

    assert len(rows) == 1
    actor_user_id, metadata_json = rows[0]
    assert actor_user_id  # 真实 actor，不是机器行
    metadata = json.loads(metadata_json)
    assert metadata["reason"] == "渠道备货"
    assert metadata["quantity"] == 7
    assert metadata["face_value_fen"] == 1500
    assert metadata["credits"] == 100
    assert metadata["request_id"] == response.json()["request_id"]


def test_write_rejects_missing_csrf_header(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    headers = _write_headers(admin_headers)
    del headers[ADMIN_CSRF_HEADER]
    response = client.post(
        "/api/control/activation-code-batches",
        json={
            "confirm": True,
            "reason": "x",
            "name": "b",
            "face_value_fen": 100,
            "credits": 10,
            "quantity": 1,
            "activation_expires_at": "2099-01-01T00:00:00+00:00",
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ADMIN_CSRF_REQUIRED"


def test_write_rejects_missing_confirmation(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = _create_batch(client, admin_headers, confirm=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"


def test_write_rejects_blank_reason(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = _create_batch(client, admin_headers, reason="   ")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "REASON_REQUIRED"


def test_write_rejects_missing_idempotency_key(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/control/activation-code-batches",
        json={
            "name": "b",
            "face_value_fen": 100,
            "credits": 10,
            "quantity": 1,
            "activation_expires_at": "2099-01-01T00:00:00+00:00",
            "confirm": True,
            "reason": "x",
        },
        headers=dict(admin_headers),  # no Idempotency-Key header
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ---------------------------------------------------------------------------
# Batch creation
# ---------------------------------------------------------------------------


def test_create_batch_success(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    response = _create_batch(client, admin_headers, name="首波", quantity=25)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "首波"
    assert body["face_value_fen"] == 1500
    assert body["unit_price_fen_snapshot"] == 1500
    assert body["credits_snapshot"] == 100
    assert body["quantity"] == 25
    assert body["status"] == "OPEN"
    assert body["created_by_user_id"] == "admin_u"
    assert body["request_id"]
    assert response.headers.get(REQUEST_ID_HEADER) == body["request_id"]
    with psycopg.connect(clean_state) as conn:
        row = _row(
            conn,
            "SELECT name, status, created_by_user_id FROM activation_code_batches WHERE id = %s",
            (body["batch_id"],),
        )
    assert row == ("首波", "OPEN", "admin_u")


def test_request_id_is_shared_by_response_and_admin_audit(
    client: TestClient,
    admin_headers: dict[str, str],
    clean_state: str,
) -> None:
    request_id = "req-t37-admin-correlation"
    headers = _write_headers(admin_headers, key="key-t37-request-id")
    headers[REQUEST_ID_HEADER] = request_id

    response = client.post(
        "/api/control/activation-code-batches",
        json={
            "name": "T37 correlation",
            "face_value_fen": 1500,
            "credits": 100,
            "quantity": 1,
            "activation_expires_at": "2099-01-01T00:00:00+00:00",
            "confirm": True,
            "reason": "request id correlation",
        },
        headers=headers,
    )

    assert response.status_code == 201, response.text
    assert response.headers[REQUEST_ID_HEADER] == request_id
    assert response.json()["request_id"] == request_id
    with psycopg.connect(clean_state) as conn:
        persisted = conn.execute(
            "SELECT creation_request_id FROM activation_code_batches WHERE id = %s",
            (response.json()["batch_id"],),
        ).fetchone()[0]
    assert persisted == request_id


def test_create_batch_idempotent_replay(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    key = "key-replay-batch"
    first = _create_batch(client, admin_headers, key=key)
    assert first.status_code == 201
    second = _create_batch(client, admin_headers, key=key)
    assert second.status_code == 201
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert second.headers.get(REPLAY_HEADER) == "true"
    assert first.headers.get(REPLAY_HEADER) is None
    with psycopg.connect(clean_state) as conn:
        count = _row(conn, "SELECT count(*) FROM activation_code_batches")[0]
        snapshots = _row(
            conn,
            "SELECT count(*) FROM admin_write_idempotency "
            "WHERE route = 'POST /api/control/activation-code-batches'",
        )[0]
    assert count == 1
    assert snapshots == 1


def test_create_batch_idempotency_conflict_on_different_payload(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    key = "key-conflict-batch"
    first = _create_batch(client, admin_headers, key=key, name="A")
    assert first.status_code == 201
    second = _create_batch(client, admin_headers, key=key, name="B")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    with psycopg.connect(clean_state) as conn:
        count = _row(conn, "SELECT count(*) FROM activation_code_batches")[0]
    assert count == 1


def test_create_batch_validates_payload(client: TestClient, admin_headers: dict[str, str]) -> None:
    for overrides in (
        {"name": "   "},
        {"face_value_fen": -1},
        {"credits": -1},
        {"face_value_fen": 0, "credits": 1},
        {"face_value_fen": 1, "credits": 0},
        {"quantity": 0},
        {"activation_expires_at": "2000-01-01T00:00:00+00:00"},
        {"activation_expires_at": "not-a-timestamp"},
    ):
        response = _create_batch(client, admin_headers, **overrides)  # type: ignore[arg-type]
        assert response.status_code == 400, overrides
        assert response.json()["detail"]["code"] == "BATCH_VALIDATION_FAILED"


def test_create_license_only_batch_allows_zero_initial_credits(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = _create_batch(
        client,
        admin_headers,
        name="零额度授权码",
        quantity=2,
        face_value_fen=0,
        credits=0,
    )
    assert response.status_code == 201, response.text
    assert response.json()["face_value_fen"] == 0
    assert response.json()["unit_price_fen_snapshot"] == 0
    assert response.json()["credits_snapshot"] == 0


# ---------------------------------------------------------------------------
# Generation + AEAD export creation
# ---------------------------------------------------------------------------


def test_generate_creates_codes_and_export(
    client: TestClient,
    admin_headers: dict[str, str],
    clean_state: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    batch = _create_batch(client, admin_headers, quantity=5).json()
    with caplog.at_level(logging.DEBUG):
        response = _generate(client, admin_headers, batch["batch_id"], quantity=5)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["batch_id"] == batch["batch_id"]
    assert body["export_id"]
    assert body["expires_at"]
    assert len(body["codes"]) == 5
    with psycopg.connect(clean_state) as conn:
        rows = conn.execute(
            "SELECT id, masked_code, status FROM activation_codes WHERE batch_id = %s ORDER BY id",
            (batch["batch_id"],),
        ).fetchall()
        events = conn.execute(
            "SELECT event, actor_user_id, request_id FROM activation_code_events "
            "WHERE code_id = ANY(%s)",
            ([row[0] for row in rows],),
        ).fetchall()
        exports = _row(
            conn,
            "SELECT count(*) FROM activation_code_exports WHERE id = %s",
            (body["export_id"],),
        )[0]
    assert len(rows) == 5
    assert {row[2] for row in rows} == {"GENERATED"}
    assert {row[0] for row in events} == {"GENERATED", "EXPORTED"}
    assert all(row[1] == "admin_u" for row in events)
    # M2 review H1: both event kinds carry the request id now — GENERATED
    # writes it from the write contract and EXPORTED (sealed by the T11
    # export helper inside the same transaction) receives it via
    # create_batch_export's request_id argument.
    assert all(row[2] for row in events)
    assert exports == 1
    # The response and every log record stay plaintext-free.
    response_text = response.text
    for row in rows:
        assert row[1] in response_text
    assert "XS04-" in response_text  # masked codes are present
    plaintext_like = [
        record.getMessage()
        for record in caplog.records
        if "XS04-" in record.getMessage() and "***" not in record.getMessage()
    ]
    assert plaintext_like == []


def test_generate_can_auto_issue_codes_for_immediate_customer_activation(
    client: TestClient,
    admin_headers: dict[str, str],
    clean_state: str,
) -> None:
    batch = _create_batch(client, admin_headers, quantity=2).json()
    response = _generate(
        client,
        admin_headers,
        batch["batch_id"],
        quantity=2,
        auto_issue=True,
    )

    assert response.status_code == 201, response.text
    code_ids = [item["code_id"] for item in response.json()["codes"]]
    with psycopg.connect(clean_state) as conn:
        codes = conn.execute(
            "SELECT status, issued_at FROM activation_codes WHERE id = ANY(%s) ORDER BY id",
            (code_ids,),
        ).fetchall()
        deliveries = conn.execute(
            "SELECT channel, delivered_by_user_id, note "
            "FROM activation_code_deliveries WHERE code_id = ANY(%s)",
            (code_ids,),
        ).fetchall()
        delivered_events = conn.execute(
            "SELECT actor_user_id, reason, request_id "
            "FROM activation_code_events "
            "WHERE code_id = ANY(%s) AND event = 'DELIVERED'",
            (code_ids,),
        ).fetchall()

    assert {row[0] for row in codes} == {"ISSUED"}
    assert all(row[1] for row in codes)
    assert len(deliveries) == 2
    assert {row[0] for row in deliveries} == {"admin_console"}
    assert all(row[1] == "admin_u" and row[2] == "生成批次码" for row in deliveries)
    assert len(delivered_events) == 2
    assert all(row[0] == "admin_u" and row[1] == "生成批次码" for row in delivered_events)
    assert all(row[2] for row in delivered_events)


def test_generate_rejects_budget_overrun(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    batch = _create_batch(client, admin_headers, quantity=3).json()
    first = _generate(client, admin_headers, batch["batch_id"], quantity=3)
    assert first.status_code == 201
    second = _generate(client, admin_headers, batch["batch_id"], quantity=1, key="key-overrun")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "BATCH_BUDGET_EXCEEDED"
    with psycopg.connect(clean_state) as conn:
        count = _row(
            conn,
            "SELECT count(*) FROM activation_codes WHERE batch_id = %s",
            (batch["batch_id"],),
        )[0]
    assert count == 3


def test_generate_rejects_unknown_batch(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = _generate(client, admin_headers, "batch-ghost", quantity=1)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "BATCH_NOT_FOUND"


def test_generate_rejects_closed_batch(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    batch = _create_batch(client, admin_headers, quantity=2).json()
    with psycopg.connect(clean_state) as conn:
        conn.execute(
            "UPDATE activation_code_batches SET status = 'CLOSED' WHERE id = %s",
            (batch["batch_id"],),
        )
        conn.commit()
    response = _generate(client, admin_headers, batch["batch_id"], quantity=1)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "BATCH_NOT_OPEN"


def test_generate_idempotent_replay(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    batch = _create_batch(client, admin_headers, quantity=4).json()
    key = "key-replay-generate"
    first = _generate(client, admin_headers, batch["batch_id"], quantity=4, key=key)
    assert first.status_code == 201
    second = _generate(client, admin_headers, batch["batch_id"], quantity=4, key=key)
    assert second.status_code == 201
    assert second.json()["export_id"] == first.json()["export_id"]
    assert second.headers.get(REPLAY_HEADER) == "true"
    with psycopg.connect(clean_state) as conn:
        codes = _row(
            conn,
            "SELECT count(*) FROM activation_codes WHERE batch_id = %s",
            (batch["batch_id"],),
        )[0]
        exports = _row(
            conn,
            "SELECT count(*) FROM activation_code_exports WHERE batch_id = %s",
            (batch["batch_id"],),
        )[0]
    assert codes == 4
    assert exports == 1


def test_generate_fails_closed_without_keys(
    client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ACTIVATION_CODE_HMAC_KEY_ENV, raising=False)
    batch = _create_batch(client, admin_headers, quantity=1).json()
    response = _generate(client, admin_headers, batch["batch_id"], quantity=1)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ACTIVATION_KEYS_UNAVAILABLE"


# ---------------------------------------------------------------------------
# One-time audited export download
# ---------------------------------------------------------------------------


def _download(
    client: TestClient,
    headers: dict[str, str],
    export_id: str,
    *,
    key: str | None = None,
    **overrides: object,
) -> object:
    payload: dict[str, object] = {"confirm": True, "reason": "渠道取件"}
    payload.update(overrides)
    return client.post(
        f"/api/control/activation-code-exports/{export_id}/download",
        json=payload,
        headers=_write_headers(headers, key=key),
    )


def _generated_export(
    client: TestClient,
    headers: dict[str, str],
    *,
    quantity: int = 2,
) -> tuple[str, list[str]]:
    batch = _create_batch(client, headers, quantity=quantity).json()
    body = _generate(client, headers, batch["batch_id"], quantity=quantity).json()
    return body["export_id"], [code["code_id"] for code in body["codes"]]


def _generated_code_id(client: TestClient, headers: dict[str, str]) -> str:
    _export_id, code_ids = _generated_export(client, headers, quantity=1)
    return code_ids[0]


def _activate_code_directly(dsn: str, code_id: str) -> None:
    """Simulate the T13 first-activation facts straight in SQL.

    The 027 shape matrix demands the issued_at/bound_user_id/activated_at
    triple for an ACTIVE row, so the helper writes all three together.
    """
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES (%s, %s, %s, 'customer') ON CONFLICT (id) DO NOTHING",
            ("customer_u", "customer_u", "Customer U"),
        )
        moment = datetime.now(UTC).replace(microsecond=0).isoformat()
        conn.execute(
            "UPDATE activation_codes SET status = 'ACTIVE', issued_at = %s, "
            "bound_user_id = %s, activated_at = %s WHERE id = %s",
            (moment, "customer_u", moment, code_id),
        )
        conn.commit()


def test_download_returns_plaintext_once_and_audits(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    export_id, code_ids = _generated_export(client, admin_headers, quantity=2)
    response = _download(client, admin_headers, export_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["export_id"] == export_id
    assert len(body["codes"]) == 2
    hmac_key = TEST_CODE_HMAC_KEY.encode()
    with psycopg.connect(clean_state) as conn:
        digests = {
            row[0]
            for row in conn.execute(
                "SELECT code_digest FROM activation_codes WHERE id = ANY(%s)",
                (code_ids,),
            ).fetchall()
        }
        audit = _row(
            conn,
            "SELECT downloaded_at, downloaded_by_user_id, download_reason, download_request_id "
            "FROM activation_code_exports WHERE id = %s",
            (export_id,),
        )
    for code in body["codes"]:
        # The returned plaintext is verifiable against the stored keyed digest.
        assert compute_code_digest(code, key=hmac_key) in digests
    assert audit is not None
    assert audit[0] is not None
    assert audit[1] == "admin_u"
    # PR #43 review P1: the reason and request id of the highest-risk admin
    # operation must be durably recorded, not validated and dropped.
    assert audit[2] == "渠道取件"
    assert audit[3] == body["request_id"]


def test_download_rejects_second_download(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    export_id, _code_ids = _generated_export(client, admin_headers, quantity=1)
    first = _download(client, admin_headers, export_id)
    assert first.status_code == 200
    second = _download(client, admin_headers, export_id, key="key-second-download")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "EXPORT_ALREADY_DOWNLOADED"


def test_download_rejects_expired_export(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    export_id, _code_ids = _generated_export(client, admin_headers, quantity=1)
    with psycopg.connect(clean_state) as conn:
        # Keep the CHECK (expires_at > created_at) satisfied while aging both.
        conn.execute(
            "UPDATE activation_code_exports SET created_at = '2000-01-01T00:00:00+00:00', "
            "expires_at = '2000-01-02T00:00:00+00:00' WHERE id = %s",
            (export_id,),
        )
        conn.commit()
    response = _download(client, admin_headers, export_id)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EXPORT_EXPIRED"


def test_download_requires_write_contract(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    export_id, _code_ids = _generated_export(client, admin_headers, quantity=1)
    assert _download(client, admin_headers, export_id, confirm=False).status_code == 400
    assert _download(client, admin_headers, export_id, reason="  ").status_code == 400
    missing_key = dict(admin_headers)
    missing_key.pop(IDEMPOTENCY_KEY_HEADER, None)
    response = client.post(
        f"/api/control/activation-code-exports/{export_id}/download",
        json={"confirm": True, "reason": "渠道取件"},
        headers=missing_key,
    )
    assert response.status_code == 400
    # Contract failures happen before the one-time consumption, so the export
    # survives them untouched.
    final = _download(client, admin_headers, export_id, key="key-after-contract")
    assert final.status_code == 200


def test_download_rejects_unknown_export(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = _download(client, admin_headers, "export-ghost")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "EXPORT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _deliver(
    client: TestClient,
    headers: dict[str, str],
    code_id: str,
    *,
    key: str | None = None,
    **overrides: object,
) -> object:
    payload: dict[str, object] = {
        "channel": "offline_handover",
        "external_order_ref": "ORD-2026-001",
        "recipient_ref": "渠道商A",
        "confirm": True,
        "reason": "渠道交付",
    }
    payload.update(overrides)
    return client.post(
        f"/api/control/activation-codes/{code_id}/deliver",
        json=payload,
        headers=_write_headers(headers, key=key),
    )


def test_deliver_transitions_code_to_issued(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    response = _deliver(client, admin_headers, code_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["code_id"] == code_id
    assert body["status"] == "ISSUED"
    assert body["delivery_id"]
    with psycopg.connect(clean_state) as conn:
        code = _row(
            conn,
            "SELECT status, issued_at, bound_user_id FROM activation_codes WHERE id = %s",
            (code_id,),
        )
        delivery = _row(
            conn,
            "SELECT channel, external_order_ref, recipient_ref, delivered_by_user_id "
            "FROM activation_code_deliveries WHERE id = %s",
            (body["delivery_id"],),
        )
        delivered_event = _row(
            conn,
            "SELECT actor_user_id, reason, request_id FROM activation_code_events "
            "WHERE code_id = %s AND event = 'DELIVERED'",
            (code_id,),
        )
    assert code is not None
    assert code[0] == "ISSUED"
    assert code[1] is not None
    assert code[2] is None
    assert delivery == ("offline_handover", "ORD-2026-001", "渠道商A", "admin_u")
    assert delivered_event is not None
    assert delivered_event[0] == "admin_u"
    assert delivered_event[1] == "渠道交付"
    assert delivered_event[2]


def test_deliver_rejects_non_generated_code(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    first = _deliver(client, admin_headers, code_id)
    assert first.status_code == 201
    second = _deliver(client, admin_headers, code_id, key="key-redeliver")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "CODE_TRANSITION_INVALID"


def test_deliver_idempotent_replay(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    key = "key-replay-deliver"
    first = _deliver(client, admin_headers, code_id, key=key)
    assert first.status_code == 201
    second = _deliver(client, admin_headers, code_id, key=key)
    assert second.status_code == 201
    assert second.json()["delivery_id"] == first.json()["delivery_id"]
    assert second.headers.get(REPLAY_HEADER) == "true"
    with psycopg.connect(clean_state) as conn:
        count = _row(
            conn,
            "SELECT count(*) FROM activation_code_deliveries WHERE code_id = %s",
            (code_id,),
        )[0]
    assert count == 1


def test_deliver_rejects_unknown_code(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = _deliver(client, admin_headers, "code-ghost")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CODE_NOT_FOUND"


def test_deliver_validates_payload(client: TestClient, admin_headers: dict[str, str]) -> None:
    code_id = _generated_code_id(client, admin_headers)
    for overrides in ({"channel": "   "}, {"channel": ""}):
        response = _deliver(client, admin_headers, code_id, **overrides)  # type: ignore[arg-type]
        assert response.status_code == 400, overrides
        assert response.json()["detail"]["code"] == "DELIVERY_VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Suspension, resume and revocation
# ---------------------------------------------------------------------------


def _status_action(
    client: TestClient,
    headers: dict[str, str],
    code_id: str,
    action: str,
    *,
    key: str | None = None,
    reason: str = "运营处置",
    **overrides: object,
) -> object:
    payload: dict[str, object] = {"confirm": True, "reason": reason}
    payload.update(overrides)
    return client.post(
        f"/api/control/activation-codes/{code_id}/{action}",
        json=payload,
        headers=_write_headers(headers, key=key),
    )


def test_suspend_and_resume_bound_code(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _deliver(client, admin_headers, code_id)
    _activate_code_directly(clean_state, code_id)
    suspended = _status_action(client, admin_headers, code_id, "suspend", reason="风控暂停")
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == "SUSPENDED"
    resumed = _status_action(client, admin_headers, code_id, "resume", reason="风控解除")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "ACTIVE"
    with psycopg.connect(clean_state) as conn:
        code = _row(
            conn,
            "SELECT status, suspended_at, activated_at FROM activation_codes WHERE id = %s",
            (code_id,),
        )
        events = {
            row[0]: row
            for row in conn.execute(
                "SELECT event, actor_user_id, reason, request_id "
                "FROM activation_code_events WHERE code_id = %s",
                (code_id,),
            ).fetchall()
        }
    # The ACTIVE shape demands suspended_at NULL again after a resume.
    assert code is not None
    assert code[0] == "ACTIVE"
    assert code[1] is None
    assert code[2] is not None
    assert set(events) == {"GENERATED", "EXPORTED", "DELIVERED", "SUSPENDED", "RESUMED"}
    assert events["SUSPENDED"][1] == "admin_u"
    assert events["SUSPENDED"][2] == "风控暂停"
    assert events["SUSPENDED"][3]
    assert events["RESUMED"][1] == "admin_u"
    assert events["RESUMED"][2] == "风控解除"
    assert events["RESUMED"][3]


def test_resume_rejects_unbound_suspended_code(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _deliver(client, admin_headers, code_id)
    suspended = _status_action(client, admin_headers, code_id, "suspend", reason="暂停渠道")
    assert suspended.status_code == 200
    # The frozen matrix has no SUSPENDED -> ISSUED edge, so a never-activated
    # code cannot resume — only EXPIRED/REVOKED remain for it.
    resumed = _status_action(client, admin_headers, code_id, "resume", reason="尝试恢复")
    assert resumed.status_code == 409
    assert resumed.json()["detail"]["code"] == "CODE_NOT_ACTIVATED"


def test_suspend_rejects_revoked_code(client: TestClient, admin_headers: dict[str, str]) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _deliver(client, admin_headers, code_id)
    revoked = _status_action(client, admin_headers, code_id, "revoke", reason="作废")
    assert revoked.status_code == 200
    suspended = _status_action(client, admin_headers, code_id, "suspend", key="key-late-suspend")
    assert suspended.status_code == 409
    assert suspended.json()["detail"]["code"] == "CODE_TRANSITION_INVALID"


def test_revoke_issued_code_records_reason(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _deliver(client, admin_headers, code_id)
    response = _status_action(client, admin_headers, code_id, "revoke", reason="渠道退货作废")
    assert response.status_code == 200
    assert response.json()["status"] == "REVOKED"
    with psycopg.connect(clean_state) as conn:
        code = _row(
            conn,
            "SELECT status, revoked_at FROM activation_codes WHERE id = %s",
            (code_id,),
        )
        event = _row(
            conn,
            "SELECT actor_user_id, reason, request_id FROM activation_code_events "
            "WHERE code_id = %s AND event = 'REVOKED'",
            (code_id,),
        )
    assert code is not None
    assert code[0] == "REVOKED"
    assert code[1] is not None
    assert event is not None
    assert event[0] == "admin_u"
    assert event[1] == "渠道退货作废"
    assert event[2]


def test_revoke_idempotent_replay(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _deliver(client, admin_headers, code_id)
    key = "key-replay-revoke"
    first = _status_action(client, admin_headers, code_id, "revoke", key=key)
    assert first.status_code == 200
    second = _status_action(client, admin_headers, code_id, "revoke", key=key)
    assert second.status_code == 200
    assert second.headers.get(REPLAY_HEADER) == "true"
    with psycopg.connect(clean_state) as conn:
        events = _row(
            conn,
            "SELECT count(*) FROM activation_code_events WHERE code_id = %s AND event = 'REVOKED'",
            (code_id,),
        )[0]
    assert events == 1


def test_archive_revoked_code_hides_it_without_deleting_history(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _deliver(client, admin_headers, code_id)
    revoked = _status_action(client, admin_headers, code_id, "revoke", reason="客户停用")
    assert revoked.status_code == 200, revoked.text

    archived = client.post(
        f"/api/control/activation-codes/{code_id}/archive",
        json={"confirm": True, "reason": "清理已撤销激活码"},
        headers={**admin_headers, "Idempotency-Key": "key-archive-code"},
    )

    assert archived.status_code == 200, archived.text
    assert archived.json()["code_id"] == code_id
    assert archived.json()["archived_at"]
    listed = client.get("/api/control/activation-codes", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 0
    assert all(item["code_id"] != code_id for item in listed.json()["items"])
    included = client.get(
        "/api/control/activation-codes",
        params={"include_archived": True},
        headers=admin_headers,
    )
    assert included.status_code == 200, included.text
    assert included.json()["total"] == 1
    assert [item["code_id"] for item in included.json()["items"]] == [code_id]
    assert included.json()["items"][0]["archived_at"]
    filtered = client.get(
        "/api/control/activation-codes",
        params={"include_archived": True, "status": "REVOKED"},
        headers=admin_headers,
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert [item["code_id"] for item in filtered.json()["items"]] == [code_id]
    other_status = client.get(
        "/api/control/activation-codes",
        params={"include_archived": True, "status": "GENERATED"},
        headers=admin_headers,
    )
    assert other_status.status_code == 200, other_status.text
    assert other_status.json()["total"] == 0
    assert other_status.json()["items"] == []
    with psycopg.connect(clean_state) as conn:
        code = _row(
            conn,
            "SELECT status, archived_at FROM activation_codes WHERE id = %s",
            (code_id,),
        )
        audit = _row(
            conn,
            "SELECT actor_user_id, action, metadata_json FROM audit_logs "
            "WHERE entity_type = 'activation_code' AND entity_id = %s "
            "AND action = 'admin.activation_code.archived'",
            (code_id,),
        )
        event_count = _row(
            conn,
            "SELECT count(*) FROM activation_code_events WHERE code_id = %s",
            (code_id,),
        )
    assert code is not None and code[0] == "REVOKED" and code[1] is not None
    assert audit is not None and audit[0] == "admin_u"
    assert json.loads(audit[2])["reason"] == "清理已撤销激活码"
    assert event_count is not None and int(event_count[0]) > 0


def test_archive_rejects_non_revoked_code(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    archived = client.post(
        f"/api/control/activation-codes/{code_id}/archive",
        json={"confirm": True, "reason": "错误清理"},
        headers={**admin_headers, "Idempotency-Key": "key-archive-active"},
    )
    assert archived.status_code == 409
    assert archived.json()["detail"]["code"] == "CODE_NOT_REVOKED"


def test_idempotency_same_key_different_resource_conflicts(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    """PR #43 review P2: the request fingerprint must identify the resource.

    Reusing one key with an identical body against a *different* code of the
    same parameterized route must be a 409 conflict, never a replay of the
    first code's response (which would silently leave the second code
    untouched).
    """
    _export_id, code_ids = _generated_export(client, admin_headers, quantity=2)
    code_a, code_b = code_ids
    _deliver(client, admin_headers, code_a)
    _deliver(client, admin_headers, code_b)
    key = "key-cross-resource"
    first = _status_action(client, admin_headers, code_a, "revoke", key=key)
    assert first.status_code == 200, first.text
    second = _status_action(client, admin_headers, code_b, "revoke", key=key)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert second.headers.get(REPLAY_HEADER) is None
    with psycopg.connect(clean_state) as conn:
        statuses = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT id, status FROM activation_codes WHERE id = ANY(%s)",
                (code_ids,),
            ).fetchall()
        }
    assert statuses[code_a] == "REVOKED"
    # Code B stays untouched: the conflict refused the write instead of
    # replaying code A's success for it.
    assert statuses[code_b] == "ISSUED"


def test_status_action_rejects_unknown_code(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    for action in ("suspend", "resume", "revoke"):
        response = _status_action(client, admin_headers, "code-ghost", action)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "CODE_NOT_FOUND"


def test_status_action_requires_write_contract(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _deliver(client, admin_headers, code_id)
    missing_key = dict(admin_headers)
    response = client.post(
        f"/api/control/activation-codes/{code_id}/suspend",
        json={"confirm": True, "reason": "x"},
        headers=missing_key,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert (
        _status_action(client, admin_headers, code_id, "suspend", confirm=False).status_code == 400
    )
    assert _status_action(client, admin_headers, code_id, "suspend", reason="  ").status_code == 400


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_codes_stays_masked_and_single_code_reveal_is_audited(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    batch_a = _create_batch(client, admin_headers, name="批次A", quantity=2).json()
    batch_b = _create_batch(client, admin_headers, name="批次B", quantity=1).json()
    generated_a = _generate(client, admin_headers, batch_a["batch_id"], quantity=2).json()
    plaintext_a = _download(client, admin_headers, generated_a["export_id"]).json()["codes"]
    generated_b = _generate(client, admin_headers, batch_b["batch_id"], quantity=1).json()
    _deliver(client, admin_headers, generated_b["codes"][0]["code_id"])

    filtered = client.get(
        "/api/control/activation-codes",
        params={"batch_id": batch_a["batch_id"], "status": "GENERATED"},
        headers=admin_headers,
    )
    assert filtered.status_code == 200
    assert filtered.headers["Cache-Control"] == "no-store"
    items = filtered.json()["items"]
    assert len(items) == 2
    for item in items:
        assert item["batch_id"] == batch_a["batch_id"]
        assert item["status"] == "GENERATED"
        assert item["masked_code"].startswith("XS04-")
        assert "***" in item["masked_code"]
        assert "activation_code" not in item
        assert "code_digest" not in item

    reveal_headers = {**admin_headers, "Idempotency-Key": "idem-reveal-code-a"}
    revealed = client.post(
        f"/api/control/activation-codes/{items[0]['code_id']}/reveal",
        json={"confirm": True, "reason": "客服复制给客户"},
        headers=reveal_headers,
    )
    assert revealed.status_code == 200, revealed.text
    assert revealed.headers["Cache-Control"] == "no-store"
    assert revealed.json()["activation_code"] in plaintext_a
    replayed = client.post(
        f"/api/control/activation-codes/{items[0]['code_id']}/reveal",
        json={"confirm": True, "reason": "客服复制给客户"},
        headers=reveal_headers,
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.headers["X-Idempotent-Replay"] == "true"
    assert replayed.json() == revealed.json()

    with psycopg.connect(clean_state) as conn:
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE action = 'admin.activation_code.revealed' AND entity_id = %s",
            (items[0]["code_id"],),
        ).fetchall()
        replay_audit = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE action = 'admin.activation_code.revealed_replay' AND entity_id = %s",
            (items[0]["code_id"],),
        ).fetchall()
    assert len(audit) == 1
    metadata = json.loads(audit[0][0])
    assert metadata["reason"] == "客服复制给客户"
    assert revealed.json()["activation_code"] not in str(audit[0][0])
    assert len(replay_audit) == 1
    replay_metadata = json.loads(replay_audit[0][0])
    assert replay_metadata["original_request_id"] == revealed.json()["request_id"]
    assert replay_metadata["replay_request_id"]
    assert revealed.json()["activation_code"] not in str(replay_audit[0][0])

    issued = client.get(
        "/api/control/activation-codes",
        params={"status": "ISSUED"},
        headers=admin_headers,
    )
    assert issued.status_code == 200
    assert [item["status"] for item in issued.json()["items"]] == ["ISSUED"]


def test_list_codes_requires_session_and_allows_auditor(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    # The exchange already set the auditor cookie on this client, so clear it
    # to exercise the truly unauthenticated path, then re-exchange.
    client.cookies.clear()
    unauthenticated = client.get("/api/control/activation-codes")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"]["code"] == "ADMIN_SESSION_INVALID"
    fresh_headers = _exchange(client, "auditor_u")
    response = client.get("/api/control/activation-codes", headers=fresh_headers)
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_list_codes_returns_bound_account_and_related_devices(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _activate_code_directly(clean_state, code_id)
    with psycopg.connect(clean_state) as conn:
        conn.execute(
            "INSERT INTO customer_devices ("
            "id, activation_code_id, user_id, slot_no, display_name, platform, "
            "fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version, "
            "status, bound_at, last_active_at) "
            "VALUES (%s, %s, %s, 1, %s, %s, %s, 1, %s, 1, 'BOUND', %s, %s)",
            (
                "device-unified-1",
                code_id,
                "customer_u",
                "办公室电脑",
                "windows",
                "fingerprint-test",
                "token-test",
                "2026-09-01T08:00:00+00:00",
                "2026-09-01T09:00:00+00:00",
            ),
        )
        conn.commit()

    response = client.get("/api/control/activation-codes", headers=admin_headers)

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["code_id"] == code_id
    assert item["bound_username"] == "customer_u"
    assert item["expires_at"] == "2099-01-01T00:00:00+00:00"
    assert item["created_at"]
    assert item["devices"] == [
        {
            "device_id": "device-unified-1",
            "slot_no": 1,
            "display_name": "办公室电脑",
            "platform": "windows",
            "status": "BOUND",
            "bound_at": "2026-09-01T08:00:00+00:00",
            "last_active_at": "2026-09-01T09:00:00+00:00",
            "unbound_at": None,
            "revoked_at": None,
        }
    ]
    assert "fingerprint_hmac" not in str(item)
    assert "token_digest" not in str(item)


def test_list_codes_derives_expired_status_without_mutating_code(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    with psycopg.connect(clean_state) as conn:
        conn.execute(
            "UPDATE activation_code_batches SET created_at='2000-01-01T00:00:00+00:00', "
            "activation_expires_at='2000-01-02T00:00:00+00:00' "
            "WHERE id=(SELECT batch_id FROM activation_codes WHERE id=%s)",
            (code_id,),
        )
    response = client.get("/api/control/activation-codes?status=EXPIRED", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["status"] == "EXPIRED"
    with psycopg.connect(clean_state) as conn:
        assert conn.execute(
            "SELECT status FROM activation_codes WHERE id=%s", (code_id,)
        ).fetchone() == ("GENERATED",)


def test_list_codes_keeps_active_code_active_after_activation_deadline(
    client: TestClient, admin_headers: dict[str, str], clean_state: str
) -> None:
    code_id = _generated_code_id(client, admin_headers)
    _activate_code_directly(clean_state, code_id)
    with psycopg.connect(clean_state) as conn:
        conn.execute(
            "UPDATE activation_code_batches "
            "SET created_at=%s, activation_expires_at=%s "
            "WHERE id=(SELECT batch_id FROM activation_codes WHERE id=%s)",
            ("2000-01-01T00:00:00+00:00", "2000-01-02T00:00:00+00:00", code_id),
        )

    active = client.get("/api/control/activation-codes?status=ACTIVE", headers=admin_headers)
    expired = client.get("/api/control/activation-codes?status=EXPIRED", headers=admin_headers)

    assert active.status_code == 200, active.text
    assert active.json()["total"] == 1
    assert active.json()["items"][0]["status"] == "ACTIVE"
    assert expired.status_code == 200, expired.text
    assert expired.json()["total"] == 0


def test_list_codes_does_not_expose_bearer_code_to_auditor(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    batch = _create_batch(client, admin_headers, quantity=1).json()
    _generate(client, admin_headers, batch["batch_id"], quantity=1)

    auditor_headers = _exchange(client, "auditor_u")
    response = client.get("/api/control/activation-codes", headers=auditor_headers)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "activation_code" not in item
    assert "***" in item["masked_code"]
    assert "code_digest" not in item

    forbidden = client.post(
        f"/api/control/activation-codes/{item['code_id']}/reveal",
        json={"confirm": True, "reason": "只读账号不允许查看"},
        headers={**auditor_headers, "Idempotency-Key": "idem-auditor-reveal"},
    )
    assert forbidden.status_code == 403


# ---------------------------------------------------------------------------
# No-Go red lines: plaintext never persists anywhere
# ---------------------------------------------------------------------------


def test_no_plaintext_code_in_logs_idempotency_snapshots_or_columns(
    client: TestClient,
    admin_headers: dict[str, str],
    clean_state: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        batch = _create_batch(client, admin_headers, quantity=1).json()
        generated = _generate(client, admin_headers, batch["batch_id"], quantity=1).json()
        export_id = generated["export_id"]
        code_id = generated["codes"][0]["code_id"]
        downloaded = _download(client, admin_headers, export_id).json()
        _deliver(client, admin_headers, code_id)
        _activate_code_directly(clean_state, code_id)
        _status_action(client, admin_headers, code_id, "suspend", reason="安全演练")
        _status_action(client, admin_headers, code_id, "resume", reason="安全演练结束")
        _status_action(client, admin_headers, code_id, "revoke", reason="安全演练收尾")

    plaintext_codes = downloaded["codes"]
    assert plaintext_codes
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    with psycopg.connect(clean_state) as conn:
        snapshots = "\n".join(
            row[0]
            for row in conn.execute("SELECT response_body FROM admin_write_idempotency").fetchall()
        )
        code_columns = "\n".join(
            str(value)
            for row in conn.execute(
                "SELECT code_digest, masked_code FROM activation_codes"
            ).fetchall()
            for value in row
        )
        export_columns = "\n".join(
            str(value)
            for row in conn.execute(
                "SELECT ciphertext, ciphertext_sha256 FROM activation_code_exports"
            ).fetchall()
            for value in row
        )
        event_columns = "\n".join(
            str(value)
            for row in conn.execute(
                "SELECT event, reason, request_id FROM activation_code_events"
            ).fetchall()
            for value in row
        )
    for plaintext in plaintext_codes:
        assert plaintext not in log_text
        assert plaintext not in snapshots
        assert plaintext not in code_columns
        assert plaintext not in export_columns
        assert plaintext not in event_columns


# ---------------------------------------------------------------------------
# Concurrent same-key writers serialize through the idempotency row
# ---------------------------------------------------------------------------


def test_idempotency_concurrent_same_key_serializes(admin_app: FastAPI, clean_state: str) -> None:
    barrier = threading.Barrier(2)
    responses: dict[int, object] = {}

    def writer(index: int) -> None:
        with TestClient(admin_app) as thread_client:
            headers = _exchange(thread_client)
            barrier.wait(timeout=10)
            responses[index] = _create_batch(
                thread_client, headers, name="并发批次", key="key-race"
            )

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    first, second = responses[0], responses[1]
    assert first.status_code == 201, first.text  # type: ignore[attr-defined]
    assert second.status_code == 201, second.text  # type: ignore[attr-defined]
    assert second.json()["batch_id"] == first.json()["batch_id"]  # type: ignore[attr-defined]
    # The barrier only aligns the starts: either thread may win the placeholder
    # insert, so the order-independent invariant is exactly one original
    # response and one replay of the same business output.
    replay_flags = [
        first.headers.get(REPLAY_HEADER) == "true",  # type: ignore[attr-defined]
        second.headers.get(REPLAY_HEADER) == "true",  # type: ignore[attr-defined]
    ]
    assert replay_flags.count(True) == 1, replay_flags
    with psycopg.connect(clean_state) as conn:
        batches = _row(conn, "SELECT count(*) FROM activation_code_batches")[0]
        snapshots = _row(conn, "SELECT count(*) FROM admin_write_idempotency")[0]
    assert batches == 1
    assert snapshots == 1


def test_minting_audit_persists_reason_and_request_id(
    client: TestClient,
    admin_headers: dict[str, str],
    clean_state: str,
) -> None:
    """M2 review H1: the minting path must persist the write contract's
    reason and request id into the durable audit trail — the batch row
    (creation_reason/creation_request_id), every GENERATED event (reason) and
    every EXPORTED event (request_id) — instead of letting them evaporate
    after contract validation."""
    created = _create_batch(client, admin_headers, quantity=2, key="mint-audit-batch")
    assert created.status_code == 201, created.text
    batch = created.json()
    generated = _generate(
        client, admin_headers, batch["batch_id"], quantity=2, key="mint-audit-gen"
    )
    assert generated.status_code == 201, generated.text
    gen_request_id = generated.headers[REQUEST_ID_HEADER]

    with psycopg.connect(clean_state) as conn:
        batch_reason, batch_request_id = conn.execute(
            "SELECT creation_reason, creation_request_id "
            "FROM activation_code_batches WHERE id = %s",
            (batch["batch_id"],),
        ).fetchone()
        generated_reasons = conn.execute(
            "SELECT reason FROM activation_code_events "
            "WHERE event = 'GENERATED' AND code_id IN "
            "(SELECT id FROM activation_codes WHERE batch_id = %s)",
            (batch["batch_id"],),
        ).fetchall()
        exported_request_ids = conn.execute(
            "SELECT request_id FROM activation_code_events "
            "WHERE event = 'EXPORTED' AND code_id IN "
            "(SELECT id FROM activation_codes WHERE batch_id = %s)",
            (batch["batch_id"],),
        ).fetchall()

    assert batch_reason == "渠道备货"
    assert batch_request_id == batch["request_id"]
    assert len(generated_reasons) == 2
    assert all(row[0] == "生成批次码" for row in generated_reasons)
    assert len(exported_request_ids) == 2
    assert all(row[0] == gen_request_id for row in exported_request_ids)


# ---------------------------------------------------------------------------
# M2 review H2 — PRICE-01 kill-switch on batch creation
# ---------------------------------------------------------------------------


def test_batch_creation_refused_until_price_freeze_decision(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    clean_state: str,
) -> None:
    """M2 review H2: PRICE-01 forbids customer-sale batches until the price
    decision freezes, and the only current guard was a doc note. Without an
    explicit opt-in the API must fail closed — no batch row, so the
    "1-fen face value / 1M credits" mistake stays unmintable."""
    monkeypatch.delenv(BATCH_CREATION_ENV, raising=False)
    response = _create_batch(client, admin_headers, quantity=5)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "BATCH_CREATION_DISABLED"
    with psycopg.connect(clean_state) as conn:
        count = conn.execute("SELECT count(*) FROM activation_code_batches").fetchone()[0]
    assert count == 0, "a refused batch creation must leave no row behind"


def test_batch_creation_refusal_leaves_idempotency_key_unused(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    clean_state: str,
) -> None:
    """The refusal must fire before the idempotency envelope is written —
    otherwise every probe during the freeze burns the operator's
    Idempotency-Key and the post-opt-in retry replays the 403 forever."""
    monkeypatch.delenv(BATCH_CREATION_ENV, raising=False)
    key = "price01-frozen-key"
    refused = _create_batch(client, admin_headers, quantity=1, key=key)
    assert refused.status_code == 403, refused.text
    with psycopg.connect(clean_state) as conn:
        count = conn.execute("SELECT count(*) FROM admin_write_idempotency").fetchone()[0]
    assert count == 0, "the kill-switch refusal must not consume the idempotency key"
    # The same key still mints once the opt-in is explicit.
    monkeypatch.setenv(BATCH_CREATION_ENV, "true")
    allowed = _create_batch(client, admin_headers, quantity=1, key=key)
    assert allowed.status_code == 201, allowed.text


def test_batch_creation_requires_explicit_true_not_any_truthy_string(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the exact "true" opt-in opens minting — "false" (and by the same
    parsing any other string) stays closed."""
    monkeypatch.setenv(BATCH_CREATION_ENV, "false")
    response = _create_batch(client, admin_headers, quantity=1)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "BATCH_CREATION_DISABLED"


def test_download_maps_corrupt_inner_payload_to_400(
    client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decryptable package with a corrupt inner payload is client data rot.

    ``decrypt_code_package`` raises ``InvalidActivationCodeError`` for a
    malformed inner payload; the download route only catches
    ``ActivationExportError``, so the error escapes as an uncontrolled 500
    (M2 review LOW).
    """
    from app import admin_activation_routes
    from app.activation_code_service import InvalidActivationCodeError

    export_id, _code_ids = _generated_export(client, admin_headers, quantity=1)

    def corrupt_package(*args: object, **kwargs: object) -> list[str]:
        raise InvalidActivationCodeError("inner payload is not a valid code package")

    monkeypatch.setattr(admin_activation_routes, "fetch_export_package", corrupt_package)
    response = _download(client, admin_headers, export_id)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "EXPORT_PACKAGE_INVALID"


def test_generate_lets_unexpected_value_error_surface_as_500(
    client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ValueError in the business closure surfaces as a 500, not a fake 503.

    A3（2026-09-02 评估）: the envelope catches only RuntimeError and
    MissingDatabaseConfigError (a missing runtime). Any other ValueError is a
    real bug — masking it as "service unavailable" hid defects from operators,
    so it now propagates and the global handler answers 500.
    """
    from app import admin_activation_routes

    batch_response = _create_batch(client, admin_headers, quantity=1)
    assert batch_response.status_code == 201, batch_response.text
    batch_id = batch_response.json()["batch_id"]

    def explode(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise ValueError("injected business bug")

    monkeypatch.setattr(admin_activation_routes, "generate_batch_codes", explode)
    with pytest.raises(ValueError, match="injected business bug"):
        _generate(client, admin_headers, batch_id, quantity=1)
