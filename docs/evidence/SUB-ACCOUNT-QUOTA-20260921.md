# 子账号月度额度（CUSTOMER-CENTER-V2 Phase 3a）

## 目标与边界

用户需求：母账号给指定子账号设置「每月最多消耗多少积分」，超限拒绝新消费；子账号侧可见本月用量进度。对应 audit-40 Phase 3 前段（额度）；业务「权限矩阵」属 Phase 3b，另行 worktree，不在本分支。

独立任务 `SUB-ACCOUNT-QUOTA-20260921`，分支 `feat/sub-account-quota-20260921`，基线 `origin/main@8cdf4020`（#184）。开工核对本地/远程分支、开放 PR、worktree、共享 claim，零同题占用；与并行 worktree（replica-ux-20260921 / viral-business-optimization-20260921）零文件交集。原子认领于 `codex-task-claims/SUB-ACCOUNT-QUOTA-20260921/claim.json`。

## 实现

- 迁移 `20260921T1200_sub_account_quotas`（PG-only 守卫，接 head `20260921T0000`）：行存在=受限、无行=不限（存量零回填即兼容）；`monthly_credits` BIGINT + CHECK ≥ 0；`ON DELETE CASCADE`；downgrade 有额度行时 fail-closed 拒绝删表。
- `app/sub_account_quota.py`：当月已用量按 `wallet_transactions`（actor + 当月 + RESERVE/RELEASE 白名单）实时聚合。额度是「限制」不是「账本」，账本唯一来源仍是钱包流水，避免双写漂移。
- `accept_operation` 统一预留前额度校验：父链上溯取最紧额度，「已用 + 本次预留 > 额度」→ 403 `SUB_ACCOUNT_QUOTA_EXCEEDED`；不改变钱包结算与恢复路径。
- 客户自助 API：GET 子账号列表（含当月用量）、POST 创建（可带初始额度）、PUT `/{id}/quota` 设置/清除；版本作用域由 `customer_sub_account_routes._lock_owned_sub` 强制（与 `ck_users_account_parent_shape` 同款应用层不变量）。
- `/api/customer/profile` 扩展子账号额度字段；前端 `SubAccountManagementPage` 额度设置与进度展示、`CustomerProfilePanel` 子账号视角额度进度卡；`generated/api.ts` 按新 schema 重新生成。

## 验证记录

- 后端专项：`test_sub_account_quota.py` 12 passed；支持 head 矩阵 12 passed；`test_customer_devices.py` 76 passed；migration guard 17 passed；`sqlite_to_postgres` 全量 38 passed（消费方复验 75 passed）。
- 四片 shard（客户 v3 PG 库，每片独立）：**6F / 22F / 5F / 0F**。较修复前（6F / 28F / 5F / 2F）自引起的 8 个失败全部消除——sqlite 契约 6（`PG_ONLY_TABLES` 补登 `sub_account_quotas`）、manifest 2（`--record` 重记）；余 33 个为环境固有。
- 环境固有失败归因（与上期 t10 同款，由 CI Linux 判定）：`cw033_pitr`×22 与 `customer_git_rollout`×5 依赖 WSL bash（本机 `execvpe(/bin/bash)` 不可用）；`cw043_viral_import_pg`×6 为 Windows 子进程/路径限制（FileNotFoundError / WinError 206）。
- 前端 `npm run check` CHECK_EXIT=0：biome + tsc + vitest **110 文件 / 1744 passed**。
- 静态门禁：ruff check ✓、ruff format --check ✓、mypy 172 文件 ✓（迁移文件格式经 ruff format 收敛）。
- 分片清单：`build-test-shards.py --check-coverage` **127 文件全覆盖**（RC=0）；从 committed profile 重生成四片字节级一致（30/34/34/29，spread ~0.1s）。
- 本地门禁根因记录（修正在本机脚本，不进仓库）：共享终端曾继承他会话导出的 `VIDEO_REPLICA_DATABASE_URL`，`migrations/env.py` 优先该变量导致迁移静默重定向（专用库为空、TRUNCATE 报 UndefinedTable、升级 0.05s 秒回）；分片脚本改为双重清理 env 并设 `PYTHONUTF8=1` 对齐 Linux CI 编码。
- CI migration guard 修复记录：PR #187 Linux quality gate "Assert migration chain invariants" 步骤结论 failure；根因 = `20260921T1200_sub_account_quotas.py` 工作区 CRLF + manifest 记录 CRLF 口径 sha256（CI LF 校验不匹配）；修复 = 文件转 LF（blob 与 c0c05f9f 一致）+ --record 重记 → 110/110 blob-vs-manifest 全量一致，CI 等价校验通过。

## 验收口径与回退

额度为机构内控限额：无行=不限保证存量兼容；超额返回 403、不扣费、不写账本；限额清除=删除行（PUT quota null）。全部验证使用独立 PG 与本地假数据，不动生产库、无真实支付。

回退：先清空 `sub_account_quotas` 行再 downgrade（有行时迁移 fail-closed 拒绝），应用侧删除校验接入即恢复原行为。本文不代表生产部署或真实支付验收完成。
