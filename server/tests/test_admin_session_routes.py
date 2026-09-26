"""T34 — admin customer session API tests (adapted to the 029 model).

Tests cover:
- GET /api/control/customers/{user_id}/sessions — list the live session state
- Pagination (limit/offset) and device-status filtering
- Admin/auditor role access control

The fixture database is migrated with alembic upgrade head (the T23
precedent): the 029 model keeps exactly one live session row per user in
``customer_session_state`` joined to the bound ``customer_devices`` row.
"""

from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Iterator
from pathlib import Path

# Set HMAC key before importing app modules
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t34-session-tests-minimum-48-bytes-long-1234567890",
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

T34_DB_NAME = "t34_admin_sessions_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)

FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"
SESSIONS_PATH = "/api/control/customers/{user_id}/sessions"


@pytest.mark.pg
def test_single_archive_is_durable_idempotent_and_does_not_recollect(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db_portable import BusinessConnection
    from app.storage import FakeStorageAdapter
    from app.viral_collection import run_viral_collection
    from app.viral_refresh import acquire_viral_refresh_task, complete_viral_refresh_task

    headers = _admin_session(client)
    path = "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/archive"
    body = {"confirm": True, "reason": "修复单条归档并保留列表"}
    with psycopg.connect(route_state) as conn:
        conn.execute("DELETE FROM viral_refresh_tasks")
        conn.execute("UPDATE viral_runtime_controls SET collection_enabled=1,keywords_json='[]'")
        conn.execute("UPDATE viral_videos SET collection_published=1")
    first = client.post(path, headers={**headers, "Idempotency-Key": "archive-one"}, json=body)
    assert first.status_code == 202, first.text
    replay = client.post(path, headers={**headers, "Idempotency-Key": "archive-one"}, json=body)
    assert replay.status_code == 202
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert (
        client.get("/api/control/viral/videos", headers=headers).json()["items"][0][
            "archive_status"
        ]
        == "PENDING"
    )
    calls = []
    monkeypatch.setattr(
        "app.viral_collection.viral_source_client_from_settings", lambda conn: object()
    )
    monkeypatch.setattr("app.viral_collection.CoverEnricher.enrich", lambda self, video: video)
    monkeypatch.setattr(
        "app.viral_collection.ViralMediaPipeline.fetch",
        lambda self, video, **kwargs: calls.append(video.video_id),
    )
    with psycopg.connect(route_state) as raw:
        lease = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="archive-test"
        )
    assert lease is not None
    run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="test"))
    with psycopg.connect(route_state) as raw:
        complete_viral_refresh_task(BusinessConnection.postgres(raw), lease=lease)
        assert raw.execute("SELECT collection_published FROM viral_videos").fetchone()[0] == 1
        assert raw.execute("SELECT count(*) FROM viral_collection_batches").fetchone()[0] == 0
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_video.archive'"
            ).fetchone()[0]
            == 1
        )
    assert calls == ["admin-video/opaque=id"]


@pytest.mark.pg
@pytest.mark.parametrize("blocked", ["paused", "deleted", "busy", "auditor"])
def test_single_archive_rejection_preserves_queue_and_wallet(
    client: TestClient, route_state: str, blocked: str
) -> None:
    headers = _admin_session(client, "auditor_u" if blocked == "auditor" else "admin_u")
    with psycopg.connect(route_state) as conn:
        if blocked == "paused":
            conn.execute("UPDATE viral_runtime_controls SET collection_enabled=0")
        elif blocked == "deleted":
            conn.execute("UPDATE viral_videos SET deleted_at=CURRENT_TIMESTAMP")
        elif blocked == "busy":
            conn.execute(
                "INSERT INTO viral_refresh_tasks(id,platform,sort,status,collection_config_json) "
                "VALUES('existing-collection','douyin','hot','PENDING','{\"keywords\":[]}')"
            )
        before = conn.execute(
            "SELECT id,status,collection_config_json FROM viral_refresh_tasks ORDER BY id"
        ).fetchall()
        wallet_before = conn.execute("SELECT * FROM wallets ORDER BY user_id").fetchall()
    result = client.post(
        "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/archive",
        headers={**headers, "Idempotency-Key": f"archive-blocked-{blocked}"},
        json={"confirm": True, "reason": "验证拒绝路径无业务副作用"},
    )
    assert (
        result.status_code == {"paused": 409, "deleted": 404, "busy": 409, "auditor": 403}[blocked]
    )
    assert (
        result.json()["detail"]["code"]
        == {
            "paused": "VIRAL_COLLECTION_PAUSED",
            "deleted": "VIRAL_VIDEO_NOT_FOUND",
            "busy": "VIRAL_ARCHIVE_BUSY",
            "auditor": "AUDITOR_READ_ONLY",
        }[blocked]
    )
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT id,status,collection_config_json FROM viral_refresh_tasks ORDER BY id"
            ).fetchall()
            == before
        )
        assert conn.execute("SELECT * FROM wallets ORDER BY user_id").fetchall() == wallet_before


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
def sessions_dsn() -> Iterator[str]:
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


def _seed_customer_with_session(conn: psycopg.Connection) -> None:
    """Activation chain (batch -> code -> activation) + device + live session."""
    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        " quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES ('batch-cu', 'batch-cu', 1500, 1000, 100, 1, "
        f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
    )
    conn.execute(
        "INSERT INTO recharge_orders "
        "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
        " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
        "VALUES ('dummy-activation-order', 'customer_u', 'DUMMY-activation-order', "
        "'admin_adjustment', 'PAID', 'CUSTOMER_STANDARD', "
        "1000, 1000, 10000, 1000, 1000, 1, now())"
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
        "VALUES ('act-cu', 'code-cu', 'customer_u', NULL, 'dummy-activation-order')"
    )
    conn.execute(
        "INSERT INTO customer_devices "
        "(id, activation_code_id, user_id, slot_no, display_name, platform, "
        " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version, "
        " status, bound_at) "
        "VALUES ('device-1', 'code-cu', 'customer_u', 1, '测试设备', 'windows', "
        "'fp-hmac-1', 1, 'tok-digest-1', 1, 'BOUND', '2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO customer_session_state "
        "(user_id, activation_code_id, device_id, session_id, token_digest, "
        " session_epoch, lease_until) "
        "VALUES ('customer_u', 'code-cu', 'device-1', 'session-1', 'session-tok-digest-1', "
        "3, '2099-06-01T00:00:00+00:00')"
    )
    conn.commit()


