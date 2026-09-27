"""CW-056 — 受支持发布 head → 最终 head 的 PostgreSQL 升级矩阵（PG-04）。

规格来源：``outputs/customer-cloud-convergence-analysis-2026-09-08/v3/客户版收敛剩余任务清单与验收完工标准-V3.md``
§CW-056。权威上游依据（均为 owner 已签认的冻结证据，本文件不自行推导版本清单）：

- ``docs/evidence/CW003-COMPATIBILITY-WINDOW.md`` §1/§6 —— 受支持发布版本清单与发布提交。
  0.1.14 在 CHANGELOG 有条目但**无发布提交**，§1 明记「跳过，无发布记录」，故不入矩阵。
- ``docs/evidence/CW053-DB-SEMANTIC-INVENTORY.md`` §3 E1 —— 已发布迁移「revision 哈希冻结，
  **字节不改**」（PG-09 永久例外）。本文件的内容级哈希守卫即该条目的机器化。

范围红线（三重依据，见 CW-053 §3 E2/E4 与 §2 line 42）：

- ``server/app/db.py`` 的 SQLite 兼容层归 CW-042，退役条件是「CW-043 全业务 PG 覆盖核销后」，
  尚未到达；它被 10 个 app 模块与 30+ 测试消费，且 ``initialize_database`` 经
  ``alembic_config`` 显式把 ``sqlalchemy.url`` 设为 SQLite。因此 **env.py 不得全局拒绝
  SQLite**，门禁只在 ``VIDEO_REPLICA_CUSTOMER_PRODUCTION`` 为真时生效。
- ``sqlite_to_postgres.py`` / ``reconcile_customer_billing.py`` 归 CW-060 独立制品（E4），
  且 CW-056 规格 line 594 明确剔除导入工具，本任务不改。

测试分三组：

- A 组（静态守卫，**不挂 PG 门**）：矩阵完整性与漏项=0、单 head 线性、已发布链字节冻结、
  offline ``--sql`` 拒绝充当交付升级脚本、客户生产拒绝 SQLite 迁移目标、migrate.sh 升级后校验 head。
- B 组（真实 PG，挂 CW-007 硬门）：空库与每个受支持起点各跑独立数据副本，核对代表数据事实、
  schema 目录跨起点逐项相等、JSON/时间语义、identity/serial/ledger 下一写、审计降级拒绝。
- C 组（真实 PG，挂硬门）：失败态可判定——失败后 alembic_version 与结构状态必须可判定，不留半结构。

标注 ``# RED`` 的用例在本任务实现前失败（真实实现缺口）；其余为回归锁，实现前即通过，
其价值在于防止未来漂移，不以「先红后绿」冒充。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic.script import ScriptDirectory
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    resolve_test_dsn,
)

SERVER_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = SERVER_DIR / "migrations"
REPO_ROOT = SERVER_DIR.parent

# 当前链尾。与 test_postgres_migrations.HEAD_REVISION 同源。
# 20260921T0000_merge_wallet_actor_and_billing_metadata 把 CUSTOMER-CENTER-V2-20260919
# （sub_accounts → wallet_actor）与 main（oral_soft_delete → merge_parallel_heads →
# add_api_metadata_to_billing_ops）两条并行链线性化为单头；
# 20260922T1200_recharge_packages 在合并链尾之上追加 recharge_packages（管理端配置档位）
# + recharge_orders.package_id/package_snapshot_json
# + customer_discounts.source_recharge_order_id，并重写 credit_calculation 三支与
# amount_price/amount_step 的套餐豁免支；
# 20260921T1200_sub_account_quotas（Phase 3a 子账号月度额度表）之后，
# 20260922T1800_sub_account_permissions（Phase 3b 功能权限矩阵表）。
# BILLING-OBS-20260922 三个迁移重挂到 sub_account_permissions 之上（PR#192 合入后
# 主链尾前移，未合并分支重挂 down_revision 是手册 §3 允许的动作）：
# 20260922T1200_analysis_task_failure_diagnostic（P0-2）追加
# analysis_tasks.upstream_diagnostic_json（jsonb）——columns +1、jsonb_columns +1；
# 20260922T1600_analysis_task_request_id（P1-4）再追加 analysis_tasks.request_id（text）
# ——columns +1；
# 20260922T2000_analysis_task_attempts（P1-6）新建失败历史表 analysis_task_attempts
# ——tables/primary_keys +1、columns +12、foreign_keys +1、unique_constraints +1
# （task_id+attempt）、check_constraints +3（status/attempt/retryable）、jsonb_columns +1
# （upstream_diagnostic_json）。
# 20260923T0000_open_h3_extended_modes 移除 T2V/R2V/L2V 门禁并 DROP 掉
# runtime_settings.h3_extended_modes_enabled——columns -1；其余计数不变。
# 本分支的 20260922T1500_viral_search_discoveries（爆款视频搜索发现记录表）
# 按手册 §3 重挂于链尾：tables/primary_keys +1、columns +9、unique_constraints +1
# （五列身份约束）、两个非 partial 查询索引；全 Text 列，无 FK/CHECK/jsonb/timestamptz 增量。
# 合并 main 后：本分支的 20260923T1800_re_add_h3_extended_modes_rollout_compat 追加于
# main 链尾（20260923T1200_admin_refund_adjustment）之上；上线评审 P1-1 的
# 20260924T0000_wechat_transaction_unique、P1-2 的 20260924T0100_oral_task_submitted_at
# 与 20260924T0200_customer_oral_task_visibility 再依次叠加其上；
# 20260926T0000_viral_homepage_rank（爆款首页策展排行）与
# 20260925T1400_api_metadata_pending_write（api_metadata 允许在 PENDING 期写入的
# 追加修复）再依次叠加于链尾；
# 20260927T1200_admin_offline_payment_source（管理员代客开通套餐的「线下收款」来源单
# 类型）再追加其上，故链尾为该值。
HEAD_REVISION = "20260927T1200_admin_offline_payment_source"

# 最后一个已发布（受支持）起点。其后的 056…090 与本迁移尚未随任何受支持版本发布，
# 故冻结范围止于此——把未发布 revision 也纳入哈希会让每次新增迁移都必须改常量，
# 那不是「冻结已发布历史」而是「冻结开发中」。
PUBLISHED_HEAD_REVISION = "055_customer_batch_visibility"
PUBLISHED_CHAIN_LENGTH = 54

# CW-053 §3 E1「字节不改」的机器化：把 revision id、父子关系与**迁移文件字节**
# 一起纳入 sha256。仅冻结关系（不含文件字节）抓不到「有人编辑了已发布迁移的
# upgrade()/downgrade() 函数体」，而那正是「只追加修复迁移」要禁止的。
# .gitattributes 的 `* text=auto eol=lf` 保证 .py 在工作区跨平台同为 LF，
# 故直接 read_bytes() 无需行尾规范化（该文件记载过 CRLF-only 重写 bd73072 的教训）。
PUBLISHED_CHAIN_CONTENT_SHA256 = "1154d46f2b136845004b16de388eb1bc10c25d9dce996ebea8367366b720587e"
# 关系级哈希，与内容级并列断言：内容哈希失配时它能区分「改了文件体」与「改了链拓扑」。
PUBLISHED_CHAIN_RELATION_SHA256 = "4f27304401324fe1f453bb19e96fe178b4bb9e850501ff1cdefe45105e1f5f1a"

# CW-003 §1 表（git 证据可追溯部分）：版本 → (发布提交, 当时的 head revision)。
# head 由 git grep 各发布 SHA 的 versions/ 目录重算得出，不按文件名数字排序——
# 历史上有过 032 重新指向 029 的分支改写（M5 review P1-3），文件名序会骗人。
SUPPORTED_RELEASE_HEADS: dict[str, tuple[str, str]] = {
    "0.1.12": ("d9a5576", "053_activation_code_archive"),
    "0.1.13": ("bc0248e", "054_admin_free_grant_adjustments"),
    "0.1.15": ("80a8e40", "054_admin_free_grant_adjustments"),
    "0.1.16": ("59e10ed", "055_customer_batch_visibility"),
}

# 矩阵起点：空库 + 受支持版本的 distinct head。"" 表示空库（直接 upgrade head）。
# 漏项=0 由 test_supported_release_head_matrix_is_frozen_and_complete 结构性保证：
# 它断言本元组恰好等于 SUPPORTED_RELEASE_HEADS 的 distinct 值集 ∪ {""}。
MATRIX_STARTING_HEADS: tuple[str, ...] = (
    "",
    "053_activation_code_archive",
    "054_admin_free_grant_adjustments",
    "055_customer_batch_visibility",
)

MATRIX_DATABASE = "cw056_head_matrix_test"
FAILSTATE_DATABASE = "cw056_failstate_test"

# 空库 → head 的 schema 面冻结计数（回归锁）。每项都绑定精确查询口径：
# 例如 triggers 用 information_schema.triggers 的**行数**（BEFORE UPDATE 与
# BEFORE DELETE 各算一行），故 18 行对应 10 个 distinct trigger，不是 10 行。
# B1（20260923T1200_admin_refund_adjustment）增量：只改 CHECK 约束、不加表也不加列。
# `ck_wallet_transactions_type` / `ck_wallet_transactions_shape` 是 drop+recreate
# （净 0），另新增一条 `ck_admin_adjustments_order_required`（反向调账以外必须有充值单）
# ——故 check_constraints +1，其余计数与表名全集不变。注意 `HEAD_SCHEMA_DIGEST`
# 仍然会变：它把 `pg_get_constraintdef` 的文本一起哈希，约束体一改就换值。
HEAD_SCHEMA_COUNTS = {
    # 两条线上各加一项，且互不相干，故两边都要取：
    #   check_constraints 322 → 323：本分支新增 ck_admin_adjustments_order_required
    #   columns           1219 → 1220：main 的 material_preferences.tags_json
    # 20260923T1800 重加 runtime_settings.h3_extended_modes_enabled 部署兼容垫片
    # （旧镜像在 MIGRATE→ROLL 混合窗口与镜像回滚时仍 SELECT 该列）：
    #   columns           1220 → 1221；digest 重算（见下）。
    # 20260924T0000 给 recharge_orders.transaction_id（wechat_native 交易号）补
    # 部分唯一索引兜底：partial_indexes 37 → 38；digest 重算（见下）。
    # 20260924T0100 给 oral_tasks 增加 submitted_at（口播轮询看播计时锚点，
    # 可空 Text 无默认）：columns 1221 → 1222；digest 重算（见下）。
    # 20260924T0200 新增 customer_oral_task_visibility（账号级隐藏口播任务，
    # 两列复合主键 + 两条 CASCADE 外键，hidden_at 走 Text）：tables 103 → 104、
    # columns 1222 → 1225、primary_keys 103 → 104、foreign_keys 193 → 195；
    # digest 重算（见下）。
    # 20260925T1400 只 CREATE OR REPLACE 触发器函数 billing_refuse_fact_rewrite()
    # （把 api_metadata 加进 billing_operations 的 PENDING 期可写列白名单）：
    # 不加表 / 列 / 索引 / 触发器，故本字典与表名全集不变；digest 进
    # pg_get_functiondef 文本，必然重算（见下）。
    # 20260927T1200 只 drop+recreate ck_admin_adjustments_source_type（追加
    # OFFLINE_PAYMENT）：计数净 0；约束文本变了，digest 重算（见下）。
    "check_constraints": 323,
    "columns": 1226,
    "foreign_keys": 195,
    "identity_columns": 0,
    "jsonb_columns": 6,
    "partial_indexes": 38,
    "primary_keys": 104,
    "sequences": 4,
    "tables": 104,
    "timestamptz_columns": 56,
    "triggers": 27,
    "unique_constraints": 39,
}

# head 的表名全集。counts 只能证明「数量没漂」，证明不了「同一批表」：
# 掉一张旧表再建一张新表，tables 计数仍不变。表名集合与下面的完整目录
# 摘要一起构成结构等价的两级断言，失配时的报错可直接指出 missing/unexpected。
# 082 的增量：tables/primary_keys +1（publish_accounts）、columns +18、
# check_constraints +3（platform/status/verify_flag 三条 CHECK）、foreign_keys +1
# （user_id → users.id ON DELETE CASCADE）。两个新索引都不是 partial，故
# partial_indexes 不变；时间戳走 sa.Text()，timestamptz_columns 不变。
# CW-075 090 的增量：本表名集追加 customer_discounts（main 链尾迁移 090 新建的表，
# 已 re-linearize 到 main 现头 089 之上：089→090）。HEAD_SCHEMA_COUNTS 与
# T4 置顶序追加 viral_videos.homepage_rank：columns +1，digest 经 --print-schema 重算。
# HEAD_SCHEMA_DIGEST 已随之重算为 090 的真实值——它们是全局 post-linearization 不变量：
# CW-078 折叠 089 时在本地 PG 探针重算过一次（digest 8fe43e16），CW-075 折叠 090 后沿
# 同一先例（postgres:16-alpine，与 CI pg-fixture 同主版本）再重算一次。故 B 组真实 PG
# 矩阵断言全绿：表名集（inventory 的 tables 与 HEAD_TABLE_NAMES 排序相等，含
# customer_api_keys 与 customer_discounts 两表）、counts、digest 三项均对齐 090 重算值。
# CW-076 的增量：users.password_hash / users.registration_source 两列 + 三条 CHECK
# （blank/known/self_register-has-password），090 之上重挂 20260912T1400 后探针再重算
# （columns +2=938、check_constraints +3=236，digest b678939f…，表名集不变）。
HEAD_TABLE_NAMES = (
    "activation_code_activations",
    "activation_code_batches",
    "activation_code_deliveries",
    "activation_code_events",
    "activation_code_exports",
    "activation_codes",
    "admin_adjustments",
    "admin_device_events",
    "admin_password_credentials",
    "admin_sessions",
    "admin_write_idempotency",
    "alembic_version",
    "analysis_task_attempts",
    "analysis_tasks",
    "assets",
    "audit_logs",
    "billing_attempts",
    "billing_credit_lots",
    "billing_evidence",
    "billing_operations",
    "billing_tariffs",
    "character_asset_reviews",
    "character_assets",
    "character_generation_tasks",
    "character_personas",
    "character_reference_selections",
    "character_sheet_tasks",
    "character_versions",
    "characters",
    "content_objects",
    "customer_api_keys",
    "customer_authorization_evidence",
    "customer_batch_visibility",
    "customer_credit_pricing",
    "customer_devices",
    "customer_discounts",
    "customer_fencing_write_evidence",
    "customer_idempotency_envelopes",
    "customer_oral_task_visibility",
    "customer_session_events",
    "customer_session_state",
    "customer_unit_prices",
    "daily_external_prices",
    "device_pairing_requests",
    "external_call_logs",
    "first_frame_tasks",
    "generation_batches",
    "generation_task_operations",
    "generation_tasks",
    "h3_provider_accounts",
    "h3_provider_task_accounts",
    "internal_access_tokens",
    "legacy_credit_policy",
    "operation_cost_rates",
    "operation_cost_records",
    "ops_alert_state",
    "oral_avatars",
    "oral_billing_reconciliation_operations",
    "oral_consents",
    "oral_tasks",
    "oral_voices",
    "person_identities",
    "project_main_characters",
    "projects",
    "prompt_optimization_receipts",
    "provider_settings",
    "publish_accounts",
    "publish_browser_accounts",
    "publish_browser_logins",
    "publish_records",
    "recharge_orders",
    "recharge_packages",
    "runtime_settings",
    "script_from_audio_tasks",
    "script_rewrite_tasks",
    "security_auth_failures",
    "security_rate_limit_counters",
    "source_frame_tasks",
    "studio_drafts",
    "studio_material_preferences",
    "studio_notification_preferences",
    "studio_saved_scripts",
    "sub_account_permissions",
    "sub_account_quotas",
    "user_queue_cursors",
    "users",
    "versions",
    "viral_collection_batches",
    "viral_collection_charges",
    "viral_collection_members",
    "viral_fetch_state",
    "viral_import_tasks",
    "viral_link_resolution_receipts",
    "viral_media_preparations",
    "viral_refresh_tasks",
    "viral_runtime_controls",
    "viral_script_cache",
    "viral_search_discoveries",
    "viral_video_favorites",
    "viral_video_visibility",
    "viral_videos",
    "wallet_credit_conversions",
    "wallet_transactions",
    "wallets",
)

# _schema_inventory 规范化 JSON 的 sha256：列（名/类型/可空/默认值）、约束定义、
# 索引定义、触发器、函数体（pg_get_functiondef）、序列结构（不含 last_value）。
# 这是「空库→head」与「旧起点→head」必须**收敛到同一 schema** 的机器化断言 ——
# 计数与表名都可能相同而列级细节不同，只有完整目录能兜住。
# 由离线 freeze probe 从本模块的同一对 helper 算出（避免 probe 与测试漂移）。
# CW-076 重挂后经 scripts/ci/migration_manifest.py --print-schema 重算（088→20260912T1400）。
# 链尾已改为 20260916T1400_content_objects，且父级从 20260914T0000_local_joint_merge
# 重挂到 main 已合入的 20260915T1600_viral_copy_cache（PR #120）——两分支曾同父，
# 不重挂会让 alembic 看到两个 head。故下列字面量是**两段迁移叠加后**的实测值
# （空库→新 head，postgres:16，见 scripts/ci/migration_manifest.py --print-schema）：
# 相对 20260914T0000 的合计增量：tables/primary_keys +2（viral_script_cache、
# content_objects）、foreign_keys +2、columns +21、timestamptz_columns +4。
# 其中 content_objects 一段：check_constraints +4（size_bytes>0 / scope /
# ref_count>=0 / scope_owner）、partial_indexes +3（user 与 global 两条唯一部分索引
# + 回收索引）；unique_constraints 不变，因为两条唯一性都用部分索引表达而非 UNIQUE。
# PROMPT-OPTIMIZE-20260916 追加 prompt_optimization_receipts及 analysis_tasks 上下文列：
# tables/primary_keys +1、columns +18、foreign_keys +1（owner_user_id→users）、
# unique_constraints +1（owner+idempotency）、check_constraints +2（mode / status）；
# 空库→新 head 于本地 postgres:16 fixture 用 migration_manifest.py --print-schema 重算。
# PUBLISH-DELIVERY-20260917 追加 publish_records 及 publish_browser_accounts 三列：
# tables/primary_keys +1、columns +29（26 + 3）、foreign_keys +4（user/account/
# video_asset/cover_asset）、check_constraints +7（records 5 条 + accounts status/source）、
# jsonb_columns +3（tags/options/stats）、partial_indexes +2（account_active/sync）、
# timestamptz_columns +6；unique_constraints 不变。同样以 --print-schema 重算。
# 合并链尾 20260921T0000_merge_wallet_actor_and_billing_metadata 同时包含：
# - SUB-ACCOUNTS-20260919（sub_accounts + wallet_actor）：users.parent_user_id/account_type、
#   wallet_transactions.actor_user_id——columns +3、check_constraints +3、foreign_keys +2、
#   partial_indexes +2，无新表
# - main 侧 browser_account_probe + oral_soft_delete + merge_parallel_heads +
#   add_api_metadata_to_billing_ops（billing_operations.viral_data 补 api_metadata 列）
# 合并后链尾由多段增量组成：
# - RECHARGE-PACKAGES-20260922（20260922T1200）追加 recharge_packages 及订单两列/权益来源列；
# - Phase 3a 20260921T1200_sub_account_quotas 追加 sub_account_quotas；
# - Phase 3b 20260922T1800_sub_account_permissions 追加 sub_account_permissions
#   （子账号功能权限矩阵，无行=全允许）：tables/primary_keys +1、columns +6、
#   foreign_keys +1（user_id→users CASCADE）；jsonb_columns 不变（权限集合/开关走 TEXT-JSON）。
# - BILLING-OBS-20260922 三个迁移重挂到 sub_account_permissions 之上：
#   P0-2 analysis_task_failure_diagnostic（analysis_tasks.upstream_diagnostic_json jsonb）；
#   P1-4 analysis_task_request_id（analysis_tasks.request_id text）；
#   P1-6 analysis_task_attempts（新建表 analysis_task_attempts：tables/primary_keys +1、
#   columns +12、foreign_keys +1、check_constraints +3、jsonb_columns +1；
#   task_id+attempt 唯一性用 unique index 表达，unique_constraints 不变）。
# - 20260923T0000_open_h3_extended_modes：移除 T2V/R2V/L2V 门禁并 DROP 掉
#   runtime_settings.h3_extended_modes_enabled（columns -1=1210；其余计数不变）。
# - 本分支 20260922T1500_viral_search_discoveries（爆款视频搜索发现记录表）重挂于链尾：
#   tables/primary_keys +1=103、columns +9=1219、unique_constraints +1=39（五列身份约束）；
#   两个查询索引均非 partial，jsonb / CHECK / FK / timestamptz 无增量；
#   表名集追加 viral_search_discoveries。
# - 20260923T1200_admin_refund_adjustment（B1 反向调账）：只动 CHECK，不加表不加列。
#   ck_wallet_transactions_type / ck_wallet_transactions_shape 为 drop+recreate（净 0），
#   新增 ck_admin_adjustments_order_required（check_constraints +1=323）；
#   admin_adjustments.recharge_order_id 放开 NOT NULL 不属于本矩阵口径的计数项
#   （只影响 is_nullable，不进 inventory digest），但它的 CHECK 伙伴会进。
#   digest 随之重算——约束文本变了，digest 必然变，这不是漂移。
# - 20260923T1800_re_add_h3_extended_modes_rollout_compat（上线评审 Fix5）：把 0000
#   DROP 掉的 runtime_settings.h3_extended_modes_enabled 以部署兼容垫片形式重加
#   （旧镜像在 MIGRATE→ROLL 混合窗口与镜像回滚时仍 SELECT 该列；新代码零读者，
#   见 test_independent_creation 的源码级回归锁）——columns +1=1221；digest 重算。
# - 20260924T0000_wechat_transaction_unique（上线评审 P1-1）：给 recharge_orders.
#   transaction_id（wechat_native 交易号，083 引入）补部分唯一索引兜底——
#   partial_indexes +1=38；不加表不加列不加约束，digest 随索引重算。
# - 20260924T0100_oral_task_submitted_at（上线评审 P1-2）：oral_tasks 增加
#   submitted_at（可空 Text，无默认）作轮询看播计时锚点——columns +1=1222；
#   不加表不加索引，digest 随之重算。
# - 20260925T1400_api_metadata_pending_write（计费触发器与 api_metadata 相撞的追加修复）：
#   仅替换 billing_refuse_fact_rewrite() 函数体，结构与计数全不变，digest 随函数文本重算。
# - 20260926T0000_viral_homepage_rank（爆款首页策展排序，来自 main）：viral_videos 增加
#   homepage_rank（可空 Integer，无回填，downgrade 直接删列）——columns +1；不加表、
#   不加索引、不加约束，digest 随之重算。本分支的 20260925T1400 重挂于其后（见该迁移
#   docstring），链尾仍是 20260925T1400，但链上新增了这一层，故下面三个字面量按
#   「合并后新链」重算。
# - 20260927T1200_admin_offline_payment_source（管理员代客开通套餐）：来源单枚举
#   CHECK 追加 OFFLINE_PAYMENT（drop+recreate，计数净 0），digest 随约束文本重算。
# digest/counts 以 scripts/ci/migration_manifest.py --print-schema 于 postgres:16 重算
# （合并后的新 head：sub_account_permissions + 三个 analysis 迁移 + viral 搜索发现表
#  + main 的 MATERIAL-UX tags_json 列 + REFUND 调账迁移 + 1800 垫片 + 交易号唯一
#  索引 + 口播提交时刻列，串成一条单 head 链）。
#  两侧原来的 digest 都不能用——本分支那条是接在 viral 之后的旧链、main 那条只到
#  MATERIAL-UX，合并后 head 变成接在 MATERIAL-UX 之后的本分支迁移，约束文本随之变化，
#  digest 必然要重算。由 scripts/ci/migration_manifest.py --print-schema 在 PG 上重算后粘贴。
HEAD_SCHEMA_DIGEST = "54b2a7e56a4534d51522b94450641fcbbb9198dbb791e5cf5d00c8f47b914651"

_SCHEMA_COUNT_QUERIES: dict[str, str] = {
    "tables": (
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    ),
    "columns": ("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public'"),
    "identity_columns": (
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'public' AND is_identity = 'YES'"
    ),
    "sequences": "SELECT count(*) FROM pg_sequences WHERE schemaname = 'public'",
    "jsonb_columns": (
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = 'jsonb'"
    ),
    "timestamptz_columns": (
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = 'timestamp with time zone'"
    ),
    "triggers": (
        "SELECT count(*) FROM information_schema.triggers WHERE trigger_schema = 'public'"
    ),
    "partial_indexes": (
        "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND indexdef LIKE '%%WHERE%%'"
    ),
    "unique_constraints": (
        "SELECT count(*) FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = 'public' AND c.contype = 'u'"
    ),
    "check_constraints": (
        "SELECT count(*) FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = 'public' AND c.contype = 'c'"
    ),
    "foreign_keys": (
        "SELECT count(*) FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = 'public' AND c.contype = 'f'"
    ),
    "primary_keys": (
        "SELECT count(*) FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = 'public' AND c.contype = 'p'"
    ),
}


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory(str(MIGRATIONS_DIR))


def _published_chain() -> list[tuple[str, str, str | None]]:
    """base → PUBLISHED_HEAD_REVISION 的 (revision, 文件字节 sha256, down_revision)。"""
    script = _script_directory()
    chain: list[tuple[str, str, str | None]] = []
    current: str | None = PUBLISHED_HEAD_REVISION
    while current is not None:
        revision = script.get_revision(current)
        digest = hashlib.sha256(Path(revision.path).read_bytes()).hexdigest()
        down = revision.down_revision
        chain.append((revision.revision, digest, down))
        # 线性链已由 test_migration_chain_has_single_linear_head 守卫；此处仍显式
        # 拒绝分支，否则 tuple 型 down_revision 会让 while 静默走丢一条支链。
        if isinstance(down, tuple):
            raise AssertionError(f"unexpected branch point at {revision.revision}")
        current = down
    chain.reverse()
    return chain


def _alembic_config(dsn: str) -> Any:
    """In-process Alembic config（沿用 test_postgres_migrations 的写法）。"""
    from alembic.config import Config

    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    return config


def _upgrade(dsn: str, revision: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(dsn), revision)


def _downgrade(dsn: str, revision: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(dsn), revision)


def _run_alembic_cli(
    *,
    args: Sequence[str],
    database_url: str | None,
    customer_production: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """子进程跑 alembic CLI。

    env.py 是脚本而非可导入模块（模块底部直接执行 run_migrations_*），所以门禁类
    断言只能经 CLI 观察 rc 与 stderr。环境变量按参数**显式构造**而不是继承，
    避免开发机 shell 里残留的 VIDEO_REPLICA_* 污染判定。
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("VIDEO_REPLICA_")}
    env["PYTHONPATH"] = str(SERVER_DIR)
    if database_url is not None:
        env["VIDEO_REPLICA_DATABASE_URL"] = database_url
    if customer_production is not None:
        env["VIDEO_REPLICA_CUSTOMER_PRODUCTION"] = customer_production
    return subprocess.run(  # noqa: S603 - fixed interpreter/args, no shell
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVER_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def _pg_gate() -> str:
    """CW-007 硬门：缺 PG 时 fail（非 skip），仅显式 opt-in 可跳过且不计证据。"""
    return require_pg_or_explicit_skip(resolve_test_dsn())


def _matrix_dsn(base_dsn: str, database: str) -> str:
    return base_dsn.rsplit("/", 1)[0] + f"/{database}"


@pytest.fixture
def matrix_database() -> Iterator[str]:
    """每个用例独占的矩阵库：进出各清理一次，绝不跨用例复用。"""
    base = _pg_gate()
    dsn = _matrix_dsn(base, MATRIX_DATABASE)
    drop_test_database(MATRIX_DATABASE)
    create_test_database(MATRIX_DATABASE)
    try:
        yield dsn
    finally:
        drop_test_database(MATRIX_DATABASE)


@pytest.fixture
def failstate_database() -> Iterator[str]:
    """失败态用例专属库，与矩阵库**不同名**。

    C 组用例故意把一个库留在 upgrade 失败后的状态。若与 B 组共用库名，
    一旦 finally 的 drop 没跑到，下一轮跑矩阵时就会拿到一个被污染的库，
    而错误现象会指向无辜的起点。独立库名把爆炸半径钉在失败态本身。
    """
    base = _pg_gate()
    dsn = _matrix_dsn(base, FAILSTATE_DATABASE)
    drop_test_database(FAILSTATE_DATABASE)
    create_test_database(FAILSTATE_DATABASE)
    try:
        yield dsn
    finally:
        drop_test_database(FAILSTATE_DATABASE)


# ---------------------------------------------------------------------------
# A 组 — 静态守卫（不挂 PG 门：这些是任何环境都必须成立的结构性不变量）
# ---------------------------------------------------------------------------


def test_supported_release_head_matrix_is_frozen_and_complete() -> None:
    """矩阵冻结完整性：漏项=0，且每个登记起点都是 head 的真祖先。

    「漏项=0」不靠人工核对，而是结构性保证：断言 B 组参数化的起点集合**恰好等于**
    CW-003 受支持版本的 distinct head 集合 ∪ {空库}。将来登记 0.1.17 却忘了加矩阵
    参数时，本用例失败。
    """
    script = _script_directory()

    assert SUPPORTED_RELEASE_HEADS, "supported release registry must not be empty"
    # CW-003 §1：0.1.14 有 CHANGELOG 条目但无发布提交，明确「跳过，无发布记录」。
    assert "0.1.14" not in SUPPORTED_RELEASE_HEADS, (
        "0.1.14 has no release commit per CW-003 §1/§6 and must not enter the matrix"
    )

    registered: set[str] = set()
    for release, (commit, head) in SUPPORTED_RELEASE_HEADS.items():
        assert commit, f"{release} must record its release commit"
        revision = script.get_revision(head)
        assert revision is not None, f"{release}: head {head} is not a known revision"
        registered.add(head)

    # 每个起点必须能升到 HEAD_REVISION，否则「起点→head」这个矩阵项无意义。
    for release, (_, head) in SUPPORTED_RELEASE_HEADS.items():
        assert _is_ancestor(script, head, HEAD_REVISION), (
            f"{release}: starting head {head} is not an ancestor of {HEAD_REVISION}"
        )

    expected = registered | {""}
    assert set(MATRIX_STARTING_HEADS) == expected, (
        f"matrix coverage drift: parametrized {sorted(MATRIX_STARTING_HEADS)} "
        f"vs registered {sorted(expected)}"
    )
    assert len(MATRIX_STARTING_HEADS) == len(set(MATRIX_STARTING_HEADS)), (
        "duplicate starting heads would silently double-count the matrix"
    )


def _is_ancestor(script: ScriptDirectory, candidate: str, target: str) -> bool:
    current: str | None = target
    while current is not None:
        if current == candidate:
            return True
        revision = script.get_revision(current)
        down = revision.down_revision
        if isinstance(down, tuple):
            return any(_is_ancestor(script, candidate, parent) for parent in down)
        current = down
    return False


def test_migration_chain_has_single_head_with_registered_merge() -> None:
    """Only the registered post-release merge may join the two installed branches.

    The published base..055 chain remains strictly linear and byte-frozen in the
    following test. Additional or changed merge parents must still fail closed.
    """
    script = _script_directory()
    heads = script.get_heads()
    assert heads == [HEAD_REVISION], f"expected exactly one head {HEAD_REVISION}, got {heads}"

    revisions = list(script.walk_revisions())
    branch_points = [
        (rev.revision, rev.down_revision)
        for rev in revisions
        if isinstance(rev.down_revision, tuple)
    ]
    assert branch_points == [
        (
            "20260921T0000_merge_wallet_actor_and_billing_metadata",
            (
                "20260919T1500_device_parent_cascade",
                "20260920T0100_add_api_metadata_to_billing_ops",
            ),
        ),
        (
            "20260920T0000_merge_parallel_heads",
            ("20260919T1000_browser_account_probe", "20260919T1000_oral_soft_delete"),
        ),
        (
            "20260914T0000_local_joint_merge",
            ("20260913T1600_shared_viral_media", "20260913T1825_h3_account_pool"),
        ),
    ], f"unregistered migration merge: {branch_points}"

    roots = [rev.revision for rev in revisions if rev.down_revision is None]
    assert len(roots) == 1, f"expected exactly one base revision, got {roots}"


def test_published_migration_chain_bytes_are_frozen() -> None:
    """已发布链（base..055）的 revision 拓扑与**文件字节**冻结 —— CW-053 §3 E1。

    修复只能通过**追加**新迁移（规格 line 599「只追加修复迁移」）。任何对已发布
    迁移的编辑都会改变内容哈希；任何拓扑改写都会改变关系哈希。
    """
    chain = _published_chain()
    assert len(chain) == PUBLISHED_CHAIN_LENGTH, (
        f"published chain length changed: {len(chain)} != {PUBLISHED_CHAIN_LENGTH}"
    )
    assert chain[0][2] is None, "the published chain must start at the single base revision"
    assert chain[-1][0] == PUBLISHED_HEAD_REVISION

    content = "\n".join(f"{rev}<-{down or ''}:{digest}" for rev, digest, down in chain)
    assert hashlib.sha256(content.encode("utf-8")).hexdigest() == (
        PUBLISHED_CHAIN_CONTENT_SHA256
    ), (
        "published migration bytes or topology changed; PG-09/CW-053 E1 requires "
        "already-released revisions to stay byte-frozen — append a new revision instead"
    )

    relation = "\n".join(f"{rev}<-{down or ''}" for rev, _, down in chain)
    assert (
        hashlib.sha256(relation.encode("utf-8")).hexdigest() == PUBLISHED_CHAIN_RELATION_SHA256
    ), "published revision topology changed (independent of file bytes)"


def test_offline_sql_is_refused_as_deliverable_upgrade_script() -> None:
    """offline ``--sql`` 必须**主动**拒绝充当交付升级脚本。 # RED

    现状只在 env.py docstring 里声明限制，运行时并不可靠：``alembic upgrade head --sql``
    实测 rc=1，但那是**偶然**崩在 009 的 ``MockConnection.exec_driver_sql`` 缺失，
    且崩溃前已吐出 9923 字符**部分** SQL（其中 ``alembic_version.version_num`` 是
    Alembic 硬编码的 VARCHAR(32)，容不下本项目 33+ 字符的 revision id）。

    偶然崩溃不是守卫：一旦 009 被修好，offline 就会静默产出一份不可执行的脚本，
    而 DBA 可能拿它当升级证据跑。故必须 fail-closed 并给出明确原因——断言落在
    **拒绝消息**上而不是 rc 上，否则本用例会因既有的偶然崩溃而假绿。
    """
    result = _run_alembic_cli(
        args=["upgrade", "head", "--sql"],
        database_url="postgresql://offline:offline@localhost:5432/offline_probe_test",
    )
    assert result.returncode != 0, "offline --sql must not succeed for the PostgreSQL chain"
    stderr = result.stderr
    assert "offline" in stderr and "not a deliverable upgrade script" in stderr, (
        "offline --sql must fail closed with an explicit refusal naming the reason, "
        f"got stderr tail: {stderr.strip()[-400:]!r}"
    )
    # 拒绝必须发生在产出任何 SQL 之前，否则半份脚本仍可能被人捡走。
    assert "CREATE TABLE" not in result.stdout, (
        "refusal must precede any SQL emission; partial scripts are not evidence"
    )


def test_customer_production_rejects_sqlite_migration_target() -> None:
    """客户生产模式下，SQLite 迁移目标必须硬失败（静默降级守卫）。 # RED

    真实事故面：运维跑 ``alembic upgrade head`` 却忘了设 ``VIDEO_REPLICA_DATABASE_URL``，
    env.py 回退到 alembic.ini 的 ``sqlite:///data/app.db``，迁移在一个 SQLite 文件上
    「成功」跑完并返回 0 —— 客户 PG 库根本没升级，而命令看起来是绿的。

    门禁只在 ``VIDEO_REPLICA_CUSTOMER_PRODUCTION`` 为真时生效：internal/桌面 lane 依赖
    ``app/db.py`` 的显式 SQLite 路径（CW-053 §3 E2，退役条件未到），不能误伤。
    """
    result = _run_alembic_cli(
        args=["upgrade", "head", "--sql"],
        database_url=None,  # 刻意不设 → 触发 alembic.ini 的 SQLite 默认回退
        customer_production="true",
    )
    assert result.returncode != 0, (
        "customer production must not silently fall back to the SQLite default"
    )
    assert "customer production requires PostgreSQL" in result.stderr, (
        "the refusal must name the customer-production boundary and the required DSN "
        f"variable, got stderr tail: {result.stderr.strip()[-400:]!r}"
    )
    assert "VIDEO_REPLICA_DATABASE_URL" in result.stderr


def test_migrate_sh_verifies_head_after_upgrade() -> None:
    """deploy/postgres/migrate.sh 必须在升级后校验 head。 # RED

    现状脚本已有 customer-production 门禁与 flock，但 ``exec ... alembic upgrade head``
    之后**不校验结果**：数据库已停在 head 时 alembic 静默 no-op 并返回 0，运维据此
    认为「升级完成」，而实际部署的是旧代码配旧 schema。
    """
    script_path = REPO_ROOT / "deploy" / "postgres" / "migrate.sh"
    assert script_path.is_file(), f"missing {script_path}"
    body = script_path.read_text(encoding="utf-8")

    # 既有门禁必须保留（不得为了加校验而拆掉 fail-closed 前置）。
    assert "VIDEO_REPLICA_CUSTOMER_PRODUCTION" in body
    assert "validate_customer_production" in body
    assert "flock" in body

    assert "alembic current" in body, (
        "migrate.sh must read back the applied revision after upgrading"
    )
    assert "expected head" in body or "HEAD" in body, (
        "migrate.sh must compare the applied revision against the expected head"
    )
    # exec 会替换进程，导致其后任何校验都不执行——加校验就必须去掉 exec。
    upgrade_line = next((line for line in body.splitlines() if "alembic upgrade head" in line), "")
    assert not upgrade_line.strip().startswith("exec "), (
        "the upgrade must not be exec'd: a post-upgrade head check has to run afterwards"
    )


# ---------------------------------------------------------------------------
# B 组 — 真实 PG 升级矩阵（挂 CW-007 硬门）
# ---------------------------------------------------------------------------

# 五类代表数据的事实快照列。刻意不用 ``SELECT *``：迁移会**追加**列，
# 星号投影的形状升级前后必然不同，那是假红而非事实丢失。只取已证明
# 在 053/054/055 三起点均存在的固定列（probe7 实测必填列零差异）。
FACT_COLUMNS: dict[str, str] = {
    "users": "id, username, display_name",
    "projects": "id, owner_user_id, name",
    "generation_batches": "id, project_id, created_by_user_id, idempotency_key, request_hash",
    "generation_tasks": "id, batch_id, provider, model",
    "wallets": "user_id, available_credits, reserved_credits",
    "wallet_transactions": (
        "id, user_id, type, available_delta, reserved_delta, "
        "recharge_order_id, task_id, billing_round, idempotency_key"
    ),
    "recharge_orders": "id, user_id, merchant_order_no, amount_fen, credits",
    "activation_code_batches": "id, name, face_value_fen, credits_snapshot, quantity",
    "activation_codes": "id, batch_id, masked_code, digest_key_version",
    "customer_devices": "id, activation_code_id, user_id, slot_no, platform",
    "customer_session_state": "user_id, activation_code_id, device_id, session_id, session_epoch",
    "customer_session_events": "id, event, user_id, device_id, session_id, session_epoch",
}

# 跨升级必须保持语义的两类承载列：
# - JSON 全存 TEXT（head 上 jsonb 列数 = 0），故比对**字节级字面量**
# - timestamptz 刻意播非 UTC 偏移，验证归一到 UTC 后仍指同一时刻
SEED_JSON = '{"prompt":"乡墅复刻","nested":{"n":1,"arr":[1,2,3]},"owner":"众墅之家"}'
SEED_TS = "2026-09-05 08:30:00+08"
SEED_TS_UTC = "2026-09-05 00:30:00+00"

# 单例行表连写用的两个 actor（第二个让写入走 UPDATE 分支）。
# runtime_settings / viral_runtime_controls 的 updated_by_user_id 都是 users 的外键，
# 而空库起点没有播种任何用户，故写入前必须先把 actor 建出来。
SINGLETON_WRITE_ACTORS: tuple[str, ...] = ("cw056-admin", "cw056-admin-2")


def _representative_facts(conn: psycopg.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """五类代表数据的事实快照（owner/钱包/订单/session/任务）。"""
    facts: dict[str, list[tuple[Any, ...]]] = {}
    for table, columns in FACT_COLUMNS.items():
        facts[table] = conn.execute(
            f"SELECT {columns} FROM {table} ORDER BY 1"  # noqa: S608 - fixed test schema
        ).fetchall()
    return facts


def _seed_representative_data(conn: psycopg.Connection) -> None:
    """在起点 head 上播五类代表数据（需 autocommit 或调用方提交）。

    wallet_transactions 必须满足 ``ck_wallet_transactions_shape``：RESERVE 要求
    ``available_delta=-1, reserved_delta=1, task_id NOT NULL, billing_round NOT NULL,
    recharge_order_id NULL``；CHARGE 要求 ``available_delta>0, reserved_delta=0,
    recharge_order_id NOT NULL, task_id NULL, billing_round NULL``。
    """
    # --- owner ---
    conn.execute(
        "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
        ("cw056-owner", "cw056-owner", "矩阵客户"),
    )
    conn.execute(
        "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
        ("cw056-admin", "cw056-admin", "调账管理员"),
    )
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        ("cw056-p1", "cw056-owner", "乡墅项目"),
    )

    # --- 任务（兼做 JSON / timestamptz 语义承载）---
    conn.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        ("cw056-b1", "cw056-p1", "cw056-owner", "cw056-key", "cw056-hash", "{}"),
    )
    conn.execute(
        "UPDATE generation_batches SET request_snapshot_json = %s WHERE id = %s",
        (SEED_JSON, "cw056-b1"),
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, provider, model) VALUES (%s, %s, %s, %s)",
        ("cw056-t1", "cw056-b1", "apilio", "h3"),
    )
    conn.execute(
        "UPDATE generation_tasks SET created_at_utc = %s WHERE id = %s",
        (SEED_TS, "cw056-t1"),
    )

    # --- 钱包 + 账本（升级前既有行必须保持 ledger_sequence IS NULL）---
    conn.execute(
        "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, %s, %s)",
        ("cw056-owner", 9, 1),
    )
    conn.execute(
        "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
        "reserved_delta, task_id, billing_round, idempotency_key) "
        "VALUES (%s, %s, 'RESERVE', -1, 1, %s, 1, %s)",
        ("cw056-tx-reserve", "cw056-owner", "cw056-t1", "cw056:reserve:t1:1"),
    )

    # --- 订单 + 对应的 CHARGE 账本行 ---
    conn.execute(
        "INSERT INTO recharge_orders (id, user_id, merchant_order_no, "
        "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        ("cw056-ro1", "cw056-owner", "cw056-merchant-1", 100, 100, 1000, 100, 2000, 20),
    )
    conn.execute(
        "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
        "reserved_delta, recharge_order_id, idempotency_key) "
        "VALUES (%s, %s, 'CHARGE', 20, 0, %s, %s)",
        ("cw056-tx-charge", "cw056-owner", "cw056-ro1", "cw056:charge:ro1"),
    )

    # --- session 链：激活码批次 → 码 → 设备 → 会话状态 → 会话事件 ---
    conn.execute(
        "INSERT INTO activation_code_batches (id, name, face_value_fen, "
        "unit_price_fen_snapshot, credits_snapshot, quantity, activation_expires_at, "
        "created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        ("cw056-acb1", "矩阵批次", 10000, 10000, 100, 1, "2027-01-01 00:00:00", "cw056-admin"),
    )
    conn.execute(
        "INSERT INTO activation_codes (id, batch_id, code_digest, digest_key_version, "
        "masked_code) VALUES (%s, %s, %s, %s, %s)",
        ("cw056-ac1", "cw056-acb1", "cw056-digest", 1, "CW05-****"),
    )
    conn.execute(
        "INSERT INTO customer_devices (id, activation_code_id, user_id, slot_no, "
        "display_name, platform, fingerprint_hmac, fingerprint_key_version, "
        "token_digest, token_key_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            "cw056-dev1",
            "cw056-ac1",
            "cw056-owner",
            1,
            "矩阵设备",
            "windows",
            "cw056-fp-hmac",
            1,
            "cw056-token-digest",
            1,
        ),
    )
    conn.execute(
        "INSERT INTO customer_session_state (user_id, activation_code_id, device_id, "
        "session_id, token_digest, session_epoch, lease_until) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            "cw056-owner",
            "cw056-ac1",
            "cw056-dev1",
            "cw056-session-1",
            "cw056-token-digest",
            1,
            "2027-01-01 00:00:00",
        ),
    )
    conn.execute(
        "INSERT INTO customer_session_events (id, event, user_id, activation_code_id, "
        "device_id, session_id, session_epoch) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ("cw056-ev1", "LOGIN", "cw056-owner", "cw056-ac1", "cw056-dev1", "cw056-session-1", 1),
    )


