"""One-shot, fail-closed SQLite-to-PostgreSQL migration for T07 / DB-05.

This is not a dual-write bridge. It requires a confirmed maintenance window,
creates an immutable private SQLite snapshot, imports all rows in one
PostgreSQL transaction, reconciles table/billing/asset facts, and commits only
when every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict, deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json, Jsonb

from app.backup import SqliteSnapshot, create_readonly_snapshot, sha256_file
from scripts.reconcile_customer_billing import (
    PG_ONLY_COLUMNS,
    PG_ONLY_TABLES,
    ReconciliationIssue,
    ReconciliationReport,
    _pg_columns,
    _sqlite_columns,
    _table_names,
    connect_sqlite_readonly,
    pg_only_table_has_divergent_state,
    reconcile_connection_pair,
    safe_error_message,
    validate_database_invariants,
)

ImportStatus = Literal["imported", "already_reconciled"]
SERVER_DIR = Path(__file__).resolve().parent.parent
SEED_TABLES = frozenset({"runtime_settings", "viral_runtime_controls"})
MIGRATION_ADVISORY_LOCK_KEYS = (0x543037, 0x44423035)
# 042 materializes UTC timestamps for two shared tables. T07 runs before the
# customer-production line opens, but its testable cutover contract permits an
# import into the current PG head. Keep these companions equal to their legacy
# source facts instead of letting the PG default make old work look recent.
PG_DERIVED_IMPORT_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "audit_logs": (("occurred_at", "created_at"),),
    "generation_tasks": (("created_at_utc", "created_at"),),
}
WALLET_LEDGER_TRIGGER = "trg_wallet_transactions_assign_ledger_sequence"
WALLET_LEDGER_SEQUENCE = "wallet_ledger_sequence_seq"


class MigrationSafetyError(RuntimeError):
    """The source or target does not satisfy the cutover safety contract."""


class MigrationReconciliationError(MigrationSafetyError):
    """Imported data failed one or more fail-closed reconciliation checks."""


MigrationPreconditionError = MigrationSafetyError


@dataclass(frozen=True)
class ImportedTable:
    table: str
    source_rows: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ImportResult:
    status: ImportStatus
    snapshot_path: str
    source_sha256: str
    snapshot_sha256: str
    tables: tuple[ImportedTable, ...]
    reconciliation: ReconciliationReport

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "snapshot_path": self.snapshot_path,
            "source_sha256": self.source_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "tables": [table.to_dict() for table in self.tables],
            "reconciliation": self.reconciliation.to_dict(),
        }


def _alembic_script() -> ScriptDirectory:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_DIR / "migrations"))
    return ScriptDirectory.from_config(config)


def release_head_revision() -> str:
    head = _alembic_script().get_current_head()
    if head is None:
        raise MigrationSafetyError("Alembic has no single migration head")
    return head


_release_head_revision = release_head_revision


def validate_revision_pair(
    source_revision: str,
    target_revision: str,
    *,
    expected_head: str | None = None,
) -> None:
    expected = expected_head or release_head_revision()
    if source_revision != expected or target_revision != expected:
        raise MigrationSafetyError(
            "Alembic revision mismatch: expected T07 Alembic head "
            f"{expected}; source={source_revision}, target={target_revision}"
        )


def _sqlite_revision(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None:
        raise MigrationSafetyError("source Alembic revision is missing")
    return str(row[0])


def _pg_revision(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None:
        raise MigrationSafetyError("target Alembic revision is missing")
    if isinstance(row, Mapping):
        return str(row["version_num"])
    return str(row[0])


def _validate_revisions(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
) -> None:
    validate_revision_pair(_sqlite_revision(sqlite_conn), _pg_revision(pg_conn))


def require_maintenance_window(confirmed: bool) -> None:
    if not confirmed:
        raise MigrationSafetyError(
            "maintenance window confirmation is required; stop all SQLite writers first"
        )


def require_migration_lock(pg_conn: Any) -> None:
    row = pg_conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s) AS acquired",
        MIGRATION_ADVISORY_LOCK_KEYS,
    ).fetchone()
    if row is None:
        raise MigrationSafetyError("migration advisory-lock query returned no row")
    acquired = bool(row["acquired"] if isinstance(row, Mapping) else row[0])
    if not acquired:
        raise MigrationSafetyError(
            "another T07 migration owns the PostgreSQL cutover lock; aborting"
        )


def require_validated_postgres_foreign_keys(pg_conn: Any) -> None:
    row = pg_conn.execute(
        """
        SELECT COUNT(*)
        FROM pg_constraint
        WHERE contype = 'f' AND NOT convalidated
        """
    ).fetchone()
    if row is None:
        raise MigrationSafetyError("PostgreSQL foreign-key validation query returned no row")
    count = int(row[0] if not isinstance(row, Mapping) else next(iter(row.values())))
    if count:
        raise MigrationSafetyError(
            f"target has {count} unvalidated foreign-key constraints; validate them before import"
        )


def _validate_schema(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
) -> list[str]:
    source_tables = _table_names(sqlite_conn, "sqlite")
    target_tables = set(_table_names(pg_conn, "postgresql"))
    missing = sorted(set(source_tables) - target_tables)
    extra = sorted(target_tables - set(source_tables))
    # PG-only tables from revisions 026/027/028/029/031/032 and later
    # customer-line revisions (admin_sessions; the activation code catalog;
    # the customer device slots, session state/events and idempotency
    # envelopes; the admin write idempotency ledger; the shared security
    # rate-limit counters and auth-failure audit; the administrator
    # recharge packages, 20260922T1200) are expected on the target head but
    # must still be empty: the T07 cutover happens before the customer
    # production line opens, so any row there is divergent state.
    unexpected_extra = [table for table in extra if table not in PG_ONLY_TABLES]
    divergent_pg_only = [
        table
        for table in extra
        if table in PG_ONLY_TABLES and pg_only_table_has_divergent_state(pg_conn, table)
    ]
    if missing or unexpected_extra or divergent_pg_only:
        raise MigrationSafetyError(
            "source/target table contract differs "
            f"(missing={len(missing)}, extra={len(unexpected_extra) + len(divergent_pg_only)})"
        )
    for table in source_tables:
        source_columns, source_pk = _sqlite_columns(sqlite_conn, table)
        target_columns, target_pk, _ = _pg_columns(pg_conn, table)
        # Exempt PG-only columns (the 041 fair-queue setting and 042 typed
        # probe timestamps): the target carries them, the T07 source never does.
        pg_only = PG_ONLY_COLUMNS.get(table, frozenset())
        if set(source_columns) != set(target_columns) - pg_only or source_pk != target_pk:
            raise MigrationSafetyError(f"source/target schema differs for table {table!r}")
    return source_tables


def _issue_summary(issues: Sequence[ReconciliationIssue]) -> str:
    codes = sorted({issue.code for issue in issues})
    if "asset_reference_orphan" in codes:
        return "asset reference reconciliation failed (asset_reference_orphan)"
    if "asset_reference_json_invalid" in codes:
        return "asset reference reconciliation failed (asset_reference_json_invalid)"
    return "database reconciliation failed (" + ", ".join(codes[:8]) + ")"


def _ensure_source_invariants(sqlite_conn: sqlite3.Connection) -> None:
    issues = validate_database_invariants(sqlite_conn, "sqlite", "source")
    if issues:
        raise MigrationReconciliationError(_issue_summary(issues))


def _target_has_non_seed_data(pg_conn: psycopg.Connection[Any], tables: Sequence[str]) -> bool:
    for table in tables:
        if table in SEED_TABLES:
            continue
        count_query = sql.SQL("SELECT COUNT(*) AS count FROM {}").format(sql.Identifier(table))
        row = pg_conn.execute(count_query).fetchone()
        if row is None:
            raise MigrationSafetyError(f"count query returned no row for table {table!r}")
        count = int(row["count"] if isinstance(row, Mapping) else row[0])
        if count:
            return True
    return False


def topological_order(
    nodes: set[str] | Sequence[str],
    dependencies: Mapping[str, set[str]],
) -> list[str]:
    node_set = set(nodes)
    remaining = {node: set(dependencies.get(node, set())) & node_set for node in node_set}
    dependants: dict[str, set[str]] = defaultdict(set)
    for node, parents in remaining.items():
        for parent in parents:
            dependants[parent].add(node)
    ready = deque(sorted(node for node, parents in remaining.items() if not parents))
    ordered: list[str] = []
    queued = set(ready)
    while ready:
        node = ready.popleft()
        queued.discard(node)
        ordered.append(node)
        for dependant in sorted(dependants.get(node, set())):
            remaining[dependant].discard(node)
            if not remaining[dependant] and dependant not in ordered and dependant not in queued:
                ready.append(dependant)
                queued.add(dependant)
    if len(ordered) != len(node_set):
        blocked = sorted(node_set - set(ordered))
        raise MigrationSafetyError(
            f"foreign-key dependency cycle prevents safe import: {len(blocked)} tables"
        )
    return ordered


def _dependency_order(sqlite_conn: sqlite3.Connection, tables: Sequence[str]) -> list[str]:
    nodes = set(tables)
    dependencies: dict[str, set[str]] = {table: set() for table in tables}
    for table in tables:
        escaped = '"' + table.replace('"', '""') + '"'
        for row in sqlite_conn.execute(f"PRAGMA foreign_key_list({escaped})").fetchall():
            parent = str(row[2])
            if parent in nodes and parent != table:
                dependencies[table].add(parent)
    return topological_order(nodes, dependencies)


def _convert_value(value: object, target_type: str) -> object:
    if value is None:
        return None
    normalized_type = target_type.lower()
    if normalized_type in {"text", "varchar", "bpchar", "name"}:
        return str(value)
    if normalized_type in {"int2", "int4", "int8", "smallint", "integer", "bigint"}:
        return int(value)
    if normalized_type in {"float4", "float8", "real", "double precision"}:
        return float(value)
    if normalized_type in {"numeric", "decimal"}:
        return Decimal(str(value))
    if normalized_type in {"bool", "boolean"}:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "t", "yes", "on"}:
                return True
            if normalized in {"0", "false", "f", "no", "off"}:
                return False
            raise MigrationSafetyError("invalid boolean value in SQLite source")
        return bool(value)
    if normalized_type == "bytea":
        if isinstance(value, memoryview):
            return bytes(value)
        return value if isinstance(value, bytes) else bytes(value)
    if normalized_type == "uuid":
        return value if isinstance(value, UUID) else UUID(str(value))
    if normalized_type in {"json", "jsonb"}:
        parsed = json.loads(value) if isinstance(value, str) else value
        return Jsonb(parsed) if normalized_type == "jsonb" else Json(parsed)
    if normalized_type == "date" and isinstance(value, str):
        return date.fromisoformat(value)
    if normalized_type in {"timestamp", "timestamptz"} and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _legacy_timestamp_as_utc(value: object) -> datetime:
    """Interpret offset-free SQLite timestamps as UTC under the T07 contract."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise MigrationSafetyError("invalid legacy timestamp in SQLite source") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sqlite_row_batches(
    sqlite_conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    batch_size: int,
) -> Iterator[list[sqlite3.Row]]:
    escaped_table = '"' + table.replace('"', '""') + '"'
    escaped_columns = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
    cursor = sqlite_conn.execute(f"SELECT {escaped_columns} FROM {escaped_table}")
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield rows


