# ADMIN-AUDIT-COUNT-A12-20260922 审计端点 COUNT 全 UNION 重算评估

任务：管理端代码综合评审报告（2026-09-12）P3 清单项「批处理 COUNT 全 UNION 重算（审计表涨大后最慢读）」
（`docs/管理端代码综合评审报告-2026-09-12.md:57`，同文件 `:72` 表格行的「COUNT 全量重算性能」）。

- 上游记载：同上。
- 基线：`origin/main @ 39a42c36`（本次评估最初在 `2a6efbb2` 上做的，`39a42c36` 的两个提交未触碰
  `server/app/admin_audit_routes.py`、`server/tests/test_admin_audit_routes.py` 与 §1 六张表所在的迁移文件；
  复核后已 ff 到 `39a42c36`）。
- 结论：**只出评估，不改代码**（行为零改动）。理由与证据见下。

## 0. 三句话结论

1. **没有正确性缺陷**：COUNT 与列表的 `WHERE`/`FROM`/`JOIN` 逐字相同，行集恒等，`total` 已是精确值（§2）。
2. **COUNT 不是本端点最慢的读**：同一份数据、同一次运行里，列表查询比 COUNT 慢 4–10 倍（§3.0）。
   报告把「最慢读」记在 COUNT 上，在当前查询形状下不成立（措辞更正，同 `ADMIN-AUDIT-BLINDSPOTS-20260922.md`
   §2.2 对同一份报告做更正的先例）。
3. 唯一能把 COUNT 再压下去的手段（第 3 分支的 partial index）**必须新增迁移**，其连锁成本（§3.5 清单）
   远大于收益；其余改法要么有正确性风险（缓存/近似），要么实测更慢（单语句改写）。**触发线见 §4。**

## 1. 六个来源逐一对照：每个来源表当前有无可支撑该查询的索引

`_UNION_SQL` 的六个分支（`server/app/admin_audit_routes.py:60-148`），对照
`server/migrations/versions/*.py`：

| # | 分支（`admin_audit_routes.py` 行号） | 来源表（建表迁移:行） | 该表上的索引（迁移:行） | 该分支可用的索引 |
|---|---|---|---|---|
| 1 | `:61-66` `ADMIN_ADJUSTMENT` | `admin_adjustments`（039:55） | PK `id`；UNIQUE `recharge_order_id`（039:62）；`(target_user_id, created_at)`（039:116）；`(admin_user_id, created_at)`（039:121）；`(created_at)`（039:126） | 无。分支内无条件，只 `JOIN users u ON u.id = aa.admin_user_id` |
| 2 | `:68-73` `ADMIN_DEVICE_<EVENT>` | `admin_device_events`（038:127） | PK `id`；`(device_id, created_at)`（038:181）；`(pairing_request_id, created_at)`（038:186）；`(admin_user_id, created_at)`（038:191） | 无（分支内无条件） |
| 3 | `:75-83` `ADMIN_SESSION_<EVENT>` | `customer_session_events`（029:164） | PK `id`；`(user_id, created_at)`（029:205）；`(session_id, created_at)`（029:210）；`(event, occurred_at)`（042:55）；`(user_id, event, occurred_at, id)`（042:60） | **无**。分支谓词是 `actor_user_id IS NOT NULL AND actor_user_id <> user_id`（`:83`），`actor_user_id` **没有任何索引** |
| 4 | `:85-92` `ACTIVATION_CODE_<EVENT>` | `activation_code_events`（027:396） | PK `id`；`(code_id, created_at)`（027:416） | 无（分支内无条件） |
| 5 | `:94-101` `ACTIVATION_CODE_DELIVERED` | `activation_code_deliveries`（027:260） | PK `id`；`(code_id, delivered_at)`（027:290） | 无（分支内无条件） |
| 6 | `:103-147` `audit_logs.action` | `audit_logs`（001:179，**无索引**） | PK `id`；`(action, occurred_at, id)`（042:85） | 无（分支内无条件） |

连接用的 `users`：`001:31` 建表，`id` 是 `sa.Text(), primary_key=True`（**唯一**），`users_pkey`。

**为什么「有索引」也不等于「可用」**（实测 plan，§3.0 数据）：

- 外层筛选（`ev.event_type/actor_user_id/target_user_id/actor_username/created_at`）作用在
  `UNION ALL` 子查询**之上**，只有**常量等值谓词**会被 PG 下推到分支：
  - `event_type = 'ADMIN_ADJUSTMENT'` 下推成功，`audit_logs` 分支因此走
    `idx_audit_logs_action_occurred_at`（Bitmap Index Scan，`actual rows=0`），COUNT 21.6/27.2 ms；
  - `actor_user_id`、`target_user_id`、`created_from/created_to` **不下推**（这些是各分支的
    `COALESCE`/`CASE`/`utc_timestamp_sql` 计算列），`Parallel Append` 仍吐满全量行（实测
    `actual rows=151733 loops=3` ≈ 455,199 行）。