def _schema_inventory(conn: psycopg.Connection) -> dict[str, Any]:
    """head schema 的完整**结构**目录（刻意排除易变状态）。

    不含行数、不含 sequence 的 ``last_value``：前者取决于播种，后者取决于写入次数，
    两者都会让「跨起点结构相等」这个真正要证的命题被噪声淹没。
    """
    inventory: dict[str, Any] = {"tables": {}, "functions": [], "sequences": []}

    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"
        ).fetchall()
    ]
    for table in tables:
        columns = conn.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        constraints = conn.execute(
            "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' AND t.relname = %s "
            "ORDER BY c.conname, c.contype",
            (table,),
        ).fetchall()
        indexes = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = %s ORDER BY indexname",
            (table,),
        ).fetchall()
        triggers = conn.execute(
            "SELECT trigger_name, action_timing, event_manipulation, action_statement "
            "FROM information_schema.triggers "
            "WHERE trigger_schema = 'public' AND event_object_table = %s "
            "ORDER BY trigger_name, action_timing, event_manipulation",
            (table,),
        ).fetchall()
        inventory["tables"][table] = {
            "columns": [tuple(row) for row in columns],
            "constraints": [tuple(row) for row in constraints],
            "indexes": [tuple(row) for row in indexes],
            "triggers": [tuple(row) for row in triggers],
        }

    # trigger 体在 pg_proc 里；不把它纳入目录就等于没校验 trigger 真实语义。
    inventory["functions"] = [
        tuple(row)
        for row in conn.execute(
            "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' ORDER BY p.proname"
        ).fetchall()
    ]
    # 只取结构（start/increment/类型），不取 last_value。
    inventory["sequences"] = [
        tuple(row)
        for row in conn.execute(
            "SELECT sequencename, data_type, start_value, increment_by, max_value "
            "FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename"
        ).fetchall()
    ]
    return inventory


