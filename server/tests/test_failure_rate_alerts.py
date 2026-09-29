"""失败率告警（方案 P1-5）的真实 PG 泳道套件。

钉住统计口径本身：终局行进分母（客户取消与进行中不算）、窗口按最后状态
变化时间、口播展示码按状态映射（表里没有 error_code 列——P1-3 曾在此
处取列回归）、最小样本与阈值、runbook 分类/建议挂载。7 类任务各插一条
失败行，让 UNION 的每个分支真实执行——这正是历史回归（列不存在）的
防线。端点很薄（AdminReader + pg_transaction），由 TestClient 冒烟钉住。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-failure-rate-alerts-minimum-48-bytes-long",
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

from app.admin_auth_routes import AdminActor, get_admin_actor
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection
from app.failure_rate_alerts import (
    FAILURE_RATE_MIN_SAMPLE,
    FAILURE_RATE_THRESHOLD_PERCENT,
    FAILURE_RATE_WINDOW_MINUTES,
    build_failure_rate_report,
)
from app.failure_rate_alerts import router as admin_alerts_router

ALERTS_DB_NAME = "failure_rate_alerts_test"
_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

# 统计只读本轮种子涉及的表；CASCADE 负责清掉引用它们的行。alert_settings
# 的单行引用 users（接收人外键），显式列入让清理不依赖 CASCADE 推断。
_RESET_TABLES = (
    "audit_logs, character_generation_tasks, character_sheet_tasks, "
    "first_frame_tasks, source_frame_tasks, analysis_tasks, oral_tasks, "
    "oral_avatars, person_identities, character_versions, character_personas, "
    "assets, generation_tasks, generation_batches, projects, users, alert_settings"
)


def _pg_dsn() -> str:
    return os.environ.get(
        "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
    )


@pytest.fixture(scope="module")
def alerts_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(ALERTS_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(ALERTS_DB_NAME)


@pytest.fixture()
def seeded(alerts_dsn: str) -> str:
    """清库并重建最小依赖（账号、项目、视频批次）。"""
    with psycopg.connect(alerts_dsn, autocommit=True) as conn:
        # append-only 审计触发器拒绝 TRUNCATE；隔离夹具库内按 P1-4 先例
        # 临时关闭触发器再清。
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {_RESET_TABLES} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('u-1', 'u-1', 'Owner', 'user')"
        )
        # 告警接收人候选（设置端点）需要真实的管理员账号：启用中的 admin /
        # auditor 可被选中，停用管理员与普通用户不进候选。
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) VALUES "
            "('admin_u', 'admin_u', 'Admin', 'admin', 1), "
            "('auditor_u', 'auditor_u', 'Auditor', 'auditor', 1), "
            "('inactive_admin', 'inactive_admin', 'Off Duty', 'admin', 0)"
        )
        # 清理会连坐 alert_settings 单行（接收人外键引用 users）：重建 id=1
        # 行（默认口径与模块常量一致），设置页与报告读的正是它。
        conn.execute("INSERT INTO alert_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        conn.execute("INSERT INTO projects (id, name, owner_user_id) VALUES ('p-1', 'P', 'u-1')")
        # 口播任务对人物与分身有外键：建最小依赖链（owner 与 created_by 同一账号）。
        conn.execute(
            "INSERT INTO person_identities ("
            " id, owner_user_id, display_name, authorization_status,"
            " source_quality_status, status, created_by"
            ") VALUES ('ident-1', 'u-1', '口播人物', 'AUTHORIZED', 'PASSED', 'ACTIVE', 'u-1')"
        )
        # 形象版本链（人物画像 → 版本）：角色视图任务对版本有外键，缺了会
        # ForeignKeyViolation。
        conn.execute(
            "INSERT INTO character_personas (id, identity_id, name) "
            "VALUES ('persona-1', 'ident-1', '张工')"
        )
        conn.execute(
            "INSERT INTO character_versions (id, persona_id, version_number) "
            "VALUES ('version-1', 'persona-1', 1)"
        )
        conn.execute(
            "INSERT INTO oral_avatars ("
            " id, identity_id, owner_user_id, title, status, source_kind, source_asset_id"
            ") VALUES ('avatar-1', 'ident-1', 'u-1', '口播分身', 'READY', 'IMAGE', 'source-1')"
        )
        # 源帧与拆解任务对素材有外键：建最小素材行。
        conn.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes, content_type,"
            " created_by_user_id"
            ") VALUES ('asset-1', 'p-1', 'material_image', 'fake://assets/asset-1.png', %s,"
            " 9, 'image/png', 'u-1')",
            ("a" * 64,),
        )
        conn.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash,"
            " request_snapshot_json, status"
            ") VALUES ('b-1', 'p-1', 'u-1', 'ik-b1', 'rh-b1', '{}', 'QUEUED')"
        )
    return alerts_dsn


def _fresh(minutes_ago: int = 10) -> str:
    return (_NOW - timedelta(minutes=minutes_ago)).isoformat()


def _video_task(
    dsn: str, task_id: str, status: str, *, updated_at: str, error_code: str | None = None
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO generation_tasks ("
            " id, batch_id, generation_mode, provider, model, status, archive_status,"
            " quality_status, error_code, created_at, updated_at"
            ") VALUES (%s, 'b-1', 'I2V', 'metaso', 'MiniMax-H3', %s, 'PENDING',"
            " 'PENDING', %s, %s, %s)",
            (task_id, status, error_code, updated_at, updated_at),
        )


def _oral_task(
    dsn: str, task_id: str, status: str, *, updated_at: str, error_message: str | None = None
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO oral_tasks ("
            " id, owner_user_id, identity_id, avatar_id, mode, title, status,"
            " estimated_cost_fen, idempotency_key, error_message, created_at, updated_at"
            ") VALUES (%s, 'u-1', 'ident-1', 'avatar-1', 'TTS', '口播', %s, 100, %s, %s,"
            " %s, %s)",
            (task_id, status, f"ik-{task_id}", error_message, updated_at, updated_at),
        )


def _all_type_failed_task(dsn: str, record_type: str, *, updated_at: str) -> None:
    """7 类任务各插一条 FAILED：驱动 UNION 的每个分支真实执行。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        if record_type == "FIRST_FRAME_IMAGE":
            conn.execute(
                "INSERT INTO first_frame_tasks ("
                " id, created_by_user_id, project_id, idempotency_key, request_hash,"
                " request_json, status, updated_at"
                ") VALUES ('ff-1', 'u-1', 'p-1', 'ik-ff', 'rh-ff', '{}', 'FAILED', %s)",
                (updated_at,),
            )
        elif record_type == "CHARACTER_SHEET_IMAGE":
            conn.execute(
                "INSERT INTO character_sheet_tasks ("
                " id, created_by_user_id, identity_id, idempotency_key, request_hash,"
                " request_json, operation, source_storage_uri, source_content_type,"
                " source_sha256, source_size_bytes, status, updated_at"
                ") VALUES ('cs-1', 'u-1', 'ident-1', 'ik-cs', 'rh-cs', '{}', 'CREATE',"
                " 'fake://uploads/source.png', 'image/png', %s, 9, 'FAILED', %s)",
                ("f" * 64, updated_at),
            )
        elif record_type == "CHARACTER_VIEW_IMAGE":
            conn.execute(
                "INSERT INTO character_generation_tasks ("
                " id, character_version_id, view_type, provider, model, idempotency_key,"
                " request_hash, candidate_number, status, created_by, updated_at"
                ") VALUES ('cv-1', 'version-1', 'FRONT_FACE', 'fake-image', 'fake-model',"
                " 'ik-cv', 'rh-cv', 1, 'FAILED', 'u-1', %s)",
                (updated_at,),
            )
        elif record_type == "SOURCE_FRAME_PROCESS":
            conn.execute(
                "INSERT INTO source_frame_tasks ("
                " id, project_id, asset_id, created_by_user_id, idempotency_key,"
                " request_hash, request_json, status, created_at, updated_at"
                ") VALUES ('sf-1', 'p-1', 'asset-1', 'u-1', 'ik-sf', 'rh-sf', '{}',"
                " 'FAILED', %s, %s)",
                (updated_at, updated_at),
            )
        elif record_type == "ANALYSIS":
            conn.execute(
                "INSERT INTO analysis_tasks ("
                " id, project_id, asset_id, created_by_user_id, duration_seconds, status,"
                " attempt, error_code, created_at, updated_at"
                ") VALUES ('an-1', 'p-1', 'asset-1', 'u-1', 8, 'FAILED', 1,"
                " 'ANALYSIS_PROVIDER_FAILED', %s, %s)",
                (updated_at, updated_at),
            )
        else:  # pragma: no cover - 测试自身使用错误
            raise AssertionError(f"unsupported record_type {record_type}")