- 分支上现有的 `(admin_user_id, created_at)` 之类索引**既不覆盖载荷列也不减少行数**（分支内没有条件可
  用它），PG 只是偶尔被代价模型选中做 Bitmap Index Scan，行数一条不减。
- 结论：**这六个来源当前没有任何索引支撑这条查询**；缺口集中在第 3 分支（域表心跳行 400,000 行全扫，
  只留 200 行）。

## 2. COUNT 与列表的 `WHERE` 是否一致 → 一致（不是正确性缺陷）

代码层面（`admin_audit_routes.py:177-246`，本次只加了 10 行注释，未改行为）：

- 同一个 `where` 字符串（`:197`）以同一参数顺序拼进两条 SQL（列表 `:218`、COUNT `:243`）；
- 同一个 `FROM (…UNION…) AS ev(…)` 与同一个 `LEFT JOIN users tu ON tu.id = ev.target_user_id`；
- 两者只差 `ORDER BY … LIMIT … OFFSET …`（只裁剪返回行，不影响计数）。
- 基数层面：`users.id` 是主键 ⇒ 该 `LEFT JOIN` 至多匹配 1 行 ⇒ 去掉它 COUNT 值不变（这一点也在实测里
  对上了：`count=455200 count_no_tu=455200`）。

实测等价性（PG 16.15 / Docker，6 表 855,000 行 → UNION **455,200** 行；详见 §3.0）：

```
count=455200  count_no_tu=455200  window_total=455200
event_type='ADMIN_ADJUSTMENT' 过滤：count=5000  ==  列表一次性取回（limit=1000000）行数 5000  → same=True
offset 越界（offset=10,000,000）：列表 0 行、total 仍为精确值
limit=0：列表 0 行、total 仍为精确值（现有 API 允许 limit=0）
```

即 `total` 就是列表可翻页到的行数本身，分页口径正确，**A12 不是正确性缺陷**。

### 2.1 附带发现（非 COUNT 缺陷，但值得记录）

同一次「激活码交付」会在 UNION 里出现**两行**、且 `event_type` 完全相同
（`ACTIVATION_CODE_DELIVERED`）：写入点 `server/app/admin_activation_routes.py:371`（写
`activation_code_deliveries`）与 `:384`（写 `activation_code_events`，`event='DELIVERED'`）在**同一事务**
里成对发生，另一条交付路径 `:569`/`:590` 同样成对；而分支 4 映射 `'ACTIVATION_CODE_' || ae.event`、
分支 5 映射常量 `'ACTIVATION_CODE_DELIVERED'`（`admin_audit_routes.py:85` 与 `:94`），两条分支落到同一个
`event_type`（`source_document_type` 分别为 `ACTIVATION_CODE` 与 `ACTIVATION_CODE_DELIVERY`，可用于区分）。

- COUNT 与列表**两边都算这两行**，所以分页口径没错；
- 但按「一次动作一行」的读法，审计页一次交付会显示两行。这属于 A10 UNION 覆盖面/重叠的设计问题
  （改它要改响应内容），不在 A12 范围，本次仅记录。

## 3. 可选改法逐条：代价、风险与实测

### 3.0 实测基线（本机 Docker Linux + 宿主 PG 5433；PG 16.15，`shared_buffers`=128 MB，
`work_mem`=4 MB，`max_parallel_workers_per_gather`=2）

灌数（`alembic upgrade head` 后）：`admin_adjustments` 5,000 / `admin_device_events` 20,000 /
`customer_session_events` 400,000（95% 心跳与自注销，1/2000 为管理员动作）/ `activation_code_events`
100,000 / `activation_code_deliveries` 30,000 / `audit_logs` 300,000 → UNION 455,200 行。

| 查询 | 第 1 次运行 | 第 2 次运行 | 备注 |
|---|---|---|---|
| A 现 COUNT（无过滤） | 38.3 ms | 79.0 ms | Parallel Append(3) 全分支 Seq Scan；PG 做了**列裁剪**，COUNT 不付 timestamp CASE 的代价 |
| B COUNT 去掉末尾 `LEFT JOIN users tu` | 35.8 ms | 48.2 ms | 与 A 恒等（PK 唯一），见 §3.4 |
| C 现列表 `LIMIT 20` | **314.1 ms** | **766.3 ms** | 每行求值 `utc_timestamp_sql` 正则再 top-N 排序 |
| D 单语句 `COUNT(*) OVER ()` + `LIMIT 20` | 443.1 ms | 740.2 ms | 有 WindowAgg 卡住 top-N → 外部归并排序 |
| E COUNT 过滤 `event_type` | 21.6 ms | 27.2 ms | 谓词下推成功（索引可用） |
| F COUNT 过滤 `actor_user_id` | 39.9 ms | 45.4 ms | 谓词不下推 |
| G 单语句 `MATERIALIZED` CTE + COUNT + LIMIT | — | 1145.7 ms | 物化 455k 宽行比省下的那次扫描更贵 |
| H COUNT 过滤 `target_user_id` | — | 41.8 ms | 谓词不下推 |
| I COUNT 过滤 `created_from` 范围 | — | 364.7 ms | 不下推且逐行求值 CASE → 无过滤 COUNT 的 4.6–9 倍 |

