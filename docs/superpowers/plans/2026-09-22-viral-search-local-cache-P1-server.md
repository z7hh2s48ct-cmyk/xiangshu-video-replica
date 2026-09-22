# 爆款视频业务优化 P1（服务端基座）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付"搜索驱动"的服务端基座——搜索接口（按次/按页计费）+ 内容池 upsert + 封面归档 + 发现记录 + 文案共享缓存读路径，并停用采集调度与采集计费。

**Architecture:** 复用既有爆款模块的持久化与计费机制：`viral_videos` 内容池 upsert、`CoverEnricher` 封面归档、`usage_billing` 两层计费（客户售价 `accept_operation`/`finish_source` + 供应商成本 `meter_call`）、`viral_script_cache` 共享文案。新增一张表 `viral_search_discoveries`（发现记录）与一组 `/api/viral/search*` 路由。采集停用采用"只摘调用不删函数"：`viral_refresh.acquire_viral_refresh_task` 内移除自动入队、`generation_worker.run_pg_collection_once` 移除采集计费结算并改返回语义。

**Tech Stack:** Python 3.12 / FastAPI / PostgreSQL(psycopg) / Alembic / pytest；uv 管理依赖。

**设计依据:** `docs/superpowers/specs/2026-09-21-viral-search-local-cache-design.md`（§5.2 接口契约 / §5.5 停用清单 / §7 计费 / §9 P1 范围 / §12 测试策略）。

---

## 0. 执行前置（必读，逐条确认后再动手）

### 0.1 工作目录

所有命令都在本 worktree 内执行：

```
e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921
```

PowerShell 是 Windows 环境：**不要用 `&&` 连接命令，用 `;`**。

### 0.2 PostgreSQL 测试夹具（每个会话开始前启一次）

```bash
bash scripts/pg-fixture.sh start
```

该脚本幂等（已在运行则跳过），起 `postgres:16-alpine` 容器，端口 `5433`，账号 `testuser/testpass`。测试库 DSN：

```
postgresql://testuser:testpass@localhost:5433/customer_v3_test
```

### 0.3 每个 PowerShell 终端跑测试前设置环境变量

```powershell
$env:TEST_POSTGRESQL_URL = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
```

未设置时 PG 测试会 fail-closed（这是仓库刻意设计，不要绕过；确要临时跳过只能显式 `$env:VIDEO_REPLICA_TEST_ALLOW_PG_SKIP = "1"`，但跳过的结果不能作为验收证据）。

### 0.4 跑测试的固定形式

```powershell
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921\server
uv run python -m pytest tests/<文件>.py::<测试名> -v
```

首次运行 `uv run` 会自动建环境/装依赖，稍慢是正常的。

### 0.5 提交约定

- 每个任务独立 commit；提交信息用中文（与仓库近期提交同风格），标题前缀用 `feat(viral):` / `test(viral):` / `chore(viral):`。
- **每个 commit 末尾必须带 trailer**（与设计文档提交 `609320dc` 同款）：

```
Co-authored-by: peihr666-max
```

示例（Task 1）：

```powershell
git add server/migrations/versions/20260922T1500_viral_search_discoveries.py `
        server/migrations/manifest.json `
        server/tests/test_viral_search_pg.py `
        server/tests/test_cw056_supported_head_matrix.py `
        server/tests/test_postgres_migrations.py `
        server/tests/test_sub_account_schema.py `
        server/tests/test_sub_account_quota.py `
        server/tests/test_activation_code_schema.py `
        server/tests/test_customer_registration.py `
        server/tests/test_customer_devices.py
git commit -m @"
feat(viral): 搜索发现记录表迁移（P1 搜索驱动基座）

- 新增 viral_search_discoveries（user/keyword/platform/video_id/search_date 五列唯一）
- 同步 head 常量（cw056/postgres_migrations/sub_account/activation/registration/devices）
- migration manifest 重新记录

Co-authored-by: peihr666-max
"@
```

### 0.6 禁止事项（仓库既定约定）

- **禁止** `git pack-refs` / `git gc` / `git gc --auto` / `git maintenance run`（本仓库 ref 写入有已知缺陷）。
- **禁止**在 `main` 检出目录改代码；所有改动都在本 worktree。
- **禁止**修改已发布迁移（`055` 及之前的文件字节被 CW-056 哈希冻结，改了必红）。

### 0.7 P1 范围边界（不做什么，避免过度实现）

本计划**只做服务端 P1**（设计文档 §9）：

- ✅ 搜索接口 + 内容池 upsert + 封面归档 + 发现记录 + 搜索计费
- ✅ 停用采集调度与采集计费
- ✅ 文案共享缓存的轻量读路径（`GET /api/viral/search/copy`）
- ❌ 不做客户端（桌面端缓存/播放/下载面板——P2）
- ❌ 不做 H3 直传/过程临时区（P3）
- ❌ 不改 `_PUBLISHED_SQL` 精选门槛、不动推荐位（P4）
- ❌ 不删 `viral_collection.py` / `viral_collection_billing.py` 文件本体，只摘调用点（保留回滚余地）

### 0.8 术语与事实基线（写代码时会用到的现状事实）

| 事实 | 值 | 来源 |
|------|-----|------|
| 平台常量 | `PLATFORM_DOUYIN="douyin"`、`PLATFORM_WECHAT="wechat_channels"` | `server/app/viral_tikhub.py:42-43` |
| 设计文档中的 "wechat" | 一律翻译为代码库常量 `"wechat_channels"` | 本计划全篇按代码库值 |
| admin 路由前缀 | 设计文档写 `/api/admin`，代码库是 `/api/control` | `server/app/admin_runtime_routes.py` router 前缀 |
| 当前迁移 head | `20260921T1200_sub_account_quotas` | `server/migrations/manifest.json` |
| 搜索计费服务名 | `viral_search`（unit=`call`，provider=`tikhub`） | 本计划 Task 2 新增 |
| 幂等键错误码先例 | `VIRAL_LINK_IDEMPOTENCY_KEY_REQUIRED`（422 前缀化） | `server/app/viral_import_routes.py:404-412` |
| `BusinessDbDep` | `Annotated[BusinessDb, Depends(get_business_db)]`，`db.write()` 产出 `(BusinessConnection, CurrentUser)` | `server/app/customer_fence.py:784` |
| `BusinessReadConn` | `Annotated[BusinessConnection, Depends(get_business_read_conn)]`（只读） | `server/app/customer_fence.py:781` |
| PG 上 `BusinessConnection.commit()` 是 no-op | 提交权威 = 外层 `db.write()` 的 fenced 事务 | `server/app/db_portable.py` |
| `viral_script_cache` 列 | `platform, video_id, result_json, producer_task_id, updated_at`（PK = platform+video_id） | `server/app/script_from_audio.py:207-218` |
| 视频号 native 键 | `native_json` 里 `export_id` / `object_nonce_id`（可能为空串） | `server/app/viral_tikhub.py:474-480` |
| 视频号详情签名 | `client.wechat_video_detail(*, export_id="", object_nonce_id=None, object_id=None) -> WechatVideoDetail` | `server/app/viral_tikhub.py:665-671` |
| 封面归档器 | `CoverEnricher(storage=..., fetcher=UrlFetcher(max_bytes=...)).enrich(video)` 返回替换后的 `ViralVideo`（带 `cover_key`） | `server/app/viral_media.py:399-436` |
| 抖音直链模板 | `DOUYIN_CANONICAL_URL_TEMPLATE = "https://www.douyin.com/jingxuan?modal_id={video_id}"` | `server/app/viral_link.py:54` |
| 数据源成本埋点 | `viral_tikhub._request` 内部已有 `meter_call("viral_data", ...)` 包裹，`source` 来自 `billing_context(source_id)` | `server/app/viral_tikhub.py:523-535`、`server/app/billing_meter.py:56-62` |
| 共享文案命中判定 | `_cached_transcript(raw)`：`text` 非空字符串 + `duration_sec` 有限正数 | `server/app/script_from_audio.py:164-180` |

---

## 1. File Structure（全部新建/修改文件一览）

**新建：**

| 文件 | 职责 |
|------|------|
| `server/migrations/versions/20260922T1500_viral_search_discoveries.py` | 发现记录表迁移（P1 唯一新表） |
| `server/app/viral_search.py` | 搜索链路纯服务：外呼归一 / 封面并发归档 / 计费预留（含重试升轮）/ 落库结算 / 上海日期 |
| `server/app/viral_search_routes.py` | `POST /api/viral/search`、`POST /api/viral/search/refresh`、`GET /api/viral/search/copy`、`GET /api/viral/search/discoveries` |
| `server/tests/test_viral_search_pg.py` | P1 全部新测试（迁移/目录/存储/接口/计费/文案） |

**修改：**

| 文件 | 改动 |
|------|------|
| `server/migrations/manifest.json` | `--record` 自动重写（勿手改） |
| `server/app/billing_catalog.py` | `SERVICES` + `SERVICE_INTERFACE` 增 `viral_search` |
| `server/app/viral_tikhub.py` | `pick_douyin_play_url` 改为直取默认播放地址 |
| `server/app/viral_store.py` | `_UPSERT_SQL` 保护既有 category；新增 `mark_viral_discoveries` / `list_viral_discoveries` |
| `server/app/script_from_audio.py` | 新增公开读路径 `cached_transcript` / `cached_transcripts` |
| `server/app/viral_routes.py` | `ViralVideoItem` 增 `hasCopy: bool = False` |
| `server/app/main.py` | `include_router(viral_search_router)` |
| `server/app/admin_runtime_routes.py` | 新增 `GET /api/control/viral/discoveries` 每日汇总 |
| `server/app/viral_refresh.py` | `acquire_viral_refresh_task` 移除自动入队调用 |
| `server/app/generation_worker.py` | `run_pg_collection_once` 移除采集计费结算、返回语义 0/1 |
| `server/app/usage_billing.py` | `reconcile_operations` 增 `viral_search` 30 分钟 sweep |
| `server/tests/test_viral_tikhub.py` | 重写 2 个播放地址测试 |
| `server/tests/test_cw058_content_asset_pg_matrix.py` | 4 处显式播种 `enqueue_due_viral_collections` |
| `server/tests/test_usage_billing.py` | 采集停用改造 + 2 个守卫测试（Task 11）；`viral_search` 超时 sweep 测试（Task 12） |
| `server/tests/test_admin_session_routes.py` | 运营发现汇总用例 + `route_state` TRUNCATE 增补（Task 10） |
| `server/tests/test_cw056_supported_head_matrix.py` 等 7 个文件 | head 常量同步（Task 1） |
| `scripts/ci/test-shards/shard-0.txt` 等 4 个分片 | 重建清单以覆盖新测试文件（Task 13） |

---

## Task 1: 迁移 `viral_search_discoveries` + head 常量同步

**Files:**
- Create: `server/migrations/versions/20260922T1500_viral_search_discoveries.py`
- Create: `server/tests/test_viral_search_pg.py`
- Modify: `server/migrations/manifest.json`（由 `--record` 重写）
- Modify: `server/tests/test_cw056_supported_head_matrix.py`、`server/tests/test_postgres_migrations.py`、`server/tests/test_sub_account_schema.py`、`server/tests/test_sub_account_quota.py`、`server/tests/test_activation_code_schema.py`、`server/tests/test_customer_registration.py`、`server/tests/test_customer_devices.py`

> 背景：本仓库每加一个迁移，都必须同步全部"链尾 head"常量与 CW-056 冻结的 schema 计数，否则 `migration_manifest.py --check` 和 CW-056 矩阵会红。这是仓库的核心仪式，逐条做，别跳。

- [ ] **Step 1: 写失败测试（新测试模块骨架 + 迁移 schema 断言）**

创建 `server/tests/test_viral_search_pg.py`：

```python
"""爆款视频搜索 P1（服务端基座）：搜索接口 / 内容池 / 发现记录 / 计费.

TEST-PG：复用 test_customer_pricing 的夹具链（cw076_registration_test 专属库，
module 级建库 → alembic head → 每用例 TRUNCATE users/wallets 等）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811

import json
from datetime import UTC, datetime

import psycopg
import pytest
from test_customer_pricing import (  # noqa: F401
    account,
    client,
    pricing_client,
    registration_client,
    registration_dsn,
    route_state,
)
from test_usage_billing import credit_lot  # noqa: F401

from app.db_portable import BusinessConnection


def test_viral_search_discoveries_schema_present_at_head(route_state: str) -> None:
    """新迁移在链尾：发现记录表 9 列 + 五列唯一约束 + 两个查询索引，且无 FK."""
    with psycopg.connect(route_state) as pg:
        columns = pg.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='viral_search_discoveries' "
            "ORDER BY ordinal_position"
        ).fetchall()
        assert [row[0] for row in columns] == [
            "id",
            "user_id",
            "keyword",
            "platform",
            "video_id",
            "search_date",
            "searched_at",
            "created_at",
            "updated_at",
        ]
        unique = pg.execute(
            "SELECT array_agg(a.attname ORDER BY a.attname) FROM pg_index i "
            "JOIN pg_class c ON c.oid=i.indrelid "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ANY(i.indkey) "
            "WHERE c.relname='viral_search_discoveries' AND i.indisunique "
            "AND NOT i.indisprimary"
        ).fetchone()
        assert unique[0] == ["keyword", "platform", "search_date", "user_id", "video_id"]
        assert (
            pg.execute(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_name='viral_search_discoveries' AND constraint_type='FOREIGN KEY'"
            ).fetchone()[0]
            == 0
        )
        indexes = {
            row[0]
            for row in pg.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                "AND tablename='viral_search_discoveries'"
            ).fetchall()
        }
        assert {
            "ix_viral_search_discoveries_user_date",
            "ix_viral_search_discoveries_date",
        } <= indexes
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
$env:TEST_POSTGRESQL_URL = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921\server
uv run python -m pytest tests/test_viral_search_pg.py::test_viral_search_discoveries_schema_present_at_head -v
```

Expected: FAIL —— 列查询返回 `[]`（表还不存在），断言 `[] == ["id", ...]` 失败。

- [ ] **Step 3: 写迁移文件**

创建 `server/migrations/versions/20260922T1500_viral_search_discoveries.py`：

```python
"""爆款视频搜索发现记录表（P1：搜索驱动 + 本地缓存）.

Revision ID: 20260922T1500_viral_search_discoveries
Revises: 20260921T1200_sub_account_quotas

「用户每天搜索到了哪些视频」是运营侧每日汇总与客户侧"我的发现"的共同事实来源：

- 一行 = 一个客户在某搜索词下命中的一条视频的"发现事实"。
- ``UNIQUE(user_id, keyword, platform, video_id, search_date)``：同一天同一词
  重复命中同一视频 → upsert 刷新 ``searched_at``（幂等重放不产生重复行）。
- ``search_date`` 为上海时区 YYYY-MM-DD 文本（"每天"维度取自业务口径，见
  ``app/admin_dates.SHANGHAI``）；``searched_at`` 存 UTC ISO 时间戳。
- 不建 FK：删用户不回收发现记录（运营侧历史汇总保留；与 viral_videos 主表的
  "无 FK 全文本列"风格一致）。
- downgrade 有行时 fail-closed：删表会使全部发现记录静默消失。