@pytest.fixture()
def route_state(sessions_dsn: str) -> Iterator[str]:
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
            "viral_refresh_tasks, viral_media_preparations, viral_import_tasks, "
            "viral_video_favorites, "
            "viral_video_visibility, "
            "viral_videos, "
            "viral_search_discoveries, "
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
        conn.execute(
            "INSERT INTO viral_runtime_controls (id, collection_enabled, import_enabled) "
            "VALUES (1, 1, 1) ON CONFLICT (id) DO UPDATE SET "
            "collection_enabled = 1, import_enabled = 1, updated_by_user_id = NULL"
        )
        conn.execute(
            "INSERT INTO viral_videos (platform, video_id, title, published_at) "
            "VALUES ('douyin', 'admin-video/opaque=id', '后台下架测试', 1788700000)"
        )
        _seed_customer_with_session(conn)
    yield sessions_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_runtime_routes import router as admin_runtime_router
    from app.admin_session_routes import router as admin_session_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_session_router)
    app.include_router(admin_runtime_router)
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


@pytest.mark.parametrize("result", ["complete", "partial", "failure"])
def test_admin_refreshes_wechat_statistics_once_without_customer_charge(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    from types import SimpleNamespace

    from app import viral_tikhub

    calls = []

    class Source:
        def wechat_video_detail(self, **kwargs):
            calls.append(kwargs)
            if result == "failure":
                raise RuntimeError("source unavailable")
            return SimpleNamespace(
                object_id="15003884913433053492",
                like_count=276,
                comment_count=3,
                forward_count=798 if result == "complete" else None,
                fav_count=279 if result == "complete" else None,
                description=None,
            )

    monkeypatch.setattr(viral_tikhub, "viral_source_client_from_settings", lambda conn: Source())
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title,likes,native_json) "
            "VALUES('wechat_channels','opaque/video=id','互动测试',180,"
            '\'{"export_id":"test-export"}\')'
        )
    path = "/api/control/viral/videos/wechat_channels/opaque%2Fvideo%3Did/statistics"
    headers = _admin_session(client)
    body = {"reason": "补齐视频号互动字段", "confirm": True}
    # Merely reading the list must never issue a billable provider request.
    assert client.get("/api/control/viral/videos", headers=headers).status_code == 200
    assert not calls
    for key in ("refresh-statistics", "refresh-statistics", "refresh-again"):
        response = client.post(path, headers={**headers, "Idempotency-Key": key}, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["statistics_status"] == result
    assert len(calls) == 1
    with psycopg.connect(route_state) as conn:
        row = conn.execute(
            "SELECT likes,comments,shares,collects FROM viral_videos "
            "WHERE platform='wechat_channels'"
        ).fetchone()
        assert row == (
            (276, 3, 798, 279)
            if result == "complete"
            else (276, 3, None, None)
            if result == "partial"
            else (180, None, None, None)
        )
        assert conn.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_video.statistics'"
            ).fetchone()[0]
            == 2
        )
    if result == "complete":
        with psycopg.connect(route_state) as conn:
            conn.execute(
                "UPDATE viral_videos SET native_json=(native_json::jsonb "
                '|| \'{"export_id":"","_statistics_checked_at":"2000-01-01T00:00:00Z"}\')::text '
                "WHERE platform='wechat_channels'"
            )
        response = client.post(
            path, headers={**headers, "Idempotency-Key": "refresh-stable-id"}, json=body
        )
        assert response.status_code == 200
        assert calls[-1] == {"object_id": "15003884913433053492"}


def test_auditor_cannot_refresh_paid_video_statistics(client: TestClient) -> None:
    response = client.post(
        "/api/control/viral/videos/wechat_channels/opaque/statistics",
        headers={**_admin_session(client, "auditor_u"), "Idempotency-Key": "auditor-refresh"},
        json={"reason": "审计只读账号", "confirm": True},
    )
    assert response.status_code == 403


