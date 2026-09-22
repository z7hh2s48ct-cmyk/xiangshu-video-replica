"""拆解失败诊断要落库：真 PG 上走 fail_analysis_task 终态，证明诊断可查询。

单元层（``test_analysis_failure_diagnostics.py``）证明诊断随异常携带、日志带关联
键；本套件补齐「落库」一环——生产事故里 UI、DB、日志三处同时丢失根因，DB 的
``error_message_redacted`` 只剩兜底句。这里断言：

- FAILED 终态把 ``AnalysisProviderFailed.upstream_diagnostic`` 写进
  ``analysis_tasks.upstream_diagnostic_json``（含 http 阶段、脱敏原因）；
- 失败日志带 task/project/asset/attempt，可 grep 到具体任务；
- 非预期异常（代码缺陷）保留完整堆栈，且不伪造上游诊断。

P1-4 追加：``request_id`` 在入队时落到 ``analysis_tasks`` 并随响应回显，失败日志
与审计同样带请求号——客服凭失败卡片上的「任务编号 + 问题编号」即可直查。

P1-6 追加：失败历史按 attempt 归档到 ``analysis_task_attempts``。任务行的终态字段
只回答「最后一次」，认领一次就清空一次；这里证明每次失败/被接管都在历史表留一行，
认领前旧失败先归档再归零，审计 metadata 补 failure_phase/attempt/error_message。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.analysis import HTTP_FAILURE_PHASE, AnalysisProviderFailed, enqueue_analysis_task
from app.analysis_routes import (
    AnalysisTaskLease,
    acquire_analysis_task,
    analysis_task_response,
    fail_analysis_task,
)
from app.db_portable import BusinessConnection

DATABASE = "billing_obs_analysis_test"


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


def _seed_owner_and_asset(dsn: str) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"analysis-owner-{suffix}"
    project_id = f"analysis-project-{suffix}"
    asset_id = f"analysis-asset-{suffix}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (%s, %s, 'QA Owner')",
            (user_id, user_id),
        )
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, 'Diagnostics')",
            (project_id, user_id),
        )
        conn.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, %s, 'reference_video', %s, %s, "
            "1024, 'video/mp4', %s)",
            (asset_id, project_id, f"cos://bucket/{asset_id}.mp4", "a" * 64, user_id),
        )
    return user_id, project_id, asset_id


def _seed_running_task(dsn: str, *, request_id: str | None = None) -> AnalysisTaskLease:
    user_id, project_id, asset_id = _seed_owner_and_asset(dsn)
    task_id = f"analysis-op-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO analysis_tasks (id, project_id, asset_id, created_by_user_id, "
            "duration_seconds, status, attempt, locked_by, request_id) "
            "VALUES (%s, %s, %s, %s, 8, 'RUNNING', 1, 'worker-1', %s)",
            (task_id, project_id, asset_id, user_id, request_id),
        )
    return AnalysisTaskLease(
        id=task_id,
        project_id=project_id,
        asset_id=asset_id,
        created_by_user_id=user_id,
        duration_seconds=8,
        worker_id="worker-1",
        attempt=1,
        request_id=request_id,
    )


def _fail_task(dsn: str, lease: AnalysisTaskLease, cause: Exception) -> None:
    with psycopg.connect(dsn, autocommit=True) as raw:
        fail_analysis_task(BusinessConnection.postgres(raw), lease=lease, cause=cause)


def test_failed_task_persists_the_upstream_diagnostic(
    diagnostics_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """上游拒绝后，DB 要能回答「上游到底说了什么」，日志要能定位到具体任务。"""
    lease = _seed_running_task(diagnostics_dsn)
    diagnostic = {
        "http_status": 400,
        "failure_phase": HTTP_FAILURE_PHASE,
        "reason": "model gemini-3.8-flash is not available",
    }
    cause = AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 400）：model gemini-3.8-flash is not available",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
        upstream_diagnostic=diagnostic,
    )

    with caplog.at_level(logging.WARNING, logger="app.analysis_routes"):
        _fail_task(diagnostics_dsn, lease, cause)

    with psycopg.connect(diagnostics_dsn, row_factory=psycopg.rows.dict_row) as raw:
        row = raw.execute(
            "SELECT status, failure_phase, upstream_diagnostic_json "
            "FROM analysis_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["failure_phase"] == HTTP_FAILURE_PHASE
    stored = row["upstream_diagnostic_json"]
    assert stored["http_status"] == 400
    assert "gemini-3.8-flash is not available" in stored["reason"]

    assert f"task={lease.id}" in caplog.text
    assert f"project={lease.project_id}" in caplog.text
    assert f"asset={lease.asset_id}" in caplog.text
    assert "attempt=1" in caplog.text


def test_unexpected_failure_keeps_the_traceback_without_inventing_a_diagnostic(
    diagnostics_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """代码缺陷要留完整堆栈；没有上游参与时不伪造诊断。"""
    lease = _seed_running_task(diagnostics_dsn)
    # 生产里 cause 永远是被 raise 捕获的实例（worker 的 except 块）；带
    # traceback 地构造，日志才能渲染出可定位的堆栈。
    try:
        raise RuntimeError("bug: unexpected null handle")
    except RuntimeError as raised:
        cause: Exception = raised

    with caplog.at_level(logging.WARNING, logger="app.analysis_routes"):
        _fail_task(diagnostics_dsn, lease, cause)

    with psycopg.connect(diagnostics_dsn, row_factory=psycopg.rows.dict_row) as raw:
        row = raw.execute(
            "SELECT status, error_code, upstream_diagnostic_json FROM analysis_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["error_code"] == "ANALYSIS_WORKER_FAILED"
    assert row["upstream_diagnostic_json"] is None

    record = next(r for r in caplog.records if r.name == "app.analysis_routes")
    assert record.exc_info is not None
    assert "unexpected null handle" in caplog.text
    assert "Traceback" in caplog.text


def test_migration_exposes_the_diagnostic_column(diagnostics_dsn: str) -> None:
    """迁移必须真的把列建出来（jsonb、nullable），否则落库断言无从谈起。"""
    with psycopg.connect(diagnostics_dsn) as raw:
        row = raw.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'analysis_tasks' "
            "AND column_name = 'upstream_diagnostic_json'"
        ).fetchone()
    assert row == ("jsonb", "YES")


def test_enqueue_stamps_the_request_id_and_response_echoes_it(
    diagnostics_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入队时带上请求号，任务行与 API 响应都要能回答「哪个请求创建了它」。"""
    user_id, project_id, asset_id = _seed_owner_and_asset(diagnostics_dsn)
    monkeypatch.setattr("app.usage_billing.accept_operation", lambda *args, **kwargs: None)
    with psycopg.connect(
        diagnostics_dsn, autocommit=True, row_factory=psycopg.rows.dict_row
    ) as raw:
        conn = BusinessConnection.postgres(raw)
        row, created = enqueue_analysis_task(
            conn,
            project_id=project_id,
            asset_id=asset_id,
            created_by_user_id=user_id,
            duration_seconds=8,
            generation_context={},
            request_id="req-support-42",
        )
        assert created is True
        assert row["request_id"] == "req-support-42"
        # 失败卡片消费的就是这个响应：任务编号 + 问题编号都要在。
        response = analysis_task_response(row)
        assert response.id == row["id"]
        assert response.request_id == "req-support-42"

    user_id, project_id, asset_id = _seed_owner_and_asset(diagnostics_dsn)
    with psycopg.connect(
        diagnostics_dsn, autocommit=True, row_factory=psycopg.rows.dict_row
    ) as raw:
        conn = BusinessConnection.postgres(raw)
        legacy, created = enqueue_analysis_task(
            conn,
            project_id=project_id,
            asset_id=asset_id,
            created_by_user_id=user_id,
            duration_seconds=8,
            generation_context={},
        )
        assert created is True
        # 存量任务与无头部调用保持兼容：列为空、响应回显 None。
        assert legacy["request_id"] is None
        assert analysis_task_response(legacy).request_id is None


