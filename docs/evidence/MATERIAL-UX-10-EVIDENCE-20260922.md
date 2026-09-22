# MATERIAL-UX-10 证据记录

任务/工作包：素材库改进工作任务清单 MATERIAL-UX-10（使用记录）
Owner / Reviewer：ZCode session (honor.pei) / 待 PR 评审分配
分支 / 基线 SHA：feat/material-ux-03-20260922（批量开发分支）/2966120c
上游规格段落：docs/素材库改进工作任务清单-2026-09-22.md「P2 任务卡 MATERIAL-UX-10」（方案 §4.4 P2-4）
改动文件：server/app/materials.py、material_routes.py、tests/test_material_usages.py、client/src/studio/ContentPages.tsx、ContentPages.test.tsx、api.ts、generated/api.ts、分片清单
失败测试或回归锁定：零引用 + generation/oral 双引用列举 + 他人素材 404
实现结果：按需单素材查询（LIMIT 20 + COUNT(*) OVER 全量计数）；generation 经 batch.created_by_user_id 归属；直出成片恒为空
验证命令与通过数：后端 usages 3 passed；前端全量 1970 passed
证据层级：AUTOMATED_VERIFIED（本地自动化全绿；未过真实链路不越级）
安全与可观测性：owner 围栏沿用 require_material/_scope_clause；动作审计沿既有 write_audit 口径；无新增密钥/凭据
迁移与回滚：无迁移
外部授权记录：无（未触碰真实支付/Provider/生产 COS）
未测试项：真实浏览器视觉校验（方向分档高度/拖拽高亮）建议评审时人工过目；Tauri 桌面端拖拽差异仅覆盖基础 web 事件
Lore 提交 SHA：487d3a55
