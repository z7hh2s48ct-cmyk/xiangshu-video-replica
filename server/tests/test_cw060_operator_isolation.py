"""CW-060 — the historical SQLite tooling is isolated behind a built artifact.

TEST-LOGIC (offline, no PostgreSQL needed). Four automated boundaries:

1. **Operator artifact** — ``deploy/operator/build_operator_package.py``
   assembles the registered transitive closure of the one-shot historical
   tools into a package whose ``manifest.json`` pins the repository commit,
   the single frozen Alembic head and the SHA-256 of every packaged file;
   building twice from the same tree is byte-identical (rebuildable, PG-09).
2. **Customer image** — ``deploy/customer-git-rollout.sh`` removes
   ``server/app/backup.py`` from the build context, fails closed when
   historical tooling leaks into it, and physically scans the built image.
3. **Reverse consumers** — every module importing the historical
   ``app.db`` / ``app.backup`` surfaces is registered; the set cannot grow
   silently, and CW-042 shrinks it deliberately.
4. **TEST-IMPORT/TEST-HISTORY gates** — the historical suites fail closed
   without PostgreSQL (hard gate, explicit opt-in skip) or are documented
   pure-static; they cannot fake-pass on a missing database.

The algorithms inside the historical tools are reused, not re-verified
here: their behaviour coverage lives in test_sqlite_to_postgres.py
(TEST-IMPORT) and test_migration_dialect_contract.py (TEST-HISTORY, AST).
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPO_ROOT / "deploy" / "operator" / "build_operator_package.py"
ROLLOUT = REPO_ROOT / "deploy" / "customer-git-rollout.sh"

# The registered PG-09 allowlist the builder assembles (kept in sync with
# PACKAGE_FILES in the builder; the builder is the single source of truth,
# this import-free mirror pins it).
_REGISTERED_PACKAGE_PREFIXES_OK = ("server/migrations/",)
_REGISTERED_PACKAGE_FILES = {
    "server/alembic.ini",
    "server/pyproject.toml",
    "server/uv.lock",
    "server/app/backup.py",
    "server/app/db.py",
    "server/app/db_pg.py",
    "server/scripts/__init__.py",
    "server/scripts/reconcile_customer_billing.py",
    "server/scripts/sqlite_to_postgres.py",
}

# Business modules that must never enter the operator artifact.
_FORBIDDEN_PACKAGE_MARKERS = (
    "app/main.py",
    "server/app/bootstrap.py",
    "app/routes",
    "app/settings.py",
    "/client/",
    ".env",
)

# Registered reverse consumers of the historical in-app SQLite surfaces
# (CW-060 inventory; CW-042 prunes the list when the lane exits).
# CW-042-b retired the SQLite business runtime: the eight runtime consumers
# were cut, and app.db survives only inside the CW-060 operator closure
# (app.backup imports connect_database for the historical backup tool).
_APP_DB_CONSUMERS = frozenset(
    {
        "server/app/backup.py",
    }
)
_APP_BACKUP_CONSUMERS = frozenset({"server/scripts/sqlite_to_postgres.py"})

_IMPORT_PATTERNS = ("from app.db import", "from app.backup import")


def _build_operator_package(output: Path) -> Path:
    """Build once into ``output``; return the manifest path."""

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--repo-root",
            str(REPO_ROOT),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    return output / "manifest.json"


def _relative_python_files_with(patterns: tuple[str, ...], roots: tuple[Path, ...]) -> set[str]:
    hits: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if any(pattern in text for pattern in patterns):
                hits.add(path.relative_to(REPO_ROOT).as_posix())
    return hits


def test_operator_package_is_reproducible(tmp_path: Path) -> None:
    """Two builds from the same tree are byte-identical, manifest included."""

    first = _build_operator_package(tmp_path / "first")
    second = _build_operator_package(tmp_path / "second")

    first_manifest = first.read_bytes()
    assert first_manifest == second.read_bytes()
    manifest = json.loads(first_manifest.decode("utf-8"))
    for entry in manifest["files"]:
        packaged_first = (tmp_path / "first" / entry["path"]).read_bytes()
        packaged_second = (tmp_path / "second" / entry["path"]).read_bytes()
        assert packaged_first == packaged_second, entry["path"]


def test_operator_manifest_binds_commit_and_single_head(tmp_path: Path) -> None:
    manifest = json.loads(_build_operator_package(tmp_path / "pkg").read_text(encoding="utf-8"))

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    assert manifest["source_tree"]["commit_sha"] == head
    assert isinstance(manifest["database_head"], str) and manifest["database_head"]

    declared = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    for relative, digest in declared.items():
        source = REPO_ROOT / relative
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        assert actual == digest, relative
    # The core operator closure is present and the head file is packaged.
    for required in (
        "server/scripts/sqlite_to_postgres.py",
        "server/scripts/reconcile_customer_billing.py",
        "server/app/backup.py",
        "server/app/db.py",
    ):
        assert required in declared, required
    head_file = REPO_ROOT / "server" / "migrations" / "versions"
    assert any(
        entry["path"].startswith("server/migrations/versions/") for entry in manifest["files"]
    ), head_file
    # manifest.sha256 pins the manifest itself.
    recorded = (tmp_path / "pkg" / "manifest.sha256").read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256((tmp_path / "pkg" / "manifest.json").read_bytes()).hexdigest() == recorded


def test_operator_package_contains_no_business_surface(tmp_path: Path) -> None:
    manifest = json.loads(_build_operator_package(tmp_path / "pkg").read_text(encoding="utf-8"))
    packaged_paths = {entry["path"] for entry in manifest["files"]}

    registered = set(_REGISTERED_PACKAGE_FILES) | {
        path for path in packaged_paths if path.startswith(_REGISTERED_PACKAGE_PREFIXES_OK)
    }
    assert packaged_paths <= registered, packaged_paths - registered
    for relative in packaged_paths:
        for marker in _FORBIDDEN_PACKAGE_MARKERS:
            assert marker not in relative, (relative, marker)


def test_customer_rollout_excludes_historical_tooling() -> None:
    rollout = ROLLOUT.read_text(encoding="utf-8")

    # Build context: backup.py removed after the app copy; the loop fails
    # closed on any historical path leaking into BUILD_CTX.
    assert 'rm -f "$BUILD_CTX/server/app/backup.py"' in rollout
    assert "server/app/backup.py server/scripts" in rollout
    assert "PRECHECK_FAILED: historical SQLite tooling" in rollout
    # Built image: physical scan for the operator entry files.
    assert "! test -e /opt/video-replica/server/app/backup.py" in rollout
    assert "! test -e /opt/video-replica/server/scripts/sqlite_to_postgres.py" in rollout
    assert "! test -e /opt/video-replica/server/scripts/reconcile_customer_billing.py" in rollout
    assert "historical SQLite tooling in the customer image" in rollout
    # The legacy scripts directory is not part of the customer build at all.
    assert "COPY server/scripts" not in rollout
    assert 'cp -a "$SOURCE/server/scripts"' not in rollout


def test_historical_sqlite_surface_consumers_are_registered() -> None:
    """Every importer of app.db / app.backup is on the CW-042 registry."""

    roots = (REPO_ROOT / "server" / "app", REPO_ROOT / "server" / "scripts")
    db_hits: set[str] = set()
    backup_hits: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            if "from app.db import" in text:
                db_hits.add(relative)
            if "from app.backup import" in text:
                backup_hits.add(relative)

    assert db_hits == set(_APP_DB_CONSUMERS), db_hits ^ set(_APP_DB_CONSUMERS)
    assert backup_hits == set(_APP_BACKUP_CONSUMERS), backup_hits ^ set(_APP_BACKUP_CONSUMERS)


def test_test_import_history_suites_fail_closed_without_pg() -> None:
    """The historical suites hard-fail on missing PG (no implicit skips)."""

    import_suite = (REPO_ROOT / "server" / "tests" / "test_sqlite_to_postgres.py").read_text(
        encoding="utf-8"
    )
    assert "require_pg_or_explicit_skip()" in import_suite
    assert "from pg_test_kit import" in import_suite

    dialect_suite = (
        REPO_ROOT / "server" / "tests" / "test_migration_dialect_contract.py"
    ).read_text(encoding="utf-8")
    # Registered as pure-static TEST-HISTORY: AST analysis that runs
    # everywhere, so there is no PG path to fake-pass on.
    assert "ast.parse" in dialect_suite
    assert "pytest.skip" not in dialect_suite
    assert "LEGACY_EXEMPTIONS" in dialect_suite


def test_builder_declares_no_app_imports() -> None:
    """The builder itself must stay outside the app import graph."""

    tree = ast.parse(BUILDER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] not in {"app", "scripts"} for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module.split(".")[0] not in {"app", "scripts"}
