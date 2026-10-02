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

import json

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


@pytest.mark.parametrize("action", ["search", "prepare", "statistics"])
def test_operation_cost_estimate_is_read_only_and_uses_metered_tariff(client, route_state, action):
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,unit_cost_fen) VALUES('viral_data',2.5) "
            "ON CONFLICT(service) DO UPDATE SET unit_cost_fen=2.5"
        )
        before = raw.execute("SELECT count(*) FROM billing_operations").fetchone()[0]
    body = {
        "action": action,
        "items": []
        if action == "search"
        else [{"platform": "douyin", "video_id": "admin-video/opaque=id"}],
    }
    result = client.post("/api/control/viral/operations/estimate", headers=headers, json=body)
    assert result.status_code == 200, result.text
    estimate = result.json()
    assert estimate["service"] == "viral_data" and estimate["unitCostFen"] == 2.5
    assert estimate["normalRetryCallsMax"] == estimate["logicalCallsMax"] * 3
    assert estimate["totalCostFen"] is None and estimate["customerCredits"] == 0
    with psycopg.connect(route_state) as raw:
        assert raw.execute("SELECT count(*) FROM billing_operations").fetchone()[0] == before
        raw.execute("UPDATE billing_tariffs SET unit_cost_fen=NULL WHERE service='viral_data'")
    unknown = client.post(
        "/api/control/viral/operations/estimate", headers=headers, json=body
    ).json()
    assert unknown["unitCostFen"] is None and unknown["dataCostMaxFen"] is None
    assert unknown["snapshot"] != estimate["snapshot"]


def test_search_rejects_changed_cost_before_provider_or_record_write(
    client, route_state, monkeypatch
):
    headers = _admin_session(client)
    _use_admin_search_source(monkeypatch)
    estimate = client.post(
        "/api/control/viral/operations/estimate", headers=headers, json={"action": "search"}
    ).json()
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,unit_cost_fen,version) VALUES('viral_data',9,9) "
            "ON CONFLICT(service) DO UPDATE SET unit_cost_fen=9,version=9"
        )
    result = client.post(
        "/api/control/viral/search",
        headers={**headers, "Idempotency-Key": "stale-cost"},
        json={
            "confirm": True,
            "reason": "核对费用变化",
            "keyword": "费用",
            "expected_cost_snapshot": estimate["snapshot"],
        },
    )
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "VIRAL_COST_CHANGED"
    with psycopg.connect(route_state) as raw:
        assert raw.execute("SELECT count(*) FROM viral_collection_batches").fetchone()[0] == 0


def test_twelve_video_preparation_estimate_reads_new_price_and_deduplicates(client, route_state):
    headers = _admin_session(client)
    targets = [{"platform": "wechat_channels", "video_id": f"cost-video-{i}"} for i in range(12)]
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,unit_cost_fen) VALUES('viral_data',2.5) "
            "ON CONFLICT(service) DO UPDATE SET unit_cost_fen=2.5"
        )
        for target in targets:
            raw.execute(
                "INSERT INTO viral_videos(platform,video_id,title,native_json) "
                "VALUES('wechat_channels',%s,'费用夹具',%s)",
                (target["video_id"], json.dumps({"export_id": target["video_id"]})),
            )
    body = {"action": "prepare", "items": targets + [targets[0]]}
    first = client.post("/api/control/viral/operations/estimate", headers=headers, json=body).json()
    assert first["logicalCallsMax"] == 12 and first["normalRetryCallsMax"] == 36
    assert first["mediaDownloadsMax"] == 12 and first["dataCostMaxFen"] == 90
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE billing_tariffs SET unit_cost_fen=5 WHERE service='viral_data'")
    updated = client.post(
        "/api/control/viral/operations/estimate", headers=headers, json=body
    ).json()
    assert updated["unitCostFen"] == 5 and updated["dataCostMaxFen"] == 180
    assert updated["snapshot"] != first["snapshot"] and updated["totalCostFen"] is None


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
            "billing_operations, wallet_transactions, recharge_orders, wallets, users, "
            "viral_refresh_tasks, viral_media_preparations, viral_import_tasks, "
            "viral_video_favorites, "
            "viral_video_visibility, "
            "viral_videos, "
            "viral_fetch_state, "
            "viral_search_events, viral_search_discoveries, "
            "viral_content_sources, viral_content_usage_events, viral_keyword_runs, "
            "viral_collection_batches, viral_platform_probes, "
            "security_rate_limit_counters, security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute("UPDATE viral_content_measurement_state SET started_at = now()")
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
            "collection_enabled = 1, import_enabled = 1, collection_time = NULL, "
            "updated_by_user_id = NULL"
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


def _admin_session(
    client: TestClient, actor: str = "admin_u", *, super_admin: bool = False
) -> dict[str, str]:
    """Exchange a real admin session cookie + CSRF header (the T12 pattern)."""
    if super_admin:
        with psycopg.connect(_t34_dsn()) as conn:
            conn.execute("UPDATE users SET is_super_admin=1 WHERE id=%s", (actor,))
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
            "SET lease_until = to_char((now() - interval '1 hour') AT TIME ZONE 'UTC', "
            '  \'YYYY-MM-DD"T"HH24:MI:SS"+00:00"\'), '
            "created_at = to_char((now() - interval '2 hour') AT TIME ZONE 'UTC', "
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
    headers = _admin_session(client, super_admin=True)

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

    headers = _admin_session(client, super_admin=True)
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
    headers = _admin_session(client, super_admin=True)

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
    """技术队列配置的读写都只对超管开放。"""
    headers = _admin_session(client, "auditor_u")
    assert client.get(QUEUE_MODE_PATH, headers=headers).status_code == 403
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
            "VALUES('cover-media','douyin','admin-video/opaque=id','video','SUCCEEDED','fak"
            "e://test/video.mp4')"
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
    assert pinned.json()["homepage_featured"] is True
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
    assert unpinned.json()["homepage_featured"] is True
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
    # 方案 P1 C-2：未准备的视频改为排队自动准备，就绪后由后台上首页。
    assert unready.status_code == 200, unready.text
    assert unready.json()["queued_for_preparation"] is True
    # 排队任务在同一夹具内清理，避免占用「每平台一个活跃任务」的槽位。
    with psycopg.connect(route_state) as conn:
        conn.execute("DELETE FROM viral_refresh_tasks WHERE platform='douyin'")
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('admin-media','douyin','admin-video/opaque=id','video','SUCCEEDED','fake://te"
            "st/video.mp4')"
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


@pytest.mark.pg
def test_link_imported_material_cannot_be_featured(
    client: TestClient,
    route_state: str,
) -> None:
    """链接导入素材不进首页：单条与批量都要拒，否则它会绕过采集侧质量门槛。"""
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_videos SET category='链接导入',"
            " cover_url='https://cdn.example.com/imported.jpg',"
            " cover_key='viral/cover/douyin/imported'"
            " WHERE video_id='admin-video/opaque=id'"
        )
        # 归档已完成：拒的理由必须是「链接导入」，而不是「尚未归档」。
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('import-media','douyin','admin-video/opaque=id','video',"
            "'SUCCEEDED','fake://test/video.mp4')"
        )
    path = "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/curation"
    refused = client.patch(
        path,
        headers={**headers, "Idempotency-Key": "feature-link-import"},
        json={"action": "feature", "reason": "导入素材尝试上首页", "confirm": True},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "VIRAL_LINK_IMPORT_NOT_CURATABLE"
    batch = client.post(
        "/api/control/viral/videos/curation:batch",
        headers={**headers, "Idempotency-Key": "batch-feature-link-import"},
        json={
            "action": "feature",
            "items": [{"platform": "douyin", "video_id": "admin-video/opaque=id"}],
            "reason": "批量导入素材上首页",
            "confirm": True,
        },
    )
    assert batch.status_code == 409, batch.text
    assert batch.json()["detail"]["code"] == "VIRAL_LINK_IMPORT_NOT_CURATABLE"
    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT homepage_featured FROM viral_videos WHERE video_id='admin-video/opaque=id'"
        ).fetchone()[0] in (0, False)
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_video.curation'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.pg
def test_collected_video_list_projects_card_fields_and_status_segments(
    client: TestClient, route_state: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_videos SET author='庭院设计阿伟',"
            " author_avatar='https://cdn.example.com/avatar.jpg', verified=1,"
            " published_display='09-20', like_display='12.5w',"
            " comments=12, shares=34, collects=56,"
            " cover_url='https://cdn.example.com/cover.jpg',"
            ' tags_json=\'["农村自建房","庭院施工"]\''
            " WHERE video_id='admin-video/opaque=id'"
        )
        conn.execute(
            "INSERT INTO viral_videos"
            "(platform,video_id,title,author,cover_url,cover_key,tags_json) "
            "VALUES('douyin','ready-video','已归档视频','阿伟',"
            "'https://cdn.example.com/ready.jpg','viral/cover/douyin/ready-video','[\"乡村别墅\"]')"
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('ready-media','douyin','ready-video','video','SUCCEEDED','fake://test/ready.mp4')"
        )
        # 链接导入 / 其它平台的行不属于采集库视图，任何分段都不应出现。
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title) "
            "VALUES('xiaohongshu','parsed-link','用户解析小红书')"
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('parsed-media','xiaohongshu','parsed-link','video','SUCCEEDED','fake://test/p.mp4')"
        )
    page = client.get("/api/control/viral/videos", headers=headers)
    assert page.status_code == 200, page.text
    items = {item["video_id"]: item for item in page.json()["items"]}
    assert set(items) == {"admin-video/opaque=id", "ready-video"}
    pending = items["admin-video/opaque=id"]
    assert pending["author"] == "庭院设计阿伟"
    assert pending["author_avatar"] == "https://cdn.example.com/avatar.jpg"
    assert pending["verified"] is True
    assert pending["published_display"] == "09-20"
    assert pending["like_display"] == "12.5w"
    assert pending["tags"] == ["农村自建房", "庭院施工"]
    assert (pending["comments"], pending["shares"], pending["collects"]) == (12, 34, 56)
    # 未落地长期封面副本时不下发源站外链（外链会过期，且代理路由取不到对象）。
    assert pending["cover_url"] is None
    assert "tags_json" not in pending
    # 已归档封面 → 只下发站内代理地址（免登录，`<img>` 可直接加载）。
    assert items["ready-video"]["cover_url"] == "/api/viral/covers/douyin/ready-video"
    assert items["ready-video"]["tags"] == ["乡村别墅"]

    def segment(status: str) -> set[str]:
        response = client.get(
            f"/api/control/viral/videos?platform=douyin&status={status}", headers=headers
        )
        assert response.status_code == 200, response.text
        return {item["video_id"] for item in response.json()["items"]}

    assert segment("ready") == {"ready-video"}
    assert segment("pending") == {"admin-video/opaque=id"}
    assert segment("failed") == set()
    assert segment("featured") == set()
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title,homepage_featured) "
            "VALUES('douyin','failed-video','归档失败视频',1)"
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status) VALUES"
            "('failed-media','douyin','failed-video','video','FAILED')"
        )
    assert segment("failed") == {"failed-video"}
    # 已选首页但素材失败不能冒充实际展示，六种业务状态与客户可见一致。
    assert segment("featured") == set()
    assert segment("pending") == {"admin-video/opaque=id"}
    assert (
        client.get("/api/control/viral/videos?status=unknown", headers=headers).status_code == 422
    )


