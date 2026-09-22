# MATERIAL-UX-01 证据记录（分组与批量）

> 素材库改进任务清单第一项（P0）：分组从"逐条自由文本"升级为"可导航、可批量维护的轻实体"；列表支持多选批量操作（整理模式），解决"改 20 个素材分组要点 20 次"。信息架构按 §4.3 P1-2 布局确认稿落地（左侧分组导航 + 整理模式）。

## §14 证据记录

```text
任务/工作包：MATERIAL-UX-01-20260922（素材库改进任务清单 P0 第一项；
  方案 §4.3 P1-2 信息架构 + §4.4 P0-1/P0-2 分组与批量）
Owner / Reviewer：Qoder session (honor.pei) 代 honor.pei / 待 PR 评审分配
分支 / 基线 SHA：feat/material-ux-01-20260922 / origin/main@7834b045
上游规格段落：素材库改进工作任务清单-2026-09-22.md §MATERIAL-UX-01；
  素材库重分析与改进方案-2026-09-22.md §4.3（信息架构布局）、P0-1/P0-2
改动文件：server/app/materials.py（新增 MaterialGroupItem / MaterialGroupsResponse /
  MaterialBulkUpdate / MaterialBulkRequest / MaterialBulkResult 模型；
  _group_expression 读侧 BTRIM 归一；list_material_groups 聚合；
  bulk_update_materials 逐条 owner 校验 + 单条汇总审计 + 去重排序）；
  server/app/material_routes.py（GET /api/studio/materials/groups、
  PATCH /api/studio/materials/bulk、列表 group 查询参数）；
  server/tests/test_material_groups_bulk.py（26 用例，818 行）；
  client/src/api.ts（listMaterialGroups / bulkUpdateMaterials、
  listMaterials 增加 group 过滤）；client/src/generated/api.ts（OpenAPI
  codegen 重生成 +468）；client/src/studio/ContentPages.tsx（分组导航 /
  整理模式跨页选择集 / 批量条 / 上传选分组 / 详情分组选择器）；
  client/src/studio/content.css（导航与批量条样式 +114）；
  client/src/studio/ContentPages.test.tsx（+2 用例）；
  scripts/ci/test-shards/shard-{0..3}.txt（注册新测试文件，覆盖门禁
  build-test-shards.py --check-coverage 通过）
失败测试或回归锁定：两组反向验证（TDD 红→绿落盘）——①后端（26 实例）：`git checkout origin/main -- server/app/materials.py server/app/material_routes.py` 回退实现后 `uv run python -m pytest tests/test_material_groups_bulk.py -q` → 22 failed / 4 passed（失败形态为断言级而非收集错误：分组聚合计数与排序、group 筛选 trim/中文/未分组语义、bulk 越权逐条跳过、上限/空值/未知字段拒绝、契约与模型边界；4 个通过为不依赖新端点的通用前置断言）；`git checkout HEAD --` 恢复后 26 passed。②前端（ContentPages.test.tsx 102 实例）：回退 ContentPages.tsx / api.ts / generated/api.ts / content.css 后 `npx vitest run src/studio/ContentPages.test.tsx`（client/ 目录内，须加载 client/vite.config.ts 的 jsdom 环境）→ 2 failed / 100 passed（恰为新增两条：分组导航按分组筛选、详情面板改入新建分组）；恢复后 102 passed
实现结果：①后端新增 `GET /api/studio/materials/groups`：按有效分组
  （COALESCE(NULLIF(BTRIM(group_override),''), base_group)）聚合当前用户
  可见、未隐藏素材，count 降序 + name 升序；同一 contact sheet 的派生
  视图只计一条（_grouped_character_clause）；②列表 API 增加 group 精确
  筛选（含"未分组"空语义），不改 5 分支 UNION 结构；③新增
  `PATCH /api/studio/materials/bulk`：{material_ids ≤100, update:{group?,
  hidden?}} 逐条 owner 校验（require_material 走既有属主范围），越权 /
  缺失 / 畸形 id 逐条 skipped，generation 直出成片 group 变更逐条降级
  跳过（单条 409 的批量等价语义），仅 hidden 走 preserve_overrides 绝不
  覆盖 title/group；全程只写一条汇总审计 studio.material.bulk_update
  （requested/updated/skipped/group_changed/hidden_changed）；④边界：
  >100 拒绝、空列表拒绝、未知字段拒绝、空白分组 422、auditor 403；
  ⑤前端分组导航（全部 / 未分组 / 各分组 + 计数）、整理模式（卡片勾选、
  全选本页、跨页 Set 累积不清空、批量条移入分组 / 恢复默认分组）、详情
  面板分组选择器 + 新建分组、上传流程选择目标分组（默认"我的上传"）；
  ⑥评审修复（6054183e）：读侧 BTRIM 归一防"幽灵分组"（存量首尾空格
  override 导航可见点进去为空）、写入侧统一 strip（_upsert_preference）、
  bulk 入参去重排序（重复 id 不多计 updated + 消除乱序行锁死锁面）、
  切换筛选时重置选择集
验证命令与通过数：server 专项 `uv run python -m pytest tests/test_material_groups_bulk.py -q` → 26 passed；前端专项 `npx vitest run src/studio/ContentPages.test.tsx`（client/ 目录）→ 102 passed；`npm run check:static` 全绿（前端 vitest 全量 1868 passed / 113 test files 含 e2e_contract 契约、tauri cargo fmt/check、ruff 423 files、mypy 174 source files）；`bash scripts/ci/run-pytest-shards.sh` 四片全绿 751+766+847+611 = 2975 passed / 1 skipped（每片独立 PG 容器端口 5433+i，跑完自动 teardown；与 MATERIAL-UX-14 分片串行执行不叠加容器名）
证据层级：AUTOMATED_VERIFIED（本地自动化；真实客户环境验收归 staging 推进）
安全与可观测性：批量接口逐条 owner 校验沿既有 _scope_clause 属主范围，
  越权素材跳过不泄露存在性；汇总审计落 write_audit（action
  studio.material.bulk_update）；require_not_auditor 拒绝 auditor 角色；
  无新增密钥 / 外呼端点
迁移与回滚：零迁移（复用 studio_material_preferences 既有表与 upsert
  语义，不建 material_groups 实体表）；还原本 PR 文件即回滚，已写入的
  分组偏好为既有数据形态、不影响旧客户端
外部授权记录：无（未触碰生产 COS / 付费 Provider / 发码）
未测试项：真实客户 Tauri 环境与真实数据规模下的分组聚合性能（归 staging）；
  存量历史 override 数据的 BTRIM 归一为读侧行为，未做批量数据迁移；
  Windows NSIS 门禁归 CI 三门禁
Lore 提交 SHA：以 PR 当前 head 为准
```

## 门禁执行记录

- 2026-09-22：`npm run check:static` 全绿（前端 vitest 全量 1868 passed /
  113 test files，含 e2e_contract.test.tsx 契约与新增 2 个前端用例；
  tauri cargo fmt/check；ruff 423 files already formatted；mypy 174 source
  files no issues）——含 CodeReview 修复后重跑。
- 2026-09-22：`bash scripts/ci/run-pytest-shards.sh` 四片全绿（751+766+847+611 =
  2975 passed / 1 skipped；每片独立 PG 容器端口 5433+i，跑完自动 teardown）。
- 2026-09-22：反向验证（详见 §14「失败测试或回归锁定」）——后端回退 22 failed /
  4 passed → 恢复 26 passed；前端回退 2 failed / 100 passed → 恢复 102 passed。
  注：首轮前端反向验证在 worktree 根目录误跑 `npx vitest`（未加载 client
  vite.config.ts，jsdom 缺失致 100 failed），已在 client/ 目录重跑并采信后者。
