from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cargo_build_uses_workspace_isolated_target_directory() -> None:
    config_path = REPO_ROOT / ".cargo" / "config.toml"

    assert config_path.exists()
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["build"]["target-dir"] == ".cargo-target"
    assert ".cargo-target/" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


def test_python_quality_commands_survive_a_relocated_virtualenv() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]

    assert "python -m mypy" in scripts["check"]
    assert "python -m pytest" in scripts["check"]
    assert "python -m pytest" in scripts["test"]
    assert "python -m pytest" in scripts["test:e2e"]
    assert "--locked mypy" not in scripts["check"]
    assert "--locked pytest" not in scripts["check"]
    assert "--locked pytest" not in scripts["test"]
    assert "--locked pytest" not in scripts["test:e2e"]


def test_api_uses_the_project_interpreter_instead_of_a_global_uvicorn() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    customer_e2e_launcher = (REPO_ROOT / "e2e/customer/setup-backend.mjs").read_text(
        encoding="utf-8"
    )

    assert "python -m uvicorn" in package["scripts"]["dev:server"]
    assert "--no-proxy-headers" in package["scripts"]["dev:server"]
    assert '"--no-proxy-headers"' in customer_e2e_launcher


def test_dev_start_commands_upgrade_the_database_before_api_or_worker() -> None:
    # CW-021 removed the packaged start-backend launchers from the desktop
    # bundle; only the development commands (which target a registered PG and
    # are never shipped) keep a bootstrapping order worth locking.
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))

    server_command = package["scripts"]["dev:server"]
    worker_command = package["scripts"]["dev:worker"]
    assert server_command.index("python -m app.bootstrap") < server_command.index(
        "python -m uvicorn"
    )
    assert worker_command.index("python -m app.bootstrap") < worker_command.index(
        "python -m app.generation_worker"
    )
    assert "--no-proxy-headers" in server_command
    collector_command = package["scripts"]["dev:viral-collection"]
    assert collector_command.index("python -m app.bootstrap") < collector_command.index(
        "python -m app.generation_worker"
    )
    assert "--viral-collection" in collector_command
    assert "--viral-collection" not in worker_command