def _insert_table_rows(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
    table: str,
    columns: Sequence[str],
    primary_key: Sequence[str],
    target_types: Mapping[str, str],
    *,
    batch_size: int = 1000,
) -> int:
    derived_columns = PG_DERIVED_IMPORT_COLUMNS.get(table, ())
    insert_columns = tuple(columns) + tuple(column for column, _source in derived_columns)
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in insert_columns)
    base = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in insert_columns),
        placeholders,
    )
    if table in SEED_TABLES:
        if not primary_key:
            raise MigrationSafetyError(f"seed table {table!r} has no primary key")
        update_columns = [column for column in insert_columns if column not in primary_key]
        if update_columns:
            updates = sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
                for column in update_columns
            )
            query = base + sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
                sql.SQL(", ").join(sql.Identifier(column) for column in primary_key),
                updates,
            )
        else:
            query = base + sql.SQL(" ON CONFLICT DO NOTHING")
    else:
        query = base

    total = 0
    with pg_conn.cursor() as cursor:
        for rows in _sqlite_row_batches(sqlite_conn, table, columns, batch_size):
            parameters = [
                tuple(_convert_value(row[column], target_types[column]) for column in columns)
                + tuple(
                    _legacy_timestamp_as_utc(row[source]) for _column, source in derived_columns
                )
                for row in rows
            ]
            cursor.executemany(query, parameters)
            total += len(rows)
    return total


