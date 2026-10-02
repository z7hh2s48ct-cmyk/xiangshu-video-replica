"""T23 / BILL-02 — audited admin adjustments (审计化后台调账).

Fail-first tests for the frozen file ``server/app/admin_customer_routes.py``
(code checklist §3.2) and migration ``039_admin_adjustments``. Task list §5
T23 exit gate: *双确认、来源单、幂等、真实 actor；禁止直接改余额* — the
adjustment lands as one atomic transaction that writes the ``PAID``
``provider='admin_adjustment'`` recharge order (revision 026 shapes: created
PAID, no trade number), the wallet ``CHARGE`` ledger row, the atomic wallet
credit increment and one append-only ``admin_adjustments`` audit row naming
the real acting administrator (dev doc §15: real actor, reason,
confirmation, Idempotency-Key, request id).

Contract under test:

- ``POST /api/control/customers/{user_id}/adjustments`` requires the full
  admin write contract: a real admin session (auditors are read-only, 403
  ``AUDITOR_READ_ONLY``), ``Idempotency-Key`` (400 without it),
  ``confirm=true`` (400 ``CONFIRMATION_REQUIRED``) and a non-blank reason
  (400 ``REASON_REQUIRED``) — the T12/T18 precedent;
- every adjustment carries a source document (来源单): a frozen enum type
  plus a non-blank reference — 400 ``ADJUSTMENT_VALIDATION_FAILED`` shapes
  are refused, and revision 039's CHECK constraints refuse the malformed
  rows the route might someday let slip (defense in depth);
- the amount is derived from the *current* frozen billing snapshot, never
  typed in by the operator: ``amount_fen = credits *
  internal_base_unit_price_fen`` with the snapshot frozen on the order —
  there is no lane where an operator types an arbitrary amount, and the
  PRICE-01 floor holds by construction (charged == base);
- the pricing scope follows the target user: a customer bound to an
  activation code is ``CUSTOMER_STANDARD``, an internal account stays
  ``INTERNAL`` (revision 026 pairing);
- the ledger difference is zero (账本差额为零): after any adjustment the
  wallet balance grew by exactly ``credits``, the order is PAID with
  ``credits`` credits and one ``CHARGE`` row with ``available_delta =
  credits`` references it — no balance mutation without its ledger row
  (禁止直接改余额);
- idempotency (revision 031 snapshot layer): a same-key retry replays the
  sealed response (``X-Idempotent-Replay: true``) without a second order,
  CHARGE or credit increment; the same key against different parameters or
  a different target user answers 409 ``IDEMPOTENCY_CONFLICT``; a business
  failure (404) rolls the placeholder back so the key stays reusable;
- ``paid_at`` and the audit row timestamps come from the PostgreSQL
  transaction clock (SES-01 discipline — never the process clock);
- the SQLite/missing-DSN runtime answers 503
  ``ADJUSTMENT_SERVICE_UNAVAILABLE`` (fail-closed, the T12/T18 precedent);
- ``admin_adjustments`` is append-only: UPDATE and DELETE are refused by the
  revision 039 trigger and TRUNCATE by the shared 036 guard;
- ``GET /api/control/customers/{user_id}/adjustments`` lists the audit
  trail for operators and auditors (the T33 management page entry point).
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip
from psycopg.errors import CheckViolation

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T23_DB_NAME = "t23_admin_adjustments_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)  # admin-session HMAC key

ADJUSTMENTS_PATH = "/api/control/customers/{user_id}/adjustments"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

# The 002/022 runtime_settings default billing snapshot (fen).
BASE_UNIT_PRICE_FEN = 1000
MIN_RECHARGE_FEN = 10000
RECHARGE_STEP_FEN = 1000

SOURCE_DOCUMENT_TYPES = (
    "CS_TICKET",
    "REFUND_APPROVAL",
    "COMPENSATION_APPROVAL",
    "LEDGER_CORRECTION",
)

CUSTOMER_USER_ID = "customer_u"
INTERNAL_USER_ID = "internal_u"
WALLETS_USER_ID = "walletless_u"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t23_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T23_DB_NAME}"


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def adjustments_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T23_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T23_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t23_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t23_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T23_DB_NAME}" WITH (FORCE)')


def _seed_customer_activation(conn: psycopg.Connection) -> None:
    """Bind customer_u to an activation code (batch -> code -> activation)."""
    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        " quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES ('batch-cu', 'batch-cu', 1500, 1000, 100, 1, "
        f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
    )
    # Create a dummy recharge order for the activation FK
    dummy_order_id = "dummy-activation-order"
    conn.execute(
        "INSERT INTO recharge_orders "
        "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
        " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
        "VALUES (%s, 'customer_u', %s, 'admin_adjustment', 'PAID', 'CUSTOMER_STANDARD', "
        "1000, 1000, 10000, 1000, 1000, 1, now())",
        (dummy_order_id, "DUMMY-activation-order"),
    )
    conn.execute(
        "INSERT INTO activation_codes "
        "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
        " issued_at, bound_user_id, activated_at) "
        "VALUES ('code-cu', 'batch-cu', 'digest-cu', 1, 'XS04-****', "
        "'ACTIVE', '2026-01-01T00:00:00+00:00', 'customer_u', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO activation_code_activations "
        "(id, code_id, user_id, first_device_id, recharge_order_id) "
        "VALUES ('act-cu', 'code-cu', 'customer_u', NULL, %s)",
        (dummy_order_id,),
    )


@pytest.fixture()
def route_state(adjustments_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE admin_adjustments, admin_device_events, "
            "device_pairing_requests, customer_session_events, "
            "customer_session_state, customer_idempotency_envelopes, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            "security_rate_limit_counters, security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        # TRUNCATE users CASCADE also swept runtime_settings (its
        # updated_by_user_id FK references users) — restore the singleton.
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen) "
            "VALUES (1, 4, 2, 1000, 10000, 1000)"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor User', 'auditor'), "
            f"('{CUSTOMER_USER_ID}', 'customer_u', 'Customer User', 'user'), "
            f"('{INTERNAL_USER_ID}', 'internal_u', 'Internal User', 'user'), "
            f"('{WALLETS_USER_ID}', 'walletless_u', 'Walletless User', 'user')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES "
            f"('{CUSTOMER_USER_ID}', 50, 0), "
            f"('{INTERNAL_USER_ID}', 10, 0)"
        )
        # Seed the opening balances as PAID orders + CHARGE rows so the
        # zero-ledger-difference invariant holds from the very first row
        # (账本差额为零：钱包余额必须有对应的 CHARGE 凭据).
        for owner, opening_credits in ((CUSTOMER_USER_ID, 50), (INTERNAL_USER_ID, 10)):
            opening_order_id = f"opening-{owner}"
            conn.execute(
                "INSERT INTO recharge_orders "
                "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
                " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
                " min_recharge_fen_snapshot, recharge_step_fen_snapshot, "
                " amount_fen, credits, paid_at) "
                "VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', 'INTERNAL', "
                "1000, 1000, 10000, 1000, %s, %s, now())",
                (
                    opening_order_id,
                    owner,
                    f"OPENING-{owner}",
                    opening_credits * 1000,
                    opening_credits,
                ),
            )
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
                " task_id, billing_round, idempotency_key) "
                "VALUES (%s, %s, 'CHARGE', %s, 0, %s, NULL, NULL, %s)",
                (
                    f"opening-charge-{owner}",
                    owner,
                    opening_credits,
                    opening_order_id,
                    f"opening-{owner}",
                ),
            )
        _seed_customer_activation(conn)
        # Freeze the billing snapshot the tests assert against.
        conn.execute(
            "UPDATE runtime_settings SET internal_base_unit_price_fen = 1000, "
            "min_recharge_fen = 10000, recharge_step_fen = 1000 WHERE id = 1"
        )
    yield adjustments_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_routes import router as admin_customer_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_customer_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    # The admin lanes must run on real admin sessions, never a dev identity
    # header shortcut (the T12/T16 fixture precedent).
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    yield app


@pytest.fixture()
def client(admin_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(admin_app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    """Exchange a real admin session cookie + CSRF header (the T12 pattern)."""
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _adjustment_path(user_id: str) -> str:
    return ADJUSTMENTS_PATH.format(user_id=user_id)


def _unit_price_path(user_id: str = CUSTOMER_USER_ID) -> str:
    return f"/api/control/customers/{user_id}/unit-price"


def _create_adjustment(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    user_id: str = CUSTOMER_USER_ID,
    credits: int = 5,
    source_document_type: str = "CS_TICKET",
    source_document_ref: str = "TICKET-1001",
    reason: str = "客服补偿：拆解失败两次",
    confirm: bool = True,
    key: str | None = None,
) -> object:
    headers = dict(admin_headers)
    headers[IDEMPOTENCY_KEY_HEADER] = key or f"key-{uuid.uuid4()}"
    return client.post(
        _adjustment_path(user_id),
        json={
            "confirm": confirm,
            "reason": reason,
            "credits": credits,
            "source_document_type": source_document_type,
            "source_document_ref": source_document_ref,
        },
        headers=headers,
    )


def _fetch_one(query: str, params: tuple[object, ...] = ()) -> tuple | None:
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        return conn.execute(query, params).fetchone()


def _fetch_all(query: str, params: tuple[object, ...] = ()) -> list[tuple]:
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        return conn.execute(query, params).fetchall()


def _wallet_balance(user_id: str) -> tuple[int, int]:
    row = _fetch_one(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
        (user_id,),
    )
    assert row is not None
    return int(row[0]), int(row[1])


def _adjustment_audit_rows(user_id: str) -> list[tuple]:
    return _fetch_all(
        "SELECT id, recharge_order_id, target_user_id, admin_user_id, "
        "source_document_type, source_document_ref, reason, request_id "
        "FROM admin_adjustments WHERE target_user_id = %s ORDER BY created_at",
        (user_id,),
    )


def _order_row(order_id: str) -> tuple:
    row = _fetch_one(
        "SELECT provider, provider_trade_no, status, pricing_scope, "
        "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        "min_recharge_fen_snapshot, recharge_step_fen_snapshot, "
        "amount_fen, credits, paid_at FROM recharge_orders WHERE id = %s",
        (order_id,),
    )
    assert row is not None
    return row


def _charge_rows(order_id: str) -> list[tuple]:
    return _fetch_all(
        "SELECT type, available_delta, reserved_delta, recharge_order_id, task_id, "
        "billing_round, idempotency_key FROM wallet_transactions "
        "WHERE recharge_order_id = %s",
        (order_id,),
    )


# ---------------------------------------------------------------------------
# Schema / migration (revision 039)
# ---------------------------------------------------------------------------


def test_admin_adjustments_table_shape(adjustments_dsn: str) -> None:
    columns = {
        str(row[0])
        for row in _fetch_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'admin_adjustments'"
        )
    }
    assert {
        "id",
        "recharge_order_id",
        "target_user_id",
        "admin_user_id",
        "source_document_type",
        "source_document_ref",
        "reason",
        "request_id",
        "created_at",
    } <= columns
    # One audit row per adjustment order, never two.
    unique_indexes = _fetch_all(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'admin_adjustments' AND indexdef LIKE '%%UNIQUE%%'"
    )
    assert any("recharge_order_id" in str(row[0]) for row in unique_indexes), unique_indexes


def test_admin_adjustments_check_constraints(adjustments_dsn: str) -> None:
    with psycopg.connect(_t23_dsn()) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('schema_u', 'schema_u', 'Schema', 'user') "
            "ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('schema_u', 0, 0) ON CONFLICT DO NOTHING"
        )
        order_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO recharge_orders "
            "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
            " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
            "VALUES (%s, 'schema_u', %s, 'admin_adjustment', 'PAID', 'INTERNAL', "
            "1000, 1000, 10000, 1000, 1000, 1, now())",
            (order_id, f"ADJ-{uuid.uuid4().hex}"),
        )

        def _insert(*, doc_type: str, doc_ref: str, reason: str, request_id: str) -> None:
            conn.execute(
                "INSERT INTO admin_adjustments "
                "(id, recharge_order_id, target_user_id, admin_user_id, "
                " source_document_type, source_document_ref, reason, request_id) "
                "VALUES (%s, %s, 'schema_u', 'admin_u', %s, %s, %s, %s)",
                (str(uuid.uuid4()), order_id, doc_type, doc_ref, reason, request_id),
            )

        # Each violating INSERT aborts the surrounding transaction, so every
        # probe runs in its own transaction and the setup commits first.
        conn.commit()
        for doc_type, doc_ref, reason, request_id in (
            ("MYSTERY", "T-1", "r", "req-1"),
            ("CS_TICKET", "   ", "r", "req-1"),
            ("CS_TICKET", "T-1", "  ", "req-1"),
            ("CS_TICKET", "T-1", "r", " "),
        ):
            with pytest.raises(CheckViolation):
                try:
                    _insert(
                        doc_type=doc_type,
                        doc_ref=doc_ref,
                        reason=reason,
                        request_id=request_id,
                    )
                finally:
                    conn.rollback()


def test_admin_adjustments_append_only(adjustments_dsn: str) -> None:
    order_id = str(uuid.uuid4())
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('append_u', 'append_u', 'Append', 'user'), "
            "('admin_u', 'admin_u', 'Admin User', 'admin') "
            "ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('append_u', 0, 0) ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO recharge_orders "
            "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
            " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
            f"VALUES ('{order_id}', 'append_u', 'ADJ-{uuid.uuid4().hex}', "
            "'admin_adjustment', 'PAID', 'INTERNAL', 1000, 1000, 10000, 1000, 1000, 1, now())"
        )
        adjustment_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO admin_adjustments "
            "(id, recharge_order_id, target_user_id, admin_user_id, "
            " source_document_type, source_document_ref, reason, request_id) "
            f"VALUES ('{adjustment_id}', '{order_id}', 'append_u', 'admin_u', "
            "'CS_TICKET', 'T-1', 'r', 'req-1')"
        )
    # The 039 trigger + the shared 036 TRUNCATE guard refuse every rewrite
    # path at the PostgreSQL level (the 029/036/038 trigger precedents).
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "UPDATE admin_adjustments SET reason = 'rewritten' WHERE id = %s",
                (adjustment_id,),
            )
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM admin_adjustments WHERE id = %s", (adjustment_id,))
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("TRUNCATE admin_adjustments")
        survivors = conn.execute("SELECT count(*) FROM admin_adjustments").fetchone()
    assert survivors is not None and int(survivors[0]) == 1


def test_audit_foreign_keys_refuse_cascade_delete(adjustments_dsn: str) -> None:
    """RESTRICT FKs (the PR review P3): CASCADE would silently erase audit rows
    whenever the row trigger is suspended (session_replication_role = replica),
    and SET NULL cannot apply to a NOT NULL actor. Every referencing delete
    fails loudly; the audit row survives."""
    fk_rows = _fetch_all(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid = 'admin_adjustments'::regclass AND contype = 'f'"
    )
    assert len(fk_rows) == 3  # order + target user + admin user
    for (confdeltype,) in fk_rows:
        assert confdeltype in ("r", "a"), confdeltype  # RESTRICT / NO ACTION only

    order_id = str(uuid.uuid4())
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('restrict_u', 'restrict_u', 'Restrict', 'user'), "
            "('admin_ru', 'admin_ru', 'Admin RU', 'admin') ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO recharge_orders "
            "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
            " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
            f"VALUES ('{order_id}', 'restrict_u', 'ADJ-{uuid.uuid4().hex}', "
            "'admin_adjustment', 'PAID', 'INTERNAL', 1000, 1000, 10000, 1000, 1000, 1, now())"
        )
        conn.execute(
            "INSERT INTO admin_adjustments "
            "(id, recharge_order_id, target_user_id, admin_user_id, "
            " source_document_type, source_document_ref, reason, request_id) "
            "VALUES ('adj-restrict-1', %s, 'restrict_u', 'admin_ru', "
            "'CS_TICKET', 'T-1', 'r', 'req-restrict-1')",
            (order_id,),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM recharge_orders WHERE id = %s", (order_id,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM users WHERE id = 'restrict_u'")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM users WHERE id = 'admin_ru'")
        survivors = conn.execute(
            "SELECT count(*) FROM admin_adjustments WHERE id = 'adj-restrict-1'"
        ).fetchone()
    assert survivors is not None and int(survivors[0]) == 1


# ---------------------------------------------------------------------------
# The adjustment happy path and its ledger invariants
# ---------------------------------------------------------------------------


def test_wallet_business_references_show_order_number_and_generation_project(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.control_routes import router as control_router

    client.app.include_router(control_router)
    headers = _admin_session(client)
    adjusted = _create_adjustment(client, headers, key="business-reference")
    assert adjusted.status_code == 201, adjusted.text
    order = adjusted.json()["order_id"]
    with psycopg.connect(_t23_dsn()) as raw:
        raw.execute(
            "INSERT INTO projects(id,owner_user_id,name) "
            "VALUES('ledger-project',%s,'庭院视频项目')",
            (CUSTOMER_USER_ID,),
        )
        raw.execute(
            "INSERT INTO generation_batches(id,project_id,created_by_user_id,idempotency_key,"
            "request_hash,request_snapshot_json) "
            "VALUES('ledger-batch','ledger-project',%s,'ledger-project','hash','{}')",
            (CUSTOMER_USER_ID,),
        )
        raw.execute(
            "INSERT INTO generation_tasks(id,batch_id,provider,model,status) "
            "VALUES('ledger-task','ledger-batch','metaso','h3','RUNNING')"
        )
        raw.execute(
            "INSERT INTO wallet_transactions(id,user_id,type,available_delta,reserved_delta,"
            "task_id,billing_round,idempotency_key) "
            "VALUES('ledger-hold',%s,'RESERVE',-3,3,'ledger-task',1,'ledger-hold')",
            (CUSTOMER_USER_ID,),
        )
        number = raw.execute(
            "SELECT merchant_order_no FROM recharge_orders WHERE id=%s", (order,)
        ).fetchone()[0]
    # Exercise the real per-operator cookie path on the isolated fixture DB.
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "1")
    result = client.get(
        "/api/control/wallet-transactions", headers=headers, params={"user_id": CUSTOMER_USER_ID}
    )
    assert result.status_code == 200, result.text
    rows = result.json()["items"]
    assert (
        next(row for row in rows if row["recharge_order_id"] == order)["business_label"]
        == f"充值订单 · {number}"
    )
    task = next(row for row in rows if row["task_id"] == "ledger-task")
    assert task["business_label"] == "视频生成 · 庭院视频项目" and task["billing_round"] == 1
    assert task["project_name"] == "庭院视频项目"
    exact = client.get(
        "/api/control/recharge-orders",
        headers=headers,
        params={"order_no": number, "user_id": CUSTOMER_USER_ID},
    )
    assert exact.status_code == 200 and exact.json()["total"] == 1
    assert exact.json()["items"][0]["id"] == order
    wrong_subject = client.get(
        "/api/control/recharge-orders",
        headers=headers,
        params={"order_no": number, "user_id": "another-customer"},
    )
    assert wrong_subject.status_code == 200 and wrong_subject.json()["total"] == 0


def test_adjustment_creates_paid_order_charge_and_audit_row(
    client: TestClient,
) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, credits=5, key="adj-create-1")
    assert response.status_code == 201, response.text
    payload = response.json()
    for field in ("adjustment_id", "order_id", "request_id"):
        assert isinstance(payload.get(field), str) and payload[field], payload
    assert int(payload["credits"]) == 5
    assert int(payload["amount_fen"]) == 5 * BASE_UNIT_PRICE_FEN
    assert payload["pricing_scope"] == "CUSTOMER_STANDARD"
    assert payload["wallet_balance_after"] == 55  # 50 + 5
    assert payload["source_document_type"] == "CS_TICKET"
    assert payload["source_document_ref"] == "TICKET-1001"
    assert response.headers.get(REQUEST_ID_HEADER) == payload["request_id"]

    order = _order_row(payload["order_id"])
    assert order[0] == "admin_adjustment"  # provider
    assert order[1] is None  # no third-party trade number
    assert order[2] == "PAID"  # created PAID by the double-confirmed transaction
    assert order[3] == "CUSTOMER_STANDARD"
    assert order[4] == BASE_UNIT_PRICE_FEN  # base snapshot
    assert order[5] == BASE_UNIT_PRICE_FEN  # charged snapshot (PRICE-01 floor holds)
    assert order[6] == MIN_RECHARGE_FEN
    assert order[7] == RECHARGE_STEP_FEN
    assert order[8] == 5 * BASE_UNIT_PRICE_FEN
    assert order[9] == 5
    assert isinstance(order[10], str) and order[10], "paid_at must be stamped"

    charges = _charge_rows(payload["order_id"])
    assert len(charges) == 1
    assert charges[0][0] == "CHARGE"
    assert charges[0][1] == 5  # available_delta
    assert charges[0][2] == 0  # reserved_delta
    assert charges[0][4] is None  # task_id
    assert charges[0][5] is None  # billing_round
    assert charges[0][6] == f"admin_adjustment:charge:{payload['order_id']}"

    audit = _adjustment_audit_rows(CUSTOMER_USER_ID)
    assert len(audit) == 1
    assert audit[0][1] == payload["order_id"]
    assert audit[0][2] == CUSTOMER_USER_ID
    assert audit[0][3] == "admin_u"  # the real acting administrator
    assert audit[0][4] == "CS_TICKET"
    assert audit[0][5] == "TICKET-1001"
    assert audit[0][7] == payload["request_id"]

    assert _wallet_balance(CUSTOMER_USER_ID) == (55, 0)


def test_adjustment_internal_scope_for_user_without_activation(
    client: TestClient,
) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(
        client,
        admin,
        user_id=INTERNAL_USER_ID,
        credits=2,
        source_document_type="LEDGER_CORRECTION",
        source_document_ref="CORR-77",
        reason="内部测试账户补充",
        key="adj-internal-1",
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["pricing_scope"] == "INTERNAL"
    assert payload["wallet_balance_after"] == 12  # 10 + 2
    assert _order_row(payload["order_id"])[3] == "INTERNAL"
    # The audit trail names the internal account too.
    assert len(_adjustment_audit_rows(INTERNAL_USER_ID)) == 1


def test_free_grant_adjustment_records_zero_amount_and_full_ledger(
    client: TestClient,
) -> None:
    """FREE_GRANT（054）：免费条数发放必须如实记 0 金额，但账务闭环完整.

    钱包照增、CHARGE 台账照写、审计行落 FREE_GRANT；按面值计金额会虚构
    收入，所以 order 的 amount_fen 必须为 0（约束放宽仅限 admin_adjustment）。
    """
    admin = _admin_session(client)
    before_available, before_reserved = _wallet_balance(CUSTOMER_USER_ID)
    response = _create_adjustment(
        client,
        admin,
        credits=3,
        source_document_type="FREE_GRANT",
        source_document_ref="PROMO-2026-09-001",
        reason="运营活动：新客免费生成条数",
        key="adj-free-grant-1",
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert int(payload["credits"]) == 3
    assert int(payload["amount_fen"]) == 0
    assert payload["source_document_type"] == "FREE_GRANT"
    assert payload["source_document_ref"] == "PROMO-2026-09-001"
    assert payload["wallet_balance_after"] == before_available + 3

    order = _order_row(payload["order_id"])
    assert order[0] == "admin_adjustment"
    assert order[2] == "PAID"
    assert order[8] == 0  # amount_fen：免费发放不虚构收入
    assert order[9] == 3  # credits 照实记账

    charges = _charge_rows(payload["order_id"])
    assert len(charges) == 1
    assert charges[0][0] == "CHARGE"
    assert charges[0][1] == 3  # 钱包照增：免费条数走同一冻结/结算通道

    audit = _adjustment_audit_rows(CUSTOMER_USER_ID)
    assert len(audit) == 1
    assert audit[0][4] == "FREE_GRANT"
    assert audit[0][5] == "PROMO-2026-09-001"

    after_available, after_reserved = _wallet_balance(CUSTOMER_USER_ID)
    assert after_available == before_available + 3
    assert after_reserved == before_reserved


def test_free_grant_replay_does_not_double_charge(client: TestClient) -> None:
    """同一幂等键重放 FREE_GRANT：返回原结果，钱包只加一次."""
    admin = _admin_session(client)
    before_available, _ = _wallet_balance(CUSTOMER_USER_ID)
    first = _create_adjustment(
        client,
        admin,
        credits=2,
        source_document_type="FREE_GRANT",
        source_document_ref="PROMO-2026-09-002",
        reason="运营活动：补偿生成失败",
        key="adj-free-grant-replay",
    )
    assert first.status_code == 201, first.text

    replay = _create_adjustment(
        client,
        admin,
        credits=2,
        source_document_type="FREE_GRANT",
        source_document_ref="PROMO-2026-09-002",
        reason="运营活动：补偿生成失败",
        key="adj-free-grant-replay",
    )
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json()["adjustment_id"] == first.json()["adjustment_id"]

    after_available, _ = _wallet_balance(CUSTOMER_USER_ID)
    assert after_available == before_available + 2


def test_free_grant_still_requires_valid_source_document(client: TestClient) -> None:
    """FREE_GRANT 不豁免任何写契约：来源单号、credits、原因照常校验."""
    admin = _admin_session(client)
    blank_ref = _create_adjustment(
        client,
        admin,
        credits=1,
        source_document_type="FREE_GRANT",
        source_document_ref="   ",
        reason="运营活动",
        key="adj-free-grant-blank-ref",
    )
    assert blank_ref.status_code == 400
    assert blank_ref.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"

    zero_credits = _create_adjustment(
        client,
        admin,
        credits=0,
        source_document_type="FREE_GRANT",
        source_document_ref="PROMO-2026-09-003",
        reason="运营活动",
        key="adj-free-grant-zero",
    )
    assert zero_credits.status_code == 400
    assert zero_credits.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"


@pytest.mark.parametrize("credits", [2_147_483_648, 2**63])
def test_free_grant_overflow_is_rejected_without_ledger_writes(
    client: TestClient, credits: int
) -> None:
    admin = _admin_session(client)
    before_wallet = _wallet_balance(CUSTOMER_USER_ID)
    before_counts = _fetch_one(
        "SELECT (SELECT COUNT(*) FROM recharge_orders), "
        "(SELECT COUNT(*) FROM wallet_transactions), "
        "(SELECT COUNT(*) FROM admin_adjustments)"
    )

    response = _create_adjustment(
        client,
        admin,
        credits=credits,
        source_document_type="FREE_GRANT",
        key="adj-free-grant-overflow",
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    assert _wallet_balance(CUSTOMER_USER_ID) == before_wallet
    assert (
        _fetch_one(
            "SELECT (SELECT COUNT(*) FROM recharge_orders), "
            "(SELECT COUNT(*) FROM wallet_transactions), "
            "(SELECT COUNT(*) FROM admin_adjustments)"
        )
        == before_counts
    )


@pytest.mark.parametrize(
    "unit_price_fen, credits", [(1000, 2_147_483), (1000, 2_147_484), (1, 2_147_483_647)]
)
def test_free_grant_at_ledger_calculation_limit_keeps_zero_amount(
    client: TestClient, unit_price_fen: int, credits: int
) -> None:
    admin = _admin_session(client)
    update = client.put(
        _unit_price_path(),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "price-grant-limit"},
        json={"confirm": True, "reason": "额度边界测试", "unit_price_fen": unit_price_fen},
    )
    assert update.status_code == 200, update.text
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE wallets SET available_credits = 0 WHERE user_id = %s", (CUSTOMER_USER_ID,)
        )
    before_available, before_reserved = _wallet_balance(CUSTOMER_USER_ID)

    response = _create_adjustment(client, admin, credits=credits, source_document_type="FREE_GRANT")

    assert response.status_code == 201, response.text
    order = _order_row(response.json()["order_id"])
    assert order[8:10] == (0, credits)
    assert _charge_rows(response.json()["order_id"])[0][1] == credits
    assert _wallet_balance(CUSTOMER_USER_ID) == (before_available + credits, before_reserved)
    assert _adjustment_audit_rows(CUSTOMER_USER_ID)[0][4] == "FREE_GRANT"


def test_zero_amount_stays_forbidden_for_non_admin_providers(
    adjustments_dsn: str,
) -> None:
    """054 的约束放宽只限 admin_adjustment：zpay 订单 0 金额仍被 DB 拒绝."""
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('zero_u', 'zero_u', 'Zero', 'user') ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('zero_u', 0, 0) ON CONFLICT DO NOTHING"
        )
        with pytest.raises(CheckViolation):
            conn.execute(
                "INSERT INTO recharge_orders "
                "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
                " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
                " min_recharge_fen_snapshot, recharge_step_fen_snapshot, "
                " amount_fen, credits, paid_at) "
                "VALUES (%s, 'zero_u', %s, 'zpay', 'PENDING', 'INTERNAL', "
                "1000, 1000, 10000, 1000, 0, 1, now())",
                (str(uuid.uuid4()), f"ZP-{uuid.uuid4().hex}"),
            )


def test_admin_cannot_adjust_or_reprice_their_own_account(client: TestClient) -> None:
    admin = _admin_session(client)

    adjustment = _create_adjustment(
        client,
        admin,
        user_id="admin_u",
        credits=1,
        key="admin-self-adjustment",
    )
    price = client.put(
        _unit_price_path("admin_u"),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "admin-self-price"},
        json={
            "confirm": True,
            "reason": "must be rejected by separation of duties",
            "unit_price_fen": 500,
        },
    )

    assert adjustment.status_code == 403
    assert adjustment.json()["detail"]["code"] == "ADMIN_SELF_SERVICE_FORBIDDEN"
    assert price.status_code == 403
    assert price.json()["detail"]["code"] == "ADMIN_SELF_SERVICE_FORBIDDEN"
    denials = _fetch_all(
        "SELECT action, entity_id, metadata_json FROM audit_logs "
        "WHERE actor_user_id = 'admin_u' "
        "AND action = 'security.admin_self_service_denied' ORDER BY created_at"
    )
    assert len(denials) == 2
    assert {str(row[1]) for row in denials} == {"admin_u"}
    assert {json.loads(str(row[2]))["attempted_action"] for row in denials} == {
        "customer_adjustment.create",
        "customer_unit_price.update",
    }


def test_suspended_code_prices_as_customer_revoked_does_not(client: TestClient) -> None:
    """Pricing follows revision 027's current-binding rule (the PR review P3):
    ACTIVE or SUSPENDED count as a current binding; a REVOKED code keeps its
    binding row for audit only and must not price as CUSTOMER_STANDARD."""
    admin = _admin_session(client)
    # The 027 status-shape CHECK couples every state to its proof timestamp
    # (suspended_at / revoked_at), so the UPDATE stamps it too.
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE activation_codes SET status = 'SUSPENDED', "
            "suspended_at = '2026-01-02T00:00:00+00:00' WHERE id = 'code-cu'"
        )
    suspended = _create_adjustment(client, admin, credits=1, key="adj-scope-susp-1")
    assert suspended.status_code == 201, suspended.text
    assert suspended.json()["pricing_scope"] == "CUSTOMER_STANDARD"

    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE activation_codes SET status = 'REVOKED', "
            "revoked_at = '2026-01-03T00:00:00+00:00' WHERE id = 'code-cu'"
        )
    revoked = _create_adjustment(client, admin, credits=1, key="adj-scope-rev-1")
    assert revoked.status_code == 201, revoked.text
    assert revoked.json()["pricing_scope"] == "INTERNAL"


def test_ledger_difference_is_zero_after_adjustment(client: TestClient) -> None:
    """账本差额为零：钱包、order 与 CHAGE 三方一致，无无凭据的余额变化."""
    admin = _admin_session(client)
    before_available, _ = _wallet_balance(CUSTOMER_USER_ID)
    first = _create_adjustment(client, admin, credits=3, key="adj-zero-1")
    second = _create_adjustment(client, admin, credits=7, key="adj-zero-2")
    assert first.status_code == 201 and second.status_code == 201

    after_available, after_reserved = _wallet_balance(CUSTOMER_USER_ID)
    assert after_available == before_available + 3 + 7
    assert after_reserved == 0

    rows = _fetch_all(
        "SELECT ro.credits, wt.available_delta "
        "FROM recharge_orders AS ro "
        "JOIN wallet_transactions AS wt ON wt.recharge_order_id = ro.id "
        "WHERE ro.provider = 'admin_adjustment' AND ro.user_id = %s "
        "AND ro.merchant_order_no NOT LIKE 'OPENING-%%'",
        (CUSTOMER_USER_ID,),
    )
    assert len(rows) == 2
    assert {row[0] for row in rows} == {3, 7}
    assert {row[1] for row in rows} == {3, 7}

    # Reconciliation: every adjustment order is PAID with exactly one CHARGE
    # and the summed deltas reconcile the wallet balance.
    summary = _fetch_one(
        "SELECT "
        "  (SELECT COALESCE(SUM(available_delta), 0) FROM wallet_transactions "
        "   WHERE user_id = %s AND type = 'CHARGE'), "
        "  (SELECT available_credits + reserved_credits FROM wallets WHERE user_id = %s)",
        (CUSTOMER_USER_ID, CUSTOMER_USER_ID),
    )
    assert summary is not None
    charged_total, wallet_total = int(summary[0]), int(summary[1])
    assert charged_total == wallet_total, "账本累计入账必须与钱包余额一致（差额为零）"


def test_adjustment_freezes_unit_price_snapshot(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _create_adjustment(client, admin, credits=2, key="adj-price-1")
    assert first.status_code == 201, first.text
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute("UPDATE runtime_settings SET internal_base_unit_price_fen = 2500 WHERE id = 1")
    try:
        second = _create_adjustment(client, admin, credits=2, key="adj-price-2")
        assert second.status_code == 201, second.text
        assert int(first.json()["amount_fen"]) == 2 * BASE_UNIT_PRICE_FEN
        assert int(second.json()["amount_fen"]) == 2 * 2500
        assert _order_row(first.json()["order_id"])[5] == BASE_UNIT_PRICE_FEN
        assert _order_row(second.json()["order_id"])[5] == 2500
    finally:
        with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
            conn.execute(
                "UPDATE runtime_settings SET internal_base_unit_price_fen = 1000 WHERE id = 1"
            )


def test_response_balance_is_the_post_update_row(client: TestClient) -> None:
    """The response balance comes from UPDATE ... RETURNING (the PR review P3):
    a concurrent wallet write landing between any pre-read and the increment
    must be visible in the response — never a stale pre-read plus credits."""
    admin = _admin_session(client)
    # A concurrent writer moved the balance to 77 out-of-band.
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE wallets SET available_credits = 77 WHERE user_id = %s",
            (CUSTOMER_USER_ID,),
        )
    response = _create_adjustment(client, admin, credits=5, key="adj-returning-1")
    assert response.status_code == 201, response.text
    assert response.json()["wallet_balance_after"] == 82  # 77 + 5 — the real row
    assert _wallet_balance(CUSTOMER_USER_ID) == (82, 0)


@pytest.mark.parametrize("source_document_type", ["CS_TICKET", "FREE_GRANT"])
def test_wallet_balance_overflow_is_rejected_not_500(
    client: TestClient, source_document_type: str
) -> None:
    """A balance already at the int4 ceiling plus one more credit must answer
    a stable 400 — never a NumericValueOutOfRange-turned-500 (PR #54 review
    P2: the amount_fen guard alone cannot see the wallet-side overflow)."""
    admin = _admin_session(client)
    # Park the wallet at the int4 ceiling; credits=1 passes every earlier
    # check (amount_fen = 1 × 1000 well inside the ledger range) and only the
    # wallet-side addition would overflow.
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE wallets SET available_credits = 2147483647 WHERE user_id = %s",
            (CUSTOMER_USER_ID,),
        )
    response = _create_adjustment(
        client,
        admin,
        credits=1,
        source_document_type=source_document_type,
        key="adj-walloverflow-1",
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    # The refused write left nothing behind: the balance is untouched and the
    # whole transaction rolled back (no order, no CHARGE, no audit row).
    assert _wallet_balance(CUSTOMER_USER_ID) == (2147483647, 0)
    order_count = _fetch_one(
        "SELECT COUNT(*) FROM recharge_orders "
        "WHERE provider = 'admin_adjustment' AND merchant_order_no NOT LIKE 'OPENING-%%' "
        "AND merchant_order_no NOT LIKE 'DUMMY-%%'"
    )
    assert order_count is not None and int(order_count[0]) == 0
    assert _adjustment_audit_rows(CUSTOMER_USER_ID) == []


# ---------------------------------------------------------------------------
# The admin write contract (dev doc §15)
# ---------------------------------------------------------------------------


def test_missing_idempotency_key_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    headers = dict(admin)
    response = client.post(
        _adjustment_path(CUSTOMER_USER_ID),
        json={
            "confirm": True,
            "reason": "客服补偿",
            "credits": 1,
            "source_document_type": "CS_TICKET",
            "source_document_ref": "T-1",
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_missing_confirmation_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, confirm=False, key="adj-confirm-1")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


def test_missing_reason_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, reason="   ", key="adj-reason-1")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "REASON_REQUIRED"


def test_auditor_cannot_adjust_but_can_read(client: TestClient) -> None:
    auditor = _admin_session(client, actor="auditor_u")
    response = _create_adjustment(client, auditor, credits=1, key="adj-auditor-1")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUDITOR_READ_ONLY"
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)

    listing = client.get(_adjustment_path(CUSTOMER_USER_ID), headers=auditor)
    assert listing.status_code == 200, listing.text


def test_unauthenticated_write_is_rejected(client: TestClient) -> None:
    response = client.post(
        _adjustment_path(CUSTOMER_USER_ID),
        json={
            "confirm": True,
            "reason": "客服补偿",
            "credits": 1,
            "source_document_type": "CS_TICKET",
            "source_document_ref": "T-1",
        },
        headers={IDEMPOTENCY_KEY_HEADER: "adj-anon-1"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Business validation
# ---------------------------------------------------------------------------


def test_nonpositive_credits_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    for credits in (0, -3):
        response = _create_adjustment(client, admin, credits=credits, key=f"adj-cr-{credits}")
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


def test_oversized_credits_would_overflow_is_rejected(client: TestClient) -> None:
    """A credit count whose derived amount overflows the int4 ledger is refused."""
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, credits=10_000_000, key="adj-overflow-1")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"


def test_unknown_user_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, user_id="ghost_u", key="adj-ghost-1")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USER_NOT_FOUND"


def test_user_without_wallet_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, user_id=WALLETS_USER_ID, key="adj-nowallet-1")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "WALLET_NOT_FOUND"


def test_invalid_source_document_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    bad_type = _create_adjustment(
        client, admin, source_document_type="SCRAP_OF_PAPER", key="adj-sdt-1"
    )
    assert bad_type.status_code == 400
    assert bad_type.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"

    blank_ref = _create_adjustment(client, admin, source_document_ref="   ", key="adj-sdr-1")
    assert blank_ref.status_code == 400
    assert blank_ref.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


# ---------------------------------------------------------------------------
# Idempotency (revision 031 snapshot layer)
# ---------------------------------------------------------------------------


def test_same_key_replays_once_without_double_charging(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _create_adjustment(client, admin, credits=4, key="adj-replay-1")
    assert first.status_code == 201, first.text
    second = _create_adjustment(client, admin, credits=4, key="adj-replay-1")
    assert second.status_code == 201, second.text
    assert second.headers.get(REPLAY_HEADER) == "true"
    assert second.json() == first.json()

    assert _wallet_balance(CUSTOMER_USER_ID) == (54, 0)  # 50 + 4 exactly once
    assert len(_adjustment_audit_rows(CUSTOMER_USER_ID)) == 1
    order_count = _fetch_one(
        "SELECT COUNT(*) FROM recharge_orders "
        "WHERE provider = 'admin_adjustment' AND merchant_order_no NOT LIKE 'OPENING-%%' "
        "AND merchant_order_no NOT LIKE 'DUMMY-%%'"
    )
    assert order_count is not None and int(order_count[0]) == 1


def test_same_key_conflicting_params_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _create_adjustment(client, admin, credits=4, key="adj-conflict-1")
    assert first.status_code == 201, first.text
    conflicting = _create_adjustment(client, admin, credits=9, key="adj-conflict-1")
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _wallet_balance(CUSTOMER_USER_ID) == (54, 0)


def test_same_key_different_target_user_is_rejected(client: TestClient) -> None:
    """The path parameters are part of the fingerprint (PR #43 review P2)."""
    admin = _admin_session(client)
    first = _create_adjustment(client, admin, credits=4, key="adj-target-1")
    assert first.status_code == 201, first.text
    cross_target = _create_adjustment(
        client, admin, user_id=INTERNAL_USER_ID, credits=4, key="adj-target-1"
    )
    assert cross_target.status_code == 409
    assert cross_target.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _wallet_balance(INTERNAL_USER_ID) == (10, 0)


def test_failed_write_does_not_burn_the_key(client: TestClient) -> None:
    admin = _admin_session(client)
    missing = _create_adjustment(client, admin, user_id="later_u", credits=1, key="adj-retry-1")
    assert missing.status_code == 404, missing.text
    # The failed write rolled its placeholder back: the same key now works
    # once the target user exists.
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('later_u', 'later_u', 'Later', 'user')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('later_u', 0, 0)"
        )
    retried = _create_adjustment(client, admin, user_id="later_u", credits=1, key="adj-retry-1")
    assert retried.status_code == 201, retried.text
    assert _wallet_balance("later_u") == (1, 0)