两轮绝对值差 2 倍（缓存/统计状态），但**相对关系一致**：列表 = COUNT 的 4–10 倍。

### 3.1 加覆盖索引（需新增迁移）——**不实施**

- 对 COUNT 无用：无过滤 COUNT 无论如何要扫全部行，"覆盖索引"也不减少行数（索引全扫 ≥ 表全扫）。
- 唯一真正有价值的一处：第 3 分支的 partial index，例如
  `ON customer_session_events (id) WHERE actor_user_id IS NOT NULL AND actor_user_id <> user_id`
  （400,000 行扫 → 200 行），能同时省掉 COUNT 与列表的绝大部分扫描量。
- **代价清单（具体文件）** —— 本仓库迁移链的连锁成本是硬成本：
  1. 新增 `server/migrations/versions/<YYYYMMDDTHHMM>_*.py`（命名必须匹配
     `server/migrations/manifest.json:naming_policy.pattern = ^\d{8}T\d{4}_[a-z0-9_]+$`）；
  2. 重新生成并提交 `server/migrations/manifest.json`（每个 revision 记 `file`+`sha256`，
     `graph.heads`/`graph.parents` 会变；CI 走 `scripts/ci/migration_manifest.py --check`，
     `.github/workflows/ci.yml:226`）；
  3. 8 处 `HEAD_REVISION`/`_HEAD_REVISION` 常量同步：
     `server/tests/test_postgres_migrations.py:50`、`test_cw056_supported_head_matrix.py:82`、
     `test_customer_registration.py:63`、`test_activation_code_schema.py:35`、
     `test_sub_account_permissions.py:70`、`test_sub_account_quota.py:69`、`test_sub_account_schema.py:38`
     （外加 `test_customer_devices.py` 里的字面量断言）；
  4. `test_cw056_supported_head_matrix.py` 的 `HEAD_SCHEMA_COUNTS`（新增表/列/索引都要改计数）、
     `HEAD_TABLE_NAMES`（本次方案不加表则免）；
  5. `test_postgres_migrations.py::test_pg_full_upgrade_downgrade_reupgrade_and_indexes` 要求
     downgrade 对称删索引；
  6. 现成样本（本次 ff 进来的 PR #205）就是这个体量：45 文件、其中 8 个测试模块 + manifest +
     4 份 `scripts/ci/test-shards/shard-*.txt` 都要跟着动。
- **风险**：并行分支上的 head 重挂冲突（迁移手册 §3）、CI 全链 guard 红、以及上述人手同步漏项。
  收益侧只有「COUNT 再省几十毫秒」，而 COUNT 只占端点 10%–20%（§3.0）⇒ 不划算。

### 3.2 物化/缓存计数 —— **拒绝**

失效面覆盖 6 张表 × 全部写路径（含 append-only 表、TRUNCATE 守卫之外的一切 INSERT）；任何一处漏失效
就是 `total` 陈旧 ⇒ 分页错位/翻页丢行。违反「`total` 必须精确」红线，且与「六源 UNION 是活的」
设计（`ADMIN-AUDIT-BLINDSPOTS-20260922.md` §2.1：读域表使历史吊销无需回填）直接冲突。

### 3.3 近似（估算/采样）—— **拒绝**（红线）

### 3.4 实测被否的两类"看起来更快"的改法 —— **不采纳**

- **单语句改写**（`COUNT(*) OVER ()` 或 `MATERIALIZED` CTE，D/G）：实测**没有稳定收益甚至更差**
  （D 443/740 ms vs 列表+COUNT 352/845 ms；G 1146 ms），而且 `limit=0` 或 `offset` 越界时返回 0 行 ⇒
  **拿不到 total**（现有 API 允许 `limit=0`，`admin_audit_routes.py:174`），要额外补一条回退 COUNT
  才等价 —— 复杂度换噪声级收益，不做。
