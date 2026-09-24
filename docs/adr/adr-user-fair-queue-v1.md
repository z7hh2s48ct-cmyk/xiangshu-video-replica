# ADR-001: 用户公平队列架构

> 说明：本仓库中的 CW-/T- 编号指向项目内部的任务跟踪系统，对应的任务台账与验收证据不随本仓库发布。

## Status

**Proposed** | **Accepted** | **Rejected** | **Deprecated**

*Status: Proposed*

## Context

客户版 V3 需要支持多用户场景下的公平任务调度。当前实现（`generation.py::acquire_generation_task_lease`）采用全局 FIFO 机制，所有用户任务混排在同一个队列中，由 Worker 按 `created_at, id` 顺序领取。

### 问题

在以下场景中，全局 FIFO 存在明显不足：

1. **资源饥饿**：单个用户可以创建大量任务，占据队列头部，导致其他用户任务长时间得不到处理
2. **并发不透明**：无法控制每个用户的并发度，可能导致单用户占用过多计算资源
3. **计费不公平**：付费用户在任务堆积时无法获得应有的优先级保证
4. **多实例协调困难**：在多 Worker 环境下，缺乏用户维度的任务分配可见性

### 设计目标

基于 T24 任务要求，新方案需要满足：

- 按 `user_id` 轮转，确保每个用户都有机会获得任务
- 默认每用户并发数为 1（可配置）
- 支持 A1000/B100/C10 等不同规格的用户持续获得机会
- 四 Worker 环境不重复领取同一任务
- 保持 Provider 提交的不确定性处理逻辑
- 不使用 Redis/消息队列，仅依赖 PostgreSQL 原生能力

## Decision

### 整体架构

采用**用户游标 + 行锁**的公平调度机制，所有状态保存在 PostgreSQL。

### 数据模型

新增表 `user_queue_cursors`，**按用户粒度，每用户一行**（当前主线模型一用户一激活码，见迁移 029 "one session row per activation code (one code per user)"，`user_id` 粒度与"每用户并发 1"的语义一一对应；若未来放开一用户多码，`user_id` 主键仍保持该语义）：

```sql
CREATE TABLE user_queue_cursors (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_dispatched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    running_tasks_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT valid_running_tasks
        CHECK (running_tasks_count >= 0 AND running_tasks_count <= 1)
);

-- 轮转热路径：空闲用户按最久未获得机会排序
CREATE INDEX idx_user_queue_cursors_rotation
    ON user_queue_cursors (last_dispatched_at ASC)
    WHERE running_tasks_count = 0;
```

类型说明：主线上 `users.id`、`generation_tasks.id`、`activation_codes.id` 均为 `TEXT`（迁移 001/027），全链路不使用 `UUID`，避免无意义的类型转换。

字段说明：
- `user_id`: 用户 ID（主键，即"每用户并发 1"的强制执行点）
- `last_dispatched_at`: 最后一次分发任务的时间（轮转排序依据）
- `running_tasks_count`: 当前运行中的任务数（CHECK 约束保证 0..1）

### 事务边界

四个核心事务（伪 SQL 详见 `t24-user-fair-queue-pseudo-sql.md`）：

1. **候选检查 + 任务领取**（Pattern A）：锁 cursor 行（`FOR UPDATE SKIP LOCKED`）→ 在该用户队列内取最早任务 → 计数 +1 → 提交
2. **任务完成释放**（Pattern B）：计数 -1，时间戳刷新
3. **任务失败释放**（Pattern C）：任务置 FAILED，**计数同样 -1**（FAILED 是终态，不再占用并发名额）
4. **冷启动**（Pattern D）：新用户激活时 upsert cursor 行

### 轮转策略

- 使用 `last_dispatched_at` 升序排列，优先处理最早获得机会的用户
- 每次成功领取任务后更新时间戳，使其排到队列尾部
- `FOR UPDATE SKIP LOCKED` 确保多 Worker 不会重复领取同一用户

