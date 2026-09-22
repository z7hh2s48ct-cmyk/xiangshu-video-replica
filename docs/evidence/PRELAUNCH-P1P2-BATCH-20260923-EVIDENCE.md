# PRELAUNCH-P1P2-BATCH-20260923 · 上线前检查 P1/P2 前端修复批次（证据）

> 2026-09-23 上线前检查发现的前端 P1/P2 项。分支 `fix/prelaunch-p1p2-batch-20260923`，
> 基线 `origin/main = 478939b1`（#206 已合并），worktree `.worktrees/PRELAUNCH-P1P2-20260923`。
> P1-3（CSV 注入）与 P2-5（迁移锁）归 `fix/admin-preflight-20260923` 分支，本批次不含。

## 修复清单

### P1-1 · SUB_ADMIN 触达母账号操作被 403 踢回登录页（三处收口）

- `client/src/customer/SubAccountManagementPage.tsx`
  - `isSessionFailure` 只认 401：403 是权限拒绝，落在页面错误区展示服务端文案，
    不再触发 `onSessionExpired` 把在线用户踢回登录页。
  - 新增 `isMasterCaller` 属性（默认 true）：服务端 `_lock_master` 边界 =
    创建/改名/密码/角色/停用/删除，对这些入口按调用方身份隐藏，
    消除「点按钮 → 403 报错」的死路；标题同步区分母/子视角。
- `client/src/customer/CustomerCenterPage.tsx`
  - 向子账号管理页传 `isMasterCaller={isMasterAccount}`。
  - `allow_api_keys=false` 的子账号初始化 default key 恒 403：记 `defaultForbidden`
    终局标记，本次挂载内不再重放注定失败的 POST。
- 测试：`SubAccountManagementPage.test.tsx` 补 403 不登出、`_lock_master` 边界
  两组用例；`CustomerCenterPage.test.tsx` 补 default key 403 不重放用例。

### P1-2 · 调账确认弹窗方向按来源而非符号判定（运营反向操作风险）

- `client/src/admin/SessionsPage.tsx`：方向口径改 `isReversalInput`（输入符号）判定，
  与 `submitAdjustment` 成功通知（seconds < 0）同源；退款审批来源 + 正数 =
  服务端走正向加款通道，弹窗必须说「增加积分」。反向文案数字取绝对值，
  不再渲染「-5 积分」双负号。
- 测试：`SessionsPage.test.tsx` 补「按金额符号而非来源类型标注确认文案」用例。

### P1-4 · 非法草稿时长静默回落 8 秒（报价与生成不一致）

- `client/src/studio/CreationPages.tsx`：`normalizeCustomerDuration` 导出，
  非法时长钳位 4–15（与复刻路径同口径）。
- `client/src/studio/StudioWorkspace.tsx`：报价输入、确认弹窗、提交载荷三处
  全部改用同一归一函数，消除「非法 → 8 秒」旁路——用户看到的时长即实际生成时长。
- 测试：`StudioWorkspace.test.tsx` 补「非法草稿时长钳位到 15 秒提交」用例。

### P2-4 · 供应商名拦截缺口（客户侧红线第二道防线）

- `client/src/api.ts` `BRANDED_SERVICE_ERRORS` 补四条：hifly/飞影、tikhub、
  dashscope、douyidou——服务端文案当前全中性，这四条拦未来任何一处漏改。
- 测试：`api.test.ts` 补对应用例。

### P2-6 · 上传报错名词与秒传计数（两处小口径）

- `client/src/studio/ContentPages.tsx`
  - `uploadSizeError` 按实际类型报错：音频称「音频」、未知类型兜底「文件」，
    不再把音频误称为「视频」。
  - 秒传（`upload_required === false`）复用已有行，本地 total 不再 +1 虚增。
- 测试：`ContentPages.test.tsx` 补对应用例。

## 验证

- 专项：`npm run test --workspace client -- run` 六个受影响测试文件，
  518 passed / 0 failed（2026-09-23）。
- 门禁：静态门 + 四分片全量 pytest 结果见认领登记行。
- 无迁移、无新依赖、无真实付费调用、无服务端改动。