def test_failed_task_log_and_audit_keep_the_request_id(
    diagnostics_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """失败后，日志和审计都要带请求号，客服才能拿卡片编号直查两个日志面。"""
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    lease = _seed_running_task(diagnostics_dsn, request_id=request_id)
    cause = AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 400）",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
        upstream_diagnostic={"http_status": 400, "reason": "model unavailable"},
    )

    with caplog.at_level(logging.WARNING, logger="app.analysis_routes"):
        _fail_task(diagnostics_dsn, lease, cause)

    assert f"request={request_id}" in caplog.text

    with psycopg.connect(diagnostics_dsn, row_factory=psycopg.rows.dict_row) as raw:
        audit = raw.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE action = 'analysis.task_failed' AND entity_id = %s",
            (lease.id,),
        ).fetchone()
    assert audit is not None
    metadata = audit["metadata_json"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["request_id"] == request_id


def test_migration_exposes_the_request_id_column(diagnostics_dsn: str) -> None:
    """P1-4 的 request_id 列必须可空 text，存量任务不因迁移失败。"""
    with psycopg.connect(diagnostics_dsn) as raw:
        row = raw.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'analysis_tasks' "
            "AND column_name = 'request_id'"
        ).fetchone()
    assert row == ("text", "YES")


# ---------------------------------------------------------------------------
# P1-6：失败历史按 attempt 留痕
# ---------------------------------------------------------------------------


