"""CW-057 — every non-HTTP command has one classified PG entry.

Two layers live here:

1. **Entry contract (TEST-LOGIC, no PG needed).** The command×classification
   registry below is the machine-checked form of the CW-057 inventory: every
   ``server/scripts`` module, every ``deploy/systemd`` unit and every
   ``deploy/postgres`` tool must appear exactly once, current CLIs must
   resolve their database through ``app.db_pg.resolve_cli_pg_dsn`` (or the
   runtime resolver), and the CW-060 pair must stay untouched. The customer
   deployment chain must not reference the internal SQLite backup entry at
   all (PG-08 旧SQLite定时备份退出正式部署; the physical package exclusion is
   CW-032's, the reachability floor is ours).
2. **Per-command PG behaviour (TEST-PG).** Current maintenance CLIs succeed
   against a real migrated PostgreSQL database, their read-only mode changes
   zero business rows, and a missing / ``sqlite://`` / ``DB_PATH`` /
   wrong-scheme DSN exits non-zero without creating any file — run with no
   reachable PG at all so "rejects before connecting" is actually proven
   (PG-01 缺/错/SQLite DSN 非零失败且不创建数据库文件).

The deep write/idempotency semantics of each purge domain keep living in
their domain suites (test_customer_idempotency, test_customer_security,
test_wallet_billing_service); CW-057 asserts the shared entry, not a
duplicate of those behaviours.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"

CW057_TEST_DB = "cw057_gate1_seed_test"

# ---------------------------------------------------------------------------
# The CW-057 inventory: command × classification × database × role × writes.
# classification is one of: current-pg, current-offline, historical-internal-p0,
# historical-cw060, runtime.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandRow:
    command: str
    entry: str
    classification: str
    database: str
    role: str
    writes: str


CURRENT_CLI_MODULES: tuple[CommandRow, ...] = tuple(
    CommandRow(row[0], row[1], row[2], row[3], row[4], row[5])
    for row in (
        (
            "python -m scripts.purge_idempotency_envelopes",
            "scripts/purge_idempotency_envelopes.py",
            "current-pg",
            "PostgreSQL (business)",
            "maintenance timer",
            "write; window-based re-run is a no-op; counts-only output",
        ),
        (
            "python -m scripts.purge_expired_export_ciphertexts",
            "scripts/purge_expired_export_ciphertexts.py",
            "current-pg",
            "PostgreSQL (business)",
            "maintenance timer",
            "write; window-based re-run is a no-op; counts-only output",
        ),
        (
            "python -m scripts.purge_stale_rate_limit_counters",
            "scripts/purge_stale_rate_limit_counters.py",
            "current-pg",
            "PostgreSQL (business)",
            "maintenance timer",
            "write; window-based re-run is a no-op; append-only audit untouched",
        ),
        (
            "python -m scripts.purge_external_call_logs",
            "scripts/purge_external_call_logs.py",
            "current-pg",
            "PostgreSQL (business)",
            "maintenance timer",
            "write; retention-window re-run is a no-op; counts-only output",
        ),
        (
            "python -m scripts.reconcile_dangling_billing_reservations",
            "scripts/reconcile_dangling_billing_reservations.py",
            "current-pg",
            "PostgreSQL (business)",
            "reconciliation timer",
            "write; ledger-keyed idempotent; counts-only output",
        ),
        (
            "python -m scripts.close_expired_native_orders",
            "scripts/close_expired_native_orders.py",
            "current-pg",
            "PostgreSQL (business)",
            "maintenance timer",
            "write; deadline-based re-run is a no-op; counts-only output",
        ),
        (
            "python -m scripts.reconcile_pending_native_orders",
            "scripts/reconcile_pending_native_orders.py",
            "current-pg",
            "PostgreSQL (business)",
            "reconciliation timer",
            "write; settlement rides the ledger-idempotent fund path; counts-only output",
        ),
        (
            "python -m scripts.check_ops_alerts",
            "scripts/check_ops_alerts.py",
            "current-pg",
            "PostgreSQL (ops state)",
            "ops alerting timer",
            "probe read + advisory-locked state write; JSON redacted errors",
        ),
        (
            "python -m scripts.issue_admin_exchange_credential",
            "scripts/issue_admin_exchange_credential.py",
            "current-offline",
            "none (HMAC only, printed once)",
            "admin management",
            "no database access",
        ),
        (
            "python -m scripts.pitr_recovery_facts",
            "scripts/pitr_recovery_facts.py",
            "current-pg",
            "PostgreSQL (PGSERVICE, no password on argv)",
            "backup verification",
            "capture writes manifest file; verify is read-only",
        ),
        (
            "python -m app.gate1_bootstrap",
            "app/gate1_bootstrap.py",
            "current-pg",
            "PostgreSQL (business, pristine migrated DB)",
            "seed",
            "single-transaction seed; advisory-locked; refuses non-pristine; redacted summary",
        ),
        (
            "python -m app.gate1_e2e",
            "app/gate1_e2e.py",
            "current-pg",
            "PostgreSQL (business, migrated by harness)",
            "dev E2E harness",
            "launches API/Worker/Playwright against the PG lane",
        ),
        (
            "python -m app.bootstrap provision-empty-customer",
            "app/bootstrap.py",
            "current-pg",
            "PostgreSQL (business)",
            "admin provisioning",
            "one-shot; SERIALIZABLE + advisory lock + pristine check",
        ),
    )
)

HISTORICAL_CLI_ROWS: tuple[CommandRow, ...] = tuple(
    CommandRow(row[0], row[1], row[2], row[3], row[4], row[5])
    for row in (
        (
            "python -m app.backup <check|backup|restore|daily>",
            "app/backup.py",
            "historical-internal-p0",
            "SQLite file (internal P0 lane)",
            "internal backup",
            "customer path is deploy/postgres/pitr-*.sh; excluded from the customer "
            "package (CW-032); exits with CW-040/CW-042",
        ),
        (
            "python -m scripts.sqlite_to_postgres",
            "scripts/sqlite_to_postgres.py",
            "historical-cw060",
            "SQLite source → PG target (one-shot)",
            "historical import",
            "CW-060 isolated operator artifact only",
        ),
        (
            "python -m scripts.reconcile_customer_billing",
            "scripts/reconcile_customer_billing.py",
            "historical-cw060",
            "SQLite source → PG target (one-shot)",
            "historical reconciliation",
            "CW-060 isolated operator artifact only",
        ),
        (
            "python -m scripts.backfill_content_objects",
            "scripts/backfill_content_objects.py",
            "current-pg",
            "PostgreSQL (business)",
            "backfill (one-shot)",
            "dry-run by default; writes only with --apply; idempotent; "
            "registers existing objects without moving any bytes",
        ),
    )
)

# CW-040-b: the internal SQLite backup units are physically retired — the
# historical registry rows are kept as comments for provenance.
# ("video-replica-backup.service", "video-replica-backup.timer")
HISTORICAL_INTERNAL_P0_UNITS: tuple[str, ...] = ()

CURRENT_PG_UNITS = (
    "video-replica-api.service",
    "video-replica-api@.service",
    "video-replica-worker.service",
    "video-replica-worker@.service",
    # PUBLISH-DELIVERY-20260917: platform deliveries + account probes (PG only).
    "video-replica-publish-worker.service",
    "video-replica-maintenance.service",
    "video-replica-maintenance.timer",
    "video-replica-maintenance-alert.service",
    "video-replica-pitr-backup.service",
    "video-replica-pitr-backup.timer",
    "video-replica-ops-alerts.service",
    "video-replica-ops-alerts.timer",
)

CURRENT_PG_SHELL_TOOLS = (
    "deploy/postgres/migrate.sh",
    "deploy/postgres/pitr-backup.sh",
    "deploy/postgres/pitr-fetch-wal.sh",
    "deploy/postgres/pitr-preflight.sh",
    "deploy/postgres/pitr-restore-drill.sh",
)

CURRENT_OFFLINE_TOOLS = (
    "scripts/customer_release_preflight.py",
    "scripts/verify_no_secrets.sh",
    "scripts/require_customer_api_base.mjs",
    "scripts/verify_customer_bundle.mjs",
    "scripts/pg-fixture.sh",
    "scripts/dev-with-pg.sh",
)

# Modules allowed to bypass resolve_cli_pg_dsn among the current CLI surface.
_CURRENT_ENTRY_EXCEPTIONS = {
    # No database access at all.
    "scripts/issue_admin_exchange_credential.py",
    # Talks to PG through the PGSERVICEFILE indirection (no argv password) —
    # its PG-only contract is enforced by pitr.env, not by a DSN argument.
    "scripts/pitr_recovery_facts.py",
}

# ---------------------------------------------------------------------------
# Layer 1 — the registry is complete and the code matches it.
# ---------------------------------------------------------------------------


def test_every_server_scripts_module_is_classified() -> None:
    script_names = {path.name for path in (SERVER_DIR / "scripts").glob("*.py")}
    script_names.discard("__init__.py")
    registered = {
        row.entry.removeprefix("scripts/")
        for row in CURRENT_CLI_MODULES + HISTORICAL_CLI_ROWS
        if row.entry.startswith("scripts/")
    }

    assert script_names == registered


def test_every_deployed_unit_is_classified() -> None:
    unit_names = {path.name for path in (REPO_ROOT / "deploy" / "systemd").glob("video-replica-*")}
    current_units = {name for name in CURRENT_PG_UNITS}
    historical_units = set(HISTORICAL_INTERNAL_P0_UNITS)

    assert unit_names == current_units | historical_units


def test_registry_positions_are_consistent() -> None:
    for row in CURRENT_CLI_MODULES + HISTORICAL_CLI_ROWS:
        assert (SERVER_DIR / row.entry).is_file(), row
    for name in HISTORICAL_INTERNAL_P0_UNITS:
        assert (REPO_ROOT / "deploy" / "systemd" / name).is_file()
    for relative in CURRENT_PG_SHELL_TOOLS + CURRENT_OFFLINE_TOOLS:
        assert (REPO_ROOT / relative).is_file()


def test_current_pg_cli_modules_resolve_through_the_unified_entry() -> None:
    for row in CURRENT_CLI_MODULES:
        if row.classification != "current-pg":
            continue
        source = (SERVER_DIR / row.entry).read_text(encoding="utf-8")
        if row.entry in _CURRENT_ENTRY_EXCEPTIONS:
            continue
        assert "resolve_cli_pg_dsn" in source or "resolve_database_config" in source, (
            f"{row.entry} must resolve its DSN through the CW-057 unified PG entry"
        )
        assert "import sqlite3" not in source, f"{row.entry} must not import the SQLite driver"


def test_customer_deployment_chain_has_no_sqlite_backup_entry() -> None:
    customer_units = ("video-replica-maintenance", "video-replica-pitr-backup")
    for name in customer_units:
        text = (REPO_ROOT / "deploy/systemd" / f"{name}.service").read_text(encoding="utf-8")
        assert "app.backup" not in text, f"{name}.service must not reference app.backup"
        assert "video-replica-backup" not in text, (
            f"{name}.service must not reference the internal SQLite backup unit"
        )
    rollout = (REPO_ROOT / "deploy/customer-git-rollout.sh").read_text(encoding="utf-8")
    assert "app.backup" not in rollout
    assert "video-replica-backup" not in rollout
    assert "VIDEO_REPLICA_DB_PATH" not in rollout
    env_example = (REPO_ROOT / "deploy/customer.env.example").read_text(encoding="utf-8")
    assert "app.backup" not in env_example
    # No active DB_PATH assignment: the customer env may only mention the
    # rejected SQLite variants inside explanatory comments.
    assert "VIDEO_REPLICA_DB_PATH=" not in env_example
    for line in env_example.splitlines():
        if "video-replica-backup" in line:
            assert line.lstrip().startswith("#"), (
                f"video-replica-backup may only appear in comments: {line.strip()}"
            )
    # The customer env keeps the explicit pointer to the PG backup channel.
    assert "pitr" in env_example.lower()


def test_historical_import_tools_stay_outside_cw057_boundary() -> None:
    """The CW-060 pair must not be silently reworked by the CW-057 entry."""

    for name in ("sqlite_to_postgres.py", "reconcile_customer_billing.py"):
        source = (SERVER_DIR / "scripts" / name).read_text(encoding="utf-8")
        assert "resolve_cli_pg_dsn" not in source, (
            f"{name} belongs to the CW-060 isolated artifact, not the unified entry"
        )


# ---------------------------------------------------------------------------
# Layer 2 — per-command rejection and PG behaviour.
# ---------------------------------------------------------------------------

_TIMER_CLI_MAINS: tuple[str, ...] = (
    "scripts.purge_idempotency_envelopes",
    "scripts.purge_expired_export_ciphertexts",
    "scripts.purge_stale_rate_limit_counters",
    "scripts.purge_external_call_logs",
    "scripts.reconcile_dangling_billing_reservations",
    "scripts.close_expired_native_orders",
    "scripts.reconcile_pending_native_orders",
)


def _import_cli_main(module_name: str):
    module = __import__(module_name, fromlist=["main"])
    return module.main


@pytest.mark.parametrize("module_name", _TIMER_CLI_MAINS)
@pytest.mark.parametrize(
    ("database_url_argv", "set_db_path"),
    [
        pytest.param([], False, id="missing-dsn"),
        pytest.param(["--database-url", ""], False, id="empty-dsn"),
        pytest.param(
            ["--database-url", "sqlite:///./cw057-should-not-exist.sqlite3"], False, id="sqlite-url"
        ),
        pytest.param(["--database-url", "mysql://u:p@localhost/db"], False, id="wrong-scheme"),
        pytest.param(
            ["--database-url", "postgresql://u:p@localhost:5/name"], True, id="db-path-leftover"
        ),
    ],
)
def test_current_clis_reject_non_pg_configuration_without_creating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    database_url_argv: list[str],
    set_db_path: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    if set_db_path:
        monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "leftover.sqlite3"))
    else:
        monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)

    exit_code = _import_cli_main(module_name)([*database_url_argv])

    assert exit_code == 1, module_name
    # Nothing was created: no SQLite file, no sidecar, no stray directory.
    assert list(tmp_path.iterdir()) == [], module_name


@pytest.fixture(scope="module")
def cw057_pg_dsn() -> Iterator[str]:
    """A dedicated migrated database for the per-command PG behaviour."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW057_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW057_TEST_DB)


