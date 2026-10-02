"""第三方接口调用日志在真 PG 上的闭环（方案 P0-9 / P0-10 / P0-12）。

- 调用日志落库：原始响应、服务商原话、第三方任务号、任务归属，且已脱敏；
- 生成记录按任一编号（我方任务号/请求编号、第三方任务号/请求编号、8 位短编号）
  都能查到同一条，并带处理建议与服务商原话；聚合与列表同口径；
- 调用列表按时间给出；查看原始响应写高敏审计，审计员被拒绝；
- 路由级（HTTP 契约）：两条新端点与编号检索按客户生产形态的 per-operator 会话
  触达真实路由，路径挂载、序列化字段与错误信封不再只有函数级证据。
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    password_admin_session,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection
from app.external_calls import (
    count_expired_calls,
    external_call_context,
    external_call_model,
    purge_expired_call_batch,
    record_external_call,
)

DATABASE = "external_calls_test"


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip()


@pytest.fixture(scope="module")
def calls_dsn() -> Iterator[str]:
    dsn = create_test_database(DATABASE)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        close_pg_pool()
        drop_test_database(DATABASE)


@pytest.fixture()
def pg_env(calls_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, calls_dsn)
    yield calls_dsn
    close_pg_pool()


def _admin() -> CurrentUser:
    return CurrentUser(id="admin_u", username="admin_u", display_name="管理员", role="admin")


def _auditor() -> CurrentUser:
    return CurrentUser(id="auditor_u", username="auditor_u", display_name="审计员", role="auditor")


def _seed_failed_analysis(dsn: str) -> str:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"calls-user-{suffix}"
    project_id = f"calls-project-{suffix}"
    asset_id = f"calls-asset-{suffix}"
    task_id = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES (%s, %s, '调用日志客户', 'customer', 1) ON CONFLICT DO NOTHING",
            (user_id, user_id),
        )
        for actor in ("admin_u", "auditor_u"):
            raw.execute(
                "INSERT INTO users (id, username, display_name, role, is_active) "
                "VALUES (%s, %s, %s, %s, 1) ON CONFLICT (id) DO NOTHING",
                (actor, actor, actor, actor.removesuffix("_u")),
            )
        raw.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '调用日志项目')",
            (project_id, user_id),
        )
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, %s, 'reference_video', %s, %s, "
            "1024, 'video/mp4', %s)",
            (asset_id, project_id, f"cos://bucket/{asset_id}.mp4", "b" * 64, user_id),
        )
        raw.execute(
            "INSERT INTO analysis_tasks (id, project_id, asset_id, created_by_user_id, "
            "duration_seconds, status, attempt, error_code, error_message_redacted, "
            "failure_phase, retryable, created_at, updated_at, completed_at) "
            "VALUES (%s, %s, %s, %s, 8, 'FAILED', 1, 'ANALYSIS_PROVIDER_FAILED', "
            "'视频拆解服务拒绝了请求（HTTP 503）', 'http', 1, "
            "'2026-09-28 10:00:00', '2026-09-28 10:00:00', '2026-09-28 10:00:30')",
            (task_id, project_id, asset_id, user_id),
        )
    return task_id


def _record_failed_call(task_id: str, provider_task_id: str) -> None:
    with (
        external_call_context("ANALYSIS", task_id, attempt=1),
        external_call_model("video-understanding"),
    ):
        record_external_call(
            provider="apilio_gemini",
            endpoint="v1/chat/completions",
            method="POST",
            url="https://api.example.com/v1/chat/completions?key=sk-secret-123456",
            request_summary={"model": "video-understanding", "api_key": "sk-abcdef123456"},
            outcome="PROVIDER_ERROR",
            http_status=503,
            response_headers={"X-Request-Id": "up-8f31a2", "Set-Cookie": "sid=secret"},
            response_body=json.dumps(
                {
                    "id": provider_task_id,
                    "error": {"code": "overloaded", "message": "upstream overloaded"},
                    "debug_url": "https://cdn.example.com/v.mp4?Signature=SIG9876543",
                }
            ),
            latency_ms=2210,
            provider_task_id=provider_task_id,
        )


def test_call_log_is_persisted_redacted_and_bound_to_the_task(pg_env: str) -> None:
    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-persist-1")

    with psycopg.connect(pg_env) as raw:
        row = raw.execute(
            "SELECT task_type, task_id, attempt, outcome, http_status, provider_task_id, "
            "provider_request_id, provider_error_code, provider_message, url_redacted, "
            "request_summary_json, response_headers_json, response_body, request_id "
            "FROM external_call_logs WHERE task_id = %s",
            (task_id,),
        ).fetchone()
    assert row is not None
    (
        task_type,
        bound_task,
        attempt,
        outcome,
        status,
        provider_task,
        provider_request,
        error_code,
        message,
        url,
        summary,
        headers,
        body,
        request_id,
    ) = row
    assert (task_type, bound_task, attempt) == ("ANALYSIS", task_id, 1)
    assert (outcome, status) == ("PROVIDER_ERROR", 503)
    assert (provider_task, provider_request) == ("resp-persist-1", "up-8f31a2")
    assert (error_code, message) == ("overloaded", "upstream overloaded")
    # 密钥与签名一律不落库。
    assert "sk-secret-123456" not in url
    assert summary["api_key"] == "[已脱敏]"
    assert "set-cookie" not in headers
    assert "SIG9876543" not in body
    assert request_id


def test_records_are_found_by_third_party_id_or_short_code_with_explanations(
    pg_env: str,
) -> None:
    from app.control_routes import list_generation_records

    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-search-1")

    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        by_vendor = list_generation_records(
            conn, _admin(), task_ref="resp-search-1", limit=50, offset=0
        )
        by_request = list_generation_records(
            conn, _admin(), task_ref="up-8f31a2", limit=50, offset=0
        )
        by_short = list_generation_records(
            conn, _admin(), task_ref=task_id[:8].upper(), limit=50, offset=0
        )
        unknown = list_generation_records(
            conn, _admin(), task_ref="no-such-reference", limit=50, offset=0
        )

    assert [item.record_id for item in by_vendor.items] == [task_id]
    assert task_id in {item.record_id for item in by_request.items}
    assert task_id in {item.record_id for item in by_short.items}
    assert unknown.total == 0
    [record] = by_vendor.items
    assert record.advice
    assert record.provider_error_code == "overloaded"
    assert record.provider_message == "upstream overloaded"


def test_records_are_found_by_our_own_request_id(pg_env: str) -> None:
    """按调用日志里的我方请求编号（与审计、服务日志同源）也能反查到记录（P0-12）。"""
    from app.control_routes import list_generation_records

    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-own-1")

    with psycopg.connect(pg_env) as raw:
        row = raw.execute(
            "SELECT request_id FROM external_call_logs WHERE task_id = %s", (task_id,)
        ).fetchone()
        assert row is not None
        request_id = str(row[0])
        conn = BusinessConnection.postgres(raw)
        found = list_generation_records(conn, _admin(), task_ref=request_id, limit=50, offset=0)

    assert request_id
    assert [item.record_id for item in found.items] == [task_id]


def test_summary_honours_task_ref_like_the_list(pg_env: str) -> None:
    """聚合与列表同口径：给了编号就只聚合那条记录，失败原因清单同步收窄（P0-12）。"""
    from app.control_routes import summarize_generation_records

    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-summary-1")

    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        scoped = summarize_generation_records(conn, _admin(), task_ref="resp-summary-1")
        unknown = summarize_generation_records(conn, _admin(), task_ref="no-such-reference")

    assert scoped.total == 1
    assert [(c.record_type, c.status, c.count) for c in scoped.counts] == [
        ("ANALYSIS", "FAILED", 1)
    ]
    # 失败原因清单必须与计数同口径，否则两边又对不上。
    assert sum(item.count for item in scoped.failure_reasons) == 1
    assert unknown.total == 0
    assert unknown.failure_reasons == []


def test_call_list_and_audited_raw_response(pg_env: str) -> None:
    from app.control_routes import list_generation_record_calls, read_external_call_response

    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-view-1")

    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        calls = list_generation_record_calls(conn, _admin(), "ANALYSIS", task_id)
        auditor_calls = list_generation_record_calls(conn, _auditor(), "ANALYSIS", task_id)
        [call] = calls.items
        assert calls.total == 1
        assert call.outcome == "PROVIDER_ERROR"
        assert call.has_response_body is True
        assert call.request_summary == {"model": "video-understanding", "api_key": "[已脱敏]"}
        # 审计员看得到调用概要，看不到含客户内容的请求摘要与原始响应。
        assert auditor_calls.items[0].request_summary is None
        with pytest.raises(HTTPException) as denied:
            read_external_call_response(conn, _auditor(), call.call_id)
        assert denied.value.status_code == 403

        response = read_external_call_response(conn, _admin(), call.call_id)
        assert response.response_body is not None
        assert "upstream overloaded" in response.response_body
        assert response.truncated is False
        audit = raw.execute(
            "SELECT actor_user_id, entity_id FROM audit_logs "
            "WHERE action = 'external_call.response_view'"
        ).fetchall()
    assert ("admin_u", call.call_id) in [tuple(row) for row in audit]


# ---------------------------------------------------------------------------
# 路由级：HTTP 契约（#22）与模型/轮次/编号检索（#23）
# ---------------------------------------------------------------------------

# 会话 HMAC 密钥：每次运行随机铸造，绝不使用真实密钥。
TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)


def _admin_session(client: TestClient) -> None:
    response = password_admin_session(client, "admin_u")
    assert response.status_code == 201, response.text


@pytest.fixture()
def route_client(pg_env: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """真实控制面路由 + 真实 PG，按客户生产形态用 per-operator 会话认证。"""
    from app.account_admin_routes import router as account_router
    from app.admin_auth_routes import ADMIN_SESSION_HMAC_KEY_ENV
    from app.admin_runtime_routes import router as runtime_router
    from app.control_routes import router as control_router
    from app.provider_gateway_routes import router as gateway_router

    application = FastAPI()
    application.include_router(control_router)
    application.include_router(account_router)
    application.include_router(runtime_router)
    application.include_router(gateway_router)
    from cryptography.fernet import Fernet

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    with TestClient(application, base_url="https://testserver") as client:
        yield client


def test_calls_route_serves_the_detail_panel_contract(
    route_client: TestClient, pg_env: str
) -> None:
    """调用列表 HTTP 契约：模型/轮次/服务商原话经 response_model 完整序列化。

    「模型」列与 attempt 字段此前只有函数级证据；从路由走一遍后这些字段必须
    出现在 JSON 里——管理端调用日志面板读的就是这份响应。
    """
    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-route-1")
    _admin_session(route_client)

    response = route_client.get(f"/api/control/generation-records/ANALYSIS/{task_id}/calls")
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["provider"] == "apilio_gemini"
    assert item["endpoint"] == "v1/chat/completions"
    assert item["model"] == "video-understanding"
    assert item["attempt"] == 1
    assert item["outcome"] == "PROVIDER_ERROR"
    assert item["http_status"] == 503
    assert item["provider_error_code"] == "overloaded"
    assert item["provider_message"] == "upstream overloaded"
    assert item["request_summary"]["api_key"] == "[已脱敏]"
    assert item["has_response_body"] is True
    assert response.json()["total"] == 1

    # 没有调用的记录返回空列表，非法类型由路径参数校验挡下。
    missing = route_client.get("/api/control/generation-records/ANALYSIS/no-such-task/calls")
    assert missing.status_code == 200
    assert missing.json()["items"] == []
    assert missing.json()["total"] == 0
    unknown_type = route_client.get(f"/api/control/generation-records/NOPE/{task_id}/calls")
    assert unknown_type.status_code == 422


def test_calls_route_reports_total_beyond_the_truncation_limit(
    route_client: TestClient, pg_env: str
) -> None:
    """清单最多 200 条（_CALL_LIST_LIMIT），total 必须给出截断前的真实条数。

    截断此前是静默的：面板只看得到前 200 条，无从知道还有更多（方案 #27）。
    """
    task_id = _seed_failed_analysis(pg_env)
    with psycopg.connect(pg_env, autocommit=True) as raw:
        with raw.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO external_call_logs "
                "(id, provider, endpoint_name, outcome, task_type, task_id) "
                "VALUES (%s, 'apilio_gemini', 'v1/chat/completions', 'SUCCEEDED', 'ANALYSIS', %s)",
                [(str(uuid.uuid4()), task_id) for _ in range(201)],
            )
    _admin_session(route_client)

    response = route_client.get(f"/api/control/generation-records/ANALYSIS/{task_id}/calls")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 201
    assert len(payload["items"]) == 200
    first_ids = {item["call_id"] for item in payload["items"]}
    last = route_client.get(
        f"/api/control/generation-records/ANALYSIS/{task_id}/calls?offset=200&limit=50"
    )
    assert last.status_code == 200
    assert last.json()["total"] == 201
    assert len(last.json()["items"]) == 1
    assert last.json()["items"][0]["call_id"] not in first_ids
    beyond = route_client.get(
        f"/api/control/generation-records/ANALYSIS/{task_id}/calls?offset=250"
    )
    assert beyond.json() == {"items": [], "total": 201}
    invalid = route_client.get(
        f"/api/control/generation-records/ANALYSIS/{task_id}/calls?offset=-1"
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize("fill", ["x", "多字节"])
def test_long_failure_is_redacted_stored_read_and_expired_with_its_object(
    pg_env: str,
    monkeypatch: pytest.MonkeyPatch,
    fill: str,
) -> None:
    from app.control_routes import list_generation_record_calls, read_external_call_response
    from app.storage import FakeStorageAdapter, storage_object_ref_from_uri

    task_id = _seed_failed_analysis(pg_env)
    storage = FakeStorageAdapter(provider="cos", bucket="diagnostic-test")
    monkeypatch.setattr("app.media_routes.get_media_storage", lambda conn: storage)
    monkeypatch.setattr("app.control_routes.storage_for_asset", lambda conn, uri: storage)
    monkeypatch.setattr("app.media_routes.storage_for_asset", lambda conn, uri: storage)
    body = json.dumps({"secret": "FAKE-CREDENTIAL-7", "message": fill * 70000 + "尾部证据"})
    with external_call_context("ANALYSIS", task_id):
        record_external_call(
            provider="p",
            endpoint="test",
            method="GET",
            url=None,
            outcome="PROVIDER_ERROR",
            response_body=body,
        )
    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        [call] = list_generation_record_calls(conn, _admin(), "ANALYSIS", task_id).items
        uri, excerpt, truncated = raw.execute(
            "SELECT response_storage_uri, response_body, response_truncated "
            "FROM external_call_logs WHERE id = %s",
            (call.call_id,),
        ).fetchone()
        assert uri is not None
        assert len(excerpt.encode("utf-8")) <= 65536
        assert truncated is False
        key = storage_object_ref_from_uri(uri).key
        stored = storage.get_object(key).decode("utf-8")
        assert stored.endswith('尾部证据"}')
        assert "FAKE-CREDENTIAL-7" not in stored
        response = read_external_call_response(conn, _admin(), call.call_id)
        assert response.response_body == stored
        assert response.truncated is False
        raw.execute(
            "UPDATE external_call_logs SET created_at = %s WHERE id = %s",
            ((datetime.now(UTC) - timedelta(days=181)).isoformat(), call.call_id),
        )
        assert purge_expired_call_batch(raw, now=datetime.now(UTC)) == 1
        assert storage.head_object(key) is None
        assert (
            raw.execute(
                "SELECT id FROM external_call_logs WHERE id = %s", (call.call_id,)
            ).fetchone()
            is None
        )


def test_long_failure_falls_back_to_pg_full_text_when_storage_is_unavailable(
    pg_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.control_routes import list_generation_record_calls, read_external_call_response
    from app.storage import StorageBackendUnavailable

    def unavailable(conn: object) -> None:
        raise StorageBackendUnavailable("fake unavailable")

    monkeypatch.setattr("app.media_routes.get_media_storage", unavailable)
    task_id = _seed_failed_analysis(pg_env)
    body = "x" * 70000 + "尾部证据"
    with external_call_context("ANALYSIS", task_id):
        record_external_call(
            provider="p",
            endpoint="test",
            method="GET",
            url=None,
            outcome="PROVIDER_ERROR",
            response_body=body,
        )
    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        [call] = list_generation_record_calls(conn, _admin(), "ANALYSIS", task_id).items
        response = read_external_call_response(conn, _admin(), call.call_id)
        assert response.response_body == body
        assert response.truncated is False


@pytest.mark.parametrize(
    "provider, queued, running",
    [
        ("p", '{"status":"queued"}', '{"status":"running"}'),
        ("hifly", '{"data":{"status":1}}', '{"data":{"status":2}}'),
    ],
)
def test_repeated_pending_polls_keep_state_changes_and_final_failure(
    pg_env: str,
    provider: str,
    queued: str,
    running: str,
) -> None:
    from app.control_routes import list_generation_record_calls

    task_id = _seed_failed_analysis(pg_env)
    with external_call_context("ANALYSIS", task_id, attempt=1):
        for _ in range(205):
            record_external_call(
                provider=provider,
                endpoint="task/status",
                method="GET",
                url="https://fake.test/task",
                outcome="SUCCEEDED",
                http_status=200,
                response_body=queued,
            )
        record_external_call(
            provider=provider,
            endpoint="task/status",
            method="GET",
            url="https://fake.test/task",
            outcome="SUCCEEDED",
            http_status=200,
            response_body=running,
        )
        record_external_call(
            provider=provider,
            endpoint="task/status",
            method="GET",
            url="https://fake.test/task",
            outcome="PROVIDER_ERROR",
            http_status=200,
            response_body='{"status":"failed","message":"最终失败原话"}',
        )
    with psycopg.connect(pg_env) as raw:
        calls = list_generation_record_calls(
            BusinessConnection.postgres(raw), _admin(), "ANALYSIS", task_id
        )
        assert calls.total == 3
        assert calls.items[0].poll_count == 205
        assert calls.items[0].last_seen_at is not None
        assert calls.items[-1].outcome == "PROVIDER_ERROR"
        assert calls.items[-1].provider_message == "最终失败原话"


def test_orphan_object_is_removed_after_insert_failure_and_deferred_delete_can_retry(
    pg_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.external_calls import resolve_pending_response_objects
    from app.storage import (
        FakeStorageAdapter,
        StorageBackendUnavailable,
        storage_object_ref_from_uri,
    )

    storage = FakeStorageAdapter(provider="cos", bucket="diagnostic-test", key_prefix="private")
    monkeypatch.setattr("app.media_routes.get_media_storage", lambda conn: storage)
    monkeypatch.setattr("app.media_routes.storage_for_asset", lambda conn, uri: storage)
    real_delete = storage.delete_object
    deleted: list[str] = []

    def tracked_delete(key: str, *, actor_id: str | None = None) -> None:
        deleted.append(key)
        real_delete(key, actor_id=actor_id)

    monkeypatch.setattr(storage, "delete_object", tracked_delete)
    # endpoint 的 NOT NULL 约束模拟对象已写入但日志 SQL 拒绝，未触发真实外部服务。
    record_external_call(
        provider="p",
        endpoint=None,
        method="GET",
        url=None,
        outcome="PROVIDER_ERROR",
        response_body="x" * 70000,
    )
    assert len(deleted) == 1
    assert storage.head_object(deleted[0]) is None

    def unavailable(key: str, *, actor_id: str | None = None) -> None:
        raise StorageBackendUnavailable("fake unavailable")

    monkeypatch.setattr(storage, "delete_object", unavailable)
    record_external_call(
        provider="p",
        endpoint=None,
        method="GET",
        url=None,
        outcome="PROVIDER_ERROR",
        response_body="x" * 70000,
    )
    with psycopg.connect(pg_env) as raw:
        pending = raw.execute(
            "SELECT call_id, storage_uri FROM external_call_response_pending"
        ).fetchall()
        assert len(pending) == 1
        call_id, uri = pending[0]
        key = storage_object_ref_from_uri(uri).key
        assert storage.head_object(key) is not None
        monkeypatch.setattr(storage, "delete_object", real_delete)
        # 活跃写入持锁时跳过，不能误删暂停中的写入。
        with psycopg.connect(pg_env) as active:
            active.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"external-response:{call_id}",),
            )
            assert (
                resolve_pending_response_objects(raw, now=datetime.now(UTC) + timedelta(hours=2))
                == 0
            )
            assert storage.head_object(key) is not None
        assert (
            resolve_pending_response_objects(raw, now=datetime.now(UTC) + timedelta(hours=4)) == 1
        )
        assert storage.head_object(key) is None


def test_historical_response_reports_unknown_completeness(pg_env: str) -> None:
    from app.control_routes import read_external_call_response

    _seed_failed_analysis(pg_env)
    call_id = str(uuid.uuid4())
    with psycopg.connect(pg_env) as raw:
        raw.execute(
            "INSERT INTO external_call_logs (id, provider, endpoint_name, response_body) "
            "VALUES (%s, 'p', 'legacy', 'legacy excerpt')",
            (call_id,),
        )
        response = read_external_call_response(BusinessConnection.postgres(raw), _admin(), call_id)
        assert response.truncated is None


def test_purge_storage_failure_does_not_starve_later_database_only_rows(
    pg_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.storage import StorageBackendUnavailable

    def unavailable(conn: object, uri: str) -> None:
        raise StorageBackendUnavailable("fake unavailable")

    monkeypatch.setattr("app.media_routes.storage_for_asset", unavailable)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    now = datetime.now(UTC)
    with psycopg.connect(pg_env) as raw:
        for i, call_id in enumerate(ids):
            raw.execute(
                "INSERT INTO external_call_logs "
                "(id, provider, endpoint_name, outcome, created_at, response_storage_uri) "
                "VALUES (%s, 'p', 'fake', 'PROVIDER_ERROR', %s, %s)",
                (
                    call_id,
                    (now - timedelta(days=200 - i)).isoformat(),
                    f"cos://fake/{call_id}.txt" if i < 2 else None,
                ),
            )
        assert purge_expired_call_batch(raw, now=now, batch_size=2) == 0
        assert count_expired_calls(raw, now=now, ready_only=True) == 1
        assert purge_expired_call_batch(raw, now=now, batch_size=2) == 1
        assert (
            raw.execute("SELECT id FROM external_call_logs WHERE id = %s", (ids[2],)).fetchone()
            is None
        )
        raw.execute("DELETE FROM external_call_logs WHERE id = ANY(%s)", (ids,))


def test_calls_route_reads_recharge_order_probe_log(route_client: TestClient) -> None:
    """充值查单（P0-9 #9）：订单号在 HTTP 层反查到支付网关调用。"""
    order_no = f"CZ{uuid.uuid4().hex[:14]}"
    with external_call_context("RECHARGE_ORDER", order_no):
        record_external_call(
            provider="zpay",
            endpoint="mapi.php",
            method="POST",
            url="https://zpay.example.com/mapi.php",
            request_summary={"pid": "merchant-1", "key": "[已脱敏]"},
            outcome="SUCCEEDED",
            http_status=200,
            response_body=json.dumps({"code": 1, "trade_no": "ZP-ROUTE-1"}),
        )
    _admin_session(route_client)

    response = route_client.get(f"/api/control/generation-records/RECHARGE_ORDER/{order_no}/calls")
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["endpoint"] == "mapi.php"
    assert item["outcome"] == "SUCCEEDED"
    assert item["provider"] == "zpay"


