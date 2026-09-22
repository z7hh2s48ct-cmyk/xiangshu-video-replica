# Agent 工作说明（短视频复刻 · 客户版 V3）

> **2026-09-10 用户确认的协作规则**：每次开工先检查任务是否已在开发；每个新任务必须使用独立 worktree；任务完成按既有测试、评审、证据和 PR 要求提交远程；合并并核验后可以清理该任务 worktree。允许不同任务并行，同一任务编号只有一个负责人和在制主分支。本文及[排班与 Worktree 协作清单](docs/客户云版开发顺序排班与Worktree协作清单.md)替代旧文档中“全仓同一时间只开一个任务分支”的规则，业务 DoD 与发布授权不变。

> **每个新任务的强制入口**：先读[任务认领登记](docs/客户云版任务认领登记.md)，执行排班清单 §3 的占用检查与认领，再按 §4 创建 worktree。只读旧账本、只看本地分支或未读取远程结果，都不能证明任务无人开发。发现已占用时继续既有任务的授权交接或选择其他未占用任务，不另开重复分支。

> **分支来源硬规则（用户补充）**：每个新分支均从最新 `origin/main` 创建。前置尚未合入 main 时等待；禁止从功能分支、未合并 PR 或其他任务的工作区快照派生，不能为抢跑依赖创建堆叠任务分支。

> **开工可勾选清单**：[客户云版新任务开工前工作清单](docs/客户云版新任务开工前工作清单.md)。每次按表核对当前准备、实时占用、前置证据、文件/环境边界、main来源和开工记录；该表整理既有要求，不以勾选替代实际检查。

> ~~当前执行清单已更新为[本地实现去重V3](outputs/customer-cloud-convergence-analysis-2026-09-08/v3/客户版收敛剩余任务清单与验收完工标准-V3.md)：57项剩余排程~~ —— **2026-09-22 已废弃**：该 57 项排程与任务账本 §18 都不再作为待办或状态依据，改看各模块自己的进度文档（见「必读正本」）。

> 2026-09-08 PostgreSQL 全面统一增量：用户已确定开发、业务数据库测试、CI、staging、生产均使用 PostgreSQL；SQLite 仅限精确登记的离线历史输入、归档与兼容工具。
> 实施与验收以[唯一数据库规范](docs/PostgreSQL唯一数据库实施与验收规范.md)及 CW-001—060 为准。此前仅客户生产 PG、默认开发 SQLite、SQLite 业务测试可作为当前验收的口径不再适用。
> 本次更新只确认规范和任务定义；原代码仍有 SQLite 分支，历史任务/测试记录保留原文，不据此声明实际迁移或生产切换已完成。

> 面向任何接手本仓库的 AI 开发代理（ChatGPT/Codex 云端、本地 CLI、IDE agent）。
> 完整协作流程见 `docs/ChatGPT网页端开发交接提示词-V3.md`；**任务进度以各模块自己的进度文档为准**（个人中心见「必读正本」），本文件不维护进度快照，V3 任务账本已废弃。

## 项目速览

- 产品：短视频复刻工作台（参考视频上传 → AI 拆解分镜 → 人物库/首帧 → Prompt/批次 → H3 视频生成 → COS 归档质检 → 钱包计费）。
- 两条线：内部 P0 单机版已完成收口（SQLite + 内部身份 + 单机 Worker）；当前主线为客户版 V3（任务 T01–T42）：PostgreSQL 全量迁移 → 激活码与首充 → 两设备单在线 → 用户公平队列 → 多实例生产与灰度。
- 技术栈：Python 3.12 / FastAPI / Alembic / pytest（`server/`）；React 19 / TypeScript 5.9 / Vite 8 / Biome / Vitest（`client/`）；Tauri 2 / Rust（`client/src-tauri/`）；PG 层用 psycopg3 同步驱动（`%s` 占位符）。

## 必读正本（按序，冲突时序号小者优先）