def _set_wallet_ledger_trigger(
    pg_conn: psycopg.Connection[Any], *, enabled: bool, trigger: str = WALLET_LEDGER_TRIGGER
) -> None:
    action = sql.SQL("ENABLE") if enabled else sql.SQL("DISABLE")
    pg_conn.execute(
        sql.SQL("ALTER TABLE wallet_transactions {} TRIGGER {}").format(
            action,
            sql.Identifier(trigger),
        )
    )
    row = pg_conn.execute(
        "SELECT tgenabled FROM pg_trigger "
        "WHERE tgrelid = 'wallet_transactions'::regclass AND tgname = %s",
        (trigger,),
    ).fetchone()
    expected = "O" if enabled else "D"
    if row is None or str(row["tgenabled"] if isinstance(row, Mapping) else row[0]) != expected:
        raise MigrationSafetyError("wallet ledger sequence trigger state could not be verified")


def _reset_wallet_ledger_sequence(pg_conn: psycopg.Connection[Any]) -> None:
    row = pg_conn.execute(
        "SELECT MAX(ledger_sequence) AS maximum FROM wallet_transactions"
    ).fetchone()
    maximum = None if row is None else (row["maximum"] if isinstance(row, Mapping) else row[0])
    pg_conn.execute(
        "SELECT setval(%s, %s, %s)",
        (WALLET_LEDGER_SEQUENCE, 1 if maximum is None else int(maximum), maximum is not None),
    )