def _report(dsn: str) -> object:
    with psycopg.connect(dsn) as raw:
        return build_failure_rate_report(BusinessConnection.postgres(raw), now=_NOW)


def _group(report: object, record_type: str) -> object:
    groups = getattr(report, "groups")
    return next(group for group in groups if group.record_type == record_type)


def test_report_counts_terminal_rows_in_window(seeded: str) -> None:
    for index in range(8):
        _video_task(seeded, f"v-ok-{index}", "SUCCEEDED", updated_at=_fresh())
    _video_task(seeded, "v-bad-1", "FAILED", updated_at=_fresh(), error_code="PROVIDER_TERMINAL")
    _video_task(seeded, "v-bad-2", "FAILED", updated_at=_fresh(), error_code="PROVIDER_TERMINAL")
    # 进行中与客户取消不进分母。
    _video_task(seeded, "v-running", "RUNNING", updated_at=_fresh())
    _video_task(seeded, "v-cancel", "CANCELLED", updated_at=_fresh())
    # 窗口外（90 分钟前）的失败不计数。
    _video_task(
        seeded,
        "v-stale",
        "FAILED",
        updated_at=_fresh(minutes_ago=FAILURE_RATE_WINDOW_MINUTES + 30),
        error_code="PROVIDER_TERMINAL",
    )

    video = _group(_report(seeded), "VIDEO")
    assert video.total == 10
    assert video.failed == 2
    assert video.failure_rate_percent == 20.0
    assert video.exceeded is False
    assert [error.error_code for error in video.top_errors] == ["PROVIDER_TERMINAL"]
    assert video.top_errors[0].count == 2
    # runbook 挂载：分类/处理人/建议与生成记录同一份词典。
    assert video.top_errors[0].category == "PROVIDER_FAULT"
    assert video.top_errors[0].owner == "OPS"
    assert video.top_errors[0].advice


