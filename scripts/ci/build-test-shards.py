#!/usr/bin/env python3
"""Deterministic pytest shard builder for the Linux quality gate.

Two responsibilities:

1. Parse a ``pytest --durations=0`` log into a per-file cost profile
   (``test-durations.json``). Durations are aggregated per test *file* by
   summing every call/setup/teardown entry whose node id lives in that file.

2. Split all discovered test files into N balanced shards using LPT (Longest
   Processing Time first) greedy bin-packing against that profile, then write
   ``shard-0.txt .. shard-(N-1).txt`` (one repo-relative path per line).

The output is fully deterministic: files are ordered by (-duration, path)
before packing, ties break to the lowest-index shard, and each shard manifest
is sorted alphabetically. Regenerating with the same inputs yields byte-identical
manifests, so the committed split is reviewable and reproducible.

The heavy PostgreSQL-touching files (e.g. test_queue_load_10k.py) naturally land
in different shards because LPT places the largest costs first, one per bin.

Usage:
    # Build the profile from a baseline log AND emit 4 shards:
    python3 scripts/ci/build-test-shards.py --from-log /tmp/ci-baseline.log --shards 4

    # Re-emit shards from an existing committed profile (no log needed):
    python3 scripts/ci/build-test-shards.py --shards 4

    # Fail-closed coverage guard (CI-7 / SH-6): assert the committed manifests
    # cover every discovered test file; exit 1 on any gap or ghost entry:
    python3 scripts/ci/build-test-shards.py --check-coverage
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TESTS_ROOT = REPO_ROOT / "server" / "tests"
DEFAULT_DURATIONS = REPO_ROOT / "scripts" / "ci" / "test-durations.json"
DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "ci" / "test-shards"

# pytest --durations line: "<seconds>s <phase> <nodeid>"; phase is one of the
# three below and the node id always carries a "::". Strong enough signal to
# avoid matching unrelated test stdout.
DURATION_LINE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)s\s+(call|setup|teardown)\s+(\S+::\S.*)$")

# Cost assumed for a discovered file that has no baseline entry yet (new tests).
# Deliberately small so unknown files do not dominate a shard; refresh the
# baseline periodically to keep the profile honest.
DEFAULT_ESTIMATE_SECONDS = 1.0


def parse_durations_log(log_path: Path) -> dict[str, float]:
    """Aggregate per-file seconds from a pytest --durations=0 log."""
    per_file: dict[str, float] = {}
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = DURATION_LINE.match(line)
            if not m:
                continue
            seconds = float(m.group(1))
            nodeid = m.group(3).strip()
            file_part = nodeid.split("::", 1)[0]
            if not file_part:
                continue
            per_file[file_part] = per_file.get(file_part, 0.0) + seconds
    return per_file


def discover_test_files(tests_root: Path) -> list[str]:
    """All test_*.py under tests_root, as repo-relative posix paths, sorted."""
    files: list[str] = []
    for path in tests_root.rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        files.append(rel)
    return sorted(files)


def find_uncovered_tests(
    manifest_dir: Path, tests_root: Path
) -> tuple[list[str], list[str]]:
    """Compare the committed shard manifests against the discovered suite.

    Returns ``(uncovered, ghosts)`` where:

    * ``uncovered`` — discovered ``test_*.py`` files absent from every manifest
      (CI would silently skip them; the CW-044 §18.2 fail-open gap).
    * ``ghosts`` — manifest entries that are not discovered test files (stale or
      renamed tests left behind in a manifest).

    Both lists are sorted for deterministic, reviewable output. Manifest entries
    are compared verbatim (whitespace stripped) against ``discover_test_files``,
    so the same repo-relative posix spelling is used on both sides.
    """
    discovered = discover_test_files(tests_root)
    discovered_set = set(discovered)
    union: set[str] = set()
    for manifest in sorted(manifest_dir.glob("shard-*.txt")):
        for line in manifest.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if entry:
                union.add(entry)
    uncovered = sorted(discovered_set - union)
    ghosts = sorted(union - discovered_set)
    return uncovered, ghosts


def normalize_profile_keys(raw: dict[str, float], files: list[str]) -> dict[str, float]:
    """Reconcile durations keys with repo-relative test paths.

    pytest --durations prints node ids relative to --rootdir, so a log produced
    by ``pytest --rootdir server server/tests`` shows ``tests/test_x.py`` while
    the manifests (and the runner) use ``server/tests/test_x.py``. Map each raw
    key onto the unique discovered file whose path ends with it; drop keys whose
    file no longer exists.
    """
    fileset = set(files)
    normalized: dict[str, float] = {}
    for key, value in raw.items():
        if key in fileset:
            normalized[key] = value
            continue
        matches = [f for f in files if f.endswith("/" + key)]
        if matches:
            # Deterministic pick when ambiguous: the shortest (closest) path.
            matches.sort(key=lambda p: (len(p), p))
            normalized[matches[0]] = normalized.get(matches[0], 0.0) + value
    return normalized


def write_durations_profile(
    dest: Path, per_file: dict[str, float], source_label: str
) -> None:
    """Write the committed cost profile. No timestamp: keep regeneration stable."""
    payload = {
        "source": source_label,
        "default_estimate_seconds": DEFAULT_ESTIMATE_SECONDS,
        "files": {k: round(per_file[k], 3) for k in sorted(per_file)},
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_durations_profile(path: Path) -> tuple[dict[str, float], float]:
    if not path.exists():
        return {}, DEFAULT_ESTIMATE_SECONDS
    data = json.loads(path.read_text(encoding="utf-8"))
    files = {str(k): float(v) for k, v in data.get("files", {}).items()}
    default = float(data.get("default_estimate_seconds", DEFAULT_ESTIMATE_SECONDS))
    return files, default


def lpt_shard(
    files: list[str], durations: dict[str, float], default: float, n: int
) -> list[list[str]]:
    """LPT greedy: sort by (-cost, path), place each into the lightest shard."""
    def cost(f: str) -> float:
        return durations.get(f, default)

    ordered = sorted(files, key=lambda f: (-cost(f), f))
    shards: list[list[str]] = [[] for _ in range(n)]
    loads: list[float] = [0.0] * n
    for f in ordered:
        # Lowest-index shard among those with the minimal current load.
        target = min(range(n), key=lambda i: (loads[i], i))
        shards[target].append(f)
        loads[target] += cost(f)
    # Deterministic, reviewable manifests: alphabetical within each shard.
    return [sorted(s) for s in shards]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shards", type=int, default=4, help="number of shards N (default 4)")
    ap.add_argument("--tests-root", type=Path, default=DEFAULT_TESTS_ROOT, help="directory to discover test_*.py under")
    ap.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS, help="cost profile JSON path")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="where to write shard-*.txt")
    ap.add_argument("--from-log", type=Path, default=None, help="pytest --durations=0 log to (re)build the profile from")
    ap.add_argument("--source-label", default="pytest --durations=0 baseline (full suite)", help="provenance note stored in the profile")
    ap.add_argument("--emit-profile-only", action="store_true", help="only write the durations profile, do not build shards")
    ap.add_argument("--check-coverage", action="store_true", help="fail-closed guard: assert the manifests in --out-dir cover every test file under --tests-root; exit 1 on any gap or ghost (writes nothing)")
    args = ap.parse_args(argv)

    # --check-coverage is a read-only guard (CI-7 / SH-6): it never builds or
    # writes shards, so it short-circuits before the profile/discover flow and
    # does not require a valid --shards.
    if args.check_coverage:
        uncovered, ghosts = find_uncovered_tests(args.out_dir, args.tests_root)
        discovered_count = len(discover_test_files(args.tests_root))
        if uncovered or ghosts:
            print(
                f"ERROR: shard manifests under {args.out_dir} do not cover the "
                f"{discovered_count} discovered test file(s) under {args.tests_root}.",
                file=sys.stderr,
            )
            if uncovered:
                print(
                    f"  {len(uncovered)} test file(s) missing from every manifest "
                    f"(CI would silently skip them):",
                    file=sys.stderr,
                )
                for path in uncovered:
                    print(f"    - {path}", file=sys.stderr)
            if ghosts:
                print(
                    f"  {len(ghosts)} manifest entrie(s) that are not discovered "
                    f"test files (stale/renamed):",
                    file=sys.stderr,
                )
                for path in ghosts:
                    print(f"    + {path}", file=sys.stderr)
            print(
                "  Regenerate with: python3 scripts/ci/build-test-shards.py --shards 4",
                file=sys.stderr,
            )
            return 1
        print(
            f"==> coverage OK: {discovered_count} discovered test file(s) fully "
            f"covered by the manifests in {args.out_dir}."
        )
        return 0

    if args.shards < 1:
        print("ERROR: --shards must be >= 1", file=sys.stderr)
        return 2

    # Discover first: profile keys must be reconciled against the real,
    # repo-relative test paths (pytest --durations prints node ids relative to
    # --rootdir, e.g. "tests/test_x.py" for "server/tests/test_x.py").
    files = discover_test_files(args.tests_root)
    if not files:
        print(f"ERROR: no test files discovered under {args.tests_root}", file=sys.stderr)
        return 1

    # 1. Profile: rebuild from log when asked, else load the committed one.
    if args.from_log is not None:
        if not args.from_log.exists():
            print(f"ERROR: log not found: {args.from_log}", file=sys.stderr)
            return 2
        raw = parse_durations_log(args.from_log)
        if not raw:
            print(f"ERROR: no durations parsed from {args.from_log} (was it run with --durations=0?)", file=sys.stderr)
            return 1
        per_file = normalize_profile_keys(raw, files)
        # Round BEFORE both writing and packing so a later no-log rebuild (which
        # loads the rounded JSON) produces byte-identical shards. Rounding after
        # packing would let sub-millisecond differences flip LPT tie-breaks.
        per_file = {k: round(v, 3) for k, v in per_file.items()}
        write_durations_profile(args.durations, per_file, args.source_label)
        print(f"==> wrote cost profile: {args.durations} ({len(per_file)} files mapped)")
        durations, default = per_file, DEFAULT_ESTIMATE_SECONDS
    else:
        durations, default = load_durations_profile(args.durations)
        if not durations:
            print(f"WARNING: no cost profile at {args.durations}; every file uses the {default}s default estimate.", file=sys.stderr)

    if args.emit_profile_only:
        return 0

    # 2. Shard.
    n = min(args.shards, len(files))
    if n != args.shards:
        print(f"WARNING: only {len(files)} test files; reducing N {args.shards} -> {n}", file=sys.stderr)

    shards = lpt_shard(files, durations, default, n)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    # Remove stale manifests from a previous, larger N so the runner never
    # picks up an orphan shard-i.txt beyond the current count.
    for stale in args.out_dir.glob("shard-*.txt"):
        try:
            idx = int(stale.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if idx >= n:
            stale.unlink()

    def cost(f: str) -> float:
        return durations.get(f, default)

    loads = []
    for i, shard in enumerate(shards):
        dest = args.out_dir / f"shard-{i}.txt"
        dest.write_text("".join(f"{p}\n" for p in shard), encoding="utf-8")
        load = sum(cost(f) for f in shard)
        loads.append(load)
        print(f"    shard-{i}.txt: {len(shard):3d} files, ~{load:7.1f}s estimated")

    if loads:
        spread = (max(loads) - min(loads))
        print(f"==> wrote {n} shards to {args.out_dir} "
              f"(balance spread ~{spread:.1f}s, total ~{sum(loads):.1f}s, {len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