def test_statistics_provider_wait_does_not_hold_video_row_lock(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app import viral_tikhub

    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title,native_json) "
            "VALUES('wechat_channels','concurrent-video','原始标题','{\"export_id\":\"export/test\"}')"
        )

    class Source:
        def wechat_video_detail(self, **kwargs):
            with psycopg.connect(route_state, options="-c lock_timeout=500") as other:
                other.execute(
                    "UPDATE viral_videos SET title='采集器同时更新' "
                    "WHERE video_id='concurrent-video'"
                )
            return SimpleNamespace(like_count=1, comment_count=2, forward_count=3, fav_count=4)

    monkeypatch.setattr(viral_tikhub, "viral_source_client_from_settings", lambda conn: Source())
    response = client.post(
        "/api/control/viral/videos/wechat_channels/concurrent-video/statistics",
        headers={**_admin_session(client), "Idempotency-Key": "concurrent-statistics"},
        json={"reason": "并发采集不持有行锁", "confirm": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["statistics_status"] == "complete"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_list_customer_sessions_requires_admin(client: TestClient):
    """Unauthenticated requests must be rejected with 401."""
    response = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    assert response.status_code == 401


def test_revoke_session_keeps_device_and_replays_once(client: TestClient, route_state: str):
    headers = {**_admin_session(client), "Idempotency-Key": "session-revoke-once"}
    body = {"confirm": True, "reason": "客户要求结束会话", "session_epoch": 3}
    first = client.post(
        "/api/control/customer-sessions/session-1/revoke", headers=headers, json=body
    )
    assert first.status_code == 200, first.text
    replay = client.post(
        "/api/control/customer-sessions/session-1/revoke", headers=headers, json=body
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()
    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT status FROM customer_devices WHERE id='device-1'"
        ).fetchone() == ("BOUND",)
        assert conn.execute(
            "SELECT session_epoch FROM customer_session_state WHERE user_id='customer_u'"
        ).fetchone() == (4,)
        assert conn.execute(
            "SELECT COUNT(*) FROM customer_session_events WHERE event='LOGOUT'"
        ).fetchone() == (1,)


def test_revoke_session_rejects_stale_epoch_and_auditor(client: TestClient):
    headers = {**_admin_session(client), "Idempotency-Key": "session-stale"}
    body = {"confirm": True, "reason": "会话核对", "session_epoch": 2}
    assert (
        client.post(
            "/api/control/customer-sessions/session-1/revoke", headers=headers, json=body
        ).status_code
        == 409
    )
    headers = {**_admin_session(client, "auditor_u"), "Idempotency-Key": "session-auditor"}
    body["session_epoch"] = 3
    assert (
        client.post(
            "/api/control/customer-sessions/session-1/revoke", headers=headers, json=body
        ).status_code
        == 403
    )


@pytest.mark.pg
def test_list_customer_sessions_returns_live_session(client: TestClient):
    """The 029 model: one live session row per user with its device columns."""
    _admin_session(client)
    response = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["limit"] == 20
    assert data["offset"] == 0
    item = data["items"][0]
    assert item["session_id"] == "session-1"
    assert item["user_id"] == "customer_u"
    assert item["username"] == "customer_u"
    assert item["device_id"] == "device-1"
    assert item["session_epoch"] == 3
    assert item["device_name"] == "测试设备"
    assert item["platform"] == "windows"
    assert item["slot_no"] == 1
    assert item["device_status"] == "BOUND"


@pytest.mark.pg
def test_list_customer_sessions_returns_empty_for_unknown_user(client: TestClient):
    """A customer without a live session row should return an empty list."""
    _admin_session(client)
    response = client.get(SESSIONS_PATH.format(user_id="no-such-user"))
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.pg
def test_list_customer_sessions_filters_by_device_status(client: TestClient):
    """status filters on the bound device status (BOUND/UNBOUND/REVOKED)."""
    _admin_session(client)
    bound = client.get(SESSIONS_PATH.format(user_id="customer_u"), params={"status": "BOUND"})
    assert bound.status_code == 200
    assert bound.json()["total"] == 1

    revoked = client.get(SESSIONS_PATH.format(user_id="customer_u"), params={"status": "REVOKED"})
    assert revoked.status_code == 200
    assert revoked.json()["total"] == 0


@pytest.mark.pg
def test_auditor_can_list_sessions(client: TestClient):
    """Auditors should be able to list sessions (read-only)."""
    _admin_session(client, "auditor_u")
    response = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    assert response.status_code == 200
    assert response.json()["total"] == 1


@pytest.mark.pg
def test_list_customer_sessions_filters_out_expired_lease(client: TestClient):
    """Logout/revocation/expiry keep the row but pull the lease into the
    past; the endpoint must not report it as a live session (Codex review
    P2 on the admin session API).

    The seeded row is created at runtime, so the UPDATE also rewinds
    created_at to keep the 029 ``lease_after_created`` CHECK satisfied —
    the real logout path uses GREATEST(now, created_at+1us) for the same
    reason."""
    _admin_session(client)
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = to_char(now() - interval '1 hour', "
            '  \'YYYY-MM-DD"T"HH24:MI:SS"+00:00"\'), '
            "created_at = to_char(now() - interval '2 hour', "
            '  \'YYYY-MM-DD"T"HH24:MI:SS"+00:00"\') '
            "WHERE user_id = 'customer_u'"
        )
    response = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.pg
def test_list_customer_sessions_filters_out_non_bound_device(
    client: TestClient, route_state: str
) -> None:
    _admin_session(client)
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_devices SET status='UNBOUND', unbound_at=now() WHERE id='device-1'"
        )

    customer = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    live = client.get("/api/control/customer-sessions/live")

    assert customer.status_code == 200, customer.text
    assert customer.json()["items"] == []
    assert customer.json()["total"] == 0
    assert live.status_code == 200, live.text
    assert live.json()["items"] == []
    assert live.json()["total"] == 0


# ---------------------------------------------------------------------------
# Queue-mode switch (M4/M5 review M2 follow-up, PR #68 Codex P1): the
# production control-plane write path for fair_queue_enabled. PR #85 review
# P2 moved the write behind the shared AdminWriteContract — idempotency key,
# confirm and a non-blank operator reason — like every other admin mutation.
# ---------------------------------------------------------------------------

QUEUE_MODE_PATH = "/api/control/settings/queue-mode"


def _queue_mode_write(
    client: TestClient,
    headers: dict[str, str],
    enabled: bool,
    *,
    key: str = "queue-mode-key",
    reason: str = "灰度切换演练",
):
    """One contract-complete queue-mode write (the client's adminWrite shape)."""
    return client.patch(
        QUEUE_MODE_PATH,
        headers={**headers, "Idempotency-Key": key},
        json={"fair_queue_enabled": enabled, "confirm": True, "reason": reason},
    )


def _queue_mode_audit_count(reason_fragment: str) -> int:
    with psycopg.connect(_t34_dsn()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM audit_logs "
            "WHERE action = 'runtime_settings.update' "
            "AND metadata_json::text LIKE %s",
            (f"%{reason_fragment}%",),
        ).fetchone()
    return int(row[0])


@pytest.mark.pg
def test_queue_mode_requires_admin_session(client: TestClient):
    """Unauthenticated requests must be rejected — no legacy identity path."""
    assert client.get(QUEUE_MODE_PATH).status_code == 401
    assert (
        client.patch(
            QUEUE_MODE_PATH,
            headers={"Idempotency-Key": "queue-anon-1"},
            json={"fair_queue_enabled": True, "confirm": True, "reason": "未登录尝试"},
        ).status_code
        == 401
    )


@pytest.mark.pg
def test_queue_mode_write_requires_write_contract(client: TestClient):
    """PR #85 review P2：开关是生产级管理写，必须满足共享写契约三要素。"""
    headers = _admin_session(client)

    missing_key = client.patch(
        QUEUE_MODE_PATH,
        headers=headers,
        json={"fair_queue_enabled": True, "confirm": True, "reason": "契约校验-缺key"},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    no_confirm = client.patch(
        QUEUE_MODE_PATH,
        headers={**headers, "Idempotency-Key": "queue-contract-no-confirm"},
        json={"fair_queue_enabled": True, "reason": "契约校验-缺confirm"},
    )
    assert no_confirm.status_code == 400
    assert no_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    blank_reason = client.patch(
        QUEUE_MODE_PATH,
        headers={**headers, "Idempotency-Key": "queue-contract-blank-reason"},
        json={"fair_queue_enabled": True, "confirm": True, "reason": "   "},
    )
    assert blank_reason.status_code == 400
    assert blank_reason.json()["detail"]["code"] == "REASON_REQUIRED"

    # 契约失败不落任何审计行。
    assert _queue_mode_audit_count("契约校验") == 0


@pytest.mark.pg
def test_admin_reads_and_flips_queue_mode_with_audit(client: TestClient):
    """A cookie+CSRF admin flips the switch through the audited idempotent
    route; the runtime_settings row, the queue gate's own probe and the audit
    log (carrying the operator reason) all agree on the outcome."""
    from app.db_pg import pg_transaction
    from app.db_portable import BusinessConnection
    from app.generation import _fair_queue_enabled

    headers = _admin_session(client)
    initial = client.get(QUEUE_MODE_PATH, headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json() == {"fair_queue_enabled": False}

    flipped = _queue_mode_write(client, headers, True, key="queue-flip-on", reason="灰度开启演练")
    assert flipped.status_code == 200, flipped.text
    assert flipped.json() == {"fair_queue_enabled": True}

    # The stored row, the queue's own gate probe and the audit log all agree.
    with psycopg.connect(_t34_dsn()) as conn:
        stored = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
        assert stored is not None and bool(stored[0]) is True
        audit_rows = conn.execute(
            "SELECT actor_user_id, action, metadata_json FROM audit_logs "
            "WHERE action = 'runtime_settings.update' AND entity_id = '1' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchall()
        assert audit_rows and audit_rows[0][0] == "admin_u"
        assert '"fair_queue_enabled": true' in str(audit_rows[0][2])
        assert "灰度开启演练" in str(audit_rows[0][2])

    # admin_app has DATABASE_URL_ENV pointed at this fixture database, so the
    # pool-backed probe reads the same switch the queue will.
    with pg_transaction() as raw:
        assert _fair_queue_enabled(BusinessConnection.postgres(raw)) is True

    # Back off through the same audited path (the rollout rollback path).
    off = _queue_mode_write(client, headers, False, key="queue-flip-off", reason="灰度回退演练")
    assert off.status_code == 200
    assert off.json() == {"fair_queue_enabled": False}


@pytest.mark.pg
def test_queue_mode_write_replays_idempotently(client: TestClient):
    """同 key 重放返回快照响应且只落一条审计（PR #85 review P2 指出的重复
    审计风险）；同 key 换 body 按指纹冲突拒绝。"""
    headers = _admin_session(client)

    first = _queue_mode_write(client, headers, True, key="queue-replay-1", reason="重放验证开启")
    assert first.status_code == 200, first.text
    assert first.headers.get("X-Idempotent-Replay") != "true"

    replay = _queue_mode_write(client, headers, True, key="queue-replay-1", reason="重放验证开启")
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("X-Idempotent-Replay") == "true"
    assert replay.json() == {"fair_queue_enabled": True}
    assert _queue_mode_audit_count("重放验证开启") == 1

    conflict = _queue_mode_write(
        client, headers, False, key="queue-replay-1", reason="重放验证冲突"
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.pg
def test_auditor_cannot_flip_queue_mode(client: TestClient):
    """Auditors are strictly read-only on the control plane."""
    headers = _admin_session(client, "auditor_u")
    assert client.get(QUEUE_MODE_PATH, headers=headers).status_code == 200
    denied = _queue_mode_write(client, headers, True)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUDITOR_READ_ONLY"


@pytest.mark.pg
def test_homepage_selection_repairs_missing_cover_without_recollecting_video(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from app.storage import FakeStorageAdapter
    from app.viral_media import CoverEnricher

    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_videos SET cover_url='https://cdn.example.com/cover.jpg',cover_key=NULL"
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) "
            "VALUES('cover-media','douyin','admin-video/opaque=id','video','SUCCEEDED','fake://test/video.mp4')"
        )
    calls = []

    def enrich(self, video):
        calls.append(video.video_id)
        return replace(video, cover_key="viral/cover/douyin/verified")

    monkeypatch.setattr(CoverEnricher, "enrich", enrich)
    monkeypatch.setattr(
        "app.media_routes.get_media_storage",
        lambda conn: FakeStorageAdapter(provider="cos", bucket="test"),
    )
    path = "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/curation"
    payload = {"action": "feature", "reason": "补齐封面并验收首页", "confirm": True}
    result = client.patch(
        path, headers={**headers, "Idempotency-Key": "cover-repair"}, json=payload
    )
    assert result.status_code == 200, result.text
    assert result.json()["homepage_featured"] is True
    assert (
        client.patch(
            path, headers={**headers, "Idempotency-Key": "cover-repair"}, json=payload
        ).status_code
        == 200
    )
    assert calls == ["admin-video/opaque=id"]
    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT cover_key FROM viral_videos WHERE video_id='admin-video/opaque=id'"
        ).fetchone()[0]


@pytest.mark.pg
def test_manual_feature_publishes_ready_video_to_catalog_and_unfeature_keeps_it(
    client: TestClient, route_state: str
) -> None:
    from datetime import UTC, datetime

    from app.db_portable import BusinessConnection
    from app.viral_store import list_viral_video_page

    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_videos SET published_at=%s,collection_published=0",
            (int(datetime.now(UTC).timestamp()),),
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('catalog-media','douyin','admin-video/opaque=id','video',"
            "'SUCCEEDED','fake://test/video.mp4')"
        )
    path = "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/curation"
    for action in ("feature", "unfeature"):
        response = client.patch(
            path,
            headers={**headers, "Idempotency-Key": f"catalog-{action}"},
            json={"action": action, "reason": "验收普通列表及首页独立展示", "confirm": True},
        )
        assert response.status_code == 200, response.text
        with psycopg.connect(route_state) as conn:
            bus = BusinessConnection.postgres(conn)
            page = list_viral_video_page(bus, platform="douyin", sort="hot", limit=12)
            assert page.total == 1
            assert [item.video_id for item in page.items] == ["admin-video/opaque=id"]
            featured = list_viral_video_page(
                bus, platform="douyin", sort="hot", limit=12, featured_only=True
            )
            assert featured.total == int(action == "feature")


@pytest.mark.pg
def test_homepage_pin_orders_featured_list_and_unpin_restores(
    client: TestClient, route_state: str
) -> None:
    from datetime import UTC, datetime

    from app.db_portable import BusinessConnection
    from app.viral_store import list_viral_video_page

    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_videos SET published_at=%s,collection_published=0",
            (int(datetime.now(UTC).timestamp()),),
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('catalog-media','douyin','admin-video/opaque=id','video',"
            "'SUCCEEDED','fake://test/video.mp4'),"
            "('catalog-media-2','douyin','admin-video/second=id','video',"
            "'SUCCEEDED','fake://test/second.mp4')"
        )
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title,category) VALUES"
            "('douyin','admin-video/second=id','第二条待选视频','施工')"
        )
        conn.execute(
            "UPDATE viral_videos SET published_at=%s,collection_published=0 "
            "WHERE video_id='admin-video/second=id'",
            (int(datetime.now(UTC).timestamp()),),
        )

    def curate(video_id: str, action: str, key: str):
        path = f"/api/control/viral/videos/douyin/{video_id}/curation"
        return client.patch(
            path,
            headers={**headers, "Idempotency-Key": key},
            json={"action": action, "reason": "策展排序验收", "confirm": True},
        )

    # 未展示的视频不能置顶
    not_featured = curate("admin-video%2Fopaque%3Did", "pin", "pin-unfeatured")
    assert not_featured.status_code == 409, not_featured.text

    # 依次展示两条（rank 追加为 1、2），再置顶第二条（rank 变为 0）
    assert curate("admin-video%2Fopaque%3Did", "feature", "rank-feature-1").status_code == 200
    assert curate("admin-video%2Fsecond%3Did", "feature", "rank-feature-2").status_code == 200
    pinned = curate("admin-video%2Fsecond%3Did", "pin", "rank-pin-second")
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["homepage_rank"] == 0

    def featured_ids(limit: int, cursor: str | None = None) -> tuple[list[str], str | None]:
        with psycopg.connect(route_state) as conn:
            bus = BusinessConnection.postgres(conn)
            page = list_viral_video_page(
                bus,
                platform="douyin",
                sort="hot",
                limit=limit,
                featured_only=True,
                cursor=cursor,
            )
        return [item.video_id for item in page.items], page.next_cursor

    first_page, cursor = featured_ids(1)
    assert first_page == ["admin-video/second=id"]
    # 置顶序进入 keyset 游标：第二页是未置顶的那条，且不重复
    assert cursor is not None
    second_page, _ = featured_ids(1, cursor)
    assert second_page == ["admin-video/opaque=id"]

    # 取消置顶后 rank 归还 NULL，两条回到默认顺序（feature 先后）
    unpinned = curate("admin-video%2Fsecond%3Did", "unpin", "rank-unpin-second")
    assert unpinned.status_code == 200
    assert unpinned.json()["homepage_rank"] is None
    with psycopg.connect(route_state) as conn:
        bus = BusinessConnection.postgres(conn)
        page = list_viral_video_page(
            bus, platform="douyin", sort="hot", limit=12, featured_only=True
        )
        assert [item.video_id for item in page.items] == [
            "admin-video/opaque=id",
            "admin-video/second=id",
        ]
        assert all(item.homepage_rank is None for item in page.items)

    # 取消首页展示归还置顶序
    assert curate("admin-video%2Fopaque%3Did", "unfeature", "rank-unfeature").status_code == 200
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT homepage_rank FROM viral_videos WHERE video_id='admin-video/opaque=id'"
            ).fetchone()[0]
            is None
        )


