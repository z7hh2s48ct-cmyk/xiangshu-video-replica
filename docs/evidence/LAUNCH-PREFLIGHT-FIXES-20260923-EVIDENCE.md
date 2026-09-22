# LAUNCH-PREFLIGHT-FIXES-20260923 · 上线前检查 HIGH 修复批次证据

日期：2026-09-23。分支：`fix/launch-preflight-fixes-20260923`（基线 `origin/main@aa1a6ab0`，#216 CI 双绿）。
worktree：`.worktrees/LAUNCH-PREFLIGHT-FIXES-20260923`；共享 claim：主仓 `.git/codex-task-claims/LAUNCH-PREFLIGHT-FIXES-20260923/claim.json`。

## 来源

2026-09-23 上线前四路独立评审（资金 / 认证权限 / 业务功能 3 路 code-reviewer + 1 路 architect 魔鬼代言人）对 09-21→09-23 合入 main 的 45 提交（server/ 155 文件 +26,944/-1,324）给出 REQUEST CHANGES / WATCH。本批承接其中 4 条 HIGH 与 1 条部署面风险：

| # | 发现 | 修复 | 文件 |
| --- | --- | --- | --- |
| H-1 | 折扣生效判断混用时钟域（应用墙钟 vs DB clock_timestamp 写入的 valid_from），充值后立即消费会按原价多扣 | `retail_snapshot` 同连接 `SELECT clock_timestamp()` 作 `at_time` 传入 `get_best_discount`（SES-01 同源） | server/app/billing_catalog.py |
| H-2 | API-Key 泳道不查母账号存活性与激活码状态，组织封禁被子账号 Token 绕过、继续扣冻结钱包 | `_verify_api_key_in_transaction` owner 查询单 SQL 扩展：`parent_ok`（LEFT JOIN 母账号 is_active=1，孤儿 fail-closed）+ `codes_ok`（NOT EXISTS BOUND 设备挂非 ACTIVE 码） | server/app/customer_fence.py |
| H-3 | viral 刷新用「ID 当关键词跑抖音通用搜索」模拟：不校验返回 video_id、忽略 platform → 无关视频入库 + 错误扣费；且 `viral_search_refresh` 无 reconcile 兜底，崩溃后预留永久冻结 | 非抖音平台预留前 422；上游结果按 video_id 精确匹配，不匹配走既有释放路径 503；reconcile searches 查询扩为 `service IN ('viral_search','viral_search_refresh')` | server/app/viral_search_refresh_routes.py、server/app/usage_billing.py |
| H-4 | `viral_search`/`viral_search_refresh` 资费无种子，漏配（或 0 价+启用，评审 M-1）即 0 积分免费外呼 TikHub | 新增 `require_priced_viral_service` 守卫：tariff 缺失 / 未启用 / 单价 0 一律 503（`VIRAL_SEARCH_UNPRICED` / `VIRAL_SEARCH_REFRESH_UNPRICED`） | server/app/viral_search_routes.py（两路由预留前调用） |
| 部署 | `20260923T0000` 在同一发布列车 DROP `runtime_settings.h3_extended_modes_enabled`，旧镜像仍 SELECT 该列：MIGRATE→ROLL 混合窗口 500、镜像回滚后持续 500（破坏 `DATABASE_HEAD_LEFT_FORWARD_COMPATIBLE` 前提） | 新迁移 `20260923T1800_re_add_h3_extended_modes_rollout_compat` 重加列（Boolean NOT NULL default FALSE，与 075 逐字段一致）；新代码零读者，源码级回归锁钉住 | server/migrations/versions/20260923T1800_*.py |

## 独立评审（修复后）