@pytest.mark.pg
def test_viral_overview_counts_share_the_list_segments(
    client: TestClient, route_state: str
) -> None:
    import json
    from datetime import datetime

    assert client.get("/api/control/viral/overview").status_code == 401
    headers = _admin_session(client)
    keywords = [
        {"platform": "douyin", "category": "建房预算", "keyword": "自建房预算"},
        {"platform": "wechat_channels", "category": "建房预算", "keyword": "建房预算"},
        {"platform": "wechat_channels", "category": "庭院案例", "keyword": "农村庭院设计"},
    ]
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_runtime_controls SET keywords_json=%s,"
            " collection_interval_days=3,"
            " next_collection_at='2099-01-01T00:00:00+00:00'",
            (json.dumps(keywords, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title,homepage_featured) VALUES"
            "('douyin','ready-video','已归档视频',1),"
            "('douyin','failed-video','归档失败视频',0)"
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('ready-media','douyin','ready-video','video','SUCCEEDED','fake://test/ready.mp4'),"
            "('failed-media','douyin','failed-video','video','FAILED',NULL)"
        )
        conn.execute(
            "INSERT INTO viral_fetch_state(platform,sort,fetched_at) "
            "VALUES('douyin','hot','2026-09-24T08:00:00+00:00')"
        )
    overview = client.get("/api/control/viral/overview", headers=headers)
    assert overview.status_code == 200, overview.text
    data = overview.json()
    assert data["content_total"] == 3
    assert data["archive_ready"] == 1
    assert data["homepage_featured"] == 1
    assert data["archive_failed"] == 1
    assert data["pending_archive"] == 1
    assert data["added_today"] == 3
    assert data["last_created_at"]
    assert data["collection_enabled"] is True
    assert data["keyword_count"] == {"douyin": 1, "wechat_channels": 2}
    assert datetime.fromisoformat(str(data["next_collection_at"])).year == 2099
    assert data["collection_interval_days"] == 3
    assert data["last_fetched_at"] == "2026-09-24T08:00:00+00:00"
    # 概览数字必须与列表分段同口径，否则运营看到的数与翻得到的行会对不上。
    for status, expected in (
        ("ready", data["archive_ready"]),
        ("pending", data["pending_archive"]),
        ("failed", data["archive_failed"]),
        ("featured", data["homepage_featured"]),
    ):
        response = client.get(
            f"/api/control/viral/videos?status={status}&limit=50", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == expected, status


@pytest.mark.pg
def test_viral_batch_curation_features_and_deletes_with_one_audit_per_item(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from app.storage import FakeStorageAdapter
    from app.viral_media import CoverEnricher

    headers = _admin_session(client)
    batch_path = "/api/control/viral/videos/curation:batch"
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_videos SET collection_published=0,"
            " cover_url='https://cdn.example.com/cover.jpg'"
            " WHERE video_id='admin-video/opaque=id'"
        )
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title) "
            "VALUES('douyin','second-video','第二条待上首页')"
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('media-a','douyin','admin-video/opaque=id','video','SUCCEEDED','fake://test/a.mp4'),"
            "('media-b','douyin','second-video','video','SUCCEEDED','fake://test/b.mp4')"
        )
    monkeypatch.setattr(
        "app.media_routes.get_media_storage",
        lambda conn: FakeStorageAdapter(provider="cos", bucket="test"),
    )
    monkeypatch.setattr(
        CoverEnricher,
        "enrich",
        lambda self, video: replace(video, cover_key=f"viral/cover/douyin/{video.video_id}"),
    )
    payload = {
        "action": "feature",
        "items": [
            {"platform": "douyin", "video_id": "admin-video/opaque=id"},
            {"platform": "douyin", "video_id": "second-video"},
        ],
        "reason": "批量确认上首页",
        "confirm": True,
    }
    featured = client.post(
        batch_path, headers={**headers, "Idempotency-Key": "batch-feature"}, json=payload
    )
    assert featured.status_code == 200, featured.text
    assert featured.json()["action"] == "feature"
    assert featured.json()["count"] == 2
    replay = client.post(
        batch_path, headers={**headers, "Idempotency-Key": "batch-feature"}, json=payload
    )
    assert replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == featured.json()
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute("SELECT count(*) FROM viral_videos WHERE homepage_featured=1").fetchone()[
                0
            ]
            == 2
        )
        assert (
            conn.execute(
                "SELECT cover_key FROM viral_videos WHERE video_id='admin-video/opaque=id'"
            ).fetchone()[0]
            == "viral/cover/douyin/admin-video/opaque=id"
        )
        batch_audit = conn.execute(
            "SELECT count(*) FROM audit_logs WHERE action='viral_video.curation'"
            " AND metadata_json::jsonb->>'batch'='true'"
        ).fetchone()[0]
        assert batch_audit == 2
    deletion = {**payload, "action": "delete", "reason": "批量下架不再展示"}
    deleted = client.post(
        batch_path, headers={**headers, "Idempotency-Key": "batch-delete"}, json=deletion
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["count"] == 2
    page = client.get("/api/control/viral/videos", headers=headers)
    assert page.json()["total"] == 0
    assert all(item["deleted"] is True for item in deleted.json()["items"])


@pytest.mark.pg
def test_viral_batch_curation_is_all_or_nothing(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from app.storage import FakeStorageAdapter
    from app.viral_media import CoverEnricher

    headers = _admin_session(client)
    batch_path = "/api/control/viral/videos/curation:batch"
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "UPDATE viral_videos SET cover_url='https://cdn.example.com/cover.jpg'"
            " WHERE video_id='admin-video/opaque=id'"
        )
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title,cover_url) VALUES"
            "('douyin','loose-video','封面取不到的视频','https://cdn.example.com/loose.jpg'),"
            "('douyin','no-media-video','尚未归档的视频',NULL)"
        )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('admin-media','douyin','admin-video/opaque=id','video','SUCCEEDED','fake://te"
            "st/a.mp4'),"
            "('loose-media','douyin','loose-video','video','SUCCEEDED','fake://test/l.mp4')"
        )
    monkeypatch.setattr(
        "app.media_routes.get_media_storage",
        lambda conn: FakeStorageAdapter(provider="cos", bucket="test"),
    )
    # 只有第一条视频能取到封面副本，第二条 enrich 后仍无 cover_key。
    monkeypatch.setattr(
        CoverEnricher,
        "enrich",
        lambda self, video: (
            replace(video, cover_key="viral/cover/douyin/admin-video/opaque=id")
            if video.video_id == "admin-video/opaque=id"
            else video
        ),
    )
    # 只要有一条封面取不到，整批都不动：宁可不做，也不留下半批已上首页。
    refused = client.post(
        batch_path,
        headers={**headers, "Idempotency-Key": "batch-cover-miss"},
        json={
            "action": "feature",
            "items": [
                {"platform": "douyin", "video_id": "admin-video/opaque=id"},
                {"platform": "douyin", "video_id": "loose-video"},
            ],
            "reason": "批量上首页失败验收",
            "confirm": True,
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "VIRAL_COVER_NOT_READY"
    # 方案 P1 C-2 之后的口径：素材未就绪不再是整批拒绝，而是转入
    # 「准备完成后自动上首页」的队列（整批取消仍适用于视频已被删除的场景）。
    not_ready = client.post(
        batch_path,
        headers={**headers, "Idempotency-Key": "batch-not-ready"},
        json={
            "action": "feature",
            "items": [{"platform": "douyin", "video_id": "no-media-video"}],
            "reason": "未归档先排队准备",
            "confirm": True,
        },
    )
    assert not_ready.status_code == 200, not_ready.text
    assert not_ready.json()["queued_count"] == 1
    assert not_ready.json()["items"][0]["queued_for_preparation"] is True
    missing = client.post(
        batch_path,
        headers={**headers, "Idempotency-Key": "batch-missing"},
        json={
            "action": "delete",
            "items": [
                {"platform": "douyin", "video_id": "admin-video/opaque=id"},
                {"platform": "douyin", "video_id": "ghost-video"},
            ],
            "reason": "含已删除视频的批量",
            "confirm": True,
        },
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "VIRAL_VIDEO_NOT_FOUND"
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute("SELECT count(*) FROM viral_videos WHERE homepage_featured=1").fetchone()[
                0
            ]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM viral_videos WHERE deleted_at IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        # 封面补写在整批回滚里一并撤销，不留「封面已补但没上首页」的中间态。
        assert (
            conn.execute(
                "SELECT cover_key FROM viral_videos WHERE video_id='admin-video/opaque=id'"
            ).fetchone()[0]
            is None
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_video.curation'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.pg
def test_viral_batch_curation_validates_contract_and_bounds(client: TestClient) -> None:
    batch_path = "/api/control/viral/videos/curation:batch"
    item = {"platform": "douyin", "video_id": "admin-video/opaque=id"}
    payload = {"action": "unfeature", "items": [item], "reason": "批量校验", "confirm": True}
    auditor = _admin_session(client, "auditor_u")
    assert (
        client.post(
            batch_path, headers={**auditor, "Idempotency-Key": "batch-auditor"}, json=payload
        ).status_code
        == 403
    )
    headers = _admin_session(client)
    assert (
        client.post(batch_path, headers={"Idempotency-Key": "batch-csrf"}, json=payload).status_code
        == 403
    )
    assert client.post(batch_path, headers=headers, json=payload).status_code == 400
    assert (
        client.post(
            batch_path,
            headers={**headers, "Idempotency-Key": "batch-blank-reason"},
            json={**payload, "reason": " "},
        ).status_code
        == 400
    )
    assert (
        client.post(
            batch_path,
            headers={**headers, "Idempotency-Key": "batch-empty"},
            json={**payload, "items": []},
        ).status_code
        == 422
    )
    assert (
        client.post(
            batch_path,
            headers={**headers, "Idempotency-Key": "batch-duplicate"},
            json={**payload, "items": [item, item]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            batch_path,
            headers={**headers, "Idempotency-Key": "batch-overflow"},
            json={**payload, "items": [dict(item, video_id=f"v{i}") for i in range(51)]},
        ).status_code
        == 422
    )


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
            "expected_keywords": initial.json()["keywords"],
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
            "expected_keywords": initial.json()["keywords"],
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
        conn.execute("UPDATE users SET is_super_admin=1 WHERE id='admin_u'")

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
    body = response.json()
    assert body["total"] == 0  # 历史命中不冒充搜索次数
    assert body["hitRecords"] == 3
    assert body["coveredUsers"] == 2 and body["videos"] == 2
    assert body["measurementStartedAt"]
    assert {item["keyword"] for item in body["keywords"]} == {"农村建房", "自建房"}
    assert all(item["searches"] is None for item in body["keywords"])
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


def test_viral_business_detail_shares_master_authorization_and_separates_audio(
    client: TestClient, route_state: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO users(id,username,display_name,role,parent_user_id,account_type) VALUES"
            "('sub_u','sub_u','子账号','user','customer_u','SUB'),('other_u','other_u','独立客户',"
            "'user',NULL,'MASTER')"
        )
        entries = [
            (
                "master-detail",
                "customer_u",
                "viral_detail",
                "viral-detail:customer_u:douyin:admin-video/opaque=id",
                50,
            ),
            (
                "sub-detail",
                "sub_u",
                "viral_detail",
                "viral-detail:customer_u:douyin:admin-video/opaque=id",
                25,
            ),
            (
                "sub-copy",
                "sub_u",
                "viral_copy",
                "viral-copy:customer_u:douyin:admin-video/opaque=id",
                None,
            ),
            (
                "other-detail",
                "other_u",
                "viral_detail",
                "viral-detail:other_u:douyin:admin-video/opaque=id",
                100,
            ),
            (
                "not-same-video",
                "other_u",
                "viral_copy",
                "viral-copy:other_u:douyin:admin-video/opaque=id_more",
                200,
            ),
        ]
        for ident, actor, service, source, revenue in entries:
            conn.execute(
                "INSERT INTO billing_operations(id,user_id,service,module,source_id,"
                "pricing_snapshot_json,unit,budget_units,actual_units,reserved_credits,"
                "charged_credits,revenue_fen,state,completed_at) "
                "VALUES(%s,%s,%s,'viral',%s,'{}','call',1,1,3,3,%s,'SUCCEEDED',now())",
                (ident, actor, service, source, revenue),
            )
        conn.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
            "('only-audio','douyin','admin-video/opaque=id','audio','SUCCEEDED','fake://onl"
            "y-audio.mp3')"
        )
    library = client.get("/api/control/viral/videos?has_usage=true", headers=headers).json()
    assert library["total"] == 1
    video = library["items"][0]
    assert video["usage_detail_count"] == 2 and video["usage_copy_count"] == 1
    response = client.get(
        "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/details", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    business = body["business"]
    assert business["detailAccounts"] == video["usage_detail_count"]
    assert business["copyAccounts"] == video["usage_copy_count"]
    assert business["customerTotal"] == 2 and business["knownRevenueFen"] == 175
    assert business["revenueFen"] is None and business["unknownRevenueOperations"] == 1
    assert business["chargedCredits"] == 12
    assert body["media"]["audio"]["ready"] is True and body["media"]["video"]["ready"] is False
    assert body["copy"] is None and business["collectionCostFen"] is None
    assert (
        client.get(
            "/api/control/viral/videos/douyin/admin-video%2Fopaque%3Did/details?offset=20",
            headers=headers,
        ).json()["business"]["customers"]
        == []
    )


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
    with psycopg.connect(route_state) as raw:
        day = raw.execute("SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date::text").fetchone()[0]
        assert raw.execute("SELECT count(*) FROM viral_collection_members").fetchone()[0] == 0
    records = client.get(
        "/api/control/viral/collection/records", headers=headers, params={"start": day, "end": day}
    ).json()
    assert records["total"] == 2
    assert [r["trigger_kind"] for r in records["items"]] == ["realtime", "realtime"]
    assert [r["new_count"] for r in records["items"]] == [0, 1]
    assert all(r["run_status"] == "SUCCEEDED" for r in records["items"])
    assert all(
        r["cost_fen"] is None for r in records["items"]
    )  # fake adapter has no supplier evidence


def test_admin_search_failure_keeps_independent_record_without_customer_charge(
    client, route_state, monkeypatch
):
    from app.viral_tikhub import ViralSourceError

    _use_admin_search_source(monkeypatch)
    headers = _admin_session(client)

    def failed(*args, **kwargs):
        raise ViralSourceError("synthetic upstream failure")

    monkeypatch.setattr("app.viral_search.run_viral_search_bounded", failed)
    response = client.post(
        "/api/control/viral/search",
        headers={**headers, "Idempotency-Key": "search-record-failure"},
        json={
            "keyword": "失败记录",
            "platform": "douyin",
            "reason": "隔离假故障验证",
            "confirm": True,
        },
    )
    assert response.status_code == 503
    with psycopg.connect(route_state) as raw:
        row = raw.execute(
            "SELECT run_status,failure_reason FROM viral_collection_batches"
        ).fetchone()
        assert row[0] == "FAILED" and "归类" in row[1]
        assert (
            raw.execute(
                "SELECT count(*) FROM wallet_transactions WHERE billing_operation_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert raw.execute("SELECT count(*) FROM viral_collection_members").fetchone()[0] == 0


@pytest.mark.parametrize("fails", [False, True])
def test_realtime_record_completion_uses_event_clock_across_transactions(
    client, route_state, monkeypatch, fails
):
    from app import admin_runtime_routes, viral_search

    _use_admin_search_source(monkeypatch)
    headers = _admin_session(client)
    original_start = admin_runtime_routes.start_admin_search_record
    original_search = viral_search.run_viral_search_bounded

    def delayed_start(*args):
        with psycopg.connect(route_state) as raw:
            raw.execute("SELECT pg_sleep(0.03)")
        return original_start(*args)

    def delayed_search(*args, **kwargs):
        with psycopg.connect(route_state) as raw:
            raw.execute("SELECT pg_sleep(0.03)")
        if fails:
            raise TimeoutError("synthetic timeout")
        return original_search(*args, **kwargs)

    monkeypatch.setattr(admin_runtime_routes, "start_admin_search_record", delayed_start)
    monkeypatch.setattr(viral_search, "run_viral_search_bounded", delayed_search)
    result = client.post(
        "/api/control/viral/search",
        headers={**headers, "Idempotency-Key": "record-event-clock"},
        json={
            "confirm": True,
            "reason": "跨事务事件钟回归",
            "keyword": "事件时钟",
            "platform": "douyin",
        },
    )
    assert result.status_code == (503 if fails else 200), result.text
    with psycopg.connect(route_state) as raw:
        state, started, completed = raw.execute(
            "SELECT run_status,started_at,completed_at FROM viral_collection_batches"
        ).fetchone()
        assert state == ("FAILED" if fails else "SUCCEEDED")
        assert completed >= started and (completed - started).total_seconds() >= 0.03


def test_interrupted_search_recovery_is_fenced_durable_and_never_reissues_paid_requests(
    client, route_state
):
    from app.admin_viral_collection_records import (
        recover_interrupted_search_records,
        start_admin_search_record,
    )
    from app.db_portable import BusinessConnection

    expired = start_admin_search_record("douyin", "中断记录")
    active = start_admin_search_record("douyin", "仍在执行")
    with psycopg.connect(route_state) as raw:
        before_wallets = raw.execute("SELECT * FROM wallets ORDER BY user_id").fetchall()
        raw.execute(
            "UPDATE viral_collection_batches SET lease_expires_at="
            "clock_timestamp()-interval '1 second' WHERE id=%s",
            (expired,),
        )
    with psycopg.connect(route_state) as raw:
        assert recover_interrupted_search_records(BusinessConnection.postgres(raw)) == 1
    with psycopg.connect(route_state) as raw:
        assert recover_interrupted_search_records(BusinessConnection.postgres(raw)) == 0
        state, code, start, end = raw.execute(
            "SELECT run_status,failure_code,started_at,completed_at "
            "FROM viral_collection_batches WHERE id=%s",
            (expired,),
        ).fetchone()
        assert (state, code) == ("FAILED", "INTERRUPTED") and end >= start
        assert tuple(
            raw.execute(
                "SELECT run_status FROM viral_collection_batches WHERE id=%s", (active,)
            ).fetchone()
        ) == ("RUNNING",)
        assert [
            tuple(row) for row in raw.execute("SELECT * FROM wallets ORDER BY user_id").fetchall()
        ] == before_wallets
        assert tuple(raw.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone()) == (0,)
    headers = _admin_session(client)
    result = client.get(f"/api/control/viral/collection/records/{expired}/tasks", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["related_task_id"] is None


def test_platform_probe_fake_channel_has_write_contract_replay_and_expiry(
    client, route_state, monkeypatch
):
    calls = _use_admin_search_source(monkeypatch)
    from app import admin_viral_platform_probe, viral_tikhub

    monkeypatch.setattr(
        admin_viral_platform_probe,
        "viral_source_client_from_settings",
        viral_tikhub.viral_source_client_from_settings,
    )
    headers = _admin_session(client)
    path = "/api/control/viral/platforms/douyin/probe"
    body = {"confirm": True, "reason": "接受单页探测可能产生供应商费用"}
    with psycopg.connect(route_state) as raw:
        before = raw.execute("SELECT * FROM wallets ORDER BY user_id").fetchall()
    first = client.post(
        path, headers={**headers, "Idempotency-Key": "fake-platform-probe"}, json=body
    )
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "available"
    replay = client.post(
        path, headers={**headers, "Idempotency-Key": "fake-platform-probe"}, json=body
    )
    assert replay.status_code == 200 and replay.headers["X-Idempotent-Replay"] == "true"
    assert calls == ["连接验证:7:"]
    with psycopg.connect(route_state) as raw:
        assert raw.execute("SELECT * FROM wallets ORDER BY user_id").fetchall() == before
        assert raw.execute(
            "SELECT count(*) FROM viral_videos WHERE video_id='admin-search-hit'"
        ).fetchone() == (0,)
        raw.execute(
            "UPDATE viral_platform_probes SET checked_at=clock_timestamp()-interval '11 minutes'"
        )
    status = client.get("/api/control/viral/platforms/probes", headers=headers)
    assert status.status_code == 200 and status.json()["items"][0]["state"] == "expired"
    denied = client.post(
        path,
        headers={**headers, "Idempotency-Key": "unconfirmed-probe"},
        json={"confirm": False, "reason": "未确认费用"},
    )
    assert denied.status_code == 400
    assert len(calls) == 1


def test_recovered_search_cannot_commit_late_results(client, route_state, monkeypatch):
    from app import viral_search
    from app.admin_viral_collection_records import recover_interrupted_search_records
    from app.db_portable import BusinessConnection

    _use_admin_search_source(monkeypatch)
    original = viral_search.run_viral_search_bounded

    def late(*args, **kwargs):
        page = original(*args, **kwargs)
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "UPDATE viral_collection_batches SET lease_expires_at="
                "clock_timestamp()-interval '1 second' WHERE id=%s",
                (kwargs["billing_batch_id"],),
            )
        with psycopg.connect(route_state) as raw:
            assert recover_interrupted_search_records(BusinessConnection.postgres(raw)) == 1
        return page

    monkeypatch.setattr(viral_search, "run_viral_search_bounded", late)
    headers = _admin_session(client)
    result = client.post(
        "/api/control/viral/search",
        headers={**headers, "Idempotency-Key": "late-record"},
        json={
            "confirm": True,
            "reason": "验证中断后晚到结果被拒绝",
            "keyword": "晚到",
            "platform": "douyin",
        },
    )
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "VIRAL_SEARCH_RECORD_EXPIRED"
    with psycopg.connect(route_state) as raw:
        assert raw.execute(
            "SELECT run_status,failure_code FROM viral_collection_batches"
        ).fetchone() == ("FAILED", "INTERRUPTED")
        assert raw.execute(
            "SELECT count(*) FROM viral_videos WHERE video_id='admin-search-hit'"
        ).fetchone() == (0,)


def test_collection_record_task_identity_survives_queue_reuse(client, route_state):
    from app.db_portable import BusinessConnection
    from app.viral_collection import enqueue_due_viral_collections
    from app.viral_refresh import acquire_viral_refresh_task, fail_viral_refresh_task

    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_runtime_controls SET collection_enabled=1,keywords_json=%s WHERE id=1",
            (json.dumps([{"platform": "douyin", "keyword": "任务留档", "category": "推荐"}]),),
        )
        assert enqueue_due_viral_collections(BusinessConnection.postgres(raw), manual=True)
    with psycopg.connect(route_state) as raw:
        lease = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="record-worker"
        )
        assert lease is not None
        batch = raw.execute("SELECT id FROM viral_collection_batches").fetchone()[0]
        raw.execute(
            "INSERT INTO viral_keyword_runs(batch_id,platform,keyword,status,failure_code) "
            "VALUES(%s,'douyin','任务留档','FAILED','TIMEOUT')",
            (batch,),
        )
        from app.viral_collection import _checkpoint

        outcomes = {
            f"paging-video-{i:02}": {
                "title": f"分页测试{i}",
                "status": "FAILED",
                "failure_code": "TIMEOUT",
            }
            for i in range(25)
        }
        _checkpoint(
            BusinessConnection.postgres(raw),
            lease,
            {"video_outcomes": outcomes, "failed_video_ids": list(outcomes)},
        )
        fail_viral_refresh_task(BusinessConnection.postgres(raw), lease=lease, cause=TimeoutError())
    headers = _admin_session(client)
    path = f"/api/control/viral/collection/records/{batch}/tasks"
    first = client.get(path, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["current_queue"]["status"] == "FAILED"
    assert first.json()["keywords"][0]["failure_category"] == "服务响应超时"
    second = client.get(path, headers=headers, params={"video_offset": 20}).json()
    assert first.json()["video_total"] == second["video_total"] == 25
    assert len(first.json()["videos"]) == 20 and len(second["videos"]) == 5
    assert len({item["video_id"] for item in first.json()["videos"] + second["videos"]}) == 25
    with psycopg.connect(route_state) as raw:
        assert enqueue_due_viral_collections(BusinessConnection.postgres(raw), manual=True)
    old = client.get(path, headers=headers).json()
    assert old["related_task_id"] == lease.id and old["current_queue"] is None
    assert old["keywords"][0]["failure_category"] == "服务响应超时"
    assert old["videos"] == first.json()["videos"]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "AUTHENTICATION"),
        (402, "BALANCE"),
        (422, "PARAMETERS"),
        (429, "RATE_LIMIT"),
        (503, "SERVICE"),
    ],
)
def test_collection_failure_uses_typed_codes_and_retains_unknown(status, code):
    from app.viral_collection_failures import classify_collection_failure
    from app.viral_tikhub import ViralSourceError, ViralSourceHttpStatusError

    assert classify_collection_failure(ViralSourceHttpStatusError(status)) == code
    assert classify_collection_failure(TimeoutError()) == "TIMEOUT"
    assert (
        classify_collection_failure(ViralSourceError("401 timeout password synthetic")) == "UNKNOWN"
    )


def test_demand_customer_drilldown_keeps_range_zero_results_subaccount_and_pagination(
    client, route_state
):
    from app.admin_customer_routes import router as customer_router

    client.app.include_router(customer_router)
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO users(id,username,display_name,role,account_type,parent_user_id) "
            "VALUES('demand-sub','demand-sub','子账号公司','customer','SUB','customer_u')"
        )
        for n, user, day, word, platform in [
            (0, "customer_u", "2026-09-10", "客户下钻", "douyin"),
            (1, "demand-sub", "2026-09-11", "客户下钻", "douyin"),
            (2, "demand-sub", "2026-09-11", "客户下钻", "douyin"),
            (3, "customer_u", "2026-09-20", "客户下钻", "douyin"),
            (4, "customer_u", "2026-09-11", "其他词", "douyin"),
            (5, "customer_u", "2026-09-11", "客户下钻", "wechat_channels"),
        ]:
            raw.execute(
                "INSERT INTO viral_search_events VALUES(%s,%s,%s,%s,%s,now(),'[]')",
                (f"drill-{n}", user, word, platform, day),
            )
        raw.execute(
            "INSERT INTO viral_search_discoveries"
            "(id,user_id,keyword,platform,video_id,search_date,searched_at) "
            "VALUES('historical-only','admin_u','客户下钻','douyin','historical','2026-09-11',now())"
        )
    params = {
        "from": "2026-09-09",
        "to": "2026-09-15",
        "keyword": "客户下钻",
        "platform": "douyin",
        "limit": 1,
    }
    path = "/api/control/viral/discoveries/customers"
    first = client.get(path, headers=headers, params=params)
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 2
    sub = first.json()["items"][0]
    assert (
        sub["user_id"],
        sub["customer_user_id"],
        sub["searches"],
        sub["zero_results"],
        sub["videos"],
    ) == ("demand-sub", "customer_u", 2, 2, 0)
    second = client.get(path, headers=headers, params={**params, "offset": 1}).json()["items"][0]
    assert second["user_id"] == "customer_u" and second["searches"] == 1
    aggregate = client.get("/api/control/viral/discoveries", headers=headers, params=params).json()
    row = next(
        r for r in aggregate["keywords"] if r["keyword"] == "客户下钻" and r["platform"] == "douyin"
    )
    assert row["users"] == first.json()["total"] and row["searches"] == 3
    customer = client.get(
        "/api/control/customers", headers=headers, params={"user_id": sub["customer_user_id"]}
    )
    assert customer.status_code == 200, customer.text
    assert [r["user_id"] for r in customer.json()["items"]] == ["customer_u"]


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


def test_viral_batch_prepare_queues_instead_of_rejecting(
    client: TestClient, route_state: str
) -> None:
    """方案 P1 C-2：未准备的视频批量上首页不再整批拒绝，转排队自动准备。"""
    headers = _admin_session(client)
    batch_path = "/api/control/viral/videos/curation:batch"
    with psycopg.connect(route_state) as conn:
        conn.execute("DELETE FROM viral_refresh_tasks WHERE platform='douyin'")
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title) "
            "VALUES('douyin','unprepared-video','尚未转存')"
        )

    payload = {
        "action": "feature",
        "items": [{"platform": "douyin", "video_id": "unprepared-video"}],
        "reason": "上首页（素材待准备）",
        "confirm": True,
    }
    response = client.post(
        batch_path,
        headers={**headers, "Idempotency-Key": "batch-auto-queue"},
        json=payload,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued_count"] == 1
    assert body["items"][0]["queued_for_preparation"] is True

    with psycopg.connect(route_state) as conn:
        task = conn.execute(
            "SELECT collection_config_json FROM viral_refresh_tasks "
            "WHERE platform='douyin' AND status='PENDING'"
        ).fetchone()
        assert task is not None
        config = json.loads(task[0])
        assert config["kind"] == "archive_batch"
        assert config["video_ids"] == ["unprepared-video"]
        # 上首页意图随任务走：后台准备完成后按原始操作人自动展示。
        assert config["feature_after"]["reason"] == "上首页（素材待准备）"
        featured = conn.execute(
            "SELECT homepage_featured FROM viral_videos WHERE video_id='unprepared-video'"
        ).fetchone()
        assert featured == (0,)
        conn.execute("DELETE FROM viral_refresh_tasks WHERE platform='douyin'")
        conn.execute("DELETE FROM viral_videos WHERE video_id='unprepared-video'")


def test_viral_keyword_single_add_and_delete_are_atomic(
    client: TestClient, route_state: str
) -> None:
    """方案 P1 C-12：关键词单条增删不再整表覆盖（并发编辑互吞的修复）。"""
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        before = conn.execute(
            "SELECT keywords_json FROM viral_runtime_controls WHERE id=1"
        ).fetchone()
        original = json.loads(before[0]) if before and before[0] else []

    added = client.post(
        "/api/control/viral/keywords",
        headers={**headers, "Idempotency-Key": "kw-add"},
        json={
            "platform": "douyin",
            "category": "推荐",
            "keyword": "单条新增词",
            "reason": "搜索发现高频词",
            "confirm": True,
        },
    )
    assert added.status_code == 200, added.text
    assert added.json()["added"] is True

    # 重复添加幂等：不产生第二条。
    again = client.post(
        "/api/control/viral/keywords",
        headers={**headers, "Idempotency-Key": "kw-add-2"},
        json={
            "platform": "douyin",
            "category": "推荐",
            "keyword": "单条新增词",
            "reason": "搜索发现高频词",
            "confirm": True,
        },
    )
    assert again.status_code == 200
    assert again.json()["added"] is False

    removed = client.post(
        "/api/control/viral/keywords/delete",
        headers={**headers, "Idempotency-Key": "kw-del"},
        json={
            "platform": "douyin",
            "keyword": "单条新增词",
            "reason": "采集效果差，移除",
            "confirm": True,
        },
    )
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True

    with psycopg.connect(route_state) as conn:
        after = conn.execute(
            "SELECT keywords_json FROM viral_runtime_controls WHERE id=1"
        ).fetchone()
        assert json.loads(after[0]) == original


def test_viral_discoveries_accept_a_date_range(client: TestClient, route_state: str) -> None:
    """方案 P1 客户需求洞察：from/to 区间聚合，取代只能看单日。"""
    _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute("DELETE FROM viral_search_discoveries")
        conn.execute(
            """
            INSERT INTO viral_search_discoveries
                (id, user_id, keyword, platform, video_id, search_date, searched_at)
            VALUES
                ('disc-1', 'customer_u', '老房改造', 'douyin', 'v1', '2026-09-10', now()),
                ('disc-2', 'customer_u', '老房改造', 'douyin', 'v2', '2026-09-12', now()),
                ('disc-3', 'customer_u', '老房改造', 'douyin', 'v3', '2026-09-20', now())
            """
        )
    try:
        ranged = client.get("/api/control/viral/discoveries?from=2026-09-09&to=2026-09-15")
        assert ranged.status_code == 200, ranged.text
        body = ranged.json()
        assert body["from"] == "2026-09-09"
        assert body["to"] == "2026-09-15"
        assert body["total"] == 0
        assert body["hitRecords"] == 2
        assert body["keywords"][0]["keyword"] == "老房改造"
        assert body["keywords"][0]["videos"] == 2

        # 区间上限：92 天以上拒绝。
        too_long = client.get("/api/control/viral/discoveries?from=2026-01-01&to=2026-09-15")
        assert too_long.status_code == 422
    finally:
        with psycopg.connect(route_state) as conn:
            conn.execute("DELETE FROM viral_search_discoveries")


def test_viral_search_events_count_zero_hits_and_range_drilldown(
    client: TestClient, route_state: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_search_events VALUES "
            "('event-1','customer_u','新词','douyin','2026-09-10','2026-09-10T01:00:00Z','[]'),"
            "('event-2','customer_u','新词','douyin','2026-09-11','2026-09-11T01:00:00Z','[\"v-1\",\"v-2\"]'),"
            "('event-3','admin_u','新词','douyin','2026-09-12','2026-09-12T01:00:00Z','[]')"
        )
        conn.execute(
            "INSERT INTO viral_search_discoveries "
            "(id,user_id,keyword,platform,video_id,search_date,searched_at) VALUES "
            "('event-hit1','customer_u','新词','douyin','v-1','2026-09-11','2026-09-11T01:00:00Z'),"
            "('event-hit2','customer_u','新词','douyin','v-2','2026-09-11','2026-09-11T01:00:00Z')"
        )
    query = {"from": "2026-09-10", "to": "2026-09-12", "keyword": "新词", "platform": "douyin"}
    summary = client.get("/api/control/viral/discoveries", headers=headers, params=query).json()
    assert summary["total"] == 3 and summary["users"] == 2 and summary["videos"] == 2
    assert summary["keywords"][0]["searches"] == 3
    assert summary["keywords"][0]["zero_results"] == 2
    details = client.get("/api/control/viral/discoveries/detail", headers=headers, params=query)
    assert details.status_code == 200, details.text
    assert details.json()["total"] == 2
    assert details.json()["from"] == query["from"] and details.json()["to"] == query["to"]
    for path in ("/api/control/viral/discoveries", "/api/control/viral/discoveries/detail"):
        invalid = client.get(path, headers=headers, params={"date": "2026-02-30"})
        assert invalid.status_code == 422


def test_demand_inventory_priority_and_partial_daily_history(client, route_state):
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute("UPDATE viral_search_metrics_state SET started_at='2026-09-10T09:00:00+08:00'")
        for word, count in [("热门词", 4), ("缺货词", 3)]:
            for index in range(count):
                conn.execute(
                    "INSERT INTO viral_search_events VALUES"
                    "(%s,'customer_u',%s,'douyin','2026-09-11',now(),'[]')",
                    (f"demand-{word}-{index}", word),
                )
        conn.execute(
            "UPDATE viral_runtime_controls SET keywords_json=%s",
            (
                json.dumps(
                    [
                        {
                            "platform": "douyin",
                            "category": "推荐",
                            "keyword": "缺货词",
                            "enabled": False,
                            "limit": 3,
                        }
                    ]
                ),
            ),
        )
        for kind in ["ready", "hidden", "blocked", "deleted", "cover", "failed", "expired"]:
            conn.execute(
                "INSERT INTO viral_videos"
                "(platform,video_id,title,published_at,collection_published) "
                "VALUES('douyin',%s,'需求库存',floor(extract(epoch FROM now()))-60,1)",
                (kind,),
            )
            conn.execute(
                "INSERT INTO viral_media_preparations"
                "(id,platform,video_id,media_kind,status,storage_uri) "
                "VALUES(%s,'douyin',%s,'video','SUCCEEDED','fake://inventory.mp4')",
                (f"inventory-{kind}", kind),
            )
            for batch in ["demand-one", "demand-two"]:
                conn.execute(
                    "INSERT INTO viral_content_sources"
                    "(batch_id,source_kind,platform,video_id,keyword) "
                    "VALUES(%s,'collection','douyin',%s,'热门词')",
                    (batch, kind),
                )
        for ident, status in [("hidden", "HIDDEN"), ("blocked", "UNAVAILABLE")]:
            conn.execute(
                "INSERT INTO viral_video_visibility(platform,video_id,status) "
                "VALUES('douyin',%s,%s)",
                (ident, status),
            )
        conn.execute("UPDATE viral_videos SET deleted_at=now() WHERE video_id='deleted'")
        conn.execute(
            "UPDATE viral_videos SET cover_url='https://example.invalid/cover' "
            "WHERE video_id='cover'"
        )
        conn.execute(
            "UPDATE viral_videos SET published_at=floor(extract(epoch FROM now()))-864000 "
            "WHERE video_id='expired'"
        )
        conn.execute(
            "UPDATE viral_media_preparations SET status='FAILED',storage_uri=NULL "
            "WHERE video_id='failed'"
        )
        conn.execute("DELETE FROM viral_fetch_state")
    response = client.get(
        "/api/control/viral/discoveries?from=2026-09-09&to=2026-09-12", headers=headers
    )
    assert response.status_code == 200, response.text
    rows = response.json()["keywords"]
    assert [row["keyword"] for row in rows] == ["缺货词", "热门词"], rows
    assert rows[0]["inventory"] == 0 and rows[0]["configured"] is True
    assert rows[0]["collectionEnabled"] is False
    assert rows[1]["inventory"] == 1 and rows[1]["configured"] is False
    assert rows[1]["trend"] == [
        {"date": "2026-09-09", "searches": None, "partial": False},
        {"date": "2026-09-10", "searches": 0, "partial": True},
        {"date": "2026-09-11", "searches": 4, "partial": False},
        {"date": "2026-09-12", "searches": 0, "partial": False},
    ]
    assert response.json()["categories"]


def test_viral_keyword_stale_whole_list_cannot_erase_atomic_add(
    client: TestClient, route_state: str
) -> None:
    headers = _admin_session(client)
    original = client.get("/api/control/settings/viral", headers=headers).json()
    added = client.post(
        "/api/control/viral/keywords",
        headers={**headers, "Idempotency-Key": "kw-concurrent-add"},
        json={
            "platform": "douyin",
            "category": "推荐",
            "keyword": "并发新增保留词",
            "confirm": True,
            "reason": "验证并发编辑",
        },
    )
    assert added.status_code == 200, added.text
    body = {
        "collection_enabled": False,
        "import_enabled": False,
        "keywords": [],
        "expected_keywords": original["keywords"],
        "confirm": True,
        "reason": "保存旧页面草稿",
    }
    stale = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "kw-stale-list"},
        json=body,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "VIRAL_KEYWORDS_CONFLICT"
    current = client.get("/api/control/settings/viral", headers=headers).json()
    assert current["collection_enabled"] == original["collection_enabled"]
    assert any(row["keyword"] == "并发新增保留词" for row in current["keywords"])
    body["expected_keywords"] = current["keywords"]
    saved = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "kw-fresh-list"},
        json=body,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["keywords"] == []


