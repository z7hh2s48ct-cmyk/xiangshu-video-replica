"""Admin first-frame reconcile endpoint on the real PG lane.

Covers the thin HTTP layer over ``prepare/decision/apply`` in
``app.image_tasks``: admin-writer gating is the tested AdminWriter
dependency, so this suite overrides it and pins routing, status mapping
and state outcomes (RESUME / FAIL / 503 / 409-inconclusive).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-admin-ff-reconcile-tests-minimum-48-bytes",
)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.admin_auth_routes import AdminActor, get_admin_writer
from app.admin_first_frame_routes import router as admin_first_frame_router
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.first_frames import ApilioImageProvider, RetryableImageProviderFailed

FF_RECONCILE_DB_NAME = "admin_ff_reconcile_test"
_SHA_A = "a" * 64


def _pg_dsn() -> str:
    return os.environ.get(
        "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
    )


@pytest.fixture(scope="module")
def reconcile_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(FF_RECONCILE_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(FF_RECONCILE_DB_NAME)


@pytest.fixture()
def api(
    reconcile_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    import psycopg

    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, reconcile_dsn)
    with psycopg.connect(reconcile_dsn, autocommit=True) as conn:
        # Append-only audit triggers refuse TRUNCATE; the T19 fixture precedent
        # bypasses triggers for the isolated fixture database only.
        conn.execute("SET session_replication_role = replica")
        conn.execute("TRUNCATE audit_logs, first_frame_tasks, projects, users CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES"
            " ('u1', 'u1', 'User One', 'user'),"
            " ('admin_u', 'admin_u', 'Admin', 'admin')"
        )
        conn.execute("INSERT INTO projects (id, name, owner_user_id) VALUES ('proj-1', 'FF', 'u1')")
    app = FastAPI()
    app.include_router(admin_first_frame_router)
    admin_actor = AdminActor(
        user_id="admin_u",
        username="admin_u",
        display_name="Admin",
        role="admin",
        auth_method="password",
        session_id="sess-1",
        session_expires_at="2030-01-01T00:00:00Z",
        last_activity_at="2030-01-01T00:00:00Z",
    )
    app.dependency_overrides[get_admin_writer] = lambda: admin_actor
    stub: dict[str, Any] = {"provider": None}

    def _fake_provider(_conn: object) -> Any:
        return stub["provider"]

    # get_image_provider is called directly inside the route (not a FastAPI
    # dependency), so patch the route module attribute instead.
    monkeypatch.setattr("app.admin_first_frame_routes.get_image_provider", _fake_provider)
    with TestClient(app) as test_client:
        yield test_client, stub
    close_pg_pool()


def _seed_uncertain(dsn: str, task_id: str, *, with_receipt: bool) -> None:
    import psycopg

    result_json = "NULL"
    if with_receipt:
        result_json = json.dumps(
            {
                "provider_submission": {
                    "schema_version": 1,
                    "task_id": "vendor-route-1",
                    "account_fingerprint": _SHA_A,
                    "request_hash": _SHA_A,
                    "model": "gpt-image-2",
                    "output_count": 1,
                    "cost_record_id": "cost-route-1",
                },
                "execution": {"provider": "apilio", "model": "gpt-image-2"},
            }
        )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO first_frame_tasks ("
            " id, created_by_user_id, project_id, idempotency_key, request_hash,"
            " request_json, result_json, status"
            ") VALUES (%s, 'u1', 'proj-1', %s, %s, '{}', %s, 'SUBMISSION_UNCERTAIN')",
            (task_id, f"ik-{task_id}", _SHA_A, result_json),
        )


def _task_row(dsn: str, task_id: str) -> dict[str, Any]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT status, error_code FROM first_frame_tasks WHERE id = %s", (task_id,)
        ).fetchone()


class _StubProvider(ApilioImageProvider):
    """实现继承自 ApilioImageProvider：路由对回执提供方做 isinstance 门。"""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome

    @property
    def account_fingerprint(self) -> str:
        return _SHA_A

    def poll_edit(self, task_id: str, *, output_count: int) -> list[object] | None:
        if self.outcome == "success":
            return [object() for _ in range(output_count)]
        if self.outcome == "running":
            return None
        if self.outcome == "unavailable":
            raise RetryableImageProviderFailed("Apilio image request failed")
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422,
            detail={"code": "FIRST_FRAME_PROVIDER_REJECTED", "message": "rejected"},
        )


def test_reconcile_route_resumes_alive_provider_task(api, reconcile_dsn: str) -> None:
    client, stub = api
    _seed_uncertain(reconcile_dsn, "ff-route-1", with_receipt=True)
    stub["provider"] = _StubProvider("success")
    response = client.post("/api/control/first-frame-tasks/ff-route-1/reconcile")
    assert response.status_code == 200
    assert response.json()["result"] == "RESUMED"
    row = _task_row(reconcile_dsn, "ff-route-1")
    assert row["status"] == "PENDING"
    assert row["error_code"] == "IMAGE_TASK_RECONCILE_RESUMED"


def test_reconcile_route_fails_dead_provider_task(api, reconcile_dsn: str) -> None:
    client, stub = api
    _seed_uncertain(reconcile_dsn, "ff-route-2", with_receipt=True)
    stub["provider"] = _StubProvider("failure")
    response = client.post("/api/control/first-frame-tasks/ff-route-2/reconcile")
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "FAILED"
    assert body["detail_code"] == "FIRST_FRAME_PROVIDER_REJECTED"
    row = _task_row(reconcile_dsn, "ff-route-2")
    assert row["status"] == "FAILED"


def test_reconcile_route_maps_provider_outage_to_503(api, reconcile_dsn: str) -> None:
    client, stub = api
    _seed_uncertain(reconcile_dsn, "ff-route-3", with_receipt=True)
    stub["provider"] = _StubProvider("unavailable")
    response = client.post("/api/control/first-frame-tasks/ff-route-3/reconcile")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "IMAGE_TASK_RECONCILE_PROVIDER_UNAVAILABLE"
    row = _task_row(reconcile_dsn, "ff-route-3")
    assert row["status"] == "SUBMISSION_UNCERTAIN"


def test_reconcile_route_rejects_non_uncertain_task(api, reconcile_dsn: str) -> None:
    import psycopg

    client, stub = api
    stub["provider"] = _StubProvider("success")
    with psycopg.connect(reconcile_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO first_frame_tasks ("
            " id, created_by_user_id, project_id, idempotency_key, request_hash,"
            " request_json, status"
            ") VALUES ('ff-route-4', 'u1', 'proj-1', 'ik-4', %s, '{}', 'PENDING')",
            (_SHA_A,),
        )
    response = client.post("/api/control/first-frame-tasks/ff-route-4/reconcile")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IMAGE_TASK_NOT_UNCERTAIN"


def test_reconcile_route_fails_closed_without_receipt(api, reconcile_dsn: str) -> None:
    client, stub = api
    _seed_uncertain(reconcile_dsn, "ff-route-5", with_receipt=False)
    stub["provider"] = _StubProvider("success")
    response = client.post("/api/control/first-frame-tasks/ff-route-5/reconcile")
    assert response.status_code == 200
    assert response.json()["result"] == "FAILED"
    row = _task_row(reconcile_dsn, "ff-route-5")
    assert row["status"] == "FAILED"


def test_reconcile_route_fails_when_provider_config_changed(api, reconcile_dsn: str) -> None:
    client, stub = api
    _seed_uncertain(reconcile_dsn, "ff-route-6", with_receipt=True)
    stub["provider"] = object()  # 非 Apilio 配置：回执永远无法从当前提供方轮询
    response = client.post("/api/control/first-frame-tasks/ff-route-6/reconcile")
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "FAILED"
    assert body["detail_code"] == "IMAGE_TASK_PROVIDER_CHANGED"
    row = _task_row(reconcile_dsn, "ff-route-6")
    assert row["status"] == "FAILED"