def _reset_sequences(pg_conn: psycopg.Connection[Any], tables: Sequence[str]) -> None:
    rows = pg_conn.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND (is_identity = 'YES' OR column_default LIKE 'nextval(%')
        ORDER BY table_name, ordinal_position
        """
    ).fetchall()
    allowed = set(tables)
    for row in rows:
        table = str(row["table_name"] if isinstance(row, Mapping) else row[0])
        column = str(row["column_name"] if isinstance(row, Mapping) else row[1])
        if table not in allowed:
            continue
        sequence_row = pg_conn.execute(
            "SELECT pg_get_serial_sequence(%s, %s) AS sequence_name",
            (table, column),
        ).fetchone()
        sequence = None
        if sequence_row is not None:
            sequence = (
                sequence_row["sequence_name"]
                if isinstance(sequence_row, Mapping)
                else sequence_row[0]
            )
        if not sequence:
            continue
        maximum_query = sql.SQL("SELECT MAX({}) AS maximum FROM {}").format(
            sql.Identifier(column), sql.Identifier(table)
        )
        maximum_row = pg_conn.execute(maximum_query).fetchone()
        if maximum_row is None:
            raise MigrationSafetyError(f"sequence maximum query returned no row for {table!r}")
        maximum = maximum_row["maximum"] if isinstance(maximum_row, Mapping) else maximum_row[0]
        if maximum is None:
            pg_conn.execute("SELECT setval(%s, 1, false)", (sequence,))
        else:
            pg_conn.execute("SELECT setval(%s, %s, true)", (sequence, int(maximum)))


def _default_snapshot_path(source: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return source.with_name(f"{source.stem}.t07-snapshot-{stamp}{source.suffix or '.db'}")


def _write_report(path: str | Path | None, result: ImportResult) -> None:
    if path is None:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # The report fingerprints the whole database (schema, row counts, hashes);
    # 0600 like the snapshot it describes (M1 review LOW).
    os.chmod(output, 0o600)


def _result(
    status: ImportStatus,
    snapshot: SqliteSnapshot,
    tables: Sequence[ImportedTable],
    reconciliation: ReconciliationReport,
) -> ImportResult:
    return ImportResult(
        status=status,
        snapshot_path=str(snapshot.path),
        source_sha256=snapshot.source_sha256,
        snapshot_sha256=snapshot.snapshot_sha256,
        tables=tuple(tables),
        reconciliation=reconciliation,
    )


def _validate_snapshot(snapshot: SqliteSnapshot) -> None:
    if not snapshot.path.is_file():
        raise FileNotFoundError(snapshot.path)
    actual = sha256_file(snapshot.path)
    if actual != snapshot.snapshot_sha256:
        raise MigrationSafetyError("SQLite snapshot SHA-256 does not match its immutable evidence")
    # Windows reports a synthesized 0o666 mode for every file regardless of
    # the 0600 creation request (mirrors the platform guard in app/backup.py).
    if sys.platform != "win32" and snapshot.path.stat().st_mode & 0o077:
        raise MigrationSafetyError("SQLite snapshot is not private; expected permissions 0600")


def migrate_snapshot(
    snapshot: SqliteSnapshot,
    postgres_dsn: str,
    *,
    report_path: str | Path | None = None,
    batch_size: int = 1000,
    fail_after_table: str | None = None,
) -> ImportResult:
    """Import one verified snapshot and commit only after full reconciliation."""

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    _validate_snapshot(snapshot)

    with closing(connect_sqlite_readonly(snapshot.path)) as sqlite_conn:
        _ensure_source_invariants(sqlite_conn)
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as pg_conn:
            with pg_conn.transaction():
                require_migration_lock(pg_conn)
                _validate_revisions(sqlite_conn, pg_conn)
                require_validated_postgres_foreign_keys(pg_conn)
                tables = _validate_schema(sqlite_conn, pg_conn)

                current = reconcile_connection_pair(sqlite_conn, pg_conn)
                if current.ok:
                    result = _result("already_reconciled", snapshot, (), current)
                    _write_report(report_path, result)
                    return result
                if _target_has_non_seed_data(pg_conn, tables):
                    raise MigrationSafetyError(
                        "target PostgreSQL is non-empty and does not reconcile exactly; "
                        "refusing to overwrite or merge divergent state"
                    )

                imported: list[ImportedTable] = []
                for table in _dependency_order(sqlite_conn, tables):
                    columns, primary_key = _sqlite_columns(sqlite_conn, table)
                    _, _, target_types = _pg_columns(pg_conn, table)
                    preserve_ledger_sequence = (
                        table == "wallet_transactions" and "ledger_sequence" in columns
                    )
                    if preserve_ledger_sequence:
                        # The 063 trigger correctly rejects caller-assigned sequences.
                        # Cutover is the sole exception: copy exact historical NULL/new
                        # sequence values while every other table guard remains active.
                        _set_wallet_ledger_trigger(pg_conn, enabled=False)
                    if table == "wallet_transactions":
                        # Archived input is not a new recharge: never manufacture funding
                        # lots or consumed revenue from historical CHARGE rows.
                        _set_wallet_ledger_trigger(
                            pg_conn, enabled=False, trigger="trg_billing_capture_charge"
                        )
                    source_rows = _insert_table_rows(
                        sqlite_conn,
                        pg_conn,
                        table,
                        columns,
                        primary_key,
                        target_types,
                        batch_size=batch_size,
                    )
                    if table == "wallet_transactions":
                        _set_wallet_ledger_trigger(
                            pg_conn, enabled=True, trigger="trg_billing_capture_charge"
                        )
                    if preserve_ledger_sequence:
                        _set_wallet_ledger_trigger(pg_conn, enabled=True)
                        _reset_wallet_ledger_sequence(pg_conn)
                    imported.append(ImportedTable(table=table, source_rows=source_rows))
                    if fail_after_table == table:
                        raise RuntimeError(f"injected failure after table {table}")
                _reset_sequences(pg_conn, tables)

                reconciliation = reconcile_connection_pair(sqlite_conn, pg_conn)
                if not reconciliation.ok:
                    raise MigrationReconciliationError(_issue_summary(reconciliation.issues))
                result = _result("imported", snapshot, imported, reconciliation)
            _write_report(report_path, result)
            return result


def import_sqlite_to_postgres(
    source_path: str | Path,
    postgres_dsn: str,
    *,
    maintenance_window_confirmed: bool = False,
    snapshot_path: str | Path | None = None,
    report_path: str | Path | None = None,
    batch_size: int = 1000,
    fail_after_table: str | None = None,
) -> ImportResult:
    require_maintenance_window(maintenance_window_confirmed)
    source = Path(source_path)
    snapshot = create_readonly_snapshot(source, snapshot_path or _default_snapshot_path(source))
    return migrate_snapshot(
        snapshot,
        postgres_dsn,
        report_path=report_path,
        batch_size=batch_size,
        fail_after_table=fail_after_table,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot SQLite to PostgreSQL migration")
    parser.add_argument("--sqlite", required=True, help="legacy SQLite database path")
    parser.add_argument(
        "--postgres-url",
        help="PostgreSQL DSN (never printed; prefer --postgres-url-env so the "
        "credential stays out of argv, ps listings and shell history)",
    )
    parser.add_argument(
        "--postgres-url-env",
        help="name of the environment variable holding the PostgreSQL DSN — "
        "the credential is read from the process environment, never passed "
        "on the command line (M1 review M4)",
    )
    parser.add_argument("--snapshot", help="explicit immutable snapshot output path")
    parser.add_argument("--report", help="optional JSON report path")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument(
        "--maintenance-window-confirmed",
        action="store_true",
        help="required acknowledgement that all SQLite writers are stopped",
    )
    args = parser.parse_args()

    if bool(args.postgres_url) == bool(args.postgres_url_env):
        parser.error("exactly one of --postgres-url / --postgres-url-env is required")
    postgres_url = args.postgres_url
    if args.postgres_url_env:
        postgres_url = os.environ.get(args.postgres_url_env)
        if not postgres_url:
            parser.exit(
                1,
                f"environment variable {args.postgres_url_env} is empty or unset; "
                "refusing to run without a PostgreSQL DSN\n",
            )

    try:
        result = import_sqlite_to_postgres(
            args.sqlite,
            postgres_url,
            maintenance_window_confirmed=args.maintenance_window_confirmed,
            snapshot_path=args.snapshot,
            report_path=args.report,
            batch_size=args.batch_size,
        )
    except Exception as error:
        parser.exit(1, safe_error_message(error, postgres_url, stage="migration") + "\n")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
