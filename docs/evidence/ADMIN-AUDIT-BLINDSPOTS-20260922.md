# ADMIN-AUDIT-BLINDSPOTS-20260922 审计中心三处盲区修复

任务：管理端代码综合评审报告（2026-09-12）§2 P2「审计中心三处盲区」三项。
上游记载：`docs/管理端代码综合评审报告-2026-09-12.md:44`、
`docs/前后端页面功能与接口匹配梳理-2026-09-18.md` 附件「遗留死代码清单」。
基线：`origin/main @ 02ac56a4`；提交 `f4f83874`。

## 1. 三处盲区与处置

| # | 盲区 | 处置 |
|---|---|---|
| 1 | 管理员强制下线只写 `customer_session_events`，不进审计 UNION —— 查不到 | UNION 增第 6 源，只收 actor 非会话属主的行 |
| 2 | `CODE_REVEAL` 与真实事件名不匹配 | 前端补真实事件名选项与族回退标签 |
| 3 | 批次创建不写审计 —— 凭空造码不可追溯 | 按 archive/reveal 先例补 `audit_logs` 行 |

## 2. 关键设计判断

### 2.1 会话审计的收口条件（为什么不是全量并入）

`customer_session_events`（迁移 029）是**域表**，同时记录客户自己的会话流量。
迁移 029 的 `ck_customer_session_events_type` 允许
`ACTIVATED/LOGIN/SWITCH/LOGOUT/TIMEOUT/HEARTBEAT`，而 `HEARTBEAT` 由
`customer_session_service.py:452` 在**每次续租**时写一行。全量并入审计会把
"查审计"淹没在心跳里。

收口条件取 `actor_user_id IS NOT NULL AND actor_user_id <> user_id`，三条边界
一次钉住（判据来源均为写入点，非推测）：

| 场景 | 写入点 | actor | 判定 |
|---|---|---|---|
| 管理员强制下线 | `customer_session_service.py:646-654` | 管理员 | ✅ 进入 |
| 客户自注销 | `customer_session_service.py:526-538` | `user_id`（即 owner） | ❌ 排除 |
| 系统超时清扫 | `customer_session_service.py:355-364` | 不传（NULL） | ❌ 排除 |

**读域表而非只记新吊销是刻意选择**：已发生的吊销本就在 `customer_session_events`
里，读它使历史吊销**无需回填**即可审计；若改为在路由里新写 `audit_logs`，则只能
覆盖此后的吊销。

统一事件名取 `ADMIN_SESSION_<EVENT>`，对齐既有 `ADMIN_DEVICE_<EVENT>` 的
"管理员动作"命名族；`source_document_type='CUSTOMER_SESSION'`，
`source_document_ref=session_id`。

### 2.2 对报告措辞的一处更正

09-12 报告称「'查看激活码明文'筛选值 `CODE_REVEAL` 与实际事件名不匹配（恒为空）」。
核对当前代码：`EVENT_OPTIONS`（`AuditEventsPage.tsx:8-20`）里**根本没有**
`CODE_REVEAL` 选项，它只作为 `eventLabel()` 的一个分支存在，而后端
`admin.activation_code.revealed`（`admin_activation_routes.py:966`）从不产生
该值。因此实际症状不是"筛不到"，而是：**下拉里没有可选项（选不到）、且 reveal
行只能显示成"系统操作"**。修复方向一致，措辞已在提交信息中更正。

顺带发现触发条件：前端筛选是 `<select>`（固定选项表），任何新后端动作都需要同步
补选项，属既有设计约束，本次按此补齐。

## 3. 改动文件

| 文件 | 改动 |
| --- | --- |
| `server/app/admin_audit_routes.py` | UNION 增 `customer_session_events` 源 + 模块 docstring 补该源与收口理由 |
| `server/app/admin_activation_routes.py` | `create_activation_code_batch` 同事务补 `audit_logs` 行 |
| `server/tests/test_admin_audit_routes.py` | 新增 `_insert_session_event` 助手；扩展 UNION 覆盖用例；新增排除性用例 |
| `server/tests/test_admin_activation_routes.py` | 新增批次审计行用例 |
| `client/src/admin/AuditEventsPage.tsx` | 事件选项补真实名；`CODE_REVEAL` 死分支替换为族回退 |
| `client/src/admin/AuditEventsPage.test.tsx` | 夹具改用真实事件名；新增选项与标签用例 |

无迁移、无配置变更、无服务端契约变更。

## 4. 验证结果

- **PG 定向：147 passed**，覆盖「受影响面」六个模块：
  `test_admin_audit_routes` / `test_admin_activation_routes` /
  `test_activation_code_routes` / `test_activation_code_schema` /
  `test_activation_code_service` / `test_account_migration`
  （后四个是新增 `audit_logs` 行的跨模块回归面）。
- **前端全量：114 文件 / 1885 测试通过**（`npm run check --workspace client`
  = biome + tsc + vitest，exit 0）。
- **静态门**：`ruff check`、`ruff format --check`、`mypy`（175 文件）全清。
- 证据层级：**AUTOMATED_VERIFIED（本地自动验证）**。独立评审与远端 CI 未完成。

## 5. 测试环境说明（重要，影响复现）

`server/tests/pg_test_kit.py:22` 顶层 `import fcntl`（POSIX-only），
**Windows 上无法导入**，因此 PG 标记测试在本机不可运行——这与仓库既有认知一致
（`ci.yml` 的 python 门禁跑在 `ubuntu-24.04`）。本次改用 Docker 内的 Linux
工具链（`remediation-quality:20260912`，Python 3.12 + uv）连宿主 PG 5433：

```sh
docker run --rm \
  -v "<worktree>:/app" \
  -v "<scripts>:/probe:ro" \
  -v "uv-cache:/uvcache" \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv \
  -e TEST_POSTGRESQL_URL=postgresql://testuser:testpass@host.docker.internal:5433/customer_v3_test \
  -w /app --entrypoint sh remediation-quality:20260912 /probe/runpg.sh <test paths>
```

`UV_PROJECT_ENVIRONMENT` 必须指向容器内路径：否则会复用/覆盖 Windows 构建的
`server/.venv`。本次运行后已复核该 venv 完好（`Scripts/python.exe` 在、导入正常）。

## 6. 未完成 / 未测试项

- **后端全量套件未跑完**：一次非分片全量在共享主机上运行 23 分钟仍未结束且无法
  观测中间进度，已中止，改为上述"受影响面"定向运行。全量由 PR 的 CI 分片作业
  （`scripts/ci/run-pytest-shards.sh`）兜底。
- 未做真实浏览器验收；本次均为只读查询与既有写路径的审计补写。
- 会话审计收口条件依赖"管理员与客户分属不同身份空间"这一既有前提；若将来出现
  管理员操作**自己**的会话（当前由 `_deny_admin_self_service` 阻断），该行会被
  判据排除——属预期，但值得在引入此类路径时复核。