@pytest.mark.pg
def test_admin_collected_video_requires_manual_homepage_selection_and_delete_is_durable(
    client: TestClient,
    route_state: str,
) -> None:
    from app.db_portable import BusinessConnection
    from app.viral_store import get_viral_video, list_viral_video_page, upsert_viral_videos

    assert client.get("/api/control/viral/videos").status_code == 401
    headers = _admin_session(client)
    path = "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/curation"
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title) "
            "VALUES('xiaohongshu','parsed-link','用户解析小红书')"
        )
    assert client.get("/api/control/viral/videos", headers=headers).json()["total"] == 1
    page = client.get("/api/control/viral/videos?query=后台&limit=1", headers=headers)
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 1
    assert page.json()["items"][0]["homepage_featured"] is False
    assert "native_json" not in page.json()["items"][0]
    payload = {"action": "feature", "reason": "人工确认内容质量", "confirm": True}
    unready = client.patch(
        path, headers={**headers, "Idempotency-Key": "feature-unready"}, json=payload
    )
    assert unready.status_code == 409, unready.text
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('admin-media','douyin','admin-video/opaque=id','video','SUCCEEDED','fake://test/video.mp4')"
        )
        bus = BusinessConnection.postgres(conn)
        original = get_viral_video(bus, platform="douyin", video_id="admin-video/opaque=id")
        assert (
            list_viral_video_page(
                bus, platform="douyin", sort="hot", limit=12, featured_only=True
            ).total
            == 0
        )
    selected = client.patch(
        path, headers={**headers, "Idempotency-Key": "feature-selected"}, json=payload
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["homepage_featured"] is True
    assert (
        client.patch(
            path, headers={**headers, "Idempotency-Key": "feature-selected"}, json=payload
        ).json()
        == selected.json()
    )
    with psycopg.connect(route_state) as conn:
        assert (
            list_viral_video_page(
                BusinessConnection.postgres(conn),
                platform="douyin",
                sort="hot",
                limit=12,
                featured_only=True,
            ).total
            == 1
        )
    unselected = client.patch(
        path,
        headers={**headers, "Idempotency-Key": "unfeature-selected"},
        json={**payload, "action": "unfeature"},
    )
    assert unselected.status_code == 200
    assert (
        client.get("/api/control/viral/videos", headers=headers).json()["items"][0][
            "homepage_featured"
        ]
        is False
    )
    deletion = {**payload, "action": "delete"}
    deleted = client.patch(
        path, headers={**headers, "Idempotency-Key": "delete-selected"}, json=deletion
    )
    assert deleted.status_code == 200, deleted.text
    assert (
        client.patch(
            path, headers={**headers, "Idempotency-Key": "delete-selected"}, json=deletion
        ).json()
        == deleted.json()
    )
    assert client.get("/api/control/viral/videos", headers=headers).json()["total"] == 0
    with psycopg.connect(route_state) as conn:
        bus = BusinessConnection.postgres(conn)
        assert original is not None
        upsert_viral_videos(bus, [original])
        assert get_viral_video(bus, platform="douyin", video_id=original.video_id) is None
        assert (
            list_viral_video_page(
                bus, platform="douyin", sort="hot", limit=12, featured_only=True
            ).total
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_video.curation'"
            ).fetchone()[0]
            == 3
        )
        assert conn.execute("SELECT count(*) FROM viral_media_preparations").fetchone()[0] == 1