> **2026-09-22 用户决定：V3 版开发计划整体作废**，不再用于判断优先级、完成度或占用。判断依据
> 改为**各模块自己的优化进度**。第 1、3 项已加废弃横幅；4–6 项是业务/测试/证据的**参考材料**，
> 可读，但其中的任务号与排期不作数。
>
> **进度真相源现在是按模块分的**：个人中心模块 →
> `docs/个人中心改版进度与方案勾选表-20260922.md`（该模块自称唯一进度真相源）；非个人中心的开发
> → `.dev-env/non-customer-center-plan.html`。
>
> 工作流本身不变（开工检查、独立 worktree、认领、提交远程、门禁），仍按
> `docs/客户云版开发顺序排班与Worktree协作清单.md` 与 `docs/客户云版任务认领登记.md` 执行；
> 但**不要用其中的任务号、批次与「在制」标签判断占用**——该文件自己也标注 §2 是 2026-09-10 的
> 原始排班、不能继续照其在制标签派发。
>
> **任务是否已合并，一律回 git 查**：`git ls-remote --heads origin`、
> `git merge-base --is-ancestor <sha> origin/main`。账本与模块文档两侧都出过「已合并写成待合并」
> 这种错，文档措辞不能作为判据。

1. ~~`docs/客户版任务清单-V3.md`~~ —— **已废弃**（曾自称唯一任务状态账本；其 §18 停在 2026-09-17）
2. `docs/客户版代码开发清单-V3.md` —— 文件映射与命名冻结（**这条工程约束仍然有效**：新文件名不得自创）
3. ~~`docs/客户版开发计划-V3.md`~~ —— **已废弃**（里程碑与排期）
4. `docs/客户版激活码完整开发文档-V3.md` —— 业务与架构参考
5. `docs/客户版测试与验收规格-V3.md` —— 测试与验收参考
6. `docs/CUSTOMER-TASK-EVIDENCE-V3.md` —— 证据账本

原文保留：业务完成状态曾只记在任务账本 §18；认领状态不能替代验收。历史签认记录保留原样。

`docs/剩余开发工作清单.md`、`docs/Windows内测与运维手册.md` 等为历史快照，不得作为实施依据。

## 标准工作流（每任务）

1. 开工检查：`git fetch origin --prune`；交叉核对**该模块的进度文档**、认领登记、本地/远程任务分支、未合并 PR、`git worktree list --porcelain` 及同仓共享认领记录。认领串行、开发可并行；同编号已有执行者、未合 PR 或不明 WIP 时不得重复认领。按排班清单 §3 登记 Owner、Reviewer、基线 SHA、文件边界、worktree 与测试资源。每个新任务从最新 `origin/main` 创建自己的分支和 worktree（CW 任务：`feat/customer-v3-cwNNN-短横线描述`；历史 T 任务沿用原命名），禁止在他人工作目录切分支或混入他人未提交改动。
2. 测试先行：先写失败测试（红），再实现（绿）。
3. push 前核对提交清单（防并发会话污染任务 PR）：先 `git fetch origin --prune`，再确认 `git log origin/main..HEAD --oneline` 中是且仅是本任务的提交；发现其他会话/他人的提交时停止推送，核对归属并保全原成果。如需重建本任务分支，也必须从最新 `origin/main` 创建，再只迁入本任务已确认的提交，不移动或删除他人成果。严禁将外来提交混入任务 PR——squash 合并后会进入 main。push 后、合并前再核对一次 PR 的 Commits 列表。
4. 按改动范围完成本地验证并提交远程，可先创建 Draft PR 取得 CI 证据；转正式评审/合并前必须满足当前 main 规定的三门禁（secret 扫描 / Linux 质量门 / Windows NSIS）及任务验收。标题 `CW-NNN: <英文摘要>`（历史 T 任务沿用 `TXX`）；受保护 main 只通过正常 PR squash merge，合并由用户或已有明确授权的执行者完成。纯文档维护运行文档/依赖/链接检查与秘密扫描，所需仓库全量门禁交 CI；不得把未运行/失败/跳过的门禁记作通过，也不得改 CI 绕过本任务检查。
5. 同一 PR 内更新**该模块自己的进度文档**（个人中心：`docs/个人中心改版进度与方案勾选表-20260922.md`；V3 任务账本已废弃，不再回填）、`docs/CUSTOMER-TASK-EVIDENCE-V3.md` 登记、`docs/evidence/TXX-EVIDENCE.md`（证据文件统一存放于 `docs/evidence/`，结构参照 `docs/evidence/T06-EVIDENCE.md`，含 §14 模板全文；2026-08-21 M0 评审 M8 起不再放仓库根目录）。
6. 一个 PR 只承载一个任务；评审评论逐条实质修复后 resolve，不得当作流程噪音跳过。
7. 需人工授权的动作（真实 ZPay / 付费 Provider / 生产 COS 变更 / 对外发码 / 灰度扩大 / 公网发布）必须先取得用户明确授权。
8. 多任务并行时，公共账本由集成人在对应 PR 内集中回填，各任务提供独立证据；冲突按真实状态逐项核对，不取 `[x]` 并集。每个 worktree 隔离运行端口和 PG 资源，未隔离的共享 PG 全量测试保持串行。
9. 合并后按排班清单 §6 核验 PR merged、squash SHA 已进入 origin/main、该 main 门禁、全部文件归档和无在用进程。满足条件后由**合并的那个会话**立即从 worktree 外执行 `git worktree remove`，并**一并删除本地分支（squash 合并须 `-D`）和远程分支（`git push origin --delete`）**；PR 页面与 `refs/pull/<N>/head` 永久保留 head，回滚不依赖分支存在。禁止 `--force`、递归删除或以分支存在/PR closed 代替合并证据；`git branch --merged` 对 squash 合并无效，不得作为判据。未合并或有未归档成果时保留 worktree；被取代而不再需要的**未合并**分支，先打注释标签 `archive/<分支名>` 并推送 origin，再删分支。合并 main 只在本任务分支上 `git merge origin/main` 后 push，不另开 `*-pr` 分支或第二个 PR。（2026-09-16 起生效：此前"默认保留分支"的做法曾累积 148 个远程分支、20 个 worktree 与 3 个重复 PR。2026-09-18 起仓库已开启 GitHub 的 Automatically delete head branches，合并后远程 head 分支自动删除，本条的 `git push origin --delete` 只用于历史残留和自动删除未生效的情况。）

