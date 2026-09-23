from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO
from urllib.error import URLError
from urllib.request import urlopen

from cryptography.fernet import Fernet

from app.db_pg import DATABASE_URL_ENV, DB_PATH_ENV, CliDatabaseConfigError, resolve_cli_pg_dsn
from app.gate1_bootstrap import bootstrap_gate1_database

CommandRunner = Callable[[list[str]], None]
DEFAULT_GATE1_API_PORT = 8000
GATE1_WEB_PORT = 5173


@dataclass(frozen=True)
class Gate1RunPaths:
    run_id: str
    run_dir: Path
    runtime_dir: Path
    media_dir: Path
    logs_dir: Path
    screenshots_dir: Path
    downloads_dir: Path
    browser_dir: Path


@dataclass
class _ManagedProcess:
    process: subprocess.Popen[bytes]
    log_file: BinaryIO


class ManagedProcesses:
    """Own child process groups so failures never leave local Gate 1 services behind."""

    def __init__(self) -> None:
        self._processes: list[_ManagedProcess] = []

    def __enter__(self) -> ManagedProcesses:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: object,
    ) -> None:
        del exc_type, exc_value, exc_traceback
        self.close()

    def start(
        self,
        *,
        name: str,
        command: list[str],
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                ),
            )
        except BaseException:
            log_file.close()
            raise
        self._processes.append(_ManagedProcess(process=process, log_file=log_file))
        # On Windows the child inherits the raw OS log handle without
        # O_APPEND, so a harness write through a freshly opened descriptor can
        # land at offset 0 and clobber the child's output. Writing through the
        # held "ab" file object keeps CRT O_APPEND semantics (seek to end
        # before every write, shared position) on every platform.
        log_file.write(f"\n[harness] started {name} pid={process.pid}\n".encode())
        log_file.flush()
        return process

    def close(self) -> None:
        first_error: BaseException | None = None
        while self._processes:
            managed = self._processes.pop()
            try:
                _stop_process_tree(managed.process)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            finally:
                managed.log_file.close()
        if first_error is not None:
            raise first_error

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        for index, managed in enumerate(self._processes):
            if managed.process is not process:
                continue
            _stop_process_tree(managed.process)
            managed.log_file.close()
            self._processes.pop(index)
            return
        raise ValueError("process is not managed")