- code-reviewer 车道（只读）：初评 REQUEST CHANGES，唯一阻塞 M-1（守卫漏 `unit_credits=0`）已修（谓词补 `not tariff.unit_credits` + 三态测试：缺失/停用/0价）；M-2（API-Key 泳道激活码封锁半径严于会话泳道）经双方车道核对：现行 schema `uq_activation_codes_bound_user_current` 钉死一用户一码，两泳道当下行为等价，采保守口径并在 docstring 写明该不变量依赖；L-1（422 次序）已调整为幂等键校验之后；L-2（位置索引）与文件既有风格一致，不改。
- architect 车道（魔鬼代言人）：总评 WATCH（无 BLOCK）。三条低成本显式化已落实：①fence 注释写明「一用户一码」不变量 + 拒绝路径 warn 日志；②③见下「登记的后续项」。
- 配套测试面修正：`test_search_flags_has_copy_on_cache_hit` 原以 0 价种子凑 charged=0——代码并无缓存命中免费分支（`hasCopy` 是信息性标记），已改为真实价 3 并断言照常计费。

## 验证门禁（本 worktree 实跑）

- 静态：`ruff check .` ✓、`ruff format --check .` ✓（459 文件）、`mypy app` ✓（182 文件 0 错误）。
- 专项红→绿：Fix1（旧实现红 / 新实现绿，`test_retail_snapshot_reads_discount_validity_at_db_clock`）；Fix2（旧实现两用例红 / 新实现绿）。
- 专项套件：test_cw078_api_keys 34/34、test_viral_search_pg 30/30（含新增 5 用例：平台拒绝、错配不入库不扣费、崩溃预留释放、搜索未定价三态、刷新未定价）、test_discount_wiring 21/21、test_cw056_supported_head_matrix 12/12、test_postgres_migrations 29/29、independent_creation + activation/registration/sub_account schema 106/106、devices/permissions/quota 补 bump 后 127/127。
- 全量分片（评审修复后终跑，`CI_SHARD_BASE_PORT=5601` 独立容器四片并行）：结果见下节「最终门禁记录」。
- 迁移：manifest `--record` 后 `--check` OK（heads=[20260923T1800]、parents/revision_count=120 一致）；cw056 schema 冻结计数 columns 1220→1221、digest 以 postgres:16 探针重算（`c9dcb178f65a9d229c6646d9ed65cd117d8a1b3b2352e9e8fb5769230c5c142b`）；8 个 head 断言测试文件全部 bump。
- 秘密扫描：`bash scripts/verify_no_secrets.sh` ✓（无硬编码秘密）。
- 过程事故与处置：共享库 customer_v3_test 曾被并行会话与本地套件交错留成「version=1200 + 垫片列已存在」的自相矛盾态（DuplicateColumn）；根因是 3 个 head 断言文件漏 bump + 双会话共用 5433 夹具。处置：补齐 bump、`create_test_database` 重建后全部复绿；终跑分片改用 5601+ 独立容器，规避共享夹具竞态。

## 最终门禁记录

- 评审修复后分片全量：shard0 911 / shard1 719 / shard2 743 / shard3 833(1 skipped) —— **3206 passed / 1 skipped，四片全 PASS**（第一轮评审前全量同样四片全绿，数字一致）。
- CI 三门禁以 PR 为准（本地未跑 cargo/audit/E2E/build，按仓规交 CI）。

## 登记的后续项（不阻塞本批）

1. 资费守卫口径推广评估：`viral_data` / `link_resolution` / `asr` 等同样有真实供应商成本的科目仍保留「漏配=免费」语义（内部免费科目依赖该语义，全局改动需逐科目甄别）。
2. 待删列：`runtime_settings.h3_extended_modes_enabled` 垫片应在「无 #196 前镜像可能运行」的下一个发布列车删除（需 digest 重算 + 8 文件 head 断言 bump + matrix 计数 1221→1220）。
3. 上线 runbook 必须新增：新库上线前配置 `viral_search` / `viral_search_refresh` 资费（否则 503，这是有意的 fail-closed）。
4. 刷新接口对视频号的支持待真实 detail→ViralVideo 映射（当前 422 明确拒绝）。

## 边界

- 未动钱包原子模型与锁序；未引入 ORM/MQ/新依赖；未触碰 025–030 冻结迁移名。
- 无真实付费调用、无生产部署、无历史数据修改；测试 digest 均为假值。
- 客户侧新增文案（422/503/401）均为中性表述，无供应商名。
