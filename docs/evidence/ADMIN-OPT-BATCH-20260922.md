# ADMIN-OPT-BATCH-20260922 管理端优化批量（评审 P2/P3 落地）

任务：管理端代码综合评审报告（2026-09-12）与匹配梳理（2026-09-18）管理端条目的批量落地。
依凭：`docs/管理端优化进展与待办-2026-09-22.md`（本线的状态基准）。
分支：`feat/admin-optimization-batch-20260922`；PR **#206**；基线 `origin/main @ 2a6efbb2`（已并入 `39a42c36`）。

---

## 1. 交付清单

### 1.1 评审 P2/P3 修复

| 项 | 处置 | 关键判据 |
|---|---|---|
| 费率页「承诺二次确认却直接落库」（P2） | 拆为「保存」=校验 → 确认框「确认并保存」=落库；框内摊开售价/成本/收费状态 before→after 与审计将记录的原因；失败留框内可就地重试并复用幂等键 | `standard` 级：该写入的审计原因由页面按科目名自动生成，额外要求手填会改变既有审计内容 |
| 总览未展示待结算/收入未确定（P3） | 按真实口径加两个待办行并可跳资金流水 | `pending_count` = PENDING 计费操作数（`billing_reports.py:174`）；`unknown_revenue_count` = 收入未确定数（`:322`） |
| `selfCheckWechatNative` 缺 CSRF（既有生产缺陷） | 改走 `adminWrite` | 控制面 POST 强制 CSRF（`admin_auth_routes.py:776-779`），裸 `requestControl` 生产必 403；服务端零改动（该路由只读、不消费写契约） |
| 单例运行时设置 upsert 非原子（P3） | 两处改为 `INSERT ... ON CONFLICT (id) DO UPDATE`（`viral_runtime_controls` / `runtime_settings`） | 原「UPDATE 看 rowcount 补 INSERT」在并发首写下双双 INSERT 撞主键 → 500 |
| 平台名与词典分叉（P3） | `SessionsPage` / `CustomerDeviceSection` 改走共享 `PLATFORM_LABELS` | 前者私刻字典只列 windows/macos/linux，**ios/android 显示英文裸码** |
| `const [pageSize] = useState(20)` 伪状态 ×3（P3） | 提为模块常量 `PAGE_SIZE`，并从依赖数组移除 | 放进 useState 却从不改它 |
| 跨路由借 logger（P3） | 改为本模块 `logging.getLogger(__name__)` | 借来的 logger 打别模块的名字，日志来源被误标 |
| ConfirmDialog 焦点圈闭（P3 a11y） | 首尾回绕圈闭，查询范围限定在 dialog 内 | `aria-modal="true"` 只是承诺，只加属性并不拦 Tab |
| 总览对账计数与资金页口径不一致（P2） | 总览改逐桶比，与 `billing_reconciliation` 同形 | 原比「两桶之和」，桶间搬移两边的和仍相等 → **漏报** |
| 三个容器页无测试（P3 测试缺口） | 补接线测试（见 1.3） | 九个区块各自已有测试，真正无覆盖的是容器这层 |

### 1.2 分页样板收敛（A11，独立 worktree 产出后整合）

`LIMIT %s OFFSET %s` **26 处 / 16 模块**收敛为 `server/app/sql_pagination.py`
（`PAGE_CLAUSE` + `page_bounds`）。只共享子句文本与绑参顺序；**各端点的上限/下限/
计数口径/响应信封全部留在原地**（统一上限即改行为，红线不允许）。三处特殊形状只换
字面量不动结构（`materials` 可选分页、`simple_character` 的 `limit+1` 游标、
`studio_search` 的 page/page_size）。新增 `tests/test_sql_pagination.py` 并按 CW-061
守卫补登记 `test-durations.json` 与 `shard-2.txt`。

### 1.3 审计端点 COUNT 评估（A12，零行为改动）

**推翻评审前提**：实测 6 表 85.5 万行 / UNION 45.5 万行下，COUNT **38–79 ms**，而
同一数据的**列表查询 314–766 ms** —— 最慢的是列表不是 COUNT。且 COUNT 与列表同
FROM、同 `LEFT JOIN users tu`、同一个 `where` 字符串，行集恒等（**非正确性缺陷**）。
已否决 `COUNT(*) OVER ()`、`MATERIALIZED` CTE、去掉多余 LEFT JOIN 三种改法。唯一有
价值的是第 3 源 partial index，需新迁移 + 8 处 head 常量同步，故附**触发线**后不做。
完整测算见 `ADMIN-AUDIT-COUNT-A12-20260922.md`。

### 1.4 文档

