"""CW-059 — task/worker-domain PostgreSQL constraint & concurrency matrix (TEST-PG).

V3 收敛清单 CW-059 remaining work: ``补齐账务与全部任务测试的真实 PG 覆盖 …
补 … 与实际锁约束（FOR UPDATE SKIP LOCKED / advisory lock / 约束 / 不确定提交
恢复）；旧测试 100% 映射``.  Segment 1 (``test_cw059_billing_pg_matrix.py``)
closed the 账务/支付/钱包 half; this module closes the **任务/生成/Worker** half
on a dedicated real-PostgreSQL database (``cw059_task_test``, port-isolated at
5437), migrated to alembic head so every task-table constraint is present.

What this file deliberately does NOT re-do (DoD「剔除重复开发」), with the suite
that already proves it on real PostgreSQL:

* per-class **worker claim / lease / expiry / recovery / SUBMISSION_UNCERTAIN /
  mixed-queue / two-device** flow, driven through the production PG functions
  (``run_pg_worker_once`` / ``acquire_*_task`` / ``fail_*_task``) → CW-030
  ``test_cw030_worker_pg_matrix.py`` (all task classes, sequential double-claim).
* two-phase claim durability, expired-lease takeover with a worker-owned audit
  row, **concurrent expiry sweepers**, exactly-once reconciliation billing,
  refusal to guess an unknown provider task id (``SUBMISSION_REQUIRES_MANUAL_
  CONFIRMATION`` = 未知收费), dangling-RESERVE detection, the worker loop drain,
  and the **true multi-connection FIFO double-claim race closed by FOR UPDATE
  SKIP LOCKED** → ``test_worker_crash_recovery.py`` (T26,
  ``test_fifo_double_claim_race_skips_locked_head``).
* 4-thread drain load and the EXPLAIN plan/index assertions
  (``uq_generation_batches_user_project_key`` index scan) →
  ``test_queue_load_10k.py``.
* task-table **existence** after ``upgrade head`` and constraint-**name**
  existence (``uq_generation_batches_user_project_key``,
  ``generation_tasks_batch_id_fkey``, ``ck_character_sheet_tasks_operation``
  definition) + partial-index WHERE clauses → ``test_postgres_migrations.py``.
* **advisory lock** on PG (DoD「advisory lock」) → ``test_sqlite_to_postgres.py``
  (``test_real_pg_migration_advisory_lock_blocks_concurrent_cutover``),
  ``test_customer_ha_smoke.py``, ``test_ops_alerts.py``, ``test_cw057_cli_pg_entry.py``.

The genuine gap this matrix fills is the **raw database constraint enforcement**
on the task tables that the flow suites never assert (they seed *valid* rows and
drive behaviour; ``test_postgres_migrations.py`` asserts only that the constraint
*names* exist). Proven here against real PostgreSQL (``CheckViolation`` 23514 /
``UniqueViolation`` 23505 / ``ForeignKeyViolation`` 23503):

* the per-class **status enum CHECK boundary** — including the DB-level proof of
  the CW-002 *registered limitation* that ``character_generation_tasks`` and
  ``source_frame_tasks`` have **no** ``SUBMISSION_UNCERTAIN`` status (CW-030
  registers it behaviourally via attempt-exhaustion; the CHECK that makes it a
  hard schema invariant is asserted nowhere else), while ``first_frame_tasks``
  *does* carry it;
* ``operation_cost_records`` — the ``UNKNOWN`` cost status (the DoD「未知收费」
  schema anchor) is legal while an arbitrary status/unit is refused, and the
  non-negative CHECKs + ``uq_operation_cost_source_subject`` freeze exactly one
  actual-cost row per (source_type, source_id, subject);
* ``generation_batches`` idempotency UNIQUE, ``generation_tasks`` orphan FK +
  ON DELETE CASCADE, ``script_rewrite_tasks`` coupled ip-profile shape CHECK, and
  ``generation_task_operations`` idempotency UNIQUE;
* and, for the two globally-unique task keys, the guarantee under **real
  multi-connection concurrency** (two independent sessions race, exactly one
  commits) — the raw-DB foundation behind the legacy SQLite HTTP-level
  ``test_generation.py::test_task_paid_regeneration_concurrency_creates_only_one_replacement``.

Each test maps 1:1 to a legacy SQLite/schema assertion it replaces or to a DoD
constraint category; the mapping is recorded in ``docs/evidence/CW059-EVIDENCE.md`` §3.2.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

CW059_TASK_DB_NAME = "cw059_task_test"

# Leaf-first DELETE order (mirrors CW-030's proven _MATRIX_CLEANUP_ORDER): the
# head schema installs a TRUNCATE guard on append-only audit tables, so the scene
# reset walks explicit reverse-dependency DELETEs instead of TRUNCATE … CASCADE.
_CLEANUP_ORDER = (
    "wallet_transactions",
    "generation_task_operations",
    "external_call_logs",
    "operation_cost_records",
    "generation_tasks",
    "generation_batches",
    "script_rewrite_tasks",
    "script_from_audio_tasks",
    "source_frame_tasks",
    "first_frame_tasks",
    "character_sheet_tasks",
    "character_generation_tasks",
    "character_assets",
    "character_versions",
    "character_personas",
    "person_identities",
    "user_queue_cursors",
    "audit_logs",
    "assets",
    "versions",
    "projects",
    "internal_access_tokens",
    "recharge_orders",
    "wallets",
    "users",
    "runtime_settings",
)

_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_SHA_A = "a" * 64


# ---------------------------------------------------------------------------
# Fixtures — CW-007 kit helpers only (no hand-rolled DSN concatenation).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def task_dsn() -> Iterator[str]:
    """A dedicated migrated PG database for the task constraint matrix.

    ``require_pg_or_explicit_skip`` hard-gates (never a silent skip counting as
    evidence); ``create/drop_test_database`` are allowlist-guarded so cleanup can
    only ever touch ``cw059_task_test``.
    """
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW059_TASK_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW059_TASK_DB_NAME)


@pytest.fixture()
def seeded(task_dsn: str) -> Iterator[str]:
    """DELETE + re-seed the single-user task scene; yield its DSN."""
    _reset_and_seed(task_dsn)
    yield task_dsn


# ---------------------------------------------------------------------------
# Seed helpers — valid FK chain so a RED is always a genuine constraint hit.
# ---------------------------------------------------------------------------


def _reset_and_seed(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for table in _CLEANUP_ORDER:
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "INSERT INTO runtime_settings ("
            " id, max_generation_count_per_batch, max_concurrent_h3_tasks,"
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen,"
            " fair_queue_enabled"
            ") VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('user_1', 'user_1', 'User One', 'employee')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('user_1', 1000, 0)"
        )
        conn.execute(
            "INSERT INTO projects (id, name, owner_user_id) "
            "VALUES ('proj_1', 'CW-059 Tasks', 'user_1')"
        )
        conn.execute(
            "INSERT INTO versions (id, project_id, kind, version_number, payload_json) "
            "VALUES ('pv_1', 'proj_1', 'video', 1, '{}')"
        )
        conn.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes, content_type,"
            " created_by_user_id"
            ") VALUES ('asset_1', 'proj_1', 'material_image',"
            " 'fake://src/asset_1.png', %s, 9, 'image/png', 'user_1')",
            (_SHA_C,),
        )
        conn.execute(
            "INSERT INTO person_identities (id, owner_user_id, display_name,"
            " authorization_status) VALUES ('identity_1', 'user_1', '张工', 'AUTHORIZED')"
        )
        conn.execute(
            "INSERT INTO character_personas (id, identity_id, name) "
            "VALUES ('persona_1', 'identity_1', '张工')"
        )
        conn.execute(
            "INSERT INTO character_versions ("
            " id, persona_id, version_number, status, source_asset_id, source_sha256,"
            " persona_snapshot_json, provider, model, generation_params_json,"
            " template_version, template_hash, required_view_types_json, created_by"
            ") VALUES ('cv_1', 'persona_1', 1, 'PUBLISHED', 'asset_1', %s, '{}',"
            " 'fake-image', 'fake-image-model', '{}', 'v1', %s, '[]', 'user_1')",
            (_SHA_D, _SHA_E),
        )
        conn.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash,"
            " request_snapshot_json, status, creation_kind"
            ") VALUES ('batch_1', 'proj_1', 'user_1', 'batch-key-1', 'batch-hash-1',"
            " '{}', 'QUEUED', 'replica')"
        )
        conn.execute(
            "INSERT INTO generation_tasks ("
            " id, batch_id, generation_mode, provider, model, status, prompt_version_id"
            ") VALUES ('task_1', 'batch_1', 'I2V', 'fake_h3', 'h3', 'PENDING', 'pv_1')"
        )


def _insert_batch(
    conn: psycopg.Connection,
    *,
    batch_id: str,
    idempotency_key: str,
    user_id: str = "user_1",
    project_id: str | None = "proj_1",
) -> None:
    conn.execute(
        "INSERT INTO generation_batches ("
        " id, project_id, created_by_user_id, idempotency_key, request_hash,"
        " request_snapshot_json, status, creation_kind"
        ") VALUES (%s, %s, %s, %s, %s, '{}', 'QUEUED', 'replica')",
        (batch_id, project_id, user_id, idempotency_key, f"rh-{batch_id}"),
    )


def _insert_cost(
    conn: psycopg.Connection,
    *,
    cost_id: str,
    source_id: str,
    subject: str = "output_seconds",
    source_type: str = "generation_task",
    unit: str = "second",
    unit_price_fen: int = 100,
    status: str | None = None,
    cost_fen: object = None,
) -> None:
    # operation_cost_records' FKs (user_id, generation_task_id) are ON DELETE SET
    # NULL and left NULL here: the row is a pure CHECK/UNIQUE probe.
    if status is None and cost_fen is None:
        conn.execute(
            "INSERT INTO operation_cost_records ("
            " id, source_type, source_id, subject, unit, unit_price_fen"
            ") VALUES (%s, %s, %s, %s, %s, %s)",
            (cost_id, source_type, source_id, subject, unit, unit_price_fen),
        )
    elif cost_fen is not None:
        conn.execute(
            "INSERT INTO operation_cost_records ("
            " id, source_type, source_id, subject, unit, unit_price_fen, cost_fen"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (cost_id, source_type, source_id, subject, unit, unit_price_fen, cost_fen),
        )
    else:
        conn.execute(
            "INSERT INTO operation_cost_records ("
            " id, source_type, source_id, subject, unit, unit_price_fen, status"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (cost_id, source_type, source_id, subject, unit, unit_price_fen, status),
        )


def _count(dsn: str, sql: str, params: tuple[object, ...] = ()) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Group A — raw DB constraint enforcement on the task tables (23514/23505/23503).
# ---------------------------------------------------------------------------


def test_pg_generation_tasks_reject_negative_attempt(seeded: str) -> None:
    """``generation_tasks_attempt_check`` (attempt >= 0) on real PG.

    The task-domain mirror of the Segment 1 wallet non-negative CHECK: the retry
    counter can never go negative, enforced by PostgreSQL rather than by app code.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model, attempt) "
                "VALUES ('task_bad_attempt', 'batch_1', 'fake_h3', 'h3', -1)"
            )
        assert exc.value.diag.table_name == "generation_tasks"
        assert exc.value.diag.constraint_name == "generation_tasks_attempt_check"