def _attempt_history(dsn: str, task_id: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as raw:
        return raw.execute(
            "SELECT attempt, status, error_code, error_message_redacted, failure_phase, "
            "retryable, upstream_diagnostic_json, request_id, completed_at "
            "FROM analysis_task_attempts WHERE task_id = %s ORDER BY attempt",
            (task_id,),
        ).fetchall()


def _acquire(dsn: str, *, worker_id: str) -> AnalysisTaskLease | None:
    with psycopg.connect(dsn, autocommit=True) as raw:
        return acquire_analysis_task(BusinessConnection.postgres(raw), worker_id=worker_id)


def _clear_pending_queue(dsn: str, *, keep: str | None = None) -> None:
    """队列是全局的（最早的 PENDING 先被认领）；本套件前序用例留下未消费的
    PENDING 任务，认领类断言前先清空，只保留本次用例要观察的那一行。"""
    with psycopg.connect(dsn, autocommit=True) as raw:
        if keep is None:
            raw.execute("DELETE FROM analysis_tasks WHERE status = 'PENDING'")
        else:
            raw.execute(
                "DELETE FROM analysis_tasks WHERE status = 'PENDING' AND id <> %s",
                (keep,),
            )


def test_failed_attempt_is_recorded_in_history(diagnostics_dsn: str) -> None:
    """失败在 attempt 历史里留一行：诊断不再只剩「最后一次」。"""
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    lease = _seed_running_task(diagnostics_dsn, request_id=request_id)
    cause = AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 400）：model unavailable",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
        upstream_diagnostic={
            "http_status": 400,
            "failure_phase": HTTP_FAILURE_PHASE,
            "reason": "model unavailable",
        },
    )

    _fail_task(diagnostics_dsn, lease, cause)

    history = _attempt_history(diagnostics_dsn, lease.id)
    assert len(history) == 1
    row = history[0]
    assert row["attempt"] == 1
    assert row["status"] == "FAILED"
    assert row["failure_phase"] == HTTP_FAILURE_PHASE
    assert row["upstream_diagnostic_json"]["http_status"] == 400
    assert row["request_id"] == request_id
    with psycopg.connect(diagnostics_dsn, row_factory=psycopg.rows.dict_row) as raw:
        task = raw.execute(
            "SELECT error_code, error_message_redacted, retryable "
            "FROM analysis_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
    assert row["error_code"] == task["error_code"]
    assert row["error_message_redacted"] == task["error_message_redacted"]
    assert row["retryable"] == task["retryable"]


def test_superseded_failure_write_still_records_the_attempt(diagnostics_dsn: str) -> None:
    """租约被取代时终态写入按设计丢弃，但这次失败本身必须留痕。"""
    lease = _seed_running_task(diagnostics_dsn)
    with psycopg.connect(diagnostics_dsn, autocommit=True) as raw:
        raw.execute(
            "UPDATE analysis_tasks SET locked_by = 'worker-2', attempt = 2 WHERE id = %s",
            (lease.id,),
        )

    _fail_task(diagnostics_dsn, lease, RuntimeError("failure of a superseded attempt"))

    with psycopg.connect(diagnostics_dsn, row_factory=psycopg.rows.dict_row) as raw:
        task = raw.execute(
            "SELECT status, attempt, locked_by FROM analysis_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
    # 接管者的租约未被旧 worker 的迟到失败覆盖。
    assert task["status"] == "RUNNING"
    assert task["attempt"] == 2
    assert task["locked_by"] == "worker-2"
    history = _attempt_history(diagnostics_dsn, lease.id)
    assert [(row["attempt"], row["status"]) for row in history] == [(1, "FAILED")]


def test_swept_running_attempt_is_archived_as_interrupted(diagnostics_dsn: str) -> None:
    """锁过期的 RUNNING 被接管时，被中断的那次尝试要留下可诊断的历史。"""
    lease = _seed_running_task(diagnostics_dsn)
    with psycopg.connect(diagnostics_dsn, autocommit=True) as raw:
        raw.execute(
            "UPDATE analysis_tasks SET locked_until = %s WHERE id = %s",
            ("2000-01-01 00:00:00", lease.id),
        )
    _clear_pending_queue(diagnostics_dsn)

    acquired = _acquire(diagnostics_dsn, worker_id="worker-2")

    # 被中断的任务不会自动重排：队列里没有 PENDING 时认领为空。
    assert acquired is None
    with psycopg.connect(diagnostics_dsn, row_factory=psycopg.rows.dict_row) as raw:
        task = raw.execute(
            "SELECT status, error_code, retryable, locked_by FROM analysis_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
    assert task["status"] == "FAILED"
    assert task["error_code"] == "ANALYSIS_WORKER_INTERRUPTED"
    assert task["retryable"] == 1
    assert task["locked_by"] is None
    history = _attempt_history(diagnostics_dsn, lease.id)
    assert [(row["attempt"], row["status"], row["error_code"]) for row in history] == [
        (1, "INTERRUPTED", "ANALYSIS_WORKER_INTERRUPTED")
    ]


def test_claim_archives_prior_failure_before_clearing(diagnostics_dsn: str) -> None:
    """认领不再静默清空：旧失败先归档成历史，再让新尝试从干净状态开始。"""
    lease = _seed_running_task(diagnostics_dsn)
    with psycopg.connect(diagnostics_dsn, autocommit=True) as raw:
        raw.execute(
            "UPDATE analysis_tasks SET status = 'PENDING', "
            "error_code = 'ANALYSIS_WORKER_INTERRUPTED', "
            "error_message_redacted = '拆解任务执行中断，请重新拆解。', "
            "failure_phase = 'provider', retryable = 1, "
            "locked_by = NULL, locked_until = NULL WHERE id = %s",
            (lease.id,),
        )
    _clear_pending_queue(diagnostics_dsn, keep=lease.id)

    acquired = _acquire(diagnostics_dsn, worker_id="worker-7")

    assert acquired is not None
    assert acquired.id == lease.id
    assert acquired.attempt == 2
    with psycopg.connect(diagnostics_dsn, row_factory=psycopg.rows.dict_row) as raw:
        task = raw.execute(
            "SELECT error_code, error_message_redacted, failure_phase, retryable "
            "FROM analysis_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
    # 新尝试的显示字段归零，不会把旧失败挂在 RUNNING 行上。
    assert task["error_code"] is None
    assert task["error_message_redacted"] is None
    assert task["failure_phase"] is None
    assert task["retryable"] == 0
    history = _attempt_history(diagnostics_dsn, lease.id)
    assert [(row["attempt"], row["status"], row["error_code"]) for row in history] == [
        (1, "SUPERSEDED", "ANALYSIS_WORKER_INTERRUPTED")
    ]


def test_failure_audit_records_phase_attempt_and_message(diagnostics_dsn: str) -> None:
    """审计补 failure_phase/attempt/error_message：不查日志也能回答卡在哪一步。"""
    lease = _seed_running_task(diagnostics_dsn)
    cause = AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 400）：model unavailable",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
        upstream_diagnostic={"http_status": 400, "reason": "model unavailable"},
    )

    _fail_task(diagnostics_dsn, lease, cause)

    with psycopg.connect(diagnostics_dsn, row_factory=psycopg.rows.dict_row) as raw:
        audit = raw.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE action = 'analysis.task_failed' AND entity_id = %s",
            (lease.id,),
        ).fetchone()
    assert audit is not None
    metadata = audit["metadata_json"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["attempt"] == 1
    assert metadata["failure_phase"] == HTTP_FAILURE_PHASE
    assert "model unavailable" in metadata["error_message"]


def test_migration_exposes_the_attempt_history_table(diagnostics_dsn: str) -> None:
    """迁移必须把 attempt 历史表建出来：列形状与唯一约束是留痕的前提。"""
    with psycopg.connect(diagnostics_dsn) as raw:
        columns = raw.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'analysis_task_attempts'"
        ).fetchall()
        unique = raw.execute(
            "SELECT count(*) FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = 'public' "
            "AND c.conname = 'uq_analysis_task_attempts_task_attempt'"
        ).fetchone()
    shapes = {name: (data_type, nullable) for name, data_type, nullable in columns}
    assert shapes["task_id"] == ("text", "NO")
    assert shapes["attempt"] == ("integer", "NO")
    assert shapes["status"] == ("text", "NO")
    assert shapes["upstream_diagnostic_json"] == ("jsonb", "YES")
    assert unique == (1,)
