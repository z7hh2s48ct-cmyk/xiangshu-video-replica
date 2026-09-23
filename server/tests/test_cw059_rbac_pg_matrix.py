"""CW-059 — permission/RBAC-domain PostgreSQL route matrix (TEST-PG).

V3 收敛清单 CW-059 remaining work: ``补齐账务与全部任务测试的真实 PG 覆盖 …
旧测试 100% 映射``.  Segments 1–2 closed the 账务/支付/钱包 and 任务/生成/Worker
halves as raw-database constraint/concurrency matrices.  This module closes the
**权限 (RBAC)** sliver that ``docs/evidence/CW059-EVIDENCE.md`` §3.3 adjudicated
as belonging to CW-059, on a dedicated real-PostgreSQL database
(``cw059_rbac_test``, port-isolated at 5437) migrated to alembic head.

§3.3 pinned three ``test_rbac.py`` functions to CW-059.  Two are already proven
on real PostgreSQL elsewhere, so this file **references** them and does not
re-do them (DoD「剔除重复开发」):

* ``test_cross_user_wallet_is_owner_scoped_and_not_leakable`` (SES-05, wallet
  owner-scoping) → ``test_cw026_converged_auth.py::
  test_customer_session_reads_resolve_to_session_owner_only`` already proves on
  the converged customer-session PG lane that each session reads only its own
  ``/api/wallet`` and a displaced token is fenced with 401 ``SESSION_REPLACED``
  (a strictly stronger guarantee).  Registered in the Segment 1 matrix docstring.
* ``test_not_found_remap_preserves_deferred_postgres_denial_audit`` → a **pure
  in-memory unit test** of ``permissions.remap_security_denial`` (no database,
  backend-agnostic, so nothing to migrate); the *deferred PG denial-audit write*
  it guards is proven on real PostgreSQL by CW-010
  (``test_oral_domain.py`` / ``test_independent_creation.py``:
  ``pg_transaction`` catches ``AuditedSecurityDenial`` → ``persist_security_denial``
  → a durable ``audit_logs`` denial row).

The genuine gap — asserted **nowhere** on PostgreSQL (``PROJECT_DELETE_HAS_ACTIVE_TASKS``
appears only in the SQLite ``test_rbac.py``) — is
``test_project_delete_blocked_while_generation_tasks_are_active``: an in-flight
paid generation task must block ``DELETE /api/projects/{id}`` so its write-back is
not orphaned.  This file drives the **production route function** ``delete_project``
itself on a real PG ``BusinessConnection`` (never a copy of its SQL, never a
mocked database), proving both directions of the guard plus the PG-only
``customer_authorization_evidence`` transaction semantics that the SQLite lane
cannot express:

* every in-flight status the route's ``has_active_tasks`` predicate names
  (PENDING/SUBMITTING/QUEUED/RUNNING/ARCHIVING) → 409
  ``PROJECT_DELETE_HAS_ACTIVE_TASKS``, the project/task survive, and the
  authorization-evidence candidate written by ``require_project_access`` **rolls
  back** with the refused transaction (never a durable row on a 409);
* a terminal status (SUCCEEDED/FAILED) → 204, the project and its batch/task are
  removed by the FK cascade the route relies on, the delete is audited, and the
  PG-only authorization evidence **commits**.

Each test maps 1:1 to the legacy SQLite assertion it replaces; the mapping is
recorded in ``docs/evidence/CW059-EVIDENCE.md`` §3.3.
"""

from __future__ import annotations

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
from app.rbac_routes import delete_project

CW059_RBAC_DB_NAME = "cw059_rbac_test"

# Leaf-first DELETE order (the head schema guards append-only audit tables with a
# TRUNCATE trigger, so the scene reset walks explicit reverse-dependency DELETEs).
# customer_authorization_evidence is deliberately ABSENT: it is append-only — a
# row-level ``customer_authorization_evidence_refuse_rewrite`` trigger RAISEs on
# any DELETE/UPDATE (surfaced by the RED recorded in CW059-EVIDENCE §4), so it can
# never be reset. The evidence assertions below are therefore *delta*-based (row
# count before vs after the route call), which is both robust to that accumulation
# and a sharper proof of the transaction semantics: a refused delete rolls the
# authorization-evidence candidate back (delta 0), a committed delete persists it
# (delta +1). It stores only digests (no FK to users/projects), so leaving its
# rows in place never blocks the rest of the reset.
_CLEANUP_ORDER = (
    "audit_logs",
    "generation_tasks",
    "generation_batches",
    "projects",
    "users",
    "runtime_settings",
)

# The status sets delete_project's has_active_tasks predicate treats as in-flight
# (block) vs terminal (allow).  generation_tasks.status is an unconstrained text
# column; these are the exact literals the production query and the fair-queue
# counter (041_user_fair_queue) use.
_ACTIVE_STATUSES = ("PENDING", "SUBMITTING", "QUEUED", "RUNNING", "ARCHIVING")
_TERMINAL_STATUSES = ("SUCCEEDED", "FAILED")


# ---------------------------------------------------------------------------
# Fixtures — CW-007 kit helpers only (no hand-rolled DSN concatenation).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rbac_dsn() -> Iterator[str]:
    """A dedicated migrated PG database for the RBAC route matrix.

    ``require_pg_or_explicit_skip`` hard-gates (never a silent skip counting as
    evidence); ``create/drop_test_database`` are allowlist-guarded so cleanup can
    only ever touch ``cw059_rbac_test``.
    """
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW059_RBAC_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW059_RBAC_DB_NAME)


