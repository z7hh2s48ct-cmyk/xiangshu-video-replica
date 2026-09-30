"""CW-024: 唯一签名安装包与安全升级流程合同。

账本要求（承接 CW-011 客户构建合同）："先锁住旧安装路径/数据保护和升级失败
合同，再修改安装 hook 及签名分发流程"；真实签名实机验收归 CW-046，真实旧数
据切换归 CW-051，本文件只锁自动化可验证的合同。

复用现有（CW-021/022 交付，不重复建设）：
- 客户 NSIS hook 的固定旧路径检测、同步卸载、失败即 Abort；
- CI 的无签名客户包构建、启动器/本地后端排除检测与 SHA 归档。

仅做剩余（本文件锁定的差额）：
1. 现有 hook 只覆盖单一固定旧路径 ``$LOCALAPPDATA\\短视频复刻工作台``，且在
   无归档/迁移证明前置的情况下直接静默卸载。CW-003 冻结的受支持版本
   （0.1.12/0.1.13/0.1.15/0.1.16）按 git 史实实际产生**两条**旧安装路径：
   0.1.12/0.1.13/0.1.15 与 0.1.16（#92 发布）以 productName
   ``短视频复刻工作台`` 安装为 NSIS currentUser，即
   ``$LOCALAPPDATA\\短视频复刻工作台``；0.1.16 W1 品牌替换（1fb997a）后以
   productName ``众墅之家`` 安装为 NSIS currentUser，即
   ``$LOCALAPPDATA\\众墅之家``。两条路径都必须被覆盖。
2. hook 必须先做运行实例守卫（运行中零改动阻断），再完整归档旧安装目录、
   校验归档存在，之后才允许执行旧版静默卸载；卸载失败时中止安装并保留可读
   备份（失败可恢复旧客户或保留可读数据）。
3. CI 产出的无签名安装包只能标注为内部测试制品（机器可读渠道标签）；
   workflow 内不允许出现任何签名材料。
4. 必须存在唯一签名发布流程（fail-closed：无签名材料即拒绝出制品），产物
   记录版本、平台、签名与 SHA。
5. 升级/卸载运行手册必须存在、可执行并完成文件登记。

凭据保护边界（CW-003 命名空间冻结 + CW-022 复验）：旧版 DPAPI 凭据信封与
注册表镜像位于安装目录之外（app data / 注册表），hook 不得触碰。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CUSTOMER_INSTALLER_HOOKS = REPO_ROOT / "client" / "src-tauri" / "customer-installer-hooks.nsh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DESKTOP_BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "desktop-build.yml"
PACKAGE_JSON = REPO_ROOT / "package.json"
CLIENT_PACKAGE_JSON = REPO_ROOT / "client" / "package.json"
TAURI_CONF = REPO_ROOT / "client" / "src-tauri" / "tauri.conf.json"
CUSTOMER_TAURI_CONF = REPO_ROOT / "client" / "src-tauri" / "tauri.customer.conf.json"
UPDATER_CAPABILITY = REPO_ROOT / "client" / "src-tauri" / "capabilities" / "updater.json"
CARGO_TOML = REPO_ROOT / "client" / "src-tauri" / "Cargo.toml"
SIGNED_RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release" / "build-customer-signed-release.ps1"

# CW-003 冻结受支持版本（0.1.14 跳过无发布）对应的全部实际旧安装路径。
# 每项 = (旧版 productName, NSIScurrentUser 安装目录名)。
LEGACY_INSTALL_PATH_NAMES = ("短视频复刻工作台", "众墅之家")

BACKUP_ROOT_NEEDLE = "$LOCALAPPDATA\\短视频复刻客户云工作台\\legacy-backup"

UNSIGNED_CHANNEL_LABEL = "internal-test-unsigned"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_marker_order(hook: str, markers: list[str], owner: str) -> None:
    """每个 marker 都必须出现，且出现顺序与给定顺序一致。"""
    cursor = 0
    for marker in markers:
        found = hook.find(marker, cursor)
        assert found != -1, f"[{owner}] 缺少合同标记: {marker!r}"
        cursor = found


def test_hook_covers_every_supported_legacy_install_path() -> None:
    hook = _read(CUSTOMER_INSTALLER_HOOKS)

    for name in LEGACY_INSTALL_PATH_NAMES:
        assert f'IfFileExists "$LOCALAPPDATA\\{name}\\uninstall.exe"' in hook, (
            f"旧安装路径 $LOCALAPPDATA\\{name} 未被覆盖"
        )
        assert f"ExecWait '\"$LOCALAPPDATA\\{name}\\uninstall.exe\" /S'" in hook, (
            f"旧安装路径 $LOCALAPPDATA\\{name} 缺少静默卸载步骤"
        )


def test_hook_uninstall_is_gated_on_running_guard_and_verified_archive() -> None:
    hook = _read(CUSTOMER_INSTALLER_HOOKS)

    for name in LEGACY_INSTALL_PATH_NAMES:
        source = f"$LOCALAPPDATA\\{name}"
        backup = f"${{CW24_LEGACY_BACKUP_ROOT}}\\{name}"
        # 合同顺序：存在检测 → 运行实例守卫（优先探测旧版主程序映像，运行中
        # 写模式打开即失败；回退卸载器自身）→ 完整归档 → 归档校验 → 写备份
        # 清单 → 静默卸载。
        exec_wait = "ExecWait '\"" + source + "\\uninstall.exe\" /S'"
        _assert_marker_order(
            hook,
            [
                f'IfFileExists "{source}\\uninstall.exe"',
                f'IfFileExists "{source}\\{name}.exe"',
                f'FileOpen $R9 "{source}\\{name}.exe" a',
                f'FileOpen $R9 "{source}\\uninstall.exe" a',
                f'CopyFiles /SILENT "{source}\\*.*" "{backup}"',
                f'IfFileExists "{backup}\\uninstall.exe"',
                "LEGACY-BACKUP-MANIFEST.txt",
                exec_wait,
            ],
            owner=name,
        )
    assert BACKUP_ROOT_NEEDLE in hook, "备份根目录必须位于客户 app data 树内"


def test_hook_fails_closed_on_every_step() -> None:
    hook = _read(CUSTOMER_INSTALLER_HOOKS)

    for name in LEGACY_INSTALL_PATH_NAMES:
        owner = f"$LOCALAPPDATA\\{name}"
        # 运行中 → 零改动阻断（提示先退出旧版）。
        assert "legacy_internal_running" in hook or "legacy_rebrand_running" in hook
        # 归档/校验失败 → 中止且旧版未被改动。
        assert "legacy_internal_archive_failed" in hook or (
            "legacy_rebrand_archive_failed" in hook
        ), f"[{owner}] 缺少归档失败阻断分支"
        # 卸载失败 → 中止，且提示中必须指明备份位置可恢复。
        assert "IfErrors legacy_internal_failed" in hook or (
            "IfErrors legacy_rebrand_failed" in hook
        ), f"[{owner}] 卸载执行错误未阻断"
        # 每条失败分支都以 Abort 收口（保留 CW-021 先例的失败阻断语义）。
        assert hook.count("Abort") >= 2, "运行/归档/卸载失败分支必须各自 Abort"
    assert "保留可读备份" in hook or "已完整备份" in hook, "卸载失败提示必须给出恢复依据"


def test_hook_preserves_frozen_credential_namespaces() -> None:
    hook = _read(CUSTOMER_INSTALLER_HOOKS)

    # CW-003 冻结的凭据命名空间（DPAPI 信封在 app data、identifier 注册表镜像）
    # 位于安装目录之外；hook 只允许处理 $LOCALAPPDATA 下的安装目录本身，
    # 不得出现任何对 app data/注册表凭据存储的删除或覆盖。
    assert "com.internal.video-replica" not in hook
    assert "com.xiangshu.video-replica.customer" not in hook
    for forbidden in ("$APPDATA", "HKCU\\Software"):
        assert forbidden not in hook, f"hook 不得触碰 {forbidden}（凭据/注册表命名空间冻结）"


def test_ci_unsigned_artifacts_are_labeled_internal_test_only() -> None:
    workflow = _read(CI_WORKFLOW)
    package = json.loads(_read(PACKAGE_JSON))

    # CI 归档的安装包是无签名的合同测试制品：目录名与机器可读渠道标签都必须
    # 表明"内部测试"属性，防止无签名包被当作客户发行物分发。
    assert "customer-cloud-internal-test-unsigned" in workflow
    assert "RELEASE-CHANNEL.txt" in workflow
    assert f"'{UNSIGNED_CHANNEL_LABEL}'" in workflow
    # workflow 内不允许出现任何签名材料；签名只发生在发布流程脚本中。
    assert "TAURI_SIGNING_PRIVATE_KEY" not in workflow
    assert "certificateThumbprint" not in workflow
    assert "signtool" not in workflow
    # CI 构建命令保持无签名（tauri:build 携带 --no-sign）。
    assert "--no-sign" in package["scripts"]["tauri:build"]


def test_signed_release_flow_exists_and_fails_closed() -> None:
    assert SIGNED_RELEASE_SCRIPT.exists(), "唯一签名发布流程脚本缺失"
    script = _read(SIGNED_RELEASE_SCRIPT)

    # fail-closed：无签名材料或无真实云端 origin 时拒绝出制品。
    assert "VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT" in script
    assert "VITE_API_BASE_URL" in script
    assert "require:customer-api-base" in script
    # 签名输入只经 Tauri 原生 Authenticode 配置注入，脚本全程不得 --no-sign。
    assert "certificateThumbprint" in script
    assert "digestAlgorithm" in script
    assert "timestampUrl" in script
    assert "'sha256'" in script
    assert "--no-sign" not in script
    # 出制品前必须验证 Authenticode 签名有效，否则中止（不产出未签名发行物）。
    assert "Get-AuthenticodeSignature" in script
    assert "Status -ne 'Valid'" in script
    # 发行物必须记录版本、平台、签名与 SHA（唯一客户制品记录）。
    assert "'signed-release'" in script
    assert "'version'" in script
    assert "'platform'" in script
    assert "'windows-x86_64'" in script
    assert "'signature'" in script
    assert "release-manifest.json" in script
    assert "SHA256SUMS.txt" in script


def test_signed_release_flow_is_the_only_release_entrypoint() -> None:
    package = json.loads(_read(PACKAGE_JSON))

    assert package["scripts"].get("release:customer") == (
        "powershell -NoProfile -ExecutionPolicy Bypass -File "
        "scripts/release/build-customer-signed-release.ps1"
    )


# ---------------------------------------------------------------------------
# 自动更新（updater）合同：签名发行物必须自带可自升级的产物。
# 分发是 nginx 静态托管（stable.json + <version>/安装包），不新增服务端路由。
# ---------------------------------------------------------------------------

UPDATER_PUBKEY_PLACEHOLDER = "REPLACE_WITH_UPDATER_PUBLIC_KEY"
UPDATER_MANIFEST_PATH = "/downloads/customer-cloud/stable.json"


def test_customer_conf_and_capability_wire_updater() -> None:
    conf = json.loads(_read(CUSTOMER_TAURI_CONF))

    updater = conf["plugins"]["updater"]
    assert updater.get("pubkey"), "customer overlay 缺少 updater 公钥"
    assert updater.get("endpoints"), "customer overlay 缺少 updater endpoint"
    # CI 构建走占位源（与 VITE_API_BASE_URL 的 staging.example.invalid 同模式），
    # 真实 endpoint 只在签名机的临时 overlay 里注入。
    assert any(
        "staging.example.invalid" in endpoint for endpoint in updater["endpoints"]
    ), "CI 构建的 endpoint 必须是占位源"
    assert all(
        endpoint.endswith(UPDATER_MANIFEST_PATH)
        for endpoint in updater["endpoints"]
    ), "endpoint 必须指向静态升级清单 stable.json"

    # dev（不带 customer overlay 的 tauri:dev）不具备更新能力：主配置不得
    # 出现 updater 配置。
    main_conf = json.loads(_read(TAURI_CONF))
    assert "plugins" not in main_conf, "updater 配置只允许存在于 customer overlay"

    capability = json.loads(_read(UPDATER_CAPABILITY))
    assert capability["windows"] == ["main"]
    assert "updater:default" in capability["permissions"], "缺少 updater 权限声明"

    # Rust 与 JS 两侧的插件依赖缺一不可：缺 JS 包前端调不到，缺 crate 构建失败。
    assert "tauri-plugin-updater" in _read(CARGO_TOML)
    client_dependencies = json.loads(_read(CLIENT_PACKAGE_JSON))["dependencies"]
    assert "@tauri-apps/plugin-updater" in client_dependencies


def test_signed_release_flow_emits_updater_artifacts_and_fails_closed() -> None:
    script = _read(SIGNED_RELEASE_SCRIPT)

    # fail-closed：缺 updater 私钥或公钥仍是占位值时拒绝出制品——那样的包
    # 装出去永远升不了级，等于悄悄废掉整个升级通道。
    assert "TAURI_SIGNING_PRIVATE_KEY is required" in script
    assert UPDATER_PUBKEY_PLACEHOLDER in script
    # updater 制品只在签名机的临时 overlay 里开启（createUpdaterArtifacts），
    # 主配置保持关闭，CI 的无签名构建因此不受影响。
    assert "createUpdaterArtifacts = $true" in script
    # 真实 endpoint 从已通过 require:customer-api-base 校验的云端 origin 推导。
    assert "$($env:VITE_API_BASE_URL.TrimEnd('/'))" in script
    assert UPDATER_MANIFEST_PATH in script
    # 产物合同：.sig 必须存在（缺失即中止），stable.json 记录签名内容与下载地址。
    assert "updater signature was not produced" in script
    assert ".exe.sig" in script
    assert "'stable.json'" in script
    assert "'signature' = (Get-Content -LiteralPath $sigPath -Raw)" in script
    assert "'windows-x86_64'" in script
    # 发行物记录里必须带 updater 段（endpoint 与签名制品的 SHA）。
    assert "'updater' = [ordered]@{" in script
    assert "'endpoint'" in script


def test_desktop_build_workflow_carries_no_signing_material() -> None:
    workflow = _read(DESKTOP_BUILD_WORKFLOW)

    # 无签名内部构建工作流同样不得出现任何签名材料；updater 私钥只存在于
    # 签名机的环境变量里，产物 .sig 由 release:customer 流程生成。
    assert "TAURI_SIGNING_PRIVATE_KEY" not in workflow
    assert "certificateThumbprint" not in workflow
    assert "signtool" not in workflow
