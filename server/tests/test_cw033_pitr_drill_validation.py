"""CW-033 pre-GA rehearsal: PITR restore-drill fail-fast validation surface.

T38 shipped the drill script and its structural contract tests
(``test_customer_pitr.py``); those tests grep the script body but never invoke
it, so a regression in the argument/env validation order would stay silent.

CW-033 is the "execute the drill" work package.  The full staging drill
(``pg_ctl`` + real base backup + WAL archive + 100-fact verify) is
GA-trigger-frozen per ``CW001-RELEASE-BASELINE.md`` §5.5 and
``CW005-DATA-DISPOSITION.md`` §7 — pre-GA has no real customer data and no
production archive, so any local rehearsal must stop before ``pitr_preflight``
touches ``psql``/``pg_basebackup``/``pg_verifybackup``.

These tests exercise the ~10 fail-fast paths that fire *before* that boundary:
CLI arg parsing, drill-env gating (``VIDEO_REPLICA_PITR_DRILL_ENV`` /
``_CONFIRM``), and recovery-root presence checks.  Every case asserts the
script exits non-zero with the documented stderr substring and creates no
side effects on disk.  Nothing here claims ``STAGING_VERIFIED`` — see
``docs/evidence/CW033-EVIDENCE.md`` for the honest scope boundary.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRILL_SCRIPT = REPO_ROOT / "deploy" / "postgres" / "pitr-restore-drill.sh"


def _drill_env(**overrides: str) -> dict[str, str]:
    """Return a minimal env for the drill script.

    Only ``PATH`` (needed to locate ``bash``) and ``HOME`` (some PG tooling
    reads it) are inherited; every ``VIDEO_REPLICA_*`` variable is stripped so
    a developer's shell profile cannot accidentally satisfy a required-var
    check.  Callers add the specific vars their case needs via ``overrides``.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    env.update(overrides)
    return env


def _run_drill(
    args: list[str],
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Invoke the drill script and capture stdout/stderr as text."""
    return subprocess.run(  # noqa: S603 - fixed absolute script path, no shell
        ["/usr/bin/env", "bash", str(DRILL_SCRIPT), *args],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _staging_gates() -> dict[str, str]:
    """Env vars that must be set correctly to reach the recovery-root checks."""
    return {
        "VIDEO_REPLICA_PITR_DRILL_ENV": "staging",
        "VIDEO_REPLICA_PITR_DRILL_CONFIRM": "RESTORE_SYNTHETIC_STAGING_DATA",
    }


def test_drill_script_is_present_and_executable() -> None:
    """Anchor: the artifact under test exists at the frozen path."""
    assert DRILL_SCRIPT.is_file(), f"missing drill script at {DRILL_SCRIPT}"
    assert os.access(DRILL_SCRIPT, os.X_OK), "drill script must be executable"


@pytest.mark.parametrize(
    ("args", "expected_stderr"),
    [
        pytest.param(
            ["--label", "ab", "--manifest", "/tmp/does-not-matter.json"],
            "PITR backup label is invalid",
            id="label-too-short",
        ),
        pytest.param(
            ["--label", "../escape", "--manifest", "/tmp/does-not-matter.json"],
            "PITR backup label is invalid",
            id="label-path-traversal",
        ),
        pytest.param(
            ["--label", "has space", "--manifest", "/tmp/does-not-matter.json"],
            "PITR backup label is invalid",
            id="label-contains-space",
        ),
        pytest.param(
            ["--label", "lead$pecial", "--manifest", "/tmp/does-not-matter.json"],
            "PITR backup label is invalid",
            id="label-contains-dollar",
        ),
    ],
)
def test_invalid_backup_label_fails_before_touching_env(
    args: list[str],
    expected_stderr: str,
) -> None:
    """CLI label regex fires first — no env, no PG, no filesystem writes."""
    completed = _run_drill(args, _drill_env())

    assert completed.returncode == 64, completed.stderr
    assert expected_stderr in completed.stderr


def test_missing_manifest_argument_is_rejected() -> None:
    """A well-formed label with no --manifest at all fails on the empty path."""
    completed = _run_drill(["--label", "cw033-dry-run"], _drill_env())

    assert completed.returncode == 64, completed.stderr
    assert "recovery manifest must be an existing absolute path" in completed.stderr


def test_relative_manifest_path_is_rejected(tmp_path: Path) -> None:
    """Manifest must be an absolute path even if the file exists relatively."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", "manifest.json"],
        _drill_env(),
        # Ensure the CWD is not tmp_path — the script must reject relative paths
        # regardless of where they resolve.
    )

    assert completed.returncode == 64, completed.stderr
    assert "recovery manifest must be an existing absolute path" in completed.stderr


def test_absolute_but_nonexistent_manifest_is_rejected(tmp_path: Path) -> None:
    """Absolute path alone is not enough — the file must exist."""
    missing = tmp_path / "not-created.json"

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(missing)],
        _drill_env(),
    )

    assert completed.returncode == 64, completed.stderr
    assert "recovery manifest must be an existing absolute path" in completed.stderr


