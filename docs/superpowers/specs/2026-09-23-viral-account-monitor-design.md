# 对标账号监控 + 自身账号发布后数据回采 · 设计

**日期**：2026-09-23
**状态**：Draft（待 P0 三次真实探测确认两个悬念）
**分支**：`worktree-viral-account-monitor`
**基准**：`a688d0fe`（含 #226 爆款关键词搜索）

---

## 1. 背景与目标

### 1.1 现状：两条链路都是半截的

**对标侧**——爆款视频库是「关键词 × 周」的横向快照。周采集调度 `enqueue_due_viral_collections()`（`server/app/viral_collection.py:31`）**已无生产调用点**（design §5.5 停用采集调度），现役模式是用户主动搜索（`server/app/viral_search.py`，P1 于 #205 合入）。看不到「某个对标账号最近发了什么、哪条在涨」，无法锁定追踪。

**发布侧**——`publish_records` 走完 `queued → publishing → published` 就结束了。表里四列已就位：

| 列 | 定义位置 | 现状 |
|---|---|---|
| `stats jsonb` | `20260917T1000_publish_records.py:41-72` | **无任何代码写入** |
| `stats_synced_at` | 同上 | **无任何代码写入** |
| `sync_requested` | 同上 | `publish_records.py:467 request_sync()` 只置位 |
| `platform_status` | 同上 | `finalize_publish_work()` 不写 |

专用索引 `idx_publish_records_sync ON (sync_requested, stats_synced_at) WHERE status='published'` 已建但**无消费方**。`claim_publish_work()` 的 `_PUBLISH_CANDIDATE_SQL` 只筛 `status='queued'`，完全不看 `sync_requested`；`publish_worker.py:run_publish_round()` 每轮只做账号探针、登录态探针、发布投递三件事，**没有 sync 轮次**。

`docs/evidence/PUBLISH-DELIVERY-20260917.md:7` 明确这是 PR-B `PUBLISH-FALLBACK-SYNC` 的范围，且写「PR-B 只加逻辑不改 DDL」。**该 PR 从未开工**（`git log --all --grep="FALLBACK-SYNC"` 零结果，无相关分支）。

前端 `client/src/studio/PublishRecordsPanel.tsx` 已渲染 `play_count/like_count/comment_count/share_count` 四项 + `stats_synced_at` + 「同步数据」按钮（`:24`、`:184-190`、`:219-227`），`isActivePublishRecord` 已把 `sync_requested` 视为活跃态触发轮询（`:56`）。**UI 就绪，等待数据。**

### 1.2 目标

把两条半截链路接成闭环，核心产出是「**对标 vs 我**」的对比分析，并把洞察喂回复刻流水线的选题与文案环节。

### 1.3 已拍板口径

| 项 | 结论 |
|---|---|
| 核心场景 | **对标 vs 我 对比为主** |
| 使用主体 | 客户端自助 + 管理端总览 **都要** |
| 账号来源 | **平台预置池**（运营维护、共享、只采一次）+ **客户自录**（租户私有） |
| 采集策略 | **分层频率 + 增量采集** |
| 自身账号回采 | **免费**（零外部供应商成本，作卖点） |
| 抖音侧回采 | 与视频号**一起做** |
| UI 落点 | **并入现有「爆款视频」页做 Tab** |
| 搜账号口径 | **两种都要**（内容反推作者 + 精准搜账号） |
| 视频号账号发现 | **只能走「搜账号」** |
| 采集成本归属 | 平台预置池 = 平台承担；**客户自录 = 全部客户扣费** |
| 账号配额 | **不限制，但预留限制接口** |

---

## 2. 数据源（TikHub，已调研确认）

> 证据源：TikHub 线上 OpenAPI 规范 **V5.3.2**（`https://api.tikhub.io/openapi.json`，1048 paths）+ 官方文档站逐接口页（`https://docs.tikhub.io/<id>.md`）。**未做推测性补全。**

### 2.1 能力矩阵

