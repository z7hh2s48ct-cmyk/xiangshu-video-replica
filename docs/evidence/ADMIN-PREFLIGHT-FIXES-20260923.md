# ADMIN-PREFLIGHT-FIXES-20260923 · 上线前管理端缺陷修复批次

> 2026-09-23 上线前检查（两通道独立评审：缺陷/安全 + 架构/魔鬼代言人）发现的问题，
> 用户指令修复第 1-5 项并独立评审后合并。分支 `fix/admin-preflight-20260923`，
> 基线 `origin/main = aa1a6ab0`（#216），worktree `.worktrees/ADMIN-PREFLIGHT-20260923`。

## 修复清单（每项先红后绿）

### 1. CSV 公式注入残留（HIGH，安全）

- 现场：`server/app/export_controller.py` 的 `generate_csv_content` 把客户可控的
  `username`、`service_type` 直写 CSV，未走 `csv_export.spreadsheet_safe_cell`；
  #193 声称"所有 lane 统一防护"，实际漏掉本 lane，#208 又把该导出接进经营分析页。
- 修复：数据行所有单元格套 `spreadsheet_safe_cell`（与其它四条 lane 同一防护）。
- 测试：`server/tests/test_admin_export_csv.py`（新文件，假 conn 直测函数，8 用例：
  六前缀参数化 + service_type + 普通名不动 + None→N/A）。
- 结果：8 红修复后 20 passed（含既有 test_csv_export.py 回归）。

### 2. 微信凭据自检缺 CSRF（MEDIUM，生产必现功能失效）

- 现场：`client/src/api.admin.ts` `selfCheckWechatNative` 裸 `requestControl` POST
  不带 `X-Admin-CSRF`；客户生产模式写方法强制 CSRF → 403 `ADMIN_CSRF_REQUIRED`，
  自检按钮在生产不可用（#200 提交信息已自认未修）。
- 修复：改走 `adminWrite`（同文件 unit-price PUT 同款封装；服务端路由不读 body，
  confirm/reason 仅随契约封装携带，行为不变）。
- 测试：`client/src/api.admin.test.ts` 新增契约回归（照 #200 reconcile 用例模式），
  断言 POST 携带 X-Admin-CSRF 与 Idempotency-Key。先红（CSRF 头 null）后绿；
  `PaymentSettingsSection.test.tsx` 41 项全绿（调用方零改动）。

### 3. 生成记录页源画面筛选 PG 500（MEDIUM，生产必现功能失效）

- 现场：`server/app/control_routes.py` 列表 SQL 的 `semantic_requested_sql` 用
  SQLite 专有 `json_valid`/`json_extract`，PG 上 `UndefinedFunction` → 500；
  前端"素材处理 / AI 评分"筛选一选即触发。#193 自己新加的 summary 端点已规避，
  列表端点漏改。
- 修复：列表分支（total 计数 + 行查询共用同一拼接点）改用与 summary 相同的
  `audit_logs` EXISTS（跨方言）。行渲染侧原有 payload + 审计双重 Python 兜底
  不变；极少数"只有 payload、没有审计"的历史行在筛选里不计入 AI 评分类，
  与 summary 计数口径一致（该取舍注释已写入代码）。
- 测试：`server/tests/test_analysis_generation_records_pg.py` 新增真 PG 用例
  （seed 两条 source_frame_tasks + 一条语义质检审计），先红
  （`function json_valid(text) does not exist`）后绿；同文件 4 项 +
  `test_cw058_content_asset_pg_matrix.py` 88 项回归全绿。

### 4. #181 掉单兜底 systemd 单元重装要求无记载（部署缺口）

- 现场：#181 给 `video-replica-maintenance.service` 加两条 ExecStart，但 rollout
  不管理 systemd 单元、手册只在首次安装写 daemon-reload、docs 零提及——按
  runbook 升级则兜底静默不生效。
- 修复：`docs/客户版部署与灰度手册.md` §7 新增步骤 3（含 install + daemon-reload
  命令与 #181 实例）；回补证据 `docs/evidence/WECHAT-NATIVE-FALLBACK-DEPLOY-20260923.md`
  （变更内容 / 部署要求 / 验证方法）。

### 5. 迁移 DDL 无锁等待上限（部署风险缓解）

- 现场：CHECK 重建等 DDL 拿 `ACCESS EXCLUSIVE` 锁无 timeout，高峰排队会拖住
  全部钱包请求；rollout 在旧实例仍服务时跑迁移，放大该窗口。
- 修复：`deploy/postgres/migrate.sh` 与 `customer-git-rollout.sh` 迁移步默认带
  `lock_timeout=5s`（`VIDEO_REPLICA_MIGRATION_LOCK_TIMEOUT` 可调，0 关闭）；
  拿不到锁快速失败，低峰重跑。手册 §7 步骤 2 补低峰要求。两脚本 `bash -n` 通过；
  `test_customer_git_rollout.py` 契约断言不受影响。

## 附带

- `scripts/ci/test-shards/` 由生成器重新生成（151 文件全覆盖，新文件入 shard-2）；
  大部分 diff 为 LPT 确定性重排的文件在片间移动（与 #213 等历史惯例一致）。

## 验证

- 专项：见各项"结果"；门禁（静态门 + 四分片全量 pytest）结果见认领登记行。
- 无迁移、无新依赖、无真实付费调用。
