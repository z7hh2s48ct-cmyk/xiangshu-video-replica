# MATERIAL-UX-14 证据记录（一键人物对象单份化）

> 素材库改进任务清单独立轨道（P1）：消除纯冗余——`simple_character.py` 中五视图的 `generated` 与 `approved` 各写一个 COS 对象，一套人物 12 个对象中 5 份为纯冗余。`approved` 行改为复用 `generated` 对象（同 URI 记录行），仅新生成生效，历史对象不追溯迁移。

## §14 证据记录

```text
任务/工作包：MATERIAL-UX-14-20260922（素材库改进任务清单独立轨道 P1；
  方案 §7.2 G3 一键人物对象单份化）
Owner / Reviewer：Qoder session (honor.pei) 代 honor.pei / 待 PR 评审分配
分支 / 基线 SHA：feat/material-ux-14-20260922 / origin/main@7834b045
上游规格段落：素材库改进工作任务清单-2026-09-22.md §MATERIAL-UX-14；
  素材库重分析与改进方案-2026-09-22.md §7.2 G3、§7.5-3
改动文件：server/app/simple_character.py（PreparedSimpleCharacterViewStorage
  收敛为 approved_asset_id: str 并删除未被读取的 content 字段；
  _generate_and_approve_views 每视图仅一次 put_object；_publish_views 的
  approved 行直接记录 generated 的 StoredObject；delete_simple_character_
  identity 清理计划按对象身份 (provider, bucket, key) 去重 + still_referenced
  True-wins 合并，审计 storage_cleanup_planned_count 同步唯一口径、
  新增 shared_storage_object_count）；
  server/tests/test_material_ux14_simple_character_objects.py（5 用例，525 行）；
  scripts/ci/test-shards/shard-{0..3}.txt（注册新测试文件，覆盖门禁
  build-test-shards.py --check-coverage 通过）
失败测试或回归锁定：反向验证（5 实例）：`git checkout origin/main -- server/app/simple_character.py` 回退实现后 `uv run python -m pytest tests/test_material_ux14_simple_character_objects.py -q` → 4 failed / 1 passed（失败：prepared 路径七对象预算与 approved 复用、非 prepared 路径 7 对象、重生成路径单份、删除清理按对象身份去重四条断言；通过的 1 条为发布失败清理路径——该行为两版一致，不锁定对象数）；`git checkout HEAD --` 恢复后 5 passed
实现结果：①对象预算 12 → 7：source 1 + contact sheet 1 + 五视图各 1
  （原五视图 generated + approved 各 1 = 10，现 5）；approved 资产行记录
  generated 的 storage_uri / sha256 / size_bytes，不再 put 第二个对象；
  ②PreparedSimpleCharacterViewStorage 收敛：approved_asset（PreparedSimple
  CharacterAsset，其 stored 与 generated 恒等）→ approved_asset_id: str，
  并删除全仓无读取点却把 5 张裁剪视图字节常驻发布对象的 content: bytes；
  ③删除清理按对象身份去重：12 资产行 → 7 唯一对象（approved/generated
  共享同一 key），still_referenced 一旦为真即保持（宁可保留共享字节），
  审计 deleted_asset_count=12 / storage_cleanup_planned_count=7 /
  shared_storage_object_count 口径一致；④人物库展示、授权、引用全链路
  行为不变：发布保持原子（approved 行写入 + character_assets.asset_id
  翻转 + is_published_selection=1 同一写事务），发布失败清理路径逐对象
  回滚全部已写对象；⑤评审修复（d4cf5701）：新测试文件注册进 CI 分片
  清单（BLOCKER——未注册会被 CI 覆盖门禁拦下且测试静默不跑）、清理去重
  （MINOR——原计划按资产行 12 计），死字段清理（MINOR）
验证命令与通过数：专项 `uv run python -m pytest tests/test_material_ux14_simple_character_objects.py -q` → 5 passed；`npm run check:static` 全绿（前端 vitest 全量 1866 passed / 113 test files、tauri cargo fmt/check、ruff 423 files、mypy 174 source files）；`bash scripts/ci/run-pytest-shards.sh` 四片全绿 733+748+847+626 = 2954 passed / 1 skipped（每片独立 PG 容器端口 5433+i；与 MATERIAL-UX-01 分片串行执行不叠加容器名）
证据层级：AUTOMATED_VERIFIED（本地自动化；真实客户环境验收归 staging 推进）
安全与可观测性：对象数减少不改授权模型；approved/generated 行共享对象
  URI，删除清理的 still_referenced 语义保证共享字节不被误删；审计新增
  shared_storage_object_count 便于观测
迁移与回滚：零迁移（无 schema 变更）；仅新生成生效，历史人物对象保持
  原样不追溯迁移；还原本 PR 文件即回滚，已生成人物数据不受影响
外部授权记录：无（未触碰生产 COS / 付费 Provider / 发码）
未测试项：真实 COS 环境下 approved 行与 generated 行共享对象 URI 的
  引用完整性（归 staging）；历史存量人物的对象数保持 12 不做迁移验证；
  Windows NSIS 门禁归 CI 三门禁
Lore 提交 SHA：以 PR 当前 head 为准
```

## 门禁执行记录

- 2026-09-22：专项 `uv run python -m pytest
  tests/test_material_ux14_simple_character_objects.py -q` → 5 passed。
- 2026-09-22：`npm run check:static` 全绿（前端 vitest 全量 1866 passed /
  113 test files；tauri cargo fmt/check；ruff 423 files already formatted；
  mypy 174 source files no issues）——含 CodeReview 修复后重跑。
- 2026-09-22：`bash scripts/ci/run-pytest-shards.sh` 四片全绿（733+748+847+626 =
  2954 passed / 1 skipped；每片独立 PG 容器，跑完自动 teardown）。
- 2026-09-22：反向验证（详见 §14「失败测试或回归锁定」）——回退 simple_character.py
  至基线 4 failed / 1 passed → 恢复后 5 passed。
