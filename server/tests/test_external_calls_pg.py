"""第三方接口调用日志在真 PG 上的闭环（方案 P0-9 / P0-10 / P0-12）。

- 调用日志落库：原始响应、服务商原话、第三方任务号、任务归属，且已脱敏；
- 生成记录按第三方任务号或 8 位短编号都能查到同一条，并带处理建议与服务商原话；
- 调用列表按时间给出；查看原始响应写高敏审计，审计员被拒绝。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection
from app.external_calls import external_call_context, record_external_call

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
    with external_call_context("ANALYSIS", task_id, attempt=1):
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


def test_call_list_and_audited_raw_response(pg_env: str) -> None:
    from app.control_routes import list_generation_record_calls, read_external_call_response

    task_id = _seed_failed_analysis(pg_env)
    _record_failed_call(task_id, provider_task_id="resp-view-1")

    with psycopg.connect(pg_env) as raw:
        conn = BusinessConnection.postgres(raw)
        calls = list_generation_record_calls(conn, _admin(), "ANALYSIS", task_id)
        auditor_calls = list_generation_record_calls(conn, _auditor(), "ANALYSIS", task_id)
        [call] = calls.items
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
