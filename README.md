# 短视频复刻工作台（xiangshu-video-replica）

AI 短视频复刻生产工作台：参考视频上传 → AI 拆解分镜 → 人物库与首帧 → Prompt/批次编排 →
视频生成 → 成片直链交付 → 按条计费。业务后端 FastAPI，桌面端 Tauri 2，生产数据真源 PostgreSQL 16。

阅读入口：[开发导航与代码地图](docs/development/README.md) · [架构决策记录](docs/adr/) ·
[决策记录](docs/decisions/) · [编辑器工作区](video-replica.code-workspace)。

## 核心能力

- **激活码体系**：批次/发放/暂停/作废/归档管理，CSPRNG 生成 + HMAC key version，掩码展示与受控明文揭示审计，
  私有对象存储密文导出，防枚举与多实例共享限流，AEAD 幂等恢复。
- **钱包计费**：任务 `RESERVE → SETTLE/RELEASE` 终态原子结算，支付回调原子入账，管理端双确认调账，
  append-only 账本与审计。
- **设备与会话**：设备注册与单在线切换（session epoch fencing），撤销传播与旧会话拒绝，全程审计。
- **用户公平队列**：按用户轮转、每用户默认并发 1，Worker 崩溃恢复与 Provider 提交不确定的人工核验。
- **管理端**：独立构建的管理控制台，per-operator 账号密码 + CSRF，职责分离（管理员不可给自己调账/改价），
  auditor 只读合规审计。
- **生产运维**：`/health` `/live` `/ready` 探针，结构化请求日志与 request id，集群异常探针，
  PG16 物理备份 + PITR 恢复演练脚本，滚动发布与失败回滚。
- **桌面端本地缓存**：源站素材由客户端直取并本地缓存（多线程分段下载、视频号本地解密），
  播放与文案提取都在本地文件上进行——音频抽取在客户端完成后再上传转写，平台不需要留存原片。

## 架构与技术栈

- **`server/`** — Python 3.12 · FastAPI · Alembic · psycopg3（同步驱动，`%s` 占位符）· pytest ·
  Ruff · mypy strict。
- **`client/`** — React 19 · TypeScript 5.9 · Vite 8 · Biome · Vitest；API 类型由 FastAPI OpenAPI 生成
  （`npm run generate:api`，产物不手工修改）。业务工作台与管理端共用同一 React 构建。
- **`client/src-tauri/`** — Tauri 2 / Rust 桌面端。桌面端不启动任何本地业务后端，只连接远程 HTTPS API；
  ffmpeg/ffprobe 随安装包分发（LGPL 构建，见 `client/src-tauri/resources/ffmpeg/README.md`）。
- **存储** — 生产使用腾讯云 COS 私有桶（启动即校验，缺/错配置 fail-closed）；开发机可回退本地文件系统存储。
  成片只持久化供应商结果直链，不下载、不转存。
- **外部 Provider** — 视频拆解（Gemini）、人物图片（GPT Image 2 / Nano Banana）、视频生成（Metaso H3）、
  支付（ZPay）、爆款内容数据源（TikHub）。开发联调可切 `fake_h3` 模拟链路，不触达付费接口。

## 目录结构

```text
client/                 React 工作台与管理端
  src/                  界面、API 适配与相邻组件测试
  src-tauri/            Tauri 桌面外壳（Rust）
server/                 FastAPI 业务后端
  app/                  路由、业务服务与 Worker
  migrations/           Alembic 迁移
  tests/                服务端测试
  scripts/              离线维护与兼容工具
e2e/                    浏览器端到端验收
tests/                  根级测试
scripts/                开发环境、质量检查、CI 与发行辅助
deploy/                 部署、运维与发布回退配置（compose / systemd / nginx / PG PITR）
docs/
  adr/                  架构决策记录
  decisions/            决策记录
  development/          开发入口与代码阅读地图
video-reverse-prompt-script-firstframe/   独立视频拆解 Skill 源码
```

按业务查找文件请用[开发导航](docs/development/README.md)。

## 环境要求

- Node.js 24+、Rust stable（Windows 使用 MSVC toolchain）、Python 3.12+、uv。
- Windows 桌面构建另需 Microsoft C++ Build Tools 与 WebView2（Tauri 2 官方要求）。
- PG 集成测试需 Docker（`scripts/pg-fixture.sh`，PG16，固定端口 5433）；浏览器 E2E 另需系统 Chrome 与 ffmpeg。

## 快速开始

```bash
npm install
uv sync --project server --locked
```

准备一个隔离的 PostgreSQL 16 开发库并显式设置 `VIDEO_REPLICA_DATABASE_URL`（除非在管理设置里明确切到
本地存储，业务数据库只有 PostgreSQL 一个真源，缺 URL 或连不上即拒绝启动），把 schema 升级到当前 head，
再启动 API 与 Worker。**不要把生产 DSN 用于开发或测试。**