def _inventory_digest(inventory: dict[str, Any]) -> str:
    canonical = json.dumps(inventory, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _schema_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {
        label: int(conn.execute(query).fetchone()[0])
        for label, query in _SCHEMA_COUNT_QUERIES.items()
    }


def _singleton_write_runtime_settings(conn: psycopg.Connection, actor: str) -> None:
    """app/settings.py:223 的真实写入形态：显式 id=1 的 upsert。

    runtime_settings / viral_runtime_controls 是**单例行表**：app 与迁移
    （002:64 / 078:110）全部显式赋 id=1，从不走 sequence。因此它们的
    ``*_id_seq`` 停在 ``last_value=1, is_called=False`` 是良性副作用：实测
    ``INSERT ... DEFAULT VALUES`` 会撞主键，但**该路径在 app 上不可达**。
    所以「下一写不碰撞」必须按 app 的真实形态验证，而不是拿不可达的
    DEFAULT VALUES 当断言对象——那会把良性设计写成缺陷（假红）。
    """
    conn.execute(
        "INSERT INTO runtime_settings (id, max_generation_count_per_batch, "
        "max_concurrent_h3_tasks, active_storage_provider, updated_by_user_id, "
        "created_at, updated_at) "
        "VALUES (1, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT(id) DO UPDATE SET "
        "max_generation_count_per_batch = excluded.max_generation_count_per_batch, "
        "max_concurrent_h3_tasks = excluded.max_concurrent_h3_tasks, "
        "active_storage_provider = excluded.active_storage_provider, "
        "updated_by_user_id = excluded.updated_by_user_id, "
        "updated_at = CURRENT_TIMESTAMP",
        (3, 2, "local", actor),
    )


def _singleton_write_viral_controls(conn: psycopg.Connection, actor: str) -> None:
    """app/admin_runtime_routes.py:198-213 的真实形态：UPDATE 后按 rowcount 决定 INSERT。"""
    updated = conn.execute(
        "UPDATE viral_runtime_controls SET collection_enabled = %s, import_enabled = %s, "
        "updated_by_user_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
        (1, 1, actor),
    )
    if updated.rowcount != 1:
        conn.execute(
            "INSERT INTO viral_runtime_controls (id, collection_enabled, import_enabled, "
            "updated_by_user_id) VALUES (1, %s, %s, %s)",
            (1, 1, actor),
        )


@pytest.mark.parametrize("starting_head", MATRIX_STARTING_HEADS)
def test_supported_head_matrix_upgrade_preserves_facts_and_schema(
    starting_head: str, matrix_database: str
) -> None:
    """每个登记起点各跑独立真实 PG 数据副本，升到 head 后事实与 schema 均符登记。

    起点 ``""`` 表示空库（规格保留验收底线：「空库及全部支持旧PG起点→目标成功」）。
    旧起点额外验证：播种的代表数据跨升级事实一致、JSON 与时间语义保持、
    账本下一写不碰撞、单例行表按 app 形态可重复写入。
    """
    dsn = matrix_database
    label = starting_head or "<empty>"

    if starting_head:
        _upgrade(dsn, starting_head)

    seeded: dict[str, list[tuple[Any, ...]]] = {}
    if starting_head:
        with psycopg.connect(dsn, autocommit=True) as conn:
            _seed_representative_data(conn)
            seeded = _representative_facts(conn)
            # 063 刻意不回填（秒级时间戳无法证明并发写入的真实顺序），故升级后
            # 既有行必须保持 ledger_sequence IS NULL。但那个 NULL 要能**归因**，就得
            # 先钉住它在起点根本不存在；否则升级后的 NULL 也可能是「列存在但
            # 没写进去」的另一种成因，两者在本用例里无法区分。
            ledger_column = conn.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'wallet_transactions' "
                "AND column_name = 'ledger_sequence'"
            ).fetchone()[0]
            assert ledger_column == 0, (
                f"{label}: 053/054/055 all predate 063, so ledger_sequence must not exist "
                "yet — otherwise the post-upgrade NULLs lose their attribution"
            )

    _upgrade(dsn, HEAD_REVISION)

    with psycopg.connect(dsn) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        assert version == [(HEAD_REVISION,)], (
            f"{label}: expected exactly one alembic_version row at {HEAD_REVISION}, got {version}"
        )

        if starting_head:
            assert _representative_facts(conn) == seeded, (
                f"{label}: representative facts changed across the upgrade"
            )

            # JSON 承载列：head 上 jsonb 列数 = 0，JSON 全存 TEXT，故比字面量。
            assert (
                conn.execute(
                    "SELECT request_snapshot_json FROM generation_batches WHERE id = %s",
                    ("cw056-b1",),
                ).fetchone()[0]
                == SEED_JSON
            ), f"{label}: TEXT-JSON payload changed"

            # timestamptz：播的是 +08 偏移，必须归一到同一 UTC 时刻而不是被截断。
            stamped = conn.execute(
                "SELECT to_char(created_at_utc AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') "
                "FROM generation_tasks WHERE id = %s",
                ("cw056-t1",),
            ).fetchone()[0]
            assert stamped == SEED_TS_UTC[:19], (
                f"{label}: timestamptz instant changed ({stamped} != {SEED_TS_UTC[:19]})"
            )

        # --- schema 目录对比：每个起点升级后必须与冻结登记逐项相等 ---
        counts = _schema_counts(conn)
        drifted = {
            key: (counts[key], HEAD_SCHEMA_COUNTS[key])
            for key in counts
            if counts[key] != HEAD_SCHEMA_COUNTS[key]
        }
        assert counts == HEAD_SCHEMA_COUNTS, (
            f"{label}: head schema counts drifted (actual, frozen): {drifted}"
        )
        inventory = _schema_inventory(conn)
        assert sorted(inventory["tables"]) == sorted(HEAD_TABLE_NAMES), (
            f"{label}: table set drifted: "
            f"missing={sorted(set(HEAD_TABLE_NAMES) - set(inventory['tables']))} "
            f"unexpected={sorted(set(inventory['tables']) - set(HEAD_TABLE_NAMES))}"
        )
        digest = _inventory_digest(inventory)
        assert digest == HEAD_SCHEMA_DIGEST, (
            f"{label}: full schema inventory digest {digest} != frozen {HEAD_SCHEMA_DIGEST} "
            "(columns/constraints/indexes/triggers/functions/sequences differ)"
        )

    # --- 下一写不碰撞（升级后的真实写入路径）---
    with psycopg.connect(dsn, autocommit=True) as conn:
        if starting_head:
            # 账本：既有行保持 NULL，新写从 1 递增；部分唯一索引
            # ``WHERE ledger_sequence IS NOT NULL`` 让两者共存不冲突。
            conn.execute(
                "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
                "reserved_delta, task_id, billing_round, idempotency_key) "
                "VALUES (%s, %s, 'SETTLE', 0, -1, %s, 1, %s)",
                ("cw056-tx-settle", "cw056-owner", "cw056-t1", "cw056:settle:t1:1"),
            )
            ledger = dict(
                conn.execute("SELECT id, ledger_sequence FROM wallet_transactions").fetchall()
            )
            assert ledger["cw056-tx-reserve"] is None, (
                f"{label}: pre-existing ledger rows must stay unsequenced (063 design)"
            )
            assert ledger["cw056-tx-charge"] is None
            assert ledger["cw056-tx-settle"] == 1, (
                f"{label}: first post-upgrade ledger write must take sequence 1, "
                f"got {ledger['cw056-tx-settle']}"
            )

            # 显式赋值与修改必须被 trigger 拒绝（账本号是数据库专属且不可变）。
            with pytest.raises(psycopg.errors.RaiseException, match="database assigned"):
                conn.execute(
                    "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
                    "reserved_delta, task_id, billing_round, idempotency_key, ledger_sequence) "
                    "VALUES (%s, %s, 'RESERVE', -1, 1, %s, 2, %s, 999)",
                    ("cw056-tx-bad", "cw056-owner", "cw056-t1", "cw056:reserve:t1:2"),
                )

        # 单例行表：按 app 真实形态连写两次（第二次走更新分支），不得撞主键。
        for actor in SINGLETON_WRITE_ACTORS:
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (actor, actor, "矩阵管理员"),
            )
        for actor in SINGLETON_WRITE_ACTORS:
            _singleton_write_runtime_settings(conn, actor)
            _singleton_write_viral_controls(conn, actor)
        assert conn.execute("SELECT count(*) FROM runtime_settings").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM viral_runtime_controls").fetchone()[0] == 1