Runtime is PG-only（与链上相邻迁移同款守卫）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260922T1500_viral_search_discoveries"
down_revision = "20260921T1200_sub_account_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "viral_search_discoveries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("search_date", sa.Text(), nullable=False),
        sa.Column("searched_at", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "keyword",
            "platform",
            "video_id",
            "search_date",
            name="uq_viral_search_discoveries_identity",
        ),
    )
    op.create_index(
        "ix_viral_search_discoveries_user_date",
        "viral_search_discoveries",
        ["user_id", "search_date"],
    )
    op.create_index(
        "ix_viral_search_discoveries_date",
        "viral_search_discoveries",
        ["search_date"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    row_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM viral_search_discoveries")
    ).scalar()
    if row_count:
        raise RuntimeError(
            "cannot downgrade: viral search discoveries exist; dropping the "
            "table would silently remove every discovery record "
            "(delete the rows first if that is really intended)"
        )
    op.drop_index("ix_viral_search_discoveries_date", table_name="viral_search_discoveries")
    op.drop_index(
        "ix_viral_search_discoveries_user_date", table_name="viral_search_discoveries"
    )
    op.drop_table("viral_search_discoveries")
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -v
```

Expected: PASS（`1 passed`）。该测试的 `route_state` 夹具每次会 DROP/CREATE `cw076_registration_test` 并 upgrade 到 head，因此天然验证"新迁移能被 head 升级路径带上"。

- [ ] **Step 5: 同步全部 head 常量（7 个文件 9 处）**

逐个把 `20260921T1200_sub_account_quotas` 改成 `20260922T1500_viral_search_discoveries`（**注意：只改"当前链尾 head"语义的常量，不要动注释里的历史叙述**）：

| 文件 | 位置锚点 |
|------|----------|
| `server/tests/test_cw056_supported_head_matrix.py` | `HEAD_REVISION = "20260921T1200_sub_account_quotas"`（L66 附近）；同时在其上方注释补充一行说明本次追加 |
| `server/tests/test_postgres_migrations.py` | `HEAD_REVISION = "20260921T1200_sub_account_quotas"`（L41 附近），注释补一行 |
| `server/tests/test_sub_account_schema.py` | `_HEAD_REVISION = "20260921T1200_sub_account_quotas"`（L35 附近） |
| `server/tests/test_sub_account_quota.py` | `_HEAD_REVISION = "20260921T1200_sub_account_quotas"`（L67 附近） |
| `server/tests/test_activation_code_schema.py` | `_HEAD_REVISION = "20260921T1200_sub_account_quotas"`（L32 附近） |
| `server/tests/test_customer_registration.py` | `HEAD_REVISION = "20260921T1200_sub_account_quotas"`（L60 附近） |
| `server/tests/test_customer_devices.py` | 两处内联断言 `assert version == "20260921T1200_sub_account_quotas"`（L2817、L2894 附近） |

替换文本示例（以 test_sub_account_quota.py 为例）：

```python
_HEAD_REVISION = "20260922T1500_viral_search_discoveries"
```

- [ ] **Step 6: 重记 migration manifest 并跑守卫**

```powershell
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921
python scripts/ci/migration_manifest.py --record
python scripts/ci/migration_manifest.py --check
```

Expected: `==> recorded server/migrations/manifest.json` 然后 `==> migration guard OK`。

- [ ] **Step 7: 重算 CW-056 冻结的 head schema 面**

CW-056 的 `HEAD_SCHEMA_COUNTS` / `HEAD_SCHEMA_DIGEST` / `HEAD_TABLE_NAMES` 是"空库 → head"的冻结快照，加表必变。先建一个空库升级到 head 并打印真实字面量：

```powershell
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921
$env:TEST_POSTGRESQL_URL = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
python -c "import sys; sys.path.insert(0, 'server'); from tests.pg_test_kit import create_test_database, upgrade_test_database_to_head; dsn = create_test_database('cw056_head_matrix_test'); upgrade_test_database_to_head(dsn); print(dsn)"
python scripts/ci/migration_manifest.py --print-schema --dsn "postgresql://testuser:testpass@localhost:5433/cw056_head_matrix_test"
python -c "import sys; sys.path.insert(0, 'server'); from tests.pg_test_kit import drop_test_database; drop_test_database('cw056_head_matrix_test')"
```

把 `--print-schema` 的输出按提示粘贴覆盖 `test_cw056_supported_head_matrix.py` 的
`HEAD_SCHEMA_COUNTS`、`HEAD_SCHEMA_DIGEST`、`HEAD_TABLE_NAMES`（以及紧随的输出里提示的其他冻结项，如该文件若有）。

预期增量（用于交叉验证，实际以打印值为准）：

- `tables`: 100 → **101**
- `columns`: 1191 → **1200**（+9）
- `primary_keys`: 100 → **101**
- `unique_constraints`: 37 → **38**
- `foreign_keys`: 191（不变）、`check_constraints`: 319（不变）、`partial_indexes`: 37（不变）
- `HEAD_TABLE_NAMES` 插入 `"viral_search_discoveries"`（按字母序在 `"viral_script_cache"` 之后、`"viral_videos"` 之前）

- [ ] **Step 8: 跑迁移相关回归**

```powershell
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921\server
uv run python -m pytest tests/test_cw056_supported_head_matrix.py tests/test_postgres_migrations.py tests/test_sub_account_schema.py tests/test_sub_account_quota.py tests/test_activation_code_schema.py -v
uv run python -m pytest tests/test_customer_registration.py -x -q
```

Expected: 全部 PASS。若 CW-056 报 counts 不匹配，按报错里的 actual 值回改（不要猜，以真实 PG 兜底）。

- [ ] **Step 9: Commit**

按 §0.5 的示例提交（文件清单、message、trailer 都在那）：

```powershell
git status --short
git add server/migrations/versions/20260922T1500_viral_search_discoveries.py server/migrations/manifest.json server/tests/test_viral_search_pg.py server/tests/test_cw056_supported_head_matrix.py server/tests/test_postgres_migrations.py server/tests/test_sub_account_schema.py server/tests/test_sub_account_quota.py server/tests/test_activation_code_schema.py server/tests/test_customer_registration.py server/tests/test_customer_devices.py
git commit -m "feat(viral): 搜索发现记录表迁移与 head 常量同步（P1 基座）

Co-authored-by: peihr666-max"
```

---

## Task 2: 计费目录新增 `viral_search` 服务

**Files:**
- Modify: `server/app/billing_catalog.py`
- Test: `server/tests/test_viral_search_pg.py`（追加）

> 背景：搜索计费走既有计价体系（`accept_operation(service="viral_search")`）。
> 计费目录有两条硬契约（`test_discount_wiring.py` 已断言）：
> ① `set(SERVICE_INTERFACE) == set(SERVICES)`（每个科目都要有接口键映射）；
> ② 可计费科目的接口键必须在 `INTERFACE_KEYS` 里。
> 决策：`viral_search` 复用 `"viral_extract"` 接口键（搜索与爆款数据同属一个客户套餐勾选项，零跨端改动）。
> 单价由管理端配置（`billing_tariffs` 无服务种子行，无 tariff 时 `credits=0` 不阻断功能）。

- [ ] **Step 1: 写失败测试（追加到 `server/tests/test_viral_search_pg.py` 末尾）**

```python
def test_viral_search_service_registered_in_billing_catalog() -> None:
    """viral_search 科目：call/tikhub/viral，接口键复用 viral_extract（两条目录契约）."""
    from app.billing_catalog import (
        INTERFACE_KEYS,
        SERVICES,
        SERVICE_INTERFACE,
        interface_for_service,
    )

    service = SERVICES["viral_search"]
    assert service.name == "爆款视频搜索"
    assert service.unit == "call"
    assert service.provider == "tikhub"
    assert service.module == "viral"
    assert service.customer_charge_allowed is True
    assert interface_for_service("viral_search") == "viral_extract"
    assert SERVICE_INTERFACE["viral_search"] in INTERFACE_KEYS
    assert set(SERVICE_INTERFACE) == set(SERVICES)
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py::test_viral_search_service_registered_in_billing_catalog -v
```

Expected: FAIL —— `KeyError: 'viral_search'`。

- [ ] **Step 3: 实现（改 `server/app/billing_catalog.py`）**

`SERVICES` 字典中 `"viral_data"` 行之后追加一行：

```python
    "viral_data": Service("爆款视频数据请求", "call", "tikhub", "viral"),
    "viral_search": Service("爆款视频搜索", "call", "tikhub", "viral"),
```

`SERVICE_INTERFACE` 字典中 `"viral_data"` 行之后追加一行：

```python
    "viral_data": "viral_extract",
    "viral_search": "viral_extract",
```

- [ ] **Step 4: 跑本测试 + 既有契约测试**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py::test_viral_search_service_registered_in_billing_catalog tests/test_discount_wiring.py tests/test_usage_billing.py::test_catalog_units_are_stable -v
```

Expected: 全部 PASS（若 `test_catalog_units_are_stable` 名称不符，用 `uv run python -m pytest tests/test_usage_billing.py -k catalog -v` 找到对应契约测试跑通即可）。

- [ ] **Step 5: Commit**

```powershell
git add server/app/billing_catalog.py server/tests/test_viral_search_pg.py
git commit -m "feat(viral): 计费目录新增 viral_search 科目（搜索按次计费）

Co-authored-by: peihr666-max"
```

---

## Task 3: `pick_douyin_play_url` 直取默认播放地址

**Files:**
- Modify: `server/app/viral_tikhub.py`（`pick_douyin_play_url`，L336-356 附近）
- Test: `server/tests/test_viral_tikhub.py`（重写 2 个测试）

> 背景（设计文档 §5.5）：现状"仅在浏览器兼容档位中选最低分辨率"是旧在线播放方案的妥协；
> P1 起 `play_url` 语义 = "下发给客户端做本地缓存的源站直链"，**分辨率按源站默认**，
> 不做档位挑选、不采用压缩低清档（2026-09-22 产品确认）。

- [ ] **Step 1: 重写失败测试（改 `server/tests/test_viral_tikhub.py`）**

把现有 `test_pick_douyin_play_url_prefers_lowest_resolution`（L121-130 附近）整段替换为：

```python
def test_pick_douyin_play_url_takes_default_play_addr() -> None:
    """P1：直取源站默认播放地址（不遍历 bit_rate 档位、不挑最低分辨率）."""
    video_block = {
        "play_addr": {"url_list": ["https://cdn/default.mp4", "https://cdn/backup.mp4"]},
        "bit_rate": [
            {"play_addr": {"height": 1280, "url_list": ["https://cdn/720.mp4"]}},
            {"play_addr": {"height": 1024, "url_list": ["https://cdn/540.mp4"]}},
        ],
    }
    assert pick_douyin_play_url(video_block) == "https://cdn/default.mp4"
```

把现有 `test_pick_douyin_play_url_falls_back_to_play_addr`（L148-151 附近）整段替换为：

```python
def test_pick_douyin_play_url_ignores_bit_rate_gears() -> None:
    """没有顶层 play_addr 时返回 None：不再回退到 bit_rate 档位."""
    assert pick_douyin_play_url({}) is None
    assert (
        pick_douyin_play_url(
            {"bit_rate": [{"play_addr": {"url_list": ["https://cdn/540.mp4"]}}]}
        )
        is None
    )
```

> 保留 `test_play_url_prefers_h264_over_smaller_bytevc2`（L636-652 附近）不改：它的顶层
> `play_addr` 就是 h264 默认地址，新实现下断言 `== "https://cdn.test/h264.mp4"` 仍成立，
> 自动成为"顶层地址优先"的回归保障。

- [ ] **Step 2: 跑测试确认失败**

```powershell
uv run python -m pytest tests/test_viral_tikhub.py -k "play_url" -v
```

Expected: `test_pick_douyin_play_url_takes_default_play_addr` FAIL（旧实现选到 540p 的 `https://cdn/540.mp4`）；`test_pick_douyin_play_url_ignores_bit_rate_gears` FAIL（旧实现回退取到 540p）。

- [ ] **Step 3: 实现（改 `server/app/viral_tikhub.py` 的 `pick_douyin_play_url`）**

```python
def pick_douyin_play_url(video_block: Mapping[str, Any]) -> str | None:
    """直取源站默认播放地址（分辨率按默认档，不做档位挑选）.

    2026-09-22 产品确认：客户端本地缓存按源站默认分辨率，不采用压缩低清档。
    默认地址即顶层 ``play_addr`` 的首个 URL；不再遍历 ``bit_rate`` 档位、
    不做 ByteVC/HEVC 过滤（旧"浏览器预览选最低档"妥协废弃）。
    """
    play_addr = video_block.get("play_addr")
    if not isinstance(play_addr, Mapping):
        return None
    return _first_url(play_addr)
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_viral_tikhub.py -v
```

Expected: 全绿（含未改的 `test_play_url_prefers_h264_over_smaller_bytevc2`）。

- [ ] **Step 5: Commit**

```powershell
git add server/app/viral_tikhub.py server/tests/test_viral_tikhub.py
git commit -m "feat(viral): 播放地址直取源站默认档（本地缓存按默认分辨率）

Co-authored-by: peihr666-max"
```

---

## Task 4: `viral_store` 增强（category 保护 + 发现记录读写）

**Files:**
- Modify: `server/app/viral_store.py`
- Test: `server/tests/test_viral_search_pg.py`（追加）

> 背景：搜索 upsert 的条目 `category=""`（搜索不带运营分类），而内容池里可能存在
> 采集时代写入的分类。`_UPSERT_SQL` 现为 `category = excluded.category`，搜索会把
> 既有分类清空 → 改成"excluded 为空则保留原值"。同时新增发现记录的两个读写函数。

- [ ] **Step 1: 写失败测试（追加到 `server/tests/test_viral_search_pg.py`）**

```python
def _viral_seed(platform: str = "douyin", video_id: str = "v-1"):
    from app.viral_tikhub import ViralVideo

    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category="",
        title=f"P1 {video_id}",
        author="作者",
        author_avatar=None,
        verified=False,
        cover_url="https://cdn.example/cover.jpg",
        duration_ms=15000,
        likes=10,
        comments=None,
        shares=None,
        collects=None,
        published_at=None,
        published_display=None,
        like_display=None,
    )


def test_upsert_keeps_existing_category_when_search_category_is_empty(
    route_state: str,
) -> None:
    """搜索条目（category=""）不得清空采集时代写入的分类；非空仍可覆盖."""
    from dataclasses import replace

    from app.viral_store import get_viral_video, upsert_viral_videos

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        upsert_viral_videos(conn, [replace(_viral_seed(), category="施工避坑")])
        upsert_viral_videos(conn, [_viral_seed()])
        kept = get_viral_video(conn, platform="douyin", video_id="v-1")
        assert kept is not None and kept.category == "施工避坑"
        upsert_viral_videos(conn, [replace(_viral_seed(), category="建房预算")])
        updated = get_viral_video(conn, platform="douyin", video_id="v-1")
        assert updated is not None and updated.category == "建房预算"


def test_mark_and_list_viral_discoveries_dedup_per_day(route_state: str) -> None:
    """同键 upsert 刷新 searched_at；不同关键词/日期各自成行；左联内容池带回视频."""
    from app.viral_store import (
        list_viral_discoveries,
        mark_viral_discoveries,
        upsert_viral_videos,
    )

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        upsert_viral_videos(conn, [_viral_seed()])
        mark_viral_discoveries(
            conn,
            user_id="u-1",
            keyword="农村建房",
            platform="douyin",
            video_ids=["v-1", "v-missing"],
            search_date="2026-09-22",
            searched_at="2026-09-22T01:00:00+00:00",
        )
        mark_viral_discoveries(
            conn,
            user_id="u-1",
            keyword="农村建房",
            platform="douyin",
            video_ids=["v-1"],
            search_date="2026-09-22",
            searched_at="2026-09-22T02:00:00+00:00",
        )
        mark_viral_discoveries(
            conn,
            user_id="u-1",
            keyword="农村建房",
            platform="douyin",
            video_ids=["v-1"],
            search_date="2026-09-23",
            searched_at="2026-09-23T01:00:00+00:00",
        )
        raw.commit()
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries WHERE user_id='u-1'"
            ).fetchone()[0]
            == 3
        )
        same_day = list_viral_discoveries(conn, user_id="u-1", search_date="2026-09-22")
        assert len(same_day) == 2
        refreshed = next(item for item in same_day if item.video is not None)
        assert refreshed.searched_at == "2026-09-22T02:00:00+00:00"
        assert refreshed.video is not None and refreshed.video.video_id == "v-1"
        missing = next(item for item in same_day if item.video is None)
        assert missing.video is None
        # 内容池条目缺失时，发现记录自身仍带有平台与视频 ID。
        assert (missing.platform, missing.video_id) == ("douyin", "v-missing")
        other_day = list_viral_discoveries(conn, user_id="u-1", search_date="2026-09-23")
        assert len(other_day) == 1
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "category or discover" -v
```

Expected: FAIL —— `ImportError: cannot import name 'mark_viral_discoveries'`；category 测试因清空而失败。

- [ ] **Step 3: 实现（改 `server/app/viral_store.py`，三处）**

**（a）** `_UPSERT_SQL` 中把这一行（L52 附近）：

```
    category = excluded.category,
```

替换为：

```sql
    category = CASE WHEN excluded.category = '' THEN viral_videos.category
                    ELSE excluded.category END,
```

**（b）** 文件顶部 import 区（L16 附近，`import threading` 之后）加：

```python
from uuid import uuid4
```

**（c）** 在 `update_viral_cover`（L675 附近）**之前**插入发现记录的读写实现：

```python
@dataclass(frozen=True)
class ViralDiscovery:
    """一条发现记录 + 其内容池条目（条目缺失/已删时为 None）."""

    platform: str
    video_id: str
    keyword: str
    search_date: str
    searched_at: str
    video: ViralVideo | None


def mark_viral_discoveries(
    conn: BusinessConnection,
    *,
    user_id: str,
    keyword: str,
    platform: str,
    video_ids: Sequence[str],
    search_date: str,
    searched_at: str,
) -> int:
    """记录"某客户某天用某词搜到了哪些视频"；同键重复命中只刷新 searched_at.

    返回尝试写入的去重条数（幂等重放不产生重复行）。
    """
    marked = 0
    for video_id in dict.fromkeys(video_ids):
        conn.execute(
            """
            INSERT INTO viral_search_discoveries
                (id, user_id, keyword, platform, video_id, search_date, searched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, keyword, platform, video_id, search_date)
            DO UPDATE SET searched_at = excluded.searched_at,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (uuid4().hex, user_id, keyword, platform, video_id, search_date, searched_at),
        )
        marked += 1
    return marked


def list_viral_discoveries(
    conn: BusinessConnection, *, user_id: str, search_date: str
) -> list[ViralDiscovery]:
    """客户"我的发现"（按天）：发现记录左联内容池，带回视频当前数据."""
    rows = conn.execute(
        """
        SELECT v.*, d.platform, d.video_id, d.keyword, d.search_date, d.searched_at
        FROM viral_search_discoveries d
        LEFT JOIN viral_videos v
            ON v.platform = d.platform AND v.video_id = d.video_id
            AND v.deleted_at IS NULL
        WHERE d.user_id = %s AND d.search_date = %s
        ORDER BY d.searched_at DESC, d.keyword, d.video_id
        """,
        (user_id, search_date),
    ).fetchall()
    discoveries: list[ViralDiscovery] = []
    for row in rows:
        mapping = dict(row)
        video = _row_to_video(mapping) if mapping.get("title") is not None else None
        discoveries.append(
            ViralDiscovery(
                platform=str(mapping["platform"]),
                video_id=str(mapping["video_id"]),
                keyword=str(mapping["keyword"]),
                search_date=str(mapping["search_date"]),
                searched_at=str(mapping["searched_at"]),
                video=video,
            )
        )
    return discoveries
```

同时把文件顶部 `from collections.abc import ...` 若不存在 `Sequence` 则补上（`viral_store.py` 现有 import 是 `from typing import Any, Literal, cast`，需要改成）：

```python
from collections.abc import Sequence
from typing import Any, Literal, cast
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "category or discover" -v
```

Expected: 全部 PASS。

- [ ] **Step 5: 跑既有 viral_store 回归（防 category/upsert 语义误伤）**

```powershell
uv run python -m pytest tests/test_viral_store.py -q
uv run python -m pytest tests/test_cw058_content_asset_pg_matrix.py -k "upsert or dedup" -q
```

Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```powershell
git add server/app/viral_store.py server/tests/test_viral_search_pg.py
git commit -m "feat(viral): 内容池 category 保护 + 发现记录读写（P1）

Co-authored-by: peihr666-max"
```

---

## Task 5: 共享文案缓存读路径（`cached_transcript` / `cached_transcripts`）

**Files:**
- Modify: `server/app/script_from_audio.py`
- Test: `server/tests/test_viral_search_pg.py`（追加）

> 背景：`viral_script_cache` 是既有的"视频 → 转写文案"跨用户共享缓存（提取文案
> 成功后由转写流水线写入，见 `_lock_script_cache`）。P1 需要两个**只读**函数：
> 单条查询（GET /api/viral/search/copy 详情）与批量查询（搜索结果回填 `hasCopy`）。只读意味着
> 不加锁、不触发转写、不计费——但必须容忍脏数据（半写状态/非法 JSON 返回 None）。

- [ ] **Step 1: 写失败测试（追加到 `server/tests/test_viral_search_pg.py`）**

```python
def test_cached_transcript_and_batch_lookup(route_state: str) -> None:
    """共享文案缓存只读查询：单条 / 批量 / 未命中 / 脏 JSON 一律安全."""
    from app.asr import TranscriptResult
    from app.script_from_audio import cached_transcript, cached_transcripts

    payload = json.dumps(
        {"text": "农村自建房避坑指南", "duration_sec": 15.0, "language": "zh"},
        ensure_ascii=False,
    )
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_script_cache WHERE video_id IN ('v-1', 'v-broken')")
        raw.execute(
            "INSERT INTO viral_script_cache (platform, video_id, result_json) "
            "VALUES ('douyin', 'v-1', %s), ('douyin', 'v-broken', 'not-json')",
            (payload,),
        )
        raw.commit()
        conn = BusinessConnection.postgres(raw)
        hit = cached_transcript(conn, platform="douyin", video_id="v-1")
        assert hit is not None
        assert hit.result == TranscriptResult("农村自建房避坑指南", 15.0, "zh")
        assert "T" in hit.updated_at  # ISO 时间串（GET /api/viral/search/copy 的 updatedAt）
        assert cached_transcript(conn, platform="douyin", video_id="v-404") is None
        assert cached_transcript(conn, platform="douyin", video_id="v-broken") is None
        batch = cached_transcripts(
            conn,
            [
                ("douyin", "v-1"),
                ("douyin", "v-404"),
                ("douyin", "v-1"),
                ("wechat_channels", "v-1"),
            ],
        )
        assert set(batch) == {("douyin", "v-1")}
        assert batch[("douyin", "v-1")].result.text == "农村自建房避坑指南"
        assert cached_transcripts(conn, []) == {}
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
$env:TEST_POSTGRESQL_URL = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921\server
uv run python -m pytest tests/test_viral_search_pg.py::test_cached_transcript_and_batch_lookup -v
```

Expected: FAIL —— `ImportError: cannot import name 'cached_transcript' from 'app.script_from_audio'`。

- [ ] **Step 3: 实现（改 `server/app/script_from_audio.py`，三处）**

**（a）** import 区（L20）把 `from collections.abc import Callable` 改成：

```python
from collections.abc import Callable, Sequence
```

**（b）** 常量区（L54 `SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES` 之后）追加一行：

```python
_TRANSCRIPT_LOOKUP_CHUNK = 100
```

**（c）** 在 `_cached_transcript`（L164-180）**之后**、`_historical_transcripts`（L183）**之前**插入：

```python
@dataclass(frozen=True)
class CachedTranscriptHit:
    """共享文案缓存命中：转写结果 + 最近写入时间（GET /api/viral/search/copy 的 updatedAt）."""

    result: TranscriptResult
    updated_at: str


def _iso_updated_at(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def cached_transcript(
    conn: BusinessConnection, *, platform: str, video_id: str
) -> CachedTranscriptHit | None:
    """只读查询共享文案缓存；未命中或内容非法返回 None. 不加锁、不转写、不计费."""
    row = conn.execute(
        "SELECT result_json, updated_at FROM viral_script_cache "
        "WHERE platform=%s AND video_id=%s",
        (platform, video_id),
    ).fetchone()
    if row is None:
        return None
    result = _cached_transcript(row["result_json"])
    if result is None:
        return None
    return CachedTranscriptHit(result=result, updated_at=_iso_updated_at(row["updated_at"]))


def cached_transcripts(
    conn: BusinessConnection, refs: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], CachedTranscriptHit]:
    """批量只读查询（搜索结果回填 hasCopy）；去重后分块 OR 查询，未命中不入结果."""
    unique = list(dict.fromkeys(refs))
    hits: dict[tuple[str, str], CachedTranscriptHit] = {}
    for start in range(0, len(unique), _TRANSCRIPT_LOOKUP_CHUNK):
        chunk = unique[start : start + _TRANSCRIPT_LOOKUP_CHUNK]
        clause = " OR ".join(["(platform=%s AND video_id=%s)"] * len(chunk))
        params: list[str] = []
        for platform, video_id in chunk:
            params.extend((platform, video_id))
        rows = conn.execute(
            f"SELECT platform, video_id, result_json, updated_at FROM viral_script_cache "
            f"WHERE {clause}",  # noqa: S608 - 占位符模板拼接，值仍走参数化
            tuple(params),
        ).fetchall()
        for row in rows:
            result = _cached_transcript(row["result_json"])
            if result is None:
                continue
            hits[(str(row["platform"]), str(row["video_id"]))] = CachedTranscriptHit(
                result=result, updated_at=_iso_updated_at(row["updated_at"])
            )
    return hits
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py::test_cached_transcript_and_batch_lookup -v
```

Expected: PASS。

- [ ] **Step 5: 回归既有转写流水线测试（import/行为误伤检查）**

```powershell
uv run python -m pytest tests/test_asr_provider.py -q
```

Expected: 全部 PASS（本改动是纯增量：一个 import 词 + 一个常量 + 一个新 dataclass + 两个新函数）。

- [ ] **Step 6: Commit**

```powershell
git add server/app/script_from_audio.py server/tests/test_viral_search_pg.py
git commit -m "feat(viral): 共享文案缓存只读查询（P1）

Co-authored-by: peihr666-max"
```

---

## Task 6: 搜索执行业务模块（外呼 + 封面归档 + 计费预留 + 落库）

**Files:**
- Create: `server/app/viral_search.py`
- Modify: `server/app/viral_store.py`（`update_viral_cover` 增加 `commit` 开关）
- Test: `server/tests/test_viral_search_pg.py`（追加）

> 背景：搜索取代采集后，"搜到 → 落池 → 记账"需要服务端业务模块，做四件事：
> ① 按平台执行一次搜索外呼（抖音单次列表 / 视频号游标翻页）；
> ② 把命中视频封面并发归档到自有存储（源站签名链接会过期）；
> ③ 客户侧计费：每翻页一次 = 一次计量（`accept_operation` 预留 1 单位，
>    落库成功后 `finish_source` 结算）；FAILED/CANCELLED 后的重试才开新轮次；
> ④ 落库：内容池 upsert + `cover_key` 回写 + 客户发现记录。
>
> 供应商成本不需要本模块处理：`viral_tikhub._request` 内的 `meter_call` 在
> `billing_context(source_id)` 生效时自动落客户单的 source attempt。
>
> 注意两个既有实现的坑：
> - `_UPSERT_SQL` 的 `ON CONFLICT DO UPDATE` **不更新 cover_key**（新行才会写入），
>   所以已存在的行必须靠 `update_viral_cover` 回写；
> - `update_viral_cover` 现在内部会 `conn.commit()`——P1 落库必须在 `db.write()`
>   单事务内完成，给它加 `commit: bool = True` 开关（默认保持既有调用者行为）。

- [ ] **Step 1: 写失败测试（追加到 `server/tests/test_viral_search_pg.py`）**

```python
class _SearchStub:
    """搜索数据源桩：只实现两个搜索面（duck typing 满足 ViralSourceClient 用法）."""

    def __init__(self, *, wechat=None):
        self.wechat = wechat
        self.calls: list[tuple[str, str, str | None]] = []

    def douyin_search(self, *, keyword, category="", sort_type="1", publish_time="7"):
        self.calls.append(("douyin", keyword, None))
        return [_viral_seed()]

    def wechat_search_page(
        self, *, keyword, category="", sort="hot", publish_time="week", cursor=None
    ):
        self.calls.append(("wechat", keyword, cursor))
        return self.wechat


def test_search_date_shanghai_follows_shanghai_calendar() -> None:
    """发现日期按上海日历归属（UTC 23:30 已是上海次日）."""
    from app.viral_search import search_date_shanghai

    assert search_date_shanghai(datetime(2026, 9, 21, 23, 30, tzinfo=UTC)) == "2026-09-22"
    assert search_date_shanghai(datetime(2026, 9, 22, 16, 30, tzinfo=UTC)) == "2026-09-23"


def test_run_viral_search_platform_shapes() -> None:
    """抖音单次列表（无翻页）；视频号透传 cursor 与 has_more."""
    from app.viral_search import run_viral_search
    from app.viral_tikhub import WechatSearchPage

    stub = _SearchStub()
    douyin = run_viral_search(stub, keyword="农村建房", platform="douyin")
    assert [video.video_id for video in douyin.items] == ["v-1"]
    assert douyin.cursor is None and douyin.has_more is False
    assert stub.calls == [("douyin", "农村建房", None)]

    stub = _SearchStub(
        wechat=WechatSearchPage(
            videos=[_viral_seed("wechat_channels", "w-1")], cursor="c-2", has_more=True
        )
    )
    wechat = run_viral_search(stub, keyword="农村建房", platform="wechat_channels", cursor="c-1")
    assert [video.video_id for video in wechat.items] == ["w-1"]
    assert wechat.cursor == "c-2" and wechat.has_more is True
    assert stub.calls == [("wechat", "农村建房", "c-1")]


def test_archive_search_covers_threads_and_falls_back(monkeypatch) -> None:
    """封面归档：成功者换自有稳定地址；下载失败者保留源站链接且不抛错."""
    from dataclasses import replace

    from app import viral_media
    from app.viral_search import archive_search_covers

    class _FakeStorage:
        def __init__(self) -> None:
            self.puts: dict[str, tuple[bytes, str | None]] = {}

        def head_object(self, key):
            return None

        def put_object(self, key, data, *, content_type=None):
            self.puts[key] = (data, content_type)

    def fake_iter_fetch(self, url):
        if "bad.example" in url:
            raise viral_media.ViralMediaError("媒体地址无法解析")
        yield b"\xff\xd8\xff\xe0fakejpeg"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", fake_iter_fetch)
    good = _viral_seed()
    bad = replace(_viral_seed(video_id="v-2"), cover_url="https://bad.example/cover.jpg")
    storage = _FakeStorage()
    enriched = archive_search_covers(storage, [good, bad])
    assert enriched[0].cover_key == viral_media.viral_cover_key("douyin", "v-1")
    assert enriched[0].cover_url == "/api/viral/covers/douyin/v-1"
    assert enriched[1].cover_key is None
    assert enriched[1].cover_url == "https://bad.example/cover.jpg"
    assert storage.puts  # 至少一个封面已归档


def test_reserve_search_operation_rounds_and_replays(client, route_state) -> None:
    """计费轮次：PENDING/SUCCEEDED 幂等复用；FAILED 后重试才开新轮次重新预留."""
    from app.usage_billing import finish_operation
    from app.viral_search import reserve_search_operation

    _, uid = account(client, "search_reserve")

    def reserve() -> str:
        with psycopg.connect(route_state) as raw:
            return reserve_search_operation(
                BusinessConnection.postgres(raw),
                user_id=uid,
                source_id="viral-search:k1",
                request_fingerprint="douyin:农村建房:",
            )

    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    first = reserve()
    assert reserve() == first  # PENDING 幂等：同轮次复用同一单，不重复扣费
    with psycopg.connect(route_state) as raw:
        finish_operation(
            BusinessConnection.postgres(raw), operation_id=first, units=0, succeeded=False
        )
    second = reserve()  # FAILED（外呼失败已释放）后重试开新轮次
    assert second != first
    with psycopg.connect(route_state) as raw:
        finish_operation(
            BusinessConnection.postgres(raw), operation_id=second, units=1, succeeded=True
        )
    assert reserve() == second  # SUCCEEDED 不重复扣费
    with psycopg.connect(route_state) as raw:
        rounds = [
            row[0]
            for row in raw.execute(
                "SELECT billing_round FROM billing_operations "
                "WHERE source_id='viral-search:k1' ORDER BY billing_round"
            ).fetchall()
        ]
        assert rounds == [1, 2]
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (47, 0)


def test_persist_viral_search_writes_pool_discoveries_and_billing(client, route_state) -> None:
    """落库三件事：内容池 upsert + cover_key 回写 + 发现记录 + 计费结算."""
    from dataclasses import replace

    from app.viral_search import persist_viral_search, reserve_search_operation

    _, uid = account(client, "search_persist")
    seeded = replace(_viral_seed(), cover_key="viral/covers/douyin/v-1.jpg")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
        conn = BusinessConnection.postgres(raw)
        operation = reserve_search_operation(
            conn, user_id=uid, source_id="viral-search:k2", request_fingerprint="douyin:自建房:"
        )
        persist_viral_search(
            conn,
            user_id=uid,
            source_id="viral-search:k2",
            keyword="自建房",
            platform="douyin",
            videos=[seeded],
            search_date="2026-09-22",
            searched_at="2026-09-22T01:00:00+00:00",
        )
        assert (
            raw.execute(
                "SELECT cover_key FROM viral_videos WHERE platform='douyin' AND video_id='v-1'"
            ).fetchone()[0]
            == "viral/covers/douyin/v-1.jpg"
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries "
                "WHERE user_id=%s AND keyword='自建房' AND search_date='2026-09-22'",
                (uid,),
            ).fetchone()[0]
            == 1
        )
        assert tuple(
            raw.execute(
                "SELECT state, charged_credits FROM billing_operations WHERE id=%s", (operation,)
            ).fetchone()
        ) == ("SUCCEEDED", 3)
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "shanghai or platform_shapes or covers or reserve_search or persist_viral" -v
```

Expected: FAIL —— `ModuleNotFoundError: No module named 'app.viral_search'`（5 个用例全部收集失败）。

- [ ] **Step 3: 实现（两处）**

**（a）** 改 `server/app/viral_store.py` 的 `update_viral_cover`（L675-691），加 `commit` 开关：

```python
def update_viral_cover(
    conn: BusinessConnection,
    *,
    platform: str,
    video_id: str,
    cover_key: str,
    commit: bool = True,
) -> None:
    """封面落存储成功后回写 key（路由据此下发自有稳定地址）.

    ``commit=False`` 供已在事务内的调用方（搜索落库）使用，避免中途提交。
    """
    conn.execute(
        """
        UPDATE viral_videos
        SET cover_key = %s, updated_at = CURRENT_TIMESTAMP
        WHERE platform = %s AND video_id = %s
        """,
        (cover_key, platform, video_id),
    )
    if commit:
        conn.commit()
```

**（b）** 创建 `server/app/viral_search.py`：

```python
"""搜索驱动的爆款业务（P1）：外呼 → 封面归档 → 计费 → 落库.

搜索成为唯一内容入口后，采集调度退场（design §5.5）。本模块只做四件事：

1. 按平台执行一次搜索外呼（抖音单次列表 / 视频号游标翻页）；
2. 把命中视频的封面归档到自有存储（源站签名链接会过期）；
3. 幂等地预留/推进客户侧搜索计费（每翻页一次 = 一次计量）；
4. 把结果 upsert 进内容池、回写封面 key、记录客户发现、结算计费单。

供应商成本由 ``billing_meter.meter_call`` 在 ``viral_tikhub._request`` 内记账
（``billing_context(source_id)`` 存在时自动落客户单的 source attempt）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from app.admin_dates import SHANGHAI
from app.db_portable import BusinessConnection
from app.usage_billing import accept_operation, finish_source
from app.viral_media import CoverEnricher, UrlFetcher, ViralStorage
from app.viral_store import mark_viral_discoveries, update_viral_cover, upsert_viral_videos
from app.viral_tikhub import (
    PLATFORM_DOUYIN,
    PLATFORM_WECHAT,
    ViralSourceClient,
    ViralVideo,
)

SEARCH_SERVICE = "viral_search"
SEARCH_COVER_MAX_BYTES = 10 * 1024 * 1024
SEARCH_COVER_WORKERS = 8


@dataclass(frozen=True)
class ViralSearchPage:
    """一次搜索外呼的结果（两个平台归一后的形态）."""

    items: list[ViralVideo]
    cursor: str | None
    has_more: bool


def search_date_shanghai(now: datetime | None = None) -> str:
    """发现日期按上海日历归属，客户端与后台看到同一天."""
    instant = now or datetime.now(UTC)
    return instant.astimezone(SHANGHAI).strftime("%Y-%m-%d")


def run_viral_search(
    client: ViralSourceClient,
    *,
    keyword: str,
    platform: str,
    cursor: str | None = None,
) -> ViralSearchPage:
    """执行一次搜索外呼：抖音单次列表（无翻页）；视频号按游标翻页."""
    if platform == PLATFORM_WECHAT:
        page = client.wechat_search_page(keyword=keyword, cursor=cursor)
        return ViralSearchPage(
            items=list(page.videos), cursor=page.cursor, has_more=page.has_more
        )
    if platform != PLATFORM_DOUYIN:
        raise ValueError(f"unsupported search platform: {platform}")
    return ViralSearchPage(
        items=list(client.douyin_search(keyword=keyword)), cursor=None, has_more=False
    )


def archive_search_covers(
    storage: ViralStorage,
    videos: list[ViralVideo],
    *,
    max_workers: int = SEARCH_COVER_WORKERS,
) -> list[ViralVideo]:
    """并发把搜索结果封面归档到自有存储；单条失败保留源站链接兜底."""
    if not videos:
        return []
    enricher = CoverEnricher(
        storage=storage, fetcher=UrlFetcher(max_bytes=SEARCH_COVER_MAX_BYTES)
    )
    with ThreadPoolExecutor(max_workers=min(max_workers, len(videos))) as pool:
        return list(pool.map(enricher.enrich, videos))


def reserve_search_operation(
    conn: BusinessConnection,
    *,
    user_id: str,
    source_id: str,
    request_fingerprint: str,
) -> str:
    """幂等预留一次搜索计费（1 单位）；失败/取消后的重试才开新轮次.

    同一 ``source_id``（= 幂等键派生）在 PENDING/SUCCEEDED 状态下复用原单，
    不重复扣费；FAILED/CANCELLED 表示本次搜索未成功交付，重试重新预留。
    """
    latest = conn.execute(
        "SELECT billing_round, state FROM billing_operations WHERE user_id=%s AND service=%s "
        "AND source_id=%s ORDER BY billing_round DESC LIMIT 1",
        (user_id, SEARCH_SERVICE, source_id),
    ).fetchone()
    billing_round = 1
    if latest is not None:
        billing_round = int(latest["billing_round"])
        if str(latest["state"]) in {"FAILED", "CANCELLED"}:
            billing_round += 1
    return accept_operation(
        conn,
        user_id=user_id,
        service=SEARCH_SERVICE,
        source_id=source_id,
        units=1,
        billing_round=billing_round,
        request_fingerprint=request_fingerprint,
    )


def persist_viral_search(
    conn: BusinessConnection,
    *,
    user_id: str,
    source_id: str,
    keyword: str,
    platform: str,
    videos: list[ViralVideo],
    search_date: str,
    searched_at: str,
) -> None:
    """搜索结果落库（内容池 + 封面 key + 发现记录 + 计费结算），不提交事务.

    调用方必须已在 ``db.write()`` 事务内、且已通过 SESSION_REPLACED 校验。
    0 结果也算成功交付（供应商成本已发生，客户消耗一次搜索）。
    """
    upsert_viral_videos(conn, videos, commit=False)
    for video in videos:
        if video.cover_key:
            update_viral_cover(
                conn,
                platform=video.platform,
                video_id=video.video_id,
                cover_key=video.cover_key,
                commit=False,
            )
    mark_viral_discoveries(
        conn,
        user_id=user_id,
        keyword=keyword,
        platform=platform,
        video_ids=[video.video_id for video in videos],
        search_date=search_date,
        searched_at=searched_at,
    )
    finish_source(conn, source_id, units=1, succeeded=True, service=SEARCH_SERVICE)
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "shanghai or platform_shapes or covers or reserve_search or persist_viral" -v
```

Expected: 5 个用例全部 PASS。

- [ ] **Step 5: 回归既有内容池与计费测试（`update_viral_cover` 签名改动检查）**

```powershell
uv run python -m pytest tests/test_viral_store.py tests/test_usage_billing.py -q
```

Expected: 全部 PASS（`commit` 带默认值，既有调用者行为不变）。

- [ ] **Step 6: Commit**

```powershell
git add server/app/viral_search.py server/app/viral_store.py server/tests/test_viral_search_pg.py
git commit -m "feat(viral): 搜索执行 + 封面归档 + 计费预留 + 落库（P1）

Co-authored-by: peihr666-max"
```

---

## Task 7: 客户搜索接口 `POST /api/viral/search`

**Files:**
- Create: `server/app/viral_search_routes.py`
- Modify: `server/app/viral_routes.py`（`ViralVideoItem` 增 `hasCopy`）
- Modify: `server/app/main.py`（注册 router）
- Test: `server/tests/test_viral_search_pg.py`（追加）

> 背景：把 Task 5/6 的部件接成客户可用的接口。一次翻页 = 一次外呼 + 一次计费 +
> 一次落库，锚定 `source_id = "viral-search:{Idempotency-Key}"`：
> `billing_context(source_id)` 让 `viral_tikhub._request` 内的 meter_call 把供应
> 商成本落到客户单的 source attempt 上。
>
> **重放语义（P1 明确取舍）**：同幂等键重复请求复用同一计费轮次（绝不重复扣费），
> 但**会重新外呼**。P1 不做服务端响应重放（客户端持有本地缓存，重放罕见；
> 重新外呼的供应商成本由平台承担）。若该键此前已成功结算，本次外呼又失败，
> 不回头改已成功的计费单。

- [ ] **Step 1: 写失败测试（追加到 `server/tests/test_viral_search_pg.py`）**

```python
@pytest.fixture()
def search_client(client):
    from app.viral_search_routes import router as viral_search_router

    client.app.include_router(viral_search_router)
    yield client
    client.app.dependency_overrides.clear()


def _use_search_stub(search_client, stub) -> None:
    from app.viral_search_routes import get_viral_search_source_client

    search_client.app.dependency_overrides[get_viral_search_source_client] = lambda: stub


class _FakeCoverStorage:
    """封面归档用的假存储（只记录 put，不落盘）."""

    def head_object(self, key):
        return None

    def put_object(self, key, data, *, content_type=None):
        pass


def _patch_search_infra(monkeypatch) -> None:
    from app import viral_media

    def fake_iter_fetch(self, url):
        yield b"\xff\xd8\xff\xe0fakejpeg"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", fake_iter_fetch)
    monkeypatch.setattr(
        "app.viral_search_routes.get_media_storage", lambda conn: _FakeCoverStorage()
    )


def test_search_requires_valid_idempotency_key(search_client) -> None:
    """缺/空 Idempotency-Key → 422 前缀化错误码（先例：viral_import_routes）."""
    _use_search_stub(search_client, _SearchStub())
    headers, _ = account(search_client, "search_key")
    response = search_client.post(
        "/api/viral/search", json={"keyword": "农村建房", "platform": "douyin"}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VIRAL_SEARCH_IDEMPOTENCY_KEY_REQUIRED"


def test_search_charges_persists_and_flags_copy(
    search_client, route_state, monkeypatch
) -> None:
    """一次搜索：外呼 + 封面归档 + 内容池/发现落库 + 按次计费一次."""
    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _SearchStub())
    headers, uid = account(search_client, "search_flow")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
        raw.execute("DELETE FROM viral_script_cache WHERE platform='douyin' AND video_id='v-1'")
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-flow-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["videoId"] for item in body["items"]] == ["v-1"]
    assert body["items"][0]["hasCopy"] is False
    assert body["billing"] == {"charged": 3, "unit": "call"}
    assert body["hasMore"] is False and body["cursor"] is None
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_videos WHERE platform='douyin' AND video_id='v-1'"
            ).fetchone()[0]
            == 1
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries "
                "WHERE user_id=%s AND keyword='农村建房'",
                (uid,),
            ).fetchone()[0]
            == 1
        )
        assert (
            raw.execute(
                "SELECT state FROM billing_operations "
                "WHERE source_id='viral-search:search-flow-1'"
            ).fetchone()[0]
            == "SUCCEEDED"
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (47, 0)


def test_search_flags_has_copy_on_cache_hit(search_client, route_state, monkeypatch) -> None:
    """共享文案缓存命中 → hasCopy=True（免费路径 charged=0 不受影响）."""
    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _SearchStub())
    headers, _ = account(search_client, "search_copy")
    payload = json.dumps({"text": "已有文案", "duration_sec": 12.0}, ensure_ascii=False)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,0) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=0"
        )
        raw.execute(
            "INSERT INTO viral_script_cache (platform, video_id, result_json) "
            "VALUES ('douyin', 'v-1', %s) ON CONFLICT (platform, video_id) "
            "DO UPDATE SET result_json=excluded.result_json",
            (payload,),
        )
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-copy-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"][0]["hasCopy"] is True
    assert body["billing"] == {"charged": 0, "unit": "call"}


def test_search_upstream_failure_refunds_and_fails_closed(
    search_client, route_state, monkeypatch
) -> None:
    """外呼失败：计费单转 FAILED 并释放预留，返回 503 稳定错误码."""
    from app.viral_tikhub import ViralSourceError

    class _FailingStub:
        def douyin_search(self, *, keyword, category="", sort_type="1", publish_time="7"):
            raise ViralSourceError("爆款数据源暂时不可用")

    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _FailingStub())
    headers, uid = account(search_client, "search_fail")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-fail-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_SEARCH_UPSTREAM_FAILED"
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT state FROM billing_operations "
                "WHERE source_id='viral-search:search-fail-1'"
            ).fetchone()[0]
            == "FAILED"
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (50, 0)


def test_search_insufficient_credits_fails_closed(
    search_client, route_state, monkeypatch
) -> None:
    """额度不足：402 INSUFFICIENT_CREDITS，不扣预留、不落发现记录（设计 §5.2）."""
    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _SearchStub())
    headers, uid = account(search_client, "search_poor")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=1, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-poor-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "INSUFFICIENT_CREDITS"
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (1, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            == 0
        )


def test_search_empty_result_charges_once(search_client, route_state, monkeypatch) -> None:
    """0 结果：正常返回空列表，但仍完成一次计量（供应商成本已发生，设计 §7）."""
    _patch_search_infra(monkeypatch)

    class _EmptyStub:
        """返回空列表的数据源桩（0 结果是一次合法交付）."""

        def douyin_search(self, *, keyword, category="", sort_type="1", publish_time="7"):
            return []

    _use_search_stub(search_client, _EmptyStub())
    headers, uid = account(search_client, "search_empty")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    response = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-empty-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["billing"] == {"charged": 3, "unit": "call"}
    assert body["hasMore"] is False and body["cursor"] is None
    with psycopg.connect(route_state) as raw:
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (47, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_search_discoveries WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            == 0
        )


def test_search_replay_reuses_billing_round(search_client, route_state, monkeypatch) -> None:
    """同幂等键重放：同一计费单同一轮次，不重复扣费."""
    _patch_search_infra(monkeypatch)
    _use_search_stub(search_client, _SearchStub())
    headers, uid = account(search_client, "search_replay")
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,3) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=3"
        )
    first = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-replay-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    second = search_client.post(
        "/api/viral/search",
        headers={**headers, "Idempotency-Key": "search-replay-1"},
        json={"keyword": "农村建房", "platform": "douyin"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["billing"] == first.json()["billing"]
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations "
                "WHERE source_id='viral-search:search-replay-1'"
            ).fetchone()[0]
            == 1
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (47, 0)
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "requires_valid or charges_persists or flags_has_copy or insufficient_credits or empty_result or upstream_failure or replay_reuses" -v
```

Expected: FAIL —— `ModuleNotFoundError: No module named 'app.viral_search_routes'`（fixture 导入期失败）。

- [ ] **Step 3: 实现（三处）**

**（a）** 改 `server/app/viral_routes.py` 的 `ViralVideoItem`（L104 后、`availability` 之前）插入一行：

```python
    isFavorite: bool = False
    hasCopy: bool = False
    availability: Literal["available", "hidden", "unavailable"] = "available"
```

**（b）** 创建 `server/app/viral_search_routes.py`：

```python
"""客户搜索接口（P1）：一次翻页 = 一次外呼 + 一次计费 + 一次落库.

- ``POST /api/viral/search``：关键词 + 平台（+ 游标）→ 命中视频 + 计费回执。
- 幂等键由 ``Idempotency-Key`` 承载，派生出 ``source_id``；同值重复请求复用
  同一计费轮次（不重复扣费），但会重新外呼（P1 不做服务端响应重放）。
- 供应商成本：``billing_context(source_id)`` 让 ``viral_tikhub._request`` 内的
  meter_call 自动落客户单的 source attempt；客户侧预留由
  ``reserve_search_operation`` 完成，落库成功后 ``persist_viral_search`` 结算。
- 响应字段与既有 ``ViralVideoItem`` 完全一致；``hasCopy`` 标记共享文案命中。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import Database
from app.billing_catalog import SERVICES
from app.billing_meter import billing_context
from app.customer_fence import BusinessDbDep
from app.media_routes import api_base_url, get_media_storage
from app.permissions import require_not_auditor
from app.script_from_audio import cached_transcripts
from app.usage_billing import finish_source
from app.viral_routes import ViralVideoItem
from app.viral_search import (
    archive_search_covers,
    persist_viral_search,
    reserve_search_operation,
    run_viral_search,
    search_date_shanghai,
)
from app.viral_tikhub import (
    ViralSourceClient,
    ViralSourceError,
    ViralSourceUnavailable,
    ViralVideo,
    viral_source_client_from_settings,
)

router = APIRouter(prefix="/api/viral", tags=["viral"])


class ViralSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=100)
    platform: Literal["douyin", "wechat_channels"] = "douyin"
    cursor: str | None = Field(default=None, max_length=2048)


class ViralSearchBilling(BaseModel):
    charged: int
    unit: str


class ViralSearchResponse(BaseModel):
    items: list[ViralVideoItem]
    cursor: str | None
    hasMore: bool
    billing: ViralSearchBilling


def get_viral_search_source_client(conn: Database) -> ViralSourceClient | None:
    """数据源客户端依赖入口（测试可通过 dependency_overrides 替换）."""
    try:
        return viral_source_client_from_settings(conn)
    except ViralSourceUnavailable:
        return None


ViralSearchSourceClientDep = Annotated[
    ViralSourceClient | None, Depends(get_viral_search_source_client)
]


def _search_item(video: ViralVideo, *, has_copy: bool) -> ViralVideoItem:
    item = ViralVideoItem(**video.to_client_dict(), hasCopy=has_copy)
    if not video.cover_key:
        item.coverUrl = None
    if item.coverUrl and item.coverUrl.startswith("/"):
        # 自有稳定封面路由：下发绝对地址，跨源前端（桌面/开发）可直接加载。
        item.coverUrl = f"{api_base_url()}{item.coverUrl}"
    return item


@router.post("/search", response_model=ViralSearchResponse)
def search_viral_videos(
    payload: ViralSearchRequest,
    db: BusinessDbDep,
    client: ViralSearchSourceClientDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ViralSearchResponse:
    key = idempotency_key.strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    keyword = payload.keyword.strip()
    cursor = (payload.cursor or "").strip() or None
    if not keyword:
        raise HTTPException(
            status_code=422,
            detail={"code": "VIRAL_SEARCH_KEYWORD_REQUIRED", "message": "搜索关键词不能为空。"},
        )
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_SEARCH_UNAVAILABLE",
                "message": "爆款视频数据源暂不可用，请联系管理员。",
            },
        )
    source_id = f"viral-search:{key}"
    fingerprint = f"{payload.platform}:{keyword}:{cursor or ''}"
    with db.write() as (conn, actor):
        with conn:
            require_not_auditor(
                conn,
                actor=actor,
                action="viral.search",
                entity_type="viral_search",
                entity_id=payload.platform,
            )
            reserve_search_operation(
                conn, user_id=actor.id, source_id=source_id, request_fingerprint=fingerprint
            )
            storage = get_media_storage(conn)
    try:
        with billing_context(source_id):
            page = run_viral_search(
                client, keyword=keyword, platform=payload.platform, cursor=cursor
            )
    except ViralSourceError as exc:
        with db.write() as (fail_conn, _fail_actor):
            with fail_conn:
                latest = fail_conn.execute(
                    "SELECT state FROM billing_operations WHERE service='viral_search' "
                    "AND source_id=%s ORDER BY billing_round DESC LIMIT 1",
                    (source_id,),
                ).fetchone()
                if latest is not None and str(latest["state"]) == "PENDING":
                    finish_source(
                        fail_conn, source_id, units=0, succeeded=False, service="viral_search"
                    )
        raise HTTPException(
            status_code=503,
            detail={"code": "VIRAL_SEARCH_UPSTREAM_FAILED", "message": str(exc)},
        ) from exc
    enriched = archive_search_covers(storage, page.items)
    searched_at = datetime.now(UTC).isoformat()
    with db.write() as (conn, completed_actor):
        with conn:
            if completed_actor.id != actor.id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            persist_viral_search(
                conn,
                user_id=actor.id,
                source_id=source_id,
                keyword=keyword,
                platform=payload.platform,
                videos=enriched,
                search_date=search_date_shanghai(),
                searched_at=searched_at,
            )
            charged = int(
                conn.execute(
                    "SELECT charged_credits FROM billing_operations "
                    "WHERE service='viral_search' AND source_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1",
                    (source_id,),
                ).fetchone()[0]
            )
            hits = cached_transcripts(
                conn, [(video.platform, video.video_id) for video in enriched]
            )
    return ViralSearchResponse(
        items=[
            _search_item(video, has_copy=(video.platform, video.video_id) in hits)
            for video in enriched
        ],
        cursor=page.cursor,
        hasMore=page.has_more,
        billing=ViralSearchBilling(charged=charged, unit=SERVICES["viral_search"].unit),
    )
```

**（c）** 改 `server/app/main.py` 两处——import 区（L81 后）加：

```python
from app.viral_search_routes import router as viral_search_router
```

`include_router` 区（L404 `app.include_router(viral_import_router)` 之后）加：

```python
app.include_router(viral_search_router)
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "requires_valid or charges_persists or flags_has_copy or insufficient_credits or empty_result or upstream_failure or replay_reuses" -v
```

Expected: 7 个用例全部 PASS。

- [ ] **Step 5: 回归（本文件全量 + 爆款媒体接口）**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py tests/test_viral_media.py -q
```

Expected: 全部 PASS（`ViralVideoItem` 加的是带默认值的新字段，既有响应不变）。

- [ ] **Step 6: Commit**

```powershell
git add server/app/viral_search_routes.py server/app/viral_routes.py server/app/main.py server/tests/test_viral_search_pg.py
git commit -m "feat(viral): 客户搜索接口 POST /api/viral/search（P1）

Co-authored-by: peihr666-max"
```
---

## Task 8: 视频直链刷新 `POST /api/viral/search/refresh`

**Files:**
- Modify: `server/app/viral_search_routes.py`（追加依赖与路由）
- Test: `server/tests/test_viral_search_pg.py`（追加）

> 背景：客户端"本地缓存"的最后一步——把内容池里的视频换成一次性凭据：
> 抖音走直链解析服务（`DouyidouLinkClient.resolve(..., purpose="copy")` 取默认
> 清晰度的 `video_url`）；视频号用内容池存量 `export_id`/`object_nonce_id` 换
> `full_url` + `decode_key`（客户端本地解密）。
>
> **不与客户计费**：本接口不设 `billing_context`，客户侧零扣费。视频号详情调用的
> 供应商成本由 `viral_tikhub._request` 内的 `meter_call("viral_data")` 自动落
> **平台操作**（`billing_meter` 的无 source 分支）；抖音解析不走 viral_data meter。
> 与既有 viral 路由一致：不设审计员守卫（`viral_routes.py` 亦无）。

- [ ] **Step 1: 写失败测试（追加到 `server/tests/test_viral_search_pg.py`）**

先在文件头 import 区追加（`from datetime import UTC, datetime` 之后）：

```python
from types import SimpleNamespace
```

再在文件末尾追加：

```python
class _LinkResolveStub:
    """抖音直链解析假客户端：记录 (url, purpose)，返回固定直链."""

    def __init__(self, *, video_url: str = "https://cdn.example/douyin.mp4") -> None:
        self.video_url = video_url
        self.calls: list[tuple[str, str]] = []

    def resolve(self, raw_url: str, *, purpose: str) -> SimpleNamespace:
        self.calls.append((raw_url, purpose))
        return SimpleNamespace(video_url=self.video_url)


def test_refresh_douyin_returns_default_quality_url(search_client) -> None:
    """抖音：videoId → 规范 URL → 直链；非法 ID 不外呼 422；未配置 503."""
    from app.viral_search_routes import get_viral_search_link_resolver

    stub = _LinkResolveStub()
    search_client.app.dependency_overrides[get_viral_search_link_resolver] = lambda: stub
    headers, _ = account(search_client, "refresh_douyin")
    response = search_client.post(
        "/api/viral/search/refresh",
        headers=headers,
        json={"platform": "douyin", "videoId": "7672703482771972081"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"url": "https://cdn.example/douyin.mp4", "decodeKey": None}
    assert stub.calls == [
        ("https://www.douyin.com/jingxuan?modal_id=7672703482771972081", "copy")
    ]
    bad = search_client.post(
        "/api/viral/search/refresh",
        headers=headers,
        json={"platform": "douyin", "videoId": "not-a-number"},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "VIRAL_SEARCH_REFRESH_ID_INVALID"
    assert len(stub.calls) == 1  # 非法 ID 不外呼
    search_client.app.dependency_overrides[get_viral_search_link_resolver] = lambda: None
    unavailable = search_client.post(
        "/api/viral/search/refresh",
        headers=headers,
        json={"platform": "douyin", "videoId": "7672703482771972081"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "VIRAL_SEARCH_REFRESH_UNAVAILABLE"


def test_refresh_wechat_returns_url_and_decode_key(search_client, route_state) -> None:
    """视频号：native 凭据 → full_url + decode_key；缺凭据/缺视频 fail-closed."""
    from app.viral_store import upsert_viral_videos
    from app.viral_tikhub import PLATFORM_WECHAT, ViralVideo, WechatVideoDetail

    class _DetailStub:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def wechat_video_detail(self, *, export_id="", object_nonce_id=None, object_id=None):
            self.calls.append({"export_id": export_id, "object_nonce_id": object_nonce_id})
            return WechatVideoDetail(
                object_id="obj-1",
                object_nonce_id=None,
                title="视频号样本",
                description=None,
                nickname="作者",
                username=None,
                create_time=None,
                like_count=None,
                fav_count=None,
                forward_count=None,
                comment_count=None,
                city=None,
                full_url="https://cdn.example/wx.mp4",
                decode_key="dk-1",
                cover_url=None,
                duration_ms=None,
                width=None,
                height=None,
            )

    def _wechat_video(video_id: str, native: dict) -> ViralVideo:
        return ViralVideo(
            platform=PLATFORM_WECHAT,
            video_id=video_id,
            category="",
            title="视频号样本",
            author="作者",
            author_avatar=None,
            verified=False,
            cover_url=None,
            duration_ms=0,
            likes=0,
            comments=None,
            shares=None,
            collects=None,
            published_at=None,
            published_display=None,
            like_display=None,
            native=native,
        )

    stub = _DetailStub()
    _use_search_stub(search_client, stub)
    headers, _ = account(search_client, "refresh_wechat")
    with psycopg.connect(route_state) as raw:
        upsert_viral_videos(
            BusinessConnection.postgres(raw),
            [
                _wechat_video(
                    "doc-refresh-1", {"export_id": "exp-1", "object_nonce_id": "nonce-1"}
                ),
                _wechat_video("doc-refresh-2", {}),
            ],
            commit=False,
        )
        raw.commit()
    response = search_client.post(
        "/api/viral/search/refresh",
        headers=headers,
        json={"platform": "wechat_channels", "videoId": "doc-refresh-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"url": "https://cdn.example/wx.mp4", "decodeKey": "dk-1"}
    assert stub.calls == [{"export_id": "exp-1", "object_nonce_id": "nonce-1"}]
    undecodable = search_client.post(
        "/api/viral/search/refresh",
        headers=headers,
        json={"platform": "wechat_channels", "videoId": "doc-refresh-2"},
    )
    assert undecodable.status_code == 422
    assert undecodable.json()["detail"]["code"] == "VIRAL_SEARCH_REFRESH_NOT_DECODABLE"
    missing = search_client.post(
        "/api/viral/search/refresh",
        headers=headers,
        json={"platform": "wechat_channels", "videoId": "doc-not-in-pool"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "VIRAL_SEARCH_REFRESH_NOT_FOUND"
    _use_search_stub(search_client, None)
    unavailable = search_client.post(
        "/api/viral/search/refresh",
        headers=headers,
        json={"platform": "wechat_channels", "videoId": "doc-refresh-1"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "VIRAL_SEARCH_UNAVAILABLE"
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "refresh" -v
```

Expected: FAIL —— `ImportError: cannot import name 'get_viral_search_link_resolver' from 'app.viral_search_routes'`（Task 7 的文件里还没有这个依赖）。

- [ ] **Step 3: 实现（两处）**

**（a）** 改 `server/app/viral_search_routes.py` 的 import 区——把 Task 7 写入的整个 import 块替换为：

```python
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.billing_catalog import SERVICES
from app.billing_meter import billing_context
from app.customer_fence import BusinessDbDep
from app.media_routes import api_base_url, get_media_storage
from app.permissions import require_not_auditor
from app.script_from_audio import cached_transcripts
from app.usage_billing import finish_source
from app.viral_link import (
    DOUYIN_CANONICAL_URL_TEMPLATE,
    DouyidouLinkClient,
    ViralLinkError,
    douyidou_link_client_from_settings,
)
from app.viral_routes import ViralVideoItem
from app.viral_search import (
    archive_search_covers,
    persist_viral_search,
    reserve_search_operation,
    run_viral_search,
    search_date_shanghai,
)
from app.viral_store import get_viral_video
from app.viral_tikhub import (
    PLATFORM_DOUYIN,
    PLATFORM_WECHAT,
    ViralSourceClient,
    ViralSourceError,
    ViralSourceUnavailable,
    ViralVideo,
    viral_source_client_from_settings,
)
```

**（b）** 在 `server/app/viral_search_routes.py` 文件末尾（`search_viral_videos` 之后）追加：

```python
class ViralRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"] = "douyin"
    videoId: str = Field(min_length=1, max_length=512)


class ViralRefreshResponse(BaseModel):
    url: str
    decodeKey: str | None = None


def get_viral_search_link_resolver(conn: Database) -> DouyidouLinkClient | None:
    """抖音直链解析客户端依赖（未配置时 None，路由回 503；测试可覆写）."""
    try:
        return douyidou_link_client_from_settings(conn)
    except ViralLinkError:
        return None


ViralSearchLinkResolverDep = Annotated[
    DouyidouLinkClient | None, Depends(get_viral_search_link_resolver)
]


@router.post("/search/refresh", response_model=ViralRefreshResponse)
def refresh_viral_search_video(
    payload: ViralRefreshRequest,
    conn: Database,
    _actor: AuthenticatedUser,
    client: ViralSearchSourceClientDep,
    resolver: ViralSearchLinkResolverDep,
) -> ViralRefreshResponse:
    """把内容池视频换成"本地缓存"所需的一次性凭据（客户侧零费用）.

    - 抖音：``resolve(规范 URL, purpose="copy")`` 取默认清晰度直链。
    - 视频号：native 里的 ``export_id``/``object_nonce_id`` 换 ``full_url`` +
      ``decode_key``（客户端本地解密）。
    - 与既有 viral 路由一致：不设审计员守卫（`_actor` 仅强制登录）；
      供应商成本随平台操作计费（`viral_tikhub._request` 内 meter_call）。
    """
    if payload.platform == PLATFORM_DOUYIN:
        if not re.fullmatch(r"\d{15,22}", payload.videoId):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VIRAL_SEARCH_REFRESH_ID_INVALID",
                    "message": "抖音视频标识无效。",
                },
            )
        if resolver is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "VIRAL_SEARCH_REFRESH_UNAVAILABLE",
                    "message": "视频链接解析服务暂不可用，请联系管理员。",
                },
            )
        try:
            resolved = resolver.resolve(
                DOUYIN_CANONICAL_URL_TEMPLATE.format(video_id=payload.videoId),
                purpose="copy",
            )
        except ViralLinkError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        return ViralRefreshResponse(url=resolved.video_url, decodeKey=None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_SEARCH_UNAVAILABLE",
                "message": "爆款视频数据源暂不可用，请联系管理员。",
            },
        )
    stored = get_viral_video(conn, platform=PLATFORM_WECHAT, video_id=payload.videoId)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "VIRAL_SEARCH_REFRESH_NOT_FOUND",
                "message": "视频不在内容池中，请先搜索该视频。",
            },
        )
    export_id = str(stored.native.get("export_id") or "").strip()
    if not export_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_REFRESH_NOT_DECODABLE",
                "message": "该视频暂不支持本地缓存，请更换视频。",
            },
        )
    nonce = str(stored.native.get("object_nonce_id") or "").strip()
    try:
        detail = client.wechat_video_detail(
            export_id=export_id, object_nonce_id=nonce or None
        )
    except ViralSourceError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_SEARCH_REFRESH_UPSTREAM_FAILED",
                "message": str(exc),
            },
        ) from exc
    if not detail.full_url:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_REFRESH_NOT_DECODABLE",
                "message": "该视频暂不支持本地缓存，请更换视频。",
            },
        )
    return ViralRefreshResponse(url=detail.full_url, decodeKey=detail.decode_key)
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "refresh" -v
```

Expected: `2 passed`（`test_refresh_douyin_returns_default_quality_url`、`test_refresh_wechat_returns_url_and_decode_key`）。

- [ ] **Step 5: 回归（本文件全量 + 链接解析）**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py tests/test_viral_link_media.py -q
```

Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```powershell
git add server/app/viral_search_routes.py server/tests/test_viral_search_pg.py
git commit -m "feat(viral): 视频直链刷新接口 POST /api/viral/search/refresh（P1）

Co-authored-by: peihr666-max"
```

