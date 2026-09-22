# MATERIAL-UX-07 证据记录

任务/工作包：素材库改进工作任务清单 MATERIAL-UX-07（列表视图与音频分形态）
Owner / Reviewer：ZCode session (honor.pei) / 待 PR 评审分配
分支 / 基线 SHA：feat/material-ux-03-20260922（批量开发分支）/88c167e9
上游规格段落：docs/素材库改进工作任务清单-2026-09-22.md「P1 任务卡 MATERIAL-UX-07」（方案 §4.3 P1-5 + §6.3）
改动文件：client/src/studio/ContentPages.tsx、ContentPages.test.tsx、content.css、types.ts、live.ts、generated/api.ts、server/app/materials.py
失败测试或回归锁定：双视图切换 + 音频折叠不混排 + 用途三态
实现结果：网格/列表切换入保活；全部 tab 音频紧凑行；音频 tab 恒列表 + 用途标签 + 口播去向；MaterialItem.audio_purpose 派生
验证命令与通过数：前端 ContentPages 120 passed；前端全量 1968 passed
证据层级：AUTOMATED_VERIFIED（本地自动化全绿；未过真实链路不越级）
安全与可观测性：owner 围栏沿用 require_material/_scope_clause；动作审计沿既有 write_audit 口径；无新增密钥/凭据
迁移与回滚：无迁移
外部授权记录：无（未触碰真实支付/Provider/生产 COS）
未测试项：真实浏览器视觉校验（方向分档高度/拖拽高亮）建议评审时人工过目；Tauri 桌面端拖拽差异仅覆盖基础 web 事件
Lore 提交 SHA：e73cd368
