# ADR: 迁移链守卫工具化（MIGRATION-GUARD-20260912）

> 说明：本仓库中的 CW-/T- 编号指向项目内部的任务跟踪系统，对应的任务台账与验收证据不随本仓库发布。

- **状态**：Accepted
- **日期**：2026-09-12
- **基线**：`origin/main` @ `360cb1f`（PR #68 COORD-DEV-PLAYBOOK 合并后）
- **关系**：本文说明迁移链守卫的**工具化实现**，把并行写入共享迁移状态时的合并协议固化成脚本与回归测试。

## 背景

手册 §6 记录了 2026-09-11~12 的 6 起真实事故，共性结论是「没有一起是 alembic/CI 的 bug——全部是并行写入共享状态时缺少合并协议」。手册把协议写了下来，但其执行形态仍是**人工复制粘贴**：

- §4 是让人把一段 heredoc 粘进终端，再把打印出的数字**逐项手抄**回 `test_cw056_supported_head_matrix.py`。
- §2 家族 #3 是「`grep -rln '"08X_'` 全部 bump 到新 head（18 处 ≈ 1 分钟）」——量不大，但**正确值是算出来的、不是判断出来的**，手改必错且错得安静。
- 产生 `HEAD_SCHEMA_DIGEST` 的离线 freeze probe 原先只存在于开发机上，该步骤一度只剩散文描述。换一台机器就复现不出来。

同时，仓库里已有一处**没被用上**的现成能力：`deploy/operator/build_operator_package.py::migration_head()` 是一段 stdlib + `ast` 的迁移图遍历器，**已经正确处理 tuple `down_revision`**（:124），且对重复 revision、不可求值父节点、未知父节点、多 head 全部 fail-closed。也就是说，「支持多父（merge revision）」在静态侧早就具备，只是没有配套的断言与契约。

## 决策

1. **把 §4 与 §5 变成可执行命令**，而不是可复制文本。
   新增 `scripts/ci/migration_manifest.py`，四个模式：`--check`（CI 守卫，免 PG）、`--record`（重新生成清单）、`--print-schema`（打印可粘贴的冻结字面量，替代 §4）、`--check-schema`（真实 PG 与冻结字面量比对）。静态半部**只用标准库 `ast`**，不 import alembic、不 import 测试模块，因此 CI 用裸 `python3` 就能跑（与 `scripts/ci/build-test-shards.py` 同款），也不受 `pg_test_kit` 顶层 `import fcntl` 的 POSIX 限制。

2. **真源不搬家**：`PUBLISHED_HEAD_REVISION`、`HEAD_REVISION`、`HEAD_SCHEMA_*` 等仍以 `server/tests/test_cw056_supported_head_matrix.py` 为准。守卫用 `ast.literal_eval` 读它们（不执行该模块），然后**独立重算再比对**。这就是「双跑」：两套实现必须同时正确，任一侧漂移都会红，不会两边一起错。本轮**不删除**任何既有手写字面量。

3. **新增迁移采用时间戳命名**（`YYYYMMDDTHHMM_<slug>`），存量文件一律不改名。
   依据：main 现链 `081 → 082 → 086 → 083 → 089` **数字序早已断裂，顺序完全由 `down_revision` 决定**，编号只是标签。而并行开发下「取下一个号」必然撞名（手册事故 ⑤ 与 #64 的重编号）。豁免规则用「一个锚点 + 祖先闭包」（`naming_policy.adoption_head`），而不是罗列上百条白名单。

   **锚点必须是生成器里的常量，不能从清单读取，也不随 `--record` 前移。** 否则存在一条自我豁免路径：加一个违规命名的迁移 → 跑 `--record`（锚点随之推到它）→ 提交，此后锚点就是违规者自己，它落进豁免集，再怎么 `--check` 都查不出来。`--check` 断言清单记录的锚点等于常量 `NAMING_POLICY_ADOPTION_HEAD`；`check_naming_policy` 在锚点未知时 fail-closed 抛错，而不是静默返回空——「策略失效」与「没有违规」在结果上无法区分。两条反例都在 `test_migration_guard_manifest.py` 里。

4. **merge revision 是收口机制，不是重挂的替代品。**
   正常路径仍按手册 §3 重挂未合并分支的 `down_revision`（那是安全的，且产物更简单——不会多出一个无主的合成 revision）。`down_revision = ("a", "b")` 的用途是**当重挂不可行时**：两个 head 已经都进了 main。此时无法再改写任何一方，只能追加一个 merge revision。守卫把「已发布段 base..055 必须保持线性」写成硬约束，从而保证 merge revision 只可能出现在已发布段之后——CW-056 的 `_published_chain()` 是线性遍历，这个边界是它成立的前提。

5. **`deploy/postgres/migrate.sh` 补多头计数守卫**，与 `deploy/customer-git-rollout.sh:206-210` 的既有写法对齐。
   精确的缺口不是「脚本多头」——那种情况 `migrate.sh:28` 的 `alembic upgrade head` 会因 `MultipleHeads` → `CommandError` 硬失败（`_upgrade_revs` 内触发，`set -e` 直接中止）。真正的缺口在 `:36`：`alembic current | awk 'NR == 1'` 面对**库里有多行 `alembic_version`** 时只读首行，可能把一个多头库判成「已到达预期 head」而报成功。修复即对两侧都计数并要求恰为 1。

## 后果

**得到**：漂移在数秒内、在昂贵的分片 pytest 之前被拦住；§4 的探针可复现且跨机器一致；「已落地迁移被静默改写」有了机器可检的痕迹；新迁移不再有抢号问题。

**代价**：每次新增或重挂迁移后，必须运行 `--record` 并提交 `server/migrations/manifest.json`（与 `scripts/ci/test-shards/*` 的既有惯例一致）。这是**有意的摩擦**——它把「忘了登记」从合并时的意外变成 CI 上一条指名道姓的失败。

## 边界（明确不声称的事）

- 清单证明的是**「当前迁移树与最后一次记录一致」**，**不是「没有人改写过历史」**。后者由 CW-056 已发布链的内容级/关系级 sha256（覆盖 base..055）与手册的流程铁律共同承担。对 056+ 段，一个愿意重新运行 `--record` 的人仍可静默改写——本 ADR 不声称堵住了这个口子。
- 守卫的证据层级上限是 `AUTOMATED_VERIFIED`。它不证明迁移在真实授权链路上可用。
- 本轮**不删除** 10 个测试文件里的 20 处 head 字面量。双跑成功、经过一个完整迁移波次之后，删除旧字面量是一个独立的后续任务。
- 证据红线（`CW001-RELEASE-BASELINE.md`、`CW056-EVIDENCE.md` 记载的「纯线性链、无 `down_revision` 元组」）的**重签动作归 owner**；本任务只提供重验证据。

## 引用

- `scripts/ci/migration_manifest.py`、`server/migrations/manifest.json`
- `server/tests/test_migration_guard_manifest.py`
- `deploy/operator/build_operator_package.py::migration_head`（既有 DAG-aware 遍历器）
- 已发布 revision 的字节冻结约定由 `server/tests/test_migration_guard_manifest.py` 钉住
