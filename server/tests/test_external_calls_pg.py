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
from app.external_calls import external_call_context, external_call_model, record_external_call

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
    from app.admin_auth_routes import ADMIN_SESSION_HMAC_KEY_ENV
    from app.control_routes import router as control_router

    application = FastAPI()
    application.include_router(control_router)
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
