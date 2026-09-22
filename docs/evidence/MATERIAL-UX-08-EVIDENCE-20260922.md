# MATERIAL-UX-08 证据记录

任务/工作包：素材库改进工作任务清单 MATERIAL-UX-08（宽高比数据与方向筛选）
Owner / Reviewer：ZCode session (honor.pei) / 待 PR 评审分配
分支 / 基线 SHA：feat/material-ux-03-20260922（批量开发分支）/e73cd368
上游规格段落：docs/素材库改进工作任务清单-2026-09-22.md「P1 任务卡 MATERIAL-UX-08」（方案 §6 P1/P2）
改动文件：server/app/materials.py、material_routes.py、tests/test_material_dimensions.py、test_material_orientation.py、前端契约与 ContentPages、content.css、分片清单
失败测试或回归锁定：三种容器解析边界 + 方向三态命中 + 缺尺寸不命中
实现结果：视频 ffprobe 顺手取宽高；PNG/JPEG/WebP 头解析失败返回 None 不阻塞；orientation 阈值 >1.05/<0.95；媒体区分档 200/160/120
验证命令与通过数：后端 14 passed（PG 6 + 单元 8）；前端全量 1966 passed
证据层级：AUTOMATED_VERIFIED（本地自动化全绿；未过真实链路不越级）
安全与可观测性：owner 围栏沿用 require_material/_scope_clause；动作审计沿既有 write_audit 口径；无新增密钥/凭据
迁移与回滚：无迁移（宽高入既有 metadata_json）
外部授权记录：无（未触碰真实支付/Provider/生产 COS）
未测试项：真实浏览器视觉校验（方向分档高度/拖拽高亮）建议评审时人工过目；Tauri 桌面端拖拽差异仅覆盖基础 web 事件
Lore 提交 SHA：f11436a9