| # | 能力 | 结论 | 端点 |
|---|---|---|---|
| 1 | 抖音按账号拉作品 | ✅ | `GET /api/v1/douyin/app/v3/fetch_user_post_videos`（`sec_user_id`,`max_cursor`,`count`≤20,`sort_type` 0新/1热,`channel` normal/lite） |
| 2 | 抖音用户资料 | ✅ 9 变体 | `handler_user_profile`(sec_uid) / `_v2`(**`unique_id` 抖音号直查**) / `_v3`(uid) / `fetch_batch_user_profile_v1`(≤10) |
| 3 | 抖音搜账号 | ✅ | `POST /api/v1/douyin/search/fetch_user_search` → `user_list[].user_info{sec_uid, unique_id, nickname, `**`follower_count`**`, signature, avatar_*}`；可筛 `douyin_user_fans`(`0_1k`/`1k_1w`/`1w_10w`/`10w_100w`/`100w_`)、`douyin_user_type` |
| 4 | 抖音 play_count | ⚠️ **常规接口全不返回** | 补调 `GET /api/v1/douyin/app/v3/fetch_video_statistics?aweme_ids=`（**逗号分隔最多 2 个**）；或 `xingtu_v2/get_item_play_count`（$0.002/次，**含投流**） |
| 5 | 抖音自有账号（creator） | ✅（**本次不采用**，见下） | `POST /api/v1/douyin/creator_v2/fetch_item_list`(cookie,start_time,end_time) / `fetch_item_overview_data` / `fetch_author_diagnosis`（共 14 个，**均需传客户 cookie**） |

> **决策（2026-09-23）**：抖音自有账号回采**走自建逆向，不走 TikHub creator_v2**——客户登录态不出我方边界。TikHub 的 `creator_v2/*` 仅作为**接口形态与字段口径的参考**（用来对照我们自建实现拿到的数据是否齐全），不进入调用链。
| 6 | 视频号按账号拉作品 | ✅ | `POST /api/v1/wechat_channels/v2/fetch_user_videos`（`username`,`last_buffer`）→ **`read_count`** + like/fav/forward/comment + `media{full_url,decode_key}` |
| 7 | 视频号账号资料 | ✅ | `POST /api/v1/wechat_channels/v2/fetch_user_profile`（粉丝/作品/获赞/收藏/转发数） |
| 8 | **视频号搜账号** | ⚠️ **无专属接口** | 间接：`POST /api/v1/wechat_search/v2/fetch_search` + `business_type="account"`，官方称"实测可搜视频号"，但**文档示例是公众号 `gh_…`，字段是否含 finder username 无法确认** |
| 9 | `sph…` 短号 → finder username | ✅ | `POST /api/v1/wechat_channels/v2/fetch_channel_id_to_username` |
| 10 | 视频号搜索结果作者 ID | ❌ 无 | `fetch_search_videos` 无作者唯一 ID（仅 `source.title`/`iconUrl`，见 `viral_tikhub.py:437-450`）；唯一路径 `exportId` → `fetch_video_detail` → 明文 `username` |
| 11 | 计费 | 按次 + 预充积分，**无订阅** | $0.001–0.01/次，逐接口定价；**仅 2xx 计费**；24h 内重复调用不重复计费；日用量阶梯（1000 内 $0.001 → 30k+ $0.0005）。查价：`GET /api/v1/tikhub/user/get_endpoint_info?endpoint=` |

**视频号 V2 接口统一 0.01$/次**（`fetch_user_videos` / `fetch_user_profile` / `fetch_video_detail` / `fetch_channel_id_to_username` / `wechat_search/v2/fetch_search`）。**抖音端点公开文档未标价**，需用 key 查。

> 注意：TikHub 官方插件仓库的 `openapi-index.json` **已过期**——它仍列有 `wechat_channels/fetch_user_search` 等线上已下线的 path。**以 `api.tikhub.io/openapi.json` 为准。**

### 2.2 两个颠覆直觉的发现