def test_audit_lineage_downgrade_refusal_is_preserved(matrix_database: str) -> None:
    """非空审计降级拒绝（规格保留验收底线）—— 039 是既有覆盖的漏项。

    025/026/028/032/037/038/042/054 已有降级拒绝用例，唯 ``039_admin_adjustments``
    无覆盖。它的 FK 全部 RESTRICT 指向 users/recharge_orders（刻意不用 CASCADE：
    否则 trigger 被挂起时 CASCADE 会静默抹掉审计行），故播种必须先有 owner 与订单。
    """
    dsn = matrix_database
    _upgrade(dsn, "039_admin_adjustments")

    # 对照组：空审计表允许降级（probe 实测 rc=0）——拒绝必须是**条件性**的，
    # 否则「总是拒绝」也能让本用例通过，那就区分不了两种实现。
    _downgrade(dsn, "038_admin_device_operations")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "038_admin_device_operations"
        )
        assert conn.execute("SELECT to_regclass('admin_adjustments')").fetchone()[0] is None

    # 实验组：非空审计表必须被拒，且拒绝后状态可判定（仍停在 039）。
    _upgrade(dsn, "039_admin_adjustments")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
            ("cw056-owner", "cw056-owner", "矩阵客户"),
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
            ("cw056-admin", "cw056-admin", "调账管理员"),
        )
        conn.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, "
            "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("cw056-ro1", "cw056-owner", "cw056-merchant-1", 100, 100, 1000, 100, 2000, 20),
        )
        conn.execute(
            "INSERT INTO admin_adjustments (id, recharge_order_id, target_user_id, "
            "admin_user_id, source_document_type, source_document_ref, reason, request_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "cw056-adj1",
                "cw056-ro1",
                "cw056-owner",
                "cw056-admin",
                "LEDGER_CORRECTION",
                "cw056-ticket-1",
                "矩阵降级拒绝取证",
                "cw056-req-1",
            ),
        )

    with pytest.raises(RuntimeError, match="cannot downgrade 039_admin_adjustments"):
        _downgrade(dsn, "038_admin_device_operations")

    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "039_admin_adjustments"
        ), "a refused downgrade must leave the revision pointer where it was"
        assert conn.execute("SELECT count(*) FROM admin_adjustments").fetchone()[0] == 1, (
            "the audit row must survive the refused downgrade"
        )


