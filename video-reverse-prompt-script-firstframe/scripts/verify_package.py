#!/usr/bin/env python3
"""Verify that a V0.6 ZIP exactly mirrors the releasable Skill directory."""

import argparse
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict


# verification/ and docs/ are development evidence: they stay in the repository for
# CI and review, but shipping them would make the installable package ~140x larger.
EXCLUDED_PARTS = {"__pycache__", "video_reverse_runs", "verification", "docs"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
EXCLUDED_NAMES = {".DS_Store"}
ALLOWED_ROOT_FILES = {"SKILL.md"}
ALLOWED_ROOT_DIRS = {"agents", "references", "scripts", "templates", "tests"}


class PackageValidationError(RuntimeError):
    pass


def _is_releasable(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return (
        not EXCLUDED_PARTS.intersection(relative.parts)
        and path.suffix not in EXCLUDED_SUFFIXES
        and path.name not in EXCLUDED_NAMES
    )


def _expected_files(skill_dir: Path) -> Dict[str, bytes]:
    expected = {}
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or not _is_releasable(path, skill_dir):
            continue
        relative = path.relative_to(skill_dir)
        allowed = (
            len(relative.parts) == 1 and relative.name in ALLOWED_ROOT_FILES
        ) or (
            len(relative.parts) > 1 and relative.parts[0] in ALLOWED_ROOT_DIRS
        )
        if not allowed:
            raise PackageValidationError(
                "unsupported Skill file: %s" % relative.as_posix()
            )
        expected[relative.as_posix()] = path.read_bytes()
    return expected


def validate_package(skill_dir: Path, archive: Path) -> Dict[str, int]:
    skill_dir = Path(skill_dir)
    archive = Path(archive)
    if not skill_dir.is_dir():
        raise PackageValidationError("Skill directory is missing")
    if not archive.is_file():
        raise PackageValidationError("ZIP archive is missing")

    expected = _expected_files(skill_dir)
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = [name for name in bundle.namelist() if not name.endswith("/")]
            if len(names) != len(set(names)):
                raise PackageValidationError("ZIP contains duplicate file entries")
            paths = [PurePosixPath(name) for name in names]
            if any(path.is_absolute() or ".." in path.parts for path in paths):
                raise PackageValidationError("ZIP contains an unsafe path")
            roots = {path.parts[0] for path in paths if path.parts}
            if len(roots) != 1 or any(len(path.parts) < 2 for path in paths):
                raise PackageValidationError("ZIP must contain one top-level Skill directory")
            archive_files = {
                PurePosixPath(*path.parts[1:]).as_posix(): name
                for path, name in zip(paths, names)
            }
            if set(archive_files) != set(expected):
                missing = sorted(set(expected).difference(archive_files))
                extra = sorted(set(archive_files).difference(expected))
                raise PackageValidationError(
                    "ZIP file list mismatch; missing=%s; extra=%s"
                    % (missing, extra)
                )
            for relative, content in expected.items():
                if bundle.read(archive_files[relative]) != content:
                    raise PackageValidationError("ZIP content is stale: %s" % relative)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageValidationError("invalid ZIP archive: %s" % exc)
    return {"files": len(expected)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        result = validate_package(args.skill_dir, args.archive)
    except PackageValidationError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print("PASS: %s files match exactly" % result["files"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
