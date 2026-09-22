# ADMIN-REVEAL-POLICY-20260922 provider 密钥 reveal 通道关系归档（补录）

> 补录说明：本任务已随 PR #201 合并，当时判据写在 `api.ts` 注释与提交信息里。按
> AGENTS 的证据模板在本批（PR #206）中补录。

任务：匹配梳理（2026-09-18）§5 把「provider 密钥 reveal 指向旧 `/api/admin` 前缀且
管理端未接」列为 P1 待办。**查证后判定这是已被代码执行的策略，不是待修缺陷**，
故只补注释、**行为零变更**。
PR：**#201**，合入 main 于 `bcc2c311`。基线 `origin/main @ ca938fe7`。

## 三条证据

1. **该路由只在内部设置车道**：`settings_routes.py:153`
   `POST /api/admin/settings/providers/{provider}/secrets/{field}/reveal`，守卫是
   `require_settings_admin` → `AuthenticatedUser` + `require_role("admin")`
   （`settings_routes.py:77-89`），与控制面的 `admin_session` + CSRF 是**两条不同通道**。
   控制面根本没有该路由 —— 把前缀改成 `/api/control` 只会 404。
2. **管理端"未接"是刻意的**：`SettingsPanel` 由两条车道共用，但 `revealSavedSecret`
   对 `source === "control"` **直接抛错**（`SettingsPanel.tsx:224-229`："控制台不支持
   显示已保存密钥"），且该处理器**只在 `source === "workspace"` 时才传给面板**
   （`:274`）—— 控制台连入口都没有。
3. **若要开放是安全姿态决策**：等于让控制面会话（含必须保持只读的 `auditor` 角色）
   获得读取 provider 明文密钥的能力。同条目的 `paid-test` 同理，属 §15 人工授权范围。

## 改动

`client/src/api.ts` 的 `revealProviderSecret` 补 27 行 JSDoc，写明上述通道关系、
"不要按字面把前缀改成 `/api/control`"的告警，以及若要开放需先拍板什么。

## 验证

客户端全量 119 文件 / 1943 用例通过（biome + tsc + vitest，exit 0）。无服务端改动。

## 边界与后续

本任务只归档判据，**未改变任何行为**。若产品决定开放控制面读取 provider 明文密钥，
需另立任务：补控制面路由 + 前端入口 + auditor 只读断言 + 审计事件名对齐。
证据层级：AUTOMATED_VERIFIED（本地自动验证）。