def test_all_record_types_are_covered(seeded: str) -> None:
    """7 类任务各一条失败：UNION 的每个分支都必须真实执行（列名回归防线）。"""
    for record_type in (
        "FIRST_FRAME_IMAGE",
        "CHARACTER_SHEET_IMAGE",
        "CHARACTER_VIEW_IMAGE",
        "SOURCE_FRAME_PROCESS",
        "ANALYSIS",
    ):
        _all_type_failed_task(seeded, record_type, updated_at=_fresh())
    _video_task(seeded, "v-1", "FAILED", updated_at=_fresh(), error_code="H3_PROVIDER_FAILED")
    _oral_task(seeded, "o-1", "FAILED", updated_at=_fresh(), error_message="上游拒绝")

    report = _report(seeded)
    assert {group.record_type for group in report.groups} == {
        "VIDEO",
        "ORAL_VIDEO",
        "FIRST_FRAME_IMAGE",
        "CHARACTER_SHEET_IMAGE",
        "CHARACTER_VIEW_IMAGE",
        "SOURCE_FRAME_PROCESS",
        "ANALYSIS",
    }
    for group in report.groups:
        assert group.total == 1
        assert group.failed == 1
        assert group.failure_rate_percent == 100.0


def test_oral_error_code_maps_from_status(seeded: str) -> None:
    """口播表没有 error_code 列：展示码按状态映射，与记录组装同一口径。"""
    _oral_task(seeded, "o-failed", "FAILED", updated_at=_fresh(), error_message="生成失败")
    _oral_task(
        seeded, "o-uncertain", "SUBMISSION_UNCERTAIN", updated_at=_fresh(), error_message="未知"
    )
    _oral_task(seeded, "o-archive", "ARCHIVE_FAILED", updated_at=_fresh(), error_message="归档失败")
    _oral_task(seeded, "o-ok", "SUCCEEDED", updated_at=_fresh())

    oral = _group(_report(seeded), "ORAL_VIDEO")
    assert oral.total == 4
    assert oral.failed == 3
    codes = {error.error_code for error in oral.top_errors}
    assert codes == {"ORAL_TASK_FAILED", "ORAL_SUBMISSION_UNCERTAIN", "ORAL_ARCHIVE_FAILED"}
    archive_error = next(
        error for error in oral.top_errors if error.error_code == "ORAL_ARCHIVE_FAILED"
    )
    assert archive_error.category == "DEFECT"
    assert archive_error.owner == "OPS"


