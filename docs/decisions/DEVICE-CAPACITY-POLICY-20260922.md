# 决策记录：客户设备数不设上限（`users.max_devices` 为预留列）

> 说明：本仓库中的 CW-/T- 编号指向项目内部的任务跟踪系统，对应的任务台账与验收证据不随本仓库发布。

## Status

**Accepted** — 本记录把「客户设备数不设上限」的口径写进代码注释与本文，
并补一条回归测试钉住（防将来补容量检查时被默认值 2 卡死）。行为零变更。

## Context

新激活方案（2026-09-18 拍板）把获客入口改为**客户自助注册建号**，密码登录任意
设备、**不限设备台数**。旧方案的"一码两槽位 + 配对审批"随任务 B 退役。

迁移 086（`086_remove_device_slot_constraints.py`）已把旧的硬上限从数据库层拆掉：

- 删除 `uq_customer_devices_slot`（每槽位唯一）与 `ck_customer_devices_slot_range`（`slot_no IN (1,2)`）
- 新增 `users.max_devices INTEGER NOT NULL DEFAULT 2` + `CHECK (max_devices >= 1)`，
  其 `server_default` 取自 `_DEFAULT_MAX_DEVICES = 2`（`customer_device_service.py:31`）
- 086 的降级守卫要求所有用户 `max_devices` 均为 2 才允许回滚

**但代码层从未补上任何容量检查。** 于是产生了一处系统性口径债：数据库已经不限，
而代码注释、route docstring 与测试 docstring 仍在用"两位槽位/有上限"的语言描述，
其中若干处直接给出了**与实现相反**的断言。

## 事实核查（逐条可复现）

| 断言 | 实际情况 | 证据 |
|---|---|---|
| 设备数有上限 | **无**。全仓无任何把设备数与 `users.max_devices` 比较的代码 | `grep -rn max_devices server/app` 只命中两处读取点，均为 SELECT 进响应体，无比较 |
| `next_free_slot()` 无空位时返回 `None` | **不可能返回 `None`**。取 `range(1, len(taken)+2)` 中最小的未占用序号，该区间有 `len(taken)+1` 个值、至多 `len(taken)` 个被占，必有空位 | `customer_device_service.py:317-327`，其 docstring 自述 "without a device count cap" |
| `consume_pairing_request()` 返回 `None` 表示"两槽位已占" | **不可达**。它只在 `next_free_slot` 为 `None` 时返回 `None`，而后者不可能为 None | `customer_device_service.py:710-712` |
| 第三台设备会被 409 `DEVICE_SLOTS_FULL` 拦截 | **两处 raise 均不可达** | `customer_device_routes.py:1276`（消费路径）、`:1320`（配对准入路径） |
| PostgreSQL 会拒绝第三行 `BOUND` | **不会**。086 已删除相关索引与 CHECK | `test_customer_devices.py:714` 的 docstring 已自认这一点，但同文件仍有相反的旧断言 |

## Decision

1. **`users.max_devices` 是预留列**：为将来可能的容量策略预留，当前**
   不参与任何准入判断**。它只作为展示字段（`device_slots_used / max_devices`）
   出现在客户列表与客户 profile 响应中。
2. **当前语义为"不限设备"**：设备槽位取"最小未占用序号"分配，随设备增减自然增长；
   不存在"槽位耗尽"状态。
3. **`DEVICE_SLOTS_FULL` 是历史遗留的错误码**：两处 raise 均为不可达分支。本次**不删除**
   （配对链路随任务 B 退役，删除会扩大改动面），但就地标注不可达，避免下一位读者
   据此以为存在上限。
4. **将来若要真的引入上限**，必须同时：改本决策记录、新增迁移（`max_devices` 已有
   CHECK 与 `server_default` 可用）、补"超限被拒"的负向测试，并核对 086 的降级守卫。
   默认值 2 不得被当作既有容量契约使用。

## Consequences

- 客户可在任意多台设备登录、绑定；设备管理页的"槽位"是展示序号，不是配额。
- 运营侧不要向客户承诺"最多 2 台设备"；`max_devices` 的 2 只是迁移默认值。
- 回归由 `test_third_device_enroll_consumes_a_third_slot` 钉住（本次新增）：
  已有两台 `BOUND` 时，第三台走完 enroll → approve → consume 必须成功绑定。

## 相关位置（本次同步修正的注释/文档）

| 位置 | 原表述 | 处置 |
|---|---|---|
| `server/app/customer_device_service.py:26-31` | 仅说常量是迁移默认值 | 补"预留列、当前不启用"与本文指引 |
| `server/app/customer_device_service.py:688-694` | `None` 表示两槽位已占 | 改为"不可达；无上限" |
| `server/app/activation_code_routes.py:129-131` | "设备上限由 `next_free_slot()` 预检查对 `users.max_devices` 强制" | **事实错误**，改为"无上限" |
| `server/app/customer_device_routes.py:31,65` | 两槽位占满答 409 | 标注该分支不可达 |
| `server/app/customer_device_routes.py:1270,1315-1321` | 两槽位已占 → 409 | 就地标注不可达 |
| `server/tests/test_customer_devices.py` 模块 docstring | "两槽位"、"`next_free_slot` 为 `None`"、"PG 拒绝第三行" | 按实际口径改写 |
| `server/tests/test_customer_devices.py:714-720` | "第三设备拦截在 service 层、由本模块 API 测试钉住" | **与同文件 `test_third_device_can_request_pairing` 结论相反**，改写 |

## 未变更

无迁移、无 route 行为变更、无响应结构变更。`DEVICE_SLOTS_FULL` 错误码保留在
既有位置（不可达），配对链路本身随任务 B 退役。

## 验证

| 项 | 结果 |
|---|---|
| 新增回归测试 `test_third_device_enroll_consumes_a_third_slot` | 通过。两台 `BOUND` 时第三台 enroll→approve→consume 返回 201、`slot_no=3`，且断言此时 `users.max_devices` 仍为 2 —— 两者并存即"该列不生效"的直接证据 |
| PG 定向（本改动文件所在三个模块） | **152 passed**（`test_customer_devices` / `test_customer_activation` / `test_customer_sessions`） |
| 静态门 | `ruff check`、`ruff format --check`、`mypy`（178 文件）全清 |
| 证据层级 | AUTOMATED_VERIFIED（本地自动验证）；独立评审与远端 CI 未完成 |

注：PG 测试在 Windows 本机不可运行（`pg_test_kit` 顶层 `import fcntl`），上述结果
取自 Docker 内 Linux 工具链连宿主 PG。

本次**行为零变更**，且该结论经机械验证而非人工目测：对三个被改的 `server/app`
模块，剥离 docstring 后比对 HEAD 版与工作区版的 AST（`ast.dump`），三者均
**完全一致** —— 一切改动都落在 docstring/注释里。业务分支一行未动（含两处不可达
`DEVICE_SLOTS_FULL` raise 的保留）。
