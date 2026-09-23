"""CW-010: independent creation (C2 独立创作) recovery baseline on real PostgreSQL.

Migrated off the ``tmp_path`` SQLite lane (route ``TestClient`` + ``get_database``
override) onto a dedicated TEST-PG database with an *independent multi-connection*
baseline. Two identity paths coexist on PG and this suite uses both deliberately.
The *fenced write* path (``BusinessDbDep`` →
``customer_fence.customer_session_snapshot``) makes a customer Bearer session the
sole identity and refuses the internal ``X-Dev-User-Id`` header with 401, so the
billing / RBAC / idempotency assertions drive the **service** layer directly inside
``pg_transaction`` rounds — the same shape CW-010a (``test_wallet_billing_service``)
uses — constructing the acting ``CurrentUser`` (employee / admin / auditor)
explicitly. The *unfenced read* path (``get_database`` + ``get_current_user``) DOES
authenticate the dev header on PG under the test runtime's ``development`` auth mode
(``internal_auth_required()`` is false and ``ALLOW_DEV_IDENTITY_HEADER=1``), so the
read routes are driven through ``TestClient`` with the dev header and zero override
(the route paragraph below). RBAC, idempotency, the mode/asset matrix, wallet
RESERVE/SETTLE/RELEASE and the PG worker (``run_pg_worker_once``) lifecycle are all
exercised against real PostgreSQL: never SQLite, never a mock-persisted store,
never a missing-PG skip (the CW-007 ``require_pg_or_explicit_skip`` hard gate fails
closed when the fixture is unreachable).

The independent-specific HTTP routes are restored here on the PG lane rather than
deleted: the *read* routes (``GET /api/independent/capabilities`` and the
independent-creation page's ``GET /api/studio/saved-prompts`` import source) ride
the unfenced ``get_database`` dependency and authenticate the dev header on PG, so
they are driven through ``TestClient`` with **zero** dependency override; the
*fenced write* route (``POST /api/independent/video-tasks``) is driven through a
``_PgBusinessDb`` double that mirrors the production ``BusinessDb.write()`` on a
real ``pg_transaction`` (catch ``AuditedSecurityDenial`` → ``persist_security_denial``
→ re-raise) so the 201 body, ``BatchResult`` serialization and wallet RESERVE all
commit to PostgreSQL — the database is never mocked, only the acting user the
Bearer-session fence would otherwise resolve. The *shared* post-creation routes
(``GET /api/generation-batches`` listing / detail / cancel) are the generation
domain's own contract and stay covered by ``test_generation.py``; this file owns
the independent-creation database baseline plus its two dedicated routes per CW-010.

Migration findings (SQLite → PostgreSQL):
- ``runtime_settings.h3_extended_modes_enabled`` carries no gate anymore: migration
  ``20260923T0000_open_h3_extended_modes`` dropped it and removed the gate from
  ``app.independent`` outright; ``20260923T1800`` re-adds the column purely as a
  rollout-compat shim (old images still SELECT it during the MIGRATE→ROLL window
  and after an image rollback) — no code path may read it again, which
  ``test_no_code_path_reads_the_retired_gate_column`` locks at source level.
  T2V / R2V / last_frame are now unconditionally open, so the cases below no
  longer have to flip a flag before submitting. ``extended_modes_enabled`` stays
  on the capabilities payload (the client type contract) but is reported ``True``
  unconditionally.
- ``operation_cost_rates`` FK-references ``users`` (ON DELETE SET NULL), so
  ``TRUNCATE users CASCADE`` clears the migration-seeded rate defaults that
  ``snapshot_generation_rates`` reads on the PG lane; the seed captures and
  restores them (ON CONFLICT DO NOTHING) so every created task freezes a real
  cost snapshot.
- ``reference_asset_ids`` over the *total* cap (12 files) is rejected by
  the ``IndependentVideoRequest`` model (``max_length``) at construction time, so
  the service-level assertion is a pydantic ``ValidationError`` (the route turned
  it into a 422 ``too_long`` body). The per-kind caps (image ≤ 8 / video ≤ 3 /
  audio ≤ 3) are enforced after the backend splits the mixed list by asset kind
  (``INDEPENDENT_REFERENCE_LIMIT_EXCEEDED``).
- The foreign-asset and auditor denials are PG ``AuditedSecurityDenial`` (an
  ``HTTPException`` subclass) because ``_raise_denial_with_audit`` defers the
  audit to the route layer on PostgreSQL; the status code (404 / 403) is asserted.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

# Set the audit HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-cw010-independent-tests-minimum-48-bytes-long-12",
)

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)
from pydantic import ValidationError

from app.auth import CurrentUser
from app.customer_fence import get_business_db
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.generation import (
    BatchResult,
    GenerationBatchListPage,
    acquire_generation_task_lease,
    cancel_generation_batch,
    get_generation_batch,
    list_generation_batches,
    refresh_batch_status,
)
from app.generation_worker import run_pg_worker_once
from app.independent import (
    IndependentVideoRequest,
    create_independent_batch,
    read_independent_capabilities,
)
from app.main import app
from app.permissions import AuditedSecurityDenial, persist_security_denial
from app.storage import FakeStorageAdapter

CW010_INDEPENDENT_DB_NAME = "cw010_independent_test"

_INDEPENDENT_TABLES = (
    "wallet_transactions, assets, versions, generation_tasks, "
    "generation_batches, user_queue_cursors, projects, wallets, "
    "runtime_settings, users"
)

EMPLOYEE_1 = CurrentUser(
    id="employee_1", username="employee_1", display_name="Employee One", role="employee"
)
EMPLOYEE_2 = CurrentUser(
    id="employee_2", username="employee_2", display_name="Employee Two", role="employee"
)
AUDITOR_1 = CurrentUser(
    id="auditor_1", username="auditor_1", display_name="Auditor One", role="auditor"
)


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def independent_dsn() -> Iterator[str]:
    """A dedicated migrated PG database for the independent-creation baseline.

    Created through the CW-007 kit helpers so ``assert_safe_test_database``
    guards the ``DROP ... WITH (FORCE)`` against the allowlisted name
    (registered in ``pg_test_kit.RECORDED_TEST_DATABASES``) and the kit resolves
    the admin DSN — no hand-rolled DSN concatenation lives in this file.
    """
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW010_INDEPENDENT_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW010_INDEPENDENT_DB_NAME)


def _executemany(pg: psycopg.Connection, sql: str, rows: list[tuple[object, ...]]) -> None:
    """psycopg3 exposes ``executemany`` on the cursor, not the connection."""
    with pg.cursor() as cursor:
        cursor.executemany(sql, rows)


def _seed_scene(dsn: str) -> None:
    """TRUNCATE and re-seed the independent-creation scene on real PostgreSQL."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        # operation_cost_rates FK-references users, so the CASCADE below clears
        # the migration-seeded defaults snapshot_generation_rates reads on PG.
        default_rates = pg.execute(
            "SELECT subject, kind, unit, resolution, unit_price_fen FROM operation_cost_rates"
        ).fetchall()
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_INDEPENDENT_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        # Explicit retail configuration for these positive-reservation scenarios.
        pg.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES "
            "('video_768p',true,1),('video_2k',true,1) ON CONFLICT(service) "
            "DO UPDATE SET enabled=true,unit_credits=1"
        )
        with pg.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO operation_cost_rates "
                "(subject, kind, unit, resolution, unit_price_fen) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
                default_rates,
            )
        pg.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        _executemany(
            pg,
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
                ("admin_1", "admin_1", "Admin One", "admin"),
                ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ],
        )
        _executemany(
            pg,
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, %s, 0)",
            [("employee_1", 1000), ("employee_2", 1000)],
        )
        _executemany(
            pg,
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            [("project_a", "employee_1", "A"), ("project_b", "employee_2", "B")],
        )
        # 独立创作素材：用户归属、无项目（materials 通道产物形态）+ 一个项目内视频资产。
        _executemany(
            pg,
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes,"
            " content_type, created_by_user_id"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    "frame-owned",
                    None,
                    "material_image",
                    "fake://generation-results/frame.png",
                    "frame-hash",
                    9,
                    "image/png",
                    "employee_1",
                ),
                (
                    "frame-other",
                    None,
                    "material_image",
                    "fake://generation-results/other.png",
                    "other-hash",
                    9,
                    "image/png",
                    "employee_2",
                ),
                (
                    "asset-video",
                    "project_a",
                    "reference_video",
                    "local://assets/ref.mp4",
                    "video-hash",
                    9,
                    "video/mp4",
                    "employee_1",
                ),
                # R2V 多模态参考：用户素材通道的视频/音频（material_video /
                # material_audio），与上方复刻源视频（reference_video）区分。
                (
                    "material-video-owned",
                    None,
                    "material_video",
                    "fake://generation-results/ref-video.mp4",
                    "material-video-hash",
                    9,
                    "video/mp4",
                    "employee_1",
                ),
                (
                    "material-audio-owned",
                    None,
                    "material_audio",
                    "fake://generation-results/ref-audio.mp3",
                    "material-audio-hash",
                    9,
                    "audio/mpeg",
                    "employee_1",
                ),
            ],
        )

        pg.execute(
            "UPDATE assets SET metadata_json=%s "
            "WHERE id IN ('material-video-owned', 'material-audio-owned')",
            (json.dumps({"duration_seconds": 4}),),
        )