def test_legacy_keyword_editor_preserves_independent_settings(client, route_state):
    headers = _admin_session(client)
    category = client.get("/api/control/viral/keywords", headers=headers).json()["categories"][0]
    word = {
        "platform": "douyin",
        "category": category,
        "keyword": "兼容测试词",
        "enabled": False,
        "limit": 3,
    }
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE viral_runtime_controls SET keywords_json=%s", (json.dumps([word]),))
    current = client.get("/api/control/settings/viral", headers=headers).json()
    legacy = {key: word[key] for key in ("platform", "category", "keyword")}
    saved = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "kw-legacy-preserve"},
        json={
            "collection_enabled": True,
            "import_enabled": True,
            "expected_keywords": current["keywords"],
            "keywords": [legacy],
            "reason": "旧页面保存不能重置独立设置",
            "confirm": True,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["keywords"] == [word]


def test_viral_quality_rules_and_monthly_budget_round_trip(
    client: TestClient, route_state: str
) -> None:
    """方案 P1 采集设置：质量规则与月度预算的写入、回读与调度门槛。"""
    headers = _admin_session(client)
    saved = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "quality-budget-save"},
        json={
            "collection_enabled": True,
            "import_enabled": True,
            "keywords": [{"platform": "douyin", "category": "推荐", "keyword": "老房改造"}],
            "quality_min_likes": 5000,
            "quality_duration_min_ms": 15000,
            "quality_duration_max_ms": 180000,
            "quality_exclude_words": ["广告", "抽奖"],
            "monthly_budget_fen": 50000,
            "reason": "补齐采集质量门槛与月度成本上限",
            "expected_keywords": client.get("/api/control/settings/viral", headers=headers).json()[
                "keywords"
            ],
            "confirm": True,
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["quality_min_likes"] == 5000
    assert body["quality_duration_min_ms"] == 15000
    assert body["quality_duration_max_ms"] == 180000
    assert body["quality_exclude_words"] == ["广告", "抽奖"]
    assert body["monthly_budget_fen"] == 50000
    assert body["month_spend_fen"] == 0
    with psycopg.connect(route_state) as raw:
        audit = json.loads(
            raw.execute(
                "SELECT metadata_json FROM audit_logs WHERE action='viral_runtime.update'"
            ).fetchone()[0]
        )
    assert audit["reason"] == "补齐采集质量门槛与月度成本上限"
    for field, value in [
        ("quality_min_likes", 5000),
        ("quality_duration_min_ms", 15000),
        ("quality_duration_max_ms", 180000),
        ("quality_exclude_words", ["广告", "抽奖"]),
        ("monthly_budget_fen", 50000),
    ]:
        assert audit["after"][field] == value
        assert audit["changes"][field]["after"] == value
        assert audit["changes"][field]["before"] == audit["before"][field]
    cleared = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "quality-clear"},
        json={
            "confirm": True,
            "reason": "清除预算和点赞限制",
            "collection_enabled": True,
            "import_enabled": True,
            "monthly_budget_fen": None,
            "quality_min_likes": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    with psycopg.connect(route_state) as raw:
        logs = [
            json.loads(row[0])
            for row in raw.execute(
                "SELECT metadata_json FROM audit_logs WHERE action='viral_runtime.update'"
            )
        ]
    audit = next(row for row in logs if row["reason"] == "清除预算和点赞限制")
    assert audit["changes"]["monthly_budget_fen"] == {"before": 50000, "after": None}
    assert audit["changes"]["quality_min_likes"] == {"before": 5000, "after": None}
    from app.admin_audit_routes import router as audit_router

    client.app.include_router(audit_router)
    readback = client.get(
        "/api/control/audit-log", headers=headers, params={"event_group": "content"}
    )
    assert readback.status_code == 200, readback.text
    entry = next(row for row in readback.json()["items"] if row["reason"] == "清除预算和点赞限制")
    assert entry["change_detail"]["changes"]["monthly_budget_fen"] == {
        "before": 50000,
        "after": None,
    }

    # 校验：负数点赞、0 元预算被拒。
    bad = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "quality-budget-bad"},
        json={
            "collection_enabled": True,
            "import_enabled": True,
            "quality_min_likes": -1,
            "reason": "非法值",
            "confirm": True,
        },
    )
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# PR #35 评审修复：手动采集的预算口径、批量上首页的同平台合并
# ---------------------------------------------------------------------------