@pytest.mark.parametrize("module_name", _TIMER_CLI_MAINS)
def test_current_clis_accept_a_valid_pg_dsn_in_read_only_mode(
    cw057_pg_dsn: str,
    module_name: str,
) -> None:
    """--dry-run is the read-only mode: exit 0, zero business rows changed."""

    main = _import_cli_main(module_name)

    assert main(["--database-url", cw057_pg_dsn, "--dry-run"]) == 0


def test_purge_cli_write_mode_is_idempotent(cw057_pg_dsn: str) -> None:
    """A real run twice in a row: second run finds nothing left to purge."""

    main = _import_cli_main("scripts.purge_idempotency_envelopes")

    assert main(["--database-url", cw057_pg_dsn]) == 0
    assert main(["--database-url", cw057_pg_dsn]) == 0


def test_gate1_e2e_cli_rejects_a_sqlite_dsn_before_any_harness_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness entry fails closed on a SQLite DSN (PG-01)."""

    from app.gate1_e2e import main as gate1_e2e_main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["gate1_e2e", "--database-url", "sqlite:///x"])

    with pytest.raises(SystemExit) as excinfo:
        gate1_e2e_main()

    assert excinfo.value.code != 0
    # No run directory, no database file — the rejection happened first.
    assert list(tmp_path.iterdir()) == []
