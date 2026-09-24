from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

import app.gate1_e2e as gate1_e2e
from app.gate1_e2e import (
    ApiRestartController,
    ManagedProcesses,
    generate_test_media,
    prepare_gate1_run,
    verify_evidence_manifest,
    wait_for_http,
    write_evidence_manifest,
)


def _isolated_child_env() -> dict[str, str]:
    # A Windows child that opens sockets dies with Winsock WinError 10106
    # when SystemRoot is missing from its environment, so keep the platform
    # minimum. Linux still needs the loader path that setup-python wires up
    # for the runner's CPython, otherwise sys.executable cannot start.
    if sys.platform == "win32":
        return {"SystemRoot": os.environ["SystemRoot"]}
    environment: dict[str, str] = {}
    ld_library_path = os.environ.get("LD_LIBRARY_PATH")
    if ld_library_path:
        environment["LD_LIBRARY_PATH"] = ld_library_path
    return environment


def test_isolated_child_env_preserves_linux_loader_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    loader_path = "/opt/actions-runner-linux/_work/_tool/Python/3.12.14/x64/lib"
    monkeypatch.setenv("LD_LIBRARY_PATH", loader_path)

    environment = _isolated_child_env()

    assert environment == {"LD_LIBRARY_PATH": loader_path}


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_prepare_gate1_run_creates_isolated_evidence_directories(tmp_path: Path) -> None:
    paths = prepare_gate1_run(tmp_path, run_id="gate1-test-run")

    assert paths.run_dir == tmp_path / "gate1-test-run"
    assert paths.runtime_dir.is_dir()
    assert paths.media_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.screenshots_dir.is_dir()
    assert paths.downloads_dir.is_dir()
    assert paths.browser_dir.is_dir()

    with pytest.raises(FileExistsError):
        prepare_gate1_run(tmp_path, run_id="gate1-test-run")

    with pytest.raises(ValueError, match="run_id"):
        prepare_gate1_run(tmp_path, run_id="../outside")


def test_write_evidence_manifest_hashes_evidence_but_excludes_runtime(tmp_path: Path) -> None:
    paths = prepare_gate1_run(tmp_path, run_id="manifest-run")
    api_log = paths.logs_dir / "api.log"
    screenshot = paths.screenshots_dir / "workspace.png"
    # CW-057: the runtime directory no longer holds a database file (the Gate 1
    # runtime database is PostgreSQL); private runtime state is still excluded
    # from the evidence manifest whatever its name is.
    runtime_state = paths.runtime_dir / "private-runtime-state.json"
    # newline="" keeps the on-disk bytes identical to the hashed b"...\n"
    # literals below; the default text-mode newline translation on Windows
    # would otherwise turn \n into \r\n and break the manifest hash.
    api_log.write_text("api ready\n", encoding="utf-8", newline="")
    screenshot.write_bytes(b"fake-png")
    runtime_state.write_bytes(b"private-runtime-state")

    manifest_path = write_evidence_manifest(paths, status="failed", commit_sha="abc123")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["run_id"] == "manifest-run"
    assert manifest["status"] == "failed"
    assert manifest["commit_sha"] == "abc123"
    assert manifest["files"] == [
        {
            "path": "logs/api.log",
            "sha256": hashlib.sha256(b"api ready\n").hexdigest(),
            "size_bytes": len(b"api ready\n"),
        },
        {
            "path": "screenshots/workspace.png",
            "sha256": hashlib.sha256(b"fake-png").hexdigest(),
            "size_bytes": len(b"fake-png"),
        },
    ]
    verify_evidence_manifest(manifest_path)

    api_log.write_text("tampered\n", encoding="utf-8", newline="")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_evidence_manifest(manifest_path)