def test_half_committed_placeholder_answers_409_not_500(client: TestClient) -> None:
    """A committed placeholder whose response snapshot never landed (the
    envelope's malformed state) must answer 409 on key reuse — never a
    TypeError-turned-500 (the PR review P3)."""
    from app.admin_customer_routes import AdjustmentRequest
    from app.admin_write_contract import idempotency_key_digest as _idempotency_key_digest
    from app.admin_write_contract import request_hash as _request_hash

    admin = _admin_session(client)
    key = "adj-halfcommit-1"
    body = AdjustmentRequest(
        confirm=True,
        reason="客服补偿",
        credits=3,
        source_document_type="CS_TICKET",
        source_document_ref="T-HALF",
    )
    route = "POST /api/control/customers/{user_id}/adjustments"
    request_hash = _request_hash(route, {"user_id": CUSTOMER_USER_ID}, body)
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO admin_write_idempotency "
            "(id, actor_user_id, route, idempotency_key_digest, request_hash) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                "placeholder-halfcommit",
                "admin_u",
                route,
                _idempotency_key_digest(key),
                request_hash,
            ),
        )

    replay = _create_adjustment(
        client, admin, credits=3, source_document_ref="T-HALF", reason="客服补偿", key=key
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    # No adjustment landed behind the refused replay.
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


# ---------------------------------------------------------------------------
# Fail-closed runtime
# ---------------------------------------------------------------------------


def test_missing_pg_runtime_fails_closed(monkeypatch: pytest.MonkeyPatch, route_state: str) -> None:
    """No database configuration at all → 503, never a partial answer.

    The admin session dependency is T12's contract (its own fail-closed
    lane lives in test_admin_auth.py): it is stubbed here so the request
    reaches the adjustment route body, whose PG guard is the unit under
    test (the T18 customer-route fail-closed precedent).
    """
    from app import admin_auth_routes
    from app.admin_auth_routes import AdminActor
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_routes import router as admin_customer_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_customer_router)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    stub_actor = AdminActor(
        user_id="admin_u",
        username="admin_u",
        display_name="Admin User",
        role="admin",
        is_super_admin=False,
        auth_method="password",
        session_id="sess-nopg",
        session_expires_at="2099-01-01T00:00:00+00:00",
        last_activity_at="2026-01-01T00:00:00+00:00",
    )

    def _stub_actor(request: Request) -> AdminActor:  # noqa: ARG001
        return stub_actor

    monkeypatch.setattr(admin_auth_routes, "get_admin_actor", _stub_actor)
    close_pg_pool()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = _create_adjustment(test_client, {}, credits=1, key="adj-nopg-1")
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "ADJUSTMENT_SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# The audit listing (read path)
# ---------------------------------------------------------------------------