---
## Task 9: 文案与发现读接口（`GET /api/viral/search/copy` + `GET /api/viral/search/discoveries`）

**Files:**
- Modify: `server/app/viral_search_routes.py`（追加两个 GET 路由）
- Test: `server/tests/test_viral_search_pg.py`（追加）

> 背景：客户端有两个"先问后做"的需求——①提取文案前先问服务端有没有共享文案
> （命中则本地瞬间填充，不触发下载/上传/ASR）；②"我的发现"按天回看搜索结果。
> 两者都是**纯读路径**：不写库、不计费、不设审计员守卫（与既有 viral 读路由一致）。

- [ ] **Step 1: 写失败测试（追加到 `server/tests/test_viral_search_pg.py`）**

```python
def test_copy_returns_cached_transcript_and_null_on_miss(search_client, route_state) -> None:
    """GET /search/copy：命中返回 text+updatedAt（只读、免费），未命中双 null."""
    headers, _ = account(search_client, "copy_read")
    payload = json.dumps({"text": "已缓存文案", "duration_sec": 12.0}, ensure_ascii=False)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO viral_script_cache (platform, video_id, result_json) "
            "VALUES ('douyin', 'v-copy-1', %s) ON CONFLICT (platform, video_id) "
            "DO UPDATE SET result_json=excluded.result_json",
            (payload,),
        )
    hit = search_client.get(
        "/api/viral/search/copy",
        headers=headers,
        params={"platform": "douyin", "videoId": "v-copy-1"},
    )
    assert hit.status_code == 200, hit.text
    assert hit.json()["text"] == "已缓存文案"
    assert hit.json()["updatedAt"]
    miss = search_client.get(
        "/api/viral/search/copy",
        headers=headers,
        params={"platform": "douyin", "videoId": "v-copy-absent"},
    )
    assert miss.status_code == 200
    assert miss.json() == {"text": None, "updatedAt": None}


def test_discoveries_list_today_and_validate_date(search_client, route_state) -> None:
    """GET /search/discoveries：默认当天（上海时区），左联内容池；坏日期 422."""
    from app.viral_search import search_date_shanghai
    from app.viral_store import upsert_viral_videos
    from app.viral_tikhub import PLATFORM_WECHAT, ViralVideo

    headers, uid = account(search_client, "discoveries_read")
    today = search_date_shanghai()
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_search_discoveries WHERE user_id=%s", (uid,))
        upsert_viral_videos(
            BusinessConnection.postgres(raw),
            [
                ViralVideo(
                    platform=PLATFORM_WECHAT,
                    video_id="doc-disc-1",
                    category="",
                    title="视频号样本",
                    author="作者",
                    author_avatar=None,
                    verified=False,
                    cover_url=None,
                    duration_ms=0,
                    likes=0,
                    comments=None,
                    shares=None,
                    collects=None,
                    published_at=None,
                    published_display=None,
                    like_display=None,
                    native={"export_id": "exp-1", "object_nonce_id": "nonce-1"},
                )
            ],
            commit=False,
        )
        raw.execute(
            "INSERT INTO viral_search_discoveries "
            "(id, user_id, keyword, platform, video_id, search_date, searched_at) VALUES "
            "('disc-1', %s, '农村建房', 'wechat_channels', 'doc-disc-1', %s, "
            "'2026-09-22T02:00:00+00:00'), "
            "('disc-2', %s, '农村建房', 'douyin', 'v-not-stored', %s, "
            "'2026-09-22T01:00:00+00:00')",
            (uid, today, uid, today),
        )
    response = search_client.get("/api/viral/search/discoveries", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["date"] == today
    assert body["total"] == 2
    assert [item["videoId"] for item in body["items"]] == ["doc-disc-1", "v-not-stored"]
    assert body["items"][0]["video"]["title"] == "视频号样本"
    assert body["items"][0]["video"]["hasCopy"] is False
    assert body["items"][1]["video"] is None
    empty = search_client.get(
        "/api/viral/search/discoveries", headers=headers, params={"date": "2000-01-01"}
    )
    assert empty.status_code == 200
    assert empty.json() == {"date": "2000-01-01", "total": 0, "items": []}
    bad = search_client.get(
        "/api/viral/search/discoveries", headers=headers, params={"date": "2026/09/22"}
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "VIRAL_SEARCH_DATE_INVALID"
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "copy_returns or discoveries_list" -v
```

