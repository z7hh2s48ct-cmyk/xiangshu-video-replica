# MATERIAL-UX-03 证据记录

任务/工作包：素材库改进工作任务清单 MATERIAL-UX-03（浏览与信息补全）
Owner / Reviewer：ZCode session (honor.pei) / 待 PR 评审分配
分支 / 基线 SHA：feat/material-ux-03-20260922（批量开发分支）/de5f09cb
上游规格段落：docs/素材库改进工作任务清单-2026-09-22.md「P0 任务卡 MATERIAL-UX-03」（方案 §4.2 P0-3 + P0-5）
改动文件：server/app/materials.py、server/app/material_routes.py、client/src/studio/ContentPages.tsx、live.ts、types.ts、api.ts、generated/api.ts、server/tests/test_material_browse_info.py、scripts/ci/test-shards
失败测试或回归锁定：默认排序逐字兼容 + 排序分页组合 + 越权返回空
实现结果：4 种排序；person/project 筛选与 5 分支 CTE 兼容；归属名派生（口播补 LEFT JOIN）
验证命令与通过数：后端专项 38 passed（PG）；前端 ContentPages 105 passed；ruff/mypy 干净
证据层级：AUTOMATED_VERIFIED（本地自动化全绿；未过真实链路不越级）
安全与可观测性：owner 围栏沿用 require_material/_scope_clause；动作审计沿既有 write_audit 口径；无新增密钥/凭据
迁移与回滚：无迁移
外部授权记录：无（未触碰真实支付/Provider/生产 COS）
未测试项：真实浏览器视觉校验（方向分档高度/拖拽高亮）建议评审时人工过目；Tauri 桌面端拖拽差异仅覆盖基础 web 事件
Lore 提交 SHA：3f55f83d