def test_auditor_calls_route_keeps_metadata_but_hides_summary(
    route_client: TestClient, pg_env: str
) -> None:
    """审计员在 HTTP 层可读调用概要，但请求摘要（含客户内容）置空。"""
    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-route-auditor")
    response = password_admin_session(route_client, "auditor_u")
    assert response.status_code == 201, response.text

    listed = route_client.get(f"/api/control/generation-records/ANALYSIS/{task_id}/calls")
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item["endpoint"] == "v1/chat/completions"
    assert item["request_summary"] is None


def test_external_call_response_route_audits_and_forbids_auditors(
    route_client: TestClient, pg_env: str
) -> None:
    """原始响应端点的 HTTP 边界：匿名 401、审计员 403、管理员 200 且落审计。"""
    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-route-view")
    with psycopg.connect(pg_env) as raw:
        row = raw.execute(
            "SELECT id FROM external_call_logs WHERE task_id = %s", (task_id,)
        ).fetchone()
    assert row is not None
    call_id = str(row[0])

    anonymous = route_client.get(f"/api/control/external-calls/{call_id}/response")
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"]["code"] == "ADMIN_SESSION_INVALID"

    response = password_admin_session(route_client, "auditor_u")
    assert response.status_code == 201, response.text
    denied = route_client.get(f"/api/control/external-calls/{call_id}/response")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "EXTERNAL_CALL_RESPONSE_FORBIDDEN"

    route_client.cookies.clear()
    _admin_session(route_client)
    seen = route_client.get(f"/api/control/external-calls/{call_id}/response")
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body["call_id"] == call_id
    assert "upstream overloaded" in body["response_body"]
    assert "SIG9876543" not in body["response_body"]
    assert body["truncated"] is False

    missing = route_client.get("/api/control/external-calls/no-such-call/response")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "EXTERNAL_CALL_NOT_FOUND"

    with psycopg.connect(pg_env) as raw:
        audit = raw.execute(
            "SELECT actor_user_id FROM audit_logs "
            "WHERE action = 'external_call.response_view' AND entity_id = %s",
            (call_id,),
        ).fetchall()
    assert ("admin_u",) in [tuple(row) for row in audit]


