"""CW-025: 桌面制品不注入 PG DSN 断言（CW-021 双配置布局）。

账本要求："验证桌面制品不接收 PG DSN，API 与 Worker 只连接同一登记 PG"。

CW-020 将客户配置提升为唯一默认；CW-021 进一步退役内部版与本地 sidecar 后，
桌面端只剩两份 Tauri 配置：
- base ``tauri.conf.json``：客户 foundation（唯一默认），承载 identifier
  ``com.xiangshu.video-replica.customer``、version、窗口 url="customer"、
  resources=[]、HTTPS-only CSP、longDescription"不包含本地 API 或 Worker 启动器"。
- customer overlay ``tauri.customer.conf.json``：仅追加发行专属 installerHooks。
- 内部 overlay ``tauri.internal.conf.json`` 已随 CW-021 删除：内部发行与
  local-sidecar feature 一并退役，不存在（也不允许重建）第四份配置。

客户制品 = base + customer overlay 合并，是纯 UI 壳，连接云端 HTTPS API，
不包含本地后端启动器，也不应该接收 PG DSN 环境变量。

本文件验证：
1. 客户制品配置（base + customer overlay）不含 VIDEO_REPLICA_DATABASE_URL / DB_PATH。
2. base（客户 foundation）resources=[]，客户制品链路不引用任何本地启动器。
3. 唯一默认客户构建脚本（tauri:build）不注入 PG DSN；内部 opt-in 构建脚本已删除。
4. 内部 overlay 配置不存在（内部发行不能被意外重建）。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_TAURI = REPO_ROOT / "client" / "src-tauri"
# CW-021：base = 客户 foundation（唯一默认）；customer overlay 仅追加 installerHooks；
# internal overlay 已删除，内部发行随 local-sidecar feature 一并退役。
TAURI_BASE_CONF = SRC_TAURI / "tauri.conf.json"
TAURI_CUSTOMER_OVERLAY_CONF = SRC_TAURI / "tauri.customer.conf.json"
TAURI_INTERNAL_OVERLAY_CONF = SRC_TAURI / "tauri.internal.conf.json"
PACKAGE_JSON = REPO_ROOT / "package.json"
REQUIRE_CUSTOMER_API_BASE = REPO_ROOT / "scripts" / "require_customer_api_base.mjs"


def test_customer_tauri_config_exists() -> None:
    """前置检查：CW-021 双份桌面制品配置存在，内部 overlay 不存在。"""
    for conf in (TAURI_BASE_CONF, TAURI_CUSTOMER_OVERLAY_CONF):
        assert conf.exists(), f"桌面制品配置不存在: {conf}"
    assert not TAURI_INTERNAL_OVERLAY_CONF.exists(), (
        "内部 overlay tauri.internal.conf.json 必须保持删除（CW-021 退役内部发行）"
    )


def test_customer_tauri_config_has_no_database_url_env() -> None:
    """CW-025: 客户制品配置（base + customer overlay）不含 VIDEO_REPLICA_DATABASE_URL。

    客户制品是纯 UI 壳，通过 HTTPS 连接云端 API，不应该接收 PG DSN。CW-020 后
    客户 foundation（identifier/resources/CSP）在 base tauri.conf.json，customer
    overlay 仅追加 installerHooks——两者都不得注入 PG DSN 或 DB_PATH。
    """
    for conf in (TAURI_BASE_CONF, TAURI_CUSTOMER_OVERLAY_CONF):
        config_text = conf.read_text(encoding="utf-8")
        config = json.loads(config_text)

        # 检查整个配置文件不含 DATABASE_URL / DB_PATH 相关字符串
        assert "VIDEO_REPLICA_DATABASE_URL" not in config_text, (
            f"{conf.name} 不得含 VIDEO_REPLICA_DATABASE_URL"
        )
        assert "DATABASE_URL" not in config_text, f"{conf.name} 不得含任何 DATABASE_URL 引用"
        assert "VIDEO_REPLICA_DB_PATH" not in config_text, (
            f"{conf.name} 不得含 VIDEO_REPLICA_DB_PATH"
        )

        # 检查 env 字段（如果存在）不含 PG DSN
        if "env" in config:
            env_vars = config["env"]
            assert "VIDEO_REPLICA_DATABASE_URL" not in env_vars, (
                f"{conf.name} env 不得注入 VIDEO_REPLICA_DATABASE_URL"
            )


def test_customer_tauri_config_has_no_backend_resources() -> None:
    """CW-025: base（客户 foundation）resources=[] 不打包任何后端启动资源。

    客户制品描述明确："不包含本地 API 或 Worker 启动器"。CW-020 后 resources 与
    longDescription 由 base tauri.conf.json 承载（不再在瘦 customer overlay）。
    """
    config = json.loads(TAURI_BASE_CONF.read_text(encoding="utf-8"))

    # 2026-09-23 用户明确授权：客户云客户端带 ffmpeg，用于本地抽音轨（P2 本地
    # 缓存）。「不得打包本地后端启动器」这条本身不变，因此这里从「必须为空」
    # 收紧为**精确白名单**——将来有人塞进 start-backend / server 之类会立刻红。
    bundle = config.get("bundle", {})
    resources = bundle.get("resources", [])
    assert resources == ["resources/ffmpeg/*"], (
        f"客户 base resources 只允许白名单内的 ffmpeg，实际: {resources}"
    )

    # 检查描述明确说不含本地启动器
    long_description = bundle.get("longDescription", "")
    assert "不包含本地 API" in long_description or "不含本地" in long_description, (
        f"客户 base 描述应明确不含本地启动器，实际: {long_description}"
    )

    # base 即客户 foundation：identifier 含 customer、CSP 为 HTTPS-only（无本地 loopback 回退）
    assert "customer" in config.get("identifier", "").lower(), (
        f"客户 base identifier 应含 'customer': {config.get('identifier', '')}"
    )
    csp = config.get("app", {}).get("security", {}).get("csp", "")
    assert "127.0.0.1:8000" not in csp, f"客户 base CSP 不得含本地 loopback API 回退: {csp}"


def test_customer_tauri_config_does_not_reference_start_backend() -> None:
    """CW-021: 客户制品（base + customer overlay）不引用 start-backend。

    CW-021 已删除 resources/start-backend.sh 与 start-backend.bat，桌面制品
    链路不再有任何本地启动器引用。
    """
    for conf in (TAURI_BASE_CONF, TAURI_CUSTOMER_OVERLAY_CONF):
        config_text = conf.read_text(encoding="utf-8")

        assert "start-backend" not in config_text, f"{conf.name} 不得引用 start-backend"
        assert "start_backend" not in config_text, f"{conf.name} 不得引用 start_backend"
    launchers = (
        SRC_TAURI / "resources" / "start-backend.sh",
        SRC_TAURI / "resources" / "start-backend.bat",
    )
    for launcher in launchers:
        assert not launcher.exists(), f"打包启动器必须保持删除（CW-021）: {launcher}"


def test_internal_edition_stays_withdrawn() -> None:
    """CW-021: 内部发行配置与构建入口必须保持退役，不能被意外重建。

    内部 overlay tauri.internal.conf.json 已删除；package.json 不得保留任何
    引用 local-sidecar / 内部配置的构建、检查或开发脚本。
    """
    assert not TAURI_INTERNAL_OVERLAY_CONF.exists(), (
        f"内部制品配置必须保持删除: {TAURI_INTERNAL_OVERLAY_CONF}"
    )

    package_text = PACKAGE_JSON.read_text(encoding="utf-8")
    package = json.loads(package_text)
    scripts = package.get("scripts", {})
    for script_name in ("check:tauri:internal", "tauri:build:internal", "tauri:dev:internal"):
        assert script_name not in scripts, f"package.json 不得保留内部构建脚本: {script_name}"
    assert "local-sidecar" not in package_text, (
        "package.json 不得再引用 local-sidecar feature（CW-021 已退役）"
    )
    assert "tauri.internal.conf.json" not in package_text, (
        "package.json 不得引用已删除的 tauri.internal.conf.json"
    )


def test_package_json_customer_build_script_does_not_inject_pg_dsn() -> None:
    """CW-025: package.json 唯一默认客户构建脚本不注入 PG DSN。

    CW-020 后 ``tauri:build``（无后缀）即客户唯一默认，使用 tauri.customer.conf.json
    + --no-default-features + origin guard，不注入 VIDEO_REPLICA_DATABASE_URL；
    ``tauri:build:customer`` 保留为别名。CW-021 后内部构建脚本已删除。
    """
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    scripts = package.get("scripts", {})

    default_build = scripts.get("tauri:build", "")
    assert default_build, "package.json 缺少 tauri:build 脚本（唯一默认客户构建）"

    # 唯一默认构建不注入任何 PG DSN / DB_PATH
    assert "VIDEO_REPLICA_DATABASE_URL" not in default_build, (
        f"唯一默认客户构建脚本不得注入 VIDEO_REPLICA_DATABASE_URL: {default_build}"
    )
    assert "DATABASE_URL" not in default_build, (
        f"唯一默认客户构建脚本不得含任何 DATABASE_URL 引用: {default_build}"
    )
    assert "VIDEO_REPLICA_DB_PATH" not in default_build, (
        f"唯一默认客户构建脚本不得注入 VIDEO_REPLICA_DB_PATH: {default_build}"
    )

    # 唯一默认构建使用 customer overlay + --no-default-features（编译期排除本地后端）
    assert "tauri.customer.conf.json" in default_build, (
        f"唯一默认客户构建脚本必须使用 tauri.customer.conf.json: {default_build}"
    )
    assert "--no-default-features" in default_build, (
        f"唯一默认客户构建脚本必须 --no-default-features: {default_build}"
    )

    # tauri:build:customer 保留为唯一默认的别名，同样不注入 PG DSN
    customer_alias = scripts.get("tauri:build:customer", "")
    assert customer_alias, "package.json 缺少 tauri:build:customer 别名脚本"
    assert "VIDEO_REPLICA_DATABASE_URL" not in customer_alias, (
        f"客户构建别名不得注入 VIDEO_REPLICA_DATABASE_URL: {customer_alias}"
    )


def test_require_customer_api_base_script_does_not_inject_pg_dsn() -> None:
    """CW-025: require_customer_api_base.mjs 不注入 PG DSN。

    这个脚本只验证客户 API base URL 配置，不应该涉及数据库配置。
    """
    if not REQUIRE_CUSTOMER_API_BASE.exists():
        raise AssertionError(f"脚本不存在: {REQUIRE_CUSTOMER_API_BASE}")

    script_text = REQUIRE_CUSTOMER_API_BASE.read_text(encoding="utf-8")

    # 检查脚本不含 DATABASE_URL 注入
    assert "VIDEO_REPLICA_DATABASE_URL" not in script_text, (
        "require_customer_api_base.mjs 不得注入 VIDEO_REPLICA_DATABASE_URL"
    )
    assert "DATABASE_URL" not in script_text, (
        "require_customer_api_base.mjs 不得含任何 DATABASE_URL 引用"
    )

    # 检查脚本不含 DB_PATH 注入
    assert "VIDEO_REPLICA_DB_PATH" not in script_text, (
        "require_customer_api_base.mjs 不得注入 VIDEO_REPLICA_DB_PATH"
    )


def test_customer_artifact_env_isolation_summary() -> None:
    """CW-025: 桌面制品环境隔离总结断言。

    综合验证：客户制品构建链路（package.json tauri:build →
    require_customer_api_base.mjs → base tauri.conf.json + customer overlay）
    全程不接触 PG DSN 或 DB_PATH，客户 foundation resources 为空；内部 opt-in
    制品已随 CW-021 退役，不存在需要隔离的第四份配置。
    """
    # 1. 配置层面：现存桌面制品均不含 PG DSN / DB_PATH，内部 overlay 不存在
    for conf in (TAURI_BASE_CONF, TAURI_CUSTOMER_OVERLAY_CONF):
        conf_text = conf.read_text(encoding="utf-8")
        assert "VIDEO_REPLICA_DATABASE_URL" not in conf_text, f"{conf.name} 含 PG DSN"
        assert "VIDEO_REPLICA_DB_PATH" not in conf_text, f"{conf.name} 含 DB_PATH"
    assert not TAURI_INTERNAL_OVERLAY_CONF.exists(), "内部 overlay 必须保持删除（CW-021）"

    # 2. 构建脚本层面（唯一默认 tauri:build；内部 opt-in 脚本已删除）
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    scripts = package.get("scripts", {})
    build_script = scripts.get("tauri:build", "")
    assert "VIDEO_REPLICA_DATABASE_URL" not in build_script, "tauri:build 注入 PG DSN"
    assert "VIDEO_REPLICA_DB_PATH" not in build_script, "tauri:build 注入 DB_PATH"
    assert "tauri:build:internal" not in scripts, "内部构建脚本必须保持删除（CW-021）"

    # 3. 辅助脚本层面
    if REQUIRE_CUSTOMER_API_BASE.exists():
        helper_text = REQUIRE_CUSTOMER_API_BASE.read_text(encoding="utf-8")
        assert "VIDEO_REPLICA_DATABASE_URL" not in helper_text
        assert "VIDEO_REPLICA_DB_PATH" not in helper_text

    # 4. 客户 foundation 只带白名单资源：ffmpeg（本地抽音轨，2026-09-23 授权），
    #    任何后端启动器仍然一个都不许有。
    base_conf = json.loads(TAURI_BASE_CONF.read_text(encoding="utf-8"))
    assert base_conf.get("bundle", {}).get("resources", []) == ["resources/ffmpeg/*"]
