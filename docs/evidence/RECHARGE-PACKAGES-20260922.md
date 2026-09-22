# RECHARGE-PACKAGES-20260922 管理员可配置充值套餐与消费折扣权益

## 本次修改清单

用户需求：充值档位不再写死 100/300/500，改由管理员在后台配置套餐；支持赠送（充 1998 到账 2000 积分）与打折（视频生成每次 9 折），也支持充值 500 到账 500 无优惠。用户已确认四项口径：①折扣权益永久有效；②带权益套餐最近一次覆盖，无权益套餐不触碰已有权益；③折扣范围（接口）套餐内可配置；④保留自定义金额入口（基础汇率、无权益）。

| 修改项 | 修改后的行为 | 主要文件 |
| --- | --- | --- |
| 数据模型 | 迁移 `20260922T1200_recharge_packages`：`recharge_packages`（金额/到账积分/折扣接口/折扣率/范围/启停/排序）+ `customer_discounts`（客户权益，含来源订单、接口、折扣率、窗口） | `server/alembic/versions/20260922T1200_recharge_packages.py` |
| 套餐服务 | 草稿校验（金额≥起充、积分>0、折扣率与接口集合合法）、快照构建/解析、`grant_package_discount` 按快照授予权益、`get_best_discount` 取该接口最优权益 | `server/app/recharge_packages.py`（新增） |
| 管理端 CRUD | 套餐列表/创建/更新/启停/删除，AdminWriter 围栏，删除用软删（停用）保留订单引用完整性 | `server/app/recharge_routes.py`、`server/app/main.py` |
| 客户侧只读 | 客户读取在售套餐列表，不暴露内部字段 | `server/app/recharge_routes.py` |
| 套餐下单冻结 | 客户按 `package_id` 下单：服务端按当前套餐生成快照写入订单 `package_snapshot_json`；后续改套餐不影响已下单 | `server/app/recharge_routes.py`、`server/app/zpay_payments.py` |
| 结算授予 | 支付结算事务内按快照授予客户折扣（带权益才授予；无权益套餐跳过、不触碰已有权益） | `server/app/zpay_payments.py` |
| 报价接线 | `GenerationPriceQuote` 增加 `discount_rate` / `discount_source`（生效折扣与来源 token）；平台折扣（`discount_basis_points`）与客户权益取更强合并 | `server/app/billing_catalog.py`、`server/app/customer_pricing.py` |
| 计费快照 | `accept_operation` 冻结生效折扣；`wallet_transactions.discount_rate` 记录实际折扣 | `server/app/usage_billing.py`、`server/app/wallet_billing_service.py` |
| 前端 | 后台套餐管理（新增/编辑/启停/折扣接口与折扣率配置）；客户充值对话框套餐卡片（赠送与折扣徽标）；钱包页折后价展示 | `client/src/admin/`、`client/src/customer/CustomerRechargeDialog.tsx`、`CustomerWalletPanel.tsx`、`client/src/rechargePackageDisplay.ts` |

评审修复（CodeReview 子代理 + 逐条实质修复）：

| 级别 | 问题 | 修复 | 文件 |
| --- | --- | --- | --- |
| H-1 | 套餐金额低于生效起充额时路由豁免全部金额校验，直接撞 DB CHECK 返回 500 | 路由层补 422 `RECHARGE_PACKAGE_BELOW_MINIMUM`；前端低于起充的套餐置灰并提示"低于起充金额 X，暂不可购" | `recharge_routes.py`、`CustomerRechargeDialog.tsx`、`CustomerWalletPanel.tsx`、`rechargePackageDisplay.ts` |
| M-1 | `grant_package_discount` 无用户级串行化，并发结算可产生两条 active 权益 | 授予前 `pg_advisory_xact_lock(hashtext('billing:customer-discount:' || user_id))`，事务结束自动释放 | `recharge_packages.py` |
| M-2 | `retail_snapshot` 的 discount_rate 记录原值、折扣为合并值，平台折扣更强时账目/展示与实收不一致 | 按生效折扣归因：平台严格更强时 rate=bp/10000、source=None；客户更强或同强归客户；无折扣 → None | `billing_catalog.py` |
| L-1 | 结算事务内快照解析异常会回滚已支付入账 | grant 调用包 `try/except (ValueError, TypeError)` + 错误日志，跳过授予但保留入账 | `zpay_payments.py` |
| L-2 | 折扣接口键前后端双份维护易漂移 | vitest 契约测试读 `server/app/billing_catalog.py` 提取 `INTERFACE_KEYS` 与前端标签逐键比对 | `rechargePackageDisplay.test.ts` |
| L-3 | `api.ts` 注释与 token 语义不符 | 注释改为折扣来源 token（`recharge_package` / `manual`） | `client/src/api.ts` |

E2E 修复（PR 门禁预检发现）：改造移除了写死的 100/300/500 预设按钮，而 `e2e/customer/recharge.spec.mjs` 仍按 `/^100 元/` 点选档位、E2E 数据库（`seed_codes.py`）不 seed 套餐——客户 E2E 将因找不到档位按钮失败（本地 `check:static` 不含 E2E，开发期未覆盖）。修复：`seed_codes.py` 插入一行启用套餐（名称「100 元档」，100 元到账 1000 积分、无权益），对话框按新交互渲染套餐卡片，E2E 继续走真实套餐下单链路（真实下单 + 边界 mock 支付码）。

不改变：自定义金额充值路径（基础汇率、无权益、原有步长/起充校验）；平台全局折扣配置与既有 订单/计费口径；内部身份与管理后台其他模块；无新依赖、无 ORM/Redis/消息队列。