def test_pg_generation_batches_reject_duplicate_idempotency_key(seeded: str) -> None:
    """``uq_generation_batches_user_project_key`` enforcement (not just existence).

    ``test_postgres_migrations.py`` asserts this UNIQUE constraint's *name* is
    present after ``upgrade head``; this proves it *rejects* a second batch with
    the same (created_by_user_id, project_id, idempotency_key) — the schema-level
    duplicate-submission guard behind the creation route's idempotency.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation) as exc:
            _insert_batch(conn, batch_id="batch_dup", idempotency_key="batch-key-1")
        assert exc.value.diag.table_name == "generation_batches"
        assert exc.value.diag.constraint_name == "uq_generation_batches_user_project_key"
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM generation_batches WHERE idempotency_key = %s",
            ("batch-key-1",),
        )
        == 1
    )


def test_pg_generation_tasks_reject_orphan_batch_foreign_key(seeded: str) -> None:
    """``generation_tasks_batch_id_fkey`` enforcement: a task cannot reference a
    batch that does not exist (23503), proven on PG rather than by name only."""
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as exc:
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model) "
                "VALUES ('task_orphan', 'no_such_batch', 'fake_h3', 'h3')"
            )
        assert exc.value.diag.table_name == "generation_tasks"
        assert exc.value.diag.constraint_name == "generation_tasks_batch_id_fkey"


def test_pg_generation_batch_delete_cascades_to_tasks(seeded: str) -> None:
    """``generation_tasks_batch_id_fkey … ON DELETE CASCADE``: removing a batch
    removes its tasks in-database, so no orphaned task can outlive its batch."""
    with psycopg.connect(seeded, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO generation_tasks (id, batch_id, provider, model) "
            "VALUES ('task_cascade', 'batch_1', 'fake_h3', 'h3')"
        )
        assert (
            _count(
                seeded, "SELECT COUNT(*) FROM generation_tasks WHERE batch_id = %s", ("batch_1",)
            )
            == 2
        )
        conn.execute("DELETE FROM generation_batches WHERE id = 'batch_1'")
    assert (
        _count(seeded, "SELECT COUNT(*) FROM generation_tasks WHERE batch_id = %s", ("batch_1",))
        == 0
    )
    assert (
        _count(seeded, "SELECT COUNT(*) FROM generation_batches WHERE id = %s", ("batch_1",)) == 0
    )


def test_pg_character_generation_tasks_reject_uncertain_status(seeded: str) -> None:
    """DB-level proof of the CW-002 *registered limitation*.

    ``ck_character_generation_tasks_status`` admits only
    PENDING/RUNNING/SUCCEEDED/FAILED — **no** ``SUBMISSION_UNCERTAIN``. CW-030
    registers this behaviourally (the class fails closed through attempt
    exhaustion); this asserts the schema itself makes uncertainty unrepresentable
    for 人物图, so a stray UNCERTAIN write is refused by PostgreSQL.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            conn.execute(
                "INSERT INTO character_generation_tasks ("
                " id, character_version_id, view_type, provider, model,"
                " idempotency_key, request_hash, candidate_number, status"
                ") VALUES ('cg_uncertain', 'cv_1', 'FRONT_FACE', 'fake-image',"
                " 'fake-image-model', 'ik-cg-u', 'rh-cg-u', 1, 'SUBMISSION_UNCERTAIN')"
            )
        assert exc.value.diag.table_name == "character_generation_tasks"
        assert exc.value.diag.constraint_name == "ck_character_generation_tasks_status"