### 并发控制

- 通过 PG 行锁 (`FOR UPDATE`) 保证线程安全
- `running_tasks_count` 的 CHECK 约束防止越界
- User 维度的锁粒度比 global lock 更细，减少 contention

### 迁移策略

- 种子：从 `generation_batches` 按 `created_by_user_id` 聚合每个用户的运行中任务数与最近任务时间（注意：任务与激活码的关联是 `generation_tasks.batch_id → generation_batches.id`，**不是** activation_code_batches——两个 "batch" 表不可混用，详见伪 SQL 修订说明）
- 历史未完成的任务继续保留原有锁机制，新生成任务走新路径
- 双轨切换由 `runtime_settings` 全局开关控制（不使用按行的 `active_migration` 列——开关是运行时全局配置，不是每行数据）

## 状态机（新增章节）

`generation_tasks.status` 与 cursor 计数的对应转移：

```text
              领取(Pattern A)              完成(Pattern B)
PENDING/QUEUED ───────────► SUBMITTING ───► RUNNING ───────► ARCHIVING/SUCCEEDED
    │               计数 0→1                    │  计数 1→0        （终态）
    │                                          │
    │   失败(Pattern C, 计数 1→0)              │ 失败(Pattern C, 计数 1→0)
    └─────────────────────────────────────────┴──────────────► FAILED（终态）

超时接管（维护任务，计数 1→0，任务回 QUEUED）：
SUBMITTING/RUNNING ── locked_until 过期 ──► QUEUED（等待重新领取）
```

- 计数 **+1** 仅发生在 Pattern A（QUEUED → SUBMITTING）
- 计数 **-1** 发生在三条路径：完成（RUNNING/ARCHIVING → 终态）、失败（→ FAILED）、超时接管（回 QUEUED）
- FAILED 是终态，**必须释放计数**（修订前伪 SQL 的 Pattern C 不释放计数会导致用户永久饥饿，见修订记录）

## 锁顺序（新增章节）

所有路径统一为 **cursor 行 → task 行** 的单向锁序：

1. **领取路径**：`SELECT ... FROM user_queue_cursors FOR UPDATE SKIP LOCKED`（拿 cursor 锁）→ `UPDATE generation_tasks ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)`（拿 task 锁）
2. **完成/失败路径**：先 `UPDATE user_queue_cursors`（拿 cursor 锁）→ 再 `UPDATE generation_tasks`（拿 task 锁）

若完成路径反过来（先锁 task 再锁 cursor），会与领取路径形成锁环：W1 持 cursor 等 task，W2 持 task 等 cursor → 死锁。统一先 cursor 后 task 后环消失。

与现有全局 FIFO 的双轨兼容：旧路径 `acquire_generation_task_lease` 只更新 `generation_tasks` 行（不触碰 cursor 表），与 cursor 行不存在锁交叉；新路径独占 cursor 行锁。

## Consequences

### 正面影响

- ✅ 用户维度公平：每个用户都有稳定的任务处理机会
- ✅ 并发可控：通过数据库约束强制执行 per-user 上限
- ✅ 零外部依赖：仅使用 PostgreSQL 原生的 SELECT FOR UPDATE 机制
- ✅ 可观测性强：`user_queue_cursors` 提供实时用户维度的调度可见性
- ✅ 向后兼容：不影响现有 Provider 重试和不确定处理逻辑

### 风险与挑战

- ⚠️ 冷启动问题：新用户首次获得机会前无历史行为记录（需初始化逻辑）
- ⚠️ 长尾用户：某些用户可能长期不活跃但占用 cursor 记录（需 cleanup 策略）
- ⚠️ 锁竞争热点：高峰时段 `user_queue_cursors` 表的 UPDATE 可能存在 contention
- ⚠️ 查询计划变化：需要针对新索引进行 EXPLAIN ANALYZE 调优
- ⚠️ **存量 SQLite 方言遗留**：`generation.py` 中 `datetime(locked_until)` 是 SQLite 函数，PG 运行时不存在该函数；T25 实现时须一并替换为 PG 原生的 `locked_until::timestamptz <= now()`（详见伪 SQL 修订记录）