class ApiRestartController:
    """Restart the isolated API when Playwright writes a local control request."""

    def __init__(
        self,
        *,
        processes: ManagedProcesses,
        command: list[str],
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
        health_url: str,
        request_path: Path,
        completion_path: Path,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self._processes = processes
        self._command = command
        self._cwd = cwd
        self._env = env
        self._log_path = log_path
        self._health_url = health_url
        self._request_path = request_path
        self._completion_path = completion_path
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._failure: BaseException | None = None
        self._last_request_id = ""

    def start(self) -> None:
        self._start_api()
        self._thread = Thread(target=self._watch, name="gate1-api-restart", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise RuntimeError("Gate 1 API restart controller did not stop")
        if self._failure is not None:
            raise RuntimeError("Gate 1 API restart failed") from self._failure

    def _start_api(self) -> None:
        self._process = self._processes.start(
            name="api",
            command=self._command,
            cwd=self._cwd,
            env=self._env,
            log_path=self._log_path,
        )
        wait_for_http(self._health_url)

    def _watch(self) -> None:
        while not self._stop_event.wait(self._poll_interval_seconds):
            request_id = self._read_request_id()
            if not request_id or request_id == self._last_request_id:
                continue
            self._last_request_id = request_id
            try:
                assert self._process is not None
                _append_text(self._log_path, f"\n[harness] restart requested id={request_id}\n")
                self._processes.stop(self._process)
                self._start_api()
                self._write_completion(request_id, status="ready")
            except BaseException as exc:
                self._failure = exc
                self._write_completion(request_id, status="failed")
                return

    def _read_request_id(self) -> str:
        try:
            request = json.loads(self._request_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return ""
        request_id = request.get("request_id") if isinstance(request, dict) else None
        if not isinstance(request_id, str):
            return ""
        return request_id if re.fullmatch(r"[A-Za-z0-9-]{1,128}", request_id) else ""

    def _write_completion(self, request_id: str, *, status: str) -> None:
        temporary_path = self._completion_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps({"request_id": request_id, "status": status}) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self._completion_path)


def prepare_gate1_run(output_root: Path, *, run_id: str) -> Gate1RunPaths:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
        raise ValueError("run_id may contain only letters, numbers, dot, underscore, and dash")
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    paths = Gate1RunPaths(
        run_id=run_id,
        run_dir=run_dir,
        runtime_dir=run_dir / "runtime",
        media_dir=run_dir / "media",
        logs_dir=run_dir / "logs",
        screenshots_dir=run_dir / "screenshots",
        downloads_dir=run_dir / "downloads",
        browser_dir=run_dir / "browser",
    )
    for directory in (
        paths.runtime_dir,
        paths.media_dir,
        paths.logs_dir,
        paths.screenshots_dir,
        paths.downloads_dir,
        paths.browser_dir,
    ):
        directory.mkdir()
    return paths


def generate_test_media(
    paths: Gate1RunPaths,
    *,
    ffmpeg_binary: str = "ffmpeg",
    runner: CommandRunner | None = None,
) -> dict[str, Path]:
    authorization_image = paths.media_dir / "authorization.png"
    source_image = paths.media_dir / "source.png"
    reference_video = paths.media_dir / "reference.mp4"
    commands = [
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1200x1600:rate=1",
            "-frames:v",
            "1",
            str(authorization_image),
        ],
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1024x1536:rate=1",
            "-frames:v",
            "1",
            str(source_image),
        ],
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x1280:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "6",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(reference_video),
        ],
    ]

    command_runner = runner or _media_command_runner(paths.logs_dir / "media.log")
    for command in commands:
        command_runner(command)

    media = {
        "authorization_image": authorization_image,
        "source_image": source_image,
        "reference_video": reference_video,
    }
    missing = [str(path) for path in media.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"Gate 1 media generation did not create: {', '.join(missing)}")
    return media


def wait_for_http(
    url: str,
    *,
    timeout_seconds: float = 30,
    poll_interval_seconds: float = 0.1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=min(2.0, timeout_seconds)) as response:  # noqa: S310
                if 200 <= response.status < 400:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
        time.sleep(poll_interval_seconds)
    detail = type(last_error).__name__ if last_error is not None else "not ready"
    raise TimeoutError(f"Timed out waiting for {url}: {detail}")