## 测试与验证

- 测试先行（RED→GREEN）：`server/tests/test_recharge_packages.py`（703 行，含管理 CRUD、客户只读、套餐下单冻结、并发串行化）与 `server/tests/test_discount_wiring.py`（491 行，含报价折后、accept 快照、钱包流水折扣、平台/客户折扣合并）先红后绿，连续两轮全绿。
- 评审修复回归：后端新增 4 项（M-1 并发持有 advisory lock 阻塞轮询、H-1 低于起充 422、M-2 平台更强折扣生效归因、L-1 无效快照不损入账）+ 前端 3 组新用例先红后绿；两个后端文件 60 passed，9 个相关文件 173 passed，前端 3 文件 33 passed。
- 本地门禁（最终代码）：`npm run check:static` 通过（secret 扫描 + Biome + tsc + 全量 vitest 1770 passed + e2e lint + tauri cargo fmt/check + ruff 418 文件 + ruff-format + mypy 173 文件）；E2E 修复后重跑仍全绿。
- 客户 E2E（本地实跑，Playwright chromium + 独立 API/库）：`recharge.spec.mjs` 专项 1 passed（7.1s，修复前本地未覆盖）；全套 `e2e/customer` 6 passed（24.3s，含 activation/pairing/material-dedupe 无回退）。
- 分片全量：`bash scripts/ci/run-pytest-shards.sh` 四片独立 PostgreSQL（5433+i）全绿：821 + 727 + 644 + 730 = 2922 passed / 1 既有 skipped，容器已自动清理。
- 迁移可验证：`alembic upgrade head` / `downgrade` 均通过（新表 additive，不动既有表结构）。

## 边界、授权与回滚

- 无真实付费调用、无生产部署、无对外发码；未调用真实 Provider；无历史数据修改。
- 回滚 = revert 本任务提交 + `alembic downgrade`（仅删本任务两表，对既有数据无副作用）；已下单订单的快照字段随表保留，不反向影响旧订单。
- 权限边界：管理端 CRUD 走 AdminWriter 围栏；客户侧仅只读在售套餐，不暴露成本/内部字段。
- 未测试项：真实 ZPay 套餐单结算与真实链路验收（REAL_CHAIN_VERIFIED）需人工授权后在 staging 执行，不在本任务范围。

## §14 任务证据

- 任务 / 工作包：用户要求充值档位可由管理员后台配置，支持赠送、折扣与无优惠三种形态（§12.21）。
- Owner：Qoder session 代 honor.pei；Reviewer：CodeReview 子代理自检评审（H-1/M-1/M-2/L-1/L-2/L-3 逐条修复并回归）。
- 分支 / main 基线：`feat/recharge-packages-20260922` / `8e9e69be657e352a450f6f05893de04ec8645ac8`（开工 fetch 核验；开放 PR #187/#176 与本任务文件无交集）。
- worktree：`.worktrees/RECHARGE-PACKAGES-20260922`；共享 claim 在 Git common dir `codex-task-claims/RECHARGE-PACKAGES-20260922/claim.json`。
- 开工查重：认领登记、排班清单、本地/远程分支、共享 claim 均已核对；`recharge_packages` 在 origin/main 不存在，无同题占用。
- 上游规格段落：§12.21；`docs/客户版任务清单-V3.md` §12 工作包增量。
- 改动文件：`server/migrations/versions/20260922T1200_recharge_packages.py`（新）、`server/migrations/manifest.json`、`server/app/recharge_packages.py`（新）、`recharge_package_routes.py`（新）、`recharge_routes.py`、`zpay_payments.py`、`billing_catalog.py`、`customer_pricing.py`、`discount_service.py`、`customer_fence.py`、`generation.py`、`generation_routes.py`、`usage_billing.py`、`wallet_billing_service.py`、`main.py`、`server/scripts/reconcile_customer_billing.py`、`server/scripts/sqlite_to_postgres.py`、`client/src/rechargePackageDisplay.ts`、管理端 `RechargePackageManager.tsx`/`SystemSettingsPage.tsx`/`api.admin.ts`、客户端 `CustomerRechargeDialog.tsx`/`CustomerWalletPanel.tsx`/`CustomerProfilePanel.tsx`/`StudioWorkspace.tsx`/`LiveWorkspacePanel.tsx` 与 CSS、`e2e/customer/seed_codes.py`、4 个分片清单、两个服务端测试文件与四个前端测试文件；账本与证据文件。
- 失败测试或回归锁定：先红后绿见"测试与验证"；既有 `test_customer_recharge`、`test_customer_pricing`、`test_zpay`、`test_wallet_billing_service`、`test_usage_billing` 等全部保持通过。
- 实现结果：套餐配置/授予/报价/快照/流水全链路接线完成；AUTOMATED_VERIFIED（本地两轮门禁）。
- 验证命令与通过数：见"测试与验证"各段（2922 passed / 1 既有 skipped；vitest 1770 passed）。
- 证据层级：AUTOMATED_VERIFIED（未执行真实链路，不宣称 STAGING_VERIFIED 及以上）。
- 安全与可观测性：管理端 AdminWriter 围栏；grant 跳过路径有 `logger.error` 留痕；无密钥/明文进入代码、日志与测试。
- 迁移与回滚：`20260922T1200_recharge_packages`（additive 两表）；downgrade 已验证。
- 外部授权记录：无（未触碰真实付费、COS、发码与公网发布）。
- 未测试项：真实 ZPay 套餐单与真实 Provider 链路（需人工授权）。
- Lore 提交 SHA：`4b5110c3`（实现）+ 评审修复提交（见 PR 当前 head）。