Expected: FAIL —— 两个用例都是 `AssertionError`（`response.status_code == 404`，路由还不存在）。

- [ ] **Step 3: 实现（两处）**

**（a）** 改 `server/app/viral_search_routes.py` import 区三行：

```python
from fastapi import APIRouter, Depends, Header, HTTPException, Query
```

```python
from app.script_from_audio import cached_transcript, cached_transcripts
```

```python
from app.viral_store import get_viral_video, list_viral_discoveries
```

**（b）** 在 `server/app/viral_search_routes.py` 文件末尾（`refresh_viral_search_video` 之后）追加：

```python
class ViralCopyResponse(BaseModel):
    text: str | None
    updatedAt: str | None


class ViralDiscoveryItem(BaseModel):
    platform: str
    videoId: str
    keyword: str
    searchedAt: str
    video: ViralVideoItem | None


class ViralDiscoveriesResponse(BaseModel):
    date: str
    total: int
    items: list[ViralDiscoveryItem]


@router.get("/search/copy", response_model=ViralCopyResponse)
def get_viral_search_copy(
    conn: Database,
    _actor: AuthenticatedUser,
    videoId: Annotated[str, Query(min_length=1, max_length=512)],
    platform: Literal["douyin", "wechat_channels"] = "douyin",
) -> ViralCopyResponse:
    """读共享文案缓存：命中即刻回填，未命中双 null（客户端再决定是否提取）.

    纯读路径：不触发下载/上传/ASR，不计费（`viral_script_cache` 跨用户共享）。
    """
    hit = cached_transcript(conn, platform=platform, video_id=videoId)
    if hit is None:
        return ViralCopyResponse(text=None, updatedAt=None)
    return ViralCopyResponse(text=hit.result.text, updatedAt=hit.updated_at)


@router.get("/search/discoveries", response_model=ViralDiscoveriesResponse)
def list_viral_search_discoveries(
    conn: Database,
    actor: AuthenticatedUser,
    search_date: Annotated[str | None, Query(alias="date", max_length=10)] = None,
) -> ViralDiscoveriesResponse:
    """客户"我的发现"（按天，默认今天·上海时区）：发现记录左联内容池."""
    resolved_date = (search_date or "").strip() or search_date_shanghai()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved_date):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_DATE_INVALID",
                "message": "日期格式应为 YYYY-MM-DD。",
            },
        )
    discoveries = list_viral_discoveries(conn, user_id=actor.id, search_date=resolved_date)
    hits = cached_transcripts(
        conn,
        [(item.platform, item.video_id) for item in discoveries if item.video is not None],
    )
    return ViralDiscoveriesResponse(
        date=resolved_date,
        total=len(discoveries),
        items=[
            ViralDiscoveryItem(
                platform=item.platform,
                videoId=item.video_id,
                keyword=item.keyword,
                searchedAt=item.searched_at,
                video=(
                    _search_item(item.video, has_copy=(item.platform, item.video_id) in hits)
                    if item.video is not None
                    else None
                ),
            )
            for item in discoveries
        ],
    )
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -k "copy_returns or discoveries_list" -v
```