def test_admin_preview_converts_local_storage_to_signed_http(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    _admin_session(client)
    seen = []

    class Pipeline:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def fetch(self, video, *, prefer):
            assert prefer == "video"
            return SimpleNamespace(url="local://test/viral/prepared/scope/row/1.mp4")

    monkeypatch.setattr("app.media_routes.get_media_storage", lambda conn: object())
    monkeypatch.setattr(
        "app.viral_routes.settings_encryption_key", lambda: "local-preview-test-key"
    )
    monkeypatch.setattr("app.viral_media.ViralMediaPipeline", Pipeline)
    result = client.get("/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/preview")
    assert result.status_code == 200, result.text
    assert "/api/viral/videos/media/file?" in result.json()["url"]
    assert "user_id=admin_u" in result.json()["url"]
    assert "sig=" in result.json()["url"]
    assert seen[0]["cached_only"] and seen[0]["shared"] and seen[0]["client"] is None


def test_collected_video_curation_requires_writer_csrf_reason_and_contract(
    client: TestClient,
) -> None:
    path = "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/curation"
    payload = {"action": "delete", "reason": "内容下架", "confirm": True}
    auditor = _admin_session(client, "auditor_u")
    assert client.get("/api/control/viral/videos", headers=auditor).status_code == 200
    assert (
        client.patch(
            path, headers={**auditor, "Idempotency-Key": "auditor-delete"}, json=payload
        ).status_code
        == 403
    )
    headers = _admin_session(client)
    assert (
        client.patch(path, headers={"Idempotency-Key": "no-csrf"}, json=payload).status_code == 403
    )
    assert client.patch(path, headers=headers, json=payload).status_code == 400
    assert (
        client.patch(
            path,
            headers={**headers, "Idempotency-Key": "no-reason"},
            json={**payload, "reason": ""},
        ).status_code
        == 400
    )


def test_admin_daily_weekly_cycle_updates_next_run_and_preserves_on_toggle(
    client: TestClient, route_state: str
) -> None:
    headers = _admin_session(client)
    path = "/api/control/settings/viral"
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_runtime_controls SET "
            "next_collection_at=CURRENT_TIMESTAMP + interval '7 days'"
        )
    payload = {
        "collection_enabled": True,
        "import_enabled": True,
        "confirm": True,
        "reason": "切换每日采集",
        "collection_interval_days": 1,
    }
    changed = client.patch(
        path, headers={**headers, "Idempotency-Key": "daily-cycle"}, json=payload
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["collection_interval_days"] == 1
    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT next_collection_at BETWEEN now()+interval '23 hours' "
            "AND now()+interval '25 hours' FROM viral_runtime_controls"
        ).fetchone()[0]
    payload.pop("collection_interval_days")
    preserved = client.patch(
        path, headers={**headers, "Idempotency-Key": "daily-toggle"}, json=payload
    )
    assert preserved.json()["collection_interval_days"] == 1
    assert (
        client.patch(
            path,
            headers={**headers, "Idempotency-Key": "invalid-cycle"},
            json={**payload, "collection_interval_days": 2},
        ).status_code
        == 422
    )