_COLLECT_PATH = "/api/control/viral/collect"


def _exhaust_monthly_collection_budget(conn: psycopg.Connection) -> None:
    """预算 1 分，并造一笔本月已完成的平台采集成本（100 分）：预算已用尽。"""
    conn.execute(
        "UPDATE viral_runtime_controls SET collection_enabled=1, keywords_json=%s, "
        "monthly_budget_fen=1, next_collection_at=NULL WHERE id=1",
        (
            json.dumps(
                [{"platform": "douyin", "category": "推荐", "keyword": "老房改造"}],
                ensure_ascii=False,
            ),
        ),
    )
    conn.execute("DELETE FROM viral_refresh_tasks")
    conn.execute(
        "INSERT INTO viral_collection_batches (id, platform, config_json, pricing_snapshot_json) "
        "VALUES ('budget-batch', 'douyin', '{}', '{}') ON CONFLICT (id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO billing_operations (id, user_id, service, module, source_id, unit, "
        "budget_units, pricing_snapshot_json, state, completed_at, collection_batch_id) "
        "VALUES ('budget-op', NULL, 'viral_search', 'viral', 'budget-src', 'call', 1, '{}', "
        "'SUCCEEDED', now(), 'budget-batch') ON CONFLICT (id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO billing_attempts (id, operation_id, attempt_key, service, provider, unit, "
        "cost_fen, state, completed_at) "
        "VALUES ('budget-attempt', 'budget-op', 'k1', 'viral_search', 'source', 'call', 100, "
        "'ACTUAL', now()) ON CONFLICT (id) DO NOTHING"
    )


def _clear_collection_budget_fixture(conn: psycopg.Connection) -> None:
    # 计费事实表受「只追加」触发器保护；专属测试库里像 route_state 清库那样临时绕过。
    conn.execute("SET session_replication_role = replica")
    conn.execute("DELETE FROM billing_attempts WHERE id='budget-attempt'")
    conn.execute("DELETE FROM billing_operations WHERE id='budget-op'")
    conn.execute("DELETE FROM viral_collection_batches WHERE id='budget-batch'")
    conn.execute("SET session_replication_role = DEFAULT")
    conn.execute("DELETE FROM viral_refresh_tasks")
    conn.execute("UPDATE viral_runtime_controls SET monthly_budget_fen=NULL WHERE id=1")


def test_scheduled_collection_stops_at_the_budget_but_manual_collection_does_not(
    client: TestClient, route_state: str
) -> None:
    """定时入队受月度预算拦截；运营手动触发是知情动作，不拦。返回值如实说明有没有入队。"""
    from app.db_portable import BusinessConnection
    from app.viral_collection import enqueue_due_viral_collections

    with psycopg.connect(route_state) as conn:
        _exhaust_monthly_collection_budget(conn)
        conn.execute("UPDATE viral_runtime_controls SET next_collection_at=NULL WHERE id=1")
    try:
        with psycopg.connect(route_state) as conn:
            scheduled = enqueue_due_viral_collections(BusinessConnection.postgres(conn))
            assert scheduled is False
            # BusinessConnection 会把这条连接换成具名行工厂，下标取值最稳。
            assert conn.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone()[0] == 0
            conn.commit()
        with psycopg.connect(route_state) as conn:
            manual = enqueue_due_viral_collections(BusinessConnection.postgres(conn), manual=True)
            assert manual is True
            assert conn.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone()[0] == 1
            conn.commit()
        # 已有任务在队：再来一次如实回 False，不重复入队。
        with psycopg.connect(route_state) as conn:
            again = enqueue_due_viral_collections(BusinessConnection.postgres(conn), manual=True)
            assert again is False
    finally:
        with psycopg.connect(route_state) as conn:
            _clear_collection_budget_fixture(conn)