**(a) 视频号的播放量比抖音好拿，成本也更低。**
`fetch_user_videos` 一次调用返回全部指标含 `read_count`；抖音要拿 play_count 必须**每条作品额外补调** `fetch_video_statistics`（每次最多 2 个 ID）→ **抖音单账号采集请求数 ≈ 1 + N/2**（N = 新作品数），**成本约为视频号的 1.5 倍**。这直接决定 §5 的频率分档与 §6 的计费定价。

**(b) 抖音「精准搜账号」几乎零成本，且字段比预期全。**
`fetch_user_search` 直接返回 `sec_uid` + `unique_id`(抖音号) + **`follower_count`**——粉丝数是判断对标价值的核心指标，一步到位，不需要额外调资料接口。

### 2.3 现有代码的两个解析缺口

| 缺口 | 位置 | 影响 |
|---|---|---|
| 抖音解析**丢弃 `author.sec_uid`** | `viral_tikhub.py:390-396, 407-409`（只取 nickname/avatar/is_verified） | 补上即可让「搜关键词 → 顺手得到账号候选」成为**零新增 API 成本**的副产品 |
| 视频号解析**只取 `source.title`/`iconUrl`** | `viral_tikhub.py:437-450` | 搜索响应确实无作者 ID（§2.1 #10），需走 `exportId` 中转 |

### 2.4 账号发现路径

| 平台 | 主页链接 | 精准搜账号 | 兜底 |
|---|---|---|---|
| **抖音** | 需扩 `_DOUYIN_ID_PATTERNS` 加 `/user/{sec_uid}`（`viral_link.py:41-45` 现有 4 个模式均非主页） | ✅ `fetch_user_search`（含粉丝数） | 运营手工填 sec_uid / 抖音号（`handler_user_profile_v2` 支持抖音号直查） |
| **视频号** | ❌ 不通：`supported_link_platform()` 只返回 douyin/xiaohongshu，视频号链接直接 422 `WECHAT_CHANNELS_LINK_UNSUPPORTED`（`viral_link.py:180-186`）；且无公开主页 URL 体系 | ⚠️ **待实测** | ①`exportId` 中转到 `fetch_video_detail` 取 `username` ②`sph…` 短号经 `fetch_channel_id_to_username` 转换 ③运营手工绑 finder username |
| **小红书** | ❌ 现有 `/user/profile/[^/]+/([0-9a-fA-F]{24})` 是**取 note_id** 的 | ❌ 未接 | **本次不做** |

---

## 3. 架构：双轨并立 + 统一对比层

| 侧 | 落点 | 说明 |
|---|---|---|
| 对标账号 | 新表 `viral_accounts` + `viral_account_subscriptions`；作品**继续落 `viral_videos`** | 对标作品自动继承列表/详情/封面归档/媒体管线/`viral_script_cache` 文案缓存/复刻导入**全链路** |
| 自身作品 | 复用 `publish_records`（**零 DDL**）+ 新表快照 | PR-B 接缝已就绪 |
| 对比层 | **服务层归一，不建宽表** | 只读投影，避免与 `viral_videos` 重复建模 |

**否决的路线**：
- ① 统一 `content_performance` 事实表——与 `viral_videos` 大面积重复，等于推倒现有链路
- ② 只给 `viral_videos` 加作者维度、不做账号实体——无法「锁定监控」，不满足需求本意

### 3.1 数据模型

**`viral_accounts`（对标账号池）**
```
id, platform, platform_user_id      -- sec_uid / finder username
nickname, avatar_url, signature, verified, follower_count, works_count, homepage_url
scope            -- 'platform'(运营预置，owner_user_id NULL) | 'customer'(客户自录)
owner_user_id    -- 客户自录时的母账号；platform 池为 NULL
status           -- 'active' | 'paused' | 'paused_insufficient_credits' | 'invalid'
tier             -- 分层频率档位（§5）
last_synced_at, next_sync_at
consecutive_empty_syncs, consecutive_failures, last_error_code
lease_owner, lease_expires_at
UNIQUE(platform, platform_user_id)   -- 三个发现入口据此去重
```

