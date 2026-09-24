# Windows 迁移：用 WSL2 跑同一套本地门禁（x64）

这套"本地 CI 门禁模拟"方法论的本质是 **在一个 Linux 环境里用 Docker 跑门禁**。
Windows 上最省事、保真度最高的做法 **不是重写脚本**，而是给 Windows 一个 Linux
运行环境：**WSL2（Ubuntu 24.04）+ Docker Desktop（WSL2 后端）**。

> **零脚本改动**：`scripts/pg-fixture.sh`、`scripts/ci/run-pytest-shards.sh`、
> `scripts/ci/self-hosted-runner/*.sh` 都是 Linux bash 脚本，在 WSL2 里 **原样运行**。
> 仓库 `.gitattributes` 已把所有 `.sh` 强制为 LF（`.bat/.cmd/.ps1` 才保留 CRLF），
> 所以 Windows 检出不会把脚本换行符打废。macOS(arm64 VM) 与 Windows(x64 WSL2)
> 共用同一套脚本与同一个 `ci.yml`。

两种用法，任选或都用：
- **A. 纯本地开发环境**：在 WSL2 里直接跑门禁命令（不需要 runner）。
- **B. 自托管 runner**：把 WSL2 注册成 GitHub Actions runner，让 PR 的 Linux 门
  直接跑在你这台 Windows 上（`ci.yml` 的 `select-runner` 在线时自动派发）。

---

## 1. 启用 WSL2 + 安装 Ubuntu 24.04

在 **PowerShell（管理员）**：
```powershell
wsl --install -d Ubuntu-24.04
wsl --set-default-version 2
```
重启后设置 Linux 用户名/密码。进入 WSL2：
```powershell
wsl -d Ubuntu-24.04
```
确认是 x64 Linux：
```bash
uname -m   # x86_64
```

## 2. 安装 Docker Desktop 并接入 WSL2

1. 安装 **Docker Desktop for Windows**，设置里启用 **"Use the WSL 2 based engine"**。
2. Settings → Resources → **WSL Integration** → 打开对 `Ubuntu-24.04` 的集成。
3. 在 WSL2 内验证（`docker` CLI 由 Desktop 提供，无需在 WSL2 内单独装引擎）：
```bash
docker info >/dev/null 2>&1 && echo "docker OK" || echo "检查 Docker Desktop 的 WSL Integration"
```
> 若不想用 Docker Desktop，也可在 WSL2 内装独立 Docker Engine（`setup-runner.sh`
> 会走 `get.docker.com` 分支），但需自行确保 `dockerd` 随 WSL 启动。Desktop 集成最省事。

## 3. 把仓库 clone 到 WSL2 文件系统内（关键，别放 /mnt/c）

**务必 clone 到 WSL2 的 Linux 文件系统（`~/…`），不要放 `/mnt/c/…`**：
- `/mnt/c` 跨 9P 协议，I/O 慢好几倍；
- Windows 侧不支持 Linux 可执行位，`chmod +x` 不生效、脚本跑不起来；
- Docker socket / 文件监听在 `/mnt/c` 下行为异常。

```bash
cd ~
git clone https://github.com/z7hh2s48ct-cmyk/xiangshu-video-replica.git repo
cd repo
```
如果你已经在 Windows 侧（`/mnt/c`）有 clone，建议在 WSL2 内重新 clone 一份到 `~`。

## 4A. 用法 A：纯本地跑门禁（不装 runner）

在 WSL2 的仓库目录内，装一次工具链，然后就能跑与 CI 等价的门禁：
```bash
bash scripts/ci/self-hosted-runner/setup-runner.sh   # 装 Node24/Python3.12/uv/Rust/ffmpeg/Tauri 依赖/Docker
npm ci
uv sync --project server --locked --group dev

# 本地门禁（等价 CI Linux 质量门）：
npm run check:static                    # 静态检查（secret/前端/e2e lint/tauri/ruff/mypy）
bash scripts/ci/run-pytest-shards.sh    # 4 片并行 pytest，每片独立 PG 容器
# 或一把梭：npm run check:sharded
```
`run-pytest-shards.sh` 会自动为每片拉起独立 PostgreSQL 容器（端口 5433+i），
跑完自动清理；无 Docker 时回退单进程顺序跑（等价旧行为）。