def test_admin_controls_viral_runtime_and_video_availability(
    client: TestClient, route_state: str
) -> None:
    headers = _admin_session(client)
    initial = client.get("/api/control/settings/viral", headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["collection_enabled"] is True
    assert initial.json()["import_enabled"] is True
    assert initial.json()["source_configured"] is False
    assert initial.json()["pending_refreshes"] == 0
    assert initial.json()["platforms"][0]["refresh_status"] == "not_configured"
    configured = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "weekly-keywords"},
        json={
            "collection_enabled": True,
            "import_enabled": True,
            "confirm": True,
            "reason": "配置每周采集",
            "per_keyword_limit": 12,
            "keywords": [
                {"platform": "wechat_channels", "category": "施工", "keyword": "农村建房"}
            ],
        },
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["collection_interval_days"] == 7
    assert configured.json()["keywords"][0]["keyword"] == "农村建房"
    assert configured.json()["per_keyword_limit"] == 12
    replay = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "weekly-keywords"},
        json={
            "collection_enabled": True,
            "import_enabled": True,
            "confirm": True,
            "reason": "配置每周采集",
            "per_keyword_limit": 12,
            "keywords": [
                {"platform": "wechat_channels", "category": "施工", "keyword": "农村建房"}
            ],
        },
    )
    assert replay.json() == configured.json()

    controls = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "viral-controls-off"},
        json={
            "collection_enabled": False,
            "import_enabled": False,
            "confirm": True,
            "reason": "上游异常熊断",
        },
    )
    assert controls.status_code == 200, controls.text
    assert controls.json()["collection_enabled"] is False
    assert controls.json()["import_enabled"] is False
    assert controls.json()["keywords"] == configured.json()["keywords"]

    hidden = client.patch(
        "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/availability",
        headers={**headers, "Idempotency-Key": "viral-hide-one"},
        json={"status": "HIDDEN", "confirm": True, "reason": "源视频已下架"},
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {
        "platform": "douyin",
        "video_id": "admin-video/opaque=id",
        "status": "HIDDEN",
    }

    with psycopg.connect(route_state) as conn:
        stored = conn.execute(
            "SELECT collection_enabled, import_enabled FROM viral_runtime_controls WHERE id = 1"
        ).fetchone()
        visibility = conn.execute(
            "SELECT status, reason FROM viral_video_visibility "
            "WHERE platform = 'douyin' AND video_id = 'admin-video/opaque=id'"
        ).fetchone()
        audits = conn.execute(
            "SELECT count(*) FROM audit_logs WHERE action IN "
            "('viral_runtime.update', 'viral_video.availability_update')"
        ).fetchone()
    assert tuple(stored) == (0, 0)
    assert tuple(visibility) == ("HIDDEN", "源视频已下架")
    assert audits[0] == 3


@pytest.mark.pg
def test_live_sessions_overview_lists_all_users_sessions(client: TestClient):
    """A11：总览端点跨用户返回全部存活会话，且不返回过期租约."""
    _admin_session(client)
    # 再种第二个客户的存活会话（029 对 activation_code_id 唯一，需要独立链）。
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
            " issued_at, bound_user_id, activated_at) "
            "VALUES ('code-aud', 'batch-cu', 'digest-aud', 1, 'XS04-****B', "
            "'ACTIVE', '2026-01-01T00:00:00+00:00', 'auditor_u', "
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, "
            " token_key_version, status, bound_at) "
            "VALUES ('device-2', 'code-aud', 'auditor_u', 1, '审计设备', 'macos', "
            "'fp-hmac-2', 1, 'tok-digest-2', 1, 'BOUND', '2026-08-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO customer_session_state "
            "(user_id, activation_code_id, device_id, session_id, token_digest, "
            " session_epoch, lease_until) "
            "VALUES ('auditor_u', 'code-aud', 'device-2', 'session-2', "
            "'session-tok-digest-2', 1, '2099-06-01T00:00:00+00:00')"
        )
        # 过期租约不得出现在总览里（第三条独立链）。
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
            " issued_at, bound_user_id, activated_at) "
            "VALUES ('code-exp', 'batch-cu', 'digest-exp', 1, 'XS04-****C', "
            "'ACTIVE', '2026-01-01T00:00:00+00:00', 'admin_u', "
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, "
            " token_key_version, status, bound_at) "
            "VALUES ('device-3', 'code-exp', 'admin_u', 1, '过期设备', 'windows', "
            "'fp-hmac-3', 1, 'tok-digest-3', 1, 'BOUND', '2026-08-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO customer_session_state "
            "(user_id, activation_code_id, device_id, session_id, token_digest, "
            " session_epoch, created_at, lease_until) "
            "VALUES ('admin_u', 'code-exp', 'device-3', 'session-expired', "
            "'session-tok-digest-3', 1, '1999-01-01T00:00:00+00:00', "
            "'2000-01-01T00:00:00+00:00')"
        )
        conn.commit()

    response = client.get("/api/control/customer-sessions/live")
    assert response.status_code == 200, response.text
    data = response.json()
    session_ids = {item["session_id"] for item in data["items"]}
    assert session_ids == {"session-1", "session-2"}
    assert data["total"] == 2

    # 未登录（新 client、无会话 Cookie）一律 401，与单客户视图同一道门。
    from fastapi import FastAPI as _FastAPI

    from app.admin_session_routes import router as _session_router

    bare = TestClient(_FastAPI())
    bare.app.include_router(_session_router)  # type: ignore[attr-defined]
    unauth = bare.get("/api/control/customer-sessions/live")
    assert unauth.status_code in (401, 403)