**`viral_account_subscriptions`（租户订阅）**
```
PRIMARY KEY(owner_user_id, account_id)
created_by_user_id, notify_enabled, created_at
```

**`viral_videos` 增列**（表定义见 `070_viral_video_library.py:44`）
```
author_id     text NULL            -- 平台用户 ID，账号维度聚合的锚点
source_kind   text NOT NULL DEFAULT 'keyword_search'   -- keyword_search|account_monitor|link_import
索引 (platform, author_id, published_at DESC)
```

**`viral_video_stats_snapshots`（对标作品时间序列）**
```
platform, video_id, captured_at, elapsed_hours,
play_count, likes, comments, shares, collects    -- 全可空（平台不提供即 NULL）
UNIQUE(platform, video_id, captured_at)
```

**`publish_record_stats_snapshots`（自身作品时间序列）**
```
record_id FK publish_records(id), captured_at, elapsed_hours,
play_count, like_count, comment_count, share_count, collect_count
UNIQUE(record_id, captured_at)
```

> **为什么必须存时间序列而非单个当前值**：对比要剔除粉丝基数差异，靠的是**发布后增速曲线**（T+1h/6h/24h/72h），单个当前值算不出来。

### 3.2 对比的四把尺子（产品核心）

直接比绝对播放量是错的——对标账号粉丝可能是我的 100 倍，会得出「我做得很差」的错误结论。有信息量的基准：

1. **对标自身基准线**：某条 vs **该账号自己的中位数** → 识别「它这条跑爆了」（而非「它粉丝多」）
2. **发布后增速曲线**：T+N 增速对比 → 剔除粉丝存量，看内容本身的冷启动能力
3. **同题材池百分位**：我这条放进同标签/同选题的对标作品池，排第几百分位
4. **相对自身**：我这条 vs 我自己历史中位数 → 有没有进步

---

## 4. 复用资产清单

| 资产 | 位置 | 复用方式 |
|---|---|---|
| TikHub 客户端四件套（Transport Protocol + 凭据 + 计费埋点 + 归一化 DTO） | `server/app/viral_tikhub.py`（790 行） | 照抄扩端点；凭据走 `SettingsRepository.load_provider_config("tikhub")`（`settings.py:57`） |
| 内容池 upsert / 游标分页 / 封面归档 / 媒体管线 / 文案缓存 | `viral_store.py`（891 行）、`viral_media.py`、`viral_script_cache` | 对标作品落 `viral_videos` 即继承 |
| DB 租约队列六件套（enqueue/status/acquire/renew/complete/fail） | `viral_refresh.py`（209 行，**最干净的范本**） | 新采集队列照抄 |
| 单例调度的 `FOR UPDATE SKIP LOCKED` 骨架 | `viral_collection.py:31 enqueue_due_viral_collections()` | 照抄形态，落到 `worker-viral` 进程 |
| 常驻 worker（无 cron/APScheduler/Redis） | `generation_worker.py`、compose `worker-viral`（`--viral-collection --idle-seconds 30`） | 新任务挂进去 |
| 发布账号（扫码 + Fernet `storage_state_enc` + 24h 探活） | `publish_browser_accounts`、`publish_browser.py`（`PROBE_INTERVAL_SECONDS = 86_400`） | 自身回采的凭据来源 |
| **视频号作品列表通道** | `channels_publisher.py:953 post_list()` | **已在被"发布后匹配短链"使用**；同文件 `:1014 find_published_post()` 是调用范例 |
| 计费（科目表 + 计量上下文 + 折扣接口） | `billing_catalog.py`、`billing_meter.py`（`meter_call` @L66） | 新增科目 + 接口键 |
| 多租户边界 | `users.parent_user_id / account_type`（`20260919T1200_sub_accounts`）——**母账号 MASTER = 钱包与资产归属单位** | 订阅与配额挂母账号 |
| 媒体解密（视频号 ISAAC64，前 128 KiB） | `viral_decrypt.py`（`KEYSTREAM_SIZE = 131_072`） | `fetch_user_videos` 返回的 `media.decode_key` 同款 |