def test_generation_records_route_searches_every_reference(
    route_client: TestClient, pg_env: str
) -> None:
    """task_ref 在 HTTP 层落到同一条记录；聚合与列表同口径（P0-12）。"""
    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-route-ref")
    _admin_session(route_client)

    listed = route_client.get(
        "/api/control/generation-records", params={"task_ref": "resp-route-ref"}
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1
    [record] = body["items"]
    assert record["record_id"] == task_id
    assert record["provider_error_code"] == "overloaded"
    assert record["provider_message"] == "upstream overloaded"
    assert record["advice"]

    summary = route_client.get(
        "/api/control/generation-records/summary", params={"task_ref": "resp-route-ref"}
    )
    assert summary.status_code == 200, summary.text
    summary_body = summary.json()
    assert summary_body["total"] == 1
    assert sum(item["count"] for item in summary_body["failure_reasons"]) == 1

    # 编号长度上限在路由层校验（Query max_length=200）。
    too_long = route_client.get("/api/control/generation-records", params={"task_ref": "x" * 201})
    assert too_long.status_code == 422


# ---------------------------------------------------------------------------
# 缩略图签发（方案 P2-1）
# ---------------------------------------------------------------------------


def _audit_count(dsn: str, action: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as raw:
        return int(
            raw.execute("SELECT count(*) FROM audit_logs WHERE action = %s", (action,)).fetchone()[
                0
            ]
        )


def _seed_video_with_thumbnail(dsn: str) -> tuple[str, str]:
    """播一条带结果资产的视频记录：资产带缩略图键、存储为对象存储。"""
    suffix = uuid.uuid4().hex[:12]
    user_id = f"thumb-user-{suffix}"
    project_id = f"thumb-project-{suffix}"
    asset_id = f"thumb-asset-{suffix}"
    task_id = f"thumb-task-{suffix}"
    thumbnail_key = f"thumbs/{asset_id}.thumb.jpg"
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES (%s, %s, '缩略图客户', 'customer', 1)",
            (user_id, user_id),
        )
        raw.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '缩略图项目')",
            (project_id, user_id),
        )
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, metadata_json, created_by_user_id) VALUES "
            "(%s, %s, 'generated_video', %s, %s, 2048, 'video/mp4', %s, %s)",
            (
                asset_id,
                project_id,
                f"cos://thumb-bucket/{asset_id}.mp4",
                "b" * 64,
                json.dumps({"thumbnail_key": thumbnail_key}),
                user_id,
            ),
        )
        batch_id = f"thumb-batch-{suffix}"
        raw.execute(
            "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
            "idempotency_key, request_hash, request_snapshot_json) "
            "VALUES (%s, %s, %s, %s, %s, '{}')",
            (batch_id, project_id, user_id, f"key-{suffix}", f"hash-{suffix}"),
        )
        raw.execute(
            "INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, model, "
            "status, archive_status, result_asset_id, created_at_utc) VALUES "
            "(%s, %s, 'I2V', 'metaso', 'MiniMax-H3', 'SUCCEEDED', 'DIRECT', %s, now())",
            (task_id, batch_id, asset_id),
        )
    return task_id, asset_id


