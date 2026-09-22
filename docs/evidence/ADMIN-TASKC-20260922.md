# ADMIN-TASKC-20260922 管理端会话与设备入口恢复（任务书 C）

任务/工作包：新激活方案落地任务书 §4 任务 C（P1）。
上游规格：`docs/新激活方案落地任务书-注册建号与后台签发Token-2026-09-18.md`；
`docs/前后端页面功能与接口匹配梳理-2026-09-18.md` §2.2 与 §5 P1 行。

分支 / 基线 SHA：`feat/admin-sessions-devices-20260922` / `02ac56a4`（origin/main）。
提交：`4bd700aa`（立项书，同分支附带）、`c2b69cdb`（本任务实现）。

## 1. 交付内容

### 1.1 SessionsPage 恢复挂载
组件与测试此前齐备但为孤儿——`grep -rn "SessionsPage" client/src` 在改动前只命中
`SessionsPage.test.tsx` 自身，`AdminApp.tsx` 未挂载。本次在「客户运营」组新增
**会话与设备** 页签并挂载，`readOnly` 透传。

`SessionsPage` 本身已完整遵守 `readOnly`（`SessionsPage.tsx:261` 调账提交守卫、
`:372` 强制下线按钮、`:399` 调账面板、`:473` 确认框 open），故无需改组件即满足
auditor 只读收敛；后端 `AdminWriter` 的 `AUDITOR_READ_ONLY` 为纵深兜底。

### 1.2 客户详情设备视图
新增 `client/src/admin/CustomerDeviceSection.tsx`：按 `user_id` 拉取该客户
`status=BOUND` 设备，接入 `unbindDevice`（解绑并踢会话）与
`revokeDeviceCredential`（凭据终态吊销）。挂载点 `CustomersPage.tsx` 的
`CustomerDetailView`（`Customer360Data` 之后）。

**刻意不接** `approve` / `replace-device`——属旧配对审批链，随任务书 B 退役。

交互契约：两动作均走 `ConfirmDialog`——解绑为 `reason` 级，吊销为 `reasonAndAck`
级（终态不可逆，需勾选知晓）；写操作复用 `adminWrite`（CSRF + 幂等键 + confirm +
reason），未新增自定义写通道。样式落在既有 `admin-customer-detail.css`，未引入新
视觉体系。

## 2. 改动文件

| 文件 | 改动 |
| --- | --- |
| `client/src/AdminApp.tsx` | `AdminTab` 增 `sessions`；「客户运营」组增页签；`tabPageTitles`/`navigationIcons`/渲染分支 |
| `client/src/AdminApp.test.tsx` | 导航断言纳入「会话与设备」；新增挂载用例 |
| `client/src/admin/CustomerDeviceSection.tsx` | 新增组件 |
| `client/src/admin/CustomerDeviceSection.test.tsx` | 新增 6 例 |
| `client/src/admin/CustomersPage.tsx` | 详情视图挂载设备区块 |
| `client/src/admin/CustomersPage.test.tsx` | 反转 listDevices 否定断言（见 §4） |
| `client/src/admin/admin-customer-detail.css` | 设备区块样式 |
| `docs/退款闭环立项书-负向调账与SOP对齐-2026-09-22.md` | 同分支附带的立项材料 |

服务端零改动；迁移链零改动。

## 3. 验证结果

- 新增 `CustomerDeviceSection.test.tsx` 6 例：BOUND 过滤与 URL 形状、空态、
  只读隐藏全部操作、解绑写契约（confirm/reason/幂等）与列表刷新、吊销
  `reasonAndAck` 未勾选被拦且不发请求、加载失败不伪造行。
- 客户端全量：`npm run check --workspace client` → **biome + tsc + vitest 全绿，
  115 文件 / 1890 测试通过**（exit 0）。
- 证据层级：**AUTOMATED_VERIFIED（本地自动验证）**。独立评审、远端 CI 门禁与
  真实浏览器验收尚未完成。

## 4. 需要复核的一处既有断言反转

`client/src/admin/CustomersPage.test.tsx` 原断言"进入客户详情不调用
`listDevices`"，是 `281a8284`（PR #102 删除 DevicesPage）之后的固化状态。任务书 C
正是要反转该状态，故改为断言 `listDevices` 以
`{status:"BOUND", userId:"user-1", limit:50}` 被调用。

同用例内 `listCustomerSessions` 与 `listAdminAdjustments` 两条否定断言**保持不动**
（本次未在详情内直连二者）。反转仅为这一条，且带注释说明来源与理由。

## 5. 未完成 / 未测试项

- 真实浏览器验收与 staging 链路未做；本任务为纯前端挂载，无迁移与配置变更。
- 任务书 C 的 DoD 第 3 条"管理端可查看在线会话/强制下线/查看客户设备/解绑吊销"
  中，**查看与操作路径已就位**，但未在真实多设备环境下实测"解绑即踢会话"的端到端
  效果（依赖真实设备与会话租约）。
- 认领登记（`docs/客户云版任务认领登记.md`）与任务账本 §18 回填**尚未执行**：该文件为
  多会话共享，回填在合入前统一进行可避免冲突。
- 任务书 B（激活码/配对审批退役）与任务 D（不限设备口径固化）不在本任务范围。