def test_threshold_respects_min_sample(seeded: str) -> None:
    for index in range(FAILURE_RATE_MIN_SAMPLE - 1):
        _video_task(
            seeded, f"v-{index}", "FAILED", updated_at=_fresh(), error_code="H3_PROVIDER_FAILED"
        )
    # 4/4 全失败仍不触发：样本不足时 100% 只是噪音，不是「突增」。
    report = _report(seeded)
    assert report.alerting is False
    assert _group(report, "VIDEO").exceeded is False

    _video_task(
        seeded,
        f"v-{FAILURE_RATE_MIN_SAMPLE - 1}",
        "FAILED",
        updated_at=_fresh(),
        error_code="H3_PROVIDER_FAILED",
    )
    crossed = _report(seeded)
    video = _group(crossed, "VIDEO")
    assert video.total == FAILURE_RATE_MIN_SAMPLE
    assert video.failure_rate_percent == 100.0
    assert video.exceeded is True
    assert crossed.alerting is True


def test_unknown_and_missing_error_codes_stay_fail_open(seeded: str) -> None:
    _video_task(
        seeded, "v-unknown", "FAILED", updated_at=_fresh(), error_code="NOT_REGISTERED_CODE"
    )
    _video_task(seeded, "v-null", "FAILED", updated_at=_fresh(), error_code=None)

    video = _group(_report(seeded), "VIDEO")
    assert video.failed == 2
    unknown = next(error for error in video.top_errors if error.error_code == "NOT_REGISTERED_CODE")
    assert unknown.category is None and unknown.owner is None and unknown.advice is None
    missing = next(error for error in video.top_errors if error.error_code is None)
    assert missing.count == 1


@pytest.fixture()
def api(seeded: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, seeded)
    app = FastAPI()
    app.include_router(admin_alerts_router)
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
    app.dependency_overrides[get_admin_actor] = lambda: admin_actor
    with TestClient(app) as test_client:
        yield test_client
    close_pg_pool()


def test_route_returns_report_for_admin(api: TestClient, seeded: str) -> None:
    # 路由内部用当前时刻：种子必须落在真实「近 1 小时」里。
    now_fresh = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    _video_task(seeded, "v-route", "FAILED", updated_at=now_fresh, error_code="H3_PROVIDER_FAILED")

    response = api.get("/api/control/alerts/failure-rate")
    assert response.status_code == 200
    body = response.json()
    assert body["window_minutes"] == FAILURE_RATE_WINDOW_MINUTES
    assert body["threshold_percent"] == 30.0
    assert body["min_sample_size"] == FAILURE_RATE_MIN_SAMPLE
    assert body["recipient_user_id"] is None
    assert body["recipient_display_name"] is None
    assert body["alerting"] is False  # 1/1 样本不足
    assert body["groups"][0]["record_type"] == "VIDEO"
    assert body["groups"][0]["failed"] == 1
    assert body["groups"][0]["top_errors"][0]["error_code"] == "H3_PROVIDER_FAILED"


