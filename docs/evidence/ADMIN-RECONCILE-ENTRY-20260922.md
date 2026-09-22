# ADMIN-RECONCILE-ENTRY-20260922 首帧任务对账管理入口（补录）

> 补录说明：本任务已随 PR #200 合并，当时只留了认领登记段与提交信息。按
> AGENTS 的证据模板在本批（PR #206）中补录，便于追溯。

任务：匹配梳理（2026-09-18）§5「首帧任务管理 reconcile 无入口」。
上游：`docs/前后端页面功能与接口匹配梳理-2026-09-18.md` §5；`docs/管理端优化进展与待办-2026-09-22.md`。
PR：**#200**，合入 main 于 `f8ff937d`。基线 `origin/main @ ca938fe7`。

## 交付

后端 `POST /api/control/first-frame-tasks/{task_id}/reconcile`
（`admin_first_frame_routes.py:40`，`AdminWriter`）一直在，但前端零引用（仅
`generated/api.ts` 有生成的类型）。实现：

- `api.admin.ts` 增 `reconcileFirstFrameTask`
- 生成记录页详情区补「重新对账」按钮，仅 `FIRST_FRAME_IMAGE` + `SUBMISSION_UNCERTAIN`
  + 非只读时显示（服务端也只在 UNCERTAIN 放行，其余 409 `IMAGE_TASK_NOT_UNCERTAIN`）
- **补上该页此前缺失的 `readOnly` prop** 并在按钮处门禁 —— 否则审计员能看到按钮却只
  拿到 403 `AUDITOR_READ_ONLY`
- `ConfirmDialog` 用 `standard` 级：该端点不消费 `reason`，服务端按供应商真实回执
  自裁决并写审计（`image_tasks.py` 的 `write_audit` 用 `decision`/`detail_code`），
  要求运营手填一个被丢弃的原因只会制造"已经留痕"的错觉
- 成功文案区分两种结局（RESUMED 回队列 / FAILED 置失败），后者已核实会调
  `finish_source(units=0)` 释放预扣

## 同条目的另外两项：前提不成立，未实现

- 口播 `POST /api/oral/tasks/{id}/refresh` 是 `AuthenticatedUser` 的**客户端**路由
  （只重读任务），"无前端入口"指客户端缺口，**不是管理端缺口**
- 口播 `billing-reconcile` 需 6 个字段（含证据资产、供应商真实结果、处置方式），属
  **需设计的运营表单**，建议单独立项

## 验证

- 客户端全量 119 文件 / 1948 用例通过（biome + tsc + vitest，exit 0）
- 新增 `GenerationRecordsPage` 4 例、`api.admin` wrapper 1 例
- 服务端零改动

## 边界

未做真实浏览器验收；"对账后余额释放"依赖真实供应商回执，未实测。
证据层级：AUTOMATED_VERIFIED（本地自动验证）。