**硬约束**（`AGENTS.md`）：禁止引入 ORM / Redis / 消息队列；PG 唯一真源；重型 vendor import **只能出现在 publish_worker 进程**（`publishers/__init__.py` 与 `douyin_adapter.py` 文件头均明示，FastAPI 进程绝不能加载）。

---

## 5. 采集调度：分层频率 + 增量

现役代码**没有 cron/APScheduler**。「定时」= `next_*_at` 字段 + 常驻 worker 轮询。`viral_collection.py:31` 的形态（`FOR UPDATE SKIP LOCKED` 单例 + `retry_count` 上限 + checkpoint 续跑）是现成骨架，直接照抄。

| 层 | 频率 | 内容 | 说明 |
|---|---|---|---|
| A 新作品发现 | 活跃账号 2–6h | 只拉作品列表首页（`count`≤20），有新增才继续翻页 | 增量：新 video_id 才入库 |
| B 新作品指标回采 | T+1h / 6h / 24h / 72h / 7d 共 5 次后停止 | 只采已知作品 | **逐步降频，不无限重拉** |
| C 账号资料 | 24h | 粉丝数 / 作品数 | 粉丝基数归因 |
| D 静默降频 | 连续 N 次无新作品 → 12h/24h | | 僵尸账号自动降本 |

**成本不对称（必须区别对待）**
- **视频号**：`fetch_user_videos` 一次调用覆盖 A+B（含 `read_count`）
- **抖音**：`fetch_user_post_videos` **不返回 play_count** → 要播放量必须补调 `fetch_video_statistics`，请求数 ≈ 1 + N/2
  - 对策：只在 T+24h 之后的快照补播放量（早期噪声大、且此时新增作品已收敛）；或按需用 `xingtu_v2/get_item_play_count`；或产品上抖音侧只做互动数

**自身账号回采**
- 发布成功即写入 T+1h/6h/24h/72h/7d 的 sync 计划 → `publish_worker.py` 新增第 4 类 claim（`sync_requested=1 AND status='published'`，索引已建好）
- 视频号通道：`ChannelsPublisher.post_list()`（vendor 已有，**已被"发布后匹配短链"使用**；同文件 `:1014 find_published_post()` 是调用范例）
- 抖音通道：**自建逆向**（决策已定）。复用 `publishers/vendor/douyin_publisher` 的 `create_session(cookie, referer)` / `sign_a_bogus` / `sign_mssdk` / `chrome_request_headers()` 基建，自己调 `creator.douyin.com` 的作品数据接口。
  - **现状缺口**：vendor 库**只有 `/web/api/media/user/info/`**（返回 uid + nickname），**没有作品列表与数据接口** → 需新写
  - 参考对照：TikHub `creator_v2/fetch_item_list` 的参数形态（`start_time`/`end_time` 必填、`fields=metrics,review,visibility`）与 `fetch_item_overview_data` 的指标字段（`metrics,review,play_info,dou_plus,content_analysis`）可作为**数据完备度的验收基准**
  - 进程边界：**只能在 publish_worker 进程内**（重型 vendor import 硬约束）

**平台的坑（写代码时必须遵守）**
1. 视频号 V2 接口 timeout **必须 ≥30 秒**，否则「已扣费收不到响应」
2. 视频号 64 位大整数 ID（`id`/`object_nonce_id`/`docID`）**必须按字符串处理**，不能过 JS `Number`
3. 视频加密 MP4 必须用**同一次响应**里的 `full_url` + `decode_key` 解密（key 每次变化）

---

## 6. 计费

`billing_catalog.py:SERVICES` 新增：

| 科目 | unit | provider | 说明 |
|---|---|---|---|
| `viral_account_sync` | call | tikhub | 对标账号采集外呼 |
| `publish_stats_sync` | call | — | 自身账号回采（零外部成本） |

