"""T34 — admin audit log API tests (adapted to the 039/users model).

Tests cover:
- GET /api/control/audit-log — list audit events
- Pagination (limit/offset) and actor_user_id filtering
- Admin/auditor role access control

The fixture database is migrated with alembic upgrade head (the T23
precedent) so every query runs against the real revision-039 schema where
admin_adjustments.admin_user_id references users.id — there is no separate
``admin_users`` table.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

# Set HMAC key before importing app modules
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t34-audit-tests-minimum-48-bytes-long-1234567890",
)

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T34_DB_NAME = "t34_admin_audit_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)

AUDIT_PATH = "/api/control/audit-log"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t34_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T34_DB_NAME}"


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def audit_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T34_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T34_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t34_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t34_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T34_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(audit_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
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
            "('customer_u', 'customer_u', 'Customer User', 'user')"
        )
    yield audit_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_audit_routes import router as admin_audit_router
    from app.admin_auth_routes import router as admin_auth_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_audit_router)
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


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    """Exchange a real admin session cookie + CSRF header (the T12 pattern)."""
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _insert_adjustment(
    conn: psycopg.Connection,
    *,
    actor: str,
    target: str,
    source_type: str,
    reason: str,
    created_at: str | None = None,
) -> str:
    """Create one PAID adjustment-style order plus its audit row.

    ``admin_adjustments.recharge_order_id`` is unique (one audit row per
    order) so every row gets its own order. ``created_at`` pins the row's
    timestamp for ordering/range tests — the Python lane writes ISO ``T``
    text while the column default writes space-formatted timestamps, and the
    unified audit view must handle both (PR #85 review).
    """
    adjustment_id = str(uuid.uuid4())
    order_id = f"audit-order-{adjustment_id}"
    conn.execute(
        "INSERT INTO recharge_orders "
        "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
        " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        " min_recharge_fen_snapshot, recharge_step_fen_snapshot, "
        " amount_fen, credits, paid_at) "
        "VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', 'CUSTOMER_STANDARD', "
        "1000, 1000, 10000, 1000, 1000, 1, now())",
        (order_id, target, f"AUDIT-{adjustment_id}"),
    )
    columns = (
        "id, recharge_order_id, target_user_id, admin_user_id, "
        "source_document_type, source_document_ref, reason, request_id"
    )
    values: tuple[object, ...] = (
        adjustment_id,
        order_id,
        target,
        actor,
        source_type,
        f"REF-{uuid.uuid4()}",
        reason,
        str(uuid.uuid4()),
    )
    if created_at is not None:
        columns += ", created_at"
        values += (created_at,)
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(
        f"INSERT INTO admin_adjustments ({columns}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return adjustment_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_list_audit_log_requires_admin(client: TestClient):
    """Unauthenticated requests must be rejected with 401."""
    response = client.get(AUDIT_PATH)
    assert response.status_code == 401


@pytest.mark.pg
def test_list_audit_log_returns_empty(client: TestClient, route_state: str):
    """An empty audit log should return an empty list."""
    _admin_session(client)
    response = client.get(AUDIT_PATH, params={"event_type": "ADMIN_ADJUSTMENT"})
    assert response.status_code == 200
    data = response.json()
    assert [i for i in data["items"] if i["event_type"] == "ADMIN_ADJUSTMENT"] == []
    assert data["total"] == 0


@pytest.mark.pg
def test_list_audit_log_returns_adjustments(client: TestClient, route_state: str):
    """Should return admin adjustments as audit events with the real actor."""
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="CS_TICKET",
            reason="客户补偿：拆解失败两次",
        )
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="REFUND_APPROVAL",
            reason="退款",
        )

    response = client.get(AUDIT_PATH, params={"event_type": "ADMIN_ADJUSTMENT"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # ORDER BY created_at DESC, id — created_at comes from the transaction
    # clock and the tie-break is the random row id, so assert as a set.
    assert {item["event_type"] for item in data["items"]} == {"ADMIN_ADJUSTMENT"}
    assert {item["actor_username"] for item in data["items"]} == {"admin_u"}
    assert {item["target_user_id"] for item in data["items"]} == {"customer_u"}
    assert {item["target_username"] for item in data["items"]} == {"customer_u"}
    assert {item["reason"] for item in data["items"]} == {
        "客户补偿：拆解失败两次",
        "退款",
    }

    by_username = client.get(
        AUDIT_PATH,
        params={"actor_username": "admin", "target_username": "customer"},
    )
    assert by_username.status_code == 200, by_username.text
    assert by_username.json()["total"] == 2


@pytest.mark.pg
def test_list_audit_log_filters_by_actor_and_paginates(client: TestClient, route_state: str):
    """actor_user_id filter and limit/offset pagination."""
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_adjustment(
            conn, actor="admin_u", target="customer_u", source_type="CS_TICKET", reason="r1"
        )
        _insert_adjustment(
            conn, actor="admin_u", target="customer_u", source_type="CS_TICKET", reason="r2"
        )
        _insert_adjustment(
            conn, actor="auditor_u", target="customer_u", source_type="CS_TICKET", reason="r3"
        )

    filtered = client.get(
        AUDIT_PATH,
        params={"event_type": "ADMIN_ADJUSTMENT", "actor_user_id": "admin_u"},
    )
    assert filtered.status_code == 200
    filtered_data = filtered.json()
    assert filtered_data["total"] == 2

    page = client.get(
        AUDIT_PATH,
        params={"event_type": "ADMIN_ADJUSTMENT", "limit": 1, "offset": 1},
    )
    assert page.status_code == 200
    page_data = page.json()
    assert page_data["total"] == 3
    assert len(page_data["items"]) == 1
    assert page_data["limit"] == 1
    assert page_data["offset"] == 1


@pytest.mark.pg
def test_auditor_can_list_audit_log(client: TestClient, route_state: str):
    """Auditors should be able to list audit events (read-only)."""
    _admin_session(client, "auditor_u")
    response = client.get(AUDIT_PATH)
    assert response.status_code == 200


@pytest.mark.pg
def test_list_audit_log_event_type_filters_exact(client: TestClient, route_state: str):
    """event_type is an exact-match filter on the unified trail (A10).

    The old single-table 400 guard is gone: the filter is genuinely applied to
    the UNION, so an unknown value answers an empty (well-formed) page instead
    of an error, and a known value narrows across every audited surface.
    """
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="CS_TICKET",
            reason="类型筛选",
        )

    unknown = client.get(AUDIT_PATH, params={"event_type": "NOT_A_TYPE"})
    assert unknown.status_code == 200
    assert unknown.json()["total"] == 0

    seeded = client.get(AUDIT_PATH, params={"event_type": "ADMIN_ADJUSTMENT"})
    assert seeded.status_code == 200
    assert seeded.json()["total"] >= 1
    assert {item["event_type"] for item in seeded.json()["items"]} == {"ADMIN_ADJUSTMENT"}


def _insert_device_event(
    conn: psycopg.Connection,
    *,
    actor: str,
    target: str,
) -> str:
    """Seed one admin device event (needs a batch + code + device chain)."""
    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        " quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES ('audit-batch', 'audit-batch', 1000, 1000, 1, 1, "
        "'2099-01-01T00:00:00+00:00', 'OPEN', 'admin_u') "
        "ON CONFLICT (id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO activation_codes "
        "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
        " issued_at, bound_user_id, activated_at) "
        "VALUES ('audit-code', 'audit-batch', 'digest-audit', 1, 'XS04-****A', "
        "'ACTIVE', '2026-01-01T00:00:00+00:00', 'customer_u', "
        "'2026-01-01T00:00:00+00:00') "
        "ON CONFLICT (id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO customer_devices "
        "(id, activation_code_id, user_id, slot_no, display_name, platform, "
        " fingerprint_hmac, fingerprint_key_version, token_digest, "
        " token_key_version, status, bound_at) "
        "VALUES ('audit-device', 'audit-code', %s, 1, '审计设备', 'windows', "
        "'audit-fp', 1, 'audit-token', 1, 'BOUND', "
        "'2026-01-01T00:00:00+00:00') "
        "ON CONFLICT (id) DO NOTHING",
        (target,),
    )
    event_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO admin_device_events "
        "(id, event, admin_user_id, target_user_id, device_id, "
        " activation_code_id, reason, request_id) "
        "VALUES (%s, 'DEVICE_ADMIN_UNBOUND', %s, %s, 'audit-device', "
        "'audit-code', %s, %s)",
        (event_id, actor, target, "运维解绑", str(uuid.uuid4())),
    )
    conn.commit()
    return event_id


def _insert_session_event(
    conn: psycopg.Connection,
    *,
    event: str,
    actor: str | None,
    owner: str,
    session_id: str = "audit-session",
) -> str:
    """Seed one ``customer_session_events`` row.

    ``actor`` is the acting user id: an administrator for a forced revoke, the
    session owner for a self-logout, or ``None`` for a system sweep (TIMEOUT).

    Reuses ``_insert_device_event``'s batch/code/device chain rather than
    minting a second one: ``activation_codes`` carries a "one current code per
    bound user" unique constraint, so two chains for the same owner collide.
    The ``ON CONFLICT (id) DO NOTHING`` inserts make the two helpers
    order-independent.
    """
    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        " quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES ('audit-batch', 'audit-batch', 1000, 1000, 1, 1, "
        "'2099-01-01T00:00:00+00:00', 'OPEN', 'admin_u') "
        "ON CONFLICT (id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO activation_codes "
        "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
        " issued_at, bound_user_id, activated_at) "
        "VALUES ('audit-code', 'audit-batch', 'digest-audit', 1, 'XS04-****A', "
        "'ACTIVE', '2026-01-01T00:00:00+00:00', %s, "
        "'2026-01-01T00:00:00+00:00') "
        "ON CONFLICT (id) DO NOTHING",
        (owner,),
    )
    conn.execute(
        "INSERT INTO customer_devices "
        "(id, activation_code_id, user_id, slot_no, display_name, platform, "
        " fingerprint_hmac, fingerprint_key_version, token_digest, "
        " token_key_version, status, bound_at) "
        "VALUES ('audit-device', 'audit-code', %s, 1, '审计设备', 'windows', "
        "'audit-fp', 1, 'audit-token', 1, 'BOUND', "
        "'2026-01-01T00:00:00+00:00') "
        "ON CONFLICT (id) DO NOTHING",
        (owner,),
    )
    event_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO customer_session_events "
        "(id, event, user_id, activation_code_id, device_id, session_id, "
        " session_epoch, actor_user_id, reason, request_id) "
        "VALUES (%s, %s, %s, 'audit-code', 'audit-device', %s, 3, %s, %s, %s)",
        (
            event_id,
            event,
            owner,
            session_id,
            actor,
            "客服确认账号异常",
            str(uuid.uuid4()),
        ),
    )
    conn.commit()
    return event_id


@pytest.mark.pg
def test_list_audit_log_unions_all_audited_surfaces(client: TestClient, route_state: str):
    """A10：一个查询看到调账、设备操作与通用审计行，不再只看一类."""
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="CS_TICKET",
            reason="客户补偿",
        )
        _insert_device_event(conn, actor="admin_u", target="customer_u")
        _insert_session_event(
            conn,
            event="LOGOUT",
            actor="admin_u",
            owner="customer_u",
            session_id="audit-session-union",
        )
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, 'admin_u', 'runtime_settings.update', "
            "'runtime_settings', '1', %s)",
            (
                str(uuid.uuid4()),
                '{"reason": "灰度切换", "request_id": "req-rt-1"}',
            ),
        )
        conn.commit()

    response = client.get(AUDIT_PATH)
    assert response.status_code == 200
    data = response.json()
    types = {item["event_type"] for item in data["items"]}
    assert "ADMIN_ADJUSTMENT" in types
    assert "ADMIN_DEVICE_DEVICE_ADMIN_UNBOUND" in types
    assert "ADMIN_SESSION_LOGOUT" in types
    assert "runtime_settings.update" in types

    # 统一形状：每行都有操作者与 request id（可空的为空串）。
    for item in data["items"]:
        assert set(item) >= {
            "event_id",
            "event_type",
            "actor_user_id",
            "actor_username",
            "target_user_id",
            "reason",
            "request_id",
            "created_at",
        }


@pytest.mark.pg
def test_default_audit_view_excludes_customer_workspace_actions(
    client: TestClient, route_state: str
):
    """P0-3：客户在工作台的日常动作也写 audit_logs，默认视图只给管理员操作."""
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE users SET role = 'customer' WHERE id = 'customer_u'",
        )
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, 'admin_u', 'provider_settings.secret_reveal', "
            "'provider_settings', 'video', %s), "
            "(%s, 'customer_u', 'project.create', 'project', 'proj-1', '{}'), "
            "(%s, 'customer_u', 'asset.download_url.create', 'asset', 'asset-1', '{}')",
            (
                str(uuid.uuid4()),
                '{"reason": "排查连接失败", "request_id": "req-reveal-1"}',
                str(uuid.uuid4()),
                str(uuid.uuid4()),
            ),
        )

    default_view = client.get(AUDIT_PATH).json()
    default_types = {item["event_type"] for item in default_view["items"]}
    assert "provider_settings.secret_reveal" in default_types
    assert "project.create" not in default_types
    assert "asset.download_url.create" not in default_types
    assert default_view["total"] == len(default_view["items"])

    customer_view = client.get(
        AUDIT_PATH, params={"scope": "customer", "target_user_id": "customer_u"}
    ).json()
    assert {item["event_type"] for item in customer_view["items"]} == {
        "project.create",
        "asset.download_url.create",
    }

    everything = client.get(AUDIT_PATH, params={"scope": "all"}).json()
    assert everything["total"] == default_view["total"] + customer_view["total"]
    assert client.get(AUDIT_PATH, params={"scope": "nobody"}).status_code == 422


@pytest.mark.pg
def test_session_audit_shows_admin_revokes_and_hides_customer_traffic(
    client: TestClient, route_state: str
):
    """会话审计只收管理员动作：客户自注销与心跳不得进审计日志.

    ``customer_session_events`` 同时记录客户自己的会话流量——每次续租写一行
    HEARTBEAT。全量并入会让"查审计"淹没在心跳里，因此 UNION 分支只保留
    actor 非会话属主的行，也就是管理员强制下线。判别的三条边界一次钉住：
    管理员动作进入、自注销排除（actor == owner）、系统清扫排除（actor 为空）。
    """
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_session_event(
            conn,
            event="LOGOUT",
            actor="admin_u",
            owner="customer_u",
            session_id="sess-forced",
        )
        _insert_session_event(
            conn,
            event="LOGOUT",
            actor="customer_u",
            owner="customer_u",
            session_id="sess-self",
        )
        _insert_session_event(
            conn,
            event="HEARTBEAT",
            actor=None,
            owner="customer_u",
            session_id="sess-self",
        )
        _insert_session_event(
            conn,
            event="TIMEOUT",
            actor=None,
            owner="customer_u",
            session_id="sess-self",
        )
        conn.commit()

    response = client.get(AUDIT_PATH, params={"event_type": "ADMIN_SESSION_LOGOUT"})
    assert response.status_code == 200
    items = response.json()["items"]

    assert {item["source_document_ref"] for item in items} == {"sess-forced"}
    forced = items[0]
    assert forced["actor_user_id"] == "admin_u"
    assert forced["actor_username"] == "admin_u"
    assert forced["target_user_id"] == "customer_u"
    assert forced["target_username"] == "customer_u"
    assert forced["source_document_type"] == "CUSTOMER_SESSION"
    assert forced["reason"] == "客服确认账号异常"
    assert forced["request_id"] != ""


@pytest.mark.pg
@pytest.mark.parametrize("action", ["customer_unit_price.update", "customer_unit_price.reset"])
def test_customer_price_audit_events_are_filterable_by_customer(
    client: TestClient, route_state: str, action: str
) -> None:
    _admin_session(client)
    event_id = str(uuid.uuid4())
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, 'admin_u', %s, 'customer_unit_price', 'customer_u', '{}')",
            (event_id, action),
        )

    listed = client.get(AUDIT_PATH)
    assert listed.status_code == 200, listed.text
    event = next(item for item in listed.json()["items"] if item["event_id"] == event_id)
    assert event["target_user_id"] == "customer_u"
    assert event["event_type"] == action

    filtered = client.get(AUDIT_PATH, params={"target_user_id": "customer_u"})
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert [item["event_id"] for item in filtered.json()["items"]] == [event_id]

    other_customer = client.get(AUDIT_PATH, params={"target_user_id": "nobody_u"})
    assert other_customer.status_code == 200, other_customer.text
    assert other_customer.json()["total"] == 0
    assert other_customer.json()["items"] == []


@pytest.mark.pg
def test_rate_change_fields_are_whitelisted_without_exposing_other_metadata(
    client: TestClient, route_state: str
) -> None:
    _admin_session(client)
    event_id = str(uuid.uuid4())
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, 'admin_u', 'operation_rate.update', "
            "'operation_cost_rate', 'video_generation_768p', %s)",
            (
                event_id,
                '{"subject":"video_generation_768p","old_unit_price_fen":9,'
                '"new_unit_price_fen":12,"reason":"供应商调价",'
                '"request_id":"req-rate-change","api_key":"fake-secret-must-not-leak"}',
            ),
        )

    response = client.get(AUDIT_PATH, params={"event_type": "operation_rate.update"})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["change_subject"] == "video_generation_768p"
    assert item["old_unit_price_fen"] == 9
    assert item["new_unit_price_fen"] == 12
    assert "fake-secret-must-not-leak" not in response.text
    assert "api_key" not in response.text


@pytest.mark.pg
def test_billing_tariff_update_exposes_subject_and_derived_change_detail(
    client: TestClient, route_state: str
) -> None:
    """BILLING-OBS P1-2：费率审计行要能被审计页读懂，且不透出未白名单字段。"""
    _admin_session(client)
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, 'admin_u', 'billing.tariff.update', 'billing_tariff', 'oral', %s)",
            (
                first_id,
                '{"old":null,"new":{"enabled":true,"unit_credits":"0.25",'
                '"unit_cost_fen":"0.000125","unit_rounding":"exact","version":1},'
                '"reason":"首次配置","request_id":"req-tariff-1",'
                '"internal_note":"must-not-leak"}',
            ),
        )
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, 'admin_u', 'billing.tariff.update', 'billing_tariff', 'oral', %s)",
            (
                second_id,
                '{"old":{"enabled":true,"unit_credits":"0.25",'
                '"unit_cost_fen":"0.000125","unit_rounding":"exact","version":1},'
                '"new":{"enabled":true,"unit_credits":"0.5",'
                '"unit_cost_fen":"0.00025","unit_rounding":"exact","version":2},'
                '"reason":"供应商调价","request_id":"req-tariff-2"}',
            ),
        )

    response = client.get(AUDIT_PATH, params={"event_type": "billing.tariff.update"})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    items = {item["event_id"]: item for item in response.json()["items"]}
    created = items[first_id]
    assert created["change_subject"] == "oral"
    assert created["change_detail"]["old"] is None
    assert created["change_detail"]["new"]["unit_credits"] == "0.25"
    assert created["change_detail"]["new"]["unit_cost_fen"] == "0.000125"
    modified = items[second_id]
    assert modified["change_subject"] == "oral"
    assert modified["change_detail"]["old"]["unit_credits"] == "0.25"
    assert modified["change_detail"]["new"]["unit_credits"] == "0.5"
    assert modified["change_detail"]["new"]["unit_cost_fen"] == "0.00025"
    assert "must-not-leak" not in response.text
    assert "internal_note" not in response.text


@pytest.mark.pg
def test_historical_price_event_without_old_value_keeps_it_unknown(
    client: TestClient, route_state: str
) -> None:
    _admin_session(client)
    event_id = str(uuid.uuid4())
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, 'admin_u', 'customer_unit_price.update', "
            "'customer_unit_price', 'customer_u', %s)",
            (event_id, '{"new_unit_price_fen":15,"reason":"历史导入"}'),
        )

    response = client.get(AUDIT_PATH, params={"event_type": "customer_unit_price.update"})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["change_subject"] == "customer_unit_price"
    assert item["old_unit_price_fen"] is None
    assert item["new_unit_price_fen"] == 15


@pytest.mark.pg
def test_list_audit_log_filters_by_target_and_time(client: TestClient, route_state: str):
    """target_user_id 与时间范围筛选（A10）。"""
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="CS_TICKET",
            reason="目标筛选",
        )

    by_target = client.get(AUDIT_PATH, params={"target_user_id": "customer_u"})
    assert by_target.status_code == 200
    assert by_target.json()["total"] >= 1

    nobody = client.get(AUDIT_PATH, params={"target_user_id": "nobody_u"})
    assert nobody.status_code == 200
    assert nobody.json()["total"] == 0

    ranged = client.get(
        AUDIT_PATH,
        params={"created_from": "2026-01-01", "created_to": "2099-01-01"},
    )
    assert ranged.status_code == 200
    assert ranged.json()["total"] >= 1

    past_only = client.get(
        AUDIT_PATH,
        params={"created_to": "2000-01-01"},
    )
    assert past_only.status_code == 200
    assert past_only.json()["total"] == 0


@pytest.mark.pg
def test_list_audit_log_orders_mixed_timestamp_formats(client: TestClient, route_state: str):
    """PR #85 评审 P2：统一视图按真实时间排序，不被文本格式分组.

    admin_adjustments 由 Python 写入 ISO ``T`` 文本，audit_logs 等表走
    ``CURRENT_TIMESTAMP`` 空格默认；同日事件若按文本排序会先按格式再按
    时间分组，跨页错位。种子：10:00 的调整行（T 格式）+ 15:00 的通用
    审计行（空格格式）——真实时间降序必须 15:00 在前，文本降序则相反。
    """
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json, "
            " created_at) "
            "VALUES (%s, 'admin_u', 'audit.space.late', 'runtime_settings', '1', "
            "'{}', '2026-09-30 15:00:00+00')",
            (str(uuid.uuid4()),),
        )
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="CS_TICKET",
            reason="排序验证",
            created_at="2026-09-30T10:00:00+00:00",
        )

    response = client.get(AUDIT_PATH)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    types = [item["event_type"] for item in items]
    assert "audit.space.late" in types
    assert "ADMIN_ADJUSTMENT" in types
    assert types.index("audit.space.late") < types.index("ADMIN_ADJUSTMENT")


@pytest.mark.pg
def test_list_audit_log_created_to_includes_whole_day(client: TestClient, route_state: str):
    """PR #85 评审 P2：date-only created_to 表示"含当天整天".

    UI 日期输入给的是 ``2026-09-30`` 这种 date-only 值；按当天零点比较
    会漏掉当天 15:20 的事件。归一为当天最后一微秒（含）。
    """
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="CS_TICKET",
            reason="整天包含验证",
            created_at="2026-09-30T15:20:00+00:00",
        )

    # event_type 过滤掉会话登录自身的审计行，只数种子行。
    same_day = client.get(
        AUDIT_PATH,
        params={"created_to": "2026-09-30", "event_type": "ADMIN_ADJUSTMENT"},
    )
    assert same_day.status_code == 200, same_day.text
    assert same_day.json()["total"] == 1

    day_before = client.get(
        AUDIT_PATH,
        params={"created_to": "2026-09-29", "event_type": "ADMIN_ADJUSTMENT"},
    )
    assert day_before.status_code == 200
    assert day_before.json()["total"] == 0

    next_day = client.get(
        AUDIT_PATH,
        params={"created_to": "2026-10-01", "event_type": "ADMIN_ADJUSTMENT"},
    )
    assert next_day.status_code == 200
    assert next_day.json()["total"] == 1
