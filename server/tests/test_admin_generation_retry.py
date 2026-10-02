"""管理端生成记录一键重试端点（方案 P1-4）的真实 PG 泳道套件。

业务裁决（能否重试、重扣积分、幂等操作记录、审计）由
``retry_generation_task`` 自身的测试矩阵覆盖；本套件钉住管理端路由的边界：
写契约三件套、业务错误码到运营中文文案的映射、PRE_PROVIDER 重新预扣、
同键重放不重复改状态。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-admin-gen-retry-tests-minimum-48-bytes-long",
)

import psycopg
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
from app.admin_generation_routes import router as admin_generation_router
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

RETRY_DB_NAME = "admin_gen_retry_test"
_RETRY_URL = "/api/control/generation-records/{task}/retry"

# 清单同时覆盖重试链路会写的每一张表（幂等操作、钱包账本、计费操作、
# 审计），否则上一条用例的余额或幂等行会泄漏进下一条。
_RESET_TABLES = (
    "user_queue_cursors, generation_task_operations, external_call_logs, assets, "
    "wallet_transactions, recharge_orders, internal_access_tokens, wallets, "
    "generation_tasks, generation_batches, billing_operations, audit_logs, "
    "runtime_settings, billing_tariffs, projects, versions, users"
)


def _pg_dsn() -> str:
    return os.environ.get(
        "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
    )


@pytest.fixture(scope="module")
def retry_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(RETRY_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(RETRY_DB_NAME)


def _reset(dsn: str) -> None:
    """清库并重建最小计费配置与两条账号（owner 与管理员）。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Append-only audit triggers refuse TRUNCATE; the T19 fixture precedent
        # bypasses triggers for the isolated fixture database only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {_RESET_TABLES} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        # 显式零售配置：每单位 1 积分（T26 先例），预扣数值可精确断言。
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES "
            "('video_768p',true,1),('video_2k',true,1)"
        )
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('u-owner', 'u-owner', 'Owner', 'user'), "
            "('admin_u', 'admin_u', 'Admin', 'admin')"
        )
        conn.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES ('p-1', 'P', 'u-owner')"
        )
        conn.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash,"
            " request_snapshot_json, status"
            ") VALUES ('b-1', 'p-1', 'u-owner', 'ik-b1', 'rh-b1', '{}', 'QUEUED')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('u-owner', 100, 0)"
        )


def _insert_task(
    dsn: str,
    task_id: str,
    *,
    status: str,
    archive_status: str = "PENDING",
    error_code: str | None = None,
    provider_result_url: str | None = None,
    prompt_snapshot_json: str | None = None,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO generation_tasks ("
            " id, batch_id, generation_mode, provider, model, status, archive_status,"
            " quality_status, error_code, provider_result_url, prompt_snapshot_json"
            ") VALUES (%s, 'b-1', 'I2V', 'metaso', 'MiniMax-H3', %s, %s, 'PENDING', %s, %s, %s)",
            (
                task_id,
                status,
                archive_status,
                error_code,
                provider_result_url,
                prompt_snapshot_json,
            ),
        )


def _wallet(dsn: str) -> tuple[int, int]:
    row = (
        psycopg.connect(dsn)
        .execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u-owner'"
        )
        .fetchone()
    )
    assert row is not None
    return int(row[0]), int(row[1])


def _task_row(dsn: str, task_id: str) -> dict[str, Any]:
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status, archive_status, error_code, retry_reason,"
            " retry_requested_by_user_id"
            " FROM generation_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
    assert row is not None
    return row


def _operation_count(dsn: str, task_id: str) -> int:
    row = (
        psycopg.connect(dsn)
        .execute("SELECT count(*) FROM generation_task_operations WHERE task_id = %s", (task_id,))
        .fetchone()
    )
    assert row is not None
    return int(row[0])


@pytest.fixture()
def api(retry_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, retry_dsn)
    _reset(retry_dsn)
    app = FastAPI()
    app.include_router(admin_generation_router)
    admin_actor = AdminActor(
        user_id="admin_u",
        username="admin_u",
        display_name="Admin",
        role="admin",
        is_super_admin=False,
        auth_method="password",
        session_id="sess-1",
        session_expires_at="2030-01-01T00:00:00Z",
        last_activity_at="2030-01-01T00:00:00Z",
    )
    app.dependency_overrides[get_admin_writer] = lambda: admin_actor
    with TestClient(app) as test_client:
        yield test_client
    close_pg_pool()


def _post(client: TestClient, task_id: str, *, key: str | None, body: dict[str, Any]) -> Any:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        _RETRY_URL.format(task=task_id),
        headers=headers,
        json=body,
    )


def test_retry_route_requires_write_contract(api: TestClient, retry_dsn: str) -> None:
    _insert_task(retry_dsn, "t-contract", status="FAILED", error_code="H3_SETTINGS_UNAVAILABLE")

    missing_key = _post(api, "t-contract", key=None, body={"confirm": True, "reason": "r"})
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    missing_confirm = _post(api, "t-contract", key="k-1", body={"reason": "r"})
    assert missing_confirm.status_code == 400
    assert missing_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    missing_reason = _post(api, "t-contract", key="k-1", body={"confirm": True, "reason": "  "})
    assert missing_reason.status_code == 400
    assert missing_reason.json()["detail"]["code"] == "REASON_REQUIRED"

    # 三次拒绝都发生在业务裁决之前：任务与幂等表保持原样。
    assert _task_row(retry_dsn, "t-contract")["status"] == "FAILED"
    assert _operation_count(retry_dsn, "t-contract") == 0