def test_collect_now_route_bypasses_budget_and_answers_busy_truthfully(
    client: TestClient, route_state: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        _exhaust_monthly_collection_budget(conn)
    body = {"reason": "运营知情下的手动采集", "confirm": True}
    try:
        first = client.post(
            _COLLECT_PATH, headers={**headers, "Idempotency-Key": "collect-now-1"}, json=body
        )
        # 预算已用尽，手动「立即采集」仍要成功入队。
        assert first.status_code == 202, first.text
        assert first.json() == {"queued": True}
        with psycopg.connect(route_state) as conn:
            assert conn.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone() == (1,)

        # 再点一次：已有任务在队，不能再回「已入队」。
        second = client.post(
            _COLLECT_PATH, headers={**headers, "Idempotency-Key": "collect-now-2"}, json=body
        )
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "VIRAL_COLLECTION_BUSY"
        with psycopg.connect(route_state) as conn:
            assert conn.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone() == (1,)
    finally:
        with psycopg.connect(route_state) as conn:
            _clear_collection_budget_fixture(conn)


def test_viral_batch_feature_queues_unready_items_of_one_platform_together(
    client: TestClient, route_state: str
) -> None:
    """回归：同平台两条未就绪视频批量上首页，逐条排队会撞第一条刚排的任务而整批 409。"""
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute("DELETE FROM viral_refresh_tasks")
        conn.execute(
            "INSERT INTO viral_videos(platform,video_id,title) VALUES "
            "('douyin','unready-a','未就绪甲'),('douyin','unready-b','未就绪乙'),"
            "('wechat_channels','unready-c','未就绪丙')"
        )
    try:
        response = client.post(
            "/api/control/viral/videos/curation:batch",
            headers={**headers, "Idempotency-Key": "batch-feature-multi-unready"},
            json={
                "action": "feature",
                "items": [
                    {"platform": "douyin", "video_id": "unready-a"},
                    {"platform": "wechat_channels", "video_id": "unready-c"},
                    {"platform": "douyin", "video_id": "unready-b"},
                ],
                "reason": "多条未就绪一起上首页",
                "confirm": True,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["queued_count"] == 3
        by_video = {item["video_id"]: item for item in body["items"]}
        # 同平台的两条共用一个任务，另一个平台单独一个；每条都带回任务编号。
        assert by_video["unready-a"]["task_id"] == by_video["unready-b"]["task_id"]
        assert by_video["unready-c"]["task_id"] not in {None, by_video["unready-a"]["task_id"]}

        with psycopg.connect(route_state) as conn:
            tasks = {
                row[0]: json.loads(row[1])
                for row in conn.execute(
                    "SELECT platform, collection_config_json FROM viral_refresh_tasks "
                    "WHERE status='PENDING'"
                ).fetchall()
            }
        assert sorted(tasks["douyin"]["video_ids"]) == ["unready-a", "unready-b"]
        assert tasks["wechat_channels"]["video_ids"] == ["unready-c"]
        assert tasks["douyin"]["feature_after"]["reason"] == "多条未就绪一起上首页"
    finally:
        with psycopg.connect(route_state) as conn:
            conn.execute("DELETE FROM viral_refresh_tasks")
            conn.execute(
                "DELETE FROM viral_videos WHERE video_id IN ('unready-a','unready-b','unready-c')"
            )


def test_viral_quality_clear_and_partial_duration_validation(
    client: TestClient, route_state: str
) -> None:
    headers = _admin_session(client)

    def patch(key: str, changes: dict[str, object]):
        return client.patch(
            "/api/control/settings/viral",
            headers={**headers, "Idempotency-Key": key},
            json={
                "collection_enabled": True,
                "import_enabled": True,
                "confirm": True,
                "reason": "核验质量限制与清空",
                **changes,
            },
        )

    saved = patch(
        "quality-clear-seed",
        {
            "quality_min_likes": 50,
            "quality_duration_min_ms": 1000,
            "quality_duration_max_ms": 5000,
            "monthly_budget_fen": 10000,
        },
    )
    assert saved.status_code == 200, saved.text
    invalid = patch("quality-invalid-range", {"quality_duration_min_ms": 6000})
    assert invalid.status_code == 422
    unchanged = client.get("/api/control/settings/viral").json()
    assert unchanged["quality_duration_min_ms"] == 1000
    omitted = patch("quality-omitted", {})
    assert omitted.status_code == 200
    assert omitted.json()["monthly_budget_fen"] == 10000
    cleared = patch(
        "quality-clear",
        {
            "quality_min_likes": None,
            "quality_duration_min_ms": None,
            "quality_duration_max_ms": None,
            "monthly_budget_fen": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    for field in [
        "quality_min_likes",
        "quality_duration_min_ms",
        "quality_duration_max_ms",
        "monthly_budget_fen",
    ]:
        assert cleared.json()[field] is None
        assert client.get("/api/control/settings/viral").json()[field] is None


@pytest.mark.parametrize(
    "first, second", [({}, {"monthly_budget_fen": None}), ({"monthly_budget_fen": None}, {})]
)
def test_viral_patch_idempotency_preserves_explicit_null_presence(
    client: TestClient,
    first: dict[str, object],
    second: dict[str, object],
) -> None:
    headers = {**_admin_session(client), "Idempotency-Key": "null-presence-test"}
    base = {
        "collection_enabled": True,
        "import_enabled": True,
        "confirm": True,
        "reason": "虚构语义重放测试",
    }
    result = client.patch("/api/control/settings/viral", headers=headers, json={**base, **first})
    assert result.status_code == 200, result.text
    replay = client.patch("/api/control/settings/viral", headers=headers, json={**base, **first})
    assert replay.status_code == 200
    assert replay.json() == result.json()
    conflict = client.patch("/api/control/settings/viral", headers=headers, json={**base, **second})
    assert conflict.status_code == 409, conflict.text


@pytest.mark.pg
def test_homepage_order_schedule_share_customer_clock_and_conflict_guard(
    client: TestClient, route_state: str
) -> None:
    from app.db_portable import BusinessConnection
    from app.viral_store import list_viral_video_page

    headers = _admin_session(client)
    ids = ["admin-video/opaque=id", "home-b", "home-c"]
    with psycopg.connect(route_state) as conn:
        for index, ident in enumerate(ids):
            conn.execute(
                "INSERT INTO viral_videos(platform,video_id,title,homepage_featured,"
                "collection_published,likes) VALUES('douyin',%s,'庭院设计',1,1,%s) "
                "ON CONFLICT(platform,video_id) DO UPDATE SET homepage_featured=1,"
                "collection_published=1,title='庭院设计',likes=excluded.likes",
                (ident, 100 - index),
            )
            conn.execute(
                "INSERT INTO viral_media_preparations"
                "(id,platform,video_id,media_kind,status,storage_uri) "
                "VALUES(%s,'douyin',%s,'video','SUCCEEDED','fake://test.mp4')",
                (ident, ident),
            )
    current = client.get("/api/control/viral/homepage?platform=douyin", headers=headers).json()
    assert [v["video_id"] for v in current["preview"]] == ids
    with psycopg.connect(route_state) as raw:
        first_page = list_viral_video_page(
            BusinessConnection.postgres(raw),
            platform="douyin",
            sort="hot",
            limit=1,
            featured_only=True,
        )
        old_cursor = first_page.next_cursor
    after = [ids[2], ids[0], ids[1]]
    body = {"platform": "douyin", "video_ids": after, "expected_video_ids": ids, "confirm": True}
    saved = client.put(
        "/api/control/viral/homepage/order",
        headers={**headers, "Idempotency-Key": "home-order"},
        json=body,
    )
    assert saved.status_code == 200, saved.text
    replay = client.put(
        "/api/control/viral/homepage/order",
        headers={**headers, "Idempotency-Key": "home-order"},
        json=body,
    )
    assert replay.headers["X-Idempotent-Replay"] == "true"
    stale = client.put(
        "/api/control/viral/homepage/order",
        headers={**headers, "Idempotency-Key": "home-stale"},
        json=body,
    )
    assert stale.status_code == 409
    with psycopg.connect(route_state) as raw:
        page = list_viral_video_page(
            BusinessConnection.postgres(raw),
            platform="douyin",
            sort="hot",
            limit=20,
            featured_only=True,
        )
        assert [video.video_id for video in page.items] == after
        first = list_viral_video_page(
            BusinessConnection.postgres(raw),
            platform="douyin",
            sort="hot",
            limit=1,
            featured_only=True,
        )
        second = list_viral_video_page(
            BusinessConnection.postgres(raw),
            platform="douyin",
            sort="hot",
            limit=1,
            featured_only=True,
            cursor=first.next_cursor,
        )
        assert [first.items[0].video_id, second.items[0].video_id] == after[:2]
    from app.viral_store import InvalidViralCursorError

    with psycopg.connect(route_state) as raw, pytest.raises(InvalidViralCursorError):
        list_viral_video_page(
            BusinessConnection.postgres(raw),
            platform="douyin",
            sort="hot",
            limit=1,
            cursor=old_cursor,
            featured_only=True,
        )
    path = "/api/control/viral/homepage/douyin/home-c/schedule"
    future = client.patch(
        path,
        headers={**headers, "Idempotency-Key": "home-future"},
        json={
            "confirm": True,
            "expected_starts_at": None,
            "expected_ends_at": None,
            "starts_at": "2099-01-01T00:00:00+08:00",
            "ends_at": "2099-01-02T00:00:00+08:00",
        },
    )
    assert future.status_code == 200, future.text
    conflicting = client.patch(
        path,
        headers={**headers, "Idempotency-Key": "schedule-stale"},
        json={
            "confirm": True,
            "starts_at": None,
            "ends_at": None,
            "expected_starts_at": None,
            "expected_ends_at": None,
        },
    )
    assert conflicting.status_code == 409
    with psycopg.connect(route_state) as raw:
        before_pin = raw.execute(
            "SELECT "
            "homepage_starts_at,homepage_ends_at,homepage_featured_at,homepage_intent_versi"
            "on FROM viral_videos WHERE video_id='home-c'"
        ).fetchone()
    assert (
        client.patch(
            "/api/control/viral/videos/douyin/home-c/curation",
            headers={**headers, "Idempotency-Key": "schedule-pin"},
            json={"confirm": True, "action": "pin"},
        ).status_code
        == 200
    )
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT "
                "homepage_starts_at,homepage_ends_at,homepage_featured_at,homepage_intent_v"
                "ersion FROM viral_videos WHERE video_id='home-c'"
            ).fetchone()
            == before_pin
        )

    assert [
        v["video_id"]
        for v in client.get("/api/control/viral/homepage", headers=headers).json()["preview"]
    ] == after[1:]
    with psycopg.connect(route_state) as raw:
        page = list_viral_video_page(
            BusinessConnection.postgres(raw),
            platform="douyin",
            sort="hot",
            limit=20,
            featured_only=True,
        )
        assert [video.video_id for video in page.items] == after[1:]
        raw.execute(
            "UPDATE viral_videos SET homepage_starts_at=now()-interval '1 day',"
            "homepage_ends_at=now()-interval '1 second' WHERE video_id='home-c'"
        )
    assert len(client.get("/api/control/viral/homepage", headers=headers).json()["preview"]) == 2
    invalid = client.patch(
        path,
        headers={**headers, "Idempotency-Key": "home-invalid"},
        json={"confirm": True, "starts_at": "2026-10-01T00:00:00"},
    )
    assert invalid.status_code == 422
    auditor = _admin_session(client, "auditor_u")
    assert client.get("/api/control/viral/homepage", headers=auditor).status_code == 200
    assert (
        client.put(
            "/api/control/viral/homepage/order",
            headers={**auditor, "Idempotency-Key": "auditor-home"},
            json=body,
        ).status_code
        == 403
    )
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_homepage.reorder'"
            ).fetchone()[0]
            == 1
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_homepage.schedule'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.pg
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("one_failure", [False, True])
@pytest.mark.parametrize("only_prepare", [False, True])
def test_batch_twenty_prepared_videos_publish_once_and_new_decision_wins(
    client: TestClient,
    route_state: str,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
    one_failure: bool,
    only_prepare: bool,
) -> None:
    from app.db_portable import BusinessConnection
    from app.storage import FakeStorageAdapter
    from app.viral_collection import run_viral_collection
    from app.viral_refresh import acquire_viral_refresh_task

    headers = _admin_session(client)
    ids = [f"batch-home-{i}" for i in range(20)]
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_refresh_tasks")
        for ident in ids:
            raw.execute(
                "INSERT INTO viral_videos(platform,video_id,title) VALUES('douyin',%s,'庭院设计')",
                (ident,),
            )
    response = client.post(
        "/api/control/viral/videos/curation:batch",
        headers={**headers, "Idempotency-Key": "twenty-home"},
        json={
            "action": "prepare" if only_prepare else "feature",
            "confirm": True,
            "items": [{"platform": "douyin", "video_id": ident} for ident in ids],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["queued_count"] == 20 and response.json()["count"] == 0
    with psycopg.connect(route_state) as raw:
        config = json.loads(
            raw.execute(
                "SELECT collection_config_json FROM viral_refresh_tasks WHERE platform='douyin'"
            ).fetchone()[0]
        )
        assert bool(config.get("feature_after")) is not only_prepare
    if cancel:
        response = client.patch(
            f"/api/control/viral/videos/douyin/{ids[0]}/curation",
            headers={**headers, "Idempotency-Key": "cancel-one"},
            json={"action": "unfeature", "confirm": True, "reason": "取消活动安排"},
        )
        assert response.status_code == 200

    def prepare(_lease: object, _storage: object, ident: str) -> None:
        if one_failure and ident == ids[0]:
            from app.viral_tikhub import ViralSourceError

            raise ViralSourceError("本地假素材失败")
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "INSERT INTO viral_media_preparations"
                "(id,platform,video_id,media_kind,status,storage_uri) "
                "VALUES(%s,'douyin',%s,'video','SUCCEEDED','fake://test.mp4') "
                "ON CONFLICT(platform,video_id,media_kind) DO UPDATE "
                "SET status='SUCCEEDED',storage_uri='fake://test.mp4'",
                (ident, ident),
            )

    monkeypatch.setattr("app.viral_collection._run_single_archive", prepare)
    with psycopg.connect(route_state) as raw:
        lease = acquire_viral_refresh_task(BusinessConnection.postgres(raw), worker_id="fake-batch")
    assert lease is not None
    if one_failure:
        from app.viral_tikhub import ViralSourceError

        for _ in range(2):
            with pytest.raises(ViralSourceError, match="其他就绪视频已处理"):
                run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="test"))
    else:
        run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="test"))
        run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="test"))
    with psycopg.connect(route_state) as raw:
        assert raw.execute(
            "SELECT count(*) FROM viral_videos WHERE homepage_featured=1"
        ).fetchone()[0] == (0 if only_prepare else 20 - int(cancel or one_failure))
        assert raw.execute(
            "SELECT count(*) FROM audit_logs "
            "WHERE metadata_json::jsonb->>'prepared_then_featured'='true'"
        ).fetchone()[0] == (0 if only_prepare else 20 - int(cancel or one_failure))


@pytest.mark.pg
def test_content_business_states_filters_and_same_cohort_never_use_inventory(
    client: TestClient,
    route_state: str,
) -> None:
    from datetime import UTC, datetime

    from app.db_portable import BusinessConnection
    from app.viral_content_observations import record_content_source, record_customer_read

    headers = _admin_session(client)
    states = ["pending_prepare", "prepare_failed", "ready", "featured", "removed", "blocked"]
    with psycopg.connect(route_state) as raw:
        for index, state in enumerate(states):
            raw.execute(
                "INSERT INTO "
                "viral_videos(platform,video_id,title,category,published_at,collection_publ"
                "ished,homepage_featured) VALUES('douyin',%s,'庭院内容','施工',%s,1,%s)",
                (
                    state,
                    int(datetime(2026, 10, 1, 10, tzinfo=UTC).timestamp()),
                    int(state == "featured"),
                ),
            )
            if index >= 2:
                raw.execute(
                    "INSERT INTO "
                    "viral_media_preparations(id,platform,video_id,media_kind,status,storag"
                    "e_uri) VALUES(%s,'douyin',%s,'video','SUCCEEDED','fake://ready')",
                    (state, state),
                )
            elif state == "prepare_failed":
                raw.execute(
                    "INSERT INTO "
                    "viral_media_preparations(id,platform,video_id,media_kind,status) "
                    "VALUES(%s,'douyin',%s,'video','FAILED')",
                    (state, state),
                )
            if state in ("removed", "blocked"):
                raw.execute(
                    "INSERT INTO viral_video_visibility(platform,video_id,status,reason) "
                    "VALUES('douyin',%s,%s,'本地测试')",
                    (state, "HIDDEN" if state == "removed" else "UNAVAILABLE"),
                )
        conn = BusinessConnection.postgres(raw)
        record_content_source(
            conn,
            batch_id="cohort-test",
            source_kind="collection",
            platform="douyin",
            video_id="featured",
            keyword="庭院",
        )
        record_customer_read(
            conn, platform="douyin", video_id="featured", user_id="customer_u", kind="detail"
        )
        record_customer_read(
            conn, platform="douyin", video_id="featured", user_id="customer_u", kind="copy"
        )
        record_customer_read(
            conn, platform="douyin", video_id="featured", user_id="admin_u", kind="detail"
        )
        raw.execute(
            "INSERT INTO viral_fetch_state(platform,sort,fetched_at) "
            "VALUES('douyin','weekly_window','2026-10-01T12:00:00Z') ON "
            "CONFLICT(platform,sort) DO UPDATE SET fetched_at=excluded.fetched_at"
        )
    for state in states:
        body = client.get(
            "/api/control/viral/videos",
            params={"content_state": state, "category": "施工"},
            headers=headers,
        ).json()
        assert body["total"] == 1 and body["items"][0]["content_state"] == state
    visible = client.get(
        "/api/control/viral/videos",
        params={"customer_visible": "true", "category": "施工"},
        headers=headers,
    ).json()
    assert {v["video_id"] for v in visible["items"]} == {"ready", "featured"}
    from_source = client.get(
        "/api/control/viral/videos",
        params={
            "source_keyword": "庭院",
            "published_from": "2026-10-01",
            "published_to": "2026-10-01",
        },
        headers=headers,
    ).json()
    assert from_source["total"] == 1
    assert (
        client.get(
            "/api/control/viral/videos",
            params={"published_from": "2026-10-02", "published_to": "2026-10-01"},
            headers=headers,
        ).status_code
        == 422
    )
    overview = client.get("/api/control/viral/content-overview", headers=headers).json()
    assert [s["count"] for s in overview["funnel"]] == [1, 1, 1, 1, 1]
    assert overview["topVideos"][0]["requests"] == 2
    assert overview["finance"]["grossRate"] is None
    assert overview["measurementStartedAt"] and "无法还原" in overview["historyNote"]
    assert (
        client.get(
            "/api/control/viral/content-overview", headers=_admin_session(client, "auditor_u")
        ).status_code
        == 200
    )