@pytest.mark.parametrize(
    ("port", "expected_stderr"),
    [
        pytest.param("abc", "recovery port is invalid", id="port-non-numeric"),
        pytest.param("0", "recovery port is invalid", id="port-zero"),
        pytest.param("-1", "recovery port is invalid", id="port-negative"),
        pytest.param("70000", "recovery port is invalid", id="port-above-max"),
        pytest.param("055432", "recovery port is invalid", id="port-leading-zero"),
    ],
)
def test_invalid_port_is_rejected(tmp_path: Path, port: str, expected_stderr: str) -> None:
    """Port regex + range check fires before env gating."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest), "--port", port],
        _drill_env(),
    )

    assert completed.returncode == 64, completed.stderr
    assert expected_stderr in completed.stderr


def test_unknown_cli_flag_prints_usage_and_exits_64(tmp_path: Path) -> None:
    """An unrecognized flag routes through the usage() helper."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest), "--bogus"],
        _drill_env(),
    )

    assert completed.returncode == 64, completed.stderr
    assert "usage:" in completed.stderr


def test_missing_drill_env_var_refuses_to_run(tmp_path: Path) -> None:
    """VIDEO_REPLICA_PITR_DRILL_ENV is required — bash `:?` exits non-zero."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(),
    )

    # bash `:?` on unset var → exit 1 with the message on stderr.
    assert completed.returncode != 0
    assert "VIDEO_REPLICA_PITR_DRILL_ENV" in completed.stderr


def test_missing_drill_confirm_var_refuses_to_run(tmp_path: Path) -> None:
    """VIDEO_REPLICA_PITR_DRILL_CONFIRM is required even when ENV is set."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(VIDEO_REPLICA_PITR_DRILL_ENV="staging"),
    )

    assert completed.returncode != 0
    assert "VIDEO_REPLICA_PITR_DRILL_CONFIRM" in completed.stderr


def test_non_staging_drill_env_is_rejected(tmp_path: Path) -> None:
    """The drill refuses to run against anything but VIDEO_REPLICA_PITR_DRILL_ENV=staging."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(
            VIDEO_REPLICA_PITR_DRILL_ENV="production",
            VIDEO_REPLICA_PITR_DRILL_CONFIRM="RESTORE_SYNTHETIC_STAGING_DATA",
        ),
    )

    assert completed.returncode == 64, completed.stderr
    assert "may run only with VIDEO_REPLICA_PITR_DRILL_ENV=staging" in completed.stderr


def test_wrong_drill_confirm_string_is_rejected(tmp_path: Path) -> None:
    """The confirm token must match RESTORE_SYNTHETIC_STAGING_DATA exactly."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(
            VIDEO_REPLICA_PITR_DRILL_ENV="staging",
            VIDEO_REPLICA_PITR_DRILL_CONFIRM="yes-please-restore",
        ),
    )

    assert completed.returncode == 64, completed.stderr
    assert "RESTORE_SYNTHETIC_STAGING_DATA" in completed.stderr


def test_missing_recovery_root_var_is_rejected(tmp_path: Path) -> None:
    """Once staging gates pass, VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT is required."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(**_staging_gates()),
    )

    assert completed.returncode != 0
    assert "VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT" in completed.stderr


def test_relative_recovery_root_is_rejected(tmp_path: Path) -> None:
    """Recovery root must be an absolute existing directory."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(
            **_staging_gates(),
            VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT="relative/recovery",
            VIDEO_REPLICA_PG_PITR_RECOVERY_DB="cw033_rehearsal",
            VIDEO_REPLICA_PG_PITR_RECOVERY_USER="cw033_rehearsal",
        ),
    )

    assert completed.returncode == 64, completed.stderr
    assert "PITR recovery root must be an existing absolute directory" in completed.stderr


def test_nonexistent_recovery_root_is_rejected(tmp_path: Path) -> None:
    """Absolute but missing directory also fails the pre-flight gate."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    missing_root = tmp_path / "recovery-not-created"

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(
            **_staging_gates(),
            VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT=str(missing_root),
            VIDEO_REPLICA_PG_PITR_RECOVERY_DB="cw033_rehearsal",
            VIDEO_REPLICA_PG_PITR_RECOVERY_USER="cw033_rehearsal",
        ),
    )

    assert completed.returncode == 64, completed.stderr
    assert "PITR recovery root must be an existing absolute directory" in completed.stderr
    # Fail-fast side effect check: the script must not create the missing root.
    assert not missing_root.exists()


def test_invalid_recovery_db_name_is_rejected(tmp_path: Path) -> None:
    """Recovery DB name must match ^[A-Za-z0-9_]+$ (no dashes, no dots)."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    recovery_root = tmp_path / "recovery-root"
    recovery_root.mkdir()

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(
            **_staging_gates(),
            VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT=str(recovery_root),
            VIDEO_REPLICA_PG_PITR_RECOVERY_DB="has-dash.and.dot",
            VIDEO_REPLICA_PG_PITR_RECOVERY_USER="cw033_rehearsal",
        ),
    )

    assert completed.returncode == 64, completed.stderr
    assert "recovery database name is invalid" in completed.stderr
    # No side effect under the recovery root either.
    assert list(recovery_root.iterdir()) == []


def test_invalid_recovery_user_is_rejected(tmp_path: Path) -> None:
    """Recovery role name follows the same conservative regex."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    recovery_root = tmp_path / "recovery-root"
    recovery_root.mkdir()

    completed = _run_drill(
        ["--label", "cw033-dry-run", "--manifest", str(manifest)],
        _drill_env(
            **_staging_gates(),
            VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT=str(recovery_root),
            VIDEO_REPLICA_PG_PITR_RECOVERY_DB="cw033_rehearsal",
            VIDEO_REPLICA_PG_PITR_RECOVERY_USER="bad;role",
        ),
    )

    assert completed.returncode == 64, completed.stderr
    assert "recovery role is invalid" in completed.stderr
    assert list(recovery_root.iterdir()) == []