def test_pg_source_frame_tasks_reject_uncertain_status(seeded: str) -> None:
    """``ck_source_frame_tasks_status`` likewise excludes ``SUBMISSION_UNCERTAIN``
    (源帧 fails closed to manual recovery, never auto-uncertain) — the schema
    invariant behind CW-030's ``test_source_frame_expired_fails_closed_to_manual_recovery``."""
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            conn.execute(
                "INSERT INTO source_frame_tasks ("
                " id, project_id, asset_id, created_by_user_id, idempotency_key,"
                " request_hash, request_json, status"
                ") VALUES ('sf_uncertain', 'proj_1', 'asset_1', 'user_1', 'ik-sf-u',"
                " 'rh-sf-u', '{}', 'SUBMISSION_UNCERTAIN')"
            )
        assert exc.value.diag.table_name == "source_frame_tasks"
        assert exc.value.diag.constraint_name == "ck_source_frame_tasks_status"


def test_pg_first_frame_tasks_status_enum_boundary(seeded: str) -> None:
    """Contrast with the two classes above: ``ck_first_frame_tasks_status``
    *does* admit ``SUBMISSION_UNCERTAIN`` (image tasks quarantine an expired
    in-flight submission), while an arbitrary status is still refused. Pins the
    per-class status-enum boundary precisely on PG."""
    with psycopg.connect(seeded, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO first_frame_tasks ("
            " id, created_by_user_id, project_id, idempotency_key, request_hash,"
            " request_json, status"
            ") VALUES ('ff_uncertain', 'user_1', 'proj_1', 'ik-ff-u', 'rh-ff-u',"
            " '{}', 'SUBMISSION_UNCERTAIN')"
        )
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            conn.execute(
                "INSERT INTO first_frame_tasks ("
                " id, created_by_user_id, project_id, idempotency_key, request_hash,"
                " request_json, status"
                ") VALUES ('ff_bogus', 'user_1', 'proj_1', 'ik-ff-b', 'rh-ff-b',"
                " '{}', 'BOGUS_STATUS')"
            )
        assert exc.value.diag.table_name == "first_frame_tasks"
        assert exc.value.diag.constraint_name == "ck_first_frame_tasks_status"
    assert (
        _count(seeded, "SELECT COUNT(*) FROM first_frame_tasks WHERE id = %s", ("ff_uncertain",))
        == 1
    )


def test_pg_operation_cost_records_accept_unknown_reject_bad_status_and_unit(seeded: str) -> None:
    """DoD「未知收费」schema anchor: ``ck_operation_cost_status`` admits
    PENDING/ACTUAL/**UNKNOWN** (a provider charge whose amount is not yet known is
    a legal, frozen state), while an arbitrary status or unit is refused by
    ``ck_operation_cost_status`` / ``ck_operation_cost_unit``."""
    with psycopg.connect(seeded, autocommit=True) as conn:
        _insert_cost(conn, cost_id="cost_unknown", source_id="task_1", status="UNKNOWN")
        with pytest.raises(psycopg.errors.CheckViolation) as bad_status:
            _insert_cost(conn, cost_id="cost_bad_status", source_id="task_s", status="BOGUS")
        assert bad_status.value.diag.table_name == "operation_cost_records"
        assert bad_status.value.diag.constraint_name == "ck_operation_cost_status"
        with pytest.raises(psycopg.errors.CheckViolation) as bad_unit:
            _insert_cost(conn, cost_id="cost_bad_unit", source_id="task_u", unit="bogus_unit")
        assert bad_unit.value.diag.constraint_name == "ck_operation_cost_unit"
    assert (
        _count(
            seeded, "SELECT COUNT(*) FROM operation_cost_records WHERE status = %s", ("UNKNOWN",)
        )
        == 1
    )


def test_pg_operation_cost_records_reject_negative_and_enforce_source_subject_unique(
    seeded: str,
) -> None:
    """``ck_operation_cost_value_non_negative`` refuses a negative frozen cost, and
    ``uq_operation_cost_source_subject`` (source_type, source_id, subject) freezes
    exactly one actual-cost row per source/subject — no double-booked cost."""
    with psycopg.connect(seeded, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as negative:
            _insert_cost(conn, cost_id="cost_negative", source_id="task_neg", cost_fen=-5)
        assert negative.value.diag.table_name == "operation_cost_records"
        assert negative.value.diag.constraint_name == "ck_operation_cost_value_non_negative"

        _insert_cost(conn, cost_id="cost_first", source_id="task_dup")
        with pytest.raises(psycopg.errors.UniqueViolation) as dup:
            _insert_cost(conn, cost_id="cost_second", source_id="task_dup")
        assert dup.value.diag.table_name == "operation_cost_records"
        assert dup.value.diag.constraint_name == "uq_operation_cost_source_subject"
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM operation_cost_records WHERE source_id = %s",
            ("task_dup",),
        )
        == 1
    )


