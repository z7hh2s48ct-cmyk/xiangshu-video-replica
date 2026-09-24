"""CW-061 shard-coverage guard tests (TEST-LOGIC, fully offline).

CW-044 §18.2 found the CI Linux gate silently runs only the test files named in
the committed shard manifests: ``scripts/ci/run-pytest-shards.sh`` adopts the four
``scripts/ci/test-shards/shard-*.txt`` as soon as they exist, without ever
checking that their union covers the real ``server/tests/test_*.py`` set, and
``ci.yml`` has no full-pytest fallback (``check:static`` is the former
``npm run check`` minus its trailing full pytest). Thirteen recent CW test files
therefore never execute in CI, and the gap grows by one every time a new test
file lands.

These tests drive the fix:

* ``find_uncovered_tests()`` — the pure set logic (uncovered + ghost detection).
* ``build-test-shards.py --check-coverage`` — the fail-closed CLI exit codes.
* the committed manifests really do cover every discovered test (SH-5 regen).
* dropping one manifest entry is detected against the real suite (SH-6 accept).
* ``run-pytest-shards.sh`` + ``ci.yml`` wire the guard in (contract assertions
  live in ``test_build_contracts.py``).

No PostgreSQL and no Docker are required: everything is set logic, an in-process
CLI call, or a static file assertion.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SHARDS = REPO_ROOT / "scripts" / "ci" / "build-test-shards.py"
COMMITTED_SHARD_DIR = REPO_ROOT / "scripts" / "ci" / "test-shards"
TESTS_ROOT = REPO_ROOT / "server" / "tests"


def _load_build_shards():
    """Load scripts/ci/build-test-shards.py (hyphenated name -> importlib)."""
    spec = importlib.util.spec_from_file_location("build_test_shards_cw061", BUILD_SHARDS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bts = _load_build_shards()


def _make_tests(root: Path, names: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).write_text(
            "def test_placeholder() -> None:\n    assert True\n", encoding="utf-8"
        )


def _write_manifests(dirpath: Path, shards: list[list[str]]) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    for i, files in enumerate(shards):
        (dirpath / f"shard-{i}.txt").write_text("".join(f"{p}\n" for p in files), encoding="utf-8")


# ---------------------------------------------------------------------------
# find_uncovered_tests() — pure set logic
# ---------------------------------------------------------------------------
def test_find_uncovered_tests_detects_a_missing_file(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    _make_tests(tests_root, ["test_a.py", "test_b.py", "test_c.py"])
    discovered = bts.discover_test_files(tests_root)
    assert len(discovered) == 3

    manifest_dir = tmp_path / "shards"
    # Cover a and b; omit c (sorted order == a, b, c).
    _write_manifests(manifest_dir, [[discovered[0]], [discovered[1]]])

    uncovered, ghosts = bts.find_uncovered_tests(manifest_dir, tests_root)
    assert uncovered == [discovered[2]]
    assert ghosts == []


def test_find_uncovered_tests_complete_returns_empty(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    _make_tests(tests_root, ["test_a.py", "test_b.py"])
    discovered = bts.discover_test_files(tests_root)

    manifest_dir = tmp_path / "shards"
    _write_manifests(manifest_dir, [[discovered[0]], [discovered[1]]])

    uncovered, ghosts = bts.find_uncovered_tests(manifest_dir, tests_root)
    assert uncovered == []
    assert ghosts == []


def test_find_uncovered_tests_detects_a_ghost_entry(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    _make_tests(tests_root, ["test_a.py"])
    discovered = bts.discover_test_files(tests_root)

    # A manifest entry for a file that no longer exists (renamed/deleted test).
    ghost = (tests_root / "test_deleted.py").as_posix()
    manifest_dir = tmp_path / "shards"
    _write_manifests(manifest_dir, [[discovered[0], ghost]])

    uncovered, ghosts = bts.find_uncovered_tests(manifest_dir, tests_root)
    assert uncovered == []
    assert ghosts == [ghost]


# ---------------------------------------------------------------------------
# build-test-shards.py --check-coverage — fail-closed CLI exit codes
# ---------------------------------------------------------------------------
def test_check_coverage_cli_fails_on_a_gap(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    _make_tests(tests_root, ["test_a.py", "test_b.py"])
    discovered = bts.discover_test_files(tests_root)

    manifest_dir = tmp_path / "shards"
    _write_manifests(manifest_dir, [[discovered[0]]])  # omit b

    rc = bts.main(
        [
            "--check-coverage",
            "--tests-root",
            str(tests_root),
            "--out-dir",
            str(manifest_dir),
        ]
    )
    assert rc == 1


def test_check_coverage_cli_passes_when_complete(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    _make_tests(tests_root, ["test_a.py", "test_b.py"])
    discovered = bts.discover_test_files(tests_root)

    manifest_dir = tmp_path / "shards"
    _write_manifests(manifest_dir, [[discovered[0]], [discovered[1]]])

    rc = bts.main(
        [
            "--check-coverage",
            "--tests-root",
            str(tests_root),
            "--out-dir",
            str(manifest_dir),
        ]
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# Real-suite acceptance
# ---------------------------------------------------------------------------
def test_committed_manifests_cover_every_discovered_test() -> None:
    """SH-5: the committed manifests must cover the whole discovered suite.

    Before the fix this fails with the 13 stale-manifest files named in
    CW-044 §18.2 (plus this very test file, which is itself a new test the
    manifests never knew about). Regenerating with ``--shards 4`` clears it.
    """
    uncovered, ghosts = bts.find_uncovered_tests(COMMITTED_SHARD_DIR, TESTS_ROOT)
    assert uncovered == [], (
        f"{len(uncovered)} test file(s) missing from the committed shard manifests "
        f"(CI would silently skip them). Regenerate via "
        f"`python3 scripts/ci/build-test-shards.py --shards 4`: {uncovered}"
    )
    assert ghosts == [], (
        f"manifest entries that are not discovered test files (stale/renamed): {ghosts}"
    )


def test_dropping_one_manifest_entry_is_detected(tmp_path: Path) -> None:
    """SH-6 acceptance: mutate a complete manifest set -> the guard flags it.

    Builds a complete split of the *real* discovered suite in a temp dir, then
    removes one entry and asserts the guard reports exactly that file as
    uncovered. This is the mutation test the CW-044 §18.2 fix calls for.
    """
    discovered = bts.discover_test_files(TESTS_ROOT)
    assert discovered, "expected the real server/tests suite to be discoverable"

    shards: list[list[str]] = [[], [], [], []]
    for i, path in enumerate(discovered):
        shards[i % 4].append(path)

    manifest_dir = tmp_path / "shards"
    _write_manifests(manifest_dir, shards)

    # Sanity: the complete split has no gap.
    uncovered0, ghosts0 = bts.find_uncovered_tests(manifest_dir, TESTS_ROOT)
    assert uncovered0 == [] and ghosts0 == []

    # Mutate: drop the first entry of shard-0.
    victim = shards[0][0]
    shards[0] = shards[0][1:]
    _write_manifests(manifest_dir, shards)

    uncovered1, _ = bts.find_uncovered_tests(manifest_dir, TESTS_ROOT)
    assert uncovered1 == [victim]