def write_evidence_manifest(
    paths: Gate1RunPaths,
    *,
    status: str,
    commit_sha: str,
) -> Path:
    manifest_path = paths.run_dir / "sha256-manifest.json"
    files: list[dict[str, str | int]] = []
    for path in sorted(paths.run_dir.rglob("*")):
        if not path.is_file() or path == manifest_path:
            continue
        relative_path = path.relative_to(paths.run_dir)
        if relative_path.parts[0] == "runtime":
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": relative_path.as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    manifest = {
        "schema_version": 1,
        "run_id": paths.run_id,
        "status": status,
        "commit_sha": commit_sha,
        "files": files,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def verify_evidence_manifest(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("Gate 1 evidence manifest files must be a list")
    run_dir = manifest_path.parent.resolve()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Gate 1 evidence manifest entry must be an object")
        relative_value = item.get("path")
        expected_sha256 = item.get("sha256")
        expected_size = item.get("size_bytes")
        if not isinstance(relative_value, str) or not isinstance(expected_sha256, str):
            raise ValueError("Gate 1 evidence manifest entry is incomplete")
        if not isinstance(expected_size, int):
            raise ValueError("Gate 1 evidence manifest size is invalid")
        evidence_path = (run_dir / relative_value).resolve()
        try:
            evidence_path.relative_to(run_dir)
        except ValueError as exc:
            raise ValueError("Gate 1 evidence manifest path escapes its run directory") from exc
        if not evidence_path.is_file():
            raise ValueError(f"Gate 1 evidence is missing: {relative_value}")
        content = evidence_path.read_bytes()
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError(f"Gate 1 evidence hash mismatch: {relative_value}")


def _to_sqlalchemy_psycopg_url(database_url: str) -> str:
    """Translate a libpq DSN into the SQLAlchemy driver URL Alembic needs."""
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return database_url.replace(prefix, "postgresql+psycopg://", 1)
    return database_url


def migrate_gate1_database(repository_root: Path, database_url: str) -> None:
    """Bring the Gate 1 PostgreSQL database to the frozen migration head.

    The PG runtime bootstrap no longer migrates (deploy/postgres/migrate.sh
    owns production migrations), so the harness runs the same Alembic
    ``upgrade head`` in-process before seeding. Failure here aborts the run
    before any API process starts.
    """

    from alembic import command
    from alembic.config import Config

    server_dir = repository_root / "server"
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", _to_sqlalchemy_psycopg_url(database_url))
    command.upgrade(config, "head")


def _gate1_runtime_environment(
    *,
    database_url: str,
    storage_root: Path,
    settings_key: str,
    fake_h3_result_path: Path,
    api_url: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    # A leftover DB_PATH would make the PG-only runtime resolver reject the
    # child outright (an ambiguous configuration is an error), so the harness
    # strips it from the child environment explicitly.
    environment.pop(DB_PATH_ENV, None)
    environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PUBLIC_BASE_URL": "",
            "VIDEO_REPLICA_AUTH_MODE": "desktop",
            "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER": "0",
            DATABASE_URL_ENV: database_url,
            "VIDEO_REPLICA_SETTINGS_KEY": settings_key,
            "VIDEO_REPLICA_DESKTOP_USER_ID": "gate1_admin",
            "VIDEO_REPLICA_FAKE_H3_RESULT_PATH": str(fake_h3_result_path),
            "VIDEO_REPLICA_FAKE_SOURCE_IMAGE_INSPECTOR": "1",
            "VIDEO_REPLICA_LOCAL_API_BASE_URL": api_url,
            "VIDEO_REPLICA_STORAGE_ROOT": str(storage_root),
            "VITE_API_BASE_URL": api_url,
            "VITE_GENERATION_PROVIDER": "fake_h3",
        }
    )
    return environment