def test_managed_processes_stop_children_when_body_fails(tmp_path: Path) -> None:
    process = None

    with pytest.raises(RuntimeError, match="forced failure"):
        with ManagedProcesses() as processes:
            process = processes.start(
                name="sleeper",
                command=[sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=tmp_path,
                env=_isolated_child_env(),
                log_path=tmp_path / "sleeper.log",
            )
            assert process.poll() is None
            raise RuntimeError("forced failure")

    assert process is not None
    assert process.poll() is not None


def test_managed_processes_continue_cleanup_after_one_stop_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = ManagedProcesses()
    first = processes.start(
        name="first",
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=_isolated_child_env(),
        log_path=tmp_path / "first.log",
    )
    second = processes.start(
        name="second",
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=_isolated_child_env(),
        log_path=tmp_path / "second.log",
    )
    original_stop = gate1_e2e._stop_process_tree
    calls = 0

    def flaky_stop(process: subprocess.Popen[bytes]) -> None:
        nonlocal calls
        calls += 1
        original_stop(process)
        if calls == 1:
            raise RuntimeError("first cleanup failed")

    monkeypatch.setattr(gate1_e2e, "_stop_process_tree", flaky_stop)

    with pytest.raises(RuntimeError, match="first cleanup failed"):
        processes.close()

    assert calls == 2
    assert first.poll() is not None
    assert second.poll() is not None


def test_managed_processes_retains_ownership_when_explicit_stop_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = ManagedProcesses()
    process = processes.start(
        name="retained",
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=_isolated_child_env(),
        log_path=tmp_path / "retained.log",
    )
    original_stop = gate1_e2e._stop_process_tree
    calls = 0

    def fail_before_first_stop(target: subprocess.Popen[bytes]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("explicit stop failed")
        original_stop(target)

    monkeypatch.setattr(gate1_e2e, "_stop_process_tree", fail_before_first_stop)

    with pytest.raises(RuntimeError, match="explicit stop failed"):
        processes.stop(process)

    assert process.poll() is None
    processes.close()
    assert calls == 2
    assert process.poll() is not None


def test_api_restart_controller_replaces_the_service_and_acknowledges_request(
    tmp_path: Path,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    request_path = tmp_path / "restart-request.json"
    completion_path = tmp_path / "restart-completion.json"
    log_path = tmp_path / "api.log"

    with ManagedProcesses() as processes:
        controller = ApiRestartController(
            processes=processes,
            command=[
                sys.executable,
                "-m",
                "http.server",
                str(port),
                "--bind",
                "127.0.0.1",
            ],
            cwd=tmp_path,
            env=_isolated_child_env(),
            log_path=log_path,
            health_url=f"http://127.0.0.1:{port}/",
            request_path=request_path,
            completion_path=completion_path,
            poll_interval_seconds=0.01,
        )
        controller.start()
        try:
            request_path.write_text('{"request_id":"restart-test"}\n', encoding="utf-8")
            # Windows may need the controller's five-second process-kill fallback
            # before the replacement service can start, so do not race that exact
            # boundary in the test.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not completion_path.exists():
                time.sleep(0.01)
            assert json.loads(completion_path.read_text(encoding="utf-8")) == {
                "request_id": "restart-test",
                "status": "ready",
            }
            assert log_path.read_text(encoding="utf-8").count("[harness] started api") == 2
        finally:
            controller.close()


def test_wait_for_http_accepts_a_ready_service() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        wait_for_http(
            f"http://127.0.0.1:{server.server_port}/health",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_port_probe_accepts_a_recently_released_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecentlyReleasedPort:
        def __init__(self) -> None:
            self.reuse_address = False

        def __enter__(self) -> RecentlyReleasedPort:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def setsockopt(self, level: int, option: int, value: int) -> None:
            if (level, option, value) == (
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            ):
                self.reuse_address = True

        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 8000)
            if not self.reuse_address:
                raise OSError("address remains in TIME_WAIT")

    monkeypatch.setattr(
        gate1_e2e.socket,
        "socket",
        lambda *args: RecentlyReleasedPort(),
    )

    gate1_e2e._require_available_port("127.0.0.1", 8000)


def test_gate1_api_port_accepts_a_safe_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GATE1_API_PORT", raising=False)
    assert gate1_e2e._gate1_api_port() == 8000

    monkeypatch.setenv("GATE1_API_PORT", "18000")
    assert gate1_e2e._gate1_api_port() == 18000

    for value in ("80", "5173", "65536", "not-a-port"):
        monkeypatch.setenv("GATE1_API_PORT", value)
        with pytest.raises(ValueError, match="GATE1_API_PORT"):
            gate1_e2e._gate1_api_port()


def test_generate_test_media_uses_deterministic_ffmpeg_outputs(
    tmp_path: Path,
) -> None:
    paths = prepare_gate1_run(tmp_path, run_id="media-run")
    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"generated")

    media = generate_test_media(paths, ffmpeg_binary="ffmpeg-test", runner=fake_runner)

    assert media == {
        "authorization_image": paths.media_dir / "authorization.png",
        "source_image": paths.media_dir / "source.png",
        "reference_video": paths.media_dir / "reference.mp4",
    }
    assert len(commands) == 3
    assert all(command[0] == "ffmpeg-test" for command in commands)
    assert "testsrc2=size=720x1280:rate=24" in commands[-1]
    assert "sine=frequency=440:sample_rate=48000" in commands[-1]


def test_run_gate1_records_git_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_git_commit(repository_root: Path) -> str:
        del repository_root
        raise RuntimeError("git metadata unavailable")

    monkeypatch.setattr(gate1_e2e, "_git_commit", fail_git_commit)

    exit_code = gate1_e2e.run_gate1(
        repository_root=tmp_path,
        output_root=tmp_path / "output",
        run_id="git-failure",
    )

    run_dir = tmp_path / "output" / "git-failure"
    manifest = json.loads((run_dir / "sha256-manifest.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert manifest["status"] == "failed"
    assert manifest["commit_sha"] == "unknown"
    assert "git metadata unavailable" in (run_dir / "logs" / "harness-error.log").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("change_kind", ["tracked", "untracked"])
def test_git_commit_rejects_a_dirty_worktree(tmp_path: Path, change_kind: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    tracked_file = tmp_path / "tracked.txt"
    tracked_file.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gate 1 Test",
            "-c",
            "user.email=gate1@example.invalid",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )
    expected_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert gate1_e2e._git_commit(tmp_path) == expected_commit

    if change_kind == "tracked":
        tracked_file.write_text("modified\n", encoding="utf-8")
    else:
        (tmp_path / "untracked.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="clean Git worktree"):
        gate1_e2e._git_commit(tmp_path)


def test_gate_status_reserves_passed_for_the_unfiltered_suite() -> None:
    assert gate1_e2e._gate_status(0, playwright_arguments=[]) == "passed"
    assert (
        gate1_e2e._gate_status(0, playwright_arguments=["--grep", "@smoke"]) == "diagnostic_passed"
    )
    assert gate1_e2e._gate_status(1, playwright_arguments=[]) == "failed"


def test_gate1_runtime_environment_forces_isolated_desktop_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "internal")
    monkeypatch.setenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "1")
    monkeypatch.setenv("VITE_GENERATION_PROVIDER", "metaso")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://production.example.com")
    # A leftover DB_PATH would make the PG-only child resolver reject the run,
    # so the harness must strip it from the child environment.
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "leftover.sqlite3"))

    environment = gate1_e2e._gate1_runtime_environment(
        database_url="postgresql://gate1:secret@localhost:5433/gate1_e2e",
        storage_root=tmp_path / "storage",
        settings_key="test-settings-key",
        fake_h3_result_path=tmp_path / "reference.mp4",
        api_url="http://127.0.0.1:18000",
    )

    assert environment["VIDEO_REPLICA_AUTH_MODE"] == "desktop"
    assert environment["VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER"] == "0"
    assert environment["VITE_GENERATION_PROVIDER"] == "fake_h3"
    assert environment["VITE_API_BASE_URL"] == "http://127.0.0.1:18000"
    assert environment["VIDEO_REPLICA_LOCAL_API_BASE_URL"] == "http://127.0.0.1:18000"
    assert environment["PUBLIC_BASE_URL"] == ""
    # CW-057: the Gate 1 runtime database is PostgreSQL only.
    assert (
        environment["VIDEO_REPLICA_DATABASE_URL"]
        == "postgresql://gate1:secret@localhost:5433/gate1_e2e"
    )
    assert "VIDEO_REPLICA_DB_PATH" not in environment


def test_run_metadata_records_whether_the_suite_is_filtered(tmp_path: Path) -> None:
    paths = prepare_gate1_run(tmp_path, run_id="filtered-run")

    gate1_e2e._write_run_metadata(
        paths,
        commit_sha="abc123",
        media={},
        playwright_arguments=["--grep", "@smoke"],
    )

    metadata = json.loads((paths.run_dir / "run.json").read_text(encoding="utf-8"))
    assert metadata["formal_run"] is False
    assert metadata["playwright_arguments"] == ["--grep", "@smoke"]


def test_playwright_argument_separator_is_not_forwarded() -> None:
    assert gate1_e2e.normalize_playwright_arguments(["--", "--grep", "@positive"]) == [
        "--grep",
        "@positive",
    ]
    assert gate1_e2e.normalize_playwright_arguments(["--grep", "@positive"]) == [
        "--grep",
        "@positive",
    ]
