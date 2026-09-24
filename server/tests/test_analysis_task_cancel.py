"""S11 — 取消拆解任务的取消语义 / 计费释放 / 晚到结果拦截（TEST-PG lane）。

背景（2026-09-21 复刻页稳定性评审 S11）：换源时旧拆解任务在服务端照跑、
照计费，结果却再也回不到界面。修复分两层：

- 服务端新增 ``POST /api/analysis-tasks/{task_id}/cancel``：PENDING 排队中的
  任务直接作废（worker 捡不到）；RUNNING 的任务取消后，worker 晚到的完成
  回写被 ``status='RUNNING' AND locked_by=`` 的状态 CAS 拦截，结果不发布、
  不重复结算。预留积分在取消事务内即时释放（cancelled 结算 = 用户不计费）。
- ``ck_analysis_tasks_status`` 只接受 PENDING/RUNNING/SUCCEEDED/FAILED，
  故取消落 ``status='FAILED'`` + ``error_code='ANALYSIS_TASK_CANCELLED'``
  （与 ``cancel_source_frame_task`` 同款），桌面端以此码区分「已取消」与真失败。
- 客户端换源 / 开始新的复刻前先取消旧任务（client 侧单测覆盖）。

The database is the only real dependency: a dedicated TEST-PG database created
through the CW-007 kit, migrated to alembic ``head`` and TRUNCATEd per test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

# Set the audit HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-s11-analysis-cancel-tests-minimum-48-bytes-long-1234",
)

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.analysis_routes import (
    AnalysisTaskLease,
    AnalysisTaskWork,
    acquire_analysis_task,
    cancel_analysis_task,
    complete_analysis_task,
)
from app.auth import CurrentUser
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.usage_billing import accept_operation, finish_source, reconcile_operations

S11_DB_NAME = "analysis_cancel_test"

# Every table the seed touches. TRUNCATE runs under
# ``session_replication_role = replica`` with CASCADE so FK / guard triggers
# (audit tables refuse truncation) do not block the reset.
_TABLES = (
    "analysis_tasks, billing_operations, wallet_transactions, billing_credit_lots, "
    "billing_tariffs, assets, projects, wallets, audit_logs, users"
)


def _owner() -> CurrentUser:
    return CurrentUser(id="owner-1", username="owner_1", display_name="Owner", role="employee")


@pytest.fixture(scope="module")
def cancel_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(S11_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(S11_DB_NAME)


@pytest.fixture()
def cancel_state(cancel_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, cancel_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield cancel_dsn
    close_pg_pool()


def _seed_task(
    dsn: str,
    *,
    status: str = "PENDING",
    wallet_credits: int = 10,
    settle: bool = False,
) -> None:
    """任务 + 入队事务内的挂起计费（与 enqueue_analysis_task 同款形状）。

    ``settle=True`` 时按成功路径结算（RESERVE → SETTLE，扣 1 积分），用于
    模拟「已交付的 SUCCEEDED 任务」；否则只挂 RESERVE。
    """
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        pg.execute(
            "INSERT INTO billing_tariffs(service, enabled, unit_credits) "
            "VALUES ('analysis', true, 1)"
        )
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('owner-1', 'owner_1', 'Owner', 'employee'), "
            "('auditor-1', 'auditor_1', 'Auditor', 'auditor')"
        )
        pg.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('owner-1', %s, 0)",
            (wallet_credits,),
        )
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES "
            "('proj-1', 'Replica Project', 'owner-1')"
        )
        pg.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, "
            " size_bytes, content_type, created_by_user_id) VALUES "
            "('asset-1', 'proj-1', 'reference_video', 'local://asset-1.mp4', "
            " repeat('a', 64), 1024, 'video/mp4', 'owner-1')"
        )
        if status == "RUNNING":
            pg.execute(
                "INSERT INTO analysis_tasks ("
                " id, project_id, asset_id, created_by_user_id, duration_seconds,"
                " status, attempt, locked_by, locked_until, started_at"
                ") VALUES ("
                " 'task-1', 'proj-1', 'asset-1', 'owner-1', 12,"
                " 'RUNNING', 1, 'worker-1', now() + interval '10 minutes', now())"
            )
        else:
            pg.execute(
                "INSERT INTO analysis_tasks ("
                " id, project_id, asset_id, created_by_user_id, duration_seconds, status"
                ") VALUES ('task-1', 'proj-1', 'asset-1', 'owner-1', 12, %s)",
                (status,),
            )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        accept_operation(conn, user_id="owner-1", service="analysis", source_id="task-1", units=1)
        if settle:
            finish_source(conn, "task-1", units=1, succeeded=True)


def _task_row(dsn: str) -> dict[str, Any]:
    with pg_transaction() as raw:
        row = (
            BusinessConnection.postgres(raw)
            .execute("SELECT * FROM analysis_tasks WHERE id = %s", ("task-1",))
            .fetchone()
        )
    assert row is not None
    return dict(row)


def _wallet(dsn: str) -> tuple[int, int]:
    with pg_transaction() as raw:
        row = (
            BusinessConnection.postgres(raw)
            .execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'owner-1'"
            )
            .fetchone()
        )
    assert row is not None
    return int(row["available_credits"]), int(row["reserved_credits"])


def _ledger(dsn: str) -> list[str]:
    with pg_transaction() as raw:
        rows = (
            BusinessConnection.postgres(raw)
            .execute(
                # 同事务内的 RESERVE→SETTLE/RELEASE 共享秒级 created_at，只有
                # 触发器分配的 ledger_sequence 能证明真实写入顺序。
                "SELECT type FROM wallet_transactions WHERE billing_operation_id = "
                "(SELECT id FROM billing_operations WHERE source_id = 'task-1' "
                " AND service = 'analysis') ORDER BY ledger_sequence"
            )
            .fetchall()
        )
    return [str(row["type"]) for row in rows]


def _operation_row(dsn: str) -> dict[str, Any]:
    with pg_transaction() as raw:
        row = (
            BusinessConnection.postgres(raw)
            .execute(
                "SELECT state, reserved_credits, actual_units, charged_credits "
                "FROM billing_operations WHERE source_id = 'task-1' AND service = 'analysis'"
            )
            .fetchone()
        )
    assert row is not None
    return dict(row)


def _audit_actions(dsn: str) -> list[str]:
    with pg_transaction() as raw:
        rows = (
            BusinessConnection.postgres(raw)
            .execute("SELECT action FROM audit_logs ORDER BY created_at, id")
            .fetchall()
        )
    return [str(row["action"]) for row in rows]


def _cancel(dsn: str, *, actor: CurrentUser | None = None) -> dict[str, Any]:
    with pg_transaction() as raw:
        row = cancel_analysis_task(
            BusinessConnection.postgres(raw),
            task_id="task-1",
            actor=actor or _owner(),
        )
    return dict(row)


def test_cancel_pending_releases_the_reservation_exactly_once(cancel_state: str) -> None:
    """排队中的任务取消：落 FAILED+取消码、预留即时释放、对账重扫不双结算。"""
    _seed_task(cancel_state)
    assert _wallet(cancel_state) == (9, 1)

    row = _cancel(cancel_state)

    assert str(row["status"]) == "FAILED"
    assert str(row["error_code"]) == "ANALYSIS_TASK_CANCELLED"
    assert row["completed_at"] is not None
    task = _task_row(cancel_state)
    assert task["status"] == "FAILED"
    assert task["error_code"] == "ANALYSIS_TASK_CANCELLED"
    # 取消文案告诉用户「这是主动取消、可以重来」，不是需要排查的失败。
    assert "取消" in str(task["error_message_redacted"])
    assert task["locked_by"] is None
    assert _wallet(cancel_state) == (10, 0)  # 预留全额归还
    assert _ledger(cancel_state) == ["RESERVE", "RELEASE"]
    operation = _operation_row(cancel_state)
    assert operation["state"] == "CANCELLED"  # 计费侧保留取消态，不写成普通失败
    assert int(operation["charged_credits"]) == 0  # 用户不计费
    assert _audit_actions(cancel_state) == ["analysis.task_cancelled"]

    # 后台对账重扫：operation 已离开 PENDING，不重复结算、钱包不再变化。
    with pg_transaction() as raw:
        reconcile_operations(BusinessConnection.postgres(raw))
    assert _ledger(cancel_state) == ["RESERVE", "RELEASE"]
    assert _wallet(cancel_state) == (10, 0)

    # 取消重放（用户重复点）返回当前事实，不再产生任何写入。
    replayed = _cancel(cancel_state)
    assert str(replayed["status"]) == "FAILED"
    assert str(replayed["error_code"]) == "ANALYSIS_TASK_CANCELLED"
    assert _ledger(cancel_state) == ["RESERVE", "RELEASE"]


def test_cancel_running_task_blocks_late_publication_and_release(cancel_state: str) -> None:
    """RUNNING 取消：worker 的 status+locked_by CAS 前提被破坏（结果不发布），
    预留释放，且任务不会被后续 worker 再捡起。

    晚到回写用真实的 complete_analysis_task 驱动（按取消前的 lease 形状），
    而不是用 acquire 做代理断言——只捡 PENDING 的 acquire 与 complete 的
    CAS 是两套逻辑，删弱 CAS 时前者不会变红。"""
    _seed_task(cancel_state, status="RUNNING")

    row = _cancel(cancel_state)

    assert str(row["status"]) == "FAILED"
    assert str(row["error_code"]) == "ANALYSIS_TASK_CANCELLED"
    task = _task_row(cancel_state)
    assert task["status"] == "FAILED"
    assert task["error_code"] == "ANALYSIS_TASK_CANCELLED"
    # complete_analysis_task 的 CAS 条件是 status='RUNNING' AND locked_by=worker：
    # 取消把两半都改掉后，晚到的完成回写只能 rollback，不发布也不结算。
    assert task["locked_by"] is None
    assert _wallet(cancel_state) == (10, 0)
    assert _ledger(cancel_state) == ["RESERVE", "RELEASE"]

    # 取消后的任务不是 PENDING，acquire（只捡 PENDING）不会再分配给它。
    with pg_transaction() as raw:
        lease = acquire_analysis_task(BusinessConnection.postgres(raw), worker_id="worker-2")
    assert lease is None

    # 晚到的完成回写：按取消前的 lease 形状真正调用 complete_analysis_task——
    # CAS（RUNNING + locked_by + attempt）已被取消破坏，只能 rollback：
    # 不发布任何 analysis / shot_card 版本、不写审计、不触碰钱包与账本。
    from app.analysis import FakeGemini, analyze_video

    work = AnalysisTaskWork(
        lease=AnalysisTaskLease(
            id="task-1",
            project_id="proj-1",
            asset_id="asset-1",
            created_by_user_id="owner-1",
            duration_seconds=12,
            worker_id="worker-1",
        ),
        provider=FakeGemini(),
        video_uri="fake",
        asset_uri="local://asset-1.mp4",
    )
    result = analyze_video(video_uri="fake", video_duration_seconds=12, provider=FakeGemini())
    with pg_transaction() as raw:
        complete_analysis_task(BusinessConnection.postgres(raw), work=work, result=result)
    task = _task_row(cancel_state)
    assert task["status"] == "FAILED"
    assert task["error_code"] == "ANALYSIS_TASK_CANCELLED"
    assert _wallet(cancel_state) == (10, 0)
    assert _ledger(cancel_state) == ["RESERVE", "RELEASE"]
    assert _audit_actions(cancel_state) == ["analysis.task_cancelled"]
    with pg_transaction() as raw:
        published = (
            BusinessConnection.postgres(raw)
            .execute("SELECT count(*) AS n FROM versions WHERE kind IN ('analysis', 'shot_card')")
            .fetchone()
        )
    assert published is not None and int(published["n"]) == 0


def test_cancel_does_not_touch_a_succeeded_task(cancel_state: str) -> None:
    """已交付（SUCCEEDED 且已结算）的任务不因取消动作作废，计费分毫不动。"""
    _seed_task(cancel_state, status="SUCCEEDED", settle=True)
    assert _wallet(cancel_state) == (9, 0)

    row = _cancel(cancel_state)

    assert str(row["status"]) == "SUCCEEDED"
    assert _task_row(cancel_state)["status"] == "SUCCEEDED"
    assert _wallet(cancel_state) == (9, 0)
    assert _ledger(cancel_state) == ["RESERVE", "SETTLE"]  # 不出现 RELEASE


def test_auditor_cannot_cancel_analysis_task(cancel_state: str) -> None:
    """审计员只读：取消被拒（403），任务保持原状。"""
    _seed_task(cancel_state)
    auditor = CurrentUser(
        id="auditor-1", username="auditor_1", display_name="Auditor", role="auditor"
    )

    with pytest.raises(HTTPException) as caught:
        _cancel(cancel_state, actor=auditor)

    assert caught.value.status_code == 403
    detail = caught.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "ROLE_FORBIDDEN"
    assert _task_row(cancel_state)["status"] == "PENDING"
    assert _ledger(cancel_state) == ["RESERVE"]