@pytest.fixture()
def scene(independent_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the app PG pool at the independent database and seed one clean scene."""
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, independent_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    _seed_scene(independent_dsn)
    yield independent_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# Service helpers — each logical block runs in its own pg_transaction
# (a separate pooled connection), the CW-010 multi-connection baseline.
# ---------------------------------------------------------------------------


def _create(request: IndependentVideoRequest, actor: CurrentUser) -> BatchResult:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return create_independent_batch(conn, actor=actor, request=request)


def _list_batches(actor: CurrentUser) -> GenerationBatchListPage:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return list_generation_batches(conn, actor=actor)


def _get_batch(batch_id: str, actor: CurrentUser) -> BatchResult:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return get_generation_batch(conn, batch_id=batch_id, actor=actor)


def _cancel_batch(batch_id: str, actor: CurrentUser) -> BatchResult:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return cancel_generation_batch(conn, actor=actor, batch_id=batch_id)


def _wallet(user_id: str) -> tuple[int, int]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    assert row is not None
    return int(row["available_credits"]), int(row["reserved_credits"])


def _ledger_count(tx_type: str) -> int:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM wallet_transactions WHERE type = %s",
            (tx_type,),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def _run_worker(worker_id: str) -> int:
    storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
    return run_pg_worker_once(worker_id=worker_id, storage=storage)


# ---------------------------------------------------------------------------
# Route doubles. The independent WRITE route rides ``BusinessDbDep`` whose
# customer session fence refuses the internal ``X-Dev-User-Id`` header on the
# PostgreSQL lane (``customer_fence.customer_session_snapshot`` → 401
# SESSION_TOKEN_REQUIRED), so it cannot be driven by the dev header the READ
# routes use. ``_PgBusinessDb`` mirrors the production ``BusinessDb.write()``: it
# opens a *real* ``pg_transaction`` and re-raises the deferred denial audit
# exactly like ``fenced_pg_transaction`` (catch ``AuditedSecurityDenial`` →
# ``persist_security_denial`` → re-raise), injecting the acting ``CurrentUser``
# the fence would otherwise resolve from a Bearer session. The database is never
# mocked. The READ routes (``GET /api/independent/capabilities``,
# ``GET /api/studio/saved-prompts``) ride the unfenced ``get_database`` and
# authenticate the dev header on PG, so they need no override at all.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_dependency_overrides() -> Iterator[None]:
    """Guarantee a route double never leaks into the next test."""
    yield
    app.dependency_overrides.clear()


class _PgBusinessDb:
    """A ``BusinessDb`` double whose ``write()`` runs on a real PG transaction."""

    def __init__(self, current_actor: CurrentUser) -> None:
        self.current_actor = current_actor

    @contextmanager
    def write(
        self, *, isolation: object = None
    ) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        try:
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), self.current_actor
        except AuditedSecurityDenial as exc:
            persist_security_denial(exc)
            raise


def _override_business_db(actor: CurrentUser) -> _PgBusinessDb:
    """Register the fenced-write double for ``actor``; autouse fixture clears it."""
    holder = _PgBusinessDb(actor)

    def override() -> _PgBusinessDb:
        return holder

    app.dependency_overrides[get_business_db] = override
    return holder


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_capabilities_report_extended_modes_enabled_by_default(scene: str) -> None:
    """门禁移除后 capabilities 恒报开放。

    migration 20260923T0000_open_h3_extended_modes 删除了开关列，``app.independent``
    也不再查询任何 ``runtime_settings`` 开关；``extended_modes_enabled`` 保留在 payload
    上（前端类型契约）但永远是 True。
    """
    with pg_transaction() as raw:
        caps = read_independent_capabilities(BusinessConnection.postgres(raw))

    assert caps.i2v_enabled is True
    assert caps.t2v_enabled is True
    assert caps.r2v_enabled is True
    assert caps.last_frame_enabled is True
    assert caps.extended_modes_enabled is True
    assert caps.max_quantity >= 1


def test_no_code_path_reads_the_retired_gate_column() -> None:
    """回归锁：门禁列虽以部署兼容垫片形式留在库里，但任何代码都不得再读它。

    20260923T1800 重加 ``runtime_settings.h3_extended_modes_enabled`` 只是为了
    MIGRATE→ROLL 混合窗口与镜像回滚兼容（旧镜像仍 SELECT 该列）；新代码读它
    即意味着门禁回流。源码级断言比 information_schema 断言更能拦住
    「列回来并被重新接上 gating」——capabilities 的 True 可能只是硬编码，
    列回来了却没人读才是要守住的不变量。
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        path.relative_to(app_dir).as_posix()
        for path in app_dir.rglob("*.py")
        if "h3_extended_modes" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_capabilities_route_serves_http_contract_on_pg(scene: str) -> None:
    """Restore the ``GET /api/independent/capabilities`` HTTP contract on PG.

    origin/main drove this through ``client.get``; the migration kept only the
    service call. The route rides the unfenced ``get_database`` dependency, so
    the real handler + real PG pool serve it with **zero** override — proving the
    route (not just the service) serves the contract off the real database.

    门禁移除后 payload 恒报开放且保留 ``extended_modes_enabled`` 字段（前端契约）。
    """
    client = TestClient(app)
    response = client.get("/api/independent/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["i2v_enabled"] is True
    assert body["extended_modes_enabled"] is True
    assert body["t2v_enabled"] is True
    assert body["r2v_enabled"] is True
    assert body["last_frame_enabled"] is True
    assert isinstance(body["max_quantity"], int) and body["max_quantity"] >= 1


# ---------------------------------------------------------------------------
# Batch creation, idempotency and the mode/asset matrix
# ---------------------------------------------------------------------------


def test_i2v_batch_creation_reserves_seconds_and_marks_independent(scene: str) -> None:
    batch = _create(
        IndependentVideoRequest(
            mode="i2v",
            prompt_text="镜头缓缓推进，展示乡墅庭院的黄昏",
            first_frame_asset_id="frame-owned",
            output_duration_seconds=10,
            resolution="768P",
            ratio="16:9",
            quantity=2,
            idempotency_key="i2v-key-1",
        ),
        EMPLOYEE_1,
    )

    assert batch.project_id is None
    assert batch.creation_kind == "independent"
    assert batch.stale is False
    assert batch.progress.total_count == 2
    assert batch.display_name == "视频生成"
    listed = next(item for item in _list_batches(EMPLOYEE_1).items if item.id == batch.id)
    assert listed.display_name == batch.display_name
    with pg_transaction() as raw:
        raw.execute(
            "UPDATE generation_batches SET display_name=%s WHERE id=%s", ("我的乡墅成片", batch.id)
        )
    assert _get_batch(batch.id, EMPLOYEE_1).display_name == "我的乡墅成片"
    renamed = next(item for item in _list_batches(EMPLOYEE_1).items if item.id == batch.id)
    assert renamed.display_name == "我的乡墅成片"

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mode_rows = conn.execute(
            "SELECT generation_mode, billed_seconds FROM generation_tasks WHERE batch_id = %s",
            (batch.id,),
        ).fetchall()
    assert {str(row["generation_mode"]) for row in mode_rows} == {"I2V"}
    assert {int(row["billed_seconds"]) for row in mode_rows} == {10}
    assert _wallet("employee_1") == (980, 20)
    assert _ledger_count("RESERVE") == 2


def test_i2v_replay_returns_same_batch_and_conflicting_key_is_rejected(scene: str) -> None:
    request = IndependentVideoRequest(
        mode="i2v",
        prompt_text="黄昏庭院航拍",
        first_frame_asset_id="frame-owned",
        output_duration_seconds=8,
        quantity=1,
        idempotency_key="replay-key",
    )
    first = _create(request, EMPLOYEE_1)
    replay = _create(request, EMPLOYEE_1)
    assert replay.id == first.id
    # 增量验收①：同一幂等键重放新增收费任务=0 —— RESERVE 恰为 1，钱包差额未被二次扣减。
    assert _ledger_count("RESERVE") == 1
    assert _wallet("employee_1") == (992, 8)

    conflict = IndependentVideoRequest(
        mode="i2v",
        prompt_text="不一样的提示词",
        first_frame_asset_id="frame-owned",
        output_duration_seconds=8,
        quantity=1,
        idempotency_key="replay-key",
    )
    with pytest.raises(HTTPException) as exc:
        _create(conflict, EMPLOYEE_1)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "IDEMPOTENCY_CONFLICT"
    # 被拒的冲突请求同样不得新增收费任务（事务回滚干净）。
    assert _ledger_count("RESERVE") == 1
    assert _wallet("employee_1") == (992, 8)


def test_extended_modes_submit_without_any_flag(scene: str) -> None:
    """门禁移除后 T2V / R2V / 尾帧可直接提交，无需任何开关。

    取代原 ``test_extended_modes_are_gated_until_verified`` /
    ``test_extended_modes_open_after_verification`` 两例：原来的 gating 分支
    （409 ``EXTENDED_MODE_PENDING_VERIFICATION``）连同开关列一起被删除，
    本用例保留「三种扩展模式都能真正落库」这条回归。
    """
    t2v = _create(
        IndependentVideoRequest(
            mode="t2v",
            prompt_text="清晨山间别墅的延时摄影",
            ratio="16:9",
            output_duration_seconds=6,
            quantity=1,
            idempotency_key="t2v-open",
        ),
        EMPLOYEE_1,
    )
    r2v = _create(
        IndependentVideoRequest(
            mode="r2v",
            prompt_text="按照参考图生成别墅外观",
            reference_asset_ids=["frame-owned"],
            output_duration_seconds=6,
            quantity=1,
            idempotency_key="r2v-open",
        ),
        EMPLOYEE_1,
    )
    tail = _create(
        IndependentVideoRequest(
            mode="i2v",
            prompt_text="首尾帧过渡",
            first_frame_asset_id="frame-owned",
            last_frame_asset_id="frame-owned",
            output_duration_seconds=6,
            quantity=1,
            idempotency_key="tail-open",
        ),
        EMPLOYEE_1,
    )

    assert t2v.stale is False
    assert r2v.creation_kind == "independent"
    assert tail.progress.total_count == 1


def test_mode_asset_matrix_is_enforced(scene: str) -> None:

    with pytest.raises(HTTPException) as missing_first:
        _create(
            IndependentVideoRequest(
                mode="i2v",
                prompt_text="没有首帧",
                output_duration_seconds=8,
                quantity=1,
                idempotency_key="matrix-1",
            ),
            EMPLOYEE_1,
        )
    assert missing_first.value.status_code == 422
    assert missing_first.value.detail["code"] == "INDEPENDENT_FIRST_FRAME_REQUIRED"

    with pytest.raises(HTTPException) as t2v_with_frame:
        _create(
            IndependentVideoRequest(
                mode="t2v",
                prompt_text="文生却带首帧",
                first_frame_asset_id="frame-owned",
                output_duration_seconds=8,
                quantity=1,
                idempotency_key="matrix-2",
            ),
            EMPLOYEE_1,
        )
    assert t2v_with_frame.value.status_code == 422
    assert t2v_with_frame.value.detail["code"] == "INDEPENDENT_MODE_ASSET_CONFLICT"

    with pytest.raises(HTTPException) as r2v_without_ref:
        _create(
            IndependentVideoRequest(
                mode="r2v",
                prompt_text="无参考",
                output_duration_seconds=8,
                quantity=1,
                idempotency_key="matrix-3",
            ),
            EMPLOYEE_1,
        )
    assert r2v_without_ref.value.status_code == 422
    assert r2v_without_ref.value.detail["code"] == "INDEPENDENT_REFERENCE_REQUIRED"

    # 别人的素材：material_image 归属校验拒绝（PG 上是 AuditedSecurityDenial 404）。
    with pytest.raises(HTTPException) as foreign_asset:
        _create(
            IndependentVideoRequest(
                mode="i2v",
                prompt_text="别人的素材",
                first_frame_asset_id="frame-other",
                output_duration_seconds=8,
                quantity=1,
                idempotency_key="matrix-4",
            ),
            EMPLOYEE_1,
        )
    assert foreign_asset.value.status_code == 404

    with pytest.raises(HTTPException) as video_asset:
        _create(
            IndependentVideoRequest(
                mode="i2v",
                prompt_text="视频当首帧",
                first_frame_asset_id="asset-video",
                output_duration_seconds=8,
                quantity=1,
                idempotency_key="matrix-5",
            ),
            EMPLOYEE_1,
        )
    assert video_asset.value.status_code == 422
    assert video_asset.value.detail["code"] == "INDEPENDENT_ASSET_KIND_UNSUPPORTED"


def test_reference_images_reject_duplicates_and_more_than_capability_limit(scene: str) -> None:

    with pytest.raises(HTTPException) as duplicate:
        _create(
            IndependentVideoRequest(
                mode="r2v",
                prompt_text="严格校验参考图",
                reference_asset_ids=["frame-owned", "frame-owned"],
                output_duration_seconds=8,
                quantity=1,
                idempotency_key="reference-duplicate",
            ),
            EMPLOYEE_1,
        )
    assert duplicate.value.status_code == 422
    assert duplicate.value.detail["code"] == "INDEPENDENT_REFERENCE_DUPLICATE"

    # 超出总兜底上限（>12 项）由请求模型 max_length 拒绝（ValidationError
    # too_long）；每类上限（图≤8/视≤3/音≤3）在分流后由
    # INDEPENDENT_REFERENCE_LIMIT_EXCEEDED 拒绝，纯函数层已单测覆盖。
    with pytest.raises(ValidationError):
        IndependentVideoRequest(
            mode="r2v",
            prompt_text="超量参考素材",
            reference_asset_ids=[f"frame-{index}" for index in range(15)],
            output_duration_seconds=8,
            quantity=1,
            idempotency_key="reference-over-limit",
        )


def test_auditor_cannot_create_independent_tasks(scene: str) -> None:
    with pytest.raises(AuditedSecurityDenial) as exc:
        _create(
            IndependentVideoRequest(
                mode="i2v",
                prompt_text="审计员不可提交",
                first_frame_asset_id="frame-owned",
                output_duration_seconds=8,
                quantity=1,
                idempotency_key="auditor-key",
            ),
            AUDITOR_1,
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Worker lifecycle: settle on success, release on confirmed failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_status", ["SUBMITTING", "QUEUED", "RUNNING", "ARCHIVING"])
def test_batch_lists_started_tasks_as_running_not_cancellable_queue(
    scene: str, task_status: str
) -> None:
    batch = _create(
        IndependentVideoRequest(
            mode="i2v",
            prompt_text="首尾帧进度复测",
            first_frame_asset_id="frame-owned",
            output_duration_seconds=4,
            quantity=2,
            idempotency_key="started-progress",
        ),
        EMPLOYEE_1,
    )
    with pg_transaction() as conn:
        conn.execute(
            "UPDATE generation_tasks SET status=%s WHERE id=%s", (task_status, batch.tasks[0].id)
        )
    listed = next(item for item in _list_batches(EMPLOYEE_1).items if item.id == batch.id)
    assert listed.status == "RUNNING"
    assert _get_batch(batch.id, EMPLOYEE_1).status == "RUNNING"
    with pytest.raises(HTTPException):
        _cancel_batch(batch.id, EMPLOYEE_1)


def test_worker_settles_independent_task_and_releases_on_failure(scene: str) -> None:
    batch = _create(
        IndependentVideoRequest(
            mode="i2v",
            prompt_text="首帧推进镜头",
            first_frame_asset_id="frame-owned",
            output_duration_seconds=10,
            quantity=2,
            idempotency_key="worker-key",
        ),
        EMPLOYEE_1,
    )

    processed = _run_worker("independent-worker")

    assert processed == 2
    assert _wallet("employee_1") == (980, 0)  # SETTLE：reserved 清零
    assert _ledger_count("SETTLE") == 2

    page = _list_batches(EMPLOYEE_1)
    independent_items = [item for item in page.items if item.id == batch.id]
    assert len(independent_items) == 1
    assert independent_items[0].creation_kind == "independent"
    assert independent_items[0].progress.progress_percent == 100

    other = _list_batches(EMPLOYEE_2)
    assert all(item.id != batch.id for item in other.items)

    detail = _get_batch(batch.id, EMPLOYEE_1)
    assert detail.project_id is None

    with pytest.raises(HTTPException) as denied:
        _get_batch(batch.id, EMPLOYEE_2)
    assert denied.value.status_code == 404


def test_independent_batches_never_leak_provider_names(scene: str) -> None:
    batch = _create(
        IndependentVideoRequest(
            mode="i2v",
            prompt_text="红线检查",
            first_frame_asset_id="frame-owned",
            output_duration_seconds=6,
            quantity=1,
            idempotency_key="redline-key",
        ),
        EMPLOYEE_1,
    )

    body = batch.model_dump_json().lower()
    assert "metaso" not in body
    assert "tikhub" not in body


def test_saved_prompts_aggregate_across_projects(
    scene: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CW-026: the converged PG lane resolves reads through customer Bearer
    # sessions only, so this route-level test authenticates two seeded
    # sessions instead of the retired X-Dev-User-Id bypass.
    from app.customer_device_service import highest_device_domain_key, keyed_digest

    monkeypatch.setenv(
        "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY",
        "cw010-independent-session-key-0123456789abcdef",
    )
    _version, session_key = highest_device_domain_key()

    def _session_token(pg, *, user_id: str, code_id: str, device_id: str) -> str:
        token = f"cw010-session-{user_id}"
        pg.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) VALUES "
            "(%s, 'cw010-batch', 1500, 1000, 100, 1, '2099-01-01T00:00:00+00:00', "
            "'OPEN', 'employee_1') ON CONFLICT (id) DO NOTHING",
            (f"cw010-batch-{user_id}",),
        )
        pg.execute(
            "INSERT INTO activation_codes (id, batch_id, code_digest, "
            "digest_key_version, masked_code, status, issued_at, activated_at, "
            "bound_user_id) VALUES "
            "(%s, %s, 'cw010-digest-' || %s, 1, 'cw010-****', 'ACTIVE', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (code_id, f"cw010-batch-{user_id}", user_id, user_id),
        )
        pg.execute(
            "INSERT INTO customer_devices (id, activation_code_id, user_id, slot_no, "
            "display_name, platform, fingerprint_hmac, fingerprint_key_version, "
            "token_digest, token_key_version, status, bound_at) VALUES "
            "(%s, %s, %s, 1, 'CW010 device', 'windows', %s, 1, %s, 1, "
            "'BOUND', '2026-08-01T00:00:00+00:00') ON CONFLICT (id) DO NOTHING",
            (
                device_id,
                code_id,
                user_id,
                keyed_digest(session_key, f"fp-{device_id}"),
                keyed_digest(session_key, f"device-token-{device_id}"),
            ),
        )
        pg.execute(
            "INSERT INTO customer_session_state (user_id, activation_code_id, device_id, "
            "session_id, token_digest, session_epoch, lease_until) VALUES "
            "(%s, %s, %s, %s, %s, 1, '2099-06-01T00:00:00+00:00') ON CONFLICT (device_id) "
            "DO NOTHING",
            (
                user_id,
                code_id,
                device_id,
                f"cw010-session-{user_id}",
                keyed_digest(session_key, token),
            ),
        )
        return token

    with psycopg.connect(scene, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('u3', 'u3', 'U3', 'employee') ON CONFLICT (id) DO NOTHING"
        )
        pg.execute(
            "INSERT INTO projects (id, owner_user_id, name) "
            "VALUES ('project_c', 'u3', 'C') ON CONFLICT (id) DO NOTHING"
        )
        pg.execute(
            "DELETE FROM customer_session_state WHERE user_id IN ('employee_1', 'employee_2')"
        )
        _executemany(
            pg,
            "INSERT INTO versions ("
            " id, project_id, kind, version_number, payload_json,"
            " created_by_user_id, scope, source, author_user_id"
            ") VALUES (%s, %s, 'saved_prompt', 1, %s, %s, 'user', 'reverse_prompt_revision', %s)",
            [
                (
                    "sp-1",
                    "project_a",
                    json.dumps({"name": "庭院黄昏", "prompt_text": "A 的提示词"}),
                    "employee_1",
                    "employee_1",
                ),
                (
                    "sp-2",
                    "project_b",
                    json.dumps({"name": "别人的", "prompt_text": "B 的提示词"}),
                    "employee_2",
                    "employee_2",
                ),
                (
                    "sp-3",
                    "project_c",
                    json.dumps({"name": "空文本", "prompt_text": ""}),
                    "u3",
                    "u3",
                ),
            ],
        )
        # 坏 payload_json 行（version_number=2 避开 (project,kind,version) 唯一键）：
        # 路由必须静默跳过（json.JSONDecodeError）而非 500。
        pg.execute(
            "INSERT INTO versions ("
            " id, project_id, kind, version_number, payload_json,"
            " created_by_user_id, scope, source, author_user_id"
            ") VALUES ('sp-bad', 'project_a', 'saved_prompt', 2, '{not-json',"
            " 'employee_1', 'user', 'reverse_prompt_revision', 'employee_1')"
        )

        token_1 = _session_token(
            pg, user_id="employee_1", code_id="cw010-code-e1", device_id="cw010-dev-e1"
        )
        token_2 = _session_token(
            pg, user_id="employee_2", code_id="cw010-code-e2", device_id="cw010-dev-e2"
        )

    # 独立创作页「导入提示词」数据源：跨项目聚合、仅作者本人、按时间倒序。
    # 恢复为 origin/main 驱动的真实路由（GET /api/studio/saved-prompts）：它走
    # 无栅栏的 get_database + get_current_user（CW-026 后 PG 上仅认客户会话
    # Bearer），零 override，因此断言的是生产 handler 本身（owner 过滤 /
    # limit 钳制 / 坏 JSON 跳过 / SavedPromptListPage 序列化），而非手抄一份
    # 它的 SQL 自证。
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token_1}"}
    response = client.get("/api/studio/saved-prompts", headers=headers)
    assert response.status_code == 200
    items = response.json()["items"]
    # 仅作者本人：排除 employee_2 的 sp-2、u3 的 sp-3；坏 JSON 行 sp-bad 被跳过。
    assert [item["id"] for item in items] == ["sp-1"]
    assert items[0]["name"] == "庭院黄昏"
    assert items[0]["prompt_text"] == "A 的提示词"
    assert items[0]["project_id"] == "project_a"

    # limit 钳制：<1 或 >100 都回落 50，仍只返回作者本人可见行。
    clamped_low = client.get("/api/studio/saved-prompts?limit=0", headers=headers).json()["items"]
    clamped_high = client.get("/api/studio/saved-prompts?limit=999", headers=headers).json()[
        "items"
    ]
    assert [item["id"] for item in clamped_low] == ["sp-1"]
    assert [item["id"] for item in clamped_high] == ["sp-1"]

    # 越权隔离：employee_2 只看到自己的 sp-2（owner 过滤由路由 actor.id 驱动）。
    other = client.get(
        "/api/studio/saved-prompts", headers={"Authorization": f"Bearer {token_2}"}
    ).json()["items"]
    assert [item["id"] for item in other] == ["sp-2"]


def test_cancel_independent_batch_releases_reserved_seconds(scene: str) -> None:
    batch = _create(
        IndependentVideoRequest(
            mode="i2v",
            prompt_text="排队中取消",
            first_frame_asset_id="frame-owned",
            output_duration_seconds=10,
            quantity=1,
            idempotency_key="cancel-key",
        ),
        EMPLOYEE_1,
    )
    assert _wallet("employee_1") == (990, 10)

    cancelled = _cancel_batch(batch.id, EMPLOYEE_1)

    assert cancelled.status == "CANCELLED"
    assert _wallet("employee_1") == (1000, 0)  # RELEASE：全额退还
    assert _ledger_count("RELEASE") == 1


@pytest.mark.parametrize("refresh_claimed_batch", [False, True])
def test_cancel_claim_race_rejects_without_partial_cancellation_or_refund(
    scene: str, refresh_claimed_batch: bool
) -> None:
    """A worker claims after cancellation reads PENDING but before its writes."""
    batch = _create(
        IndependentVideoRequest(
            mode="i2v",
            prompt_text="取消与领取竞争",
            first_frame_asset_id="frame-owned",
            output_duration_seconds=6,
            quantity=2,
            idempotency_key="cancel-claim-race",
        ),
        EMPLOYEE_1,
    )
    read_completed = Barrier(2)

    def claim_after_cancel_read() -> dict[str, object] | None:
        read_completed.wait(timeout=10)
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            lease = acquire_generation_task_lease(conn, worker_id="cancel-race-worker")
            if refresh_claimed_batch:
                refresh_batch_status(conn, batch_id=batch.id)
            return lease

    with ThreadPoolExecutor(max_workers=1) as executor:
        claim = executor.submit(claim_after_cancel_read)

        def before_cancel_write(sql: str) -> None:
            if "UPDATE generation_batches" in sql and "status = 'CANCELLED'" in sql:
                read_completed.wait(timeout=10)
                assert claim.result(timeout=10) is not None

        with pytest.raises(HTTPException) as rejected:
            with pg_transaction() as raw:
                conn = BusinessConnection.postgres(raw)
                conn.set_trace_callback(before_cancel_write)
                cancel_generation_batch(conn, actor=EMPLOYEE_1, batch_id=batch.id)

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == (
        "BATCH_NOT_CANCELLABLE" if refresh_claimed_batch else "BATCH_ALREADY_ACTIVE"
    )
    assert _wallet("employee_1") == (988, 12)
    assert _ledger_count("RELEASE") == 0
    with psycopg.connect(scene) as pg:
        assert pg.execute(
            "SELECT status FROM generation_batches WHERE id=%s", (batch.id,)
        ).fetchone() == ("RUNNING" if refresh_claimed_batch else "QUEUED",)
        assert pg.execute(
            "SELECT status FROM generation_tasks WHERE batch_id=%s ORDER BY status", (batch.id,)
        ).fetchall() == [("PENDING",), ("SUBMITTING",)]
        assert pg.execute("SELECT COUNT(*) FROM external_call_logs").fetchone() == (0,)
        assert pg.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='generation_batch.cancel'"
        ).fetchone() == (0,)