Expected: `2 passed`。

- [ ] **Step 5: 回归（本文件全量）**

```powershell
uv run python -m pytest tests/test_viral_search_pg.py -q
```

Expected: 全部 PASS（含 Task 1-8 的全部用例）。

- [ ] **Step 6: Commit**

```powershell
git add server/app/viral_search_routes.py server/tests/test_viral_search_pg.py
git commit -m "feat(viral): 共享文案读取与我的发现接口（P1）

Co-authored-by: peihr666-max"
```

---
## Task 10: 运营侧发现汇总 `GET /api/control/viral/discoveries`

**Files:**
- Modify: `server/app/admin_runtime_routes.py`（import 区两行 + 在 `read_collected_viral_videos` 之后插入新路由）
- Test: `server/tests/test_admin_session_routes.py`（追加 1 个用例 + `route_state` TRUNCATE 增补一表）

> 背景：设计 §5.2 的运营视图要求"每日汇总（关键词热度、内容池增量、推荐位候选来源）"。
> 沿用现有 admin 模式——`admin_runtime_routes.py` 的现网前缀是 `/api/control`
> （既有先例 `GET /api/control/viral/videos`），`AdminReader` 只读、`pg_transaction()`
> 取连接。按 `(keyword, platform)` 分组统计、热度降序；明细可由客户侧
> `GET /api/viral/search/discoveries` 与内容池 `GET /api/control/viral/videos` 交叉查看，
> 本接口不做明细分页（YAGNI）。`date` 为**必填**（运营汇总的完整性以显式日期为准）。

