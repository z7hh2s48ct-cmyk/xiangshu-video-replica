"""T38 PostgreSQL PITR and recovery-drill contracts.

The repository can prove the scripts and the 100-fact verifier without
pretending that a developer fixture is an off-site archive.  Actual archive
storage, restore hardware, and the staging drill remain evidence-driven work.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from stat import S_IMODE

import pytest

from scripts.pitr_recovery_facts import (
    RecoveryFact,
    RecoveryManifestError,
    _connect,
    _normalized_wal_name,
    build_manifest,
    read_manifest,
    verify_manifest_facts,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _facts(count: int = 100) -> tuple[RecoveryFact, ...]:
    return tuple(
        RecoveryFact(
            activation_id=f"activation-{number:03d}",
            recharge_order_id=f"order-{number:03d}",
            charge_id=f"charge-{number:03d}",
            user_id=f"user-{number:03d}",
            session_epoch=number + 1,
        )
        for number in range(count)
    )


def test_recovery_manifest_requires_exactly_one_hundred_recovered_facts() -> None:
    facts = _facts()
    manifest = build_manifest(
        facts,
        recovery_target_time="2026-08-26T05:30:00+00:00",
        archived_wal="0000000100000000000000A1",
    )

    assert manifest["required_count"] == 100
    assert verify_manifest_facts(manifest, facts) == 100

    displaced_epoch = list(facts)
    displaced_epoch[-1] = RecoveryFact(
        activation_id="activation-099",
        recharge_order_id="order-099",
        charge_id="charge-099",
        user_id="user-099",
        session_epoch=999,
    )
    with pytest.raises(RecoveryManifestError, match="session epoch"):
        verify_manifest_facts(manifest, tuple(displaced_epoch))


def test_recovery_manifest_records_the_post_base_backup_sample_boundary() -> None:
    manifest = build_manifest(
        _facts(),
        recovery_target_time="2026-08-26T05:30:00+00:00",
        archived_wal="0000000100000000000000A1",
        sample_not_before="2026-08-26T05:00:00+00:00",
    )

    assert manifest["sample_not_before"] == "2026-08-26T05:00:00+00:00"


def test_recovery_manifest_refuses_an_insufficient_cross_domain_sample() -> None:
    with pytest.raises(RecoveryManifestError, match="at least 100"):
        build_manifest(
            _facts(99),
            recovery_target_time="2026-08-26T05:30:00+00:00",
            archived_wal="0000000100000000000000A1",
        )


def test_recovery_manifest_refuses_null_identifiers_and_boolean_epochs() -> None:
    malformed_identifier = list(_facts())
    malformed_identifier[0] = RecoveryFact(
        activation_id=None,  # type: ignore[arg-type]
        recharge_order_id="order-000",
        charge_id="charge-000",
        user_id="user-000",
        session_epoch=1,
    )
    with pytest.raises(RecoveryManifestError, match="activation id"):
        build_manifest(
            malformed_identifier,
            recovery_target_time="2026-08-26T05:30:00+00:00",
            archived_wal="0000000100000000000000A1",
        )

    malformed_epoch = list(_facts())
    malformed_epoch[0] = RecoveryFact(
        activation_id="activation-000",
        recharge_order_id="order-000",
        charge_id="charge-000",
        user_id="user-000",
        session_epoch=True,  # type: ignore[arg-type]
    )
    with pytest.raises(RecoveryManifestError, match="session epoch"):
        build_manifest(
            malformed_epoch,
            recovery_target_time="2026-08-26T05:30:00+00:00",
            archived_wal="0000000100000000000000A1",
        )


def test_recovery_manifest_accepts_postgres_timeline_history_wal_names() -> None:
    assert _normalized_wal_name("00000002.history") == "00000002.history"


def test_recovery_manifest_is_0600_and_never_overwrites_a_prior_drill(
    tmp_path: Path,
) -> None:
    manifest = build_manifest(
        _facts(),
        recovery_target_time="2026-08-26T05:30:00+00:00",
        archived_wal="0000000100000000000000A1",
    )
    manifest_path = tmp_path / "staging-drill.json"

    write_manifest(manifest_path, manifest)

    if os.name != "nt":
        assert S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert read_manifest(manifest_path) == manifest
    with pytest.raises(RecoveryManifestError, match="already exists"):
        write_manifest(manifest_path, manifest)


def test_default_recovery_facts_connection_uses_the_dedicated_backup_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service_file = tmp_path / "backup.pg_service.conf"
    service_file.write_text("[pitr-backup]\n", encoding="utf-8")
    observed: list[str] = []

    def fake_connect(conninfo: str) -> object:
        observed.append(conninfo)
        return object()

    monkeypatch.setenv("VIDEO_REPLICA_PG_BACKUP_SERVICE_FILE", str(service_file))
    monkeypatch.setenv("VIDEO_REPLICA_PG_BACKUP_SERVICE", "pitr-backup")
    monkeypatch.setenv("PGSERVICEFILE", "/incorrect-service-file")
    monkeypatch.setenv("PGSERVICE", "incorrect-service")
    monkeypatch.setattr("scripts.pitr_recovery_facts.psycopg.connect", fake_connect)

    _connect(None)

    assert observed == [""]
    assert os.environ["PGSERVICEFILE"] == str(service_file)
    assert os.environ["PGSERVICE"] == "pitr-backup"


def test_pitr_shell_entry_points_are_committed_as_executables() -> None:
    artifacts = (
        "deploy/postgres/pitr-preflight.sh",
        "deploy/postgres/pitr-backup.sh",
        "deploy/postgres/pitr-fetch-wal.sh",
        "deploy/postgres/pitr-restore-drill.sh",
    )
    completed = subprocess.run(
        ["git", "ls-files", "--stage", "--", *artifacts],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    modes = [line.split(maxsplit=1)[0] for line in completed.stdout.splitlines()]

    assert modes == ["100755"] * len(artifacts)


def test_t38_uses_physical_base_backups_and_continuous_wal_not_pg_dump() -> None:
    preflight = _read("deploy/postgres/pitr-preflight.sh")
    backup = _read("deploy/postgres/pitr-backup.sh")
    restore = _read("deploy/postgres/pitr-restore-drill.sh")
    fetch_wal = _read("deploy/postgres/pitr-fetch-wal.sh")
    scripts = "\n".join((preflight, backup, restore, fetch_wal))

    assert "pg_basebackup" in backup
    assert "--wal-method=stream" in backup
    assert "pg_verifybackup" in backup
    assert "wal_level" in preflight
    assert "archive_mode" in preflight
    assert "archive_command" in preflight
    assert "archive_library" in preflight
    assert "pg_switch_wal" in preflight
    assert "assert-wal" in preflight
    assert "put-base" in backup
    assert "assert-base" in backup
    assert "get-base" in restore
    assert "recovery.signal" in restore
    assert "restore_command" in restore
    assert "recovery_target_time" in restore
    assert "pg_ctl" in restore
    assert "get-wal" in fetch_wal
    assert "pg_dump" not in scripts


def test_t38_recovery_drill_is_staging_only_and_has_a_dedicated_backup_identity() -> None:
    preflight = _read("deploy/postgres/pitr-preflight.sh")
    restore = _read("deploy/postgres/pitr-restore-drill.sh")
    service = _read("deploy/systemd/video-replica-pitr-backup.service")
    timer = _read("deploy/systemd/video-replica-pitr-backup.timer")
    environment = _read("deploy/customer.env.example")

    assert "VIDEO_REPLICA_PG_BACKUP_SERVICE_FILE" in preflight
    assert "VIDEO_REPLICA_PG_BACKUP_SERVICE" in preflight
    assert "VIDEO_REPLICA_DATABASE_URL:?" not in preflight
    assert "VIDEO_REPLICA_PITR_DRILL_ENV=staging" in restore
    assert "VIDEO_REPLICA_PITR_DRILL_CONFIRM=RESTORE_SYNTHETIC_STAGING_DATA" in restore
    assert "EnvironmentFile=/etc/video-replica/pitr.env" in service
    assert "EnvironmentFile=/etc/video-replica/customer.env" not in service
    assert "pitr-backup.sh" in service
    assert "OnCalendar=" in timer
    assert "VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER" in environment
    assert "VIDEO_REPLICA_PG_BACKUP_SERVICE_FILE" in environment
