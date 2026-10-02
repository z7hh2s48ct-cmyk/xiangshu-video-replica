"""管理端要能看见视频拆解：生成记录 + 失败聚合 + 仪表盘待办。

2026-09-20 事故复盘的另一半：拆解是付费上游调用，却在管理端「生成记录」里完全
缺席——失败原因、失败阶段、可否重试、上游原话都无处可查，客服只能看到一个
「失败」状态。P0-2 已把诊断落进 ``analysis_tasks``，本套件在真 PG 上证明它能
被管理端读出来：

- ``record_type=ANALYSIS`` 列出拆解任务，带失败阶段、可否重试、上游状态与原因，
  以及来自账本的已结算积分与上游成本（不猜、不以 0 代替未知）；
- ``failure_phase`` 只约束拆解分支，不会打到没有该列的表上；
- summary 聚合按 record_type × status 计数，并回答「拆解为什么失败」。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.usage_billing import accept_operation, finish_operation, record_attempt

DATABASE = "billing_obs_records_test"


def test_diagnostic_category_filter_paginates_complete_queue(records_dsn, seeded, owner):
    from app.control_routes import list_generation_records

    with psycopg.connect(records_dsn) as raw:
        raw.execute(
            "UPDATE analysis_tasks SET error_code='UNKNOWN_TEST_FAILURE' WHERE id=%s",
            (seeded["network"],),
        )
        unknown = list_generation_records(
            BusinessConnection.postgres(raw),
            owner,
            diagnostics=True,
            failure_category="UNCLASSIFIED",
            limit=1,
            offset=0,
        )
        assert unknown.total == 1
        assert unknown.items[0].record_id == seeded["network"]
        assert unknown.items[0].failure_category == "UNCLASSIFIED"
        all_failures = list_generation_records(
            BusinessConnection.postgres(raw), owner, diagnostics=True, limit=1, offset=0
        )
        second = list_generation_records(
            BusinessConnection.postgres(raw), owner, diagnostics=True, limit=1, offset=1
        )
        assert all_failures.total == second.total == 3
        assert all_failures.items[0].record_id != second.items[0].record_id
        raw.rollback()


def test_clone_records_query_summary_and_measured_status_history(records_dsn, seeded, owner):
    from app.control_routes import (
        list_generation_records,
        read_generation_record_history,
        summarize_generation_records,
    )

    with psycopg.connect(records_dsn) as raw:
        raw.execute(
            "INSERT INTO person_identities(id,owner_user_id,display_name,status) "
            "VALUES('history-person',%s,'隔离测试人物','ACTIVE')",
            (seeded["user"],),
        )
        asset = raw.execute(
            "SELECT id FROM assets WHERE created_by_user_id=%s LIMIT 1", (seeded["user"],)
        ).fetchone()[0]
        for table, kind in [("oral_avatars", "ORAL_AVATAR"), ("oral_voices", "ORAL_VOICE")]:
            extra_columns = ",source_kind" if table == "oral_avatars" else ""
            extra_values = ",'VIDEO'" if table == "oral_avatars" else ""
            raw.execute(
                f"INSERT INTO {table}"
                f"(id,identity_id,owner_user_id,title,status,source_asset_id{extra_columns}) "
                f"VALUES(%s,'history-person',%s,'测试克隆','PENDING',%s{extra_values})",
                (kind, seeded["user"], asset),
            )
            raw.execute(
                f"UPDATE {table} SET status='RUNNING',vendor_task_id=%s WHERE id=%s",
                (kind + "-vendor", kind),
            )
            raw.execute(
                f"UPDATE {table} SET status='FAILED',error_message='未分类错误' WHERE id=%s",
                (kind,),
            )
            page = list_generation_records(
                BusinessConnection.postgres(raw),
                owner,
                record_type=kind,
                diagnostics=True,
                failure_category="UNCLASSIFIED",
                limit=20,
                offset=0,
            )
            assert page.total == 1 and page.items[0].record_id == kind
            assert page.items[0].failed_at is not None
            assert (
                page.items[0].user_id == seeded["user"]
                and page.items[0].failure_category == "UNCLASSIFIED"
            )
            summary = summarize_generation_records(
                BusinessConnection.postgres(raw), owner, record_type=kind
            )
            assert [(item.record_type, item.status, item.count) for item in summary.counts] == [
                (kind, "FAILED", 1)
            ]
            assert summary.failure_reasons[0].count == 1
            by_vendor = list_generation_records(
                BusinessConnection.postgres(raw),
                owner,
                task_ref=kind + "-vendor",
                limit=20,
                offset=0,
            )
            assert [item.record_id for item in by_vendor.items] == [kind]
            history = read_generation_record_history(
                kind, kind, BusinessConnection.postgres(raw), owner, limit=2, offset=0
            )
            next_page = read_generation_record_history(
                kind, kind, BusinessConnection.postgres(raw), owner, limit=2, offset=2
            )
            assert history["total"] == next_page["total"] == 3
            assert [(item["before"], item["after"]) for item in history["items"]] == [
                ("RUNNING", "FAILED"),
                ("PENDING", "RUNNING"),
            ]
            assert [(item["before"], item["after"]) for item in next_page["items"]] == [
                (None, "PENDING")
            ]
            raw.execute(f"UPDATE {table} SET title='改名不改变状态' WHERE id=%s", (kind,))
            assert (
                read_generation_record_history(
                    kind, kind, BusinessConnection.postgres(raw), owner, limit=20, offset=0
                )["total"]
                == 3
            )
        # Old analysis rows have a current status but no invented transition backfill.
        old = read_generation_record_history(
            "ANALYSIS",
            seeded["network"],
            BusinessConnection.postgres(raw),
            owner,
            limit=20,
            offset=0,
        )
        # This fixture is created after head migration, so actual INSERT/UPDATE events exist.
        assert old["measurementStartedAt"] and old["historyRule"]
        raw.rollback()


UPSTREAM_REASON = "model gemini-3.8-flash is not available"


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip()


@pytest.fixture(scope="module")
def records_dsn() -> Iterator[str]:
    dsn = create_test_database(DATABASE)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(DATABASE)


@pytest.fixture(scope="module")
def seeded(records_dsn: str) -> dict[str, str]:
    return _seed(records_dsn)


def _seed(dsn: str) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"obs-cust-{suffix}"
    project_id = f"obs-project-{suffix}"
    asset_id = f"obs-asset-{suffix}"
    version_id = f"obs-version-{suffix}"
    ids = {
        "user": user_id,
        "https": f"obs-analysis-http-{suffix}",
        "https_second": f"obs-analysis-http2-{suffix}",
        "network": f"obs-analysis-network-{suffix}",
        "ok": f"obs-analysis-ok-{suffix}",
        "pending": f"obs-analysis-pending-{suffix}",
    }
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES (%s, %s, '观测客户', 'customer', 1)",
            (user_id, user_id),
        )
        raw.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '拆解观测项目')",
            (project_id, user_id),
        )
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, %s, 'reference_video', %s, %s, "
            "1024, 'video/mp4', %s)",
            (asset_id, project_id, f"cos://bucket/{asset_id}.mp4", "a" * 64, user_id),
        )
        raw.execute(
            "INSERT INTO versions (id, project_id, asset_id, kind, version_number, "
            "payload_json, created_by_user_id) VALUES (%s, %s, %s, 'analysis', 1, %s, %s)",
            (
                version_id,
                project_id,
                asset_id,
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider_response_ref": {
                            "provider": "apilio_gemini",
                            "model": "gemini-3.8-flash",
                            "response_id": "resp-obs-1",
                        },
                    }
                ),
                user_id,
            ),
        )
        for task_id, status, created_at in (
            (ids["https"], "FAILED", "2026-09-21 10:00:00"),
            (ids["https_second"], "FAILED", "2026-09-21 11:00:00"),
            (ids["network"], "FAILED", "2026-09-21 12:00:00"),
            (ids["ok"], "SUCCEEDED", "2026-09-21 13:00:00"),
            (ids["pending"], "PENDING", "2026-09-21 14:00:00"),
        ):
            raw.execute(
                "INSERT INTO analysis_tasks (id, project_id, asset_id, created_by_user_id, "
                "duration_seconds, status, attempt, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, 8, %s, %s, %s, %s)",
                (
                    task_id,
                    project_id,
                    asset_id,
                    user_id,
                    status,
                    1 if status != "PENDING" else 0,
                    created_at,
                    created_at,
                ),
            )
        raw.execute(
            "UPDATE analysis_tasks SET error_code = 'ANALYSIS_PROVIDER_FAILED', "
            "error_message_redacted = %s, failure_phase = 'http', retryable = 1, "
            "upstream_diagnostic_json = %s, completed_at = '2026-09-21 10:00:30' "
            "WHERE id IN (%s, %s)",
            (
                f"视频拆解服务拒绝了请求（HTTP 400）：{UPSTREAM_REASON}",
                json.dumps(
                    {"http_status": 400, "failure_phase": "http", "reason": UPSTREAM_REASON}
                ),
                ids["https"],
                ids["https_second"],
            ),
        )
        raw.execute(
            "UPDATE analysis_tasks SET error_code = 'ANALYSIS_PROVIDER_UNREACHABLE', "
            "error_message_redacted = '无法连接视频拆解服务，请检查网络后重试。', "
            "failure_phase = 'network', retryable = 1, upstream_diagnostic_json = %s, "
            "completed_at = '2026-09-21 12:00:20' WHERE id = %s",
            (
                json.dumps({"http_status": None, "failure_phase": "network", "reason": "URLError"}),
                ids["network"],
            ),
        )
        raw.execute(
            "UPDATE analysis_tasks SET result_version_id = %s, "
            "completed_at = '2026-09-21 13:01:00' WHERE id = %s",
            (version_id, ids["ok"]),
        )
        raw.execute(
            "INSERT INTO billing_tariffs (service, enabled, unit_credits, unit_cost_fen) "
            "VALUES ('analysis', TRUE, 4, 30)"
        )
        raw.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES (%s, 100, 0)",
            (user_id,),
        )
        # 走真实结算路径（accept → attempt → finish），积分与成本都由账本算出。
        conn = BusinessConnection.postgres(raw)
        operation = accept_operation(
            conn, user_id=user_id, service="analysis", source_id=ids["ok"], units=1
        )
        record_attempt(conn, operation_id=operation, attempt_key="first", usage=1)
        finish_operation(conn, operation_id=operation, units=1, succeeded=True)
    ids["version"] = version_id
    return ids


@pytest.fixture()
def owner() -> CurrentUser:
    return CurrentUser(id="admin_u", username="admin_u", display_name="管理员", role="admin")


def test_analysis_records_expose_failure_diagnostics_and_settled_evidence(
    records_dsn: str, seeded: dict[str, str], owner: CurrentUser
) -> None:
    from app.control_routes import list_generation_records

    with psycopg.connect(records_dsn) as raw:
        page = list_generation_records(
            BusinessConnection.postgres(raw), owner, record_type="ANALYSIS", limit=50, offset=0
        )

    assert page.total == len(page.items) == 5
    rows = {item.record_id: item for item in page.items}
    assert set(rows) == {
        seeded["https"],
        seeded["https_second"],
        seeded["network"],
        seeded["ok"],
        seeded["pending"],
    }
    for item in page.items:
        assert item.record_type == "ANALYSIS"
        assert item.username == seeded["user"]
        assert item.project_name == "拆解观测项目"

    failed = rows[seeded["https"]]
    assert failed.status == "FAILED"
    assert failed.failure_phase == "http"
    assert failed.retryable is True
    assert failed.upstream_status == 400
    assert failed.upstream_reason == UPSTREAM_REASON
    assert failed.error_code == "ANALYSIS_PROVIDER_FAILED"
    assert failed.charged_credits == 0
    # 没有成本证据就说未知，不用 0 冒充。
    assert failed.provider_cost is None
    assert failed.provider_cost_status == "UNAVAILABLE"

    network = rows[seeded["network"]]
    assert network.failure_phase == "network"
    assert network.retryable is True
    assert network.upstream_status is None
    assert network.upstream_reason == "URLError"

    settled = rows[seeded["ok"]]
    assert settled.status == "SUCCEEDED"
    assert settled.result_reference == seeded["version"]
    # 服务/模型来自结果版本的 provider_response_ref：上游调用的事实留痕。
    assert settled.provider == "apilio_gemini"
    assert settled.model == "gemini-3.8-flash"
    assert settled.charged_credits == 4
    assert settled.provider_cost == pytest.approx(0.3)
    assert settled.provider_cost_status == "ESTIMATED"
    assert settled.failure_phase is None
    assert settled.retryable is None

    pending = rows[seeded["pending"]]
    assert pending.status == "PENDING"
    assert pending.completed_at is None
    # 还没失败就不谈「能不能重试」。
    assert pending.retryable is None


def test_failure_phase_filter_only_narrows_the_analysis_branch(
    records_dsn: str, seeded: dict[str, str], owner: CurrentUser
) -> None:
    from app.control_routes import list_generation_records

    with psycopg.connect(records_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        narrowed = list_generation_records(
            conn,
            owner,
            record_type="ANALYSIS",
            failure_phase="network",
            limit=50,
            offset=0,
        )
        other_branch = list_generation_records(
            conn, owner, record_type="VIDEO", failure_phase="network", limit=50, offset=0
        )

    assert [item.record_id for item in narrowed.items] == [seeded["network"]]
    assert narrowed.total == 1
    # 其他类型没有 failure_phase 列：过滤器不得泄漏过去（否则 500）。
    assert other_branch.total == 0


def test_status_filter_accepts_several_values_for_one_business_state(
    records_dsn: str, seeded: dict[str, str], owner: CurrentUser
) -> None:
    """P0-6：一个下拉项（如「已取消」）可能对应多种底层拼写，逗号分隔一起查。"""
    from app.control_routes import list_generation_records, summarize_generation_records

    with psycopg.connect(records_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        combined = list_generation_records(
            conn, owner, record_type="ANALYSIS", status="SUCCEEDED, PENDING", limit=50, offset=0
        )
        summary = summarize_generation_records(
            conn, owner, record_type="ANALYSIS", status="PENDING,FAILED"
        )

    assert {item.record_id for item in combined.items} == {seeded["ok"], seeded["pending"]}
    assert combined.total == 2
    assert summary.total == 4
    # 含 FAILED 时失败原因聚合照常给出，不因为多值而被当成「非失败视图」。
    assert sum(item.count for item in summary.failure_reasons) == 3


def test_summary_aggregates_by_type_and_status_with_failure_reasons(
    records_dsn: str, owner: CurrentUser
) -> None:
    from app.control_routes import summarize_generation_records

    with psycopg.connect(records_dsn) as raw:
        summary = summarize_generation_records(BusinessConnection.postgres(raw), owner)

    assert summary.total == 5
    counts = {(item.record_type, item.status): item.count for item in summary.counts}
    assert counts[("ANALYSIS", "FAILED")] == 3
    assert counts[("ANALYSIS", "SUCCEEDED")] == 1
    assert counts[("ANALYSIS", "PENDING")] == 1

    reasons = summary.failure_reasons
    assert [item.count for item in reasons] == [2, 1]
    assert reasons[0].failure_phase == "http"
    assert reasons[0].error_code == "ANALYSIS_PROVIDER_FAILED"
    assert reasons[0].reason == UPSTREAM_REASON
    assert reasons[0].retryable is True
    # P2-2：聚合行带 runbook 译文，错误码不再是终点。
    assert reasons[0].advice is not None and "重试" in reasons[0].advice
    assert reasons[1].failure_phase == "network"
    assert reasons[1].advice is not None and "网络" in reasons[1].advice


def test_summary_honours_the_same_filters_as_the_records_list(
    records_dsn: str, owner: CurrentUser
) -> None:
    from app.control_routes import summarize_generation_records

    with psycopg.connect(records_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        narrowed = summarize_generation_records(
            conn, owner, record_type="ANALYSIS", failure_phase="network"
        )
        empty = summarize_generation_records(conn, owner, username="no-such-account")
        succeeded_only = summarize_generation_records(
            conn, owner, record_type="ANALYSIS", status="SUCCEEDED"
        )

    assert narrowed.total == 1
    assert [(item.record_type, item.status, item.count) for item in narrowed.counts] == [
        ("ANALYSIS", "FAILED", 1)
    ]
    assert [item.reason for item in narrowed.failure_reasons] == ["URLError"]

    assert empty.total == 0
    assert empty.counts == []
    assert empty.failure_reasons == []

    # 当前视图里没有失败行时不能凭空给失败原因。
    assert succeeded_only.failure_reasons == []
    assert succeeded_only.total == 1


def _seed_source_frames(dsn: str) -> dict[str, str]:
    """两条源画面任务：一条带语义质检审计留痕，一条没有。"""
    suffix = uuid.uuid4().hex[:12]
    user_id = f"obs-src-user-{suffix}"
    project_id = f"obs-src-project-{suffix}"
    asset_id = f"obs-src-asset-{suffix}"
    plain_task = f"obs-src-plain-{suffix}"
    scored_task = f"obs-src-scored-{suffix}"
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES (%s, %s, '源画面观测客户', 'customer', 1)",
            (user_id, user_id),
        )
        raw.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '源画面观测项目')",
            (project_id, user_id),
        )
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, %s, 'reference_video', %s, %s, "
            "1024, 'video/mp4', %s)",
            (asset_id, project_id, f"cos://bucket/{asset_id}.mp4", "b" * 64, user_id),
        )
        for task_id, created_at in (
            (plain_task, "2026-09-21 09:00:00"),
            (scored_task, "2026-09-21 09:30:00"),
        ):
            raw.execute(
                "INSERT INTO source_frame_tasks (id, project_id, asset_id, created_by_user_id, "
                "idempotency_key, request_hash, request_json, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, '{}', 'SUCCEEDED', %s)",
                (
                    task_id,
                    project_id,
                    asset_id,
                    user_id,
                    f"ik-{task_id}",
                    f"rh-{task_id}",
                    created_at,
                ),
            )
        # 只有这条任务跑过语义质检：审计留痕是它被归入 AI 评分类的依据。
        raw.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, "
            "metadata_json) VALUES (%s, NULL, 'source_frame.semantic_quality_started', "
            "'source_frame_task', %s, %s)",
            (
                f"obs-src-audit-{suffix}",
                scored_task,
                json.dumps({"provider": "apilio_gemini", "model": "gemini-3.8-flash"}),
            ),
        )
    return {"user": user_id, "plain": plain_task, "scored": scored_task}


def test_source_frame_filters_use_portable_sql_on_postgres(
    records_dsn: str, owner: CurrentUser
) -> None:
    """源画面筛选（素材处理 / AI 评分）在 PG 上必须可用。

    回归：列表 SQL 曾用 SQLite 专有的 json_valid/json_extract 做语义归类，
    PG 上直接 ``function json_valid(...) does not exist`` → 500，管理端两个
    筛选选项全废。summary 已改走审计 EXISTS（跨方言），本用例钉住列表分支
    （含 total 计数与行查询两处拼接点）同样只用跨方言 SQL。
    """
    seeded = _seed_source_frames(records_dsn)
    from app.control_routes import list_generation_records

    with psycopg.connect(records_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        process_page = list_generation_records(
            conn, owner, record_type="SOURCE_FRAME_PROCESS", limit=50, offset=0
        )
        scored_page = list_generation_records(
            conn, owner, record_type="SOURCE_FRAME_AI_SCORE", limit=50, offset=0
        )

    process_ids = {item.record_id for item in process_page.items}
    assert seeded["plain"] in process_ids
    assert seeded["scored"] not in process_ids
    assert process_page.total >= 1

    scored_ids = {item.record_id for item in scored_page.items}
    assert seeded["scored"] in scored_ids
    assert seeded["plain"] not in scored_ids
    assert scored_page.total >= 1
    scored_row = next(item for item in scored_page.items if item.record_id == seeded["scored"])
    assert scored_row.record_type == "SOURCE_FRAME_AI_SCORE"
    assert scored_row.username == seeded["user"]


def test_status_group_filters_and_summary_cards(
    records_dsn: str, seeded: dict[str, str], owner: CurrentUser
) -> None:
    """方案 P1：status_group 5 组口径 + 聚合卡（成功率/失败数/平均耗时）。"""
    from app.control_routes import list_generation_records, summarize_generation_records

    with psycopg.connect(records_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        failed_view = list_generation_records(
            conn, owner, record_type="ANALYSIS", status_group="failed", limit=50, offset=0
        )
        queued_view = list_generation_records(
            conn, owner, record_type="ANALYSIS", status_group="queued", limit=50, offset=0
        )
        summary = summarize_generation_records(
            conn, owner, record_type="ANALYSIS", status_group="failed"
        )
        full = summarize_generation_records(conn, owner, record_type="ANALYSIS")

    # 「失败」组在拆解分支 = FAILED（夹具无取消/归档失败行）。
    assert failed_view.total == 3
    # PENDING 属「排队中」组。
    assert queued_view.total == 1

    # 组筛选下聚合与列表同口径。
    assert summary.total == 3
    assert summary.succeeded_count == 0
    assert summary.failed_count == 3
    assert summary.success_rate_pct == 0
    # 失败视图没有成功样本，平均耗时不给数字而不是假装 0 秒。
    assert summary.avg_duration_seconds is None

    # 全量视图：1 成功 / 3 失败 / 5 总，成功率 20%。
    assert full.succeeded_count == 1
    assert full.failed_count == 3
    assert full.success_rate_pct == 20.0
    # 夹具成功任务带 completed_at（比 created_at 晚 60 秒），加权平均 = 60 秒。
    assert full.avg_duration_seconds == 60.0


def test_refund_uses_current_billing_round_not_prior_release(records_dsn, seeded, owner):
    from app.control_routes import list_generation_records

    with psycopg.connect(records_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        first = accept_operation(
            conn,
            user_id=seeded["user"],
            service="analysis",
            source_id=seeded["network"],
            units=1,
            billing_round=1,
        )
        finish_operation(conn, operation_id=first, units=0, succeeded=False)
        second = accept_operation(
            conn,
            user_id=seeded["user"],
            service="analysis",
            source_id=seeded["network"],
            units=1,
            billing_round=2,
        )
        page = list_generation_records(conn, owner, user_id=seeded["user"], limit=100, offset=0)
        item = next(item for item in page.items if item.record_id == seeded["network"])
        assert item.credits_refunded is False
        finish_operation(conn, operation_id=second, units=0, succeeded=False)
        page = list_generation_records(conn, owner, user_id=seeded["user"], limit=100, offset=0)
        item = next(item for item in page.items if item.record_id == seeded["network"])
        assert item.credits_refunded is True
        raw.rollback()


def test_company_keyword_and_project_keep_customer_id_scope(records_dsn, seeded, owner):
    from app.control_routes import list_generation_records, summarize_generation_records

    with psycopg.connect(records_dsn) as raw:
        raw.execute(
            "UPDATE users SET display_name='公司甲',username='same-prefix' WHERE id=%s",
            (seeded["user"],),
        )
        raw.execute(
            "INSERT INTO users(id,username,display_name,role) "
            "VALUES('g21-other','same-prefix-other','公司甲分公司','customer')"
        )
        raw.execute(
            "INSERT INTO projects(id,owner_user_id,name) "
            "VALUES('g21-other-project','g21-other','拆解观测项目')"
        )
        raw.execute(
            "INSERT INTO analysis_tasks(id,project_id,asset_id,created_by_user_id,"
            "duration_seconds,status) "
            "SELECT 'g21-other-task','g21-other-project',asset_id,'g21-other',"
            "duration_seconds,'FAILED' "
            "FROM analysis_tasks WHERE id=%s",
            (seeded["network"],),
        )
        conn = BusinessConnection.postgres(raw)
        hit = list_generation_records(
            conn,
            owner,
            username="公司甲",
            user_id=seeded["user"],
            project_name="拆解观测",
            limit=100,
            offset=0,
        )
        assert hit.total == 5 and all(row.user_id == seeded["user"] for row in hit.items)
        summary = summarize_generation_records(
            conn, owner, username="公司甲", user_id=seeded["user"], project_name="拆解观测"
        )
        assert summary.total == 5 and sum(row.count for row in summary.failure_reasons) == 3
        miss = list_generation_records(
            conn,
            owner,
            username="公司甲",
            user_id=seeded["user"],
            project_name="其它项目",
            limit=100,
            offset=0,
        )
        assert miss.total == 0
        assert (
            summarize_generation_records(
                conn, owner, username="公司甲", user_id=seeded["user"], project_name="其它项目"
            ).total
            == 0
        )
        raw.rollback()


def test_project_name_filter_matches_video_and_analysis_only(
    records_dsn: str, seeded: dict[str, str], owner: CurrentUser
) -> None:
    """方案 P1：项目名筛选——拆解按 project_id 命中，视频按 batch→projects 命中。"""
    from app.control_routes import list_generation_records

    with psycopg.connect(records_dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        hit = list_generation_records(
            conn, owner, record_type="ANALYSIS", project_name="拆解观测", limit=50, offset=0
        )
        miss = list_generation_records(
            conn, owner, record_type="ANALYSIS", project_name="不存在的项目", limit=50, offset=0
        )

    assert hit.total == 5
    assert {item.record_id for item in hit.items} >= {seeded["ok"], seeded["pending"]}
    assert miss.total == 0


def test_stable_user_id_isolates_same_prefix_list_summary_and_diagnostics(
    records_dsn, seeded, owner
):
    from app.control_routes import list_generation_records, summarize_generation_records

    with psycopg.connect(records_dsn) as raw:
        raw.execute("UPDATE users SET username='a' WHERE id=%s", (seeded["user"],))
        raw.execute("INSERT INTO users(id,username,display_name) VALUES('m14-ab','ab','同名展示')")
        raw.execute(
            "INSERT INTO analysis_tasks"
            "(id,project_id,asset_id,created_by_user_id,duration_seconds,status) "
            "SELECT 'm14-other-task',project_id,asset_id,'m14-ab',8,'FAILED' "
            "FROM analysis_tasks WHERE id=%s",
            (seeded["network"],),
        )
        conn = BusinessConnection.postgres(raw)
        fuzzy = list_generation_records(conn, owner, username="a", limit=100, offset=0)
        assert any(item.user_id == "m14-ab" for item in fuzzy.items)
        for offset in [0, 1]:
            page = list_generation_records(
                conn, owner, user_id=seeded["user"], limit=1, offset=offset
            )
            assert page.total == 5 and len(page.items) == 1
            assert page.items[0].user_id == seeded["user"]
        summary = summarize_generation_records(conn, owner, user_id=seeded["user"])
        assert sum(item.count for item in summary.counts) == 5
        assert sum(reason.count for reason in summary.failure_reasons) == 3
        diagnostics = list_generation_records(
            conn, owner, diagnostics=True, user_id=seeded["user"], limit=100, offset=0
        )
        assert diagnostics.total == 3
        assert all(item.user_id == seeded["user"] for item in diagnostics.items)
        none = list_generation_records(
            conn, owner, user_id="missing-stable-user", limit=100, offset=0
        )
        assert none.total == 0