def test_customer_full_adjustment_pages_and_merged_refund_history(
    client: TestClient, route_state, monkeypatch
) -> None:
    from app.admin_audit_routes import router as audit_router

    client.app.include_router(audit_router)
    admin = _admin_session(client)
    assert (
        _create_adjustment(
            client,
            admin,
            credits=100,
            source_document_type="FREE_GRANT",
            source_document_ref="opening-100",
            key="open100",
        ).status_code
        == 201
    )
    refund = _create_adjustment(
        client,
        admin,
        credits=-100,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="refund100",
        reason="已审批退款扣减100积分",
        key="refund100",
    )
    assert refund.status_code == 201, refund.text
    for index in range(23):
        result = _create_adjustment(
            client,
            admin,
            credits=1,
            source_document_type="FREE_GRANT",
            source_document_ref=f"paged-{index}",
            key=f"paged-{index}",
        )
        assert result.status_code == 201, result.text
    assert (
        _create_adjustment(
            client,
            admin,
            user_id=INTERNAL_USER_ID,
            credits=1,
            source_document_ref="other-customer",
            key="other-customer",
        ).status_code
        == 201
    )
    pages = [
        client.get(
            _adjustment_path(CUSTOMER_USER_ID),
            headers=admin,
            params={"limit": 20, "offset": offset},
        ).json()
        for offset in [0, 20]
    ]
    items = [item for page in pages for item in page["items"]]
    assert [len(page["items"]) for page in pages] == [20, 5]
    assert all(page["total"] == 25 for page in pages)
    assert len({item["adjustment_id"] for item in items}) == 25
    returned = next(item for item in items if item["source_document_ref"] == "refund100")
    assert returned["admin_username"] == "admin_u"
    assert (returned["balance_before"], returned["balance_after"]) == (150, 50)
    for action in ["suspend", "resume"]:
        response = client.post(
            f"/api/control/customers/{CUSTOMER_USER_ID}/{action}",
            headers={**admin, IDEMPOTENCY_KEY_HEADER: f"merged-{action}"},
            json={"confirm": True, "reason": f"客户操作记录验收-{action}"},
        )
        assert response.status_code == 200, response.text
    changed_price = client.put(
        _unit_price_path(),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "merged-price"},
        json={"confirm": True, "reason": "客户操作记录验收-改价", "unit_price_fen": 1100},
    )
    assert changed_price.status_code == 200, changed_price.text
    reset_price = client.put(
        _unit_price_path(),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "merged-price-reset"},
        json={"confirm": True, "reason": "客户操作记录验收-恢复默认", "unit_price_fen": None},
    )
    assert reset_price.status_code == 200, reset_price.text
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES('customer-read',%s,'analysis.create','task','own-task','{}')",
            (CUSTOMER_USER_ID,),
        )
    with psycopg.connect(route_state) as raw:
        for event_id, metadata in [
            ("legacy-price", {"old_unit_price_fen": None, "new_unit_price_fen": 1200}),
            ("legacy-missing-price", {}),
        ]:
            raw.execute(
                "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,"
                "metadata_json,created_at) "
                "VALUES(%s,'admin_u','customer_unit_price.update','customer_unit_p"
                "rice',%s,%s,'2000-01-01T00:00:00+00:00')",
                (event_id, CUSTOMER_USER_ID, json.dumps(metadata)),
            )
    pages = [
        client.get(
            "/api/control/audit-log",
            headers=admin,
            params={
                "scope": "all",
                "target_user_id": CUSTOMER_USER_ID,
                "limit": 20,
                "offset": offset,
            },
        ).json()
        for offset in [0, 20]
    ]
    events = [item for page in pages for item in page["items"]]
    assert all(page["total"] == 32 for page in pages)
    assert [len(page["items"]) for page in pages] == [20, 12]
    event = next(item for item in events if item["source_document_ref"] == "refund100")
    assert event["actor_username"] == "admin_u" and event["reason"] == "已审批退款扣减100积分"
    assert event["change_detail"]["changes"]["available_credits"] == {"before": 150, "after": 50}
    assert any(item["event_type"] == "analysis.create" for item in events)
    for event_type in ["customer.suspend", "customer.resume", "customer_unit_price.update"]:
        operation = next(item for item in events if item["event_type"] == event_type)
        assert operation["actor_username"] == "admin_u"
        assert operation["reason"].startswith("客户操作记录验收-")
    price_event = next(
        item for item in events if item["event_type"] == "customer_unit_price.update"
    )
    assert (price_event["old_unit_price_fen"], price_event["new_unit_price_fen"]) == (None, 1100)
    assert price_event["change_detail"]["changes"]["customer_unit_price"] == {
        "before": {
            "mode": "DEFAULT",
            "custom_unit_price_fen": None,
            "effective_unit_price_fen": 1000,
        },
        "after": {
            "mode": "CUSTOM",
            "custom_unit_price_fen": 1100,
            "effective_unit_price_fen": 1100,
        },
    }
    reset_event = next(item for item in events if item["event_type"] == "customer_unit_price.reset")
    assert reset_event["change_detail"]["changes"]["customer_unit_price"] == {
        "before": {
            "mode": "CUSTOM",
            "custom_unit_price_fen": 1100,
            "effective_unit_price_fen": 1100,
        },
        "after": {
            "mode": "DEFAULT",
            "custom_unit_price_fen": None,
            "effective_unit_price_fen": 1000,
        },
    }
    legacy_event = next(item for item in events if item["event_id"] == "legacy-price")
    assert legacy_event["change_detail"]["changes"]["customer_unit_price"]["before"] == {
        "mode": "DEFAULT",
        "custom_unit_price_fen": None,
        "effective_unit_price_fen": None,
    }
    missing_event = next(item for item in events if item["event_id"] == "legacy-missing-price")
    assert missing_event["change_detail"] is None
    assert all(item["target_user_id"] == CUSTOMER_USER_ID for item in events)
    import csv
    import io

    from app.control_routes import router as control_router

    client.app.include_router(control_router)
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "1")
    for path, expected_total, page_sizes in [
        ("/api/control/recharge-orders", 26, [20, 6]),
        ("/api/control/wallet-transactions", 26, [20, 6]),
    ]:
        responses = [
            client.get(
                path,
                headers=admin,
                params={"user_id": CUSTOMER_USER_ID, "limit": 20, "offset": offset},
            )
            for offset in [0, 20]
        ]
        assert all(response.status_code == 200 for response in responses)
        fund_pages = [response.json() for response in responses]
        assert all(page["total"] == expected_total for page in fund_pages)
        assert [len(page["items"]) for page in fund_pages] == page_sizes
        fund_items = [item for page in fund_pages for item in page["items"]]
        assert len({item["id"] for item in fund_items}) == expected_total
        assert all(item["user_id"] == CUSTOMER_USER_ID for item in fund_items)
    exported = client.get(
        "/api/control/wallet-transactions.csv", headers=admin, params={"user_id": CUSTOMER_USER_ID}
    )
    assert exported.status_code == 200, exported.text
    csv_rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
    assert len(csv_rows) == 26
    assert all(row["user_id"] == CUSTOMER_USER_ID for row in csv_rows)
    assert any(
        row["business_label"] == "退款扣减" and row["available_delta"] == "-100" for row in csv_rows
    )
    assert any(
        row["order_no"] and row["business_label"] == "充值订单 · " + row["order_no"]
        for row in csv_rows
    )


