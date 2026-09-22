# PRELAUNCH-P1P2-20260923 · 上线前检查 P1/P2 修复批次（个人中心 + 管理端弹窗 + 独立创作 + 公共层）

- Owner：ZCode 会话（GLM-5.3）代 honor.pei / sess_3964a657
- Reviewer：执行者自检 + PR 门禁（三门禁）；审查发现来自 2026-09-23 上线前检查（基线 1606d59e→478939b1 的四路代码审查）
- 基线：`origin/main@478939b1`（#206 已合并）；分支 `fix/prelaunch-p1p2-batch-20260923`；worktree `.worktrees/PRELAUNCH-P1P2-20260923`；原子认领 `codex-task-claims/PRELAUNCH-P1P2-20260923/claim.json`
- 与并行任务分工：`fix/admin-preflight-20260923`（另一会话在制）已认领 **P1-3（CSV 注入防护）与 P2-5（迁移加锁）**，本批次**不做**这两项，文件边界零重叠（彼改 export_controller.py/control_routes.py/deploy/，本批不触）。彼分支基于 aa1a6ab0 落后 main 1 提交，其中"微信自检 CSRF"与已合并的 #206 重复，合并前需重挂处理——已在交接说明中提示。

## 修复清单（TDD：先红后绿）

### P1-1 SUB_ADMIN 403 误判会话过期强制登出 + 按钮未裁剪
- `SubAccountManagementPage.tsx`：`isSessionFailure` 只认 401（403 是权限拒绝，落到页面错误区展示服务端中文文案）；新增 `isMasterCaller` prop（默认 true），创建表单 + 重命名/设置密码/设为管理员/停用/启用/删除按钮按母账号裁剪（与服务端 `_lock_master` 边界一致：SUB_ADMIN 保留列表/设置额度/设置权限）。
- `CustomerCenterPage.tsx`：传入 `isMasterCaller={isMasterAccount}`。
- 测试：新增 2 用例（403 不登出 + SUB_ADMIN 视图裁剪），文件 25/25 绿。

### P1-2 反向调账确认弹窗方向按输入符号判定（"调账符号"前科复发形态）
- `SessionsPage.tsx`：新增 `isReversalInput = Number(credits) < 0`；表单标题、提交按钮、ConfirmDialog 的 title/confirmLabel/description 全部改按输入符号；负数分支展示绝对值（"反向调账 5 积分（账本扣减）"），与 `submitAdjustment` 成功通知（`seconds < 0`）同源。来源类型 `isReversalSource` 继续只管输入约束（min/hint）。
- 测试：新增"退款审批来源+正数 → 弹窗显示增加积分口径"用例；既有 B1 用例断言随文案形态更新（`-5 积分`→`5 积分`，方向由「扣减」表达）。12/12 绿。

### P1-4 独立创作页时长非法值静默回落 8 秒（#179 决策的残留旁路）
- `CreationPages.tsx`：`normalizeCustomerDuration` 导出（4–15 钳位）。
- `StudioWorkspace.tsx`：**三处**回落（`videoQuoteInput`、提交参数 `output_duration_seconds`、弹窗报价派生 `videoDuration`）全部改用钳位；报价与提交同源归一。
- 测试：新增"duration=20 → 报价/提交均按 15"用例（该测试同时暴露了第三处遗漏，正是弹窗报价曾回落 8 导致报价参数不匹配）。110/110 绿。

### P2-1 删除 useGenerationDrafts 死代码（1738 → 141 行）
- hook 本体（91–1277）+ 专属辅助（shouldRecoverScriptRewrite/rewriteXxx/localDraftXxx 等）+ 只被 hook 引用的私有函数（含 **normalizeDurationOption 的 4/15 两档折叠门禁**——审查指出的"复用即带回 8 秒发 4 秒 bug"风险源）全部删除。
- 保留 live.ts 仍在用的幂等三函数（restore/restoreOrCreate/clear）+ 测试重置 + `IdempotencyRecord`/`isIdempotencyRecord`/`requestFingerprint`/`createIdempotencyKey`；文件名不变避免 import churn，文件头注明收敛史。
- 验证：tsc/biome 干净；live.test.ts 83/83。

### P2-2 改密/退出所有设备成功后的定制文案落到终屏
- `useCustomerSession.ts`：`expireSessionLocally(notice?)` 参数化 + `expiredNotice` 状态（传输层 401 过期清空，防旧文案冒充新原因）。
- `RootApp.tsx`：`session-expired` 终屏 description 优先用 notice。
- `workspace-shell.ts`/`CustomerWorkspace.tsx`：`onSessionExpired: (notice?: string) => void` 透传。
- `CustomerCenterPage.tsx`：`onSessionsEnded` 改为 `account.onSessionExpired(reason)`——不再走注定 401 的二次 logout（旧实现传输层 EXPIRED 事件抢先切屏，setNotice 的说明永远不可见）。
- 测试：useCustomerSession 31/31（新增 notice 带入+清理用例）；CustomerCenterPage 40/40（"退出所有设备"用例反转为断言 onSessionExpired 携带 reason、不再 onLogout）。

### P2-3 子账号设置密码/额度 window.prompt → 受控弹窗
- 复用既有 `sub-account-modal` 样式与交互规格：密码弹窗（type=password 遮蔽、6–128 前端校验与服务端 `validate_password_policy` 同口径、失败留弹窗可重试）；额度弹窗（预填现值、留空清除、沿用 parseQuotaInput 校验）。
- 测试：额度两用例改写为弹窗交互；新增密码弹窗用例（遮蔽断言/过短就地报错不发请求/成功 POST `/sub-1/password`）。26/26 绿。

### P2-4 供应商文案过滤表补 4 pattern（纵深防御）
- `api.ts` `BRANDED_SERVICE_ERRORS` 补 hifly|飞影 / tikhub / dashscope / douyidou（对应中性文案：数字人口播/爆款数据/音频转写/链接解析服务）。服务端文案当前全中性，此为第二道防线。
- 测试：api.test.ts 表驱动 +4 用例，11/11 绿。

### P2-6 三个小杂项
- 秒传 total 虚增：`ContentPages.tsx` `upload_required === false` 时本地计数 +0（服务端未新增行）。
- 音频超限文案：`uploadSizeError` 按 MIME 三态命名（图片/音频/视频/文件兜底），不再把音频误称"视频"。
- Token 初始化 403 重放：`CustomerCenterPage.tsx` 新增 `defaultForbidden` ref，403（allow_api_keys=false 子账号）终局后本次挂载不再重放注定失败的 POST。
- 测试：ContentPages（UX-04 段）5/5；CustomerCenterPage 40/40（新增 403 不重放用例）。

## 验证

- 分项（开发期）：各专项 vitest 见上，全部先红后绿。
- 收尾全量：`npm run check:sharded`（静态门 + 4 片独立 PG 并行 pytest）——结果见 PR 描述（本文件在 push 前回填实际数字）。
- 不含：cargo/审计/build（CI 三门禁承载，本批零 Rust/依赖/构建配置改动）。

## 边界与不做

- 不修 P1-3（CSV 注入）/P2-5（迁移锁表）——归 ADMIN-PREFLIGHT-20260923。
- 无服务端业务改动、无迁移、无新依赖、无真实付费调用、无生产变更。
- 桌面端未实测 window.prompt 旧路径行为（P2-3 已把它替换为受控弹窗，该风险随之消除）。