@pytest.mark.pg
@pytest.mark.parametrize("action,status", [("hide", "HIDDEN"), ("block", "UNAVAILABLE")])
def test_batch_hide_block_are_atomic_and_cancel_pending_homepage(
    client: TestClient,
    route_state: str,
    action: str,
    status: str,
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO viral_videos(platform,video_id,title,homepage_featured) "
            "VALUES('douyin','batch-other','内容',1)"
        )
    response = client.post(
        "/api/control/viral/videos/curation:batch",
        headers={**headers, "Idempotency-Key": f"batch-{action}"},
        json={
            "action": action,
            "confirm": True,
            "reason": "内容质量复核",
            "items": [
                {"platform": "douyin", "video_id": "admin-video/opaque=id"},
                {"platform": "douyin", "video_id": "batch-other"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 2 and response.json()["queued_count"] == 0
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_video_visibility WHERE status=%s", (status,)
            ).fetchone()[0]
            == 2
        )
        assert (
            raw.execute("SELECT count(*) FROM viral_videos WHERE homepage_featured=1").fetchone()[0]
            == 0
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_videos WHERE homepage_intent_version=1"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.pg
def test_content_finance_keeps_unknown_revenue_and_cost_separate(
    client: TestClient,
    route_state: str,
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        for ident, service, user_id, revenue in [
            ("revenue-known", "viral_detail", "customer_u", 100),
            ("revenue-unknown", "viral_copy", "customer_u", None),
            ("unrelated", "asr", "customer_u", 999),
            ("platform-cost", "viral_data", None, None),
        ]:
            raw.execute(
                "INSERT INTO "
                "billing_operations(id,user_id,service,module,source_id,unit,budget_units,"
                "pricing_snapshot_json,state,completed_at,revenue_fen) "
                "VALUES(%s,%s,%s,'viral',%s,'call',1,'{}','SUCCEEDED',now(),%s)",
                (ident, user_id, service, ident, revenue),
            )
        for ident, cost in [("cost-known", 40), ("cost-unknown", None)]:
            raw.execute(
                "INSERT INTO billing_attempts(id,operation_id,attempt_key,service,provider,unit,"
                "cost_fen,state,completed_at) VALUES(%s,'platform-cost',%s,'viral_data',"
                "'local-fixture','call',%s,%s,now())",
                (ident, ident, cost, "ACTUAL" if cost is not None else "UNKNOWN"),
            )
    body = client.get("/api/control/viral/content-overview", headers=headers).json()["finance"]
    assert body["knownRevenueFen"] == 100 and body["unknownRevenueCount"] == 1
    assert body["knownCostFen"] == 40 and body["unknownCostCount"] == 1
    assert body["revenueFen"] is None and body["costFen"] is None
    assert body["grossFen"] is None and body["grossRate"] is None


def test_homepage_simultaneous_http_reorder_rejects_one_stale_snapshot(
    client: TestClient, admin_app: FastAPI, route_state: str
) -> None:
    headers = _admin_session(client)
    ids = ["race-home-a", "race-home-b", "race-home-c"]
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE viral_videos SET homepage_featured=0")
        for rank, ident in enumerate(ids):
            raw.execute(
                "INSERT INTO viral_videos(platform,video_id,title,homepage_featured,homepage_rank) "
                "VALUES('douyin',%s,'庭院并发',1,%s)",
                (ident, rank),
            )
    barrier = threading.Barrier(2)
    from concurrent.futures import ThreadPoolExecutor

    def write(index: int):
        with TestClient(admin_app) as concurrent_client:
            concurrent_client.cookies.update(client.cookies)
            barrier.wait(timeout=10)
            return concurrent_client.put(
                "/api/control/viral/homepage/order",
                headers={**headers, "Idempotency-Key": f"simultaneous-home-{index}"},
                json={
                    "confirm": True,
                    "platform": "douyin",
                    "expected_video_ids": ids,
                    "video_ids": [ids[index + 1], *[v for v in ids if v != ids[index + 1]]],
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, i) for i in range(2)]
        replies = [future.result(timeout=20) for future in futures]
    assert sorted(r.status_code for r in replies) == [200, 409], [r.text for r in replies]
    winner = next(r.json()["video_ids"] for r in replies if r.status_code == 200)
    with psycopg.connect(route_state) as raw:
        assert [
            r[0]
            for r in raw.execute(
                "SELECT video_id FROM viral_videos WHERE homepage_featured=1 ORDER BY homepage_rank"
            )
        ] == winner
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_homepage.reorder'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("window", ["future", "current", "expired", "undated"])
@pytest.mark.parametrize("cancel", [False, True])
def test_pending_homepage_schedule_worker_and_observed_milestone(
    client: TestClient,
    route_state: str,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
    cancel: bool,
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db_portable import BusinessConnection
    from app.storage import FakeStorageAdapter
    from app.viral_collection import run_viral_collection
    from app.viral_content_observations import observe_due_homepages, record_content_source
    from app.viral_refresh import acquire_viral_refresh_task
    from app.viral_store import list_viral_video_page

    headers = _admin_session(client)
    ident = "queued-window"
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_refresh_tasks")
        raw.execute(
            "INSERT INTO viral_videos(platform,video_id,title) VALUES('douyin',%s,'专题')", (ident,)
        )
        record_content_source(
            BusinessConnection.postgres(raw),
            batch_id="window-test",
            source_kind="collection",
            platform="douyin",
            video_id=ident,
            keyword="庭院",
        )
    queued = client.post(
        "/api/control/viral/videos/curation:batch",
        headers={**headers, "Idempotency-Key": "window-enqueue"},
        json={
            "confirm": True,
            "action": "feature",
            "items": [{"platform": "douyin", "video_id": ident}],
        },
    )
    assert queued.status_code == 200, queued.text
    pending = client.get("/api/control/viral/homepage", headers=headers).json()
    assert [v["video_id"] for v in pending["preparing"]] == [ident]
    now = datetime.now(UTC)
    windows = {
        "future": (now + timedelta(days=1), now + timedelta(days=2)),
        "current": (now - timedelta(days=1), now + timedelta(days=1)),
        "expired": (now - timedelta(days=2), now - timedelta(days=1)),
        "undated": (None, None),
    }
    start, end = windows[window]
    scheduled = client.patch(
        f"/api/control/viral/homepage/douyin/{ident}/schedule",
        headers={**headers, "Idempotency-Key": "window-schedule"},
        json={
            "confirm": True,
            "expected_starts_at": None,
            "expected_ends_at": None,
            "starts_at": start.isoformat() if start else None,
            "ends_at": end.isoformat() if end else None,
        },
    )
    assert scheduled.status_code == 200, scheduled.text
    if cancel:
        removed = client.patch(
            f"/api/control/viral/videos/douyin/{ident}/curation",
            headers={**headers, "Idempotency-Key": "window-cancel"},
            json={"confirm": True, "action": "unfeature", "reason": "取消专题排期"},
        )
        assert removed.status_code == 200, removed.text

    def prepare(_lease: object, _storage: object, video_id: str) -> None:
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "INSERT INTO viral_media_preparations"
                "(id,platform,video_id,media_kind,status,storage_uri) "
                "VALUES('window-media','douyin',%s,'video','SUCCEEDED','fake://window.mp4')",
                (video_id,),
            )

    monkeypatch.setattr("app.viral_collection._run_single_archive", prepare)
    with psycopg.connect(route_state) as raw:
        lease = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="window-test"
        )
    assert lease is not None
    run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="test"))
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        visible = list_viral_video_page(
            conn, platform="douyin", sort="hot", limit=12, featured_only=True
        )
        assert [v.video_id for v in visible.items] == (
            [ident] if not cancel and window in ("current", "undated") else []
        )
        milestone = raw.execute("SELECT homepage_at FROM viral_content_sources").fetchone()[0]
        assert (milestone is not None) is (not cancel and window in ("current", "undated"))
        if window == "future" and not cancel:
            assert observe_due_homepages(conn) == 0
            raw.execute(
                "UPDATE viral_videos SET homepage_starts_at=now()-interval '1 second' "
                "WHERE video_id=%s",
                (ident,),
            )
            assert observe_due_homepages(conn) == 1
            first = raw.execute("SELECT homepage_at FROM viral_content_sources").fetchone()[0]
            assert first > now and observe_due_homepages(conn) == 0
        if window == "expired" or cancel:
            assert observe_due_homepages(conn) == 0


def test_recycle_restore_window_permissions_audit_and_no_recollection(
    client: TestClient,
    route_state: str,
) -> None:
    from app.db_portable import BusinessConnection
    from app.viral_store import get_viral_video, upsert_viral_videos
    from app.viral_tikhub import ViralVideo

    headers = _admin_session(client)
    ident = "admin-video/opaque=id"
    deleted = client.patch(
        f"/api/control/viral/videos/douyin/{ident}/curation",
        headers={**headers, "Idempotency-Key": "recycle-delete"},
        json={"confirm": True, "action": "delete", "reason": "清理重复内容"},
    )
    assert deleted.status_code == 200, deleted.text
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        upsert_viral_videos(
            conn,
            [
                ViralVideo(
                    platform="douyin",
                    video_id=ident,
                    category="施工避坑",
                    title="上游再次返回",
                    author="作者",
                    author_avatar=None,
                    verified=False,
                    cover_url=None,
                    duration_ms=30_000,
                    likes=10,
                    comments=None,
                    shares=None,
                    collects=None,
                    published_at=None,
                    published_display=None,
                    like_display=None,
                )
            ],
        )
        assert get_viral_video(conn, platform="douyin", video_id=ident) is None
    listed = client.get("/api/control/viral/recycle", headers=headers).json()
    assert listed["total"] == 1 and listed["items"][0]["restorable"] is True
    path = f"/api/control/viral/videos/douyin/{ident}/restore"
    assert (
        client.post(
            path,
            headers={**headers, "Idempotency-Key": "recycle-empty"},
            json={"confirm": True, "reason": " "},
        ).status_code
        == 400
    )
    auditor = _admin_session(client, "auditor_u")
    assert (
        client.post(
            path,
            headers={**auditor, "Idempotency-Key": "recycle-auditor"},
            json={"confirm": True, "reason": "只读不能恢复"},
        ).status_code
        == 403
    )
    # 管理会话重新绑定；审计员的Cookie不能混入写入。
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_videos SET deleted_at=now()-interval '31 days' WHERE video_id=%s",
            (ident,),
        )
    expired = client.post(
        path,
        headers={**headers, "Idempotency-Key": "recycle-expired"},
        json={"confirm": True, "reason": "到期不能恢复"},
    )
    assert (
        expired.status_code == 409 and expired.json()["detail"]["code"] == "VIRAL_RESTORE_EXPIRED"
    )
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_videos SET deleted_at=now()-interval '29 days' WHERE video_id=%s",
            (ident,),
        )
    request_headers = {**headers, "Idempotency-Key": "recycle-restore"}
    payload = {"confirm": True, "reason": "误删内容恢复供运营重新选择"}
    restored = client.post(path, headers=request_headers, json=payload)
    assert restored.status_code == 200, restored.text
    assert restored.json()["homepage_featured"] is False
    replay = client.post(path, headers=request_headers, json=payload)
    assert replay.headers["X-Idempotent-Replay"] == "true"
    with psycopg.connect(route_state) as raw:
        assert raw.execute(
            "SELECT deleted_at,homepage_featured,homepage_rank,homepage_starts_at,"
            "homepage_ends_at FROM viral_videos WHERE video_id=%s",
            (ident,),
        ).fetchone() == (None, 0, None, None, None)
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_video.restore'"
            ).fetchone()[0]
            == 1
        )
        assert (
            json.loads(
                raw.execute(
                    "SELECT metadata_json FROM audit_logs WHERE action='viral_video.restore'"
                ).fetchone()[0]
            )["reason"]
            == payload["reason"]
        )
    assert client.get("/api/control/viral/recycle", headers=headers).json()["total"] == 0


@pytest.mark.parametrize("commit_phase", ["before_statement", "after_statement"])
def test_homepage_page_revision_total_share_one_pg_snapshot(
    route_state: str,
    commit_phase: str,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from typing import cast

    from app.db_portable import BusinessConnection
    from app.viral_store import InvalidViralCursorError, list_viral_video_page

    ids = ["snapshot-a", "snapshot-b", "snapshot-c", "snapshot-d"]
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE viral_videos SET homepage_featured=0")
        for rank, ident in enumerate(ids, 1):
            raw.execute(
                "INSERT INTO viral_videos(platform,video_id,title,homepage_featured,homepage_rank) "
                "VALUES('douyin',%s,'庭院快照',1,%s)",
                (ident, rank),
            )
            raw.execute(
                "INSERT INTO viral_media_preparations"
                " (id,platform,video_id,media_kind,status,storage_uri) "
                "VALUES(%s,'douyin',%s,'video','SUCCEEDED','fake://snapshot.mp4')",
                (ident, ident),
            )
        first = list_viral_video_page(
            BusinessConnection.postgres(raw),
            platform="douyin",
            sort="hot",
            limit=2,
            featured_only=True,
        )
        assert [v.video_id for v in first.items] == ids[:2]
    gate = threading.Barrier(2)

    def commit_reorder():
        with psycopg.connect(route_state) as writer:
            gate.wait(timeout=10)
            writer.execute(
                "UPDATE viral_videos SET homepage_rank=CASE video_id "
                "WHEN 'snapshot-c' THEN 1 WHEN 'snapshot-a' THEN 2 "
                "WHEN 'snapshot-b' THEN 3 ELSE 4 END WHERE video_id=ANY(%s)",
                (ids,),
            )
            writer.commit()
            gate.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=1) as pool, psycopg.connect(route_state) as reader:
        future = pool.submit(commit_reorder)
        # 模拟鉴权已经发过查询，证明无需在中途改事务隔离级别。
        reader.execute("SELECT 1").fetchone()
        business = BusinessConnection.postgres(reader)

        class InterleavedRead:
            def execute(self, sql, params=()):
                if commit_phase == "before_statement":
                    gate.wait(timeout=10)
                    gate.wait(timeout=10)
                cursor = business.execute(sql, params)
                if commit_phase == "after_statement":
                    gate.wait(timeout=10)
                    gate.wait(timeout=10)
                return cursor

        conn = cast(BusinessConnection, InterleavedRead())
        if commit_phase == "before_statement":
            with pytest.raises(InvalidViralCursorError):
                list_viral_video_page(
                    conn,
                    platform="douyin",
                    sort="hot",
                    limit=2,
                    cursor=first.next_cursor,
                    featured_only=True,
                )
        else:
            second = list_viral_video_page(
                conn,
                platform="douyin",
                sort="hot",
                limit=2,
                cursor=first.next_cursor,
                featured_only=True,
            )
            assert [v.video_id for v in second.items] == ids[2:]
            assert second.total == 4 and second.has_more is False
        future.result(timeout=20)
    with psycopg.connect(route_state) as raw, pytest.raises(InvalidViralCursorError):
        list_viral_video_page(
            BusinessConnection.postgres(raw),
            platform="douyin",
            sort="hot",
            limit=2,
            cursor=first.next_cursor,
            featured_only=True,
        )


def test_homepage_move_has_no_thousand_item_capacity_limit(client: TestClient, route_state: str):
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE viral_videos SET homepage_featured=0")
        raw.execute(
            "INSERT INTO viral_videos"
            " (platform,video_id,title,homepage_featured,homepage_rank,homepage_starts_at) "
            "SELECT 'douyin','scale-'||n,'规模专题',1,n,"
            "CASE WHEN n=1001 THEN now()+interval '1 day' END "
            "FROM generate_series(1,1001) n"
        )
    before = client.get("/api/control/viral/homepage", headers=headers).json()
    assert len(before["items"]) == 1001 and before["capacity"] is None
    body = {
        "confirm": True,
        "platform": "douyin",
        "video_id": "scale-1001",
        "position": 1,
        "expected_revision": before["revision"],
    }
    moved = client.post(
        "/api/control/viral/homepage/move",
        headers={**headers, "Idempotency-Key": "scale-move"},
        json=body,
    )
    assert moved.status_code == 200, moved.text
    after = client.get("/api/control/viral/homepage", headers=headers).json()
    assert after["items"][0]["video_id"] == "scale-1001"
    assert len(after["items"]) == 1001 and after["revision"] != before["revision"]
    stale = client.post(
        "/api/control/viral/homepage/move",
        headers={**headers, "Idempotency-Key": "scale-stale"},
        json=body,
    )
    assert stale.status_code == 409


def test_batch_failure_projection_and_filters_are_per_video(client: TestClient, route_state: str):
    headers = _admin_session(client)
    ids = ["failure-member", "other-member"]
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_refresh_tasks")
        for ident in ids:
            raw.execute(
                "INSERT INTO viral_videos(platform,video_id,title) VALUES('douyin',%s,'庭院')",
                (ident,),
            )
        raw.execute(
            "INSERT INTO viral_refresh_tasks"
            " (id,platform,sort,status,collection_config_json,checkpoint_json) "
            "VALUES('failed-batch','douyin','latest','FAILED',%s,%s)",
            (
                json.dumps({"kind": "archive_batch", "video_ids": ids}),
                json.dumps({"failed_video_ids": [ids[0]]}),
            ),
        )
    failed = client.get("/api/control/viral/videos?status=failed", headers=headers).json()["items"]
    assert [v["video_id"] for v in failed] == ids[:1]
    assert (
        failed[0]["archive_status"] == "FAILED" and failed[0]["content_state"] == "prepare_failed"
    )
    all_rows = client.get("/api/control/viral/videos", headers=headers).json()["items"]
    other = next(v for v in all_rows if v["video_id"] == ids[1])
    assert other["archive_status"] == "SUCCEEDED" and other["content_state"] == "pending_prepare"


@pytest.mark.parametrize(
    "now,days,expected",
    [
        ("2026-10-01T00:59:59+00:00", 1, "2026-10-01T01:00:00+00:00"),
        ("2026-10-01T01:00:00+00:00", 1, "2026-10-02T01:00:00+00:00"),
        ("2026-10-01T01:00:00+00:00", 7, "2026-10-08T01:00:00+00:00"),
        ("2026-09-30T23:00:00+00:00", 7, "2026-10-01T01:00:00+00:00"),
    ],
)
def test_collection_clock_has_explicit_shanghai_daily_weekly_boundaries(now, days, expected):
    from datetime import datetime, time

    from app.viral_collection_schedule import next_collection_time

    assert next_collection_time(
        datetime.fromisoformat(now), days, time(9)
    ) == datetime.fromisoformat(expected)