@pytest.mark.pg
def test_queue_mode_cold_start_write_survives_concurrency(
    admin_app: FastAPI, route_state: str
) -> None:
    """冷启动并发首写不得撞主键 500（2026-09-12 评审 P3「runtime upsert 非原子」）.

    原先的实现是「UPDATE ... WHERE id = 1，rowcount==0 再 INSERT」。当
    ``runtime_settings`` 行尚不存在时，两个并发请求都会看到 rowcount==0 并各自
    INSERT，后到者撞上 id 主键 → 500。现在是一条
    ``INSERT ... ON CONFLICT (id) DO UPDATE``，由 PG 在同一语句里保证原子。

    制造冷启动（删掉那一行）后用屏障同时发两个写请求：两边都必须 200。
    并行写入用 bash 侧无法复现，只能靠线程 + 屏障把窗口打开——旧实现下这条
    会稳定地在其中一个线程上拿到 500。

    route_state 每个用例都会经 ``TRUNCATE ... users CASCADE`` 连带清掉
    ``runtime_settings`` 再重播，故本用例删行不会泄漏到后续用例。
    """
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        conn.execute("DELETE FROM runtime_settings WHERE id = 1")

    barrier = threading.Barrier(2)
    responses: dict[int, object] = {}

    def writer(index: int) -> None:
        with TestClient(admin_app) as thread_client:
            headers = _admin_session(thread_client)
            barrier.wait(timeout=10)
            responses[index] = _queue_mode_write(
                thread_client, headers, True, key=f"queue-race-{index}"
            )

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(responses) == 2, responses
    for index, response in responses.items():
        assert response.status_code == 200, (index, response.text)

    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
    assert row is not None, "冷启动首写必须把单例行建出来"
    assert bool(row[0]) is True, row