- 新增 `docs/管理端优化进展与待办-2026-09-22.md`（本线状态基准，含销项清单与边界）
- `docs/客户版任务清单-V3.md` 加顶部**废弃标记**（不删除：保留 92 条已完成项追溯，
  且被 `CUSTOMER-TASK-EVIDENCE-V3` 按「任务账本§18」引用）
- 保留 `docs/decisions/DEVICE-CAPACITY-POLICY-20260922.md`（任务书 D 决策记录）

---

## 2. 验证

| 项 | 实测 |
|---|---|
| 客户端全量（合并 main 后） | **124 文件 / 1971 用例通过**（biome + tsc + vitest，exit 0） |
| PG 定向：审计/加载/仪表盘/分页 | **66 passed** |
| PG 定向：`test_admin_session_routes` + `test_admin_dashboard_routes` | **44 passed** |
| PG 定向：迁移链守卫（`test_migration_guard_manifest` + `test_postgres_migrations`） | **43 passed** |
| 分片覆盖守卫（CI fail-closed） | `build-test-shards.py --check-coverage` → **143 文件全覆盖** |
| 静态门 | `ruff check` / `ruff format --check`（450 文件）/ `mypy`（183 文件）全清 |

**红→绿验证过三项**（不是推断）：
1. 并发首写：旧实现下稳定复现 `UniqueViolation: "runtime_settings_pkey"`
2. 对账逐桶比：旧口径下 `assert 0 == 1`（桶间搬移漏报）
3. 焦点圈闭：去掉 `trapFocus` 调用后该用例在断言处变红

**等价性证明**（A11）：AST 渲染每个文件全部字符串字面量后比对 HEAD vs 工作区，
16/16 一致、26 条子句；带负对照（人为多 3 空格 → MISMATCH）证明校验器不恒真。

证据层级：**AUTOMATED_VERIFIED**（本地自动验证）。独立评审、远端 CI 与真实浏览器验收未完成。

---

## 3. 未测试 / 边界

1. **后端全量套件未整体跑**（3094 条）。A11 跑了约 40 个受影响模块；本批其余改动按
   模块定向验证。未跑的重模块：`cw030_worker_pg_matrix` / `recharge_packages` /
   `cw078_api_keys` / `customer_account_security` / `customer_registration` 等。
2. **容器镜像缺 ffmpeg**：音频/视频探测类用例在本机 fixture 阶段即 error（A11 侧
   `test_oral_domain` 37 passed / 45 errors；`test_cw058` 6 failed）。已用**基线 A/B**
   证明同样失败存在于改动前（`git archive 2a6efbb2` 重跑，失败逐字同名）——属 harness
   环境缺口，非本批引入，但这些用例在本机拿不到绿。
3. **A11 落点超出管理端**：另触及 `oral.py` / `materials.py` / `wallet_routes.py` /
   `studio_search.py` / `viral_collection_billing.py` / `provider_gateway_routes.py`。
   这是该 P3 的真实范围（分页样板是服务端通病），如需拆分为独立 PR 可拆。
   `provider_gateway_routes.py` 那 1 处在 `server/tests/` 无测试模块 import，其不变性
   仅由 AST 证明 + mypy 兜底。
4. 未做真实浏览器验收；`test_cw061_shard_coverage_guard` 之外的 CI 元数据未单独复验。
5. 认领登记已回填（本批与上一轮六个任务）；任务账本不再回填（该账本已废弃）。

---

## 4. 与本线原始判断不同之处（避免后人重复排查）

| 原判断 | 核实结果 |
|---|---|
| provider 密钥 reveal「指向旧前缀且管理端未接」 | **是已执行的策略**，非缺陷（控制面本不可读 provider 明文密钥） |
| `CustomersPage` 的 `aria-expanded="false"` 写死 | **非缺陷**：展开时整个列表被详情视图替换，值恒为准确 |
| `control_routes.py` 的 `sqlite3.Row` 残留注解 | **非残留**：`get_database` 明写 SQLite 泳道服务内部/桌面，注解准确 |
| `Pagination` 的 `hasMore` 零调用 | **刻意保留**的能力（不伪造总数），只更正过时注释 |
| 「COUNT 全 UNION 重算是审计表涨大后最慢读」 | **实测不成立**：最慢的是列表；且 COUNT 行集与列表恒等 |
| 「`control_routes.py` 单文件 13 次分页样板」 | **不成立**：该文件 3 次；精确是 26 子句 / 16 模块 + 9 边界拷贝 |
| 「费率页无二次确认」「资金导出不带筛选」「审计三处盲区」 | 均已在更早 PR 修掉（#199 等），本批不复改 |