def test_pull_requests_run_linux_quality_and_windows_nsis_gates() -> None:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    fork_pr_guard = (
        "github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository"
    )

    assert workflow_path.exists()
    workflow = workflow_path.read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert workflow.count("branches: [main]") == 2
    assert "pull_request_target:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    # 5 checkouts (changes, secret-scan, quality-linux, ffmpeg-windows-binaries,
    # windows-nsis); select-runner runs github-script only and checks out nothing.
    # ffmpeg-windows-binaries 是「客户安装包打入 LGPL ffmpeg」新增的：Windows
    # runner 跑不了 Linux 容器，交叉编译必须在 Linux 上做，产物再以 artifact 交给
    # windows-nsis。它同样 checkout，因此这里多一个。
    assert workflow.count("persist-credentials: false") == 5
    assert "secret-scan:" in workflow
    assert "name: Secret scan" in workflow
    assert "quality-linux:" in workflow
    assert "name: Linux quality gate" in workflow
    assert "windows-nsis:" in workflow
    assert "name: Windows Tauri and NSIS" in workflow
    # Supporting jobs added by the CI-localization change: `changes` (path
    # filtering) and `select-runner` (dual-path Linux gate: a self-hosted
    # `video-replica` runner when one is online, else GitHub-hosted). The pinned
    # actions and the arch-agnostic runner label are contract-checked so a future
    # edit cannot silently drop the dual path or unpin an action.
    assert "changes:" in workflow
    assert "name: Detect changes" in workflow
    assert "select-runner:" in workflow
    assert "name: Select runner" in workflow
    assert "dorny/paths-filter@ceb8a2b8f2d89434be7ff52d3de7ec3738c5cc9d" in workflow
    assert "actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3" in workflow
    assert '["self-hosted","linux","video-replica"]' in workflow
    assert "fromJSON(needs.select-runner.outputs.runner)" in workflow
    # Every job (changes, select-runner, secret-scan, quality-linux,
    # ffmpeg-windows-binaries, windows-nsis) carries the same-repo fork-PR guard,
    # so a fork PR never runs untrusted code.
    assert workflow.count(f"if: {fork_pr_guard}") == 6
    # ubuntu-24.04: the `changes` path filter, the Linux gate, and
    # ffmpeg-windows-binaries (which cross-compiles the Windows ffmpeg the
    # customer installer ships — the Windows runner cannot run Linux containers).
    assert workflow.count("runs-on: ubuntu-24.04") == 3
    assert workflow.count("runs-on: windows-2025") == 1
    assert "npm run check:security" in workflow
    # The Linux gate splits the former single `npm run check` into static checks
    # plus sharded pytest (each shard against its own isolated PostgreSQL
    # container); `npm run check` is no longer invoked verbatim in ci.yml.
    assert "run: npm run check:static\n" in workflow
    assert "run: bash scripts/ci/run-pytest-shards.sh\n" in workflow
    assert "npm run build" in workflow
    assert "npm audit --audit-level=high" in workflow
    assert "cargo test --manifest-path client/src-tauri/Cargo.toml --locked" in workflow
    assert "npm run check:tauri" in workflow
    # CW-021: the local sidecar feature and the internal edition are gone, so
    # CI only ever builds the customer cloud bundle.
    assert "check:tauri:internal" not in workflow
    assert "tauri:build:internal" not in workflow
    assert "internal NSIS" not in workflow
    assert "npm run tauri:build:customer" in workflow
    assert "VITE_API_BASE_URL: https://staging.example.invalid" in workflow
    windows_job = workflow.split("\n  windows-nsis:\n", 1)[1]
    job_config, windows_steps = windows_job.split("\n    steps:\n", 1)
    assert "runner.temp" not in job_config
    assert "LOCAL_ARTIFACT_ROOT" not in job_config
    step = windows_steps.split(
        "      - name: Archive unsigned customer cloud NSIS installer locally\n", 1
    )[1].split("\n      - name:", 1)[0]
    assert (
        "\n        env:\n"
        "          LOCAL_ARTIFACT_ROOT: ${{ runner.temp }}/video-replica-artifacts\n"
    ) in step
    assert workflow.count("LOCAL_ARTIFACT_ROOT") == 4
    assert workflow.count("SHA256SUMS.txt") == 1
    # CW-022: the Windows job must execute the OS-native credential (DPAPI)
    # and durable-identity tests that the Linux gate compiles out.
    assert "cargo test --manifest-path client/src-tauri/Cargo.toml --locked" in windows_job
    # CW-021 widened payload detection: beyond the launcher scripts the
    # customer installer must not carry an embedded server/Python runtime, a
    # SQLite business database, or boot/port markers.
    # 2026-09-23 用户明确授权放开 ffmpeg 一项：客户云客户端要用它做本地抽音轨
    # （P2 本地缓存），那是客户端自身能力，不是被 CW-021 清掉的本地后端。因此
    # ffmpeg 从禁项与路径正则里移除 —— 但**其余禁项一个都不能少**，下面逐条钉住。
    assert "Verify customer installer excludes local backend distribution" in workflow
    assert "pyvenv.cfg" in workflow
    assert "ffmpeg.exe" in workflow
    assert "'.db', '.sqlite', '.sqlite3', '.pyd'" in workflow
    assert "[\\\\/](server|\\.venv)[\\\\/]" in workflow
    # 放开 ffmpeg 之后，路径正则里不能再出现它，否则安装包会被误判为非法。
    assert "ffmpeg)[\\\\/]" not in workflow
    assert "VIDEO_REPLICA_BOOT_COMMAND" in workflow
    assert "127.0.0.1:8000" in workflow
    assert "7-Zip\\7z.exe" in workflow
    # The launcher names may only appear inside the payload gate's forbidden
    # list; no build/check/archive step may reference them.
    workflow_before_payload_gate = workflow.split(
        "Verify customer installer excludes local backend distribution", 1
    )[0]
    assert "start-backend" not in workflow_before_payload_gate
    assert "start-backend.bat" in workflow
    assert "start-backend.sh" in workflow
    # 5 checkouts: changes / secret-scan / quality-linux /
    # ffmpeg-windows-binaries / windows-nsis。
    assert workflow.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1") == 5
    assert workflow.count("actions/setup-node@820762786026740c76f36085b0efc47a31fe5020") == 3
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    # ffmpeg 的 Windows 交叉编译只能跑在 Linux（Windows runner 跑不了 Linux
    # 容器），产物须以 artifact 传给 windows-nsis —— 这是本仓唯一允许用
    # artifact 的地方，且必须 pin 到具体 SHA。
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert "actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131" in workflow
    assert ".cargo-target/release/bundle/nsis/*.exe" in workflow