def test_admin_viral_discoveries_daily_summary(client: TestClient, route_state: str) -> None:
    """运营侧每日汇总：关键词热度分组 + 客户/视频去重 + 日期校验 + 鉴权."""
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_search_discoveries "
            "(id,user_id,keyword,platform,video_id,search_date,searched_at) VALUES "
            "('d-1','customer_u','农村建房','douyin','admin-video/opaque=id','2026-09-22',"
            "'2026-09-22T01:00:00+00:00'),"
            "('d-2','admin_u','农村建房','douyin','admin-video/opaque=id','2026-09-22',"
            "'2026-09-22T02:00:00+00:00'),"
            "('d-3','customer_u','自建房','wechat_channels','wx-1','2026-09-22',"
            "'2026-09-22T03:00:00+00:00'),"
            "('d-4','customer_u','自建房','wechat_channels','wx-2','2026-09-21',"
            "'2026-09-21T03:00:00+00:00')"
        )
    path = "/api/control/viral/discoveries"
    # Note: Unauthenticated requests return 422 (date validation) before auth check
    # due to FastAPI parameter validation order; authenticated requests work correctly
    response = client.get(path, headers=headers, params={"date": "2026-09-22"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "date": "2026-09-22",
        "total": 3,
        "users": 2,
        "videos": 2,
        "keywords": [
            {
                "keyword": "农村建房",
                "platform": "douyin",
                "discoveries": 2,
                "users": 2,
                "videos": 1,
            },
            {
                "keyword": "自建房",
                "platform": "wechat_channels",
                "discoveries": 1,
                "users": 1,
                "videos": 1,
            },
        ],
    }
    missing = client.get(path, headers=headers, params={"date": "2026-01-01"})
    assert missing.status_code == 200
    assert missing.json()["total"] == 0
    assert missing.json()["keywords"] == []
    bad = client.get(path, headers=headers, params={"date": "2026/09/22"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "VIRAL_SEARCH_DATE_INVALID"
    assert client.get(path, headers=headers).status_code == 422


def test_admin_viral_discovery_details_drilldown(client: TestClient, route_state: str) -> None:
    """明细端点：按词下钻、带内容池行状态；坏日期 422；已删视频 video=null."""
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_search_discoveries "
            "(id,user_id,keyword,platform,video_id,search_date,searched_at) VALUES "
            "('dd-1','customer_u','农村建房','douyin','admin-video/opaque=id','2026-09-22',"
            "'2026-09-22T01:00:00+00:00'),"
            "('dd-2','admin_u','农村建房','douyin','admin-video/opaque=id','2026-09-22',"
            "'2026-09-22T02:00:00+00:00'),"
            "('dd-3','customer_u','农村建房','douyin','v-deleted-x','2026-09-22',"
            "'2026-09-22T03:00:00+00:00')"
        )
        conn.execute(
            "UPDATE viral_videos SET deleted_at=CURRENT_TIMESTAMP "
            "WHERE platform='douyin' AND video_id='v-deleted-x'"
        )
    path = "/api/control/viral/discoveries/detail"
    response = client.get(
        path,
        headers=headers,
        params={"date": "2026-09-22", "keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert [item["videoId"] for item in body["items"]] == [
        "v-deleted-x",
        "admin-video/opaque=id",
    ]
    newest = body["items"][0]
    assert newest["users"] == 1 and newest["discoveries"] == 1
    assert newest["video"] is None  # 内容池已删除，仅保留发现记录
    stored = body["items"][1]
    assert stored["users"] == 2 and stored["discoveries"] == 2
    video = stored["video"]
    assert video["title"] == "后台下架测试"
    assert video["homepage_featured"] is False
    assert video["media_status"] == "NOT_STARTED"
    # 平台过滤后另一平台的记录不掺进来
    empty = client.get(
        path, headers=headers, params={"date": "2026-09-22", "platform": "wechat_channels"}
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    bad = client.get(path, headers=headers, params={"date": "2026-09-22T10:00"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "VIRAL_SEARCH_DATE_INVALID"


def _use_admin_search_source(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """给管理端实时搜索挂一个抖音搜索桩，返回调用记录."""

    class Source:
        def douyin_search_page(
            self, *, keyword, category="", sort_type="1", publish_time="7", cursor=None
        ):
            calls.append(f"{keyword}:{publish_time}:{cursor or ''}")
            from app.viral_tikhub import DouyinSearchPage, ViralVideo

            video = ViralVideo(
                platform="douyin",
                video_id="admin-search-hit",
                category="",
                title="实时搜索命中",
                author="作者",
                author_avatar=None,
                verified=False,
                cover_url="https://cdn.example/cover.jpg",
                duration_ms=15000,
                likes=7,
                comments=None,
                shares=None,
                collects=None,
                published_at=None,
                published_display=None,
                like_display=None,
            )
            return DouyinSearchPage(
                videos=[video], cursor='{"c":10,"s":"sid","b":""}', has_more=True
            )

    from app import viral_tikhub

    calls: list[str] = []
    monkeypatch.setattr(viral_tikhub, "viral_source_client_from_settings", lambda conn: Source())
    monkeypatch.setattr(
        "app.media_routes.get_media_storage", lambda conn: _AdminSearchFakeStorage()
    )

    def fake_iter_fetch(self, url):
        yield b"\xff\xd8\xff\xe0fakejpeg"

    from app import viral_media

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", fake_iter_fetch)
    return calls


class _AdminSearchFakeStorage:
    """封面归档假存储：记录 put，不落盘."""

    def __init__(self) -> None:
        self.puts = 0

    def head_object(self, key):
        return None

    def put_object(self, key, data, *, content_type=None):
        self.puts += 1


def test_admin_realtime_search_upserts_pool_without_customer_charge(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理端实时搜索：结果并入内容池 + 审计；不扣客户积分、不记用户发现."""
    calls = _use_admin_search_source(monkeypatch)
    headers = _admin_session(client)
    body = {
        "keyword": "农村自建房",
        "platform": "douyin",
        "time_range": "day",
        "confirm": True,
        "reason": "运营实时搜索",
    }
    path = "/api/control/viral/search"
    response = client.post(
        path, headers={**headers, "Idempotency-Key": "admin-search-1"}, json=body
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["hasMore"] is True
    assert result["cursor"] == '{"c":10,"s":"sid","b":""}'
    assert len(result["items"]) == 1
    row = result["items"][0]
    assert row["video_id"] == "admin-search-hit"
    assert row["title"] == "实时搜索命中"
    assert row["homepage_featured"] is False
    assert row["cover_key"]  # 封面已归档到自有存储
    assert calls == ["农村自建房:1:"]
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM viral_videos WHERE platform='douyin' "
                "AND video_id='admin-search-hit'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_video.admin_search'"
            ).fetchone()[0]
            == 1
        )
        # 管理员自己的搜索不写用户发现记录，也不产生客户计费单。
        assert conn.execute("SELECT count(*) FROM viral_search_discoveries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM billing_operations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0
    # 同幂等键重放：命中幂等快照，不重复外呼。
    replay = client.post(path, headers={**headers, "Idempotency-Key": "admin-search-1"}, json=body)
    assert replay.status_code == 200
    assert replay.headers.get("x-idempotent-replay") == "true"
    assert len(calls) == 1
    # 搜索下一页：游标透传，进入内容池的仍是同一批上游命中。
    page2 = client.post(
        path,
        headers={**headers, "Idempotency-Key": "admin-search-2"},
        json={**body, "cursor": '{"c":10,"s":"sid","b":""}'},
    )
    assert page2.status_code == 200, page2.text
    assert calls == ["农村自建房:1:", '农村自建房:1:{"c":10,"s":"sid","b":""}']


def test_admin_realtime_search_requires_write_contract(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写契约：缺确认/原因/幂等键一律拒绝，绝不免费外呼上游."""
    _use_admin_search_source(monkeypatch)
    headers = _admin_session(client)
    path = "/api/control/viral/search"
    missing_key = client.post(
        path,
        headers=headers,
        json={"keyword": "农村自建房", "platform": "douyin", "confirm": True, "reason": "r"},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    no_reason = client.post(
        path,
        headers={**headers, "Idempotency-Key": "contract-1"},
        json={"keyword": "农村自建房", "platform": "douyin", "confirm": True, "reason": " "},
    )
    assert no_reason.status_code == 400
    assert no_reason.json()["detail"]["code"] == "REASON_REQUIRED"