def test_list_adjustments_returns_audit_trail(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _create_adjustment(
        client, admin, credits=1, source_document_ref="T-A", key="adj-list-1"
    )
    second = _create_adjustment(
        client,
        admin,
        credits=2,
        source_document_type="COMPENSATION_APPROVAL",
        source_document_ref="COMP-B",
        key="adj-list-2",
    )
    assert first.status_code == 201 and second.status_code == 201

    listing = client.get(_adjustment_path(CUSTOMER_USER_ID), headers=admin)
    assert listing.status_code == 200, listing.text
    payload = listing.json()
    items = payload["items"]
    assert len(items) == 2
    by_ref = {item["source_document_ref"]: item for item in items}
    assert set(by_ref) == {"T-A", "COMP-B"}
    assert by_ref["T-A"]["credits"] == 1
    assert by_ref["COMP-B"]["credits"] == 2
    assert by_ref["T-A"]["amount_fen"] == BASE_UNIT_PRICE_FEN
    assert by_ref["COMP-B"]["amount_fen"] == 2 * BASE_UNIT_PRICE_FEN
    assert by_ref["T-A"]["admin_user_id"] == "admin_u"
    assert by_ref["T-A"]["source_document_type"] == "CS_TICKET"
    assert by_ref["COMP-B"]["source_document_type"] == "COMPENSATION_APPROVAL"
    for item in items:
        assert item["status"] == "PAID"
        assert item["request_id"]
        assert item["adjustment_id"]
        assert item["order_id"]

    # Pagination is bounded.
    paged = client.get(
        _adjustment_path(CUSTOMER_USER_ID),
        params={"limit": 1, "offset": 1},
        headers=admin,
    )
    assert paged.status_code == 200, paged.text
    assert len(paged.json()["items"]) == 1
    assert paged.json()["items"][0]["source_document_ref"] == "COMP-B"

    newest_first = client.get(
        _adjustment_path(CUSTOMER_USER_ID),
        params={"sort": "desc", "limit": 1},
        headers=admin,
    )
    assert newest_first.status_code == 200, newest_first.text
    assert newest_first.json()["items"][0]["source_document_ref"] == "COMP-B"


def test_list_adjustments_for_unknown_user_is_empty(client: TestClient) -> None:
    admin = _admin_session(client)
    listing = client.get(_adjustment_path("ghost_u"), headers=admin)
    assert listing.status_code == 200
    assert listing.json()["items"] == []


def test_list_all_adjustments_includes_users_filters_and_balances(client: TestClient) -> None:
    admin = _admin_session(client)
    created = _create_adjustment(client, admin, credits=5, source_document_ref="GLOBAL-LOOKUP")
    assert created.status_code == 201, created.text

    listing = client.get(
        "/api/control/adjustments",
        params={
            "actor_username": "admin",
            "target_username": "customer",
            "source_document_type": "CS_TICKET",
        },
        headers=admin,
    )
    assert listing.status_code == 200, listing.text
    item = next(i for i in listing.json()["items"] if i["source_document_ref"] == "GLOBAL-LOOKUP")
    assert item["admin_username"] == "admin_u"
    assert item["target_username"] == "customer_u"
    assert item["balance_after"] == item["balance_before"] + 5

    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="immutable"):
            conn.execute(
                "UPDATE wallet_transactions SET ledger_sequence = NULL "
                "WHERE recharge_order_id = %s",
                (item["order_id"],),
            )


def test_list_customers_returns_activated_customers(client: TestClient) -> None:
    """ADM-02 read path: the customer list is the activation fact (masked
    code, username, activation time, code status) — display metadata only."""
    admin = _admin_session(client)
    response = client.get("/api/control/customers", headers=admin)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert payload["limit"] == 20 and payload["offset"] == 0
    customer = payload["items"][0]
    assert customer["user_id"] == CUSTOMER_USER_ID
    assert customer["username"] == "customer_u"
    assert customer["display_name"] == "Customer User"
    assert customer["available_credits"] == 50
    assert customer["reserved_credits"] == 0
    assert customer["device_slots_used"] == 0
    assert customer["activation_code"] == "XS04-****"
    assert customer["status"] == "ACTIVE"
    assert customer["created_at"]
    assert customer["generation_total"] == 0
    assert customer["generation_succeeded"] == 0
    assert customer["generation_failed"] == 0
    assert customer["generation_in_progress"] == 0
    assert customer["generation_attention"] == 0
    assert customer["credits_spent"] == 0


def test_list_customers_filters_status_time_and_balance(client: TestClient) -> None:
    admin = _admin_session(client)
    matched = client.get(
        "/api/control/customers",
        params={
            "status": "ACTIVE",
            "created_from": "2020-01-01",
            "created_to": "2099-12-31",
            "balance_min": 50,
            "balance_max": 50,
        },
        headers=admin,
    )
    assert matched.status_code == 200, matched.text
    assert matched.json()["total"] == 1

    excluded = client.get(
        "/api/control/customers",
        params={"status": "SUSPENDED", "balance_min": 51},
        headers=admin,
    )
    assert excluded.status_code == 200, excluded.text
    assert excluded.json()["items"] == []