def test_failed_independent_task_releases_credits(
    scene: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_FAKE_H3_OUTCOME", "provider_failed")
    batch = _create(
        IndependentVideoRequest(
            mode="i2v",
            prompt_text="供应商失败的回款",
            first_frame_asset_id="frame-owned",
            output_duration_seconds=6,
            quantity=1,
            idempotency_key="fail-key",
        ),
        EMPLOYEE_1,
    )

    _run_worker("fail-worker")

    assert _wallet("employee_1") == (1000, 0)  # RELEASE：失败不扣费
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        status_row = conn.execute(
            "SELECT status FROM generation_tasks WHERE batch_id = %s",
            (batch.id,),
        ).fetchone()
    assert status_row is not None
    assert str(status_row["status"]) == "FAILED"


def test_t2v_and_r2v_tasks_run_through_worker_with_protocol_payload(scene: str) -> None:
    _create(
        IndependentVideoRequest(
            mode="t2v",
            prompt_text="清晨山间别墅的延时摄影",
            ratio="16:9",
            output_duration_seconds=6,
            quantity=1,
            idempotency_key="t2v-worker",
        ),
        EMPLOYEE_1,
    )
    _create(
        IndependentVideoRequest(
            mode="r2v",
            prompt_text="按照参考图生成别墅外观",
            reference_asset_ids=["frame-owned"],
            output_duration_seconds=6,
            quantity=1,
            idempotency_key="r2v-worker",
        ),
        EMPLOYEE_1,
    )

    processed = _run_worker("mode-worker")

    assert processed == 2
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            "SELECT generation_mode, provider_request_json FROM generation_tasks"
        ).fetchall()
    by_mode = {str(row["generation_mode"]): row for row in rows}
    t2v_request = json.loads(str(by_mode["T2V"]["provider_request_json"]))
    assert [item["type"] for item in t2v_request["content"]] == ["text"]

    r2v_request = json.loads(str(by_mode["R2V"]["provider_request_json"]))
    roles = [item.get("role") for item in r2v_request["content"][1:]]
    assert roles == ["reference_image"]
    assert r2v_request["content"][1]["name"] == "ref-1"
    assert "first_frame" not in roles and "last_frame" not in roles