def test_pg_script_rewrite_tasks_enforce_ip_profile_shape(seeded: str) -> None:
    """``ck_script_rewrite_tasks_ip_profile_shape`` couples the optional character
    IP profile: either all three of (identity_id, snapshot, hash) are NULL, or all
    three are present with a 64-char hash. A half-populated profile is refused.

    Each shape probe gets its own project so the sibling partial unique index
    ``uq_script_rewrite_tasks_active_project`` (one active rewrite per project,
    proven in the next test) can never mask the ip-profile CHECK.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        for suffix in (2, 3, 4):
            conn.execute(
                "INSERT INTO projects (id, name, owner_user_id) VALUES (%s, %s, 'user_1')",
                (f"proj_{suffix}", f"CW-059 Tasks {suffix}"),
            )
        # legal: no IP profile at all.
        conn.execute(
            "INSERT INTO script_rewrite_tasks ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash, request_json"
            ") VALUES ('sr_null_profile', 'proj_1', 'user_1', 'ik-sr-1', 'rh-sr-1', '{}')"
        )
        # legal: full IP profile with a 64-char hash.
        conn.execute(
            "INSERT INTO script_rewrite_tasks ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash, request_json,"
            " identity_id, ip_profile_snapshot_json, ip_profile_hash"
            ") VALUES ('sr_full_profile', 'proj_2', 'user_1', 'ik-sr-2', 'rh-sr-2', '{}',"
            " 'identity_1', '{}', %s)",
            (_SHA_A,),
        )
        # illegal: identity + snapshot but a hash of the wrong length.
        with pytest.raises(psycopg.errors.CheckViolation) as bad_hash:
            conn.execute(
                "INSERT INTO script_rewrite_tasks ("
                " id, project_id, created_by_user_id, idempotency_key, request_hash,"
                " request_json, identity_id, ip_profile_snapshot_json, ip_profile_hash"
                ") VALUES ('sr_bad_hash', 'proj_3', 'user_1', 'ik-sr-3', 'rh-sr-3', '{}',"
                " 'identity_1', '{}', 'short')"
            )
        assert bad_hash.value.diag.table_name == "script_rewrite_tasks"
        assert bad_hash.value.diag.constraint_name == "ck_script_rewrite_tasks_ip_profile_shape"
        # illegal: identity set but snapshot missing.
        with pytest.raises(psycopg.errors.CheckViolation) as bad_snapshot:
            conn.execute(
                "INSERT INTO script_rewrite_tasks ("
                " id, project_id, created_by_user_id, idempotency_key, request_hash,"
                " request_json, identity_id, ip_profile_hash"
                ") VALUES ('sr_bad_snapshot', 'proj_4', 'user_1', 'ik-sr-4', 'rh-sr-4', '{}',"
                " 'identity_1', %s)",
                (_SHA_A,),
            )
        assert bad_snapshot.value.diag.constraint_name == "ck_script_rewrite_tasks_ip_profile_shape"
    assert _count(seeded, "SELECT COUNT(*) FROM script_rewrite_tasks") == 2


def test_pg_script_rewrite_tasks_reject_second_active_per_project(seeded: str) -> None:
    """``uq_script_rewrite_tasks_active_project``: one *active* rewrite per project.

    Discovered by the RED that shaped the test above — a partial unique index
    (``WHERE status IN ('PENDING','RUNNING')``) that ``pg_constraint`` does not
    list. A second active rewrite on the same project is refused, while a terminal
    (SUCCEEDED) rewrite does not occupy the slot, so a fresh one may start. Proves
    the partial-index WHERE boundary on real PG.
    """
    with psycopg.connect(seeded, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO script_rewrite_tasks ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash, request_json,"
            " status"
            ") VALUES ('sr_active_1', 'proj_1', 'user_1', 'ik-sa-1', 'rh-sa-1', '{}', 'PENDING')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as dup:
            conn.execute(
                "INSERT INTO script_rewrite_tasks ("
                " id, project_id, created_by_user_id, idempotency_key, request_hash,"
                " request_json, status"
                ") VALUES ('sr_active_2', 'proj_1', 'user_1', 'ik-sa-2', 'rh-sa-2', '{}',"
                " 'RUNNING')"
            )
        assert dup.value.diag.table_name == "script_rewrite_tasks"
        assert dup.value.diag.constraint_name == "uq_script_rewrite_tasks_active_project"
        # Retire the active row; the partial index no longer covers the project.
        conn.execute(
            "UPDATE script_rewrite_tasks SET status = 'SUCCEEDED' WHERE id = 'sr_active_1'"
        )
        conn.execute(
            "INSERT INTO script_rewrite_tasks ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash, request_json,"
            " status"
            ") VALUES ('sr_active_3', 'proj_1', 'user_1', 'ik-sa-3', 'rh-sa-3', '{}', 'PENDING')"
        )
    assert (
        _count(
            seeded, "SELECT COUNT(*) FROM script_rewrite_tasks WHERE project_id = %s", ("proj_1",)
        )
        == 2
    )


def test_pg_generation_task_operations_enforce_idempotency_unique(seeded: str) -> None:
    """``uq_generation_task_operations_idempotency`` (actor_user_id, task_id,
    action, idempotency_key) refuses a duplicate audited operation, so one actor's
    one action on one task under one idempotency key is recorded exactly once."""
    with psycopg.connect(seeded, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO generation_task_operations ("
            " id, task_id, actor_user_id, action, idempotency_key, request_hash, result_status"
            ") VALUES ('op_1', 'task_1', 'user_1', 'REGENERATE', 'op-key-1', 'op-hash-1',"
            " 'ACCEPTED')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as exc:
            conn.execute(
                "INSERT INTO generation_task_operations ("
                " id, task_id, actor_user_id, action, idempotency_key, request_hash,"
                " result_status"
                ") VALUES ('op_2', 'task_1', 'user_1', 'REGENERATE', 'op-key-1', 'op-hash-1',"
                " 'ACCEPTED')"
            )
        assert exc.value.diag.table_name == "generation_task_operations"
        assert exc.value.diag.constraint_name == "uq_generation_task_operations_idempotency"
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM generation_task_operations WHERE idempotency_key = %s",
            ("op-key-1",),
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Group B — the globally-unique task keys hold under REAL multi-connection
# concurrency (DoD「真实 PG 多连接」+「约束」): two independent sessions race and
# exactly one commits. The raw-DB foundation the legacy SQLite HTTP-level
# regeneration race (test_generation.py) could only assert through the route.
# ---------------------------------------------------------------------------


def _race_two(dsn: str, attempt: Callable[[str], None]) -> list[str]:
    """Run ``attempt(label)`` on two barrier-synchronised threads; return outcomes."""
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(label: str) -> None:
        barrier.wait()
        try:
            attempt(label)
            result = "inserted"
        except psycopg.errors.UniqueViolation:
            result = "duplicate"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(f"r{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_pg_concurrent_duplicate_batch_idempotency_commits_exactly_once(seeded: str) -> None:
    """Two connections race to submit the same batch idempotency key.

    ``uq_generation_batches_user_project_key`` must serialise them across
    independent sessions: exactly one batch commits, the loser receives
    ``UniqueViolation`` — the duplicate-submission guard is not an artefact of a
    single connection. This is the raw-DB guarantee behind the legacy SQLite
    ``test_generation.py::test_task_paid_regeneration_concurrency_creates_only_one_replacement``
    (which asserted [200, 409] only at the HTTP route level).
    """

    def attempt(label: str) -> None:
        with psycopg.connect(seeded, autocommit=True) as conn:
            _insert_batch(conn, batch_id=f"race_{label}", idempotency_key="CW059-RACE-KEY")

    outcomes = _race_two(seeded, attempt)
    assert sorted(outcomes) == ["duplicate", "inserted"], outcomes
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM generation_batches WHERE idempotency_key = %s",
            ("CW059-RACE-KEY",),
        )
        == 1
    )


def test_pg_concurrent_operation_cost_freeze_commits_exactly_once(seeded: str) -> None:
    """Two connections race to freeze the actual cost of one source/subject.

    ``uq_operation_cost_source_subject`` decides the winner: exactly one
    ``operation_cost_records`` row lands for (generation_task, task_race,
    output_seconds), so a provider charge is never double-booked even when two
    workers reconcile the same source concurrently.
    """

    def attempt(label: str) -> None:
        with psycopg.connect(seeded, autocommit=True) as conn:
            _insert_cost(conn, cost_id=f"cost_race_{label}", source_id="task_race")

    outcomes = _race_two(seeded, attempt)
    assert sorted(outcomes) == ["duplicate", "inserted"], outcomes
    assert (
        _count(
            seeded,
            "SELECT COUNT(*) FROM operation_cost_records WHERE source_id = %s",
            ("task_race",),
        )
        == 1
    )