def test_list_customers_aggregates_generation_usage_and_settled_credits(
    client: TestClient,
) -> None:
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) "
            "VALUES ('usage-project', %s, 'Usage Project')",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO generation_batches ("
            "id, project_id, created_by_user_id, idempotency_key, request_hash, "
            "request_snapshot_json) VALUES ("
            "'usage-batch', 'usage-project', %s, 'usage-key', 'usage-hash', '{}')",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO generation_tasks (id, batch_id, provider, model, status, archive_status) "
            "VALUES "
            "('usage-success', 'usage-batch', 'metaso', 'h3', 'SUCCEEDED', 'ARCHIVED'), "
            "('usage-failed', 'usage-batch', 'metaso', 'h3', 'FAILED', 'PENDING'), "
            "('usage-running', 'usage-batch', 'metaso', 'h3', 'RUNNING', 'PENDING'), "
            "('usage-attention', 'usage-batch', 'metaso', 'h3', "
            " 'SUBMISSION_UNCERTAIN', 'PENDING')"
        )
        conn.execute(
            "INSERT INTO person_identities (id,owner_user_id,display_name,authorization_status,"
            "source_quality_status,status,created_by) VALUES "
            "('usage-person',%s,'口播人物','AUTHORIZED','PASSED','ACTIVE',%s)",
            (CUSTOMER_USER_ID, CUSTOMER_USER_ID),
        )
        conn.execute(
            "INSERT INTO oral_avatars(id,identity_id,owner_user_id,title,status,source_kind,"
            "source_asset_id) VALUES "
            "('usage-avatar','usage-person',%s,'分身','READY','IMAGE','source')",
            (CUSTOMER_USER_ID,),
        )
        for state in ("SUCCEEDED", "FAILED", "RUNNING", "SUBMISSION_UNCERTAIN", "ARCHIVE_FAILED"):
            conn.execute(
                "INSERT INTO oral_tasks(id,owner_user_id,identity_id,avatar_id,mode,title,status,"
                "estimated_cost_fen,idempotency_key,request_hash) VALUES "
                "(%s,%s,'usage-person','usage-avatar','TTS','口播',%s,0,%s,'hash')",
                (f"usage-oral-{state}", CUSTOMER_USER_ID, state, f"usage-oral-{state}"),
            )
        conn.execute(
            "INSERT INTO wallet_transactions ("
            "id, user_id, type, available_delta, reserved_delta, task_id, "
            "billing_round, idempotency_key) VALUES ("
            "'usage-settle', %s, 'SETTLE', 0, -1, 'usage-success', 1, 'usage-settle')",
            (CUSTOMER_USER_ID,),
        )

    admin = _admin_session(client)
    response = client.get("/api/control/customers", headers=admin)

    assert response.status_code == 200, response.text
    customer = response.json()["items"][0]
    assert customer["generation_total"] == 9
    assert customer["generation_succeeded"] == 2
    assert customer["generation_failed"] == 2
    assert customer["generation_in_progress"] == 2
    assert customer["generation_attention"] == 3
    assert customer["credits_spent"] == 1