def test_packaged_local_backend_launchers_are_removed() -> None:
    # CW-021: the desktop bundle must not ship any local backend launcher. The
    # packaged start scripts, the local-sidecar Cargo feature, the internal
    # overlay config, and the sidecar startup code in lib.rs are retired.
    for launcher in (
        "client/src-tauri/resources/start-backend.sh",
        "client/src-tauri/resources/start-backend.bat",
        "client/src-tauri/tauri.internal.conf.json",
    ):
        assert not (REPO_ROOT / launcher).exists(), f"{launcher} must be removed (CW-021)"
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    for script in ("check:tauri:internal", "tauri:build:internal", "tauri:dev:internal"):
        assert script not in package["scripts"], f"{script} must be removed (CW-021)"
    assert "local-sidecar" not in json.dumps(package)
    cargo_toml = (REPO_ROOT / "client/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    assert "local-sidecar" not in cargo_toml
    lib_rs = (REPO_ROOT / "client/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    for marker in (
        "BackendProcess",
        "BOOT_COMMAND",
        "local_api_ready",
        "start_local_services",
        "127.0.0.1:8000",
    ):
        assert marker not in lib_rs, f"lib.rs must not keep local sidecar code: {marker}"


def test_ci_shard_coverage_guard_is_wired() -> None:
    # CW-061 (SH-6 + CI-7): the shard-coverage guard must be wired into both the
    # runner and the workflow so a stale committed manifest fails the build
    # instead of silently skipping tests.
    #
    # Background: run-pytest-shards.sh only runs the files named in the committed
    # shard manifests; before CW-061 it never checked that those manifests cover
    # every server/tests/test_*.py, so 13 recent CW test files never ran in CI
    # (CW-044 §18.2). The guard is fail-closed and independent of every prerequisite.
    runner = (REPO_ROOT / "scripts" / "ci" / "run-pytest-shards.sh").read_text(encoding="utf-8")
    # SH-6: resolve_manifests must fail-closed via --check-coverage before adopting
    # the committed manifests.
    assert "build-test-shards.py" in runner
    assert "--check-coverage" in runner

    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # CI-7: a standalone coverage-assertion step, ordered BEFORE the sharded pytest.
    assert "build-test-shards.py --check-coverage" in workflow
    assert "Assert shard manifests cover every test file" in workflow
    assert workflow.index("build-test-shards.py --check-coverage") < workflow.index(
        "bash scripts/ci/run-pytest-shards.sh"
    )


def test_verify_customer_bundle_asserts_admin_base_stylesheet_marker() -> None:
    # ADMIN-BUNDLE-CSS-CONTRACT-20260912（双向合同的「包含向」）：
    # CW-019 拆分双入口后 styles.css 只剩客户壳一处导入，管理端产物整份缺失
    # 基础样式表（.admin-shell 布局、--admin-* 令牌定义），产线半裸渲染时全部
    # 「排除断言」（客户制品不含管理代码）依旧全绿——排除向发现不了「缺自身
    # 依赖」的回归。verify 脚本必须在阳性对照之外，显式断言管理制品 CSS 含
    # 基础样式表标记；源码级契约见 client/src/entryContract.test.ts。
    script = (REPO_ROOT / "scripts" / "verify_customer_bundle.mjs").read_text(encoding="utf-8")

    assert "runAdminBaseStylesheetControl" in script
    assert ".admin-shell{" in script
    assert "--admin-bg" in script
    # 断言必须真的接进主流程（防「定义了但没调用」的静默失效），
    # 且先于排除断言执行——让最严重的回归最早失败。
    assert script.index("runAdminBaseStylesheetControl();") < script.index(
        "const nameHits = checkForbiddenFileNames(relPaths)"
    )


def _run_desktop_artifact_collector(
    platform: str,
    bundle_dir: Path,
    output_dir: Path,
    api_base_url: str = "https://api.example.com",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["VITE_API_BASE_URL"] = api_base_url
    return subprocess.run(
        [
            "node",
            str(REPO_ROOT / "scripts/release/collect-desktop-artifacts.mjs"),
            platform,
            str(bundle_dir),
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("platform", "suffix", "signature"),
    [
        ("windows-x86_64", ".exe", "unsigned"),
        ("macos-arm64", ".dmg", "ad-hoc/unnotarized"),
        ("macos-x86_64", ".dmg", "ad-hoc/unnotarized"),
    ],
)
def test_desktop_artifact_collector_archives_one_installer_with_provenance(
    tmp_path: Path,
    platform: str,
    suffix: str,
    signature: str,
) -> None:
    bundle_dir = tmp_path / "bundle"
    output_dir = tmp_path / "output"
    bundle_dir.mkdir()
    installer = bundle_dir / f"video-replica-{platform}{suffix}"
    payload = f"installer:{platform}".encode()
    installer.write_bytes(payload)

    result = _run_desktop_artifact_collector(platform, bundle_dir, output_dir)

    assert result.returncode == 0, result.stderr
    assert (output_dir / installer.name).read_bytes() == payload
    digest = hashlib.sha256(payload).hexdigest()
    assert (output_dir / "SHA256SUMS.txt").read_text(encoding="utf-8") == (
        f"{digest}  {installer.name}\n"
    )
    assert (output_dir / "RELEASE-CHANNEL.txt").read_text(encoding="utf-8") == (
        "internal-test-unsigned\n"
    )
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert manifest["version"] == package["version"]
    assert manifest["platform"] == platform
    assert manifest["source_sha"] == source_sha
    assert manifest["api_base_url"] == "https://api.example.com"
    assert manifest["signature"] == signature
    assert manifest["artifact"] == installer.name
    assert manifest["sha256"] == digest


@pytest.mark.parametrize(
    ("platform", "api_base_url", "installer_names", "expected_error"),
    [
        (
            "windows-x86_64",
            "https://api.example.com",
            [],
            "exactly one non-empty .exe installer",
        ),
        (
            "windows-x86_64",
            "https://api.example.com",
            ["one.exe", "two.exe"],
            "exactly one non-empty .exe installer",
        ),
        (
            "linux-x86_64",
            "https://api.example.com",
            ["one.exe"],
            "unsupported platform",
        ),
        (
            "constructor",
            "https://api.example.com",
            ["one.exe"],
            "unsupported platform",
        ),
        (
            "__proto__",
            "https://api.example.com",
            ["one.exe"],
            "unsupported platform",
        ),
        (
            "macos-arm64",
            "http://localhost:8000",
            ["one.dmg"],
            "VITE_API_BASE_URL must be a routable",
        ),
    ],
)
def test_desktop_artifact_collector_rejects_invalid_input(
    tmp_path: Path,
    platform: str,
    api_base_url: str,
    installer_names: list[str],
    expected_error: str,
) -> None:
    bundle_dir = tmp_path / "bundle"
    output_dir = tmp_path / "output"
    bundle_dir.mkdir()
    for name in installer_names:
        (bundle_dir / name).write_bytes(b"installer")

    result = _run_desktop_artifact_collector(platform, bundle_dir, output_dir, api_base_url)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not output_dir.exists() or not any(output_dir.iterdir())


def test_desktop_artifact_collector_does_not_overwrite_existing_output(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    output_dir = tmp_path / "output"
    bundle_dir.mkdir()
    output_dir.mkdir()
    (bundle_dir / "installer.dmg").write_bytes(b"new installer")
    sentinel = output_dir / "existing.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = _run_desktop_artifact_collector("macos-arm64", bundle_dir, output_dir)

    assert result.returncode != 0
    assert "output directory must be empty" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in output_dir.iterdir()) == ["existing.txt"]


def test_desktop_artifact_collector_rejects_an_empty_installer(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    output_dir = tmp_path / "output"
    bundle_dir.mkdir()
    (bundle_dir / "empty.exe").touch()

    result = _run_desktop_artifact_collector("windows-x86_64", bundle_dir, output_dir)

    assert result.returncode != 0
    assert "exactly one non-empty .exe installer" in result.stderr
    assert not output_dir.exists()


def _matrix_entry(workflow: str, platform: str) -> str:
    lines = workflow.splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.strip() == f"- platform: {platform}"
    )
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start + 1)
            if line.strip().startswith("- platform:")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_desktop_installer_workflow_builds_three_internal_test_targets() -> None:
    workflow_path = REPO_ROOT / ".github/workflows/desktop-build.yml"

    assert workflow_path.exists()
    workflow = workflow_path.read_text(encoding="utf-8")
    assert workflow.startswith("name: Desktop installers\n")
    assert "workflow_dispatch:" in workflow
    assert "push:" in workflow
    push_config = workflow.split("\n  push:\n", 1)[1].split("\n\npermissions:", 1)[0]
    assert "main" in push_config
    assert "feat/desktop-actions-20260917" in push_config
    assert "pull_request:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert workflow.count("- platform:") == 3
    expected = {
        "windows-x86_64": ("windows-2025", "x86_64-pc-windows-msvc", "nsis"),
        "macos-arm64": ("macos-15", "aarch64-apple-darwin", "app"),
        "macos-x86_64": ("macos-15-intel", "x86_64-apple-darwin", "app"),
    }
    for platform, (runner, target, bundles) in expected.items():
        entry = _matrix_entry(workflow, platform)
        assert f"runner: {runner}" in entry
        assert f"target: {target}" in entry
        assert f"bundles: {bundles}" in [line.strip() for line in entry.splitlines()]
        expected_config = (
            "src-tauri/tauri.customer.conf.json"
            if platform == "windows-x86_64"
            else "src-tauri/tauri.macos.conf.json"
        )
        assert f"config: {expected_config}" in entry
    assert "hdiutil create" in workflow
    assert 'ln -s /Applications "$stage/Applications"' in workflow
    assert "actions/upload-artifact@" in workflow
    assert "if-no-files-found: error" in workflow
    assert "retention-days: 7" in workflow
    assert "output/desktop/${{ matrix.platform }}" in workflow
    for filename in ("manifest.json", "SHA256SUMS.txt", "RELEASE-CHANNEL.txt"):
        assert filename in workflow
    # 发布通道按 ref 收敛：v* 标签触发的 release 任务要创建 GitHub Release，
    # 是整个工作流唯一允许 contents: write 的位置；三个构建 job 本体保持只读。
    # 发布实现固定为 gh CLI，第三方发布动作仍然被禁。
    build_half, release_half = workflow.split("  release:", 1)
    assert "contents: write" not in build_half
    assert "contents: write" in release_half
    assert "softprops/action-gh-release" not in workflow
    assert "gh release create" in workflow


def test_macos_tauri_overlay_builds_dmg_without_distribution_signing() -> None:
    config_path = REPO_ROOT / "client/src-tauri/tauri.macos.conf.json"

    assert config_path.exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["bundle"]["targets"] == ["app", "dmg"]
    assert config["bundle"]["macOS"]["signingIdentity"] == "-"
    assert config["bundle"]["icon"] == ["icons/icon.icns"]
    assert config["bundle"]["resources"] == []