- **平台预置池** → 采集时**不挂** `billing_context`（平台承担）
- **客户自录账号** → 挂该母账号的 `billing_context`，走 `viral_extract` 折扣接口键（`SERVICE_INTERFACE`），**全部客户扣费**
- **自身账号回采** → `customer_charge_allowed=False, zero_cost_platform=True`（零外部供应商成本，作卖点免费）
- 计费范式：`accept_operation` → 外呼（`meter_call`）→ `finish_source(units=N, succeeded=…)`；失败路径必须 `finish_source(units=0, succeeded=False)` 释放预留（`viral_search_routes.py:152-165` 是标准写法）

**配额：当前不限制，但预留限制接口**
- 定义 `check_monitor_quota(conn, owner_user_id) -> QuotaDecision`，当前恒返回 `allowed=True`
- **所有添加账号入口必须先过它**；后续加限制只改函数体，不动调用点
- 与 `sub_account_quotas` 同族但不复用其表（语义不同）

**余额不足的失败模式**（客户扣费下的必然问题）
- 采集外呼前预检母账号钱包余额；不足则跳过本轮并把账号标 `paused_insufficient_credits`，前端提示充值
- **绝不能因余额不足把账号标 `invalid`**——那是登录态失效的语义

---

## 7. UI 落点

**客户端**（React 19 / `client/src/studio/`，自研 hash 路由，无 react-router；页面枚举 `types.ts:9`）
- 「爆款视频」页新增**「关注账号」Tab**：账号列表 → 账号详情（作品流 + 指标曲线 + **「去复刻」入口**）
  - 顺手补上现有断点：爆款卡片与详情页**没有**「去复刻/去口播/存素材库」（`docs/真实业务流程全景梳理-2026-09-21.md` §7 #6）
- 「发布管理」页增强：`PublishRecordsPanel.tsx` 已有 stats 列与「同步数据」按钮，补趋势图
- 「对比分析」：T+N 曲线对比、同题材百分位、对标基准线

**管理端**（`client/src/admin/`，独立 Vite 构建 `vite.admin.config.ts`）
- 对标账号池维护：增删改、批量导入、采集状态、失败原因
- 监控总览：账号数 / 采集次数 / API 成本 / 失败率

---

## 8. 分阶段

| 阶段 | 交付 | 依赖 |
|---|---|---|
| **P0 Spike** | 3 次 TikHub 探测（§9）+ 真机验证视频号 `post_list` 字段 + 查抖音端点单价 | **无代码依赖，最先做**（成本 ≤ $0.03） |
| **P1 自身账号回采** | sync worker 轮次 + 快照表 + 视频号 `post_list` 回采 + 抖音通道 + 前端趋势图 | 打通 PR-B 空壳，接缝已完全就绪，**最快见效** |
| **P2 对标账号监控** | 账号实体 + 订阅 + 采集队列 + 分层调度 + 作品落 `viral_videos`（`author_id`）+ 「去复刻」入口 | 依赖 P0 |
| **P3 对比分析** | 四把尺子的服务层 + 对比页 | 依赖 P1+P2 有数据 |
| **P4 管理端 + 配额计费** | 账号池维护、监控总览、配额、计费科目上线 | |

**P0 与 P1 可并行**——P1 完全依赖已有的发布账号与 vendor 库，不依赖 P0 结论。

---

## 9. P0 Spike 动作清单

需一个真实 `TIKHUB_API_KEY` 与一个真实账号：

| # | 动作 | 决定 |
|---|---|---|
| 1 | `POST /api/v1/wechat_search/v2/fetch_search` body `{"keyword":"乡墅","business_type":"account","raw":false}` → items 里有没有带 `@finder` 的账号项 | **视频号能否"一步搜账号"**（用户已定视频号只能走搜账号） |
| 2 | `POST /api/v1/douyin/search/fetch_user_search` body `{"keyword":"乡墅","cursor":0}` → 确认 `sec_uid`/`unique_id`/`follower_count` 实际存在 | 抖音搜账号是否照文档工作 |
| 3 | `GET /api/v1/douyin/app/v3/fetch_user_post_videos?sec_user_id=<上一步>&count=5` → 确认 `statistics.play_count` 是否恒 0 | **是否必须逐条补调** → 抖音侧成本翻不翻倍 |
| 4 | `GET /api/v1/tikhub/user/get_endpoint_info?endpoint=/api/v1/douyin/search/fetch_user_search` | 抖音端点单价 |
| 5 | 真机：用已绑定视频号账号跑一次 `ChannelsPublisher.post_list()` | 视频号回采的数据完备度 |