@pytest.mark.parametrize("library_assets", [False, True])
def test_r2v_reference_video_and_audio_flow_through_worker_payload(
    scene: str, library_assets: bool
) -> None:
    """R2V 参考视频/音频端到端：请求 → prompt_snapshot → lease → provider_request。"""
    if library_assets:
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            conn.execute(
                "UPDATE assets SET kind = 'character_approved_image', project_id = 'project_a' "
                "WHERE id = 'frame-owned'"
            )
            conn.execute(
                "UPDATE assets SET kind = 'oral_audio', project_id = 'project_a' "
                "WHERE id = 'material-audio-owned'"
            )
    _create(
        IndependentVideoRequest(
            mode="r2v",
            prompt_text="视频@1的人物用图片@2替换，音色参考@3，保留user@1.example。",
            reference_asset_ids=["material-video-owned", "frame-owned", "material-audio-owned"],
            output_duration_seconds=6,
            quantity=1,
            idempotency_key="r2v-media-worker",
        ),
        EMPLOYEE_1,
    )

    processed = _run_worker("media-worker")

    assert processed == 1
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute(
            "SELECT prompt_snapshot_json, provider_request_json FROM generation_tasks"
        ).fetchone()
    assert row is not None
    snapshot = json.loads(str(row["prompt_snapshot_json"]))
    assert [video["uri"] for video in snapshot["reference_videos"]] == [
        "fake://generation-results/ref-video.mp4"
    ]
    assert [audio["uri"] for audio in snapshot["reference_audios"]] == [
        "fake://generation-results/ref-audio.mp3"
    ]
    request_payload = json.loads(str(row["provider_request_json"]))
    assert request_payload["content"][0]["text"] == (
        "视频<Video 1>的人物用图片<Picture 1>替换，音色参考<Audio 1>，保留user@1.example。"
    )
    assert snapshot["reference_labels"] == {"1": "<Video 1>", "2": "<Picture 1>", "3": "<Audio 1>"}
    roles = [item.get("role") for item in request_payload["content"][1:]]
    assert roles == ["reference_image", "reference_video", "reference_audio"]
    assert request_payload["content"][1]["image_url"]["url"]
    assert request_payload["content"][2]["video_url"]["url"]
    assert request_payload["content"][3]["audio_url"]["url"]