# ---------------------------------------------------------------------------
# C 组 — 失败态可判定（挂 CW-007 硬门）
# ---------------------------------------------------------------------------

# 055（最后一个已发布起点）**之后**才创建的表，用于「失败不留半结构」断言。
# 逐个核对过建表迁移：operation_cost_rates=056、daily_external_prices=058、
# oral_tasks=065、studio_drafts=067、publish_accounts=082。选早于 055 的表会假红
# （它们在起点就已存在），而选拼错的表名会假绿（to_regclass 对不存在的名字同样
# 返回 NULL），所以下面的用例额外断言每个名字都在 HEAD_TABLE_NAMES 里。
# 尾项 publish_accounts 由链尾迁移创建，是「一条都没半建成」的最强哨兵。
LATE_TABLES_AFTER_PUBLISHED_HEAD: tuple[str, ...] = (
    "operation_cost_rates",
    "daily_external_prices",
    "oral_tasks",
    "studio_drafts",
    "publish_accounts",
)


def test_failed_upgrade_leaves_deterministic_state(failstate_database: str) -> None:
    """失败不留半结构或未知状态（规格保留验收底线）。

    在 053 起点预建一张 055 才会建的表，人为制造 CREATE TABLE 冲突，然后
    ``upgrade head`` 必须失败，且失败后：
    - alembic_version **恰好一行**且停在起点（不是空、不是多行、不是中间 revision）
    - 055 之后才有的对象零个被半建成
    - 冲突表未被迁移改写
    """
    dsn = failstate_database
    start = "053_activation_code_archive"
    _upgrade(dsn, start)

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE customer_batch_visibility (cw056_bogus integer)")
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
            ("cw056-owner", "cw056-owner", "矩阵客户"),
        )

    with pytest.raises(Exception) as excinfo:  # noqa: B017, PT011 - 断言落在事后状态上
        _upgrade(dsn, HEAD_REVISION)
    assert "customer_batch_visibility" in str(excinfo.value), (
        f"the failure must come from the injected conflict, got {excinfo.value!r}"
    )

    with psycopg.connect(dsn) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        assert version == [(start,)], (
            f"after a failed upgrade alembic_version must hold exactly the starting "
            f"revision, got {version}"
        )

        # 055 之后才有的对象不得被半建成
        for late_table in LATE_TABLES_AFTER_PUBLISHED_HEAD:
            # 防空转：表名拼错时 to_regclass 也是 NULL，断言会无条件通过。
            assert late_table in HEAD_TABLE_NAMES, (
                f"{late_table} is not a head table; the half-build check would be vacuous"
            )
            assert conn.execute("SELECT to_regclass(%s)", (late_table,)).fetchone()[0] is None, (
                f"{late_table} was half-created by the failed upgrade"
            )

        # 冲突表未被改写（仍是注入的单列形状）
        bogus_columns = [
            str(row[0])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'customer_batch_visibility' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        assert bogus_columns == ["cw056_bogus"], (
            f"the conflicting table must be untouched, got columns {bogus_columns}"
        )

        # 起点已有数据不丢
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