## 4B. 用法 B：注册成自托管 runner（让 PR CI 跑在这台 Windows）

```bash
# 1) 工具链（同 4A 第一步）
bash scripts/ci/self-hosted-runner/setup-runner.sh

# 2) 注册：GitHub 仓库 Settings → Actions → Runners → New self-hosted runner 取 token
bash scripts/ci/self-hosted-runner/register-runner.sh
#    按提示粘贴 token（输入隐藏），或一次性：RUNNER_TOKEN=AXXX... bash .../register-runner.sh

# 3) 常驻
bash scripts/ci/self-hosted-runner/runner-service.sh start
bash scripts/ci/self-hosted-runner/runner-service.sh status   # -> ONLINE
```
runner 标签为 `self-hosted, linux, X64, video-replica`（arch/OS 由 runner 自动附加）。
`ci.yml` 的 `select-runner` **只匹配自定义标签 `video-replica`、不写死 arch**，所以
这台 x64 Windows/WSL2 runner 与 macOS 的 arm64 VM runner **可被同一套 workflow 选中**，
谁在线用谁。

> **启用自动派发（一次性只读 PAT secret）**：`select-runner` 要列出仓库自托管 runner，
> 这个 REST 调用需要 **Administration(read)** 权限，而 workflow 的 `GITHUB_TOKEN` **没有**
> 该权限（`administration` 也不是合法 `permissions:` 键）；缺 token 时它 403 并**干净回退**
> GitHub 托管 `ubuntu-24.04`（你的 runner 不会被用到）。要让 PR CI 真派发到本机，一次性加：
> 1) 建 **fine-grained PAT**：仓库访问只勾本仓库，权限只给 **Administration: Read-only**；
> 2) 仓库 Settings → Secrets and variables → Actions → New repository secret，
>    名字 `SELF_HOSTED_RUNNER_READ_PAT`，值填该 PAT。
> `ci.yml` 用 `${{ secrets.SELF_HOSTED_RUNNER_READ_PAT || github.token }}`：设了就自动派发，
> 没设就始终走托管（仍正确，只是不在本机跑）。token 只读、限本仓、绝不打印/落盘/入库。

> **WSL2 生命周期注意**：`wsl --shutdown` 或 Windows 重启会停掉 WSL2 里的 runner。
> 需要接 PR 任务时，先 `wsl -d Ubuntu-24.04` 进环境再 `runner-service.sh start`。
> WSL2 若启用了 systemd（`/etc/wsl.conf` 里 `[boot] systemd=true`），可用
> `runner-service.sh install-systemd` 让它随 WSL 启动常驻。

---

## 与 macOS 路径的差异一览

| 环节 | macOS | Windows |
|---|---|---|
| Linux 环境 | OrbStack/Lima VM（arm64） | WSL2 Ubuntu 24.04（x64） |
| Docker | VM 内 Engine / OrbStack 共享 | Docker Desktop + WSL Integration |
| 仓库位置 | VM 内 `~/repo` | WSL2 内 `~/repo`（**不要** `/mnt/c`） |
| runner 标签 | `self-hosted,linux,ARM64,video-replica` | `self-hosted,linux,X64,video-replica` |
| bash 脚本 | 原样 | **原样**（`.gitattributes` 已保 LF） |
| ci.yml | 同一份 | 同一份（arch 无关） |

## 逃生阀（与 macOS 一致）

- `runner-service.sh stop` → 新 PR 自动回退 GitHub 托管 `ubuntu-24.04`。
- 仓库变量 `CI_FORCE_HOSTED=true` → 不停 runner 也强制走托管。
- 仓库变量 `CI_PYTEST_SHARDS=N` → 按本机核数调分片数（脚本据 `test-durations.json` 重新均衡）。

## 安全

`ci.yml` 每个 job 都保留同仓 PR 守卫
（`github.event.pull_request.head.repo.full_name == github.repository`），
fork PR 绝不会把不可信代码派发到你的自托管 runner。注册 token 仅交互式使用、
用后即由 `config.sh` 消费，脚本不落盘、不打印、不写进仓库或历史。