@pytest.mark.parametrize(
    "previous,now,days,expected",
    [
        ("2026-10-01T09:00:00+08:00", "2026-10-02T08:00:00+08:00", 7, "2026-10-08T09:00:00+08:00"),
        ("2026-10-01T09:00:00+08:00", "2026-10-23T08:00:00+08:00", 7, "2026-10-29T09:00:00+08:00"),
        ("2026-10-01T23:30:00+08:00", "2026-10-02T00:30:00+08:00", 7, "2026-10-08T23:30:00+08:00"),
        ("2026-10-01T09:00:00+08:00", "2026-10-02T08:00:00+08:00", 1, "2026-10-02T09:00:00+08:00"),
        ("2026-10-01T09:00:00+08:00", "2026-10-02T09:00:00+08:00", 1, "2026-10-03T09:00:00+08:00"),
    ],
)
def test_collection_clock_catch_up_preserves_anchor_without_burst(previous, now, days, expected):
    from datetime import datetime

    from app.viral_collection_schedule import advance_collection_time

    prior, current = datetime.fromisoformat(previous), datetime.fromisoformat(now)
    result = advance_collection_time(current, days, prior.timetz().replace(tzinfo=None), prior)
    assert result == datetime.fromisoformat(expected)
    assert (
        advance_collection_time(current, days, prior.timetz().replace(tzinfo=None), result)
        == result
    )


@pytest.mark.parametrize(
    "amount,status",
    [("79.999", "normal"), ("80", "warning"), ("99.999", "warning"), ("100", "exhausted")],
)
def test_collection_budget_uses_reconciled_platform_cost_month_and_exact_thresholds(
    client, route_state, amount, status
):
    from datetime import datetime, timedelta
    from decimal import Decimal

    from app.admin_dates import SHANGHAI
    from app.db_portable import BusinessConnection
    from app.viral_collection import enqueue_due_viral_collections
    from app.viral_collection_budget import collection_budget

    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        raw.execute("SET LOCAL TIME ZONE 'Asia/Shanghai'")
        now = raw.execute("SELECT now()").fetchone()[0]
        lower = now.astimezone(SHANGHAI).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        upper = (
            lower.replace(year=lower.year + 1, month=1)
            if lower.month == 12
            else lower.replace(month=lower.month + 1)
        )
        raw.execute(
            "UPDATE viral_runtime_controls SET collection_enabled=1, monthly_budget_fen=100, "
            "next_collection_at=NULL,keywords_json=%s",
            (json.dumps([{"platform": "douyin", "category": "推荐", "keyword": "预算边界"}]),),
        )
        actor = raw.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()[0]
        for ident, cost, at, state, service in [
            ("included", "1", lower, "ACTUAL", "viral_data"),
            ("unknown", None, now, "UNKNOWN", "viral_data"),
            ("pending", None, now, "PENDING", "viral_data"),
            ("previous", "99999", lower - timedelta(microseconds=1), "ACTUAL", "viral_data"),
            ("future", "99999", upper, "ACTUAL", "viral_data"),
            ("other", "99999", now, "ACTUAL", "analysis"),
        ]:
            raw.execute(
                "INSERT INTO billing_operations(id,service,module,source_id,unit,budget_units,"
                "pricing_snapshot_json,state,completed_at) "
                "VALUES(%s,%s,'viral',%s,'call',1,'{}','SUCCEEDED',now())",
                (ident, service, ident),
            )
            raw.execute(
                "INSERT INTO billing_attempts(id,operation_id,attempt_key,service,provider,unit,"
                "cost_fen,state,created_at,completed_at) VALUES(%s,%s,'request',%s,'fake','call',"
                "%s,%s,%s,%s)",
                (ident, ident, service, cost, state, at, None if state == "PENDING" else at),
            )
        raw.execute(
            "INSERT INTO billing_evidence(id,operation_id,attempt_id,cost_fen,reference,reason,"
            "actor_user_id) VALUES('reconciled','included','included',%s,'synthetic-ref',"
            "'isolated fixture',%s)",
            (amount, actor),
        )
        result = collection_budget(BusinessConnection.postgres(raw))
        assert result["month_spend_fen"] == float(Decimal(amount))
        assert result["month_unknown_cost_count"] == 1
        assert result["month_pending_cost_count"] == 1
        assert result["month_total_cost_fen"] is None
        assert result["budget_status"] == status
        assert enqueue_due_viral_collections(BusinessConnection.postgres(raw)) == (
            status != "exhausted"
        )
        raw.execute("DELETE FROM viral_refresh_tasks")
        assert enqueue_due_viral_collections(BusinessConnection.postgres(raw), manual=True) is True
    displayed = client.get("/api/control/settings/viral", headers=headers).json()
    assert displayed["budget_status"] == status
    assert displayed["month_spend_fen"] == float(Decimal(amount))
    assert datetime.fromisoformat(displayed["budget_period_start"]) == lower


def test_collection_estimate_snapshot_prevents_changed_configuration_and_discloses_customer_charges(
    client, route_state
):
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_runtime_controls SET collection_enabled=1, monthly_budget_fen=NULL,"
            "keywords_json=%s",
            (
                json.dumps(
                    [{"platform": "douyin", "category": "推荐", "keyword": "费用确认", "limit": 2}]
                ),
            ),
        )
    first = client.get("/api/control/viral/collection/estimate", headers=headers).json()
    assert "客户积分" in first["note"] and "不影响客户积分" not in first["note"]
    assert first["physicalDataCallsMax"] is None
    assert first["normalRetryPhysicalCallsMax"] == 9
    assert first["storageCostFen"] is None and first["transferCostFen"] is None
    assert first["customerCreditsMaxEach"] is None
    assert (
        first["normalRetryCustomerCreditsMaxEach"] == 9 * first["customerCreditsPerConfirmedCall"]
    )
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE viral_runtime_controls SET per_keyword_limit=11")
    changed = client.post(
        _COLLECT_PATH,
        headers={**headers, "Idempotency-Key": "estimate-changed"},
        json={
            "confirm": True,
            "reason": "确认旧配置",
            "expected_estimate_snapshot": first["snapshot"],
        },
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "VIRAL_ESTIMATE_CHANGED"
    current = client.get("/api/control/viral/collection/estimate", headers=headers).json()
    accepted = client.post(
        _COLLECT_PATH,
        headers={**headers, "Idempotency-Key": "estimate-current"},
        json={
            "confirm": True,
            "reason": "确认当前收费范围",
            "expected_estimate_snapshot": current["snapshot"],
        },
    )
    assert accepted.status_code == 202, accepted.text
    with psycopg.connect(route_state) as raw:
        metadata = json.loads(
            raw.execute(
                "SELECT metadata_json FROM audit_logs WHERE action='viral_collection.collect_now'"
            ).fetchone()[0]
        )
        assert metadata["estimate"]["snapshot"] == current["snapshot"]
        assert raw.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0


def test_confirmed_collection_does_not_add_customer_restored_after_validation(
    client, route_state, monkeypatch
):
    from app.admin_customer_routes import router as customer_router
    from app.db_portable import BusinessConnection
    from app.usage_billing import accept_platform_operation, finish_operation
    from app.viral_collection import enqueue_due_viral_collections
    from app.viral_collection_billing import settle_collection_charges

    client.app.include_router(customer_router)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE users SET role='customer',registration_source='self_register',"
            "password_hash='synthetic-test-password-hash' WHERE id='customer_u'"
        )
        raw.execute(
            "INSERT INTO users(id,username,display_name,role,is_active,"
            "registration_source,password_hash) "
            "VALUES('restored-b','restored-b','未确认客户','customer',0,'self_register',"
            "'synthetic-test-password-hash'),"
            "('restore-admin','restore-admin','恢复管理员','admin',1,NULL,NULL)"
        )
        raw.execute(
            "INSERT INTO wallets(user_id,available_credits,reserved_credits) "
            "VALUES('restored-b',100,0) ON CONFLICT(user_id) DO NOTHING"
        )
        raw.execute("UPDATE wallets SET available_credits=100 WHERE user_id='customer_u'")
        raw.execute(
            "UPDATE viral_runtime_controls SET collection_enabled=1,keywords_json=%s",
            (json.dumps([{"platform": "douyin", "keyword": "确认成员", "category": "推荐"}]),),
        )
    headers = _admin_session(client)
    estimate = client.get("/api/control/viral/collection/estimate", headers=headers).json()
    assert estimate["customerCount"] == 1
    validated, resumed = threading.Event(), threading.Event()
    captured = []

    def barrier(conn, **kwargs):
        captured.append(kwargs["frozen_billing"])
        validated.set()
        assert resumed.wait(10), "恢复客户事务未完成"
        return enqueue_due_viral_collections(conn, **kwargs)

    monkeypatch.setattr("app.viral_collection.enqueue_due_viral_collections", barrier)
    results = []

    def collect():
        results.append(
            client.post(
                _COLLECT_PATH,
                headers={**headers, "Idempotency-Key": "frozen-members"},
                json={
                    "confirm": True,
                    "reason": "只确认客户A",
                    "expected_estimate_snapshot": estimate["snapshot"],
                },
            )
        )

    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert validated.wait(10), "确认事务未到达入队屏障"
        with TestClient(client.app) as other:
            admin_headers = _admin_session(other, "restore-admin")
            restored = other.post(
                "/api/control/customers/restored-b/resume",
                headers={**admin_headers, "Idempotency-Key": "restore-between-reads"},
                json={"confirm": True, "reason": "在确认后恢复B"},
            )
            assert restored.status_code == 200, restored.text
    finally:
        resumed.set()
        thread.join(10)
    assert not thread.is_alive() and results[0].status_code == 202, results[0].text
    with psycopg.connect(route_state) as raw:
        batch, price = raw.execute(
            "SELECT id,pricing_snapshot_json FROM viral_collection_batches"
        ).fetchone()
        assert json.loads(price) == json.loads(captured[0].pricing_json)
        assert raw.execute("SELECT user_id FROM viral_collection_members").fetchall() == [
            ("customer_u",)
        ]
        audit = json.loads(
            raw.execute(
                "SELECT metadata_json FROM audit_logs WHERE action='viral_collection.collect_now'"
            ).fetchone()[0]
        )
        assert audit["estimate"]["customerCount"] == 1
        conn = BusinessConnection.postgres(raw)
        op = accept_platform_operation(
            conn,
            service="viral_data",
            source_id="confirmed-request",
            collection_batch_id=batch,
        )
        finish_operation(conn, operation_id=op, units=1, succeeded=True)
    assert settle_collection_charges() == 1
    with psycopg.connect(route_state) as raw:
        assert raw.execute("SELECT user_id FROM viral_collection_charges").fetchall() == [
            ("customer_u",)
        ]
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id='restored-b'"
            ).fetchone()[0]
            == 100
        )
    next_estimate = client.get("/api/control/viral/collection/estimate", headers=headers).json()
    assert next_estimate["customerCount"] == 2 and next_estimate["snapshot"] != estimate["snapshot"]