def test_list_customers_supports_pagination_and_username_filter(
    client: TestClient,
) -> None:
    admin = _admin_session(client)
    # The username filter matches the seeded customer.
    response = client.get("/api/control/customers", params={"username": "customer"}, headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    # A filter that matches nothing returns an empty, well-formed page.
    response = client.get("/api/control/customers", params={"username": "nobody"}, headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert response.json()["items"] == []
    # A page beyond the data is empty but well-formed.
    response = client.get(
        "/api/control/customers", params={"limit": 20, "offset": 40}, headers=admin
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_list_customers_keyword_matches_company_name(client: TestClient) -> None:
    """运营按公司名识别账号：关键字筛选同时匹配用户名与 display_name。

    夹具里 username 是 ``customer_u``、display_name 是 ``Customer User``。两条
    断言互为对照，各自只有一支能命中，所以不会互相掩盖：
    - ``User`` 不是 ``customer_u`` 的子串 → 命中只能来自 display_name 分支；
    - ``customer_u`` 不是 ``Customer User`` 的子串 → 命中只能来自 username 分支。
    """
    admin = _admin_session(client)
    company = client.get("/api/control/customers", params={"username": "User"}, headers=admin)
    assert company.status_code == 200, company.text
    assert company.json()["total"] == 1
    assert company.json()["items"][0]["display_name"] == "Customer User"

    username = client.get(
        "/api/control/customers", params={"username": "customer_u"}, headers=admin
    )
    assert username.status_code == 200, username.text
    assert username.json()["total"] == 1


def test_list_customers_is_auditor_readable(client: TestClient) -> None:
    auditor = _admin_session(client, actor="auditor_u")
    response = client.get("/api/control/customers", headers=auditor)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_list_customers_rejects_anonymous(client: TestClient) -> None:
    response = client.get("/api/control/customers")
    assert response.status_code == 401


def test_username_filter_escapes_like_wildcards(
    client: TestClient,
    adjustments_dsn: str,
) -> None:
    """A12（2026-09-02 评估）：用户名筛选中的 %/_ 必须按字面量匹配."""
    admin = _admin_session(client)
    # 种一个带下划线的用户名：未转义时 "t_omer" 的 _ 会通配命中 customer_u。
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('cust_wild', 'cust_t_omer_x', '通配用户', 'customer') "
            "ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO recharge_orders "
            "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
            " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
            "VALUES ('dummy-wild-order', 'cust_wild', 'DUMMY-wild-order', "
            "'admin_adjustment', 'PAID', 'CUSTOMER_STANDARD', "
            "1000, 1000, 10000, 1000, 1000, 1, now())"
        )
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
            " issued_at, bound_user_id, activated_at) "
            "VALUES ('code-wild', 'batch-cu', 'digest-wild', 1, 'XS04-****W', "
            "'ACTIVE', '2026-01-01T00:00:00+00:00', 'cust_wild', "
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO activation_code_activations "
            "(id, code_id, user_id, first_device_id, recharge_order_id) "
            "VALUES ('act-wild', 'code-wild', 'cust_wild', NULL, "
            "'dummy-wild-order')"
        )

    # 字面量 "t_omer" 只命中带下划线的用户名，不再通配到 customer_u。
    response = client.get("/api/control/customers", params={"username": "t_omer"}, headers=admin)
    assert response.status_code == 200, response.text
    usernames = {row["username"] for row in response.json()["items"]}
    assert usernames == {"cust_t_omer_x"}

    # 普通子串匹配不受影响。
    response = client.get("/api/control/customers", params={"username": "customer"}, headers=admin)
    assert response.status_code == 200, response.text
    usernames = {row["username"] for row in response.json()["items"]}
    assert "customer_u" in usernames
    assert "cust_t_omer_x" not in usernames


def test_value_error_inside_write_is_not_masked_as_503(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3（2026-09-02 评估）：业务层裸 ValueError 必须浮出为 500，不得伪装 503."""
    import app.admin_write_contract as admin_write_contract_module

    admin = _admin_session(client)

    def _raise_value_error(*args: object, **kwargs: object) -> None:
        raise ValueError("simulated business-layer bug")

    # The envelope now lives in the shared contract module — patch its
    # pg_transaction reference, not the route module's.
    monkeypatch.setattr(admin_write_contract_module, "pg_transaction", _raise_value_error)
    with pytest.raises(ValueError, match="simulated business-layer bug"):
        _create_adjustment(client, admin, credits=1, key="adj-ve-1")


def test_admin_can_set_read_and_reset_customer_unit_price(client: TestClient) -> None:
    admin = _admin_session(client)

    initial = client.get(_unit_price_path(), headers=admin)
    assert initial.status_code == 200, initial.text
    assert initial.json()["unit_price_fen"] == BASE_UNIT_PRICE_FEN
    assert initial.json()["custom_unit_price_fen"] is None

    update = client.put(
        _unit_price_path(),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "price-set-500"},
        json={
            "confirm": True,
            "reason": "客户合同约定价格",
            "unit_price_fen": 500,
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["unit_price_fen"] == 500
    assert update.json()["custom_unit_price_fen"] == 500
    assert update.json()["default_unit_price_fen"] == BASE_UNIT_PRICE_FEN
    assert update.json()["min_recharge_fen"] == MIN_RECHARGE_FEN
    assert update.json()["recharge_step_fen"] == 500

    stored = _fetch_one(
        "SELECT unit_price_fen, updated_by_user_id FROM customer_unit_prices WHERE user_id = %s",
        (CUSTOMER_USER_ID,),
    )
    assert stored == (500, "admin_u")

    reset = client.put(
        _unit_price_path(),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "price-reset-default"},
        json={
            "confirm": True,
            "reason": "恢复系统默认价格",
            "unit_price_fen": None,
        },
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["unit_price_fen"] == BASE_UNIT_PRICE_FEN
    assert reset.json()["custom_unit_price_fen"] is None
    assert (
        _fetch_one(
            "SELECT unit_price_fen FROM customer_unit_prices WHERE user_id = %s",
            (CUSTOMER_USER_ID,),
        )
        is None
    )


def test_customer_unit_price_controls_adjustment_order_snapshot(client: TestClient) -> None:
    admin = _admin_session(client)
    update = client.put(
        _unit_price_path(),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "price-adjustment-500"},
        json={
            "confirm": True,
            "reason": "客户合同约定价格",
            "unit_price_fen": 500,
        },
    )
    assert update.status_code == 200, update.text

    adjustment = _create_adjustment(client, admin, credits=2)
    assert adjustment.status_code == 201, adjustment.text
    order = _order_row(adjustment.json()["order_id"])
    assert order[4] == BASE_UNIT_PRICE_FEN
    assert order[5] == 500
    assert order[7] == 500
    assert order[8] == 1000


@pytest.mark.parametrize("unit_price_fen", [0, -1, True, "500", 2_147_483_648])
def test_customer_unit_price_rejects_invalid_values(
    client: TestClient,
    unit_price_fen: object,
) -> None:
    admin = _admin_session(client)
    response = client.put(
        _unit_price_path(),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: f"invalid-price-{unit_price_fen}"},
        json={
            "confirm": True,
            "reason": "非法价格测试",
            "unit_price_fen": unit_price_fen,
        },
    )
    assert response.status_code in {400, 422}


def test_customer_unit_price_requires_an_activated_customer(client: TestClient) -> None:
    admin = _admin_session(client)
    response = client.put(
        _unit_price_path(INTERNAL_USER_ID),
        headers={**admin, IDEMPOTENCY_KEY_HEADER: "price-internal-user"},
        json={
            "confirm": True,
            "reason": "内部账号不能设置客户价",
            "unit_price_fen": 500,
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CUSTOMER_NOT_FOUND"


def test_customers_csv_export_is_audited_and_filtered(client: TestClient) -> None:
    """C6：客户列表导出走服务端，带 A2 审计与筛选."""
    admin = _admin_session(client)
    response = client.get(
        "/api/control/customers.csv",
        params={"username": "customer"},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "username" in response.text
    assert "customer_u" in response.text

    audit = _fetch_one(
        "SELECT COUNT(*) FROM audit_logs WHERE action = 'control.export' "
        "AND entity_id = 'customers'"
    )
    assert audit is not None and int(audit[0]) == 1

    # 审计员不可导出（AdminWriter 门）。B3（2026-09-22 评审）：批量导出是数据
    # 外带动作而非查看，资金两个 CSV 导出已收敛到同一口径，三处导出此处对齐。
    auditor = _admin_session(client, actor="auditor_u")
    denied = client.get("/api/control/customers.csv", headers=auditor)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUDITOR_READ_ONLY"


def test_customers_csv_export_normalizes_status_casing(client: TestClient) -> None:
    """PR #85 评审 P2：状态筛选在服务端归一为大写枚举.

    UI 下拉传小写 active/suspended/revoked；导出查询比对的是大写库状态
    （ACTIVE/…），直接透传会得到只有表头的空 CSV。
    """
    admin = _admin_session(client)
    lowered = client.get(
        "/api/control/customers.csv",
        params={"status": "active"},
        headers=admin,
    )
    assert lowered.status_code == 200, lowered.text
    assert "customer_u" in lowered.text

    suspended = client.get(
        "/api/control/customers.csv",
        params={"status": "SUSPENDED"},
        headers=admin,
    )
    assert suspended.status_code == 200, suspended.text
    assert "customer_u" not in suspended.text


def test_customers_csv_export_uses_the_same_date_and_balance_filters(
    client: TestClient,
) -> None:
    admin = _admin_session(client)
    response = client.get(
        "/api/control/customers.csv",
        params={
            "created_from": "2099-01-01",
            "created_to": "2099-12-31",
            "balance_min": 0,
            "balance_max": 999999,
        },
        headers=admin,
    )
    assert response.status_code == 200, response.text
    assert "customer_u" not in response.text


def test_customers_csv_export_carries_and_filters_on_company_name(
    client: TestClient,
) -> None:
    """导出与列表同口径：带出公司名，且关键字筛选覆盖它。

    断言表头而非仅断言内容：公司名列若只是"值恰好出现"，无法区分它是独立列
    还是被并进了别的列。
    """
    admin = _admin_session(client)
    response = client.get("/api/control/customers.csv", params={"username": "User"}, headers=admin)
    assert response.status_code == 200, response.text
    header, *body = response.text.splitlines()
    assert header.rstrip("\r").split(",") == [
        "username",
        "display_name",
        "masked_code",
        "activated_at",
        "status",
    ]
    assert body, "按公司名筛选应命中夹具客户，而不是只有表头"
    assert "Customer User" in response.text


def test_customer_code_materials_never_reach_control_csv_exports(
    client: TestClient,
) -> None:
    """CW-009/S2: 码明文与 digest 不得进入控制台 CSV 导出。

    Positive control: the fixture-seeded *activated* row (``customer_u`` with
    digest ``digest-cu``) must flow through the export, so the negative
    assertions below are actually exercised — a header-only CSV must fail
    this test.  An *unactivated* ISSUED code is additionally seeded to pin
    the join boundary: its digest must never leak either.
    """
    admin = _admin_session(client)
    raw_code = "CW09-CSV-CODE-0001"
    code_digest = "cw09-digest-3f9c2b7a"

    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) VALUES "
            "('cw09-csv-batch', 'cw09-csv', 1500, 1000, 100, 1, "
            "'2099-01-01T00:00:00+00:00', 'OPEN', 'admin_u') "
            "ON CONFLICT (id) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
            " issued_at, bound_user_id, activated_at) VALUES "
            "('code-cw09-csv', 'cw09-csv-batch', %s, 1, 'CW09-****', 'ISSUED', "
            "'2026-01-01T00:00:00+00:00', NULL, NULL) "
            "ON CONFLICT (id) DO NOTHING",
            (code_digest,),
        )

    response = client.get("/api/control/customers.csv", headers=admin)
    assert response.status_code == 200, response.text
    # Positive control — the activated fixture row really flows through.
    assert "customer_u" in response.text
    # Negative assertions on the material the export must never carry.
    assert "digest-cu" not in response.text
    assert raw_code not in response.text
    assert code_digest not in response.text


def test_customers_csv_escapes_formula_prefix_usernames(client: TestClient) -> None:
    """C15 (2026-09-17 收敛): customers.csv 的用户可控 username 列必须以撇号
    转义危险公式前缀 (= + - @ \t \r)，阻断 CSV/表格公式注入。

    端点级回归锁——若将来把导出改回裸 ``str(value)`` (丢掉
    ``spreadsheet_safe_cell``)，本测试立即失败；既有安全 username
    (``customer_u``) 不得被过度转义。
    """
    import csv
    import io

    admin = _admin_session(client)
    # role='customer' + registration_source='activation_code' 命中导出的 OR
    # 分支 (无需激活链); username 为 text 无字符集 CHECK，可承载公式前缀探针。
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        for user_id, username in (
            ("cust_csv_eq", "=1+1cmd"),
            ("cust_csv_minus", "-2danger"),
        ):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role, registration_source) "
                "VALUES (%s, %s, 'CSV 注入探针', 'customer', 'activation_code') "
                "ON CONFLICT DO NOTHING",
                (user_id, username),
            )

    response = client.get("/api/control/customers.csv", headers=admin)
    assert response.status_code == 200, response.text

    rows = list(csv.reader(io.StringIO(response.text)))
    username_idx = rows[0].index("username")
    usernames = {row[username_idx] for row in rows[1:] if row}

    # 危险前缀被撇号转义成惰性文本……
    assert "'=1+1cmd" in usernames
    assert "'-2danger" in usernames
    # ……裸公式绝不作为活动单元格出现。
    assert "=1+1cmd" not in usernames
    assert "-2danger" not in usernames
    # 安全 username 不被过度转义。
    assert "customer_u" in usernames


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Tokyo", "America/Los_Angeles"])
def test_w15_order_and_wallet_exports_match_shanghai_filters(
    route_state: str, timezone: str
) -> None:
    import csv
    import io

    from app.auth import CurrentUser
    from app.control_routes import (
        export_recharge_orders_csv,
        export_wallet_transactions_csv,
        list_recharge_orders,
        list_wallet_transactions,
    )
    from app.db_portable import BusinessConnection

    admin = CurrentUser(id="admin_u", username="admin_u", display_name="Admin User", role="admin")
    with psycopg.connect(route_state, autocommit=True) as raw:
        raw.execute("SELECT set_config('TimeZone', %s, false)", (timezone,))
        for index, created in enumerate(
            (
                "2030-09-11 15:59:59",
                "2030-09-11 16:00:00",
                "2030-09-12T00:00:00Z",
                "2030-09-12T23:59:59.999999+08:00",
                "2030-09-12T16:00:00+00:00",
            )
        ):
            raw.execute(
                "INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider, status, "
                "pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
                "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
                "paid_at, created_at, channel, provider_trade_no) "
                "SELECT %s, user_id, %s, 'zpay', status, pricing_scope, "
                "base_unit_price_fen_snapshot, "
                "charged_unit_price_fen_snapshot, min_recharge_fen_snapshot, "
                "recharge_step_fen_snapshot, "
                "amount_fen, credits, paid_at, %s, 'alipay', %s FROM recharge_orders WHERE "
                "id='opening-customer_u'",
                (f"w15-order-{index}", f"W15-{index}", created, f"w15-provider-{index}"),
            )
            raw.execute(
                "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
                "reserved_delta, "
                "recharge_order_id, idempotency_key, created_at) VALUES (%s, 'customer_u', "
                "'CHARGE', 50, 0, %s, %s, %s)",
                (f"w15-tx-{index}", f"w15-order-{index}", f"w15-key-{index}", created),
            )
        conn = BusinessConnection.postgres(raw)
        options = {
            "username": "customer_u",
            "created_from": "2030-09-12",
            "created_to": "2030-09-12",
        }
        orders = list_recharge_orders(
            conn=conn, _actor=admin, status="PAID", channel="alipay", limit=50, offset=0, **options
        )
        csv_response = export_recharge_orders_csv(
            conn=conn, actor=admin, status="PAID", channel="alipay", limit=5000, **options
        )
        records = list(csv.DictReader(io.StringIO(bytes(csv_response.body).decode("utf-8-sig"))))
        assert {item.order_no for item in orders.items} == {"W15-1", "W15-2", "W15-3"}
        assert {row["order_no"] for row in records} == {item.order_no for item in orders.items}
        assert sum(int(row["amount_fen"]) for row in records) == sum(
            item.amount_fen for item in orders.items
        )
        assert csv_response.headers["X-Export-Total"] == "3"
        assert csv_response.headers["X-Export-Truncated"] == "false"
        wallet = list_wallet_transactions(
            conn=conn, _actor=admin, type="CHARGE", limit=50, offset=0, **options
        )
        csv_response = export_wallet_transactions_csv(
            conn=conn, actor=admin, type="CHARGE", limit=5000, **options
        )
        records = list(csv.DictReader(io.StringIO(bytes(csv_response.body).decode("utf-8-sig"))))
        assert (
            {row["id"] for row in records}
            == {item.id for item in wallet.items}
            == {"w15-tx-1", "w15-tx-2", "w15-tx-3"}
        )
        assert csv_response.headers["X-Export-Returned"] == "3"


def test_w15_large_export_reports_real_filtered_total(route_state: str) -> None:
    import csv
    import io

    from app.auth import CurrentUser
    from app.control_routes import export_recharge_orders_csv
    from app.db_portable import BusinessConnection

    with psycopg.connect(route_state, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider, status, "
            "pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
            "paid_at, created_at, channel, provider_trade_no) "
            "SELECT 'w15-large-'||g, user_id, 'W15-LARGE-'||g, 'zpay', status, pricing_scope, "
            "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, "
            "recharge_step_fen_snapshot, amount_fen, credits, paid_at, '2030-09-12 00:00:00', "
            "'alipay', 'w15-provider-'||g "
            "FROM recharge_orders CROSS JOIN generate_series(1,5001) g WHERE "
            "id='opening-customer_u'"
        )
        response = export_recharge_orders_csv(
            conn=BusinessConnection.postgres(raw),
            actor=CurrentUser(id="admin_u", username="admin_u", display_name="Admin", role="admin"),
            username="customer_u",
            status="PAID",
            channel="alipay",
            created_from="2030-09-12",
            created_to="2030-09-12",
            limit=5000,
        )
        assert response.headers["X-Export-Total"] == "5001"
        assert response.headers["X-Export-Returned"] == "5000"
        assert response.headers["X-Export-Truncated"] == "true"
        assert (
            len(list(csv.DictReader(io.StringIO(bytes(response.body).decode("utf-8-sig"))))) == 5000
        )


# ---------------------------------------------------------------------------
# B1 — audited reverse adjustment (反向调账, SOP §10 step 2)
# ---------------------------------------------------------------------------
#
# `docs/客户版部署与灰度手册.md` §10 第 2 步要求「管理员经 T23 审计调账做反向调账，
# 实际退付在 ZPay 后台办理」。在本批之前枚举里有「退款审批」，通道却只收正数，
# 动作与标签方向相反。下面这组用例锁住放开后的形状：
#
# - 反向调账落一条 `REFUND` 流水（available_delta < 0、不挂订单/任务/计费轮次），
#   **不建**充值单（D4），审计行挂在 `admin_adjustments` 上；
# - 余额下限（D2）：超过当前可用余额的退款返回 400 `ADJUSTMENT_BALANCE_INSUFFICIENT`，
#   DB 的 `ck_wallets_available_nonnegative` 是纵深兜底；
# - 正向调账零回归：其余来源类型仍然只收正数。


def _ledger_rows(user_id: str, tx_type: str) -> list[tuple]:
    return _fetch_all(
        "SELECT id, available_delta, reserved_delta, recharge_order_id, task_id, "
        "oral_task_id, billing_round, billing_operation_id, idempotency_key, auth_source "
        "FROM wallet_transactions WHERE user_id = %s AND type = %s ORDER BY id",
        (user_id, tx_type),
    )


def _audit_row_for(adjustment_id: str) -> tuple:
    row = _fetch_one(
        "SELECT id, recharge_order_id, target_user_id, admin_user_id, "
        "source_document_type, source_document_ref, reason, request_id "
        "FROM admin_adjustments WHERE id = %s",
        (adjustment_id,),
    )
    assert row is not None
    return row


def test_reversal_writes_refund_row_and_audit_row_without_order(client: TestClient) -> None:
    """DoD 1: 反向调账 → `REFUND` 负向流水 + 可审计的 admin_adjustments 行，且无充值单."""
    admin = _admin_session(client)
    before_counts = _fetch_one(
        "SELECT (SELECT COUNT(*) FROM recharge_orders), "
        "(SELECT COUNT(*) FROM wallet_transactions), "
        "(SELECT COUNT(*) FROM admin_adjustments)"
    )

    response = _create_adjustment(
        client,
        admin,
        credits=-8,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-2026-0918",
        reason="客户诉求退款：视频未交付，工单 2026-0918",
        key="adj-refund-1",
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    # No order is produced (D4), and the field stays a string: "" not "None".
    assert payload["order_id"] == ""
    assert payload["credits"] == "-8"
    assert payload["amount_fen"] == "0"
    assert payload["wallet_balance_after"] == 42
    assert payload["source_document_type"] == "REFUND_APPROVAL"
    assert payload["source_document_ref"] == "REFUND-2026-0918"
    assert payload["request_id"]

    # Exactly one new ledger row, one new audit row, and NO new order.
    after_counts = _fetch_one(
        "SELECT (SELECT COUNT(*) FROM recharge_orders), "
        "(SELECT COUNT(*) FROM wallet_transactions), "
        "(SELECT COUNT(*) FROM admin_adjustments)"
    )
    assert after_counts[0] == before_counts[0]
    assert after_counts[1] == before_counts[1] + 1
    assert after_counts[2] == before_counts[2] + 1

    refunds = _ledger_rows(CUSTOMER_USER_ID, "REFUND")
    assert len(refunds) == 1
    (
        row_id,
        available_delta,
        reserved_delta,
        order_id,
        task_id,
        oral_task_id,
        billing_round,
        billing_operation_id,
        idempotency_key,
        auth_source,
    ) = refunds[0]
    assert available_delta == -8
    assert reserved_delta == 0
    # The 20260923T1200 shape branch: a reversal is not attached to anything.
    assert order_id is None
    assert task_id is None
    assert oral_task_id is None
    assert billing_round is None
    assert billing_operation_id is None
    assert auth_source == "internal"
    assert str(idempotency_key) == f"admin_adjustment:refund:{payload['adjustment_id']}"

    audit = _audit_row_for(str(payload["adjustment_id"]))
    assert audit[1] is None  # recharge_order_id — the D4 hole, guarded by the CHECK
    assert audit[2] == CUSTOMER_USER_ID
    assert audit[3] == "admin_u"  # real acting administrator, not a service identity
    assert audit[4] == "REFUND_APPROVAL"
    assert audit[5] == "REFUND-2026-0918"
    assert "工单 2026-0918" in str(audit[6])
    assert str(audit[7])

    assert _wallet_balance(CUSTOMER_USER_ID) == (42, 0)
    assert str(row_id) == f"admin_adjustment:refund:{payload['adjustment_id']}"


def test_reversal_is_visible_in_the_audit_listings(client: TestClient) -> None:
    """反向调账必须出现在两个调账列表里（LEFT JOIN），并且能看出方向."""
    admin = _admin_session(client)
    created = _create_adjustment(
        client,
        admin,
        credits=-6,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-2026-0919",
        reason="退款审批通过",
        key="adj-refund-list-1",
    )
    assert created.status_code == 201, created.text
    forward = _create_adjustment(client, admin, credits=4, key="adj-forward-list-1")
    assert forward.status_code == 201, forward.text

    per_customer = client.get(_adjustment_path(CUSTOMER_USER_ID), headers=admin)
    assert per_customer.status_code == 200, per_customer.text
    items = {item["adjustment_id"]: item for item in per_customer.json()["items"]}
    assert per_customer.json()["total"] == 2
    reversal_item = items[created.json()["adjustment_id"]]
    assert reversal_item["credits"] == -6
    assert reversal_item["order_id"] == ""
    assert reversal_item["amount_fen"] == 0
    assert reversal_item["source_document_type"] == "REFUND_APPROVAL"
    assert reversal_item["source_document_ref"] == "REFUND-2026-0919"
    # The forward row keeps its order-derived values (regression on the COALESCE).
    forward_item = items[forward.json()["adjustment_id"]]
    assert forward_item["credits"] == 4
    assert forward_item["order_id"] == forward.json()["order_id"]
    assert forward_item["amount_fen"] > 0

    global_list = client.get("/api/control/adjustments", headers=admin)
    assert global_list.status_code == 200, global_list.text
    global_items = {item["adjustment_id"]: item for item in global_list.json()["items"]}
    assert global_list.json()["total"] == 2
    global_reversal = global_items[created.json()["adjustment_id"]]
    assert global_reversal["credits"] == -6
    assert global_reversal["order_id"] == ""
    assert global_reversal["admin_username"] == "admin_u"
    assert global_reversal["target_username"] == "customer_u"
    # Deterministic ledger balances still resolve for the order-less row:
    # the opening 50 → 44 after the reversal, and balance_before is 50.
    assert global_reversal["balance_after"] == 44
    assert global_reversal["balance_before"] == 50
    # Filtering by the new source type finds it.
    filtered = client.get(
        "/api/control/adjustments?source_document_type=REFUND_APPROVAL", headers=admin
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1


def test_reversal_beyond_available_balance_is_rejected_with_a_business_code(
    client: TestClient,
) -> None:
    """DoD 3（最关键的一条）D2：不允许负余额，超额退款是 400 而不是 500，也不静默截断."""
    admin = _admin_session(client)
    before_wallet = _wallet_balance(CUSTOMER_USER_ID)
    before_counts = _fetch_one(
        "SELECT (SELECT COUNT(*) FROM recharge_orders), "
        "(SELECT COUNT(*) FROM wallet_transactions), "
        "(SELECT COUNT(*) FROM admin_adjustments)"
    )

    # 50 available, ask for 51 back: the customer already consumed nothing here,
    # but the guard is the balance, not the consumption history.
    over = _create_adjustment(
        client,
        admin,
        credits=-51,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-2026-0920",
        reason="超额退款必须被拒",
        key="adj-refund-over-1",
    )
    assert over.status_code == 400, over.text
    detail = over.json()["detail"]
    assert detail["code"] == "ADJUSTMENT_BALANCE_INSUFFICIENT"
    # The operator must be told the consumed part is not refundable through the
    # ledger — never silently clamped to the maximum refundable amount.
    assert "unspent" in detail["message"]
    assert _wallet_balance(CUSTOMER_USER_ID) == before_wallet
    assert (
        _fetch_one(
            "SELECT (SELECT COUNT(*) FROM recharge_orders), "
            "(SELECT COUNT(*) FROM wallet_transactions), "
            "(SELECT COUNT(*) FROM admin_adjustments)"
        )
        == before_counts
    )

    # The boundary is inclusive: refunding exactly the whole balance lands at 0.
    exact = _create_adjustment(
        client,
        admin,
        credits=-50,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-2026-0920",
        reason="全额退未消耗部分",
        key="adj-refund-exact-1",
    )
    assert exact.status_code == 201, exact.text
    assert exact.json()["wallet_balance_after"] == 0

    # And one credit past it is refused again — the floor is zero, not "one more".
    floor = _create_adjustment(
        client,
        admin,
        credits=-1,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-2026-0920",
        reason="余额为 0 后不能再退",
        key="adj-refund-floor-1",
    )
    assert floor.status_code == 400, floor.text
    assert floor.json()["detail"]["code"] == "ADJUSTMENT_BALANCE_INSUFFICIENT"
    assert _wallet_balance(CUSTOMER_USER_ID) == (0, 0)

    # The DB CHECK remains the defence in depth for any path that bypasses the
    # route (red line: never UPDATE the balance directly, and never relax this).
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        with pytest.raises(CheckViolation):
            conn.execute(
                "UPDATE wallets SET available_credits = -1 WHERE user_id = %s",
                (CUSTOMER_USER_ID,),
            )


def test_reversal_rejects_zero_and_oversized_credits(client: TestClient) -> None:
    """反向调账要求非零整数；越界与溢出仍是 400，且在动账本之前就被拦下."""
    admin = _admin_session(client)
    before_wallet = _wallet_balance(CUSTOMER_USER_ID)
    for credits in (0, 2_147_483_648, -(2_147_483_648)):
        response = _create_adjustment(
            client,
            admin,
            credits=credits,
            source_document_type="REFUND_APPROVAL",
            source_document_ref="REFUND-BOUNDS",
            key=f"adj-refund-bounds-{credits}",
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    assert _wallet_balance(CUSTOMER_USER_ID) == before_wallet
    assert _ledger_rows(CUSTOMER_USER_ID, "REFUND") == []


def test_positive_refund_approval_still_uses_the_forward_charge_path(
    client: TestClient,
) -> None:
    """`REFUND_APPROVAL` 只是「允许负数」，不是「只能负数」：补记仍走正向 CHARGE."""
    admin = _admin_session(client)
    response = _create_adjustment(
        client,
        admin,
        credits=3,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-REVERSAL-2026",
        reason="撤回一笔退款：补记回客户账本",
        key="adj-refund-forward-1",
    )
    assert response.status_code == 201, response.text
    assert response.json()["order_id"] != ""
    order = _order_row(response.json()["order_id"])
    assert order[0] == "admin_adjustment"
    charges = _fetch_all(
        "SELECT id FROM wallet_transactions WHERE type = 'CHARGE' AND recharge_order_id = %s",
        (response.json()["order_id"],),
    )
    assert len(charges) == 1
    assert _ledger_rows(CUSTOMER_USER_ID, "REFUND") == []


@pytest.mark.parametrize(
    "source_document_type",
    ["CS_TICKET", "COMPENSATION_APPROVAL", "FREE_GRANT", "CREDIT_COMPENSATION"],
)
def test_non_reversal_sources_still_reject_negative_credits(
    client: TestClient, source_document_type: str
) -> None:
    """正向来源类型的负数是 400（零回归）：只有退款类来源能反向记账."""
    admin = _admin_session(client)
    before_wallet = _wallet_balance(CUSTOMER_USER_ID)
    response = _create_adjustment(
        client,
        admin,
        credits=-1,
        source_document_type=source_document_type,
        key=f"adj-forward-negative-{source_document_type}",
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    assert _wallet_balance(CUSTOMER_USER_ID) == before_wallet


def test_reversal_is_idempotent_and_auditor_cannot_write_it(client: TestClient) -> None:
    """四件套对反向调账同样成立：幂等重放不重复冲减；auditor 只读."""
    admin = _admin_session(client)
    first = _create_adjustment(
        client,
        admin,
        credits=-5,
        source_document_type="LEDGER_CORRECTION",
        source_document_ref="LEDGER-FIX-2026-1",
        reason="账本更正：重复计费冲回",
        key="adj-refund-replay-1",
    )
    assert first.status_code == 201, first.text
    after_first = _wallet_balance(CUSTOMER_USER_ID)

    replay = _create_adjustment(
        client,
        admin,
        credits=-5,
        source_document_type="LEDGER_CORRECTION",
        source_document_ref="LEDGER-FIX-2026-1",
        reason="账本更正：重复计费冲回",
        key="adj-refund-replay-1",
    )
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json()["adjustment_id"] == first.json()["adjustment_id"]
    assert _wallet_balance(CUSTOMER_USER_ID) == after_first
    assert len(_ledger_rows(CUSTOMER_USER_ID, "REFUND")) == 1

    # A second reversal must coexist: the UNIQUE index on recharge_order_id
    # survives the DROP NOT NULL and PostgreSQL treats NULLs as distinct, so
    # "one audit row per order" still holds while order-less rows accumulate.
    # (Run before the auditor login below — the shared client's cookie jar is
    # single-session, so signing in as the auditor replaces the admin session.)
    second = _create_adjustment(
        client,
        admin,
        credits=-4,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-2026-0922",
        reason="第二笔独立退款",
        key="adj-refund-replay-2",
    )
    assert second.status_code == 201, second.text
    assert second.json()["order_id"] == ""
    orderless = _fetch_all(
        "SELECT id FROM admin_adjustments WHERE recharge_order_id IS NULL AND target_user_id = %s",
        (CUSTOMER_USER_ID,),
    )
    assert {str(row[0]) for row in orderless} == {
        first.json()["adjustment_id"],
        second.json()["adjustment_id"],
    }
    after_second = _wallet_balance(CUSTOMER_USER_ID)

    auditor = _admin_session(client, actor="auditor_u")
    denied = _create_adjustment(
        client,
        auditor,
        credits=-5,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-2026-0921",
        key="adj-refund-auditor-1",
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUDITOR_READ_ONLY"
    assert _wallet_balance(CUSTOMER_USER_ID) == after_second
    assert len(_ledger_rows(CUSTOMER_USER_ID, "REFUND")) == 2


def test_reconcile_invariants_stay_green_after_a_refund(client: TestClient) -> None:
    """SOP §10 第 3 步：处置后跑 reconcile_customer_billing，差额为零。

    反向调账落一条负向 `REFUND` 后，钱包汇总、PAID 单↔CHARGE 配对与计费轮次三条
    不变量都必须保持成立——这正是「不放开负向调账，SOP 就无法闭环」那句判断的
    反面验收：账本侧能反向记账，差额才对得平。

    同时这也是 `reconcile_customer_billing` 的 PG_ONLY 清单**不需要同步**的证据：
    `REFUND` 不引入新表 / 新列，而三条账本不变量要么与方向无关（钱包汇总是
    求和），要么已被类型/轮次条件排除在 `REFUND` 之外（配对只看 `CHARGE`，
    轮次只看 `task_id`+`billing_round` 非空）。
    """
    from scripts.reconcile_customer_billing import validate_database_invariants

    def _invariants() -> tuple:
        with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
            return validate_database_invariants(conn, "postgresql", "target")

    # The seeded fixture is not invariant-clean and never claimed to be: the
    # 027 activation FK needs a PAID dummy order that has no CHARGE row, so the
    # baseline carries exactly that one paid_order_charge_mismatch. What this
    # test has to prove is that the refund adds **no** issue on top of it
    # (and in particular no wallet_balance_mismatch).
    baseline = _invariants()
    baseline_codes = sorted(f"{issue.code}:{issue.scope}" for issue in baseline)

    admin = _admin_session(client)
    before_zero = _create_adjustment(client, admin, credits=4, key="adj-reconcile-before")
    assert before_zero.status_code == 201, before_zero.text
    reversed_away = _create_adjustment(
        client,
        admin,
        credits=-9,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="REFUND-2026-0922",
        reason="对账前的一笔实际退款",
        key="adj-reconcile-refund",
    )
    assert reversed_away.status_code == 201, reversed_away.text

    after = _invariants()
    assert [f"{issue.code}:{issue.scope}" for issue in after] == baseline_codes, baseline_codes
    assert not any(issue.code == "wallet_balance_mismatch" for issue in after)

    # And the wallet/ledger identity the aggregate check is built on, spelled out:
    # the wallet must equal the signed sum of its own ledger rows.
    wallet_total, ledger_total = _fetch_one(
        "SELECT w.available_credits, COALESCE(SUM(wt.available_delta), 0) "
        "FROM wallets w LEFT JOIN wallet_transactions wt ON wt.user_id = w.user_id "
        "WHERE w.user_id = %s GROUP BY w.available_credits",
        (CUSTOMER_USER_ID,),
    )
    assert wallet_total == ledger_total


def test_adjustments_csv_exports_rows_with_same_filters(client: TestClient) -> None:
    """资金中心·人工调整导出：与 /adjustments 同筛选口径，走 control.export 审计。"""
    headers = _admin_session(client)
    created = _create_adjustment(
        client,
        headers,
        reason="资金中心导出：月度对账用",
        key=f"csv-{uuid.uuid4()}",
    )
    assert created.status_code == 201, created.text

    exported = client.get(
        "/api/control/adjustments.csv?target_username=customer_u", headers=headers
    )
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("text/csv")
    assert "customer_u" in exported.text
    assert "资金中心导出：月度对账用" in exported.text
    # 表头与列表字段同口径。
    assert exported.text.splitlines()[0].startswith("created_at,")

    # 筛选不匹配时只有表头，不泄漏其他客户行。
    empty = client.get("/api/control/adjustments.csv?target_username=nobody", headers=headers)
    assert empty.status_code == 200
    assert len(empty.text.splitlines()) == 1


def _suspend_headers(client: TestClient, key: str) -> dict[str, str]:
    headers = _admin_session(client)
    headers[IDEMPOTENCY_KEY_HEADER] = key
    return headers


def test_suspend_and_resume_customer_with_audit_and_session_revoke(
    client: TestClient,
) -> None:
    """方案 P1 客户管理：暂停吊销会话并写审计；恢复只翻回标记；幂等重放一致。"""
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        # 给目标客户一台在绑设备 + 一条在线会话：暂停必须在同一事务里把它吊销。
        conn.execute(
            """
            INSERT INTO customer_devices
                (id, activation_code_id, user_id, slot_no, display_name,
                 platform, status, bound_at, fingerprint_hmac,
                 fingerprint_key_version, token_digest, token_key_version)
            VALUES ('dev-suspend-1', NULL, %s, 1, '暂停用设备', 'windows',
                    'BOUND', now(), 'test-fingerprint', 1, 'test-token-digest', 1)
            ON CONFLICT (id) DO NOTHING
            """,
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            """
            INSERT INTO customer_session_state
                (session_id, user_id, device_id, session_epoch, lease_until,
                 last_heartbeat_at, token_digest)
            VALUES ('sess-suspend-1', %s, 'dev-suspend-1',
                    1, now() + interval '30 minutes', now(),
                    'sess-token-digest')
            ON CONFLICT (session_id) DO NOTHING
            """,
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO customer_devices(id,user_id,slot_no,display_name,platform,status,bound_at,"
            "fingerprint_hmac,fingerprint_key_version,token_digest,token_key_version) "
            "VALUES('dev-suspend-2',%s,2,'另一设备','windows','BOUND',now(),'fp-two',1,'token-two',1)",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO customer_session_state(session_id,user_id,device_id,session_epoch,"
            "lease_until,last_heartbeat_at,token_digest) VALUES"
            "('sess-suspend-2',%s,'dev-suspend-2',1,now()+interval '30 minutes',now(),'sess-two')",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO projects(id,owner_user_id,name) VALUES('pause-project',%s,'在途项目')",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO generation_batches(id,project_id,created_by_user_id,idempotency_key,"
            "request_hash,request_snapshot_json) "
            "VALUES('pause-batch','pause-project',%s,'pause','hash','{}')",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO generation_tasks(id,batch_id,provider,model,status) "
            "VALUES('pause-running','pause-batch','metaso','h3','RUNNING')"
        )
        wallet_before = conn.execute(
            "SELECT * FROM wallets WHERE user_id=%s", (CUSTOMER_USER_ID,)
        ).fetchone()
        ledger_before = conn.execute("SELECT count(*) FROM wallet_transactions").fetchone()
        conn.execute(
            "INSERT INTO users(id,username,display_name,role,account_type,parent_user_id) "
            "VALUES('paused-child','paused-child','暂停客户子账号','customer','SUB',%s)",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO customer_devices(id,user_id,slot_no,display_name,platform,status,bound_at,"
            "fingerprint_hmac,fingerprint_key_version,token_digest,token_key_version) "
            "VALUES('child-dev','paused-child',1,'子账号设备','windows','BOUND',now(),"
            "'child-fp',1,'child-token',1)"
        )
        conn.execute(
            "INSERT INTO customer_session_state(session_id,user_id,device_id,session_epoch,"
            "lease_until,last_heartbeat_at,token_digest) "
            "VALUES('child-session','paused-child','child-dev',1,"
            "now()+interval '30 minutes',now(),'child-session-token')"
        )

    headers = _suspend_headers(client, f"suspend-{uuid.uuid4()}")
    suspended = client.post(
        f"/api/control/customers/{CUSTOMER_USER_ID}/suspend",
        json={"confirm": True, "reason": "客户申请暂停使用"},
        headers=headers,
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["is_active"] == 0
    assert suspended.json()["session_revoked"] in (True, False)
    listed = client.get("/api/control/customers", headers=headers).json()["items"][0]
    assert listed["status"] == "SUSPENDED" and listed["account_active"] is False
    assert listed["activation_status"] == "ACTIVE"
    assert (
        client.get("/api/control/customers", headers=headers, params={"status": "active"}).json()[
            "total"
        ]
        == 0
    )
    exported = client.get(
        "/api/control/customers.csv", headers=headers, params={"status": "suspended"}
    )
    assert exported.status_code == 200, exported.text
    assert "customer_u" in exported.text and "SUSPENDED" in exported.text

    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        active = conn.execute(
            "SELECT is_active FROM users WHERE id = %s",
            (CUSTOMER_USER_ID,),
        ).fetchone()
        assert active == (0,)
        sessions = conn.execute(
            "SELECT session_epoch,lease_until::timestamptz<=clock_timestamp() "
            "FROM customer_session_state WHERE user_id=%s",
            (CUSTOMER_USER_ID,),
        ).fetchall()
        assert sessions == [(2, True), (2, True)]
        assert conn.execute(
            "SELECT session_epoch,lease_until::timestamptz<=clock_timestamp() "
            "FROM customer_session_state WHERE user_id='paused-child'"
        ).fetchone() == (2, True)
        assert (
            conn.execute("SELECT * FROM wallets WHERE user_id=%s", (CUSTOMER_USER_ID,)).fetchone()
            == wallet_before
        )
        assert conn.execute("SELECT count(*) FROM wallet_transactions").fetchone() == ledger_before
        assert conn.execute(
            "SELECT status FROM generation_tasks WHERE id='pause-running'"
        ).fetchone() == ("RUNNING",)
        audit_rows = conn.execute(
            "SELECT action FROM audit_logs WHERE action = 'customer.suspend' AND entity_id = %s",
            (CUSTOMER_USER_ID,),
        ).fetchall()
        assert len(audit_rows) == 1

    # 幂等重放：同一键返回同一结果，不写第二条审计。
    replay = client.post(
        f"/api/control/customers/{CUSTOMER_USER_ID}/suspend",
        json={"confirm": True, "reason": "客户申请暂停使用"},
        headers=headers,
    )
    assert replay.status_code == 200
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        audit_rows = conn.execute(
            "SELECT action FROM audit_logs WHERE action = 'customer.suspend' AND entity_id = %s",
            (CUSTOMER_USER_ID,),
        ).fetchall()
        assert len(audit_rows) == 1

    resume = client.post(
        f"/api/control/customers/{CUSTOMER_USER_ID}/resume",
        json={"confirm": True, "reason": "客户结清欠款，恢复使用"},
        headers=_suspend_headers(client, f"resume-{uuid.uuid4()}"),
    )
    assert resume.status_code == 200, resume.text
    assert resume.json()["is_active"] == 1
    resumed_list = client.get("/api/control/customers", headers=headers).json()["items"][0]
    assert resumed_list["status"] == "ACTIVE" and resumed_list["account_active"] is True
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        resumed = conn.execute(
            "SELECT is_active FROM users WHERE id = %s", (CUSTOMER_USER_ID,)
        ).fetchone()
        assert resumed == (1,)
        assert (
            conn.execute("SELECT * FROM wallets WHERE user_id=%s", (CUSTOMER_USER_ID,)).fetchone()
            == wallet_before
        )
        assert conn.execute("SELECT count(*) FROM wallet_transactions").fetchone() == ledger_before
        assert conn.execute(
            "SELECT count(*) FROM customer_session_state WHERE user_id=%s "
            "AND lease_until::timestamptz>clock_timestamp()",
            (CUSTOMER_USER_ID,),
        ).fetchone() == (0,)
        conn.execute(
            "DELETE FROM audit_logs WHERE action IN ('customer.suspend', "
            "'customer.resume') AND entity_id = %s",
            (CUSTOMER_USER_ID,),
        )
        conn.execute("UPDATE users SET is_active = 1 WHERE id = %s", (CUSTOMER_USER_ID,))
        # 设备/会话/事件行留在专用测试库：customer_session_events 是
        # append-only（触发器拒绝 DELETE），强清会让清理本身失败。
        conn.execute("DELETE FROM customer_session_state WHERE session_id = 'sess-suspend-1'")


def test_suspend_rejects_blank_reason_and_missing_user(client: TestClient) -> None:
    headers = _suspend_headers(client, f"blank-{uuid.uuid4()}")
    blank = client.post(
        f"/api/control/customers/{CUSTOMER_USER_ID}/suspend",
        json={"confirm": True, "reason": "   "},
        headers=headers,
    )
    assert blank.status_code == 400

    missing = client.post(
        "/api/control/customers/no-such-user/suspend",
        json={"confirm": True, "reason": "不存在"},
        headers=_suspend_headers(client, f"missing-{uuid.uuid4()}"),
    )
    assert missing.status_code == 404


def test_customer_attention_is_global_sorted_and_matches_export(client: TestClient) -> None:
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        for index in range(25):
            conn.execute(
                "INSERT INTO users(id,username,display_name,role,registration_source,created_at) "
                "VALUES (%s,%s,%s,'customer','activation_code',now()-interval '40 days')",
                (f"scope-{index}", f"scope-{index}", f"公司{index:02}"),
            )
            conn.execute(
                "INSERT INTO wallets(user_id,available_credits,reserved_credits) VALUES (%s,%s,0)",
                (f"scope-{index}", 49 if index == 24 else 50),
            )
        conn.execute(
            "INSERT INTO projects(id,owner_user_id,name) VALUES ('scope-p','scope-24','项目')"
        )
        conn.execute(
            "INSERT INTO generation_batches(id,project_id,created_by_user_id,idempotency_key,"
            "request_hash,request_snapshot_json) VALUES "
            "('scope-b','scope-p','scope-24','scope-key','hash','{}')"
        )
        conn.execute(
            "INSERT INTO generation_tasks(id,batch_id,provider,model,status,ar"
            "chive_status,created_at) "
            "VALUES ('scope-success','scope-b','metaso','h3','SUCCEEDED','ARCHIVED',now()),"
            "('scope-failed','scope-b','metaso','h3','FAILED','PENDING',now()),"
            "('scope-old','scope-b','metaso','h3','FAILED','PENDING',now()-interval '31 days')"
        )
    headers = _admin_session(client)
    params = {"username": "scope-", "low_balance_threshold": 50, "limit": 1}
    page = client.get("/api/control/customers", params=params, headers=headers)
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 25
    assert page.json()["attention_counts"] == {
        "low_balance": 1,
        "recent_failure": 1,
        "inactive": 24,
    }
    for attention, expected in [("low_balance", 1), ("recent_failure", 1), ("inactive", 24)]:
        filtered = client.get(
            "/api/control/customers", params={**params, "attention": attention}, headers=headers
        )
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["total"] == expected
        assert filtered.json()["attention_counts"] == page.json()["attention_counts"]
    target = client.get(
        "/api/control/customers",
        params={"user_id": "scope-24", "low_balance_threshold": 50},
        headers=headers,
    ).json()["items"][0]
    assert target["generation_total_30d"] == 2 and target["generation_failed_30d"] == 1
    assert target["success_rate_30d"] == 50 and target["low_balance"] is True
    target = client.get(
        "/api/control/customers",
        params={"user_id": "scope-23", "low_balance_threshold": 50},
        headers=headers,
    ).json()["items"][0]
    assert target["success_rate_30d"] is None and target["low_balance"] is False
    assert (
        client.get(
            "/api/control/customers", params={"attention": "low_balance"}, headers=headers
        ).status_code
        == 400
    )
    exported = client.get(
        "/api/control/customers.csv", params={**params, "attention": "low_balance"}, headers=headers
    )
    assert exported.status_code == 200, exported.text
    assert "scope-24" in exported.text and "scope-23" not in exported.text
    for sort in ["recharge", "month_consumed", "last_active"]:
        for direction in ["asc", "desc"]:
            ids = []
            for offset in [0, 10, 20]:
                rows = client.get(
                    "/api/control/customers",
                    headers=headers,
                    params={
                        **params,
                        "limit": 10,
                        "offset": offset,
                        "sort": sort,
                        "direction": direction,
                    },
                ).json()["items"]
                ids.extend(row["user_id"] for row in rows)
            assert len(ids) == len(set(ids)) == 25
            if sort == "last_active":
                assert ids[0 if direction == "desc" else -1] == "scope-24"


def test_customer_overview_has_thirty_days_and_five_mixed_events(client: TestClient) -> None:
    headers = _admin_session(client)
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE users SET role='customer', registration_source='activation_code' WHERE id=%s",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO projects(id,owner_user_id,name) VALUES ('timeline-p',%s,'趋势项目')",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO generation_batches(id,project_id,created_by_user_id,i"
            "dempotency_key,request_hash,request_snapshot_json) VALUES ('timel"
            "ine-b','timeline-p',%s,'timeline-key','hash','{}')",
            (CUSTOMER_USER_ID,),
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO generation_tasks(id,batch_id,provider,model,status,ar"
                "chive_status,created_at) VALUES (%s,'timeline-b','metaso','h3','S"
                "UCCEEDED','ARCHIVED',now()-interval '5 minutes')",
                (f"timeline-{i}",),
            )
        conn.execute(
            "INSERT INTO wallet_transactions(id,user_id,type,available_delta,r"
            "eserved_delta,task_id,billing_round,idempotency_key,created_at) V"
            "ALUES ('timeline-settle',%s,'SETTLE',0,-7,'timeline-0',1,'timelin"
            "e-settle',now()-interval '5 minutes')",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO customer_devices(id,user_id,slot_no,display_name,plat"
            "form,status,bound_at,fingerprint_hmac,fingerprint_key_version,tok"
            "en_digest,token_key_version) VALUES ('synthetic-device',%s,1,'隔离设"
            "备','windows','BOUND',now(),'synthetic-fingerprint',1,'synthetic-t"
            "oken',1)",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO customer_session_events(id,event,user_id,session_id,d"
            "evice_id,session_epoch,request_id,created_at) VALUES ('timeline-l"
            "ogin','LOGIN',%s,'synthetic-session','synthetic-device',1,'synthe"
            "tic-login',now()-interval '3 minutes')",
            (CUSTOMER_USER_ID,),
        )
    response = _create_adjustment(
        client,
        headers,
        credits=4,
        source_document_type="FREE_GRANT",
        source_document_ref="timeline-free",
        reason="合成时间线测试",
    )
    assert response.status_code == 201, response.text
    result = client.get(f"/api/control/customers/{CUSTOMER_USER_ID}/overview", headers=headers)
    assert result.status_code == 200, result.text
    payload = result.json()
    assert len(payload["daily_consumption"]) == 30
    assert sum(day["credits"] for day in payload["daily_consumption"]) == 7
    assert len(payload["timeline"]) == 5
    assert {"recharge", "adjustment", "login", "generation"} <= {
        event["kind"] for event in payload["timeline"]
    }
    assert len({(event["kind"], event["event_id"]) for event in payload["timeline"]}) == 5
    assert (
        client.get("/api/control/customers/no-such-user/overview", headers=headers).status_code
        == 404
    )
    auditor = _admin_session(client, "auditor_u")
    assert (
        client.get(
            f"/api/control/customers/{CUSTOMER_USER_ID}/overview", headers=auditor
        ).status_code
        == 200
    )


def test_customer_text_times_preserve_offsets(client: TestClient) -> None:
    from app.admin_customer_metrics import utc_text_timestamp

    del client
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute("SET TIME ZONE 'Asia/Shanghai'")
        rows = conn.execute(
            "SELECT " + utc_text_timestamp("value") + " FROM (VALUES ('2026-10-02T05:00:00'),"
            "('2026-10-02T13:00:00+08:00'),('2026-10-02T05:00:00Z')) times(value)"
        ).fetchall()
    assert rows[0][0] == rows[1][0] == rows[2][0]
    assert rows[0][0].timestamp() == 1790917200


def test_funds_compensation_offline_and_unverified_history_are_separate(client: TestClient) -> None:
    from app.admin_dashboard_routes import router

    client.app.include_router(router)
    headers = _admin_session(client)
    for source, credits in [("CS_TICKET", 10), ("COMPENSATION_APPROVAL", 2), ("FREE_GRANT", 3)]:
        result = _create_adjustment(
            client,
            headers,
            credits=credits,
            source_document_type=source,
            source_document_ref=f"synthetic-{source}",
        )
        assert result.status_code == 201, result.text
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        today = (
            conn.execute("SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date")
            .fetchone()[0]
            .isoformat()
        )
        conn.execute(
            "INSERT INTO recharge_orders(id,user_id,merchant_order_no,provider"
            ",status,pricing_scope,base_unit_price_fen_snapshot,charged_unit_p"
            "rice_fen_snapshot,min_recharge_fen_snapshot,recharge_step_fen_sna"
            "pshot,amount_fen,credits,paid_at) VALUES ('real-offline',%s,'synt"
            "hetic-offline','admin_adjustment','PAID','CUSTOMER_STANDARD',1000"
            ",1000,10000,1000,20000,20,now())",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO admin_adjustments(id,recharge_order_id,target_user_id"
            ",admin_user_id,source_document_type,source_document_ref,reason,re"
            "quest_id) VALUES ('real-offline','real-offline',%s,'admin_u','OFF"
            "LINE_PAYMENT','synthetic-offline-voucher','隔离线下凭证','synthetic-off"
            "line-request')",
            (CUSTOMER_USER_ID,),
        )
        conn.execute(
            "INSERT INTO recharge_orders(id,user_id,merchant_order_no,provider"
            ",status,pricing_scope,base_unit_price_fen_snapshot,charged_unit_p"
            "rice_fen_snapshot,min_recharge_fen_snapshot,recharge_step_fen_sna"
            "pshot,amount_fen,credits,paid_at) VALUES ('manual-history',%s,'sy"
            "nthetic-history','admin_adjustment','PAID','CUSTOMER_STANDARD',10"
            "00,1000,10000,1000,5000,5,now())",
            (CUSTOMER_USER_ID,),
        )
    report = client.get(
        "/api/control/funds/summary", headers=headers, params={"start": today, "end": today}
    )
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["recharge_fen"] == payload["offline_fen"] == 20000
    assert payload["grant_credits"] == 15
    assert payload["by_method"] == [{"method": "offline", "orders": 1, "amount_fen": 20000}]
    # Opening manual amounts carry no receipt evidence; keep separate, never infer offline.
    assert payload["unverified_manual_orders"] >= 1 and payload["unverified_manual_fen"] >= 5000
    business = client.get(
        "/api/control/business/overview", headers=headers, params={"start": today, "end": today}
    ).json()
    assert (
        business["metrics"]["recharge_fen"] == 20000
        and business["metrics"]["paying_customers"] == 1
    )
