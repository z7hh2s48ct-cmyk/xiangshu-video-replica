# ADMIN-REFUND-NEGATIVE-ADJUSTMENT-20260922 受控负向调账（B1，唯一 P0）

任务：把退款 SOP §10 第 2 步（`docs/客户版部署与灰度手册.md`）要求的**反向调账**变成可执行、
可审计、可对账的动作。立项依据 `docs/退款闭环立项书-负向调账与SOP对齐-2026-09-22.md`。
分支 `feat/admin-refund-negative-adjustment-20260922`；基线 `origin/main @ 944e2246`（**基于最新 main**，非 stale base）。

## 1. 为什么要做

SOP §10 规定"退款走 T23 审计调账反向调账 + ZPay 商户后台人工退付，两笔事实以来源单对齐"。
但代码只允许正向加秒，账本 CHECK 也没有退款类型 —— **SOP 第 2 步在代码里执行不了**，
而下游后果是：账本侧不能反向记账、ZPay 侧实际退了钱，两边永远差一笔，
SOP 自己的"事后 `reconcile_customer_billing` 差额为零"永远无法满足。

## 2. 已拍板口径（用户 2026-09-22 决定）

**不允许负余额**；退款上限＝当前可用余额；原则是**按行业惯例只退未消耗部分**。
由此产生一条**设计边界（不是缺口）**：已消耗额度对应已交付服务，账本层面不退回，
争议走线下/拒付通道；超退必须**明确 400**，不得静默截断成最大可退额。
钱包下限 `ck_wallets_available_nonnegative` **未放开**，未设透支分支。

D3/D4/D5 取实现默认：新增 `REFUND` 类型 / 订单侧不加 `REFUNDED` / 单人+审计。

## 3. 交付

| 层 | 改动 |
|---|---|
| 迁移 | `20260923T1200_admin_refund_adjustment.py`：`ck_wallet_transactions_type` 加 `'REFUND'`；形状约束加 REFUND 分支；`admin_adjustments.recharge_order_id` 放开 NOT NULL + 新增 `ck_admin_adjustments_order_required`（只有退款类来源可以无单） |
| 路由 | `admin_customer_routes.py`：按来源单分流校验（负向非零 / 其余维持原 `1<=credits` 与逐字不变的错误文案）；负向写一条 `REFUND` 流水、**不建 `recharge_orders` 行**；余额护栏与超退 400 |
| 类型 | `TransactionType` 三处同步（`control_routes.py` / `wallet_routes.py` / `recharge_routes.py`）。第三处是必须的：`recharge_routes._customer_ledger_entry` 直接构造 `WalletTransactionResponse`，漏改则客户钱包出现 REFUND 行即 pydantic 500 |
| 前端 | `vocabulary.ts`（REFUND 词典 + `signedCredits`）、`AdjustmentsPage.tsx`（反向提示 + 带符号积分）、`SessionsPage.tsx`（退款类来源放开负号、切文案、确认弹窗） |
| 迁移链同步 | `manifest.json` 重录；`HEAD_REVISION` **7 处**；`test_customer_devices.py` 两处 head 字面量（**不是** `HEAD_REVISION = …` 形式，头两次按赋值形态 grep 漏掉、被跑测试抓出来，已收敛为模块常量）；CW-056 矩阵 `check_constraints` 322→323 与 `HEAD_SCHEMA_DIGEST` 重算 |

## 4. 三处实现强于原始规格

1. **"只加分支、不改既有五类"由机制保证**：`_live_shape_expression()` 从库上读回
   `ck_wallet_transactions_shape` 的现文本再追加一个 OR，不重打既有文本 ——
   不依赖人工誊抄（正是 #199 那类事故的成因）。降级用**冻结的原文本**还原，往返保真。
2. **余额护栏是原子行级护栏**：写在 UPDATE 的 `WHERE available_credits + credits >= 0` 里，
   不存在"先读再核对"的 TOCTOU 竞态。
3. **两个方向的 WHERE 刻意不共用**：正向用 `<= 2147483647 - credits` 避免 int4 溢出
   变成资金端点裸 500（PR #54 先例），反向不可能溢出。

**对原始规格的一处纠正**：立项书 §4.1 引自 `20260913T0630` 的 CONVERSION 形状，
但库里真源是 `20260913T1100`（多了 `billing_operation_id IS NULL`），故 REFUND 分支
按**库里实际分支**对齐 —— 立项书那处引用偏旧。

## 5. 验证（在基于最新 main 的树上）

| 项 | 实测 |
|---|---|
| 客户端全量 | **131 文件 / 2053 用例通过** |
| PG：`test_admin_customer_routes` + `test_customer_devices` + `test_migration_guard_manifest` | **171 passed** |
| PG：`test_postgres_migrations` + `test_cw056_supported_head_matrix` | **41 passed** |
| 迁移清单守卫 | `migration_manifest.py --check` → OK |
| 分片覆盖守卫 | 144 文件全覆盖 |
| 静态门 | ruff check / format（451 文件）/ mypy（182 文件）全清 |
| 迁移往返（实现方实测） | 升级→降级后三条约束文本与 `recharge_order_id` 可空性逐字节相同；库里有 REFUND 流水时降级被拒且 revision 不动 |
| 对账（实现方实测） | 反向调账后 `validate_database_invariants` 问题集合与基线相同，钱包余额 == 账本 `available_delta` 有符号求和 |

证据层级 **AUTOMATED_VERIFIED**（本地自动验证）。独立评审、远端 CI、真实浏览器验收未完成。

## 6. 未验证 / 已知边界

1. **后端全量套件（3129 用例）未拿到整体结论**。首跑至 29%（907 用例）抓到 6 个 F：
   4 个与本批无关（3 个 desktop 制品契约 —— 镜像无桌面工具链；1 个音频探测 —— 镜像缺
   ffmpeg），2 个是本批真实漏项（`test_customer_devices` 的 head 字面量），**已修并复跑 77 passed**；
   修完后的干净重跑因宿主机被其它会话占满而提前终止。**不要读成"全量已绿"。**
2. **`client/src/generated/api.ts` 未重新生成**：本地重生成会产出约 4.8 万行无关 diff（既有漂移，
   属既有技术债）。后果：客户侧三个 `transactionTypeLabel` / `LEDGER_TYPE_LABEL` 因
   `Record<WalletTransaction["type"], string>` 陈旧未含 REFUND，**客户钱包里的 REFUND 行
   暂显示原始类型字符串**；管理端词典已补。建议与 `generated/api.ts` 重生成一并解决。
3. 未做真实浏览器验收（表单交互仅 vitest 覆盖）。
4. SQLite 内部泳道不受影响（迁移在非 PG 方言下直接 return，022+ 既有先例）。