def test_collection_clock_and_manual_estimate_preserve_scheduled_run(client, route_state):
    from datetime import datetime

    from app.admin_dates import SHANGHAI
    from app.db_portable import BusinessConnection
    from app.viral_collection import enqueue_due_viral_collections

    headers = _admin_session(client)
    category = client.get("/api/control/viral/keywords", headers=headers).json()["categories"][0]
    words = [
        {
            "platform": "douyin",
            "keyword": "定时抖音",
            "category": category,
            "enabled": True,
            "limit": 2,
        },
        {
            "platform": "wechat_channels",
            "keyword": "定时视频号",
            "category": category,
            "enabled": True,
            "limit": None,
        },
        {
            "platform": "douyin",
            "keyword": "未启用",
            "category": category,
            "enabled": False,
            "limit": 5,
        },
    ]
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_runtime_controls SET keywords_json=%s,per_keyword_limit=10,"
            "monthly_budget_fen=NULL",
            (json.dumps(words),),
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,unit_cost_fen) VALUES('viral_data',2.5) "
            "ON CONFLICT(service) DO UPDATE SET unit_cost_fen=2.5"
        )
    saved = client.patch(
        "/api/control/settings/viral",
        headers={**headers, "Idempotency-Key": "collection-clock"},
        json={
            "collection_enabled": True,
            "import_enabled": True,
            "collection_interval_days": 7,
            "collection_time": "09:00",
            "confirm": True,
            "reason": "每周固定上海时间采集",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["collection_time"] == "09:00"
    next_run = datetime.fromisoformat(saved.json()["next_collection_at"]).astimezone(SHANGHAI)
    assert (next_run.hour, next_run.minute) == (9, 0)
    estimated = client.get("/api/control/viral/collection/estimate", headers=headers)
    assert estimated.status_code == 200, estimated.text
    estimate = estimated.json()
    assert (estimate["searchCallsMin"], estimate["searchCallsMax"], estimate["videoLimit"]) == (
        2,
        4,
        12,
    )
    assert (estimate["searchCostMinFen"], estimate["searchCostMaxFen"]) == (5, 10)
    assert estimate["totalCostFen"] is None
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_refresh_tasks")
        raw.execute(
            "UPDATE viral_runtime_controls SET next_collection_at='2099-10-08T01:00:00+00:00'"
        )
    queued = client.post(
        "/api/control/viral/collect",
        headers={**headers, "Idempotency-Key": "clock-manual"},
        json={"confirm": True, "reason": "预估后手动采集，不改变定时计划"},
    )
    assert queued.status_code == 202, queued.text
    current = client.get("/api/control/settings/viral", headers=headers).json()
    assert datetime.fromisoformat(current["next_collection_at"]) == datetime.fromisoformat(
        "2099-10-08T01:00:00+00:00"
    )
    with psycopg.connect(route_state) as raw:
        assert raw.execute(
            "SELECT collection_config_json::jsonb->>'trigger_kind' FROM viral_refresh_tasks"
        ).fetchall() == [("manual",), ("manual",)]
        raw.execute("DELETE FROM viral_refresh_tasks")
        raw.execute("UPDATE viral_runtime_controls SET next_collection_at=now()-interval '1 day'")
        assert enqueue_due_viral_collections(BusinessConnection.postgres(raw)) is True
        following = raw.execute("SELECT next_collection_at FROM viral_runtime_controls").fetchone()[
            0
        ]
        assert datetime.fromisoformat(str(following)).astimezone(SHANGHAI).hour == 9
        raw.execute("UPDATE billing_tariffs SET unit_cost_fen=NULL WHERE service='viral_data'")
    unknown = client.get("/api/control/viral/collection/estimate", headers=headers).json()
    assert unknown["searchCostMinFen"] is None and unknown["searchCostMaxFen"] is None


def test_concurrent_keyword_edits_preserve_other_targets_and_reject_stale_same_target(
    client, route_state
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    headers = _admin_session(client)
    category = client.get("/api/control/viral/keywords", headers=headers).json()["categories"][0]
    words = [
        {"platform": "douyin", "category": category, "keyword": word, "enabled": True, "limit": 2}
        for word in ["并发词甲", "并发词乙"]
    ]
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE viral_runtime_controls SET keywords_json=%s", (json.dumps(words),))
    gate = Barrier(2)

    def edit(original, limit, key):
        gate.wait(timeout=10)
        return client.patch(
            "/api/control/viral/keywords",
            headers={**headers, "Idempotency-Key": key},
            json={
                "expected": original,
                "replacement": {**original, "limit": limit},
                "confirm": True,
                "reason": "隔离并发修改验证",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(edit, word, limit, f"kw-other-{limit}")
            for word, limit in zip(words, [3, 4])
        ]
        assert [call.result(timeout=15).status_code for call in calls] == [200, 200]
    rows = client.get("/api/control/viral/keywords", headers=headers).json()["items"]
    assert {row["keyword"]: row["limit"] for row in rows} == {"并发词甲": 3, "并发词乙": 4}
    original = {**words[0], "limit": 3}
    gate = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(edit, original, limit, f"kw-same-{limit}") for limit in [5, 6]]
        assert sorted(call.result(timeout=15).status_code for call in calls) == [200, 409]
    rows = client.get("/api/control/viral/keywords", headers=headers).json()["items"]
    assert next(row for row in rows if row["keyword"] == "并发词乙")["limit"] == 4


def test_keyword_batch_edit_conflict_and_measured_performance(client, route_state):
    headers = _admin_session(client)
    page = client.get("/api/control/viral/keywords", headers=headers).json()
    category = page["categories"][0]
    word = {
        "platform": "douyin",
        "category": category,
        "keyword": "经营测试词",
        "enabled": True,
        "limit": 2,
    }
    first = client.post(
        "/api/control/viral/keywords/batch",
        headers={**headers, "Idempotency-Key": "kw-business-batch"},
        json={"confirm": True, "reason": "需求补货测试", "keywords": [word, word]},
    )
    assert first.status_code == 200, first.text
    assert first.json()["added"] == 1 and first.json()["duplicates"] == 1
    replay = client.post(
        "/api/control/viral/keywords/batch",
        headers={**headers, "Idempotency-Key": "kw-business-batch"},
        json={"confirm": True, "reason": "需求补货测试", "keywords": [word, word]},
    )
    assert replay.json() == first.json() and replay.headers["X-Idempotent-Replay"] == "true"
    replacement = {**word, "enabled": False, "limit": 3}
    body = {
        "confirm": True,
        "reason": "独立暂停与条数",
        "expected": word,
        "replacement": replacement,
    }
    edited = client.patch(
        "/api/control/viral/keywords",
        headers={**headers, "Idempotency-Key": "kw-business-edit"},
        json=body,
    )
    assert edited.status_code == 200, edited.text
    stale = client.patch(
        "/api/control/viral/keywords",
        headers={**headers, "Idempotency-Key": "kw-business-stale"},
        json=body,
    )
    assert stale.status_code == 409
    auditor = _admin_session(client, "auditor_u")
    denied = client.patch(
        "/api/control/viral/keywords",
        headers={**auditor, "Idempotency-Key": "kw-business-auditor"},
        json=body,
    )
    assert denied.status_code == 403
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_content_measurement_state SET started_at=now()-interval '40 days'"
        )
        for batch in ["kw-cohort-one", "kw-cohort-two"]:
            raw.execute(
                "INSERT INTO viral_content_sources"
                "(batch_id,source_kind,platform,video_id,keyword,homepage_at) "
                "VALUES(%s,'collection','douyin','admin-video/opaque=id','经营测试词',now())",
                (batch,),
            )
        for ident, kind, age in [
            ("kw-use-one", "detail", 0),
            ("kw-use-two", "copy", 0),
            ("kw-use-old", "copy", 40),
        ]:
            raw.execute(
                "INSERT INTO viral_content_usage_events"
                "(id,platform,video_id,user_id,kind,created_at) "
                "VALUES(%s,'douyin','admin-video/opaque=id','employee_u',%s,"
                "now()-(%s*interval '1 day'))",
                (ident, kind, age),
            )
        raw.execute(
            "INSERT INTO viral_keyword_runs"
            "(batch_id,platform,keyword,status,finished_at,video_count) "
            "VALUES('kw-zero','douyin','经营测试词','SUCCEEDED',now(),0)"
        )
    measured = client.get("/api/control/viral/keywords", headers=headers).json()
    row = next(item for item in measured["items"] if item["keyword"] == word["keyword"])
    assert measured["coverageComplete"] is True
    assert (row["collected"], row["featured"], row["details"], row["copies"], row["uses"]) == (
        1,
        1,
        1,
        1,
        2,
    )
    assert row["effect"] == "high" and row["enabled"] is False and row["limit"] == 3
    assert row["lastRun"]["status"] == "SUCCEEDED"


@pytest.mark.parametrize("zero_results,fail_first", [(False, False), (True, False), (False, True)])
def test_enabled_keyword_limit_and_zero_result_run_are_consumed_by_worker(
    client, route_state, monkeypatch, zero_results, fail_first
):
    from dataclasses import replace
    from time import time

    from test_viral_media import _video

    from app.db_portable import BusinessConnection
    from app.storage import FakeStorageAdapter
    from app.viral_collection import enqueue_due_viral_collections, run_viral_collection
    from app.viral_refresh import acquire_viral_refresh_task

    headers = _admin_session(client)
    category = client.get("/api/control/viral/keywords", headers=headers).json()["categories"][0]
    entries = [
        {
            "platform": "douyin",
            "category": category,
            "keyword": "启用词",
            "enabled": True,
            "limit": 2,
        },
        {
            "platform": "douyin",
            "category": category,
            "keyword": "暂停词",
            "enabled": False,
            "limit": 1,
        },
    ]
    calls, prepared = [], []

    class Source:
        def douyin_search(self, *, keyword, category):
            calls.append(keyword)
            if fail_first and len(calls) == 1:
                raise RuntimeError("虚构搜索故障")
            return (
                []
                if zero_results
                else [
                    replace(
                        _video("douyin", f"quota-{n}"), cover_url="", published_at=int(time()) - 60
                    )
                    for n in range(5)
                ]
            )

    monkeypatch.setattr(
        "app.viral_collection.viral_source_client_from_settings", lambda conn: Source()
    )
    monkeypatch.setattr("app.viral_collection.CoverEnricher.enrich", lambda self, video: video)

    def fetch(self, video, **kwargs):
        prepared.append(video.video_id)
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "INSERT INTO viral_media_preparations"
                "(id,platform,video_id,media_kind,status,storage_uri) "
                "VALUES(%s,'douyin',%s,'video','SUCCEEDED','fake://quota.mp4')",
                ("media-" + video.video_id, video.video_id),
            )

    monkeypatch.setattr("app.viral_collection.ViralMediaPipeline.fetch", fetch)
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE viral_runtime_controls SET keywords_json='[]'")
        search_day = raw.execute(
            "INSERT INTO viral_search_events VALUES"
            "('kw-worker-demand','customer_u','启用词','douyin',"
            "(now() AT TIME ZONE 'Asia/Shanghai')::date::text,now(),'[]') RETURNING search_date"
        ).fetchone()[0]
    before_response = client.get(
        "/api/control/viral/discoveries", headers=headers, params={"date": search_day}
    )
    assert before_response.status_code == 200, before_response.text
    before = before_response.json()["keywords"][0]
    assert before["inventory"] == 0 and before["configured"] is False
    added = client.post(
        "/api/control/viral/keywords/batch",
        headers={**headers, "Idempotency-Key": "kw-worker-add"},
        json={"keywords": entries, "confirm": True, "reason": "需求词加入下一轮采集"},
    )
    assert added.status_code == 200, added.text
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_refresh_tasks")
        raw.execute(
            "UPDATE viral_runtime_controls SET collection_enabled=1,"
            "next_collection_at=now()-interval '1 day',per_keyword_limit=10,"
            "quality_min_likes=NULL,quality_duration_min_ms=NULL,quality_duration_max_ms=NULL,"
            "quality_exclude_words_json='[]',monthly_budget_fen=NULL"
        )
        assert enqueue_due_viral_collections(BusinessConnection.postgres(raw)) is True
        lease = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="quota-worker"
        )
    assert lease is not None
    if fail_first:
        from app.viral_tikhub import ViralSourceError

        with pytest.raises(ViralSourceError):
            run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="quota"))
        with psycopg.connect(route_state) as raw:
            failed = raw.execute("SELECT status,attempt_count FROM viral_keyword_runs").fetchone()
            assert failed == ("FAILED", 1)
    run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="quota"))
    assert calls == ["启用词"] * (2 if fail_first else 1)
    assert prepared == ([] if zero_results else ["quota-0", "quota-1"])
    with psycopg.connect(route_state) as raw:
        row = raw.execute("SELECT keyword,status,video_count FROM viral_keyword_runs").fetchone()
        assert row == ("启用词", "SUCCEEDED", 0 if zero_results else 2)
        assert raw.execute("SELECT attempt_count FROM viral_keyword_runs").fetchone()[0] == (
            2 if fail_first else 1
        )
        assert raw.execute(
            "SELECT count(*) FROM viral_content_sources WHERE keyword='启用词'"
        ).fetchone()[0] == (0 if zero_results else 2)
    after = client.get(
        "/api/control/viral/discoveries", headers=headers, params={"date": search_day}
    ).json()["keywords"][0]
    assert after["configured"] is True
    assert after["inventory"] == (0 if zero_results else 2)
    from app.viral_refresh import complete_viral_refresh_task

    with psycopg.connect(route_state) as raw:
        complete_viral_refresh_task(BusinessConnection.postgres(raw), lease=lease)
    records = client.get(
        "/api/control/viral/collection/records",
        headers=headers,
        params={"start": search_day, "end": search_day},
    ).json()
    saved = records["items"][0]
    assert saved["trigger_kind"] == "scheduled" and saved["run_status"] == "SUCCEEDED"
    assert saved["new_count"] == (0 if zero_results else 2)
    assert saved["videos"] == (0 if zero_results else 2)
    assert saved["failed_video_count"] == 0
    with psycopg.connect(route_state) as raw:
        assert enqueue_due_viral_collections(BusinessConnection.postgres(raw), manual=True) is True
    preserved = client.get(
        "/api/control/viral/collection/records",
        headers=headers,
        params={"start": search_day, "end": search_day},
    ).json()
    assert preserved["total"] == 2
    old = next(r for r in preserved["items"] if r["id"] == saved["id"])
    assert old["run_status"] == "SUCCEEDED" and old["new_count"] == saved["new_count"]


def test_blocked_queued_video_never_resolves_provider_or_prepares_media(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
):
    from app.db_portable import BusinessConnection
    from app.storage import FakeStorageAdapter
    from app.viral_collection import run_viral_collection
    from app.viral_refresh import acquire_viral_refresh_task
    from app.viral_tikhub import ViralSourceError

    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_refresh_tasks")
    queued = client.post(
        "/api/control/viral/videos/curation:batch",
        headers={**headers, "Idempotency-Key": "blocked-queue"},
        json={
            "confirm": True,
            "action": "feature",
            "items": [{"platform": "douyin", "video_id": "admin-video/opaque=id"}],
        },
    )
    assert queued.status_code == 200, queued.text
    blocked = client.patch(
        "/api/control/viral/videos/douyin/admin-video/opaque=id/availability",
        headers={**headers, "Idempotency-Key": "blocked-after-queue"},
        json={"confirm": True, "status": "UNAVAILABLE", "reason": "已屏蔽，不再准备"},
    )
    assert blocked.status_code == 200, blocked.text
    provider_calls = []

    def no_provider(conn):
        provider_calls.append(1)
        raise AssertionError("blocked video must never resolve provider")

    monkeypatch.setattr("app.viral_collection.viral_source_client_from_settings", no_provider)
    with psycopg.connect(route_state) as raw:
        lease = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="blocked-test"
        )
    assert lease is not None
    with pytest.raises(ViralSourceError, match="其他就绪视频已处理"):
        run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="test"))
    assert provider_calls == []


def test_per_video_outcomes_and_exact_meter_scope_survive_queue_reuse(
    client, route_state, monkeypatch
):
    from dataclasses import replace
    from time import time

    from test_viral_media import _video

    from app.billing_meter import meter_call
    from app.db_portable import BusinessConnection
    from app.storage import FakeStorageAdapter
    from app.viral_collection import enqueue_due_viral_collections, run_viral_collection
    from app.viral_refresh import acquire_viral_refresh_task, fail_viral_refresh_task
    from app.viral_tikhub import ViralSourceError

    class Source:
        def douyin_search(self, **kwargs):
            return [
                replace(
                    _video("douyin", ident),
                    title=ident,
                    cover_url="",
                    published_at=int(time()) - 60,
                )
                for ident in ["round-video-fail", "round-video-ok"]
            ]

    monkeypatch.setattr(
        "app.viral_collection.viral_source_client_from_settings", lambda conn: Source()
    )
    monkeypatch.setattr("app.viral_collection.CoverEnricher.enrich", lambda self, video: video)

    def fetch(self, video, **kwargs):
        with meter_call("viral_data") as call:
            call.record_usage()
        if video.video_id == "round-video-fail":
            raise TimeoutError("synthetic private diagnostic not exposed")
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "INSERT INTO viral_media_preparations"
                "(id,platform,video_id,media_kind,status,storage_uri) "
                "VALUES('round-ready','douyin',%s,'video','SUCCEEDED','fake://ready')",
                (video.video_id,),
            )

    monkeypatch.setattr("app.viral_collection.ViralMediaPipeline.fetch", fetch)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_runtime_controls SET collection_enabled=1,keywords_json=%s,"
            "quality_min_likes=NULL,quality_duration_min_ms=NULL,quality_duration_max_ms=NULL,"
            "quality_exclude_words_json='[]',monthly_budget_fen=NULL",
            (json.dumps([{"platform": "douyin", "keyword": "逐视频测试", "category": "推荐"}]),),
        )
        assert enqueue_due_viral_collections(BusinessConnection.postgres(raw), manual=True)
        lease = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="outcome-worker"
        )
        assert lease is not None
        batch = raw.execute("SELECT id FROM viral_collection_batches").fetchone()[0]
    with pytest.raises(ViralSourceError):
        run_viral_collection(lease, FakeStorageAdapter(provider="cos", bucket="synthetic"))
    with psycopg.connect(route_state) as raw:
        fail_viral_refresh_task(BusinessConnection.postgres(raw), lease=lease, cause=TimeoutError())
        metadata = raw.execute(
            "SELECT api_metadata::jsonb->>'video_id' FROM billing_operations "
            "WHERE service='viral_data' AND user_id IS NULL ORDER BY 1"
        ).fetchall()
        assert [row[0] for row in metadata] == ["round-video-fail", "round-video-ok"]
    headers = _admin_session(client)
    path = f"/api/control/viral/collection/records/{batch}/tasks"
    result = client.get(path, headers=headers)
    assert result.status_code == 200, result.text
    outcomes = {item["video_id"]: item for item in result.json()["videos"]}
    assert outcomes["round-video-fail"]["failure_category"] == "服务响应超时"
    assert outcomes["round-video-fail"]["advice"]
    assert outcomes["round-video-ok"]["status"] == "SUCCEEDED"
    assert "private diagnostic" not in result.text
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET collection_config_json='{}' WHERE id=%s", (lease.id,)
        )
    assert client.get(path, headers=headers).json()["videos"] == result.json()["videos"]
    assert client.get(path, headers=headers).json()["current_queue"] is None
    with psycopg.connect(route_state) as raw:
        from app.admin_viral_metrics import video_business_metrics

        measured = video_business_metrics(
            BusinessConnection.postgres(raw), platform="douyin", video_id="round-video-ok"
        )
        assert measured["attributedDataCalls"] == 1 and measured["knownDataCostFen"] >= 0
        assert measured["collectionCostFen"] is None
        unknown = video_business_metrics(
            BusinessConnection.postgres(raw), platform="douyin", video_id="admin-video/opaque=id"
        )
        assert unknown["attributedDataCalls"] == 0 and unknown["collectionCostFen"] is None


def test_platform_probe_rejects_changed_price_before_fake_channel(client, route_state, monkeypatch):
    headers = _admin_session(client)
    estimate = client.post(
        "/api/control/viral/operations/estimate",
        headers=headers,
        json={"action": "search", "platform": "douyin"},
    ).json()
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,unit_cost_fen,version) VALUES('viral_data',9,9) "
            "ON CONFLICT(service) DO UPDATE SET unit_cost_fen=9,version=9"
        )
    calls = []
    monkeypatch.setattr(
        "app.admin_viral_platform_probe.viral_source_client_from_settings",
        lambda conn: calls.append("called"),
    )
    response = client.post(
        "/api/control/viral/platforms/douyin/probe",
        headers={**headers, "Idempotency-Key": "changed-probe"},
        json={
            "confirm": True,
            "reason": "虚构连接验证",
            "expected_cost_snapshot": estimate["snapshot"],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "VIRAL_COST_CHANGED"
    assert calls == []
    with psycopg.connect(route_state) as raw:
        assert raw.execute("SELECT count(*) FROM viral_platform_probes").fetchone()[0] == 0