# ---------------------------------------------------------------------------
# Seed helpers — a single-owner project with one generation task, so the guard's
# decision is the only variable.
# ---------------------------------------------------------------------------


def _reset_and_seed(dsn: str, *, task_status: str) -> None:
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
            "INSERT INTO projects (id, name, owner_user_id) "
            "VALUES ('proj_1', 'CW-059 RBAC', 'user_1')"
        )
        conn.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash,"
            " request_snapshot_json, status, creation_kind"
            ") VALUES ('batch_1', 'proj_1', 'user_1', 'rbac-key-1', 'rbac-hash-1',"
            " '{}', 'QUEUED', 'replica')"
        )
        conn.execute(
            "INSERT INTO generation_tasks ("
            " id, batch_id, generation_mode, provider, model, status"
            ") VALUES ('task_1', 'batch_1', 'I2V', 'fake_h3', 'h3', %s)",
            (task_status,),
        )


def _owner_actor() -> CurrentUser:
    """The project owner: an ``employee`` — passes require_not_auditor and
    require_project_access without a denial."""
    return CurrentUser(id="user_1", username="user_1", display_name="User One", role="employee")


def _count(dsn: str, sql: str, params: tuple[object, ...] = ()) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# The guard, both directions, driving the real delete_project route on real PG.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_status", _ACTIVE_STATUSES)
def test_pg_project_delete_blocked_by_active_generation_task(
    rbac_dsn: str, task_status: str
) -> None:
    """An in-flight generation task blocks the project delete (409) and the
    refused transaction rolls back cleanly — the SQLite ``test_rbac.py::
    test_project_delete_blocked_while_generation_tasks_are_active`` invariant
    (which exercised only RUNNING) replayed against the production route on real
    PostgreSQL across every status the guard names."""
    _reset_and_seed(rbac_dsn, task_status=task_status)
    evidence_before = _count(rbac_dsn, "SELECT COUNT(*) FROM customer_authorization_evidence")

    with pytest.raises(HTTPException) as exc:
        # psycopg commits on clean exit / rolls back on exception, mirroring the
        # single fenced transaction the route runs inside in production.
        with psycopg.connect(rbac_dsn) as raw:
            delete_project(
                project_id="proj_1",
                conn=BusinessConnection.postgres(raw),
                actor=_owner_actor(),
            )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "PROJECT_DELETE_HAS_ACTIVE_TASKS"
    # The project and its in-flight task survive the refused delete.
    assert _count(rbac_dsn, "SELECT COUNT(*) FROM projects WHERE id = %s", ("proj_1",)) == 1
    assert _count(rbac_dsn, "SELECT COUNT(*) FROM generation_tasks WHERE id = %s", ("task_1",)) == 1
    # require_project_access wrote a PG-only authorization-evidence candidate, but
    # the 409 rolls the transaction back so it never becomes a durable row: the
    # append-only evidence count is unchanged (delta 0).
    assert (
        _count(rbac_dsn, "SELECT COUNT(*) FROM customer_authorization_evidence") == evidence_before
    )


@pytest.mark.parametrize("task_status", _TERMINAL_STATUSES)
def test_pg_project_delete_allowed_once_generation_task_terminal(
    rbac_dsn: str, task_status: str
) -> None:
    """Once the task settles (SUCCEEDED/FAILED) the same delete succeeds (204),
    the FK cascade removes the project's batch and task, the delete is audited,
    and the PG-only authorization evidence commits — the converse boundary that
    proves the guard is status-driven, not a blanket block."""
    _reset_and_seed(rbac_dsn, task_status=task_status)

    with psycopg.connect(rbac_dsn) as raw:
        response = delete_project(
            project_id="proj_1",
            conn=BusinessConnection.postgres(raw),
            actor=_owner_actor(),
        )

    assert response.status_code == 204
    # The project, its batch and its task are gone (the cascade the route relies
    # on: projects → generation_batches → generation_tasks).
    assert _count(rbac_dsn, "SELECT COUNT(*) FROM projects WHERE id = %s", ("proj_1",)) == 0
    assert (
        _count(rbac_dsn, "SELECT COUNT(*) FROM generation_batches WHERE id = %s", ("batch_1",)) == 0
    )
    assert _count(rbac_dsn, "SELECT COUNT(*) FROM generation_tasks WHERE id = %s", ("task_1",)) == 0
    # The delete is audited (audit_logs is reset per test, so this row is from THIS
    # committed transaction), and the PG-only owner-access authorization evidence is
    # durable: require_project_access writes a bounded digest pair deduplicated by
    # ON CONFLICT (resource_type, actor_digest, owner_digest) DO NOTHING, so exactly
    # one row exists for user_1 → user_1's project no matter how many terminal-status
    # deletes commit it (the second RED in CW059-EVIDENCE §4 surfaced this dedup).
    # The SQLite test can never assert it — recorded only when conn.is_postgres.
    assert (
        _count(rbac_dsn, "SELECT COUNT(*) FROM audit_logs WHERE action = %s", ("project.delete",))
        == 1
    )
    assert (
        _count(
            rbac_dsn,
            "SELECT COUNT(*) FROM customer_authorization_evidence WHERE resource_type = %s",
            ("project",),
        )
        == 1
    )
