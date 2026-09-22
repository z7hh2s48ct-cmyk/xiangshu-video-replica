# MATERIAL-UX-05 证据记录

任务/工作包：素材库改进工作任务清单 MATERIAL-UX-05（标签底座）
Owner / Reviewer：ZCode session (honor.pei) / 待 PR 评审分配
分支 / 基线 SHA：feat/material-ux-03-20260922（批量开发分支）/ae83ab8a
上游规格段落：docs/素材库改进工作任务清单-2026-09-22.md「P1 任务卡 MATERIAL-UX-05」（方案 §4.3 P1-1）
改动文件：server/migrations/versions/20260922T2200_material_preference_tags.py、manifest.json、server/app/materials.py、material_routes.py、tests/test_material_tags.py、前端契约与 ContentPages、分片清单
失败测试或回归锁定：迁移升降级 + JSONB 包含过滤 + 规整上限
实现结果：tags_json TEXT-JSON 默认 '[]' 零回填（初版 JSONB 因 SQLite 历史迁移 lane 不可渲染与 reconcile 列比对 fail-closed 被修复为 TEXT + 查询侧 ::jsonb 转型，commit fe1aa50d）；PATCH tags 全量覆盖（None 不动/[] 清空）；>20 个或 >40 字 422；聚合 count DESC,tag ASC
验证命令与通过数：PG 契约 9 passed；前端 ContentPages 113 passed；迁移 head 断言五文件 bump；manifest --record 重记
证据层级：AUTOMATED_VERIFIED（本地自动化全绿；未过真实链路不越级）
安全与可观测性：owner 围栏沿用 require_material/_scope_clause；动作审计沿既有 write_audit 口径；无新增密钥/凭据
迁移与回滚：迁移 20260922T2200（downgrade 删列，标签为增强数据可弃）；migration guard manifest 已重记
外部授权记录：无（未触碰真实支付/Provider/生产 COS）
未测试项：真实浏览器视觉校验（方向分档高度/拖拽高亮）建议评审时人工过目；Tauri 桌面端拖拽差异仅覆盖基础 web 事件
Lore 提交 SHA：cfc0359c
