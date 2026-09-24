# 开发导航

先从[项目说明](../../README.md)了解启动和验证方式，再按下面的业务入口阅读代码。此页按现有实现组织导航。

## 目录职责

| 目录 | 内容与使用方式 |
| --- | --- |
| [client](../../client/) | React 客户工作台、管理端、Tauri 桌面外壳 |
| [server](../../server/) | FastAPI 路由、业务服务、Worker、数据库迁移及后端测试 |
| [e2e](../../e2e/) | 跨前后端的浏览器验收 |
| [scripts](../../scripts/) | 开发环境、检查、CI 与发行辅助脚本 |
| [deploy](../../deploy/) | 容器、nginx、systemd 与发布回退配置 |
| [docs](../adr/) | 架构决策记录（ADR）与决策记录 |
| [video-reverse-prompt-script-firstframe](../../video-reverse-prompt-script-firstframe/) | 独立视频拆解 Skill 源码；不属于在线后端 |

使用 VS Code 等兼容编辑器时，可打开仓库根目录的 [video-replica.code-workspace](../../video-replica.code-workspace)，分栏浏览前端、后端、文档、脚本、部署与 E2E。所有路径相对仓库，支持任意独立 worktree。

## 前端怎么读

| 关注点 | 起点 |
| --- | --- |
| 客户入口、登录与会话 | [main.tsx](../../client/src/main.tsx) → [RootApp.tsx](../../client/src/RootApp.tsx) → [customer](../../client/src/customer/) |
| 工作台导航与页面组合 | [studio/StudioWorkspace.tsx](../../client/src/studio/StudioWorkspace.tsx) |
| 项目、拆解与分镜工作区 | [App.tsx](../../client/src/App.tsx)、[AnalysisWorkspace.tsx](../../client/src/AnalysisWorkspace.tsx)、[ProjectDetailFlow.tsx](../../client/src/ProjectDetailFlow.tsx) |
| 人物库与场景形象 | [CharacterLibrary.tsx](../../client/src/CharacterLibrary.tsx)、[CharacterScenePanel.tsx](../../client/src/CharacterScenePanel.tsx) |
| 视频生成、参数与结果 | [GenerationComposer.tsx](../../client/src/GenerationComposer.tsx)、[GenerationLauncher.tsx](../../client/src/GenerationLauncher.tsx)、[VideoResultStage.tsx](../../client/src/VideoResultStage.tsx) |
| 管理端 | [admin-main.tsx](../../client/src/admin-main.tsx) → [AdminApp.tsx](../../client/src/AdminApp.tsx) → [admin](../../client/src/admin/) |
| API 请求与类型 | [api.ts](../../client/src/api.ts)、[api.admin.ts](../../client/src/api.admin.ts)、[generated/api.ts](../../client/src/generated/api.ts) |
| 桌面系统能力 | [src-tauri/src/lib.rs](../../client/src-tauri/src/lib.rs)、[下载](../../client/src-tauri/src/video_downloads.rs)、[发布账号](../../client/src-tauri/src/publish_accounts.rs) |

组件测试与组件同目录，名称为 `*.test.ts` / `*.test.tsx`；公共测试设置在 [client/src/test](../../client/src/test/)。`src/generated/api.ts` 由 OpenAPI 生成，不手改。界面展示资产放 `client/public/` 或 `client/src/assets/`。

## 后端怎么读

先看 [app/main.py](../../server/app/main.py) 的应用启动与路由注册，再读对应 `*_routes.py`，沿调用进入业务模块；测试在 [server/tests](../../server/tests/)。

| 业务 | 路由与业务实现 |
| --- | --- |
| 参考视频拆解 | [analysis_routes.py](../../server/app/analysis_routes.py)、[analysis.py](../../server/app/analysis.py) |
| 人物、首帧与提示词 | [character_routes.py](../../server/app/character_routes.py)、[first_frames.py](../../server/app/first_frames.py)、[h3_prompts.py](../../server/app/h3_prompts.py)、[prompt_rules](../../server/app/prompt_rules/) |
| 生成与异步执行 | [generation_routes.py](../../server/app/generation_routes.py)、[generation.py](../../server/app/generation.py)、[generation_worker.py](../../server/app/generation_worker.py) |
| 数字人口播 | [oral_routes.py](../../server/app/oral_routes.py)、[oral.py](../../server/app/oral.py)、[oral_worker.py](../../server/app/oral_worker.py) |
| 发布与交付 | [publish_routes.py](../../server/app/publish_routes.py)、[publish_delivery.py](../../server/app/publish_delivery.py)、[publish_worker.py](../../server/app/publish_worker.py)、[publishers](../../server/app/publishers/) |
| 采集与文案 | [viral_collection.py](../../server/app/viral_collection.py)、[script_from_audio.py](../../server/app/script_from_audio.py)、[script_rewrite.py](../../server/app/script_rewrite.py) |
| 身份与会话 | [customer_auth.py](../../server/app/customer_auth.py)、[customer_session_service.py](../../server/app/customer_session_service.py)、[customer_fence.py](../../server/app/customer_fence.py) |
| 钱包与计费 | [wallet_routes.py](../../server/app/wallet_routes.py)、[usage_billing.py](../../server/app/usage_billing.py)、[billing_meter.py](../../server/app/billing_meter.py) |
| 数据、配置与媒体 | [db_pg.py](../../server/app/db_pg.py)、[settings.py](../../server/app/settings.py)、[storage.py](../../server/app/storage.py) |

数据库迁移保留在 [server/migrations](../../server/migrations/)，已发布迁移不改名。离线维护脚本在 [server/scripts](../../server/scripts/)，适用范围见各脚本自身的说明。

## 开发入口与资料归位

命令在仓库根目录运行，定义以 [package.json](../../package.json) 为准：

| 工作 | 命令或入口 |
| --- | --- |
| 安装前端依赖 | `npm ci` |
| 启动前端 | `npm run dev:client` |
| 启动 API | `npm run dev:server` |
| 启动生成 Worker | `npm run dev:worker` |
| 启动发布 Worker | `npm run dev:publish-worker` |
| 本地静态与测试门 | `npm run check:static`；完整要求见 [AGENTS.md](../../AGENTS.md) |
| 隔离 PostgreSQL 全量验证 | `npm run check:sharded`；资源清理遵守工作区 AGENTS.md |
| 发布前检查 | [customer_release_preflight.py](../../scripts/customer_release_preflight.py)；不是启动开发环境的命令 |
| CI 与发行辅助 | [scripts/ci](../../scripts/ci/)、[scripts/release](../../scripts/release/) |

新资料按用途放置：架构与决策记录在 `docs/`，本地临时截图和日志在被忽略的 `output/`。

业务源码路径、构建入口和迁移名称保持既有合同。
