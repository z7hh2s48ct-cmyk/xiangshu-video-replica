"""Capture and verify the T38 cross-domain PITR recovery sample.

The manifest intentionally contains only the identifiers and session epoch
needed to prove that an activation, its first order/CHARGE, and current session
state survived recovery.  It must be stored beside the protected backup
artifacts, never checked into Git or emitted to logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

MANIFEST_VERSION = 1
DEFAULT_REQUIRED_COUNT = 100
_WAL_NAME = re.compile(r"^(?:[0-9A-F]{24}|[0-9A-F]{8}\.history)$")
_BACKUP_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

_SAMPLE_FACTS_QUERY_TEMPLATE = """
SELECT
    activation.id,
    recharge_order.id,
    charge.id,
    session_state.user_id,
    session_state.session_epoch
FROM activation_code_activations AS activation
JOIN recharge_orders AS recharge_order
  ON recharge_order.id = activation.recharge_order_id
JOIN wallet_transactions AS charge
  ON charge.recharge_order_id = recharge_order.id
 AND charge.type = 'CHARGE'
JOIN customer_session_state AS session_state
  ON session_state.user_id = activation.user_id
{sample_boundary}
ORDER BY activation.activated_at ASC, activation.id ASC
LIMIT %s
"""

_POST_BASE_SAMPLE_FACTS_QUERY = _SAMPLE_FACTS_QUERY_TEMPLATE.format(
    sample_boundary="WHERE activation.activated_at::timestamptz >= %s::timestamptz"
)

_FACT_BY_ACTIVATION_QUERY = """
SELECT
    activation.id,
    recharge_order.id,
    charge.id,
    session_state.user_id,
    session_state.session_epoch
FROM activation_code_activations AS activation
JOIN recharge_orders AS recharge_order
  ON recharge_order.id = activation.recharge_order_id
JOIN wallet_transactions AS charge
  ON charge.recharge_order_id = recharge_order.id
 AND charge.type = 'CHARGE'
JOIN customer_session_state AS session_state
  ON session_state.user_id = activation.user_id