- 环境变量样例见 [`.env.example`](.env.example)；PG 测试夹具用 `scripts/pg-fixture.sh`，开发库与测试库必须隔离。
- 只调试浏览器界面时运行 `npm run dev:client`。
- 开发身份 Header 只在 Vite 开发构建中发送；生产构建即使误设 `VITE_DEV_USER_ID` 也会忽略。
- Provider API Key 与云存储凭据经 Fernet 加密写入数据库（Windows 下主密钥由当前用户 DPAPI 保护，
  macOS 使用钥匙串），启动时校验可解密。任何真实密钥不得进入代码、日志或 PR。
- 视频拆解模型可在「模型服务」设置中配置：`analysis_model` 留空使用服务端默认主模型；
  `analysis_model_fallbacks` 留空使用内置备选，填写一个模型名可替换内置备选，填写 `disabled` 可关闭自动切换。
  为控制一次任务的耗时和费用，最多允许一个备选模型；超量配置会被拒绝。
  仅当主模型返回 HTTP 429 或明确报告模型不可用的 HTTP 400/404 时切换，网络超时及其他错误保留原失败结果。

## 验证与门禁

开发期每轮迭代只跑受影响专项（秒级）：

```bash
# server/ 目录
uv run python -m pytest tests/test_<受影响文件>.py -q
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

收尾提交前跑一次全仓门禁（等价于 CI 的 Linux 质量门）：

```bash
npm run check:static            # secret 扫描 → 前端 Biome/tsc/vitest → e2e lint → Tauri cargo → Ruff/mypy
npm run check:sharded           # 四分片并行 pytest，每片一个独立 PG 容器
# 等价顺序回退：scripts/pg-fixture.sh start && npm run check && scripts/pg-fixture.sh stop
```

- 服务端全量 pytest 每任务只跑一次，由收尾门禁统一承载，不要在前面单独重复执行。
- 不要用两个全量 pytest 实例同时打同一个 PG 夹具（共享测试库会互踩造成假性失败）。
- `cargo test`、`npm audit`、客户浏览器 E2E 与 `npm run build` 只在 CI 三门禁执行，本地门禁不含；
  涉及 Rust / 构建 / 依赖变更时以 CI 为准。
- 客户端单独验证：`npm run check --workspace client`（Biome + tsc + Vitest）。

## 构建与发布

```bash
npm run tauri:build            # 客户云版 NSIS，强制非 loopback HTTPS API origin
npm run test:customer-e2e      # 客户浏览器 E2E（激活 / 设备配对 / 充值）
```

- 发布前必须运行 `scripts/customer_release_preflight.py` 并得到 `T45_PREFLIGHT_OK`。
- 部署模板见 [`deploy/`](deploy/)（nginx / systemd / PG PITR 脚本 / 环境变量样例），
  `deploy/customer-git-rollout.sh` 从明确的 40 位提交拉取代码、本机构建前端并滚动更新 API 与 Worker，
  失败自动恢复旧镜像与静态站点。
- 桌面安装包由 `.github/workflows/desktop-build.yml` 构建（Windows 未签名，macOS 为 ad-hoc 签名且未公证）。

## 工程红线

- 禁止引入 ORM、Redis、消息队列框架；禁止 SQLite/PG 双真源与双写。
- 已发布的 Alembic revision 只可追加修复，不得篡改。
- 任何真实 API key、激活码明文、设备/session token 不得进入代码、日志、测试夹具或 PR。
- 证据层级逐级推进：`CODE_PRESENT → AUTOMATED_VERIFIED → STAGING_VERIFIED → REAL_CHAIN_VERIFIED →
  PRODUCTION_GO`；未过真实链路不得标 `PRODUCTION_GO`。

## 文档索引

| 文档 | 用途 |
| --- | --- |
| [`docs/development/README.md`](docs/development/README.md) | 开发入口与代码阅读地图 |
| [`docs/adr/`](docs/adr/) | 架构决策记录（迁移链守卫、用户公平队列） |
| [`docs/decisions/`](docs/decisions/) | 决策记录（支付 SDK 选型、设备容量口径） |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 分支、提交信息与门禁约定 |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本更新日志 |
| [`AGENTS.md`](AGENTS.md) | 面向 AI 开发代理的工程约定 |

## 许可证

保留所有权利。源代码可见但未授予任何使用、复制、修改或再分发许可，详见 [LICENSE](LICENSE)。
仓库内含第三方组件（Lucide 图标、social-auto-upload 等）与随包分发的 LGPL ffmpeg 构建，
各自遵循其原始许可，见对应目录下的 LICENSE 文件。