@pytest.mark.parametrize(
    ("unbound", "code"),
    [("@10", "REFERENCE_ALIAS_UNRESOLVED"), ("<Video 2>", "REFERENCE_NOT_BOUND")],
)
def test_r2v_rejects_unbound_prompt_references_before_reserve(
    scene: str, unbound: str, code: str
) -> None:
    balance = _wallet(EMPLOYEE_1.id)
    with pytest.raises(HTTPException) as exc:
        _create(
            IndependentVideoRequest(
                mode="r2v",
                prompt_text=f"人物动作参考 {unbound}",
                reference_asset_ids=["material-video-owned", "frame-owned"],
                output_duration_seconds=6,
                quantity=1,
                idempotency_key="unbound-reference",
            ),
            EMPLOYEE_1,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == code
    assert _wallet(EMPLOYEE_1.id) == balance
    assert _ledger_count("RESERVE") == 0


def test_r2v_rejects_replica_source_video_as_reference(scene: str) -> None:
    """素材类别门禁：复刻源视频（kind=reference_video）不得充当 R2V 参考素材。

    统一混合列表按 kind 分流，仅素材通道类别（material_image / material_video /
    material_audio 等）可作参考；被拆解的复刻源视频（reference_video）不在并集内。
    """
    with pytest.raises(HTTPException) as exc:
        _create(
            IndependentVideoRequest(
                mode="r2v",
                prompt_text="用复刻源视频冒充参考素材",
                reference_asset_ids=["asset-video"],
                output_duration_seconds=6,
                quantity=1,
                idempotency_key="r2v-bad-reference-kind",
            ),
            EMPLOYEE_1,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_ASSET_KIND_UNSUPPORTED"


def test_video_task_route_creates_batch_through_fenced_write_on_pg(scene: str) -> None:
    """Restore the ``POST /api/independent/video-tasks`` fenced-write HTTP contract.

    origin/main drove this through ``client.post``; the migration kept only the
    service call. The real ``get_business_db`` fence refuses the dev header on PG
    (401 SESSION_TOKEN_REQUIRED), so the ``_PgBusinessDb`` double mirrors the
    production ``BusinessDb.write()`` on a *real* ``pg_transaction``: the 201
    status, the ``BatchResult`` body and the wallet RESERVE all commit to
    PostgreSQL — the database is never mocked, only the acting user the
    Bearer-session fence would otherwise resolve.
    """
    _override_business_db(EMPLOYEE_1)
    client = TestClient(app)
    payload = {
        "mode": "i2v",
        "prompt_text": "路由建批：黄昏庭院航拍",
        "first_frame_asset_id": "frame-owned",
        "output_duration_seconds": 10,
        "quantity": 1,
        "idempotency_key": "route-i2v-1",
    }
    response = client.post("/api/independent/video-tasks", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["creation_kind"] == "independent"
    assert body["project_id"] is None
    assert body["progress"]["total_count"] == 1

    # fenced 写确实提交到真 PG（非 mock 持久化）：增量验收②成功建批预留一次。
    assert _wallet("employee_1") == (990, 10)
    assert _ledger_count("RESERVE") == 1

    # 同一幂等键经同一路由重放：新增收费任务=0，返回同一批次。
    replay = client.post("/api/independent/video-tasks", json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]
    assert _ledger_count("RESERVE") == 1
    assert _wallet("employee_1") == (990, 10)


@pytest.mark.parametrize("duration", [None, 1.9, 15.01, float("nan")])
def test_r2v_rejects_invalid_reference_duration_before_reserve(
    scene: str, duration: float | None
) -> None:
    with pg_transaction() as raw:
        raw.execute(
            "UPDATE assets SET metadata_json=%s WHERE id='material-video-owned'",
            (json.dumps({"duration_seconds": duration}),),
        )
    with pytest.raises(HTTPException) as exc:
        _create(
            IndependentVideoRequest(
                mode="r2v",
                prompt_text="视频参考",
                reference_asset_ids=["material-video-owned"],
                output_duration_seconds=4,
                quantity=1,
                idempotency_key="invalid-duration",
            ),
            EMPLOYEE_1,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INDEPENDENT_REFERENCE_DURATION_INVALID"
    assert _ledger_count("RESERVE") == 0


def test_r2v_rejects_total_reference_duration_before_reserve(scene: str) -> None:
    with pg_transaction() as raw:
        raw.execute(
            "UPDATE assets SET metadata_json=%s WHERE id='material-video-owned'",
            (json.dumps({"duration_seconds": 12}),),
        )
        raw.execute(
            "UPDATE assets SET kind='material_video', content_type='video/mp4', "
            "metadata_json=%s WHERE id='material-audio-owned'",
            (json.dumps({"duration_seconds": 4}),),
        )
    with pytest.raises(HTTPException) as exc:
        _create(
            IndependentVideoRequest(
                mode="r2v",
                prompt_text="视频参考",
                reference_asset_ids=["material-video-owned", "material-audio-owned"],
                output_duration_seconds=4,
                quantity=1,
                idempotency_key="total-duration",
            ),
            EMPLOYEE_1,
        )
    assert exc.value.detail["code"] == "INDEPENDENT_REFERENCE_DURATION_LIMIT_EXCEEDED"
    assert _ledger_count("RESERVE") == 0


def test_r2v_allows_fifteen_seconds_per_media_type(scene: str) -> None:
    with pg_transaction() as raw:
        raw.execute(
            "UPDATE assets SET metadata_json=%s WHERE id IN "
            "('material-video-owned', 'material-audio-owned')",
            (json.dumps({"duration_seconds": 15}),),
        )
    result = _create(
        IndependentVideoRequest(
            mode="r2v",
            prompt_text="分别引用视频和声音",
            reference_asset_ids=["material-video-owned", "material-audio-owned"],
            output_duration_seconds=4,
            quantity=1,
            idempotency_key="separate-duration-limits",
        ),
        EMPLOYEE_1,
    )
    assert result.quantity == 1
    assert _ledger_count("RESERVE") == 1


def test_r2v_rejects_audio_total_before_reserve(scene: str) -> None:
    with pg_transaction() as raw:
        raw.execute(
            "UPDATE assets SET kind='material_audio', content_type='audio/mpeg', "
            "metadata_json=%s WHERE id IN ('material-video-owned', 'material-audio-owned')",
            (json.dumps({"duration_seconds": 8}),),
        )
    with pytest.raises(HTTPException) as exc:
        _create(
            IndependentVideoRequest(
                mode="r2v",
                prompt_text="声音参考",
                reference_asset_ids=["material-video-owned", "material-audio-owned"],
                output_duration_seconds=4,
                quantity=1,
                idempotency_key="audio-duration-limit",
            ),
            EMPLOYEE_1,
        )
    assert exc.value.detail["code"] == "INDEPENDENT_REFERENCE_DURATION_LIMIT_EXCEEDED"
    assert _ledger_count("RESERVE") == 0