- [ ] **Step 1: 写失败测试（两处改动都在 `server/tests/test_admin_session_routes.py`）**

**（a）** `route_state` fixture 的 TRUNCATE 列表（L243-256）在 `"viral_videos, "` 之后加一行：

```python
            "viral_search_discoveries, "
```

**（b）** 文件末尾追加：

```python
@pytest.mark.pg
def test_admin_viral_discoveries_daily_summary(client: TestClient, route_state: str) -> None:
    """运营侧每日汇总：关键词热度分组 + 客户/视频去重 + 日期校验 + 鉴权."""
    headers = _admin_session(client)
    with psycopg.connect(route_state) as conn:
        conn.execute(
            "INSERT INTO viral_search_discoveries "
            "(id,user_id,keyword,platform,video_id,search_date,searched_at) VALUES "
            "('d-1','customer_u','农村建房','douyin','admin-video/opaque=id','2026-09-22',"
            "'2026-09-22T01:00:00+00:00'),"
            "('d-2','admin_u','农村建房','douyin','admin-video/opaque=id','2026-09-22',"
            "'2026-09-22T02:00:00+00:00'),"
            "('d-3','customer_u','自建房','wechat_channels','wx-1','2026-09-22',"
            "'2026-09-22T03:00:00+00:00'),"
            "('d-4','customer_u','自建房','wechat_channels','wx-2','2026-09-21',"
            "'2026-09-21T03:00:00+00:00')"
        )
    path = "/api/control/viral/discoveries"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=headers, params={"date": "2026-09-22"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "date": "2026-09-22",
        "total": 3,
        "users": 2,
        "videos": 2,
        "keywords": [
            {
                "keyword": "农村建房",
                "platform": "douyin",
                "discoveries": 2,
                "users": 2,
                "videos": 1,
            },
            {
                "keyword": "自建房",
                "platform": "wechat_channels",
                "discoveries": 1,
                "users": 1,
                "videos": 1,
            },
        ],
    }
    missing = client.get(path, headers=headers, params={"date": "2026-01-01"})
    assert missing.status_code == 200
    assert missing.json()["total"] == 0
    assert missing.json()["keywords"] == []
    bad = client.get(path, headers=headers, params={"date": "2026/09/22"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "VIRAL_SEARCH_DATE_INVALID"
    assert client.get(path, headers=headers).status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

```powershell
$env:TEST_POSTGRESQL_URL = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921\server
uv run python -m pytest tests/test_admin_session_routes.py -k daily_summary -v
```

Expected: FAIL —— `client.get(path).status_code` 是 404（路由不存在），断言 `404 == 401` 失败；即使跳过该行，`response.status_code` 也是 404。

- [ ] **Step 3: 实现（两处改动都在 `server/app/admin_runtime_routes.py`）**

**（a）** import 区（L22-27）两行改为：

```python
import json
import re
import uuid
```

```python
from fastapi import APIRouter, HTTPException, Query, Request, Response
```

**（b）** 在 `read_collected_viral_videos` 的结尾
（`return {"items": items, "total": int(total), "offset": offset, "limit": limit}`）
之后、`@router.post("/viral/videos/{platform}/{video_id:path}/archive", status_code=202)`
之前插入：

```python
@router.get("/viral/discoveries")
def read_viral_search_discoveries(
    _actor: AdminReader,
    search_date: Annotated[str, Query(alias="date", max_length=10)],
) -> dict[str, object]:
    """搜索发现每日汇总（运营视图）：按关键词×平台的热度分组.

    一行 = 一个 (keyword, platform) 分组；`users`/`videos` 为去重覆盖数。
    明细（谁在什么时候搜到什么）由客户侧 `GET /api/viral/search/discoveries`
    与内容池 `GET /api/control/viral/videos` 交叉查看。
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", search_date):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_DATE_INVALID",
                "message": "日期格式应为 YYYY-MM-DD。",
            },
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        totals = conn.execute(
            "SELECT count(*),count(DISTINCT user_id),"
            "count(DISTINCT platform || '/' || video_id) "
            "FROM viral_search_discoveries WHERE search_date=%s",
            (search_date,),
        ).fetchone()
        rows = conn.execute(
            "SELECT keyword,platform,count(*) AS discoveries,"
            "count(DISTINCT user_id) AS users,count(DISTINCT video_id) AS videos "
            "FROM viral_search_discoveries WHERE search_date=%s "
            "GROUP BY keyword,platform "
            "ORDER BY discoveries DESC,keyword,platform LIMIT 100",
            (search_date,),
        ).fetchall()
    return {
        "date": search_date,
        "total": int(totals[0]),
        "users": int(totals[1]),
        "videos": int(totals[2]),
        "keywords": [dict(row) for row in rows],
    }
```

- [ ] **Step 4: 跑测试确认通过**

```powershell
uv run python -m pytest tests/test_admin_session_routes.py -k daily_summary -v
```

Expected: `1 passed`。

- [ ] **Step 5: 回归（本文件全量——`route_state` fixture 有改动，验证其余用例不受影响）**

```powershell
uv run python -m pytest tests/test_admin_session_routes.py -q
```

Expected: 全部 PASS（本文件既有用例 + 新用例）。

- [ ] **Step 6: Commit**

```powershell
git add server/app/admin_runtime_routes.py server/tests/test_admin_session_routes.py
git commit -m "feat(viral): 运营侧搜索发现每日汇总接口（P1）

Co-authored-by: peihr666-max"
```

---
## Task 11: 停用采集调度与采集计费触发路径

**Files:**
- Modify: `server/app/viral_refresh.py`（`acquire_viral_refresh_task` 删除自动入队）
- Modify: `server/app/generation_worker.py`（`run_pg_collection_once` 移除结算 + `--viral-collection` help 文案）
- Modify: `server/tests/test_usage_billing.py`（改造 1 个 + 追加 2 个守卫用例）
- Modify: `server/tests/test_cw058_content_asset_pg_matrix.py`（4 处显式入队适配）

> 背景：设计 §5.5 "`viral_collection.py` 每日/每周采集调度 → **停用**（调度注册移除）"、
> "`viral_collection_billing.py` 采集计费 → **停用**（不再有采集消费）"；§12 "采集计费确无触发路径"。
>
> **停用原则 = 只摘调用、不删函数**。两个"发动机"调用被摘除：
> ① `acquire_viral_refresh_task` 内的 `enqueue_due_viral_collections`（不再自动产生采集任务）；
> ② `run_pg_collection_once` 内的 `settle_collection_charges`（不再产生客户采集扣费）。
> `enqueue_due_viral_collections` / `settle_collection_charges` / `reconcile_collection_requests`
> / `create_collection_batch` / `eligible_collection_users` **全部保留**——函数级测试
> （`test_usage_billing.py` 的 `test_collection_meter_*` / `test_expired_collection_call_*` /
> `test_activation_suspension_*` / `test_shared_collection_reports_*`、`test_cw043_viral_import_pg.py`
> 的显式入队用例）继续通过；admin 单条归档（`single_archive`）任务继续走同一消费通道。
>
> 停用后 collector 进程（`--viral-collection`）的语义：只消费**显式入队**的任务
> （admin 归档 / 失败重试），空轮返回 0；`collection_enabled` 开关保留
> （admin 归档通道的暂停开关，既有 409 `VIRAL_COLLECTION_PAUSED` 语义不变）。

- [ ] **Step 1: 改测试（先红）**

**（a）** `server/tests/test_usage_billing.py`：把 `test_scheduled_collector_wires_batch_meter_and_settlement`（L438-490）整段替换为：

```python
def test_scheduled_collector_records_platform_cost_without_charging_customers(
    client, route_state, monkeypatch
):
    """P1 停用采集计费后的 collector 语义：显式入队的任务照常消费、供应商成本
    照记平台单（user_id IS NULL），但不再产生任何客户采集扣费."""
    from app.billing_meter import meter_call
    from app.generation_worker import run_pg_collection_once
    from app.storage import FakeStorageAdapter
    from app.viral_collection import enqueue_due_viral_collections

    user = account(client, "scheduled_collection")[1]
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) VALUES "
            "('viral_data',true,2,0.5)"
        )
        credit_lot(raw, user, key="scheduled-funds", credits=100, amount_fen=100)
        raw.execute(
            "INSERT INTO viral_runtime_controls(id,collection_enabled,keywords_json) "
            "VALUES(1,1,%s) ON CONFLICT(id) DO UPDATE SET "
            "collection_enabled=1,keywords_json=excluded.keywords_json,next_collection_at=NULL",
            (json.dumps([{"platform": "douyin", "category": "其他", "keyword": "别墅"}]),),
        )
        raw.execute("DELETE FROM viral_refresh_tasks")
        enqueue_due_viral_collections(BusinessConnection.postgres(raw))

    class Source:
        def douyin_search(self, **_kwargs):
            with meter_call("viral_data"):
                return []

    monkeypatch.setattr(
        "app.viral_collection.viral_source_client_from_settings", lambda _: Source()
    )
    assert (
        run_pg_collection_once(
            worker_id="billing-integration",
            storage=FakeStorageAdapter(provider="cos", bucket="test"),
        )
        == 1
    )
    with psycopg.connect(route_state) as raw:
        assert raw.execute("SELECT status FROM viral_refresh_tasks").fetchone()[0] == "SUCCEEDED"
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id=%s", (user,)
            ).fetchone()[0]
            == 100
        )
        config = json.loads(
            raw.execute("SELECT collection_config_json FROM viral_refresh_tasks").fetchone()[0]
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE collection_batch_id=%s "
                "AND user_id IS NULL AND state='SUCCEEDED'",
                (config["billing_batch_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE collection_batch_id=%s "
                "AND user_id IS NOT NULL",
                (config["billing_batch_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_collection_charges c "
                "JOIN billing_operations p ON p.id=c.request_id "
                "WHERE p.collection_batch_id=%s",
                (config["billing_batch_id"],),
            ).fetchone()[0]
            == 0
        )
```

**（b）** 同文件，紧跟其后追加两个守卫用例：

```python
def test_acquire_no_longer_enqueues_scheduled_collection(client, route_state):
    """P1 停用采集调度：即使运行控制开着、关键词已到期，acquire 也不再自动入队
    （采集任务只能被显式入队——admin 单条归档 / 失败重试）."""
    from app.viral_refresh import acquire_viral_refresh_task

    account(client, "no_auto_enqueue")
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_refresh_tasks")
        rows_before = raw.execute("SELECT count(*) FROM viral_collection_batches").fetchone()[0]
        raw.execute(
            "INSERT INTO viral_runtime_controls(id,collection_enabled,keywords_json,"
            "next_collection_at) VALUES(1,1,%s,NULL) ON CONFLICT(id) DO UPDATE SET "
            "collection_enabled=1,keywords_json=excluded.keywords_json,next_collection_at=NULL",
            (json.dumps([{"platform": "douyin", "category": "其他", "keyword": "别墅"}]),),
        )
        conn = BusinessConnection.postgres(raw)
        assert acquire_viral_refresh_task(conn, worker_id="disabled-schedule") is None
        assert raw.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone()[0] == 0
        assert (
            raw.execute("SELECT count(*) FROM viral_collection_batches").fetchone()[0]
            == rows_before
        )


def test_collector_does_not_settle_legacy_confirmed_collection_requests(client, route_state):
    """P1 停用采集计费：即使库里存留"已确认未结算"的历史平台请求，collector
    空轮也不再触发任何客户结算（settle 触发路径已摘除）."""
    from app.generation_worker import run_pg_collection_once
    from app.storage import FakeStorageAdapter
    from app.viral_collection_billing import create_collection_batch

    user = account(client, "legacy_settlement")[1]
    with psycopg.connect(route_state) as raw:
        credit_lot(
            raw,
            user,
            key="legacy-settlement-funds",
            credits=100,
            amount_fen=100,
            provider="admin_adjustment",
        )
        raw.execute(
            "INSERT INTO "
            "activation_code_batches(id,name,face_value_fen,unit_price_fen_snapshot,"
            "credits_snapshot,quantity,activation_expires_at,status,created_by_user_id) "
            "VALUES('legacy-code-batch','test',1000,10,100,1,'2099-01-01','OPEN',%s)",
            (user,),
        )
        raw.execute(
            "INSERT INTO "
            "activation_codes(id,batch_id,code_digest,digest_key_version,masked_code,status,"
            "issued_at,bound_user_id,activated_at) "
            "VALUES('legacy-code','legacy-code-batch','legacy-digest',1,'TEST-****','ACTIVE',"
            "'2026-01-01',%s,'2026-01-01')",
            (user,),
        )
        raw.execute(
            "INSERT INTO activation_code_activations(id,code_id,user_id,first_device_id,"
            "recharge_order_id) "
            "VALUES('legacy-binding','legacy-code',%s,NULL,'legacy-settlement-funds')",
            (user,),
        )
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES('viral_data',true,2,0.5)"
        )
        raw.execute("DELETE FROM viral_refresh_tasks")
        batch = create_collection_batch(
            BusinessConnection.postgres(raw),
            platform="douyin",
            config={"keywords": ["别墅"]},
            user_ids=[user],
        )
        raw.execute(
            "INSERT INTO billing_operations(id,service,module,source_id,unit,budget_units,"
            "pricing_snapshot_json,collection_batch_id,state,actual_units) "
            "VALUES('legacy-request','viral_data','viral','legacy-request','call',1,'{}',%s,"
            "'SUCCEEDED',1)",
            (batch,),
        )
    assert (
        run_pg_collection_once(
            worker_id="legacy-settle-check",
            storage=FakeStorageAdapter(provider="cos", bucket="test"),
        )
        == 0
    )
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_collection_charges c "
                "JOIN billing_operations p ON p.id=c.request_id "
                "WHERE p.collection_batch_id=%s",
                (batch,),
            ).fetchone()[0]
            == 0
        )
        assert (
            raw.execute(
                "SELECT available_credits FROM wallets WHERE user_id=%s", (user,)
            ).fetchone()[0]
            == 100
        )
```

**（c）** `server/tests/test_cw058_content_asset_pg_matrix.py` 四处适配（4 个测试原本依赖 acquire 内的自动入队，现在显式入队）：

1）`test_weekly_collection_failure_keeps_published_snapshot_and_resumes_checkpoints`（L2894 起）：

- L2899 附近的 import 块加一行：

```python
    from app.generation_worker import run_pg_collection_once
    from app.viral_collection import enqueue_due_viral_collections
    from app.viral_tikhub import ViralSourceError
```

- 在 `storage = FakeStorageAdapter(provider="fake", bucket="weekly")` 之前加：

```python
    enqueue_due_viral_collections(bus)
```

- 第二次消费前（`UPDATE viral_media_preparations ...` 那条 `pg.execute` 之后、
  `assert run_pg_collection_once(worker_id="collector", storage=storage) == 1` 之前）再加一次：

```python
    enqueue_due_viral_collections(bus)
```

2）`test_weekly_wechat_cached_media_refreshes_statistics_but_pause_prevents_details`（L3035 起）：

- L3044 的 import 块加一行：

```python
    from app.generation_worker import run_pg_collection_once
    from app.viral_collection import enqueue_due_viral_collections
```

- 在 `assert run_pg_collection_once(worker_id="collector", storage=storage) == 1` 之前加：

```python
    enqueue_due_viral_collections(BusinessConnection.postgres(pg))
```

3）`test_viral_list_reads_only_and_weekly_worker_prepares_cloud_media_on_pg`（L3252 起）：

- L3261 的 import 块加一行：

```python
    from app.generation_worker import run_pg_collection_once, run_pg_worker_once
    from app.viral_collection import enqueue_due_viral_collections
```

- 在 `pg.commit()`（设置 keywords 之后）与 `processed = run_pg_collection_once(` 之间加：

```python
    enqueue_due_viral_collections(BusinessConnection.postgres(pg))
```

- [ ] **Step 2: 跑测试确认失败（红：触发路径仍在）**

```powershell
$env:TEST_POSTGRESQL_URL = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921\server
uv run python -m pytest tests/test_usage_billing.py -k "scheduled_collector or no_longer_enqueues or does_not_settle" -v
```

Expected: 3 FAIL ——
`scheduled_collector`：`run_pg_collection_once(...)` 返回 2（`1 + settle`）且钱包被扣到 98；
`no_longer_enqueues`：`viral_refresh_tasks` 非空（acquire 仍自动入队）；
`does_not_settle`：返回 1（settle 结算了遗留请求）且落了一条 charge 行。

- [ ] **Step 3: 实现（三处改动）**

**（a）** `server/app/viral_refresh.py`：`acquire_viral_refresh_task` 内删除自动入队（L118-120 三行），

```python
    from app.viral_collection import enqueue_due_viral_collections

    enqueue_due_viral_collections(conn)
```

删除后函数开头直接进入开关检查：

```python
def acquire_viral_refresh_task(
    conn: BusinessConnection, *, worker_id: str
) -> ViralRefreshLease | None:
    enabled = conn.execute(
        "SELECT collection_enabled FROM viral_runtime_controls WHERE id=1"
    ).fetchone()
```

**（b）** `server/app/generation_worker.py`：`run_pg_collection_once`（L1762-1772）整段替换为：

```python
def run_pg_collection_once(*, worker_id: str, storage: StorageAdapter) -> int:
    """Dedicated collector: never run in the customer generation worker pool.

    P1 起采集调度与采集计费停用：本进程只消费显式入队的任务
    （admin 单条归档 / 失败重试），不再自动入队、不再触发客户采集扣费
    （设计 §5.5 / §12）。供应商成本仍由 meter_call 记平台单。
    """
    with pg_transaction() as raw:
        lease = acquire_viral_refresh_task(BusinessConnection.postgres(raw), worker_id=worker_id)
    if lease is None:
        return 0
    _run_pg_viral_refresh(lease, storage)
    return 1
```

**（c）** 同文件 CLI help 文案（L1920-1923）：

```python
    parser.add_argument(
        "--viral-collection",
        action="store_true",
        help="run only explicitly queued viral archive tasks (scheduled collection is disabled)",
    )
```

> 不要删除：`viral_collection.enqueue_due_viral_collections`、`viral_collection_billing.settle_collection_charges`
> / `reconcile_collection_requests` / `create_collection_batch` / `eligible_collection_users`、
> `--viral-collection` CLI 参数、前端 `package.json` 的 `dev:viral-collection` script
> （`test_build_contracts.py` 断言其存在）、`viral_fetch_state` 表与 admin 状态显示。

- [ ] **Step 4: 跑测试确认通过（绿）**

```powershell
uv run python -m pytest tests/test_usage_billing.py -k "scheduled_collector or no_longer_enqueues or does_not_settle" -v
```

Expected: `3 passed`。

```powershell
uv run python -m pytest tests/test_cw058_content_asset_pg_matrix.py -k "weekly_collection_failure or weekly_wechat_cached or viral_list_reads_only" -v
```

Expected: `6 passed`（failure 用例 3 个参数化 + wechat 用例 2 个参数化 + viral_list 1 个）。

- [ ] **Step 5: 回归（停用波及面）**

```powershell
uv run python -m pytest tests/test_usage_billing.py tests/test_cw043_viral_import_pg.py tests/test_admin_session_routes.py -q
```

Expected: 全部 PASS —— 函数级采集计费用例（直接调 `settle_collection_charges`）不受影响；
cw043 显式入队用例不受影响；admin 单条归档消费路径不受影响（该用例 `keywords_json='[]'`，
自动入队本为 no-op）。

- [ ] **Step 6: Commit**

```powershell
git add server/app/viral_refresh.py server/app/generation_worker.py server/tests/test_usage_billing.py server/tests/test_cw058_content_asset_pg_matrix.py
git commit -m "feat(viral): 停用采集调度与采集计费触发路径（P1）

Co-authored-by: peihr666-max"
```

---
## Task 12: 结算补偿覆盖搜索预留（`reconcile_operations` sweep）

**Files:**
- Modify: `server/app/usage_billing.py`（`reconcile_operations` 增 `viral_search` 30 分钟 sweep）
- Test: `server/tests/test_usage_billing.py`（追加）

> 背景：搜索计费是"先预留（PENDING）→ 与落库同事务结算"（Task 6 的 `reserve_search_operation` + `persist_viral_search`）。
> 进程若在预留之后、页面事务提交之前崩溃，会留下一条永不结算的 PENDING 预留，客户 1 点预算被永久冻结。
> `reconcile_operations` 已有同类机制（`viral_data` 采集 30 分钟超时按 `actual_units` 结清），本任务补搜索分支：
> 超过 30 分钟仍 PENDING 的搜索预留按"未交付"释放（FAILED，units=0）。
>
> 为什么是 FAILED 而不是 SUCCEEDED + units=0：① 搜索交付是原子的（预留与落库同事务），PENDING 残留必为未交付；
> ② FAILED 让 `reserve_search_operation` 在重试时开新轮次（Task 6 的 `billing_round += 1` 分支）；
> ③ 若按 SUCCEEDED 结清，后续重试命中 `finish_operation` 的"计费请求已按不同结果结算"保护（`usage_billing.py:306-308`）会抛 `RuntimeError`。

- [ ] **Step 1: 写失败测试（追加到 `server/tests/test_usage_billing.py` 末尾）**

```python
def test_reconcile_releases_stale_search_operations(client, route_state):
    """崩溃残留的搜索预留：超时后按未交付释放（FAILED），重试开新轮次."""
    from app.usage_billing import reconcile_operations
    from app.viral_search import reserve_search_operation

    _, uid = account(client)
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        raw.execute("UPDATE wallets SET available_credits=10 WHERE user_id=%s", (uid,))
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search',true,1) ON CONFLICT (service) "
            "DO UPDATE SET enabled=true, unit_credits=1"
        )
        stale = reserve_search_operation(
            conn, user_id=uid, source_id="stale-search-1", request_fingerprint="fp-stale"
        )
        fresh = reserve_search_operation(
            conn, user_id=uid, source_id="fresh-search-1", request_fingerprint="fp-fresh"
        )
        assert tuple(
            raw.execute(
                "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (8, 2)
        # 模拟"预留后、页面事务提交前"崩溃：把预留老化到窗口之外。
        raw.execute(
            "UPDATE billing_operations SET created_at=now()-interval '31 minutes' WHERE id=%s",
            (stale,),
        )
        assert reconcile_operations(conn) == 1
        assert reconcile_operations(conn) == 0
        assert tuple(
            raw.execute(
                "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (9, 1)
        assert raw.execute(
            "SELECT state,actual_units FROM billing_operations WHERE id=%s", (stale,)
        ).fetchone() == ("FAILED", 0)
        assert raw.execute(
            "SELECT state FROM billing_operations WHERE id=%s", (fresh,)
        ).fetchone() == ("PENDING",)
        # 释放后的重试开新轮次：同一 source_id 重新预留，billing_round 升到 2。
        retry = reserve_search_operation(
            conn, user_id=uid, source_id="stale-search-1", request_fingerprint="fp-stale"
        )
        assert retry != stale
        assert raw.execute(
            "SELECT billing_round FROM billing_operations WHERE id=%s", (retry,)
        ).fetchone() == (2,)
        assert tuple(
            raw.execute(
                "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s", (uid,)
            ).fetchone()
        ) == (8, 2)
```

- [ ] **Step 2: 跑测试确认失败（红：sweep 尚未实现）**

```powershell
$env:TEST_POSTGRESQL_URL = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921\server
uv run python -m pytest tests/test_usage_billing.py::test_reconcile_releases_stale_search_operations -v
```

Expected: FAIL —— `assert 0 == 1`。现有实现只扫 `viral_data`（`usage_billing.py:540-548`），
搜索预留的 `source_id` 不匹配通用 operations 查询里的任何任务表（`usage_billing.py:571-606`），
因此 `reconcile_operations` 返回 0，搜索预留仍停在 PENDING、钱包仍是 (9, 1) 而非 (10, 0)。

- [ ] **Step 3: 实现（`server/app/usage_billing.py` 两处改动）**

**（a）** 在 `reconcile_operations` 的 `viral_data` sweep 之后插入搜索 sweep ——
即 L547-548 的 `for row in stale: finish_operation(...)` 之后、L549 `# Expired link calls cannot safely be repeated.`
注释之前：

```python
    # A search page is an atomic delivery: the reservation settles in the same
    # transaction that persists the page, so a PENDING row past the window means
    # the page was never delivered. Release it; a retry opens a new billing round
    # through reserve_search_operation.
    searches = conn.execute(
        "SELECT id FROM billing_operations WHERE state='PENDING' "
        "AND service='viral_search' AND user_id IS NOT NULL AND created_at<now()-interval '30 "
        "minutes' "
        "ORDER BY created_at LIMIT %s",
        (limit,),
    ).fetchall()
    for row in searches:
        finish_operation(conn, operation_id=str(row[0]), units=0, succeeded=False)
```

**（b）** 同函数稍后把 `settled = len(stale)`（L607）改为：

```python
    settled = len(stale) + len(searches)
```

> 不动的地方：`_reconcile_first_frame_auxiliary_costs`、`viral_data` sweep、link/prompt receipts 状态收尾、
> 通用 operations 查询与主循环。搜索的 `source_id` 不匹配任何任务表，通用查询天然不会重复处理；
> `user_id IS NOT NULL` 条件把供应商平台单（`user_id IS NULL`，由 `billing_meter` 自结算）排除在外。

- [ ] **Step 4: 跑测试确认通过（绿）**

```powershell
uv run python -m pytest tests/test_usage_billing.py::test_reconcile_releases_stale_search_operations -v
```

Expected: `1 passed`。

- [ ] **Step 5: 回归（结算补偿整篇）**

```powershell
uv run python -m pytest tests/test_usage_billing.py -q
```

Expected: 全部 PASS —— 既有 `viral_data` 超时结清、link receipt 释放、first_frame 辅助成本恢复用例不受影响。

- [ ] **Step 6: Commit**

```powershell
git add server/app/usage_billing.py server/tests/test_usage_billing.py
git commit -m "feat(viral): 结算补偿释放超时搜索预留（P1）

Co-authored-by: peihr666-max"
```

---
## Task 13: 收尾门禁（分片清单重建 + 全量回归）

**Files:**
- Modify: `scripts/ci/test-shards/shard-0.txt` ～ `shard-3.txt`（重建，纳入 `server/tests/test_viral_search_pg.py`）

> 背景：CI 的 Linux 质量门禁（`.github/workflows/ci.yml`）在跑分片前先执行两道 fail-closed 守卫：
> ① `python3 scripts/ci/migration_manifest.py --check`；② `python3 scripts/ci/build-test-shards.py --check-coverage`
> （后者是 CW-044 §18.2 的修复：提交的分片清单必须覆盖每一个 `server/tests/test_*.py`，漏一个新文件就红）。
> Task 1 新增了 `server/tests/test_viral_search_pg.py`（Task 6/7/9 继续往里追加用例），但 `scripts/ci/test-shards/`
> 里的清单还是 129 个文件的旧版本——**本任务必须重建并提交**，否则 CI 在 `--check-coverage` 处 fail-closed。
> （当前本地跑 `--check-coverage` 是绿的：`129 discovered test file(s) fully covered`；Task 1 之后会变成 130 个
> 文件而清单未覆盖 → 红。）

- [ ] **Step 1: 重建分片清单**

```powershell
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921
python scripts/ci/build-test-shards.py --shards 4
```

Expected: 无报错（重写 `scripts/ci/test-shards/shard-0.txt` ～ `shard-3.txt`）。确认新测试文件已进清单：

```powershell
Select-String -Path scripts/ci/test-shards/*.txt -Pattern "test_viral_search_pg"
```

Expected: 恰好 1 行命中，形如 `scripts\ci\test-shards\shard-2.txt:server/tests/test_viral_search_pg.py`
（落在哪个分片由时长画像决定，不必与示例一致）。

- [ ] **Step 2: 静态门禁（CI 同款两道守卫）**

```powershell
python scripts/ci/build-test-shards.py --check-coverage
python scripts/ci/migration_manifest.py --check
```

Expected:
- 第一条 → `==> coverage OK: 130 discovered test file(s) fully covered by the manifests in ...`
- 第二条 → `==> migration guard OK`

（两条 exit code 均为 0。若第一条报 gap，说明 Step 1 没生效，回到 Step 1 重跑。）

- [ ] **Step 3: 全量回归（顺序全量 = CI 分片脚本的回退路径）**

```powershell
$env:TEST_POSTGRESQL_URL = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921\server
uv run python -m pytest tests -q
```

Expected: 整套绿（最后一行 `N passed`，`failed` 计数为 0）。这套是 PG 矩阵全量，耗时较长，请一次跑完；
中途失败**不要**改测试放行——按失败用例回到对应 Task 修复后重跑。

> 可选加速（本机 Docker 可用时）：回仓库根执行 `bash scripts/ci/run-pytest-shards.sh`，
> 走 CI 同款 4 分片并行（每片独立 PG 容器，结束自动清理、自动校验分片清单覆盖）。

- [ ] **Step 4: Commit（分片清单）**

```powershell
cd e:\众墅之家爆款短视频创作\.worktrees\viral-business-optimization-20260921
git add scripts/ci/test-shards/
git commit -m "chore(ci): 重建测试分片清单（纳入 viral 搜索 PG 测试）

Co-authored-by: peihr666-max"
```

---

## 完成标准（DoD）

13 个 Task 的 checkbox 全部勾完、每个 commit 末尾都有 `Co-authored-by: peihr666-max` trailer 之后，
按下表做最终验收（映射设计文档 §5.2 接口契约 / §5.5 停用清单 / §9 P1 范围 / §12 测试策略）：

**接口契约（设计 §5.2）**

- [ ] `POST /api/viral/search` —— 每翻页一次 = 外呼一次 + 计费一次 + 内容池 upsert + 封面归档 + 发现记录（Task 6/7 测试绿）
- [ ] `POST /api/viral/search/refresh` —— 单视频直链刷新（Task 8 测试绿）
- [ ] `GET /api/viral/search/copy` —— 共享文案缓存读路径，二次提取瞬间命中（Task 9 测试绿）
- [ ] `GET /api/viral/search/discoveries` —— 客户侧发现记录（Task 9 测试绿）
- [ ] `GET /api/control/viral/discoveries` —— 运营侧每日汇总（Task 10 测试绿）

**核心能力**

- [ ] 抖音播放地址直取默认分辨率（Task 3；为 P2 客户端"默认分辨率缓存"提供直链）
- [ ] 内容池 upsert 保护既有 category + 发现记录读写（Task 4）
- [ ] 共享文案缓存读路径（Task 5）

**数据与计费**

- [ ] `viral_search_discoveries` 迁移 + head 常量全同步 + manifest 重记（Task 1；CW-056 冻结矩阵绿）
- [ ] `viral_search` 进计费目录、复用 `viral_extract` 接口键、不预置 tariff（Task 2）
- [ ] 搜索预留幂等（PENDING/SUCCEEDED 复用）+ 超时释放 + 重试升轮（Task 6/7/12 测试绿）
- [ ] 供应商成本仍以 `meter_call("viral_data")` 记平台单，客户不为采集先付（Task 11 守卫测试绿）

**停用清单（设计 §5.5，只摘调用不删函数）**

- [ ] `acquire_viral_refresh_task` 不再自动入队（Task 11）
- [ ] `run_pg_collection_once` 不再触发结算、返回语义 0/1（Task 11）
- [ ] `enqueue_due_viral_collections` / `settle_collection_charges` / `reconcile_collection_requests` /
      `create_collection_batch` / `eligible_collection_users` / `--viral-collection` CLI /
      前端 `dev:viral-collection` script 均保留未删（Task 11「不要删除」清单；CLI 由既有 `test_build_contracts.py` 契约断言）

**门禁与回归**

- [ ] `python scripts/ci/build-test-shards.py --check-coverage` → `coverage OK: 130 ...`
- [ ] `python scripts/ci/migration_manifest.py --check` → `migration guard OK`
- [ ] 全量 `uv run python -m pytest tests -q` 无 `failed`

---