def test_retry_route_requeues_pre_provider_failure_and_recharges(
    api: TestClient, retry_dsn: str
) -> None:
    _insert_task(
        retry_dsn,
        "t-pre",
        status="FAILED",
        error_code="H3_SETTINGS_UNAVAILABLE",
        prompt_snapshot_json='{"output_duration_seconds": 2, "resolution": "768P"}',
    )

    response = _post(
        api, "t-pre", key="k-pre", body={"confirm": True, "reason": "运营重试：设置不可用已恢复"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "t-pre"
    assert body["status"] == "PENDING"

    row = _task_row(retry_dsn, "t-pre")
    assert row["status"] == "PENDING"
    assert row["error_code"] is None
    assert row["retry_reason"] == "运营重试：设置不可用已恢复"
    assert row["retry_requested_by_user_id"] == "admin_u"
    # PRE_PROVIDER 路径按 2 秒档位重新预扣：100 → 98 available，0 → 2 reserved。
    assert _wallet(retry_dsn) == (98, 2)


def test_retry_route_replays_same_key_without_second_charge(
    api: TestClient, retry_dsn: str
) -> None:
    _insert_task(
        retry_dsn,
        "t-replay",
        status="FAILED",
        error_code="H3_SETTINGS_UNAVAILABLE",
        prompt_snapshot_json='{"output_duration_seconds": 2, "resolution": "768P"}',
    )
    body = {"confirm": True, "reason": "运营重试"}

    first = _post(api, "t-replay", key="k-replay", body=body)
    assert first.status_code == 200
    second = _post(api, "t-replay", key="k-replay", body=body)
    assert second.status_code == 200

    # 同键重放返回当前任务态，不执行第二次调度写、不再扣一次积分。
    assert _operation_count(retry_dsn, "t-replay") == 1
    assert _wallet(retry_dsn) == (98, 2)


def test_retry_route_rejects_non_retryable_state_with_localized_message(
    api: TestClient, retry_dsn: str
) -> None:
    # 正常成功件（DIRECT 存档）：不落入任何可重试分支。
    _insert_task(retry_dsn, "t-done", status="SUCCEEDED", archive_status="DIRECT")

    response = _post(api, "t-done", key="k-done", body={"confirm": True, "reason": "r"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "TASK_RETRY_NOT_ALLOWED"
    assert detail["message"] == "任务当前状态不允许原地重试，请刷新后确认最新状态。"


def test_retry_route_localizes_missing_task(api: TestClient, retry_dsn: str) -> None:
    response = _post(api, "no-such-task", key="k-404", body={"confirm": True, "reason": "r"})
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "TASK_NOT_FOUND"
    assert detail["message"] == "任务不存在，或该记录不是视频生成任务（原地重试仅支持视频任务）。"


def test_retry_route_requeues_archive_recovery(api: TestClient, retry_dsn: str) -> None:
    # 已付款成片存档失败：ARCHIVE_ONLY 路径重新排队恢复，不重新扣费。
    _insert_task(
        retry_dsn,
        "t-archive",
        status="SUCCEEDED",
        archive_status="ARCHIVE_FAILED",
        provider_result_url="https://cdn.example.com/result.mp4",
    )

    response = _post(
        api, "t-archive", key="k-archive", body={"confirm": True, "reason": "恢复存档"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["archive_status"] == "ARCHIVE_FAILED"
    assert _wallet(retry_dsn) == (100, 0)


def test_archive_guidance_drives_existing_retry_without_new_charge(
    api: TestClient, retry_dsn: str
) -> None:
    from app.control_routes import ControlGenerationRecord, _attach_retry_guidance
    from app.db_portable import BusinessConnection

    _insert_task(
        retry_dsn,
        "t-guidance",
        status="SUCCEEDED",
        archive_status="ARCHIVE_FAILED",
        provider_result_url="https://synthetic.invalid/already-paid.mp4",
    )
    record = ControlGenerationRecord.model_construct(
        record_id="t-guidance", record_type="VIDEO", status="SUCCEEDED"
    )
    with psycopg.connect(retry_dsn) as conn:
        _attach_retry_guidance(BusinessConnection.postgres(conn), [record])
    assert record.retry_path == "ARCHIVE_ONLY"
    assert "不重新生成或扣费" in str(record.handling_advice)
    body = {"confirm": True, "reason": "恢复已付款归档"}
    for _ in range(2):
        response = _post(api, "t-guidance", key="k-guidance", body=body)
        assert response.status_code == 200
    assert _wallet(retry_dsn) == (100, 0)
    assert _operation_count(retry_dsn, "t-guidance") == 1


def test_paid_terminal_failure_has_no_retry_guidance(api: TestClient, retry_dsn: str) -> None:
    from app.control_routes import ControlGenerationRecord, _attach_retry_guidance
    from app.db_portable import BusinessConnection

    _insert_task(retry_dsn, "t-paid-terminal", status="FAILED", error_code="PROVIDER_FAILED")
    with psycopg.connect(retry_dsn) as conn:
        conn.execute(
            "UPDATE generation_tasks SET provider_task_id='synthetic-paid-task' WHERE id=%s",
            ("t-paid-terminal",),
        )
        record = ControlGenerationRecord.model_construct(
            record_id="t-paid-terminal", record_type="VIDEO", status="FAILED"
        )
        _attach_retry_guidance(BusinessConnection.postgres(conn), [record])
    assert record.retry_path is None
    assert "确认费用" in str(record.handling_advice)
    response = _post(
        api, "t-paid-terminal", key="k-paid", body={"confirm": True, "reason": "核对后处理"}
    )
    assert response.status_code == 409
    assert _wallet(retry_dsn) == (100, 0)