### 监控指标建议

- 每个用户的 `running_tasks_count` 分布直方图
- `user_queue_cursors` 表的锁等待时间
- 任务领取延迟：从 QUEUED 到 SUBMITTING 的时长
- 用户饥饿检测：连续 N 分钟未获得机会的用户数量

## Alternatives Considered

### Alternative 1: 多队列分级 priority queues

为不同付费等级维护独立队列，Worker 按权重轮询。

**Pros**: 付费用户优先；**Cons**: 复杂度过高，违背"简单可靠"原则

### Alternative 2: Token bucket 限流器

使用令牌桶算法控制每个用户的任务发放速率。

**Pros**: 更平滑的流量控制；**Cons**: 需要额外状态机，增加复杂度

### Alternative 3: Redis + Lua 脚本

使用 Redis 原子操作实现调度器。

**Pros**: 性能更好；**Cons**: **违反禁止引入 Redis 的红线**，且增加运维依赖

## 修订记录（纳入主线时修订）

T24 分支原始版本（commit 6a1a325）评审发现并修正的问题：

1. **UUID 误用**：原 DDL 以 `code_id UUID PRIMARY KEY` 为主键并给 `users(id)` 引用标 UUID；主线三表主键均为 TEXT，已统一
2. **粒度不一致**：原主键是 code_id（激活码）但语义是"每用户并发 1"；已改为 `user_id` 主键，粒度与语义一致
3. **SQLite 方言**：`datetime(locked_until) <= CURRENT_TIMESTAMP` 是 SQLite 函数，PG 下不存在；改为 `locked_until::timestamptz <= now()` 并标注存量代码同类问题
4. **WITH HOLD 误用**：`FOR UPDATE SKIP LOCKED WITH HOLD` 是游标语法，普通 SELECT 无此子句，已删除
5. **伪回滚**：Step 3 失败时的"UPDATE 回滚 Step 2"是伪回滚——同一事务内 cursor 行已被 FOR UPDATE 锁定，UPDATE 必然命中，且显式 UPDATE 也会被 ROLLBACK 撤销；已删除
6. **FAILED 计数永不释放**：原 Pattern C 对 FAILED 任务不递减计数，用户永久占用并发名额（饥饿）；已修正为同样释放
7. **schema 引用错误**：`activation_codes.code_id`、`generation_tasks.code_id` 列不存在；`ac.batch_id = gb.id` 把 activation_code_batches 与 generation_batches 混为一谈；已按真实链路（task→batch→created_by_user_id）重写
8. **迁移开关**：按行的 `active_migration` 列改为 `runtime_settings` 全局开关（开关是运行时全局配置）

## Implementation Notes

### T24 交付物

- [x] `docs/adr/adr-user-fair-queue-v1.md` - 本文档
- [x] `docs/adr/t24-user-fair-queue-pseudo-sql.md` - 伪 SQL + 边界条件分析

### T25 后续工作

- [ ] 实现迁移（`user_queue_cursors` 表 + 种子 + runtime_settings 开关）
- [ ] 修改 `acquire_generation_task_lease` 集成新逻辑（含存量 `datetime()` 方言替换）
- [ ] Worker 并发测试（4 Worker × N 用户 × M 任务）
- [ ] 压测报告（10,000 任务规模）

## References

- Current Implementation: `server/app/generation.py::acquire_generation_task_lease` (L3512)
- PostgreSQL Documentation: [SELECT FOR UPDATE](https://www.postgresql.org/docs/current/sql-select.html)

---

*Created: 2026-08-25*
*Maintained by: Architecture Team*