## 硬红线（摘要）

- 迁移文件名冻结：`025_postgres_runtime_compatibility` … `030_user_fair_queue`；已发布 revision 只可追加修复，不得篡改。
- 禁止引入 ORM、Redis、消息队列框架；禁止 SQLite/PG 双真源与双写。
- 并行红线：T13 不得早于 T08/T10；T20 不得早于 T19；T21 逐写路由验证 fencing；T25 公平队列 PG-first；T36 不得单实例冒充多实例；T40 真实付费需人工授权。
- 任何真实 API key、激活码明文、设备/session token 不得进入代码、日志、测试夹具或 PR。
- 证据层级逐级推进（`CODE_PRESENT → AUTOMATED_VERIFIED → STAGING_VERIFIED → REAL_CHAIN_VERIFIED → PRODUCTION_GO`）；未过真实链路不得标 `PRODUCTION_GO`。

## 验证命令（分层：开发期快速反馈，任务收尾全量门禁）

> **分层原则**：全量 pytest（账本口径 1446+ 用例，历史全绿耗时约 21–30 分钟）每任务只跑**一次**——收尾提交 PR 前，由第 3 步的 `npm run check` 统一承载（其脚本末尾已包含服务端全量 pytest；不要在第 2 步再单独跑一遍全量，否则一次收尾 = 两次全量）。开发期每轮迭代只跑受影响专项（秒级）。**严禁两个全量 pytest 实例同时打同一个 PG fixture**（各 PG 测试文件有独立库，但 5 个文件共享 `customer_v3_test`，并发会互踩造成假性失败——2026-08-23 T19 实测教训）。注意：`cargo test`、`npm audit`、客户浏览器 E2E 与 `npm run build` 四项**只在 CI 三门禁里执行**，本地 `npm run check` 不含——涉及 Rust/构建/依赖变更时以 CI 结果为准。**并行 pytest 的正确姿势**：不要用两个终端各跑一份全量去打同一个 fixture；用 `bash scripts/ci/run-pytest-shards.sh`（或 `npm run check:sharded`），它给每片拉起**独立 PG 容器**（端口 5433+i，物理隔离，零库名冲突/零共享锁争用），4 片并行把 pytest 段从 ~16min 压到 ~6min（本机实测 2242 passed/1 skipped，与顺序全量逐一致）。

