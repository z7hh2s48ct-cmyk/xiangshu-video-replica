from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_rollout_uses_installed_topology_before_fetching_new_release() -> None:
    script = (REPO_ROOT / "deploy/customer-git-rollout.sh").read_text()
    assert 'COMPOSE="${CUSTOMER_COMPOSE:-$SCRIPT_DIR/customer/compose.yaml}"' in script
    assert 'COMPOSE_ENV="${CUSTOMER_COMPOSE_ENV:-/etc/video-replica/compose.env}"' in script
    assert "text.replace(old, new)" not in script


def test_image_override_changes_parameterized_images_without_touching_topology(tmp_path) -> None:
    script = (REPO_ROOT / "deploy/customer-git-rollout.sh").read_text()
    start = script.index("write_image_override() {")
    function = script[start : script.index("\n}\n", start) + 3]
    target = tmp_path / "image.json"
    subprocess.run(
        [
            "bash",
            "-c",
            function + '\nwrite_image_override "$1" "$2" api-1 worker-1 migrate',
            "test",
            str(target),
            "registry.example/release:abc",
        ],
        check=True,
    )
    document = json.loads(target.read_text())
    assert document == {
        "services": {
            service: {"image": "registry.example/release:abc"}
            for service in ("api-1", "worker-1", "migrate")
        }
    }
    assert target.stat().st_mode & 0o077 == 0


def test_bootstrap_records_the_database_head_required_by_first_upgrade() -> None:
    script = (REPO_ROOT / "deploy/customer/bootstrap-base-image.sh").read_text()
    assert "video-replica.database-head=$EXPECTED_DB_HEAD" in script
    assert "org.opencontainers.image.revision=$RELEASE_SHA" in script


@pytest.mark.parametrize("has_previous", [False, True])
@pytest.mark.parametrize("recovery_ready", [False, True])
def test_rollout_restores_image_selection_and_checks_recovery(
    tmp_path, has_previous: bool, recovery_ready: bool
) -> None:
    script = (REPO_ROOT / "deploy/customer-git-rollout.sh").read_text()
    start = script.index("rollback() {")
    function = script[start : script.index("\n}\n", start) + 3]
    (tmp_path / "backup").mkdir()
    (tmp_path / "image.json").write_text('{"services":{"api-1":{"image":"new"}}}')
    previous = '{"services":{"api-1":{"image":"old"}}}'
    if has_previous:
        (tmp_path / "backup/image-before.json").write_text(previous)
    harness = """
set -Eeuo pipefail
ROOT="$1"
BACKUP="$ROOT/backup"
IMAGE_OVERRIDE="$ROOT/image.json"
SITE="$ROOT/site"
ADMIN_SITE="$ROOT/admin"
ROLLOUT_STARTED=1
IMAGE_SWITCHED=1
REQUIRED_SERVICES=(api-1 api-2 worker-1 worker-2 worker-3 worker-4)
OPTIONAL_SERVICES=(worker-viral worker-publish)
SERVICES=("${REQUIRED_SERVICES[@]}" worker-viral worker-publish)
ROLLBACK_SERVICES=("${REQUIRED_SERVICES[@]}" worker-viral)
mark() { echo "$1" > "$ROOT/status"; }
compose() { echo "$*" >> "$ROOT/commands"; }
wait_ready() { return "$2"; }
""".replace('return "$2"', f"return {0 if recovery_ready else 1}")
    result = subprocess.run(
        ["bash", "-c", harness + function + "\nrollback 17 1 fake-command", "test", str(tmp_path)],
        capture_output=True,
    )
    assert result.returncode == 17, result.stderr.decode()
    if has_previous:
        assert (tmp_path / "image.json").read_text() == previous
    else:
        assert not (tmp_path / "image.json").exists()
    expected = "FAILED_ROLLED_BACK" if recovery_ready else "FAILED_ROLLBACK_INCOMPLETE"
    assert (tmp_path / "status").read_text().strip() == expected
    commands = (tmp_path / "commands").read_text().splitlines()
    assert "stop worker-publish" in commands
    assert "stop worker-viral" not in commands
    assert any(
        line.startswith("up -d --no-deps api-1") and "worker-viral" in line for line in commands
    )


def test_customer_git_rollout_requires_an_exact_revision_before_any_mutation() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert (
        'REPO_URL="${VIDEO_REPLICA_GIT_REPO_URL:-'
        'https://github.com/z7hh2s48ct-cmyk/xiangshu-video-replica}"'
    ) in script
    assert '[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]' in script
    assert 'git -C "$SOURCE" fetch --depth=1 origin "$RELEASE_SHA"' in script
    assert '[[ "$(git -C "$SOURCE" rev-parse HEAD)" == "$RELEASE_SHA" ]]' in script
    assert 'git -C "$SOURCE" diff --quiet' in script
    assert "mark PREFLIGHT" in script
    runtime = script[script.index('cd "$ROOT"') :]
    assert runtime.index("mark PREFLIGHT") < runtime.index("compose up")


def test_customer_git_rollout_builds_web_and_preserves_database_rollback_evidence() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert "npm ci --ignore-scripts" in script
    # CW-019：客户与管理是两个独立制品，发布链必须一次构建两个并跑排除断言。
    # 原 needle "npm run build --workspace client" 只产出 client/dist，会让
    # /admin 在合并后首次发布时静默 500。
    assert "npm run build:all" in script
    assert "npm run verify:customer-bundle" in script
    assert 'NODE_BUILD_IMAGE="node:24-bookworm-slim"' in script
    assert 'docker image inspect "$NODE_BUILD_IMAGE"' in script
    assert 'docker run --rm -v "$SOURCE:/workspace"' in script
    assert 'require_command "node"' not in script
    assert 'require_command "npm"' not in script
    assert "pg_dump -Fc" in script
    assert "DATABASE_HEAD_LEFT_FORWARD_COMPATIBLE" in script
    assert 'trap \'rollback "$?" "$LINENO" "$BASH_COMMAND"\' ERR' in script
    assert "VIDEO_REPLICA_DATABASE_URL" not in script
    assert "VIDEO_REPLICA_SETTINGS_KEY" not in script