WHERE activation.id = %s
"""


class RecoveryManifestError(RuntimeError):
    """The recovery sample is absent, malformed, or does not match."""


@dataclass(frozen=True)
class RecoveryFact:
    activation_id: str
    recharge_order_id: str
    charge_id: str
    user_id: str
    session_epoch: int


def _require_non_blank(value: object, field: str) -> str:
    if value is None:
        raise RecoveryManifestError(f"recovery fact {field} must not be blank")
    normalized = str(value).strip()
    if not normalized:
        raise RecoveryManifestError(f"recovery fact {field} must not be blank")
    return normalized


def _normalize_fact(value: RecoveryFact) -> RecoveryFact:
    if isinstance(value.session_epoch, bool):
        raise RecoveryManifestError("recovery fact session epoch must be an integer")
    try:
        epoch = int(value.session_epoch)
    except (TypeError, ValueError) as error:
        raise RecoveryManifestError("recovery fact session epoch must be an integer") from error
    if epoch < 1:
        raise RecoveryManifestError("recovery fact session epoch must be positive")
    return RecoveryFact(
        activation_id=_require_non_blank(value.activation_id, "activation id"),
        recharge_order_id=_require_non_blank(value.recharge_order_id, "recharge order id"),
        charge_id=_require_non_blank(value.charge_id, "charge id"),
        user_id=_require_non_blank(value.user_id, "user id"),
        session_epoch=epoch,
    )


def _normalize_target_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryManifestError("recovery target time must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise RecoveryManifestError("recovery target time must include an offset")
    return parsed.astimezone(UTC).isoformat()


def _normalized_wal_name(value: str) -> str:
    wal_name = _require_non_blank(value, "archived WAL")
    if not _WAL_NAME.fullmatch(wal_name):
        raise RecoveryManifestError("archived WAL name is invalid")
    return wal_name


def _facts_digest(facts: Sequence[RecoveryFact]) -> str:
    canonical = json.dumps(
        [asdict(fact) for fact in facts],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_manifest(
    facts: Sequence[RecoveryFact],
    *,
    recovery_target_time: str,
    archived_wal: str,
    required_count: int = DEFAULT_REQUIRED_COUNT,
    sample_not_before: str | None = None,
) -> dict[str, object]:
    """Build a deterministic, 0600-only recovery manifest payload."""
    if required_count < DEFAULT_REQUIRED_COUNT:
        raise RecoveryManifestError("recovery manifest must require at least 100 facts")
    normalized_facts = tuple(
        sorted((_normalize_fact(fact) for fact in facts), key=lambda fact: fact.activation_id)
    )
    if len(normalized_facts) < required_count:
        raise RecoveryManifestError(f"recovery manifest requires at least {required_count} facts")
    if len(normalized_facts) != required_count:
        raise RecoveryManifestError(
            "recovery manifest must contain exactly its required fact count"
        )
    if len({fact.activation_id for fact in normalized_facts}) != required_count:
        raise RecoveryManifestError("recovery manifest activation identifiers must be unique")
    if len({fact.recharge_order_id for fact in normalized_facts}) != required_count:
        raise RecoveryManifestError("recovery manifest recharge order identifiers must be unique")
    if len({fact.charge_id for fact in normalized_facts}) != required_count:
        raise RecoveryManifestError("recovery manifest charge identifiers must be unique")

    normalized_target_time = _normalize_target_time(recovery_target_time)
    manifest: dict[str, object] = {
        "version": MANIFEST_VERSION,
        "required_count": required_count,
        "recovery_target_time": normalized_target_time,
        "archived_wal": _normalized_wal_name(archived_wal),
        "facts": [asdict(fact) for fact in normalized_facts],
        "facts_sha256": _facts_digest(normalized_facts),
    }
    if sample_not_before is not None:
        normalized_sample_boundary = _normalize_target_time(sample_not_before)
        if normalized_sample_boundary > normalized_target_time:
            raise RecoveryManifestError("recovery sample boundary is after its target time")
        manifest["sample_not_before"] = normalized_sample_boundary
    return manifest


def _facts_from_manifest(manifest: dict[str, object]) -> tuple[RecoveryFact, ...]:
    if manifest.get("version") != MANIFEST_VERSION:
        raise RecoveryManifestError("recovery manifest version is unsupported")
    required_count = manifest.get("required_count")
    if not isinstance(required_count, int) or required_count < DEFAULT_REQUIRED_COUNT:
        raise RecoveryManifestError("recovery manifest required count is invalid")
    target_time = manifest.get("recovery_target_time")
    if not isinstance(target_time, str):
        raise RecoveryManifestError("recovery manifest target time is missing")
    _normalize_target_time(target_time)
    sample_not_before = manifest.get("sample_not_before")
    if sample_not_before is not None:
        if not isinstance(sample_not_before, str):
            raise RecoveryManifestError("recovery manifest sample boundary is invalid")
        if _normalize_target_time(sample_not_before) > _normalize_target_time(target_time):
            raise RecoveryManifestError(
                "recovery manifest sample boundary is after its target time"
            )
    archived_wal = manifest.get("archived_wal")
    if not isinstance(archived_wal, str):
        raise RecoveryManifestError("recovery manifest archived WAL is missing")
    _normalized_wal_name(archived_wal)
    raw_facts = manifest.get("facts")
    if not isinstance(raw_facts, list):
        raise RecoveryManifestError("recovery manifest facts are missing")
    try:
        facts = tuple(_normalize_fact(RecoveryFact(**fact)) for fact in raw_facts)
    except (TypeError, KeyError) as error:
        raise RecoveryManifestError("recovery manifest fact shape is invalid") from error
    normalized = build_manifest(
        facts,
        recovery_target_time=target_time,
        archived_wal=archived_wal,
        required_count=required_count,
    )
    digest = manifest.get("facts_sha256")
    if not isinstance(digest, str) or digest != normalized["facts_sha256"]:
        raise RecoveryManifestError("recovery manifest fingerprint does not match its facts")
    return tuple(sorted(facts, key=lambda fact: fact.activation_id))


def verify_manifest_facts(
    manifest: dict[str, object], recovered_facts: Sequence[RecoveryFact]
) -> int:
    """Fail closed unless every recorded activation/order/CHARGE/epoch matches."""
    expected = _facts_from_manifest(manifest)
    recovered = {
        _normalize_fact(fact).activation_id: _normalize_fact(fact) for fact in recovered_facts
    }
    if len(recovered) != len(recovered_facts):
        raise RecoveryManifestError("recovered activation identifiers are duplicated")
    for fact in expected:
        actual = recovered.get(fact.activation_id)
        if actual is None:
            raise RecoveryManifestError("recovered activation fact is missing")
        if actual.session_epoch != fact.session_epoch:
            raise RecoveryManifestError("recovered session epoch does not match")
        if actual != fact:
            raise RecoveryManifestError("recovered activation/order/charge fact does not match")
    if len(recovered) != len(expected):
        raise RecoveryManifestError("recovery verifier received an unexpected fact count")
    return len(expected)


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    """Atomically publish a root/backup-user-only manifest without overwriting it."""
    _facts_from_manifest(manifest)
    if not path.parent.is_dir():
        raise RecoveryManifestError("recovery manifest parent directory does not exist")
    if path.exists():
        raise RecoveryManifestError("recovery manifest already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            raise RecoveryManifestError("recovery manifest already exists") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def read_manifest(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise RecoveryManifestError("recovery manifest cannot be read") from error
    if not isinstance(manifest, dict):
        raise RecoveryManifestError("recovery manifest root must be an object")
    _facts_from_manifest(manifest)
    return manifest


def _row_to_fact(row: Sequence[object]) -> RecoveryFact:
    return RecoveryFact(
        activation_id=str(row[0]),
        recharge_order_id=str(row[1]),
        charge_id=str(row[2]),
        user_id=str(row[3]),
        session_epoch=int(str(row[4])),
    )


def capture_manifest(
    connection: psycopg.Connection[Any], *, required_count: int, sample_not_before: str
) -> dict[str, object]:
    normalized_sample_boundary = _normalize_target_time(sample_not_before)
    with connection.cursor() as cursor:
        cursor.execute(_POST_BASE_SAMPLE_FACTS_QUERY, (normalized_sample_boundary, required_count))
        facts = tuple(_row_to_fact(row) for row in cursor.fetchall())
        cursor.execute("SELECT clock_timestamp()")
        target_row = cursor.fetchone()
        cursor.execute("SELECT pg_walfile_name(pg_switch_wal())")
        wal_row = cursor.fetchone()
    if target_row is None or wal_row is None:
        raise RecoveryManifestError("PostgreSQL did not return a recovery target")
    target_time = target_row[0]
    archived_wal = wal_row[0]
    if not isinstance(target_time, datetime) or not isinstance(archived_wal, str):
        raise RecoveryManifestError("PostgreSQL did not return a recovery target")
    return build_manifest(
        facts,
        recovery_target_time=target_time.astimezone(UTC).isoformat(),
        archived_wal=archived_wal,
        required_count=required_count,
        sample_not_before=normalized_sample_boundary,
    )


def verify_recovered_database(
    connection: psycopg.Connection[Any], manifest: dict[str, object]
) -> int:
    expected = _facts_from_manifest(manifest)
    with connection.cursor() as cursor:
        recovered: list[RecoveryFact] = []
        for fact in expected:
            cursor.execute(_FACT_BY_ACTIVATION_QUERY, (fact.activation_id,))
            row = cursor.fetchone()
            if row is not None:
                recovered.append(_row_to_fact(row))
    return verify_manifest_facts(manifest, tuple(recovered))


def _configure_backup_service_connection() -> None:
    """Make an implicit connection use the dedicated, protected libpq service."""
    service_file_value = os.environ.get("VIDEO_REPLICA_PG_BACKUP_SERVICE_FILE", "")
    service_name = os.environ.get("VIDEO_REPLICA_PG_BACKUP_SERVICE", "")
    service_file = Path(service_file_value)
    if not service_file_value or not service_file.is_absolute() or not service_file.is_file():
        raise RecoveryManifestError("protected backup libpq service file is unavailable")
    if not _BACKUP_SERVICE_NAME.fullmatch(service_name):
        raise RecoveryManifestError("protected backup libpq service name is invalid")

    # `pitr-preflight.sh` is a separate process, so its exports cannot configure
    # this CLI invocation. Override any inherited libpq defaults to preserve the
    # dedicated backup identity without placing credentials in argv or the manifest.
    os.environ["PGSERVICEFILE"] = str(service_file)
    os.environ["PGSERVICE"] = service_name


def _connect(conninfo: str | None) -> psycopg.Connection[Any]:
    if not conninfo:
        _configure_backup_service_connection()
    return psycopg.connect(conninfo or "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="capture or verify T38 PITR recovery facts")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "verify"):
        command = subcommands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument(
            "--conninfo", help="credential-free recovery conninfo or libpq service"
        )
        if name == "capture":
            command.add_argument("--required-count", type=int, default=DEFAULT_REQUIRED_COUNT)
            command.add_argument(
                "--not-before",
                required=True,
                help="UTC timestamp immediately after the selected base backup completed",
            )
    target = subcommands.add_parser("target")
    target.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "target":
            manifest = read_manifest(args.manifest)
            print(manifest["recovery_target_time"])
            return 0
        if args.command == "capture":
            with _connect(args.conninfo) as connection:
                manifest = capture_manifest(
                    connection,
                    required_count=args.required_count,
                    sample_not_before=args.not_before,
                )
            write_manifest(args.manifest, manifest)
            print(
                json.dumps(
                    {
                        "captured_recovery_facts": manifest["required_count"],
                        "facts_sha256": manifest["facts_sha256"],
                        "archived_wal": manifest["archived_wal"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        manifest = read_manifest(args.manifest)
        with _connect(args.conninfo) as connection:
            verified = verify_recovered_database(connection, manifest)
        print(
            json.dumps(
                {"verified_recovery_facts": verified, "facts_sha256": manifest["facts_sha256"]}
            )
        )
        return 0
    except (OSError, psycopg.Error, RecoveryManifestError):
        print("PITR recovery fact operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
