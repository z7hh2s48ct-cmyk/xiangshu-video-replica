"""管理端任务诊断：按任务编号 / 问题编号直查失败历史与上游诊断。

用户报障时只说得出「哪个任务拆解失败」或失败卡片上的「问题编号」；生成记录
列表回答的是「最后一次怎么样了」，重试前的失败只留在 ``analysis_task_attempts``
里。本套件在真 PG 上证明 ``GET /api/control/analysis-diagnostics``：

- 按 ``task_id`` 精确命中：任务摘要 + 按 attempt 升序的重试历史，每次尝试带
  错误码 / 失败阶段 / 可否重试 / 上游状态与原因；
- 按 ``request_id`` 检索：入队请求号命中任务行，失败当场请求号命中尝试行；
- 两个条件都不给时拒绝——只做定点诊断，不做全量浏览；
- 查不到时返回空列表而非 404，检索无结果是正常结论。
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
from app.db_portable import BusinessConnection

DATABASE = "billing_obs_diag_test"


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip()


@pytest.fixture(scope="module")
def diagnostics_dsn() -> Iterator[str]:
    dsn = create_test_database(DATABASE)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(DATABASE)


@pytest.fixture(scope="module")
def seeded(diagnostics_dsn: str) -> dict[str, str]:
    """一个中断过一次、失败过两次的拆解任务：任务行只留最后一次结论。"""
    suffix = uuid.uuid4().hex[:12]
    ids = {
        "user": f"diag-user-{suffix}",
        "project": f"diag-project-{suffix}",
        "asset": f"diag-asset-{suffix}",
        "task": f"diag-task-{suffix}",
        # 入队时的请求号（任务行）与失败当场的请求号（尝试行）可以不同：
        # 前者回答「谁发起的」，后者回答「那次失败属于哪次请求」。
        "request": f"diag-req-{suffix}",
        "attempt_request": f"diag-req-attempt-{suffix}",
    }
    with psycopg.connect(diagnostics_dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES (%s, %s, '诊断客户', 'customer', 1)",
            (ids["user"], ids["user"]),
        )
        raw.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '诊断项目')",
            (ids["project"], ids["user"]),
        )
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, %s, 'reference_video', %s, %s, "
            "1024, 'video/mp4', %s)",
            (
                ids["asset"],
                ids["project"],
                f"cos://bucket/{ids['asset']}.mp4",
                "a" * 64,
                ids["user"],
            ),
        )
        raw.execute(
            "INSERT INTO analysis_tasks (id, project_id, asset_id, created_by_user_id, "
            "duration_seconds, status, attempt, request_id, error_code, "
            "error_message_redacted, failure_phase, retryable, upstream_diagnostic_json, "
            "created_at, updated_at, completed_at) "
            "VALUES (%s, %s, %s, %s, 8, 'FAILED', 3, %s, 'ANALYSIS_PROVIDER_FAILED', %s, "
            "'http', 1, %s, '2026-09-21 10:00:00', '2026-09-21 10:05:00', "
            "'2026-09-21 10:05:00')",
            (
                ids["task"],
                ids["project"],
                ids["asset"],
                ids["user"],
                ids["request"],
                "视频拆解服务拒绝了请求（HTTP 400）：model not available",
                json.dumps(
                    {
                        "http_status": 400,
                        "failure_phase": "http",
                        "reason": "model not available",
                    }
                ),
            ),
        )
        network_diagnostic = json.dumps(
            {"http_status": None, "failure_phase": "network", "reason": "URLError"}
        )
        http_diagnostic = json.dumps(
            {
                "http_status": 400,
                "failure_phase": "http",
                "reason": "model not available",
            }
        )
        raw.execute(
            "INSERT INTO analysis_task_attempts (id, task_id, attempt, status, error_code, "
            "error_message_redacted, failure_phase, retryable, upstream_diagnostic_json, "
            "request_id, completed_at, created_at) VALUES "
            "(%s, %s, 1, 'INTERRUPTED', 'ANALYSIS_WORKER_INTERRUPTED', %s, NULL, 1, NULL, "
            "%s, '2026-09-21 10:01:00', '2026-09-21 10:01:00'), "
            "(%s, %s, 2, 'FAILED', 'ANALYSIS_PROVIDER_UNREACHABLE', %s, 'network', 1, %s, "
            "%s, '2026-09-21 10:03:00', '2026-09-21 10:03:00'), "
            "(%s, %s, 3, 'FAILED', 'ANALYSIS_PROVIDER_FAILED', %s, 'http', 1, %s, "
            "%s, '2026-09-21 10:05:00', '2026-09-21 10:05:00')",
            (
                f"ata-{suffix}-1",
                ids["task"],
                "拆解任务执行中断，请重新拆解。",
                ids["request"],
                f"ata-{suffix}-2",
                ids["task"],
                "无法连接视频拆解服务，请检查网络后重试。",
                network_diagnostic,
                ids["attempt_request"],
                f"ata-{suffix}-3",
                ids["task"],
                "视频拆解服务拒绝了请求（HTTP 400）：model not available",
                http_diagnostic,
                ids["request"],
            ),
        )
    return ids


@pytest.fixture()
def owner() -> CurrentUser:
    return CurrentUser(id="admin_u", username="admin_u", display_name="管理员", role="admin")


def test_task_id_lookup_returns_the_attempt_history(
    diagnostics_dsn: str, seeded: dict[str, str], owner: CurrentUser
) -> None:
    from app.control_routes import get_analysis_diagnostics

    with psycopg.connect(diagnostics_dsn) as raw:
        result = get_analysis_diagnostics(
            BusinessConnection.postgres(raw), owner, task_id=seeded["task"]
        )

    assert result.total == 1
    record = result.items[0]
    assert record.task_id == seeded["task"]
    assert record.request_id == seeded["request"]
    assert record.username == seeded["user"]
    assert record.project_name == "诊断项目"
    assert record.status == "FAILED"
    assert record.attempt == 3
    assert record.error_code == "ANALYSIS_PROVIDER_FAILED"
    assert record.failure_phase == "http"
    assert record.retryable is True
    assert record.upstream_status == 400
    assert record.upstream_reason == "model not available"
    # P2-2：结论同时给出 runbook 译文（错误码 → 下一步动作）。
    assert record.advice is not None and "重试" in record.advice
    assert record.completed_at == "2026-09-21 10:05:00"

    # 重试历史按 attempt 升序：中断 → 网络失败 → 上游拒绝，每次结论不互相覆盖。
    assert [attempt.attempt for attempt in record.attempts] == [1, 2, 3]
    interrupted, network, rejected = record.attempts
    assert interrupted.status == "INTERRUPTED"
    assert interrupted.error_code == "ANALYSIS_WORKER_INTERRUPTED"
    assert interrupted.retryable is True
    assert interrupted.advice is not None and "容器" in interrupted.advice
    # 中断没有上游诊断：不能编造状态码。
    assert interrupted.upstream_status is None
    assert interrupted.upstream_reason is None
    assert network.status == "FAILED"
    assert network.failure_phase == "network"
    assert network.upstream_status is None
    assert network.upstream_reason == "URLError"
    assert network.request_id == seeded["attempt_request"]
    assert network.advice is not None and "网络" in network.advice
    assert rejected.status == "FAILED"
    assert rejected.upstream_status == 400
    assert rejected.upstream_reason == "model not available"
    assert rejected.request_id == seeded["request"]
    # 同一错误码走到哪一次尝试都是同一句建议。
    assert rejected.advice == record.advice


def test_request_id_lookup_matches_task_row_and_attempt_row(
    diagnostics_dsn: str, seeded: dict[str, str], owner: CurrentUser
) -> None:
    from app.control_routes import get_analysis_diagnostics

    with psycopg.connect(diagnostics_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        by_enqueue = get_analysis_diagnostics(conn, owner, request_id=seeded["request"])
        by_attempt = get_analysis_diagnostics(conn, owner, request_id=seeded["attempt_request"])
        combined_miss = get_analysis_diagnostics(
            conn, owner, task_id=seeded["task"], request_id="no-such-request"
        )

    # 入队请求号落在任务行上——失败卡片上的「问题编号」就是它。
    assert [item.task_id for item in by_enqueue.items] == [seeded["task"]]
    # 失败当场请求号只出现在尝试行：检索仍要能顺着历史找到任务。
    assert [item.task_id for item in by_attempt.items] == [seeded["task"]]
    # 两个条件同时给出时取交集，不会退化成并集。
    assert combined_miss.items == []


def test_lookup_without_any_key_is_rejected(diagnostics_dsn: str, owner: CurrentUser) -> None:
    from app.control_routes import get_analysis_diagnostics

    with psycopg.connect(diagnostics_dsn) as raw:
        with pytest.raises(HTTPException) as excinfo:
            get_analysis_diagnostics(BusinessConnection.postgres(raw), owner)

    assert excinfo.value.status_code == 422
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "ANALYSIS_DIAGNOSTICS_QUERY_REQUIRED"


def test_unknown_ids_return_an_empty_result(
    diagnostics_dsn: str, seeded: dict[str, str], owner: CurrentUser
) -> None:
    from app.control_routes import get_analysis_diagnostics

    with psycopg.connect(diagnostics_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        by_task = get_analysis_diagnostics(conn, owner, task_id="no-such-task")
        by_request = get_analysis_diagnostics(conn, owner, request_id="no-such-request")

    # 检索无结果是正常结论，不是 404：客服输入错编号时得到的是「没找到」。
    assert by_task.items == []
    assert by_request.items == []
