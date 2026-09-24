"""Build the isolated, hash-locked historical SQLite operator package (CW-060).

PG-09: the one-shot historical tools (``scripts.sqlite_to_postgres``,
``scripts.reconcile_customer_billing``, backed by ``app.backup``/``app.db``)
must ship as an independent, version-bound operator artifact — never as part
of the customer API/Worker image. This builder assembles the minimal
transitive source closure into an output directory plus a deterministic
``manifest.json`` (file list + SHA-256) and ``manifest.sha256``:

- the packaged source tree is pinned to the exact repository commit it was
  built from (``source_tree.commit_sha``) and to the single frozen Alembic
  head of ``server/migrations`` (``database_head``);
- the build is reproducible: same sources -> byte-identical
  ``manifest.json`` (no timestamps, sorted keys, sorted file list);
- the manifest records the honest dirty state of the working tree so a
  package built from uncommitted sources can never masquerade as a clean
  release artifact.

Usage (repo root or any directory):

    python3 deploy/operator/build_operator_package.py --output /tmp/operator-pkg

Standard library only — no ``app`` imports, runnable with any Python 3.12+.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The complete registered source closure of the operator tool. Adding a file
# here is a reviewed decision (PG-09 allowlist); everything else stays out of
# the artifact so business modules cannot drift into the operator package.
PACKAGE_FILES: tuple[str, ...] = (
    "server/alembic.ini",
    "server/pyproject.toml",
    "server/uv.lock",
    "server/app/backup.py",
    "server/app/db.py",
    "server/app/db_pg.py",
    "server/scripts/__init__.py",
    "server/scripts/reconcile_customer_billing.py",
    "server/scripts/sqlite_to_postgres.py",
)

ENTRY_POINTS: tuple[str, ...] = (
    "python -m scripts.sqlite_to_postgres",
    "python -m scripts.reconcile_customer_billing",
)

MANIFEST_SCHEMA_VERSION = 1


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_level_str_assignments(tree: ast.AST, names: set[str]) -> dict[str, object]:
    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in names:
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    found[target.id] = "<unevaluable>"
    return found


def migration_head(migrations_dir: Path) -> str:
    """Compute the single Alembic head of the frozen migration chain.

    Reimplements the graph walk with the standard library so the builder has
    no runtime dependency on the server virtualenv. Fails closed on multiple
    heads, missing parents or declared branch labels (CW-056 froze a linear
    chain; the operator artifact binds to exactly one revision).
    """

    versions_dir = migrations_dir / "versions"
    revisions: dict[str, str] = {}
    parents: dict[str, object] = {}
    referenced: set[str] = set()
    for path in sorted(versions_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = _module_level_str_assignments(tree, {"revision", "down_revision", "branch_labels"})
        revision = found.get("revision")
        if not isinstance(revision, str):
            raise RuntimeError(f"migration {path.name} declares no string revision")
        if revision in revisions:
            raise RuntimeError(f"duplicate migration revision {revision!r}")
        revisions[revision] = path.name
        parents[revision] = found.get("down_revision")
        if found.get("branch_labels") not in (None,):
            raise RuntimeError(
                f"migration {path.name} declares branch_labels; "
                "the operator package binds to a linear single-head chain"
            )
    for revision, down in parents.items():
        candidates = down if isinstance(down, tuple) else (down,)
        for candidate in candidates:
            if candidate is None:
                continue
            if not isinstance(candidate, str):
                raise RuntimeError(
                    f"migration {revisions[revision]} has an unevaluable down_revision"
                )
            if candidate not in revisions:
                raise RuntimeError(
                    f"migration {revisions[revision]} references unknown parent {candidate!r}"
                )
            referenced.add(candidate)
    heads = sorted(set(revisions) - referenced)
    if len(heads) != 1:
        raise RuntimeError(
            f"expected exactly one migration head, found {len(heads)}: {heads}"
        )
    return heads[0]


def build_package(repo_root: Path, output: Path) -> Path:
    """Assemble the operator package into ``output`` and return its manifest."""

    if not repo_root.is_dir():
        raise RuntimeError(f"repo root does not exist: {repo_root}")

    commit_sha = _git(repo_root, "rev-parse", "HEAD")
    dirty = bool(_git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"))
    database_head = migration_head(repo_root / "server" / "migrations")

    copied: list[dict[str, object]] = []
    relative_candidates = sorted(PACKAGE_FILES)
    relative_candidates += sorted(
        f"server/migrations/versions/{path.name}"
        for path in (repo_root / "server" / "migrations" / "versions").glob("*.py")
    )
    relative_candidates.append("server/migrations/env.py")
    for relative in sorted(set(relative_candidates)):
        source = repo_root / relative
        if not source.is_file():
            raise RuntimeError(f"registered package file is missing: {relative}")
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        copied.append(
            {"path": relative, "sha256": sha256_of(source), "bytes": source.stat().st_size}
        )

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact": "video-replica-operator",
        "source_tree": {
            "commit_sha": commit_sha,
            "dirty": dirty,
        },
        "database_head": database_head,
        "entry_points": list(ENTRY_POINTS),
        "files": copied,
        "contract": {
            "target": "PostgreSQL only; the target DSN comes from the operator's "
            "own environment, never from the customer runtime",
            "maintenance_window": "requires a confirmed maintenance window; the "
            "sealed original SQLite file must hash-unchanged survive the run",
            "isolation": "not part of the customer API/Worker image (PG-09); "
            "entry points are the two modules above and nothing else",
        },
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "manifest.sha256").write_text(
        f"{sha256_of(manifest_path)}  manifest.json\n", encoding="utf-8"
    )
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the isolated historical-SQLite operator package (CW-060/PG-09)."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root (default: derived from this script's location)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="output directory (created; must not already exist)",
    )
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise RuntimeError(f"output directory already exists: {args.output}")
        manifest_path = build_package(args.repo_root.resolve(), args.output.resolve())
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"operator package built: {args.output} ({manifest_path.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