**分叉预案**

| 结果 | 应对 |
|---|---|
| 视频号 `business_type=account` 能拿到 finder username | 一步到位，按计划执行 |
| 不能 | ①`exportId` → `fetch_video_detail` 中转（每步 $0.01）②`sph…` 短号 → `fetch_channel_id_to_username` ③运营手工绑 |
| 抖音不返回 play_count（大概率） | ①按需补调（成本 ×1.5）②抖音侧只做互动数③对客户明示"播放量按需付费补采" |

---

## 10. 工程门禁（项目硬规范）

1. **迁移**：`server/migrations/versions/YYYYMMDDTHHMM_<snake>.py`，`revision` = 文件名（`scripts/ci/migration_manifest.py:60 NAMING_PATTERN`）；跑 `python3 scripts/ci/migration_manifest.py --record`；同步 `server/tests/test_cw056_supported_head_matrix.py` 的 `HEAD_REVISION`(@L86) + `HEAD_SCHEMA_COUNTS` + `HEAD_TABLE_NAMES` + `HEAD_SCHEMA_DIGEST`
2. **测试**：新 `server/tests/test_*.py`（unit，**注入 fake transport 不打网络**）+ `*_pg.py`（PG 夹具 `server/tests/pg_test_kit.py`，DSN `localhost:5433/customer_v3_test`）；**登记到 `scripts/ci/test-shards/shard-N.txt`**（`test_cw061_shard_coverage_guard.py` 校验）
3. **前端**：`client/src/api.ts` 加调用 → `npm run generate:api` 刷新 `client/src/generated/api.ts`（tsc 即 schema-drift 门禁）；同目录加 `.test.tsx`
4. **文档**：进度文档 + `docs/CUSTOMER-TASK-EVIDENCE-V3.md` + `docs/evidence/<TASK-ID>-EVIDENCE.md`
5. **收尾**：`npm run check:sharded`（~6min）或 `npm run check`（~16min），**全量只跑一次**；严禁两个全量实例打同一 PG fixture
6. **密钥红线**：TikHub key 走服务端加密配置，**永不入代码/日志/PR**；`scripts/verify_no_secrets.sh` 在 CI 首步跑

---

## 11. 验证方式（端到端）

- **P1**：绑定真抖音/视频号账号 → 发布一条 → 等 sync 计划触发 → `SELECT stats, stats_synced_at FROM publish_records` 有值 → 前端趋势图出点；`sync_requested` 归零
- **P2**：管理端加一个预置账号 → 观察 `next_sync_at` 到期被 claim → `viral_videos` 出现 `source_kind='account_monitor'` 且 `author_id` 正确 → 客户端能播、能提取文案、能去复刻
- **P3**：构造已知数据（A 账号 5 条 / B 账号 3 条），断言中位数、百分位、T+N 增速正确（纯函数单测 + 一个 PG 集成）
- **成本**：跑 24h 后核对 `billing_operations` 中 `viral_account_sync` 的实际调用次数与账号数 × 频率是否吻合

---

## 12. 待定项

1. ~~抖音自有账号回采走哪条路~~ → **已定：自建逆向**（2026-09-23）
2. 采集频率档位数值（2h/4h/6h）——做成运行期可配（照 `viral_runtime_controls.collection_interval_days` 先例）
3. 抖音 play_count 采买策略：全量补采 / 只对爆款补 / 不采（待 P0 第 3 项结果）
4. 客户自录账号连续失败或余额不足时的通知渠道