def test_customer_git_rollout_injects_the_cloud_admin_origin() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert 'PUBLIC_ORIGIN="https://video.zszhj.cn"' in script
    assert '-e "VITE_CLOUD_ADMIN_ORIGIN=$PUBLIC_ORIGIN"' in script


def test_customer_git_rollout_only_rolls_optional_services_when_configured() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert "OPTIONAL_SERVICES=(worker-viral worker-publish)" in script
    assert ("mapfile -t CONFIGURED_SERVICES < <(compose config --services)") in script
    assert 'SERVICES+=("$service")' in script
    assert 'WORKER_SERVICES+=("$service")' in script
    assert 'for service in "${WORKER_SERVICES[@]}"; do' in script


def test_customer_git_rollout_allows_retry_after_forward_compatible_rollback() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert (
        '"$CURRENT_HEAD_BEFORE" != "$OLD_IMAGE_DB_HEAD" '
        '&& "$CURRENT_HEAD_BEFORE" != "$EXPECTED_DB_HEAD"'
    ) in script
    assert (
        "database revision is neither the active image head nor the target release head" in script
    )


def test_customer_git_rollout_ignores_only_root_package_version_metadata() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert "dependency_manifest_hash" in script
    assert "format_path = sys.argv[2]" in script
    assert 'if format_path.endswith("pyproject.toml"):' in script
    assert 'package["name"] != "video-replica-api"' in script
    assert 'dependency_manifest_hash "$SOURCE/$dependency_file" "$dependency_file"' in script
    assert 'dependency_manifest_hash - "$dependency_file"' in script
    assert "Python dependency change requires a base-image release" in script


def test_customer_git_rollout_ships_admin_artifact_in_the_same_release_run() -> None:
    """CW-019：管理制品必须与客户制品同 SHA 同轮替换，且失败要能回滚。

    本脚本是仓库里唯一的客户站部署可执行路径。拆分后 nginx 从 $ADMIN_SITE 提供
    /admin/，若脚本不产出、不备份、不原子替换 dist-admin，合并后首次发布仍会照常
    打印 SUCCESS，而 /admin 静默 500——失败发生在脚本的成功判据之外，故必须由
    断言锁住，不能只靠人工评审。
    """
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    # 管理站与客户站并列，且暂存目录同样按 release SHA + STAMP 命名（同轮）
    assert 'ADMIN_SITE="${SITE}-admin"' in script
    assert 'STAGE_ADMIN_SITE="$ROOT/admin-site-git-$SHORT_SHA-$STAMP"' in script
    # 构建产物落地校验：与客户站同规格（index.html 非空 + assets 目录 + 提取资源名）
    assert (
        '[[ -s "$SOURCE/client/dist-admin/index.html" '
        '&& -d "$SOURCE/client/dist-admin/assets" ]]' in script
    )
    assert (
        "EXPECTED_ADMIN_ASSET=$(grep -oE 'assets/[^\" ]+\\.js' "
        '"$SOURCE/client/dist-admin/index.html" | head -n 1)' in script
    )
    # 备份与回滚：归档存在才恢复（首次引入 dist-admin 的发布没有前一版可回滚）
    assert 'tar -czf "$BACKUP/admin-site-before.tar.gz" -C "$ADMIN_SITE" .' in script
    assert 'sha256sum "$BACKUP/admin-site-before.tar.gz" >> "$BACKUP/BACKUP-SHA256SUMS"' in script
    assert 'if [[ -f "$BACKUP/admin-site-before.tar.gz" ]]; then' in script
    assert 'tar -xzf "$BACKUP/admin-site-before.tar.gz" -C "$ADMIN_SITE"' in script
    # 原子替换：先清空再落盘，紧随客户站之后，两制品永不出自不同 release SHA
    assert 'mkdir -p "$STAGE_SITE" "$STAGE_ADMIN_SITE"' in script
    assert 'cp -a "$SOURCE/client/dist-admin"/. "$STAGE_ADMIN_SITE"/' in script
    assert 'grep -q "$EXPECTED_ADMIN_ASSET" "$STAGE_ADMIN_SITE/index.html"' in script
    assert 'cp -a "$STAGE_ADMIN_SITE"/. "$ADMIN_SITE"/' in script
    assert script.index('cp -a "$STAGE_SITE"/. "$SITE"/') < script.index(
        'cp -a "$STAGE_ADMIN_SITE"/. "$ADMIN_SITE"/'
    )
    # 真实探活：只比对落盘文件不够，必须证明 /admin/ 经 nginx 真的返回新资源名。
    # 拆分前 /admin 由客户 bundle 自己渲染，缺 nginx location 或缺 dist-admin 会
    # 降级到客户激活页而非报错，脚本会误判 SUCCESS。
    assert (
        'ADMIN_HTML=$(curl -fsS --max-time 20 "$PUBLIC_ORIGIN/admin/?release=$SHORT_SHA")' in script
    )
    assert 'grep -q "$EXPECTED_ADMIN_ASSET" <<< "$ADMIN_HTML"' in script
    assert "ADMIN_SITE=%s\\nADMIN_ASSET=%s" in script
    # 暂存残留会让 cp -a 混入上一轮字节，precheck 必须拒绝
    assert '! -e "$STAGE_ADMIN_SITE"' in script