def run_gate1(
    *,
    repository_root: Path,
    output_root: Path,
    run_id: str,
    database_url: str | None = None,
    playwright_arguments: list[str] | None = None,
) -> int:
    paths = prepare_gate1_run(output_root, run_id=run_id)
    normalized_playwright_arguments = list(playwright_arguments or [])
    commit_sha = "unknown"
    status = "failed"
    exit_code = 1
    try:
        commit_sha = _git_commit(repository_root)
        api_port = _gate1_api_port()
        api_url = f"http://127.0.0.1:{api_port}"
        web_url = f"http://127.0.0.1:{GATE1_WEB_PORT}"
        _require_command("ffmpeg")
        _require_command("uv")
        _require_command("npm")
        _require_available_port("127.0.0.1", api_port)
        _require_available_port("127.0.0.1", GATE1_WEB_PORT)

        # CW-057: the Gate 1 runtime database is PostgreSQL only. A missing or
        # SQLite DSN fails closed here (CliDatabaseConfigError lands in the
        # harness error log) and never creates a database file.
        resolved_database_url = resolve_cli_pg_dsn(database_url)
        migrate_gate1_database(repository_root, resolved_database_url)

        settings_key = Fernet.generate_key().decode("ascii")
        storage_root = paths.runtime_dir / "storage"
        storage_root.mkdir()
        runtime_env = _gate1_runtime_environment(
            database_url=resolved_database_url,
            storage_root=storage_root,
            settings_key=settings_key,
            fake_h3_result_path=paths.media_dir / "reference.mp4",
            api_url=api_url,
        )

        previous_key = os.environ.get("VIDEO_REPLICA_SETTINGS_KEY")
        os.environ["VIDEO_REPLICA_SETTINGS_KEY"] = settings_key
        try:
            bootstrap_gate1_database(
                resolved_database_url,
                user_id="gate1_admin",
                display_name="Gate 1 Admin",
            )
        finally:
            if previous_key is None:
                os.environ.pop("VIDEO_REPLICA_SETTINGS_KEY", None)
            else:
                os.environ["VIDEO_REPLICA_SETTINGS_KEY"] = previous_key

        media = generate_test_media(paths)
        _write_run_metadata(
            paths,
            commit_sha=commit_sha,
            media=media,
            playwright_arguments=normalized_playwright_arguments,
        )

        with ManagedProcesses() as processes:
            api_restart_request_path = paths.runtime_dir / "api-restart-request.json"
            api_restart_completion_path = paths.runtime_dir / "api-restart-completion.json"
            api_controller = ApiRestartController(
                processes=processes,
                command=[
                    "uv",
                    "--cache-dir",
                    ".uv-cache",
                    "run",
                    "--project",
                    "server",
                    "--locked",
                    "uvicorn",
                    "app.main:app",
                    "--app-dir",
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                cwd=repository_root,
                env=runtime_env,
                log_path=paths.logs_dir / "api.log",
                health_url=f"{api_url}/health",
                request_path=api_restart_request_path,
                completion_path=api_restart_completion_path,
            )
            api_controller.start()
            try:
                processes.start(
                    name="vite",
                    command=[
                        "npm",
                        "run",
                        "dev",
                        "--workspace",
                        "client",
                        "--",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(GATE1_WEB_PORT),
                    ],
                    cwd=repository_root,
                    env=runtime_env,
                    log_path=paths.logs_dir / "vite.log",
                )
                wait_for_http(f"{web_url}/")

                playwright_env = runtime_env.copy()
                playwright_env.update(
                    {
                        "GATE1_RUN_DIR": str(paths.run_dir),
                        "GATE1_WEB_URL": web_url,
                        "GATE1_API_URL": api_url,
                        "GATE1_API_RESTART_REQUEST_PATH": str(api_restart_request_path),
                        "GATE1_API_RESTART_COMPLETION_PATH": str(api_restart_completion_path),
                        "GATE1_MEDIA_DIR": str(paths.media_dir),
                        "GATE1_STORAGE_ROOT": str(storage_root),
                        "GATE1_WORKER_COMMAND": json.dumps(
                            [
                                "uv",
                                "--cache-dir",
                                ".uv-cache",
                                "run",
                                "--project",
                                "server",
                                "--locked",
                                "python",
                                "-m",
                                "app.generation_worker",
                                "--once",
                            ]
                        ),
                    }
                )
                playwright_command = [
                    "npx",
                    "playwright",
                    "test",
                    "--config",
                    "e2e/gate1/playwright.config.mjs",
                    *normalized_playwright_arguments,
                ]
                playwright_process = processes.start(
                    name="playwright",
                    command=playwright_command,
                    cwd=repository_root,
                    env=playwright_env,
                    log_path=paths.logs_dir / "playwright.log",
                )
                exit_code = playwright_process.wait()
                if exit_code == 0:
                    completed_commit_sha = _git_commit(repository_root)
                    if completed_commit_sha != commit_sha:
                        raise RuntimeError("Gate 1 Git commit changed while the suite was running")
                status = _gate_status(
                    exit_code,
                    playwright_arguments=normalized_playwright_arguments,
                )
            finally:
                api_controller.close()
    except BaseException:
        status = "failed"
        (paths.logs_dir / "harness-error.log").write_text(
            traceback.format_exc(),
            encoding="utf-8",
        )
        exit_code = 1
    finally:
        manifest_path = write_evidence_manifest(paths, status=status, commit_sha=commit_sha)
        verify_evidence_manifest(manifest_path)
    return exit_code


def _media_command_runner(log_path: Path) -> CommandRunner:
    def run(command: list[str]) -> None:
        with log_path.open("ab") as log_file:
            subprocess.run(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=True,
            )

    return run


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    # sys.platform (rather than os.name) lets mypy's platform stubs narrow
    # away the POSIX-only os.killpg/SIGKILL calls on Windows.
    if sys.platform == "win32":
        if process.poll() is not None:
            return
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if sys.platform == "win32":
        process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    process.wait(timeout=5)


def _require_available_port(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            # 与 Uvicorn/Vite 的监听行为一致：允许立即复用刚释放、仍处于
            # TIME_WAIT 的本地地址，同时仍会拒绝真正存在的监听进程。
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        except OSError as exc:
            raise RuntimeError(f"Gate 1 requires free port {host}:{port}") from exc


def _gate1_api_port() -> int:
    raw_value = os.environ.get("GATE1_API_PORT", "").strip()
    if not raw_value:
        return DEFAULT_GATE1_API_PORT
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise ValueError("GATE1_API_PORT must be an integer from 1024 to 65535") from exc
    if port < 1024 or port > 65535 or port == GATE1_WEB_PORT:
        raise ValueError("GATE1_API_PORT must be from 1024 to 65535 and differ from the web port")
    return port


def _require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise RuntimeError(f"Gate 1 requires command: {command}")


def _git_commit(repository_root: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RuntimeError(
            "Gate 1 requires a clean Git worktree; commit or stash source changes first"
        )
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_run_metadata(
    paths: Gate1RunPaths,
    *,
    commit_sha: str,
    media: Mapping[str, Path],
    playwright_arguments: list[str],
) -> None:
    metadata = {
        "schema_version": 1,
        "run_id": paths.run_id,
        "commit_sha": commit_sha,
        "started_at": datetime.now(UTC).isoformat(),
        "runtime": "React/Vite + FastAPI + one-shot Worker + PostgreSQL + LocalStorageAdapter",
        "gate_scope": "macOS desktop FakeProvider E2E; not Windows WebView2 acceptance",
        "formal_run": not playwright_arguments,
        "playwright_arguments": playwright_arguments,
        "media": {name: path.relative_to(paths.run_dir).as_posix() for name, path in media.items()},
    }
    (paths.run_dir / "run.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_text(path: Path, content: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(content)


def _default_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def normalize_playwright_arguments(arguments: list[str]) -> list[str]:
    return arguments[1:] if arguments[:1] == ["--"] else arguments


def _gate_status(exit_code: int, *, playwright_arguments: list[str]) -> str:
    if exit_code != 0:
        return "failed"
    return "passed" if not playwright_arguments else "diagnostic_passed"


def main() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Run the local desktop Gate 1 Playwright flow")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=repository_root / "output" / "playwright" / "gate1",
    )
    parser.add_argument("--run-id", default=_default_run_id())
    parser.add_argument(
        "--database-url",
        default="",
        help=(
            "PostgreSQL DSN for the Gate 1 runtime database "
            "(defaults to VIDEO_REPLICA_DATABASE_URL; SQLite is rejected)"
        ),
    )
    parser.add_argument("playwright_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        resolved_database_url = resolve_cli_pg_dsn(args.database_url)
    except CliDatabaseConfigError as exc:
        parser.error(f"error: {exc}")
        return
    raise SystemExit(
        run_gate1(
            repository_root=repository_root,
            output_root=args.output_root,
            run_id=args.run_id,
            database_url=resolved_database_url,
            playwright_arguments=normalize_playwright_arguments(args.playwright_args),
        )
    )


if __name__ == "__main__":
    main()