### 本地门禁前置原则（每任务、每 worktree/分支强制，OS 无关）

> **原则**：代码开发完 → CodeReview 自检通过后 → **push/开 PR 之前**，必须在本地把 Linux 质量门跑绿一次。macOS / Linux / Windows(WSL2) 用**同一套命令**（Windows 迁移见 `scripts/ci/self-hosted-runner/provision-windows-wsl2.md`）。

- **必跑**：`npm run check:static`（secret + 前端 + e2e lint + tauri + ruff + ruff-format + mypy）+ `bash scripts/ci/run-pytest-shards.sh`（分片并行 pytest）。二者合起来 = 原 `npm run check` 全集，只是 pytest 改并行。仍可用 `npm run check`（顺序全量）作为等价回退。
- **双路径豁免**：当自托管 runner（`scripts/ci/self-hosted-runner/`）在线时，PR 的 Linux 门就跑在你本机热缓存上——**PR CI 即本地跑**，可不再单独跑第二遍本地全量；runner 离线回退 GitHub 托管时，提交前必须本地跑绿一次。
- 该原则卡在既有并行工作流的「CodeReview 自检（push 前）」→「push→CI」之间，不新增环节，只把本地门禁从建议升级为**强制**。


### 开发期（每任务每轮迭代，快）

```bash
# server/ 目录；CW-007 硬门：fixture 未启动时 PG 套件直接失败（非 skip）。
# 开发期快速反馈可显式放行跳过（不得用于验收证据）：
#   VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1 uv run python -m pytest tests/test_<受影响文件>.py -q
uv run python -m pytest tests/test_<受影响文件>.py -q   # 只跑专项，秒级
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

### 任务收尾（每任务一次，PR 前必须全绿）

两条等价路径，二选一（都等价于 CI Linux 质量门；差异只在 pytest 顺序还是并行）：

**路径 A — 顺序全量（原始，需手动起 fixture）**
```bash
# 1) 先启动 PostgreSQL fixture（Docker PG16，端口 5433；脚本必须带子命令，无参数会打印 usage 并退出 1）
scripts/pg-fixture.sh start
# 2) 服务端专项复验（server/ 目录；全量 pytest 不在这一步跑——由第 3 步统一承载，避免双跑；
#    CW-007 硬门：fixture 未启动时 PG 套件失败而非 skip，缺库伪绿/缺库 skip 均不得声明 AUTOMATED_VERIFIED）
uv run ruff check . && uv run ruff format --check . && uv run mypy app
# 3) 全仓门禁（仓库根目录，覆盖前端/Tauri/服务端全套；脚本末尾含服务端全量 pytest——每任务唯一的一次全量）
npm run check
# 收尾：scripts/pg-fixture.sh stop；DSN 覆盖用环境变量 TEST_POSTGRESQL_URL
```

**路径 B — 静态检查 + 分片并行 pytest（更快，推荐；脚本自管 PG，勿手动起 fixture）**
```bash
# 静态门 + 4 片并行 pytest；每片独立 PG 容器（端口 5433+i），跑完自动清理。
# 注意：不要先跑 scripts/pg-fixture.sh start——默认 fixture 占 5433 会与 shard-0 撞端口。
npm run check:sharded
# 等价拆开：npm run check:static && bash scripts/ci/run-pytest-shards.sh
# 无 Docker 时脚本自动回退单进程顺序跑（等价路径 A 的 pytest 段）。
```


## 环境变量备忘

- PG唯一数据库目标：所有运行环境显式配置`VIDEO_REPLICA_DATABASE_URL`；SQLite/DB_PATH/缺URL/不可达PG拒绝启动，不依赖生产开关。现有代码尚需CW-025/042实现全环境约束。
- 旧`VIDEO_REPLICA_DB_PATH`/内部固定身份仅作迁移识别，不再作为推荐开发路径；业务测试遵循TEST-*分类，SQLite历史例外不得承载当前业务。
- 密钥只进服务端密钥存储（Fernet 加密），永不入库、入码、入 PR。