def test_thumbnail_route_requires_an_admin_session(route_client: TestClient) -> None:
    """新端点的权限面走路由级证据：没有管理员会话一律挡在门外。"""
    response = route_client.get("/api/control/generation-records/ANALYSIS/whatever/thumbnail")
    assert response.status_code == 401


def test_thumbnail_route_returns_a_placeholder_without_media(
    route_client: TestClient, pg_env: str
) -> None:
    """没有媒体的记录给空 url：管理端显示占位，而不是给一张必然 404 的图。

    同时钉住「没签出就不留痕」——审计只在真的给出媒体地址时写。
    """
    task_id = _seed_failed_analysis(pg_env)
    _admin_session(route_client)

    response = route_client.get(f"/api/control/generation-records/ANALYSIS/{task_id}/thumbnail")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["record_type"] == "ANALYSIS"
    assert body["record_id"] == task_id
    assert body["url"] is None
    assert body["expires_in_seconds"] > 0
    assert _audit_count(pg_env, "generation_record.thumbnail_view") == 0


def test_thumbnail_route_signs_object_storage_and_writes_audit(
    route_client: TestClient, pg_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有缩略图键的对象存储资产：签出地址并写审计（查看客户媒体要留痕）。

    存储用假适配器替身，但 provider 是 ``cos``——这正是签发分支的判据。
    """
    from app import control_routes
    from app.storage import FakeStorageAdapter

    task_id, _asset_id = _seed_video_with_thumbnail(pg_env)
    _admin_session(route_client)
    fake = FakeStorageAdapter(provider="cos", bucket="thumb-tests")
    monkeypatch.setattr(control_routes, "storage_for_asset", lambda conn, uri: fake)

    response = route_client.get(f"/api/control/generation-records/VIDEO/{task_id}/thumbnail")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["url"], str) and body["url"]
    assert _audit_count(pg_env, "generation_record.thumbnail_view") == 1


# ---------------------------------------------------------------------------
# 保留期清理（P0-9）：失败调用 180 天、成功调用 30 天，超期删行。
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _reset_call_logs(dsn: str) -> None:
    # 同库里其它用例留下的行时间都是「现在」，不会超期；清空只是让断言按
    # 「本用例造的行」精确比对，不受行数累计影响。
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute("DELETE FROM external_call_logs")


def _insert_call_row(
    dsn: str, call_id: str, *, outcome: str | None, age_days: int, now: datetime = _NOW
) -> None:
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO external_call_logs (id, provider, endpoint_name, outcome, created_at) "
            "VALUES (%s, 'retention_test', 'probe', %s, %s)",
            (call_id, outcome, (now - timedelta(days=age_days)).isoformat()),
        )


def _surviving_call_ids(dsn: str) -> set[str]:
    with psycopg.connect(dsn, autocommit=True) as raw:
        return {row[0] for row in raw.execute("SELECT id FROM external_call_logs").fetchall()}


def test_retention_keeps_failures_longer_than_successes(pg_env: str) -> None:
    _reset_call_logs(pg_env)
    # 同样 100 天：成功的该删，失败的要留——这才证明按成败分了两档。
    _insert_call_row(pg_env, "succ-31d", outcome="SUCCEEDED", age_days=31)
    _insert_call_row(pg_env, "succ-29d", outcome="SUCCEEDED", age_days=29)
    _insert_call_row(pg_env, "succ-100d", outcome="SUCCEEDED", age_days=100)
    _insert_call_row(pg_env, "fail-100d", outcome="PROVIDER_ERROR", age_days=100)
    _insert_call_row(pg_env, "fail-179d", outcome="TIMEOUT", age_days=179)
    _insert_call_row(pg_env, "fail-181d", outcome="NETWORK_ERROR", age_days=181)
    # P0-9 之前的旧行没有 outcome：无法判断成败，按失败对待，宁可多留。
    _insert_call_row(pg_env, "legacy-100d", outcome=None, age_days=100)
    _insert_call_row(pg_env, "legacy-181d", outcome=None, age_days=181)

    with psycopg.connect(pg_env, autocommit=True) as conn:
        assert count_expired_calls(conn, now=_NOW) == 4
        # 统计不改数据。
        assert len(_surviving_call_ids(pg_env)) == 8
        with conn.transaction():
            deleted = purge_expired_call_batch(conn, now=_NOW)
    assert deleted == 4
    assert _surviving_call_ids(pg_env) == {"succ-29d", "fail-100d", "fail-179d", "legacy-100d"}


def test_retention_batches_until_nothing_is_left_and_is_idempotent(pg_env: str) -> None:
    _reset_call_logs(pg_env)
    for index in range(5):
        _insert_call_row(pg_env, f"old-{index}", outcome="SUCCEEDED", age_days=45)
    _insert_call_row(pg_env, "fresh", outcome="SUCCEEDED", age_days=1)

    with psycopg.connect(pg_env, autocommit=True) as conn:
        sizes = []
        while True:
            with conn.transaction():
                batch = purge_expired_call_batch(conn, now=_NOW, batch_size=2)
            sizes.append(batch)
            if batch == 0:
                break
        # 分批：2 + 2 + 1，最后一次返回 0 表示已清完；重复执行不再删任何行。
        assert sizes == [2, 2, 1, 0]
        with conn.transaction():
            assert purge_expired_call_batch(conn, now=_NOW) == 0
    assert _surviving_call_ids(pg_env) == {"fresh"}


def test_purge_cli_dry_run_changes_nothing_and_real_run_deletes_only_expired(
    pg_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.purge_external_call_logs import main as purge_main

    _reset_call_logs(pg_env)
    real_now = datetime.now(UTC)
    _insert_call_row(pg_env, "cli-old", outcome="SUCCEEDED", age_days=40, now=real_now)
    _insert_call_row(pg_env, "cli-fail-old", outcome="PROVIDER_ERROR", age_days=200, now=real_now)
    _insert_call_row(pg_env, "cli-keep", outcome="PROVIDER_ERROR", age_days=40, now=real_now)

    assert purge_main(["--database-url", pg_env, "--dry-run"]) == 0
    assert "eligible for purge: 2" in capsys.readouterr().out
    assert _surviving_call_ids(pg_env) == {"cli-old", "cli-fail-old", "cli-keep"}

    assert purge_main(["--database-url", pg_env]) == 0
    assert "purged 2 external call log row(s)" in capsys.readouterr().out
    assert _surviving_call_ids(pg_env) == {"cli-keep"}

    # 再跑一遍：没有可删的行，输出仍只有计数。
    assert purge_main(["--database-url", pg_env]) == 0
    assert "purged 0 external call log row(s)" in capsys.readouterr().out


def test_orphan_cleanup_failure_does_not_starve_later_deletable_object(
    pg_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.external_calls import resolve_pending_response_objects
    from app.storage import FakeStorageAdapter, StorageBackendUnavailable

    fake = FakeStorageAdapter(provider="cos", bucket="orphan-fairness")
    ids = [str(uuid.uuid4()) for _ in range(3)]
    real_delete = fake.delete_object
    attempted: list[str] = []

    def selective_delete(key: str, *, actor_id: str | None = None) -> None:
        attempted.append(key)
        if key in ids[:2]:
            raise StorageBackendUnavailable("fictional deletion unavailable")
        real_delete(key, actor_id=actor_id)

    monkeypatch.setattr(fake, "delete_object", selective_delete)
    monkeypatch.setattr("app.media_routes.storage_for_asset", lambda conn, uri: fake)
    now = datetime.now(UTC)
    with psycopg.connect(pg_env, autocommit=True) as raw:
        raw.execute("DELETE FROM external_call_response_pending")
        for i, call_id in enumerate(ids):
            uri = fake.put_object(call_id, b"fake", content_type="text/plain").uri
            raw.execute(
                "INSERT INTO external_call_response_pending (call_id,storage_uri,created_at) "
                "VALUES (%s,%s,%s)",
                (call_id, uri, now - timedelta(hours=5 - i)),
            )
        assert resolve_pending_response_objects(raw, now=now, batch_size=2) == 0
        assert resolve_pending_response_objects(raw, now=now, batch_size=2) == 1
        assert attempted == ids
        assert fake.head_object(ids[2]) is None
        assert raw.execute("SELECT count(*) FROM external_call_response_pending").fetchone()[0] == 2
        assert resolve_pending_response_objects(raw, now=now, batch_size=2) == 0
        monkeypatch.setattr(fake, "delete_object", real_delete)
        assert resolve_pending_response_objects(raw, now=now + timedelta(hours=2)) == 2


@pytest.mark.parametrize("actor_id", ["admin_u", "auditor_u"])
def test_technical_routes_reject_non_super_admin_sessions(
    route_client: TestClient, pg_env: str, actor_id: str
) -> None:
    _seed_failed_analysis(pg_env)
    with psycopg.connect(pg_env) as raw:
        raw.execute("UPDATE users SET is_super_admin=0 WHERE id = %s", (actor_id,))
    login = password_admin_session(route_client, actor_id)
    from app.admin_auth_routes import ADMIN_CSRF_HEADER, ADMIN_SESSION_COOKIE

    headers = {
        ADMIN_CSRF_HEADER: login.json()["csrf_token"],
        "Cookie": f"{ADMIN_SESSION_COOKIE}={route_client.cookies.get(ADMIN_SESSION_COOKIE)}",
    }
    routes = [
        ("GET", "/api/control/settings"),
        ("PUT", "/api/control/settings/providers/metaso"),
        ("POST", "/api/control/settings/providers/metaso/connection-test"),
        ("POST", "/api/control/settings/providers/metaso/paid-test"),
        ("PATCH", "/api/control/settings/runtime"),
        ("GET", "/api/control/settings/h3-accounts"),
        ("PUT", "/api/control/settings/h3-accounts/fake"),
        ("GET", "/api/control/settings/queue-mode"),
        ("PATCH", "/api/control/settings/queue-mode"),
        ("GET", "/api/admin/providers/list"),
        ("GET", "/api/admin/providers/metaso/status"),
        ("POST", "/api/admin/providers/metaso/switch"),
        ("GET", "/api/admin/providers/metaso/usage-history"),
        ("POST", "/api/admin/providers/test-connection"),
        ("GET", "/api/control/external-calls"),
    ]
    for method, path in routes:
        result = route_client.request(
            method, path, headers=headers, json={} if method != "GET" else None
        )
        assert result.status_code == 403, (method, path, result.status_code, result.text)
    # 资金、采集仍属于运营权限，没有连带禁止普通管理员/审计员读概要。
    for path in (
        "/api/control/recharge-orders",
        "/api/control/settings/customer-payments",
        "/api/control/settings/viral",
    ):
        assert route_client.get(path).status_code == 200


def test_global_calls_super_admin_filters_paging_metrics_and_audited_response(
    route_client: TestClient, pg_env: str
) -> None:
    from app.admin_auth_routes import ADMIN_CSRF_HEADER

    task_id = _seed_failed_analysis(pg_env)
    provider = "fake-global-" + uuid.uuid4().hex[:8]
    with external_call_context("ANALYSIS", task_id, attempt=1):
        for latency in (100, 200):
            record_external_call(
                provider=provider,
                endpoint="fake/status",
                method="GET",
                url=None,
                outcome="SUCCEEDED",
                response_body='{"status":"queued"}',
                latency_ms=latency,
            )
        record_external_call(
            provider=provider,
            endpoint="fake/status",
            method="GET",
            url=None,
            outcome="PROVIDER_ERROR",
            response_body='{"error":{"message":"原始失败"}}',
            latency_ms=300,
            provider_task_id="fake-third-task",
        )
    with psycopg.connect(pg_env) as raw:
        raw.execute("UPDATE users SET is_super_admin=1 WHERE id='admin_u'")
        raw.execute(
            "UPDATE external_call_logs SET provider_request_id='fake-third-request' "
            "WHERE task_id=%s AND outcome='PROVIDER_ERROR'",
            (task_id,),
        )
        raw.execute(
            "INSERT INTO external_call_observations(id,provider,outcome,latency_ms,created_at) "
            "VALUES (%s,%s,'TIMEOUT',9000,now()-interval '25 hours')",
            (str(uuid.uuid4()), provider),
        )
    login = password_admin_session(route_client, "admin_u")
    assert login.status_code == 201
    assert route_client.get("/api/control/settings").status_code == 200
    assert route_client.get("/api/control/settings/h3-accounts").status_code == 200
    assert route_client.get("/api/control/settings/queue-mode").status_code == 200
    listed = route_client.get(
        "/api/control/external-calls", params={"provider": provider, "limit": 1}
    )
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert data["total"] == 2 and len(data["items"]) == 1
    assert listed.headers["cache-control"] == "no-store"
    metric = next(m for m in data["metrics"] if m["provider"] == provider)
    assert metric["total"] == 3 and metric["failed"] == 1
    assert metric["failure_rate_pct"] == pytest.approx(100 / 3)
    assert metric["avg_latency_ms"] == 200 and metric["latency_samples"] == 3
    page2 = route_client.get(
        "/api/control/external-calls", params={"provider": provider, "limit": 1, "offset": 1}
    ).json()
    assert page2["items"][0]["call"]["call_id"] != data["items"][0]["call"]["call_id"]
    for ref in (task_id, task_id[:8], "fake-third-task", "fake-third-request"):
        found = route_client.get(
            "/api/control/external-calls", params={"provider": provider, "task_ref": ref}
        ).json()
        assert found["total"] >= 1
        assert all(item["task_id"] == task_id for item in found["items"])
    failed = route_client.get(
        "/api/control/external-calls",
        params={"provider": provider, "endpoint": "fake/status", "outcome": "PROVIDER_ERROR"},
    ).json()
    assert failed["total"] == 1
    call_id = failed["items"][0]["call"]["call_id"]
    assert route_client.get(f"/api/control/external-calls/{call_id}/response").status_code == 200
    assert (
        route_client.get(
            "/api/control/external-calls", params={"created_from": "2099-01-01"}
        ).json()["total"]
        == 0
    )
    assert (
        route_client.get(
            "/api/control/external-calls", params={"created_from": "invalid"}
        ).status_code
        == 422
    )
    assert (
        route_client.get(
            "/api/control/external-calls",
            params={"created_from": "2026-10-02", "created_to": "2026-10-01"},
        ).status_code
        == 422
    )
    # 已登录超管仍必须带 CSRF，不能因标记绕过写保护。
    assert (
        route_client.patch(
            "/api/control/settings/queue-mode",
            json={"fair_queue_enabled": False, "confirm": True, "reason": "fake"},
        ).status_code
        == 403
    )
    headers = {ADMIN_CSRF_HEADER: login.json()["csrf_token"], "Idempotency-Key": str(uuid.uuid4())}
    written = route_client.patch(
        "/api/control/settings/queue-mode",
        headers=headers,
        json={"fair_queue_enabled": False, "confirm": True, "reason": "隔离验证超管写入"},
    )
    assert written.status_code == 200, written.text
    with psycopg.connect(pg_env) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='external_call.response_view' "
                "AND entity_id=%s",
                (call_id,),
            ).fetchone()[0]
            == 1
        )
        raw.execute("UPDATE users SET is_super_admin=0 WHERE id='admin_u'")


@pytest.mark.parametrize(
    "actor_id, super_admin", [("admin_u", False), ("auditor_u", False), ("admin_u", True)]
)
def test_legacy_technical_settings_cannot_bypass_super_admin_flag(
    route_client: TestClient, pg_env: str, actor_id: str, super_admin: bool
) -> None:
    from app.auth import get_current_user
    from app.settings_routes import router as legacy_router

    _seed_failed_analysis(pg_env)
    with psycopg.connect(pg_env) as raw:
        raw.execute("UPDATE users SET is_super_admin=%s WHERE id=%s", (int(super_admin), actor_id))
    route_client.app.include_router(legacy_router)
    # 旧身份层用明确的测试替身；技术权限仍读取真实隔离PG，不覆盖权限依赖。
    route_client.app.dependency_overrides[get_current_user] = (
        _admin if actor_id == "admin_u" else _auditor
    )
    routes = [
        ("GET", "/api/admin/settings", None),
        ("PUT", "/api/admin/settings/providers/metaso", {"config": {}}),
        ("POST", "/api/admin/settings/providers/metaso/secrets/api_key/reveal", None),
        (
            "PATCH",
            "/api/admin/settings/runtime",
            {"max_generation_count_per_batch": 4, "max_concurrent_h3_tasks": 2},
        ),
        ("POST", "/api/admin/settings/providers/metaso/connection-test", None),
        ("POST", "/api/admin/settings/providers/metaso/paid-test", None),
        ("POST", "/api/admin/settings/diagnostic-test", None),
        ("GET", "/api/admin/settings/diagnostic-reports/fake/download", None),
    ]
    # 禁止真实探针。这里仅验证拒绝侧及读取侧，允许侧付费测试由既有假探针验证。
    for method, path, body in routes if not super_admin else routes[:1]:
        result = route_client.request(method, path, json=body)
        assert result.status_code == (200 if super_admin else 403), (path, result.text)
    if super_admin:
        from app.settings import SettingsRepository

        with psycopg.connect(pg_env) as raw:
            SettingsRepository(BusinessConnection.postgres(raw)).save_provider_config(
                "metaso", {"api_key": "FICTIONAL-REVEAL-VALUE"}, actor_user_id="admin_u"
            )
        result = route_client.post("/api/admin/settings/providers/metaso/secrets/api_key/reveal")
        assert result.status_code == 200, result.text
        assert result.json()["value"] == "FICTIONAL-REVEAL-VALUE"
        assert result.headers["cache-control"] == "no-store"
        with psycopg.connect(pg_env) as raw:
            metadata = raw.execute(
                "SELECT metadata_json FROM audit_logs "
                "WHERE action='provider_settings.secret_reveal' AND actor_user_id='admin_u'"
            ).fetchall()
        assert metadata and all("FICTIONAL-REVEAL-VALUE" not in str(row) for row in metadata)
    route_client.app.dependency_overrides.clear()
    with psycopg.connect(pg_env) as raw:
        raw.execute("UPDATE users SET is_super_admin=0 WHERE id=%s", (actor_id,))


def test_response_cleanup_gate_keeps_shared_content_references(
    pg_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from app.external_calls import _resolve_pending_response_object

    fake_storage = Mock()
    monkeypatch.setattr("app.media_routes.storage_for_asset", lambda conn, uri: fake_storage)
    call_id, pending_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(UTC)
    with psycopg.connect(pg_env) as raw:
        raw.execute(
            "INSERT INTO external_call_logs "
            "(id,provider,endpoint_name,outcome,created_at,response_storage_uri) "
            "VALUES(%s,'p','gate-test','PROVIDER_ERROR','1900-01-01T00:00:00+00:00',%s)",
            (call_id, "local://fake/content/protected-response.txt"),
        )
        raw.execute(
            "INSERT INTO external_call_response_pending(call_id,storage_uri) VALUES(%s,%s)",
            (pending_id, "local://fake/content/protected-pending.txt"),
        )
        try:
            assert purge_expired_call_batch(raw, now=now, batch_size=1) == 0
            remaining = raw.execute(
                "SELECT response_storage_uri,cleanup_retry_at FROM external_call_logs WHERE id=%s",
                (call_id,),
            ).fetchone()
            assert remaining is not None
            assert remaining[0] == "local://fake/content/protected-response.txt"
            assert datetime.fromisoformat(str(remaining[1])) == now + timedelta(hours=1)
            assert _resolve_pending_response_object(raw, pending_id) is False
            pending = raw.execute(
                "SELECT storage_uri FROM external_call_response_pending WHERE call_id=%s",
                (pending_id,),
            ).fetchone()
            assert (
                pending is not None and pending[0] == "local://fake/content/protected-pending.txt"
            )
            fake_storage.delete_object.assert_not_called()
        finally:
            raw.execute("DELETE FROM external_call_logs WHERE id=%s", (call_id,))
            raw.execute(
                "DELETE FROM external_call_response_pending WHERE call_id=%s", (pending_id,)
            )


def test_short_reference_collision_and_ambiguous_legacy_prefix(pg_env: str) -> None:
    from app.control_routes import _task_ref_filter

    suffix = uuid.uuid4().hex
    target = f"collision-{suffix}"
    with psycopg.connect(pg_env) as raw:
        candidate = raw.execute(
            "SELECT upper(substr(md5(%s),1,8))", (f"VIDEO:{target}:0",)
        ).fetchone()[0]
        raw.execute(
            "INSERT INTO task_diagnostic_refs(task_type,task_id,short_ref,request_id,"
            "root_task_id) VALUES('VIDEO',%s,%s,'synthetic-root',%s)",
            (f"occupied-{suffix}", candidate, f"occupied-{suffix}"),
        )
        raw.execute("SELECT register_task_diagnostic_ref('VIDEO',%s,'{}')", (target,))
        conn = BusinessConnection.postgres(raw)
        assigned = raw.execute(
            "SELECT short_ref FROM task_diagnostic_refs WHERE task_id=%s", (target,)
        ).fetchone()[0]
        assert len(assigned) == 8 and assigned != candidate
        assert _task_ref_filter(conn, assigned.lower()).ids == (target,)
        for number in (1, 2):
            raw.execute(
                "SELECT register_task_diagnostic_ref('VIDEO',%s,'{}')",
                (f"abcdef12-{suffix}-{number}",),
            )
        with pytest.raises(HTTPException) as error:
            _task_ref_filter(conn, "abcdef12")
        assert error.value.status_code == 409


def test_enqueue_worker_retry_audit_and_service_log_share_a_stable_trace(
    pg_env: str, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from app.db_pg import pg_transaction
    from app.ops_metrics import bind_request_context, current_request_context
    from app.permissions import write_audit

    seed = _seed_failed_analysis(pg_env)
    task = str(uuid.uuid4())
    request_id = "synthetic-enqueue-" + uuid.uuid4().hex
    with bind_request_context(request_id=request_id, method="POST", route="/synthetic/queue"):
        with pg_transaction() as raw:
            raw.execute(
                "INSERT INTO analysis_tasks(id,project_id,asset_id,created_by_user_id"
                ",duration_seconds,status) SELECT %s,project_id,asset_id,created_by_u"
                "ser_id,duration_seconds,'FAILED' FROM analysis_tasks WHERE id=%s",
                (task, seed),
            )
            write_audit(
                BusinessConnection.postgres(raw),
                actor=_admin(),
                action="synthetic.task.queued",
                entity_type="analysis_task",
                entity_id=task,
                commit=False,
            )
    caplog.set_level(logging.INFO, logger="app.external_calls")
    for attempt in (1, 2):
        with external_call_context("ANALYSIS", task, attempt=attempt):
            assert current_request_context().request_id == request_id
            record_external_call(
                provider="apilio_gemini",
                endpoint="synthetic/poll",
                method="GET",
                url=None,
                outcome="TIMEOUT",
                exception_type="TimeoutError",
                latency_ms=20,
            )
            with pg_transaction() as raw:
                write_audit(
                    BusinessConnection.postgres(raw),
                    actor=_admin(),
                    action="synthetic.task.retry",
                    entity_type="analysis_task",
                    entity_id=task,
                    commit=False,
                )
    assert current_request_context() is None
    with psycopg.connect(pg_env) as raw:
        rows = raw.execute(
            "SELECT request_id,attempt,exception_type FROM external_call_logs WHERE t"
            "ask_id=%s ORDER BY attempt",
            (task,),
        ).fetchall()
        assert rows == [(request_id, 1, "TimeoutError"), (request_id, 2, "TimeoutError")]
        metadata = raw.execute(
            "SELECT metadata_json FROM audit_logs WHERE entity_id=%s", (task,)
        ).fetchall()
        assert len(metadata) == 3
        assert all(
            json.loads(row[0])["trace_request_id"] == request_id
            and json.loads(row[0])["request_id"] == request_id
            for row in metadata
        )
    traced = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "app.external_calls" and record.message.startswith("{")
    ]
    assert len(traced) == 4 and all(
        item["request_id"] == request_id and item["task_id"] == task for item in traced
    )


def test_two_replacements_keep_three_vendor_ids_and_global_calls_in_one_history(
    pg_env: str,
) -> None:
    from fastapi import Response

    from app.control_routes import (
        _task_ref_filter,
        list_generation_record_calls,
        list_global_external_calls,
    )

    root, _ = _seed_video_with_thumbnail(pg_env)
    ids = [root, str(uuid.uuid4()), str(uuid.uuid4())]
    with psycopg.connect(pg_env) as raw:
        for previous, current in zip(ids, ids[1:], strict=False):
            raw.execute(
                "INSERT INTO generation_tasks(id,batch_id,generation_mode,provider,mo"
                "del,status,retry_of_task_id,created_at_utc) SELECT %s,batch_id,gener"
                "ation_mode,provider,model,'FAILED',%s,now() FROM generation_tasks WH"
                "ERE id=%s",
                (current, previous, previous),
            )
    vendors = [f"synthetic-vendor-{uuid.uuid4().hex}" for _ in ids]
    for attempt, (task, vendor) in enumerate(zip(ids, vendors, strict=True), 1):
        with external_call_context("VIDEO", task, attempt=attempt):
            record_external_call(
                provider="metaso",
                endpoint="synthetic/submit",
                method="POST",
                url=None,
                outcome="PROVIDER_ERROR",
                provider_task_id=vendor,
                response_body='{"code":"SYNTHETIC_UNMAPPED","message":"synthetic reason"}',
            )
    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        refs = raw.execute(
            "SELECT short_ref,root_task_id,request_id FROM task_diagnostic_refs WHERE"
            " task_id=ANY(%s)",
            (ids,),
        ).fetchall()
        assert len({row[0] for row in refs}) == 3
        assert {row[1] for row in refs} == {root}
        assert len({row[2] for row in refs}) == 1
        for ref in [*ids, *vendors, *(row[0] for row in refs)]:
            assert set(_task_ref_filter(conn, ref).ids) >= set(ids)
        calls = list_generation_record_calls(conn, _admin(), "VIDEO", ids[-1], limit=200, offset=0)
        assert calls.total == 3 and {call.provider_task_id for call in calls.items} == set(vendors)
        page = list_global_external_calls(
            conn,
            _admin(),
            Response(),
            provider=None,
            endpoint=None,
            outcome=None,
            task_ref=vendors[0],
            created_from=None,
            created_to=None,
            limit=50,
            offset=0,
        )
        assert page.total == 3


def test_cross_provider_same_vendor_reference_requires_provider_qualification(pg_env: str) -> None:
    from app.control_routes import _task_ref_filter

    first = _seed_failed_analysis(pg_env)
    second = _seed_failed_analysis(pg_env)
    vendor = "shared-synthetic-" + uuid.uuid4().hex
    for provider, task in [("provider-a", first), ("provider-b", second)]:
        with external_call_context("ANALYSIS", task):
            record_external_call(
                provider=provider,
                endpoint="synthetic/submit",
                method="POST",
                url=None,
                outcome="SUCCEEDED",
                provider_task_id=vendor,
            )
    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        with pytest.raises(HTTPException) as error:
            _task_ref_filter(conn, vendor)
        assert error.value.status_code == 409
        assert _task_ref_filter(conn, "provider-a:" + vendor).ids == (first,)
        assert _task_ref_filter(conn, "provider-b:" + vendor).ids == (second,)


def test_same_internal_code_different_actual_reasons_do_not_merge(pg_env: str) -> None:
    from app.control_routes import TaskRefFilter, _generation_failure_reasons

    ids = [_seed_video_with_thumbnail(pg_env)[0] for _ in range(3)]
    with psycopg.connect(pg_env) as raw:
        raw.execute(
            "UPDATE generation_tasks SET status='FAILED',error_code='PROVIDER_TERMINA"
            "L' WHERE id=ANY(%s)",
            (ids,),
        )
    for task, reason in zip(
        ids, ["synthetic cause A", "synthetic cause B", "synthetic cause A"], strict=True
    ):
        with external_call_context("VIDEO", task):
            record_external_call(
                provider="metaso",
                endpoint="synthetic/poll",
                method="GET",
                url=None,
                outcome="PROVIDER_ERROR",
                response_body=json.dumps({"status": "failed", "message": reason}),
            )
    with psycopg.connect(pg_env) as raw:
        items = _generation_failure_reasons(
            BusinessConnection.postgres(raw),
            username=None,
            status=None,
            record_type=None,
            failure_phase=None,
            created_from=None,
            created_to=None,
            task_ref=TaskRefFilter(ids=tuple(ids), prefix=None),
        )
    assert {(item.reason, item.count) for item in items} == {
        ("synthetic cause A", 2),
        ("synthetic cause B", 1),
    }


def test_long_request_body_permissions_and_pending_provider_namespace(
    route_client: TestClient, pg_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.failure_runbook import FailureExplanation
    from app.provider_failure_runbook import PROVIDER_FAILURE_MAPPINGS, ProviderFailureMapping

    task = _seed_failed_analysis(pg_env)
    code = "SYNTHETIC_" + uuid.uuid4().hex
    prompt = "synthetic long prompt " * 700 + "TAIL-MARKER"
    for provider in ("synthetic-provider-a", "synthetic-provider-b"):
        with external_call_context("ANALYSIS", task):
            record_external_call(
                provider=provider,
                endpoint="synthetic/prompt",
                method="POST",
                url="https://api.synthetic.test/submit?prompt=TAIL-MARKER&sig=FICTIONAL-SIGNATURE",
                outcome="PROVIDER_ERROR",
                request_summary={"prompt": prompt},
                response_body=json.dumps({"code": code, "message": prompt}),
            )
    _admin_session(route_client)
    calls = route_client.get(f"/api/control/generation-records/ANALYSIS/{task}/calls")
    assert calls.status_code == 200 and all(
        item["request_summary"]["prompt"].endswith("TAIL-MARKER") for item in calls.json()["items"]
    )
    pending = route_client.get("/api/control/provider-errors/pending").json()
    assert {item["provider"] for item in pending if item["error_code"] == code} == {
        "synthetic-provider-a",
        "synthetic-provider-b",
    }
    monkeypatch.setitem(
        PROVIDER_FAILURE_MAPPINGS,
        ("synthetic-provider-a", code.casefold()),
        ProviderFailureMapping(
            FailureExplanation(category="CONFIG", owner="ENGINEERING", advice="合成映射验证。"),
            "synthetic-revision",
            "synthetic-contract",
        ),
    )
    mapped = route_client.get(f"/api/control/generation-records/ANALYSIS/{task}/calls").json()[
        "items"
    ]
    assert (
        mapped[0]["failure_category"] == "CONFIG"
        and mapped[0]["mapping_revision"] == "synthetic-revision"
    )
    assert mapped[1]["failure_category"] == "UNCLASSIFIED"
    pending = route_client.get("/api/control/provider-errors/pending").json()
    assert {item["provider"] for item in pending if item["error_code"] == code} == {
        "synthetic-provider-b"
    }
    response = password_admin_session(route_client, "auditor_u")
    assert response.status_code == 201
    auditor = route_client.get(f"/api/control/generation-records/ANALYSIS/{task}/calls")
    assert "TAIL-MARKER" not in auditor.text
    for item in auditor.json()["items"]:
        assert (
            item["request_summary"] is None
            and item["provider_message"] is None
            and item["error_message"] is None
        )
        assert (
            route_client.get(f"/api/control/external-calls/{item['call_id']}/response").status_code
            == 403
        )


def test_task_reference_customer_ownership_and_only_safe_field(pg_env: str) -> None:
    from fastapi import Response

    from app.task_diagnostic_routes import customer_task_reference

    task = _seed_failed_analysis(pg_env)
    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        owner = raw.execute(
            "SELECT created_by_user_id FROM analysis_tasks WHERE id=%s", (task,)
        ).fetchone()[0]
        actor = CurrentUser(
            id=owner, username="synthetic", display_name="合成客户", role="customer"
        )
        assert list(
            customer_task_reference(conn, actor, Response(), "ANALYSIS", task).model_dump()
        ) == ["short_ref"]
        stranger = CurrentUser(
            id="unrelated-customer", username="synthetic", display_name="合成客户", role="customer"
        )
        with pytest.raises(HTTPException) as error:
            customer_task_reference(conn, stranger, Response(), "ANALYSIS", task)
        assert error.value.status_code == 404


def test_diagnostic_migrations_upgrade_downgrade_and_restore(pg_env: str) -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    close_pg_pool()
    with psycopg.connect(pg_env) as raw:
        original_count = raw.execute("SELECT count(*) FROM external_call_logs").fetchone()[0]
        assert raw.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname='external_call_provider_outcome_idx'"
        ).fetchone()
        assert (
            raw.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname='capture_task_diagnostic_ref'"
            ).fetchone()[0]
            == 9
        )
    server = Path(__file__).resolve().parent.parent
    config = Config(str(server / "alembic.ini"))
    config.set_main_option("script_location", str(server / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", pg_env.replace("postgresql://", "postgresql+psycopg://")
    )
    try:
        command.downgrade(config, "20261002T0430_payment_methods")
        with psycopg.connect(pg_env) as raw:
            assert raw.execute("SELECT to_regclass('task_diagnostic_refs')").fetchone()[0] is None
            assert (
                raw.execute(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name"
                    "='external_call_logs' AND column_name='exception_type'"
                ).fetchone()[0]
                == 0
            )
            assert (
                raw.execute("SELECT count(*) FROM external_call_logs").fetchone()[0]
                == original_count
            )
    finally:
        command.upgrade(config, "head")
    with psycopg.connect(pg_env) as raw:
        assert raw.execute("SELECT to_regclass('task_diagnostic_refs')").fetchone()[0]
        assert (
            raw.execute("SELECT count(*) FROM external_call_logs").fetchone()[0] == original_count
        )