# ---------------------------------------------------------------------------
# 通知与告警设置（P2-4）：接收人与失败率口径的读写
# ---------------------------------------------------------------------------

_SETTINGS_PATH = "/api/control/settings/alerts"
_CANDIDATES_PATH = "/api/control/settings/alerts/recipient-candidates"
_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_REPLAY_HEADER = "X-Idempotent-Replay"


def _put_settings(api: TestClient, *, key: str | None = None, **overrides: object) -> object:
    body: dict[str, object] = {
        "confirm": True,
        "reason": "调整告警口径",
        "recipient_user_id": None,
        "failure_rate_window_minutes": FAILURE_RATE_WINDOW_MINUTES,
        "failure_rate_threshold_percent": FAILURE_RATE_THRESHOLD_PERCENT,
        "failure_rate_min_sample": FAILURE_RATE_MIN_SAMPLE,
    }
    body.update(overrides)
    return api.put(
        _SETTINGS_PATH,
        headers={_IDEMPOTENCY_KEY_HEADER: key if key is not None else f"key-{uuid.uuid4()}"},
        json=body,
    )


def test_settings_route_returns_defaults(api: TestClient) -> None:
    response = api.get(_SETTINGS_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["recipient_user_id"] is None
    assert body["recipient_display_name"] is None
    assert body["failure_rate_window_minutes"] == FAILURE_RATE_WINDOW_MINUTES
    assert body["failure_rate_threshold_percent"] == FAILURE_RATE_THRESHOLD_PERCENT
    assert body["failure_rate_min_sample"] == FAILURE_RATE_MIN_SAMPLE
    assert body["updated_at"]  # 迁移默认 clock_timestamp 写入


def test_update_settings_round_trip_and_report_reflects(api: TestClient, seeded: str) -> None:
    # 90 分钟前的失败：默认 60 分钟窗口外；窗口改 120 后计入。
    _video_task(
        seeded,
        "v-old",
        "FAILED",
        updated_at=(datetime.now(UTC) - timedelta(minutes=90)).isoformat(),
        error_code="H3_PROVIDER_FAILED",
    )
    for index in range(2):
        _video_task(
            seeded,
            f"v-new-{index}",
            "FAILED",
            updated_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
            error_code="H3_PROVIDER_FAILED",
        )

    before = api.get("/api/control/alerts/failure-rate").json()
    assert before["alerting"] is False  # 2/2 样本不足

    updated = _put_settings(
        api,
        recipient_user_id="auditor_u",
        failure_rate_window_minutes=120,
        failure_rate_threshold_percent=50.0,
        failure_rate_min_sample=2,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["recipient_user_id"] == "auditor_u"
    assert body["recipient_display_name"] == "Auditor"
    assert body["failure_rate_window_minutes"] == 120
    assert body["failure_rate_threshold_percent"] == 50.0
    assert body["failure_rate_min_sample"] == 2

    # 报告立即反映新口径：120 分钟窗口把 90 分钟前的失败计入 → 3/3 ≥ 50%。
    after = api.get("/api/control/alerts/failure-rate").json()
    assert after["window_minutes"] == 120
    assert after["threshold_percent"] == 50.0
    assert after["min_sample_size"] == 2
    assert after["recipient_user_id"] == "auditor_u"
    assert after["recipient_display_name"] == "Auditor"
    assert after["total"] == 3
    assert after["alerting"] is True

    # 设置页与报告读同一行：GET 回读一致。
    round_trip = api.get(_SETTINGS_PATH).json()
    assert round_trip["recipient_user_id"] == "auditor_u"
    assert round_trip["failure_rate_window_minutes"] == 120


def test_update_settings_validates_recipient(api: TestClient) -> None:
    ghost = _put_settings(api, recipient_user_id="ghost")
    assert ghost.status_code == 400
    assert ghost.json()["detail"]["code"] == "ALERT_SETTINGS_VALIDATION_FAILED"

    # 普通用户不是管理端账号；停用的管理员也不能接告警。
    assert _put_settings(api, recipient_user_id="u-1").status_code == 400
    assert _put_settings(api, recipient_user_id="inactive_admin").status_code == 400

    # 显式清空是合法状态；空串等价清空。
    assert _put_settings(api, recipient_user_id="admin_u").status_code == 200
    cleared = _put_settings(api, recipient_user_id=None)
    assert cleared.status_code == 200
    assert cleared.json()["recipient_user_id"] is None
    assert _put_settings(api, recipient_user_id="admin_u").status_code == 200
    blank = _put_settings(api, recipient_user_id="   ")
    assert blank.status_code == 200
    assert blank.json()["recipient_user_id"] is None


def test_update_settings_requires_write_contract(api: TestClient) -> None:
    no_key = _put_settings(api, key="")
    assert no_key.status_code == 400
    assert no_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    no_confirm = _put_settings(api, confirm=False)
    assert no_confirm.status_code == 400
    assert no_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    no_reason = _put_settings(api, reason="   ")
    assert no_reason.status_code == 400
    assert no_reason.json()["detail"]["code"] == "REASON_REQUIRED"


def test_update_settings_replay_and_conflict(api: TestClient) -> None:
    key = f"key-{uuid.uuid4()}"
    first = _put_settings(api, key=key, failure_rate_threshold_percent=45.0)
    assert first.status_code == 200, first.text

    replayed = _put_settings(api, key=key, failure_rate_threshold_percent=45.0)
    assert replayed.status_code == 200
    assert replayed.headers.get(_REPLAY_HEADER) == "true"
    assert replayed.json()["failure_rate_threshold_percent"] == 45.0

    conflict = _put_settings(api, key=key, failure_rate_threshold_percent=55.0)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_update_settings_validates_ranges(api: TestClient) -> None:
    assert _put_settings(api, failure_rate_window_minutes=0).status_code == 422
    assert _put_settings(api, failure_rate_window_minutes=10081).status_code == 422
    assert _put_settings(api, failure_rate_threshold_percent=101).status_code == 422
    assert _put_settings(api, failure_rate_threshold_percent=-1).status_code == 422
    assert _put_settings(api, failure_rate_min_sample=0).status_code == 422


def test_update_settings_is_audited(api: TestClient, seeded: str) -> None:
    assert (
        _put_settings(
            api,
            recipient_user_id="auditor_u",
            failure_rate_threshold_percent=25.0,
            reason="把阈值收紧到 25%",
        ).status_code
        == 200
    )
    with psycopg.connect(seeded) as conn:
        row = conn.execute(
            "SELECT actor_user_id, entity_type, entity_id, metadata_json FROM audit_logs "
            "WHERE action = 'alerts.settings.update'"
        ).fetchone()
    assert row is not None
    assert row[0] == "admin_u"
    assert row[1] == "alert_settings"
    assert row[2] == "1"
    metadata = json.loads(row[3])
    assert metadata["reason"] == "把阈值收紧到 25%"
    assert metadata["old"]["failure_rate_threshold_percent"] == 30.0
    assert metadata["new"]["failure_rate_threshold_percent"] == 25.0
    assert metadata["new"]["recipient_user_id"] == "auditor_u"


def test_recipient_candidates_list_active_managers(api: TestClient) -> None:
    response = api.get(_CANDIDATES_PATH)
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["user_id"] for item in items] == ["admin_u", "auditor_u"]
    by_id = {item["user_id"]: item for item in items}
    assert by_id["admin_u"]["display_name"] == "Admin"
    assert by_id["auditor_u"]["role"] == "auditor"
    assert "inactive_admin" not in by_id
    assert "u-1" not in by_id