- **COUNT 去掉末尾 `LEFT JOIN users tu`**（B，仅当无 `target_username` 过滤时）：**等价**
  （`users.id` 是主键，连接不放大行数），实测 COUNT 38.3→35.8 ms / 79.0→48.2 ms，折算到端点总耗时
  0.7%–3.7%。收益在噪声量级，代价是 COUNT 出现两种 SQL 形状（有/无 `target_username` 时不同），
  多一处「两个形状必须同步」的维护面 ⇒ 不做。

### 3.5 不动，只写清触发条件 —— **本次采纳**（见 §4）

## 4. 触发条件：数据到什么量级再处理

- 无过滤 COUNT 随 UNION 行数近似线性（本机 455,200 行 → 38–79 ms，即约 0.08–0.17 µs/行，并行 3 worker、
  命中缓存、CPU bound）。线性外推（同一硬件、同缓存状态、无过滤）：
  | UNION 行数 | 无过滤 COUNT 预期 | 带日期范围过滤的 COUNT 预期（×4.6–9） |
  |---|---|---|
  | 45 万（本次实测） | 38–79 ms | 0.36 s |
  | 500 万 | 0.4–0.9 s | 2–8 s |
  | 5000 万 | 4–9 s | 20–80 s |
- 先膨胀的会是第 3 源：`customer_session_events` 的心跳按每次续租一行增长
  （`ADMIN-AUDIT-BLINDSPOTS-20260922.md` §2.1 已记录写入点），而它的分支只留约 1/2000 的行却要全扫。
  所以**触发线应按「心跳行数」而不是「审计行数」算**。
- 建议触发线（任一命中即按 §3.1 出迁移方案，并同时复核列表侧）：
  1. `customer_session_events` 单表 > 500 万行；或
  2. `list_audit_log` 的 COUNT 语句 P95 > 200 ms（慢查询日志/APM 口径）；或
  3. 审计页在**带日期范围**筛选下的 P95 > 1 s（实测日期范围会把 COUNT 放大 4.6–9 倍，是最贵的一档）。
- 届时的处理顺序：先按 §3.1 的第 3 源 partial index（需迁移），再单独评估列表侧
  「每行求值 `utc_timestamp_sql` + 43 万行排序」的成本（那才是当前真正的大头，且同样受迁移链约束）。

## 5. 未验证 / 不确定

1. **生产数据分布未知**：本次用合成数据（6 表 85.5 万行，心跳占比 95%）。若生产的 `audit_logs` 才是
   最大表，各分支占比会变，但「列表 > COUNT」与「无索引可用」这两条结论不依赖分布。
2. **单机单版本**：PG 16.15、`max_parallel_workers_per_gather=2`。关掉并行聚合后 COUNT 会明显变慢
   （没有 Parallel Append），§4 的外推不覆盖这种配置。
3. **不是基准测试**：EXPLAIN ANALYZE 两次运行绝对值差 2 倍（缓存/统计），`TIMING OFF`；
   同机并发、连接池排队、生产 IO 都没测。
4. **SQLite 车道不适用**：该端点是 PG-only（`AUDIT_SERVICE_UNAVAILABLE`），未评估。
5. **§2.1 的重复行只读了写入点代码**，未通过 API 端到端断言（两处写入在同一事务内的同一
   `code_id` 上成对发生，因此推断成立）；也**未能确定这是否是刻意设计**（分支 4/5 分别记录了不同的
   字段面），需要 A10 的 owner 判断。
6. 未评估前端：`client/src/admin/AuditEventsPage.tsx` 默认不带筛选（走 §3.0 的 A/C 档），
   只有操作员手填日期才进入最贵的 I 档；前端是否应默认收窄时间窗属于产品决策。

## 6. 复现方法

1. Linux 工具链容器（`uv sync --project server --locked`，容器内 venv）+ 宿主 PG 5433；
   `alembic upgrade head`（`server/migrations`，`alembic.ini`）到 §基线链尾。
2. 按 §3.0 的行数灌数（`generate_series` 一条 INSERT 一张表；`users`/`activation_code_batches`/
   `activation_codes`/`customer_devices`/`recharge_orders` 各灌最小骨架满足 FK 与 CHECK）。
3. `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)` 三种形状：现 COUNT（`SELECT COUNT(*) FROM (…UNION…) ev LEFT
   JOIN users tu …`）、现列表（+`ORDER BY ev.created_at DESC, ev.event_id LIMIT/OFFSET`）、
   去掉 `tu` 的 COUNT；筛选组合取 `event_type` / `actor_user_id` / `target_user_id` /
   `created_from`（后者用 `append_admin_date_filters` 生成的同一个 CASE 表达式）。
4. 等价性用「COUNT 值 vs 列表 `limit=1000000` 一次性取回的行数」比对（§2 的 `same=True`），
   并单测 `offset` 越界与 `limit=0` 两种边界。
