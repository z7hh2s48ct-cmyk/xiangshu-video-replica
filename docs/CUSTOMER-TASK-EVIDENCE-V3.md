# Customer Edition Task Evidence Record V3

> 2026-09-17 BRAND-CORRECTION：首排恢复横排，Logo/文字/间距按原基线放大20%；欢迎页接入共享组件，修复无尺寸约束巨图；桌面图标仅补纯黑底。回归先红后绿，静态门1676前端与PG2527通过/1既有skip，独立评审APPROVE。已打开本地示例预览，未合并或部署。见[独立证据](evidence/BRAND-CORRECTION-20260917.md)。

## DESKTOP-ACTIONS-20260917

GitHub Actions 三平台桌面安装包与可下载 Artifact；复用现有 Tauri、API 校验与内测渠道，不改原三门禁。范围、先红后绿、构建与下载证据见[独立记录](evidence/DESKTOP-ACTIONS-20260917.md)；当前 AUTOMATED_VERIFIED（前端 1674 passed、后端 2540 passed / 1 既有 skip、21 项构建合同、独立评审通过）；远程构建待执行，未合并或部署。

> 2026-09-17 PRELAUNCH-REMEDIATION：上线评审 R02—R06 修复通过本地完整质量门与失败补验；R01/#130、R07/#129、R08/#126 已合入主线并整合。已提交 [PR #131](https://github.com/peihr666-max/xiangshu-video-replica-/pull/131)。仅代码修复，不变更生产验收等级。见[独立证据](evidence/PRELAUNCH-REMEDIATION-20260917.md)。

## 2026-09-15 MATERIAL-CACHE / R90 浏览器持久缓存

缓存 API 24 项新增回归覆盖授权、账号/版本隔离、容量、校验、超时、并发及清理竞争，完整 API 134 项通过；页面新增 5 项回归，完整 ContentPages 90 项通过。有效 RED 后实现，最终完整前端 100 文件 1544 项通过；TypeScript/Biome/秘密扫描及独立交叉评审通过。真实既有视频从 Blob 完整播放 4.458333 秒，清理后容量由 15.3 MB 归零，45 条云端素材保留，已打开视频播放至结束无媒体错误。原始日志和浏览器观察仅留本机。该项为本机验证，不代表离线素材库或全部历史素材验收；详见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。

## PUBLISH-QR-REUSE-20260915 — 双端官方扫码添加账号

采用 MIT `dreammis/social-auto-upload@0012d2c355f88f683cc38dde2a2db209e14091bc` 的二维码定位适配；新增网页服务器隔离浏览器与 Fernet 状态保存，Windows 沿用本机 WebView2。三平台两套脚本实测取码通过；Linux 静态门（1465 前端 / 150 Python 文件类型检查）与专项 PG 11 项通过。后端首次全量 2168 passed / 18 failed / 1 既有 skip；修复后受影响模块分组复验 121、27、42 passed，覆盖全部失败，最终唯一覆盖 2186 passed / 1 既有 skip。查重确认 PR #109 为独立联调任务；扫码兼容其网页会话请求头后，前端专项 196 项通过。证据层级 AUTOMATED_VERIFIED；真实手机确认、Windows 安装包、PR 三门禁和部署未完成。Owner 当前 Codex 任务，Reviewer 待 PR；详见[独立证据](evidence/PUBLISH-QR-REUSE-20260915.md)。

## 2026-09-15 MEDIA-BOUNDARY / R91—R93

R91 字幕越权覆盖 15 项有效 RED 后协议专项 32 passed；R92 错误网页假下载有效 RED 后下载/API 专项 138 passed；R93 重试与领取冲突 2 项有效 RED 后独立 PostgreSQL 恢复专项 26 passed。各项类型/静态与交叉评审通过，完整静态门通过（前端 1528 项）；后端全量首次 2269 passed、12 failed、1 既有 skipped，修正测试打包及一项旧字幕夹具后 44 项复验通过。历史音频 24.013197 秒在线和缓存两次完整播放均 ended=true、error=null；只读账务核对无残留冻结或重复结算。原始账号、任务与配置仅留本机。详见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。

## 2026-09-15 VIRAL-COPY-CACHE

服务器文案缓存核心有效 RED 为 3 failed / 2 passed，实现后核心 5 passed、扩展边界 12 passed；迁移静态 17 passed、隔离 PostgreSQL 迁移/历史起点 6 passed。最终缓存20项通过；完整Worker/ASR专项102项后追加两项历史竞争回归通过，完整前端1515项，独立PG全量2283 passed / 1既有 skipped。静态初次两处行长失败后补验剩余Python检查通过；收尾仅新增等待提示，单项及类型/格式检查通过。独立复审通过，本机追加迁移和已有文案持久缓存验证完成，无新增识别或收费。详见[任务证据](evidence/VIRAL-COPY-CACHE-20260915.md)。


## 2026-09-15 JOINT-RETEST R89 请求收入与用量展示

用户确认同时显示消费折合和实付收入后，经营分析列表/详情分别读取既有 nominal_revenue_fen 与 revenue_fen，利润保持原口径；用量一位小数且保留零值与未知状态。两组有效 RED 后，经营分析 8 项、管理端 31 文件 184 项通过，TypeScript/Biome/秘密扫描及交叉评审完成。真实管理端刷新、请求明细及核算详情已复验，未改账务事实。详见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)，原始数据仅留本机。

## 2026-09-15 JOINT-RETEST 用户质量验收与只读业务审计

R88 陈旧辅助质检成本收口有效 RED 后，完整计费隔离 PostgreSQL 专项 49 passed；覆盖源任务并发重开、调用完成占锁、批量限制、幂等及客户账务不变。Ruff/format、Mypy、秘密扫描和交叉评审通过。确认无在途任务后，原 Worker 已重启加载；实际遗留记录由 PENDING 转 UNKNOWN，调用总数及任务/账务核对结果不变，未重新生成。状态缺陷已完成本机验证，账单核实仍独立保留。

既有提交 3d2c794 的 Linux、Windows 与秘密扫描远程门禁通过。用户对既有 24 秒口播确认声音可接受、嘴型需调整；本机全帧时间轴检查正常且当前上传/归档不改音视频时序，尚无可证实的本地嘴型修复。管理端真实失败转写详情显示预留全退，与只读钱包流水一致。画幅交付路径、严格人物保真缺口及后续验收边界见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。未调用付费供应商或新增云对象，业务标识、账务明细和媒体留在本机。

## 2026-09-15 JOINT-RETEST R85—R87 验证汇总

旧轮询租约竞争、手动保存请求重叠和续期提交回滚均先实质 RED 后修复。完整前端 1508 passed、Worker 恢复 22 passed、内容资产隔离 PG 64 passed；上次 Linux CI 的四条 E2E 失败在同提交独立副本仅叠加同源代理启动修复后 4 passed。TypeScript、Biome、Python 静态检查和交叉评审完成；新提交远程 CI 独立核验，不把旧提交失败掩写为通过。三条实际视频完整播放通过，24 秒口播输入/输出音轨技术比对通过，无新增生成或云写入。详见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。

## 2026-09-15 JOINT-RETEST 迟到轮询与实际播放

R85 的四种旧/新 Worker 租约、普通/超时轮询交错先 RED，再补齐租约身份与未被替代条件；恢复专项 22 passed，Ruff/format、Mypy、秘密扫描及 diff 检查通过。只读复核与既有成功/失败写入策略一致。真实前端通过键盘启动播放器，中文云视频、24 秒口播和 68.27 秒长视频完整播放结束；没有把键盘路径通过外推为原鼠标控件崩溃根因已消除。口播与人物样片技术证据、本轮边界见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。未新建付费任务或写云对象，原 Worker 在确认空闲后加载恢复修复，仍只处理用户提交任务。

## 2026-09-15 JOINT-RETEST 画幅归档与播放缓存

R83 媒体处理先红后绿，限定显式 2K、9:16 的新手动归档，写存储前规范化并保存源/输出技术元数据；再次保存幂等，处理失败不上传，最终会话及冻结请求变化拒绝落库。R84 两个任务页面按结果版本隔离预览缓存，拒绝旧响应覆盖新资产，播放失败可重签。最终完整前端 1506 项、媒体专项 26 项、内容资产独立 PG 专项 58 项通过；TypeScript、Biome、Ruff/format、Mypy、秘密扫描和 diff 检查通过。真实刷新恢复登录和钱包、已有成片加载已复验。Docker 手动关闭导致依赖中断的故障已恢复，不将此归因为 Cookie 丢失。真实规范化云归档、完整播放、人物保真及口播听觉仍保留，详见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。

## 2026-09-15 JOINT-RETEST 并行联调增量

三个子 Agent 分别调查画幅、取消恢复及会话素材，主执行者统一页面复验和整合。R80 本机上传 Cookie 传输头、R81 同页旧 Cookie 清理/新登录竞争、R82 排队取消/Worker 领取竞争均先红后绿；最终完整前端 1501 项、专用隔离 PG 独立生成专项 33 项通过。Cookie 增量另经其他 Agent 只读复核，未发现新增阻断，范围不含跨标签页互斥。中文历史云素材加载已实测成功，完整播放遇内置浏览器崩溃仍保留；9:16 本地纠正样片八项技术检查通过，未上传或接入产品。现有运行任务不支持取消，应验证明确拒绝而非要求实际中止。原 PR 继续进行，详见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。

## 2026-09-15 JOINT-RETEST 会话与历史预览续测

R78 同源 HttpOnly Cookie 与 CSRF 传输、有效会话心跳恢复已完成；真实两次整页刷新保留登录。R79 中文云对象键授权校验改为 UTF-8 字节比较，同名访问及异名拒绝回归通过，实际页面修复后复验因浏览器连接故障保留待办。最终前端 1495 项通过；后端完整运行 2201 通过、1 失败、1 既有跳过，失败为新增测试占位符触发秘密扫描，修复后含该失败用例及存储、会话、Worker 恢复的 102 项专项全部通过。54 条非空本地素材均完整解码；画幅与人物保真未通过，口播听觉尚未完成。详情见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。保留本机业务证据，远程仅技术摘要，任务仍进行中。

## 2026-09-15 JOINT-RETEST 取消退款续测

已在本机真实界面完成图生视频入队取消及预留积分全额释放，供应商调用未增加，Worker 已恢复。R76 修复模式开放状态与提交提示，R77 修复取消任务预览仍等待；最终完整前端 1493 项通过，Biome、TypeScript 与 diff 检查通过。历史本地素材 54 条文件大小及哈希一致；失败重试和全部素材预览仍未验收。完整账户及账务证据仅留本机，技术详情见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。原任务保持进行中，未合并、未部署。

## 2026-09-15 JOINT-RETEST 续测记录

继续原分支与 PR #109。已补齐指定视频号的工作台精选、详情、搜索和前端 35.433333 秒完整播放验收；时长修复后的历史素材可预览并带入参考创作。R75 修复工作区热更新解绑/重绑期间无身份请求的迟到 401 误退新会话：布局阶段绑定凭据、隔离凭据来源与用户、入口使用稳定状态持有容器，迟到响应按请求所属工作区判断。真实热更新复验 56 次请求无 401/5xx；相关 166 项测试通过，完整前端结果见任务证据。浏览器整页刷新仍缺规范要求的 HttpOnly Cookie 会话，其他质量及异常流程保留。详情见[任务证据](evidence/BUSINESS-REMEDIATION-20260914.md)。状态仍为进行中，未合并、未部署。

## 2026-09-15 — JOINT-RETEST：R52—R71技术交付与验收边界

本节仅包含技术结论。完整测试账号、任务标识、账务及供应商消费证据保留本机，不随本次PR新增外发。开发使用独立worktree与隔离PostgreSQL，未部署、充值或对外发布。当前为部分业务链路复验通过，不代表整体REAL_CHAIN_VERIFIED或PRODUCTION_GO。

| 编号 | 问题及修复 | 验证 |
|---|---|---|
| R52—R57 | 心跳刷新不再误判慢请求会话替换；导入刷新标题；浏览器使用正常HTTP下载；窄屏导航可关闭；完成状态停止等待；过滤未归档虚拟参考并在弹窗显示错误。保留撤销、切换及资产归属边界。 | 真实导入、下载、导航与任务展示复验；相关自动化通过。 |
| R58 | 视频草稿使用工作区保存，避免误调要求正文非空的文案接口。 | 前端保存与重新登录恢复通过。 |
| R59 | 未开放参考模式提供刷新开放状态入口。 | 自动化通过；管理开关翻转后的现场按钮复验待补。 |
| R60 | 允许有权限的已批准人物图及口播音频作为参考，仍要求可用存储与归属。 | 图片、视频、音频混合参考真实创建完成。 |
| R61 | 云归档使用60秒操作等待；响应不确定时只读核对，避免自动重复生成或扣费。 | 真实成片保存成功；慢响应和响应丢失回归通过。 |
| R62 | 混合素材全局编号编译为按媒体类型编号；保留原提示词快照。 | 请求映射、混合顺序及边界回归通过，真实请求已核对。 |
| R63 | 确认摘要展示当前视频提示词；无项目任务正确显示独立创作。 | 真实界面复验通过。 |
| R64 | ASR临时文件改用已有授权项目目录；失败清理保留记录并退避。 | 真实提取、文案保存及终稿确认通过；失败释放与成功结算符合账本。旧目录清理仍保留待办，不冒称完成。 |
| R65 | ASR完成/失败后刷新钱包，保持权限代际保护。 | 真实完成页直接显示最终余额；成功/失败回归通过。 |
| R66 | 选择器将已解析的云图片预览传回创作页；草稿仍保存资产ID。 | 首尾帧恢复可见；即时预览回归通过。 |
| R67 | 批次依子任务状态显示进行中，避免供应商已受理仍出现排队/取消。 | 同一在途真实批次自动恢复正确显示，无重复提交。 |
| R68 | 未命名独立任务显示提示词摘要，手动标题优先；不改写历史数据。 | 真实任务详情及列表通过，PG列表/详情及命名优先级通过。 |
| R69 | 视频上传解码探测并保存实际时长。R2V预扣前验证单段2–15秒、视频/音频各累计15秒、混合12项；前端校验已知累计时长并提供整理及明确错误提示。未知时长由后端拒绝并提示重传。 | 原12秒视频重新上传成功；与4秒成片组合被禁用生成，移除后恢复；选择器拒绝超额追加；旧素材缺时长请求未建批、未预扣。 |
| R70 | 个人中心流水改按数据库记账序号倒序，与管理端一致；历史无序号记录保留原时间/ID回退。修复同秒结算、退回因随机ID而颠倒的问题，不改写账本。 | UI先复现；PG回归先失败后32项通过，覆盖两端同序、分页及业务过滤；原账户两笔同秒流水UI复验已纠正，余额与流水条数不变。 |
| R71 | ASR完成回填时显式保留未保存文案标记，避免切换视频复刻后被空项目脚本覆盖。 | 真实UI发现正文清空；从既有转写记录恢复，无重复转写。新增跨页面集成测试先失败后通过；三文件209项、类型和Biome通过。重新登录恢复原文及正文，往返视频复刻仍保留。 |

实际业务覆盖：链接导入/AI拆解、文生视频、混合参考生视频、上传视频提取文案、首尾帧2K双条生成、完整音频数字人口播、成片播放/归档/浏览器下载、个人明细与后端账本核对。2K两条输出均完整播放，实际4.458333秒、浏览器显示1463×2560；其中本地下载文件编码1440×2560、SAR64:63、DAR4:7，尚非严格9:16。音频口播成片完整播放24秒、864×1040。已在系统默认下载目录找到两条此前由浏览器下载的测试成片（2K及15秒），文件大小与SHA256匹配归档，FFmpeg完整解码通过。

质量边界：R2V仍未满足“只替换人物、背景/构图/字幕保持不变”，不能以生成成功视为质量合格。建议严格场景保持采用人物区域跟踪/局部替换后合成原背景及原音轨；该方案当前尚未实现。没有听觉证据证明口型、台词和音色完全一致。

R69参考约束依据：[MiniMax官方模型卡](https://huggingface.co/MiniMaxAI/MiniMax-H3)。图片产品上限8保持，混合总上限独立为12。新增视频解码探测复用已有组件，不改变音频验证标志，也不伪造或批量补写历史元数据。

验证证据：前端初轮100文件1472项及类型/Biome/e2e lint/Tauri检查通过；最终工作区/创作页/状态208项、API104项通过。后端Ruff、Mypy147文件通过；本轮ASR/状态PG73项通过，R69独立创建/内容资产矩阵79项通过，补充边界后独立创建/请求模型47项通过。早期全量PG分片2176通过、1旧矩阵失败、1原有跳过；矩阵修正相关4项通过，后续按受影响专项复验，未虚报再跑全量。提交门禁以当前PR头为准。代码评审为开发者自检，未冒称独立评审。

管理端已恢复登录，重启期间遗留的配置502在刷新后恢复；新增ASR、2K与音频口播账务已与后端及个人中心逐项核对，临时H3扩展模式已通过管理UI恢复关闭。未保存文稿跨页面保持完整。

剩余工作：严格9:16输出（需校验像素比例并等比补边）；严格人物替换效果；部分历史素材可用性/时长与预览；参考详情信息；取消等异常分支；视频号归档/前端可见及少量缺失统计（已确认历史记录无媒体任务、关键词为空、缺少单条归档入口，不能用全平台重采代替）；供应商实际费用对账。曾观察素材选择后钱包读取失败，实际账本未改变，需继续核查读取链路。主分支合并仅交付已验证修复，不能当作全业务完成。

## FIX-ASR-400-20260914 — 本地音频传递与请求参数

本地短音频以 Base64 直传，云存储保留签名 URL；修正 Flash 必填音频格式和采样率类型，预检先于付费尝试。单次真实请求已返回 HTTP 200，并发现解析器独立音频可能是背景音乐；抖音、小红书的文案提取现统一使用完整视频内音轨。最终 Linux 静态门通过（1418 前端），全量 PG 2048 passed / 1 既有 skip。[证据](evidence/FIX-ASR-400-20260914.md)与[同类问题排查](媒体云端可访问性排查-20260914.md)区分代码修复、环境准备和真实付费验收。

## FE-VIRAL-ACTIONS-20260913 — 爆款主操作与剩余工作

爆款列表/收藏/首页精选统一详情及提取文案；详情移除复刻；自检修复新项目/素材交接。专项 245 项及完整本地门禁通过（前端 1413、后端 1971，1 既有 TLS skip）；PR/main 最终结果以 GitHub 记录为准。[证据](evidence/FE-VIRAL-ACTIONS-20260913.md)与[剩余 14 项任务](前端剩余工作任务清单-20260913.md)分别记录代码与实际验收边界。


## FE-CHARACTER-SCENES-20260913 — 人物形象与场景任务体验

用户截图反馈的入口、进度、场景刷新、照片布局、多行定位输入和素材按套展示，见[任务证据](evidence/FE-CHARACTER-SCENES-20260913.md)。代码自检、本地全量 PG 1953 passed / 1 原有 skip 与前端验收通过；正面封面、视角选择及卡片对齐记录见证据；未调用真实图片供应商，未修改其他任务的本地联调环境。

## VIRAL-CLOUD-MEDIA-20260913

AUTOMATED_VERIFIED；每日/每周采集、人工精选/删除、云归档、逐请求批次账单已实现，两位独立终审通过；后端专项 104 项、相关页面 17 项通过。已集成最新 main ce798f1，本地完整静态1418项通过；全量2010通过/19失败/1既有skip，失败修复后相关173项通过；[PR #100](https://github.com/peihr666-max/xiangshu-video-replica-/pull/100) 三门禁与合并待核验。[完整证据](evidence/VIRAL-CLOUD-MEDIA-20260913.md)。



## FIX-VIDEO-LINK-20260913

FIX-VIDEO-LINK-20260913：链接媒体预检修复、代码自检和本地质量检查已完成，进入 PR 三门禁。一次授权解析诊断已使用，后续复验复用缓存；不启动转写或视频生成。见[证据](evidence/FIX-VIDEO-LINK-20260913.md)。 本地前端1400通过；后端全量1969通过/1临时口令误报/1原有skip，受影响文件更换合成口令后84通过。

## ADMIN-R01-20260913 — 管理请求与会话契约

[独立证据](evidence/ADMIN-R01-20260913.md)：撤销 CSRF、401 登录过期与字符串业务错误；RED 5 failed，迟到 401 自检再复现 2 failed；最终管理专项 186、完整前端 1399、后端 1952 passed / 1 原有 TLS 环境 skip。本地完整门禁通过，独立评审和 PR 三门禁待完成。

## BILLING-20260913 — 逐项计费与经营核算

用户确认取消套餐、按功能逐项扣分；未配置费用由平台承担。实现、独立终审与本地完整自动验证已完成（1383 前端 / 1952 后端，1 原有 TLS skip），[PR #94](https://github.com/peihr666-max/xiangshu-video-replica-/pull/94) 三门禁已通过并正常 squash 合并（3f77c61c0b6d5e39fb4ef71965619353655ce81e）；未做历史迁移或生产切换。见[BILLING-20260913-EVIDENCE.md](evidence/BILLING-20260913-EVIDENCE.md)。



## JT2-20260913 第二部分隔离联调记录

JT2-20260913：用户新授权下已完成28项不同范围隔离检查（管理9、导出3、Worker3、网络9、存储协议4），覆盖原40行中的11行且含部分验证/重复引用；18行仍有代码或最终制品前置，11类人工输入单列。未声明原40行或任何生产整体验收完成。证据见[JT2-20260913](evidence/JT2-20260913.md)。

## UC 第一批 / CW-077 — 在制验证

2026-09-12：账号注册登录、无限多设备独立会话、工作台修正和小红书导入增量。Linux 静态门及 1346 前端测试通过；真实注册→自动登录→主界面→头像资料通过；后端全量 2872 passed / 1 既有 TLS 重复覆盖 skipped；真实浏览器 4 passed，PR/CI/合并未完成。详见 [批次证据](evidence/UC-BATCH01-ACCOUNT-ACCESS.md)，不将该记录视作上线结论。

> 当前执行清单已更新为[本地实现去重V3](../outputs/customer-cloud-convergence-analysis-2026-09-08/v3/客户版收敛剩余任务清单与验收完工标准-V3.md)：57项剩余排程，复用既有代码；原60项及CW-006/008/011保留追溯，当前状态仅见任务账本§18。此更新不代表代码或数据迁移已完成。

## PostgreSQL全面统一文档评审（2026-09-08，定义更新）

本轮基于分析/任务定义提交822a3b8，确认全环境及所有业务数据库测试使用PG，新增PG-01—12合同；原CW-001—052保留，细化为60项工作定义，实施状态在任务账本§17。新增任务与文档检查不提升T03—T09、T45等历史证据等级。

交付：[文档评审](../outputs/customer-cloud-convergence-analysis-2026-09-08/PostgreSQL全面统一文档评审报告.md)、[统一规范](PostgreSQL唯一数据库实施与验收规范.md)、[任务与验收V2](../outputs/customer-cloud-convergence-analysis-2026-09-08/客户版收敛详细任务清单与验收完工标准.md)、[定义校验](../outputs/customer-cloud-convergence-analysis-2026-09-08/task-list-validation.json)。只验证文档；未执行应用PG全套、真实数据导入或生产切换。后续每项关闭按§14记录测试类别与数据路线、PG版本/head/隔离ID、代码和制品SHA及实际结果。

> 2026-09-08 PostgreSQL 全面统一增量：用户已确定开发、业务数据库测试、CI、staging、生产均使用 PostgreSQL；SQLite 仅限精确登记的离线历史输入、归档与兼容工具。
> 实施与验收以[唯一数据库规范](PostgreSQL唯一数据库实施与验收规范.md)及 CW-001—060 为准。此前仅客户生产 PG、默认开发 SQLite、SQLite 业务测试可作为当前验收的口径不再适用。
> 本次更新只确认规范和任务定义；原代码仍有 SQLite 分支，历史任务/测试记录保留原文，不据此声明实际迁移或生产切换已完成。

## COORD-W6-PHYS-EXIT-20260912 — W6 物理退出批次（039 手册定版 + 040/041/042-b 物理删除，2026-09-12，owner 签认 D5）

决策正本 [COORD-W6-PHYS-EXIT-20260912.md](evidence/COORD-W6-PHYS-EXIT-20260912.md)：前置已满足（039 手册本批次定版 + 040/041 入口关闭证据已随 #77 合入），物理删除提前至 pre-GA 执行；GA 触发义务（039 演练/CW-051 停写）不变。**范围实测修正**：internal_billing=活账务核心不删；db.py/backup.py=CW-060 operator 闭包保留。交付：`docs/切换回滚手册.md`（039 定版）；040-b 发行面五类制品删除+CI filter 收缩；041-b internal_accounts 删除+防复活钉；042-b SQLite 运行时全面摘除（db_portable/6 消费者/settings keystore/lifespan/三把锁/cw060 注册表/35 套件退休）。证据 [CW039-EVIDENCE.md](evidence/CW039-EVIDENCE.md)、[CW042B-EVIDENCE.md](evidence/CW042B-EVIDENCE.md)。

## COORD-W6-UNBLOCK-20260912 — W6 清理组解锁决策 + 批次实施（2026-09-12，owner 签认）

owner 决策正本 [COORD-W6-UNBLOCK-20260912.md](evidence/COORD-W6-UNBLOCK-20260912.md)：D1=CW-042 拆 042-a/b（照 CW042-SCOPE-INVENTORY §2.4 申请签认）；D2=CW-040/041 pre-GA 范围沿 CW-001 P1 落地为「入口 fail-closed」；D3=CW-039 拆「手册定版（纯文档可先做）/真实演练（维持 GA 冻结）」不记 N/A；D4=单 worktree 统一批次开发授权。CW001 §6 增 P3 行、§7 增签认行；排班清单 §2.4 四行备注、账本 §18 五行同步。背景：039 前置全为 GA 触发/环境阻塞，pre-GA 结构性无法闭环（盘点 §2.2），是 W6 清理组唯一死闸门。

## CW-042-a — SQLite 回退入口进程级 fail-closed（2026-09-12，AUTOMATED_VERIFIED）

批次分支 `feat/customer-v3-w6-unblock`，基线 `origin/main@55220f7`。唯一生产码改动 `server/app/db.py`（+31）：`_refuse_sqlite_in_customer_production` 挂 connect/initialize/upgrade 三入口（常量与既有三处声明同款，bootstrap 反向 import 成环故不引入）；TDD RED 9 failed→GREEN 14 passed（[CW042A-EVIDENCE.md](evidence/CW042A-EVIDENCE.md)）。内部 lane 回归 test_db+local_settings_key 35 passed；CW-056 internal-lane 迁移锁不受影响（alembic 直连不经 app.db）。

## CW-041 — 内部身份入口 fail-closed（pre-GA，2026-09-12，AUTOMATED_VERIFIED）

零生产码改动；新增 `test_cw041_internal_identity_exit.py` 5 用例：internal_accounts CLI 客户生产态拒绝（真实缺口——该 CLI 从不运行 lifespan，此前无任何生产守卫，由 042-a 扼流点闭合）+ 内部 lane 端到端保持 + CLI-only 结构钉（app 零导入方）+ 解析器契约；既有四层守卫（cw026/cw025 lifespan/identity 门/数据级）登记引用不重复。见 [CW041-EVIDENCE.md](evidence/CW041-EVIDENCE.md)。

## CW-040 — 内部发行/专属运维入口 fail-closed（pre-GA，2026-09-12，AUTOMATED_VERIFIED）

零生产码改动；新增 `test_cw040_internal_release_exit.py` 7 用例全离线：NSIS 三重扫描标记集契约锁、packaging_tools 仅 paths-filter 提及禁执行、npm/签名通道/deploy/customer 零内部制品引用、backup CLI 生产态拒绝 + 内部 lane 端到端保持。入口面实测表与 CI filter 联动处置登记见 [CW040-EVIDENCE.md](evidence/CW040-EVIDENCE.md)。

## CW-043 — A4 独立复核签署（2026-09-12，非实现会话）

按 §E.3 核验包独立复跑（ZCode 会话，实现者为 Qoder 会话，满足独立性）：12 新用例 + §B 五文件 82 用例 = **94 passed @ vs-pg-cw043a@5444**；全量复跑 42F 归因（22 pitr 平台 + 17 viral 滚动日期炸弹[FIX-TESTBASE #78 已修] + 3 例 W6 回归已修复）与文档核验见 CW043-IMPLEMENTATION-EVIDENCE.md §F 签字段。

## CW-063 — 管理端 h3_extended_modes_enabled 开关（缺口 4b 控制面补齐，2026-09-11，AUTOMATED_VERIFIED）

分支 `feat/customer-v3-cw063-h3-extended-modes-toggle`，基线 `origin/main@a093f61`（开工即最新、`merge-base --is-ancestor` rc=0 无需 rebase）；独立 worktree + 原子认领 `.git/codex-task-claims/CW-063/`（六层查重确认独占）。补齐缺口 4b：数据列 `runtime_settings.h3_extended_modes_enabled`（075 迁移 `server_default FALSE`）与消费逻辑 `independent.py:108-112` 基线已就绪，唯管理端读/写端点 + UI 从未实施。交付：①后端 `admin_runtime_routes.py` +144——Pydantic `H3ExtendedModesResponse`/`H3ExtendedModesUpdateRequest(AdminWriteContract)`（:61-91）、`GET /api/control/settings/h3-extended-modes`（:451-467，AdminReader）、`PATCH`（:470-547，AdminWriter + `write_with_idempotency` + upsert 兜底空表 + `audit_logs` action='runtime_settings.update' metadata.setting='h3_extended_modes'），完全复用 queue-mode 模板；②前端 `H3ExtendedModesSection.tsx`（116 行，镜像 `QueueModeSection.tsx`）+ `api.admin.ts` `fetchH3ExtendedModes`/`updateH3ExtendedModes`（+39，复用 `adminWrite`）+ `SystemSettingsPage.tsx` services tab 挂载（+2）。TDD 先红后绿：后端 RED（端点缺失）→GREEN、前端 RED（`Failed to resolve import ./H3ExtendedModesSection`）→GREEN 3 passed。**真实 PG16 首跑暴露 4 处测试自身缺陷（非生产码）逐项根因修复**：`_audit_rows` 误用 `metadata_json->>'setting'` 于 `sa.Text()` 列（迁移 001，非 jsonb）→ `UndefinedFunction: operator does not exist: text ->> unknown`，改为按 action 查询 + Python `json.loads` 解析过滤；`requires_reason`/`requires_confirm` 期望 422 但共享 `require_write_contract`（`admin_write_contract.py:77-87`）对空 reason/confirm=false 抛 `http_error(400)`（参考 `test_admin_rate_routes.py:167/175` 同断言 400），修为 400 + `detail.code` 校验。验证：后端 pytest **10 passed**（真实 PG16 `vs-pg-cw063@5442`、专属库 `cw063_h3_extended_modes_test` alembic head + admin_u/auditor_u 种子、fcntl shim 仓外注入不入库）+ ruff check/format --check + mypy 全绿；前端 `vitest src/admin` **22 文件 141 passed** + biome 4 文件 clean + tsc -b rc=0；`pg_test_kit.py` +5 仅登记 allowlist（纯增量，与 CW-056/058/059 同款）。安全可观测：读写分离、auditor 403、无 session 401、写契约（confirm+reason+Idempotency-Key+CSRF）、幂等重放不重复写审计、每次翻转留 audit_logs、PG 不可用 fail-closed 503；零迁移改动、默认关不变。诚实边界：4a 真实付费 H3 探针属 §15 人工授权（需真实 metaso key + 预算 + 人工），本任务只交付控制面、默认关上线零风险、不声称探针已跑；与 CW-056（independent.py 在制）文件零重叠；本机仅跑受影响专项 + 静态门，全量 pytest 与三门禁最终归 push 后 CI。完整 §14 记录见 [CW063-EVIDENCE.md](evidence/CW063-EVIDENCE.md)。

## CW-032 — 补齐可重建后端交付包及配置检查（唯一默认部署包，2026-09-11，AUTOMATED_VERIFIED）

分支 `feat/customer-v3-cw032-rebuildable-backend-package`，基线 `origin/main@d49f851`（CW-060 #27 合入后）；八项前置 004/019/025/027/031/056/057/060 均已在 main。交付：①新增唯一默认交付包 `deploy/customer/compose.yaml`——拓扑恰为 rollout SERVICES∪{db,migrate}，`migrate` 为唯一 schema DDL 角色（一次性 `alembic upgrade head`；api/worker 以 `depends_on: service_completed_successfully` 等待且服务块零 alembic=多实例不竞争改 schema）；api-1/api-2 绑定 `127.0.0.1:8001/8002` 与 nginx `customer.conf.example` upstream 交叉一致；db `pg_isready`、api `/health` urllib 容器健康检查；`max_connections=120`+`superuser_reserved_connections=3`；连接池预算 6 进程×`DEFAULT_POOL_MAX`(8)=48+1+3≤120 以 `app/db_pg.py` 活值机器断言（注释+README+测试三处同源）。②`deploy/customer/bootstrap-base-image.sh`——空白宿主机首镜像（干净基底+`uv sync --locked`+compileall+ffmpeg/ffprobe+四 import 检查+CW-060 实物扫描），配合 README `docker compose up -d` 序列实现空白隔离目录仅用登记制品重建 API/Worker/Admin。③rollout `COMPOSE` 默认指向库内 compose（`CUSTOMER_COMPOSE=` 兼容旧主机文件），BACKUP 段新增 `compose.sha256`，与镜像三标签/BACKUP-SHA256SUMS 构成制品哈希链。④正式包目录封闭清单（compose/README/bootstrap 三文件）机器断言，internal-P0/SQLite backup/operator 工具名仅允许出现在排除声明（正式单元 SQLite backup=0；物理退出仍归 CW-040/042）；首管=`provision-empty-customer --confirm-empty-database` 命令与实现逐字一致；配置 fail-fast 矩阵（PG DSN/根密钥/私有 COS/ffprobe/ffmpeg→既有可执行验收落点）机器断言登记。验证：新增专项 8 用例全离线全绿；交叉回归 rollout（CW-019 契约）+internal_deployment+cw057+cw060+bootstrap门+docs/HA/pitr 套件 161 passed/1 skipped；ruff/format/mypy/secret 全过。诚实边界：空白环境重建与 rollout 全流程未在真实宿主机执行（命令契约面闭合，实物归 CW-047）。完整角色/预算/矩阵见 `docs/evidence/CW032-EVIDENCE.md`。

## CW-028 — 迁出共享设置工具并核销旧路由消费者（2026-09-11）

分支 `feat/customer-v3-cw028-shared-settings-migration`，基线 `origin/main@8ab85c7`。差额定位：`control_routes.py:41` 直接 `from app.settings_routes import` 六个共享工具（ProviderTester/ProviderTestResult/get_provider_tester/merge_provider_config/remove_cos_lifecycle_rules/require_supported_provider），管理控制面反向依赖 admin 设置路由模块。迁移：九个共享符号（六符号+Noop/Hifly/Storage 三测试器类+HiflyAccountProbe）落 `app/settings.py` 唯一中性实现——`app/hifly.py:25` 顶层反向导入 app.settings，HiflyProviderTester 默认客户端工厂与 Hifly 异常改为调用期惰性导入破环（可观察行为不变）；storage 无反向依赖保持顶层导入。settings_routes 仅再导出自身消费的六符号（`is` 身份断言钉死单一实现），测试器实现类不再从路由模块暴露；control_routes 源码零 `settings_routes` 引用（源码扫描断言钉死）。新增 `tests/test_cw028_shared_settings_contract.py` 34 用例：RED（ImportError）→GREEN；merge_provider_config 四象限合同（普通覆盖/掩码回传保留原值/空白密钥保留/空白普通字段删除）、require_supported_provider 大小写归一+422 UNSUPPORTED_PROVIDER、get_provider_tester 三层组合（Storage→Hifly→Noop）、remove_cos_lifecycle_rules 四分支（removed/skipped×2/failed，monkeypatch 目标 app.settings.create_storage_adapter）、Noop paid_test 501、ProviderTestResult exclude_none。回归：test_settings 86 passed（含 4 处 monkeypatch 目标改指 app.settings）；test_internal_admin 22 passed；test_admin_auth+cw027 权限矩阵+合同文件在 Docker Linux（python:3.12-slim + PG16.15 vs-pg-cw028 独立网络，pg_test_kit fcntl 为 POSIX-only 与 CI Linux 同环境）**131 passed/0 fail/0 skip**；ruff/format/mypy strict 104 files 全绿。逐端点消费者清单与兼容允许清单（冻结供 CW-041）：/api/control 13 端点与管理 /api/admin/settings 9 端点全部有客户端页面/封装级消费者保留；唯一 CW-041 复核候选=POST paid-test（无客户端封装、服务端恒 501 存根）；GET /api/control/accounts 页面级无调用登记「保留兼容待复核」不标可删；旧通道 /api/wallet、/api/wallet/transactions、POST/GET /api/recharge-orders、GET /api/recharge-orders/{order_no} 为 WalletPanel 受支持回退路径（App.tsx:423 / LiveWorkspacePanel:155）保留兼容，替代路径 /api/recharge/customer/*，CW-041 取消注册前须先收敛客户端回退挂载。本任务零取消注册、零路由删除（取消注册归 CW-041、源文件删除归 CW-042）。逐项断言与 §14 记录见 `docs/evidence/CW028-EVIDENCE.md`。

## CW-060 — 隔离既有历史SQLite工具与兼容测试（PG-09 operator 制品化，2026-09-11，AUTOMATED_VERIFIED）

分支 `feat/customer-v3-cw060-isolate-historical-sqlite-tools`，基线 `origin/main@8ab85c7`（CW-057 #19 合入后）；前置 CW-005/007/053/056 均在 main；用户授权由本会话开发该前置以解锁 CW-032。历史算法零重写零字节改动（`sqlite_to_postgres.py`/`reconcile_customer_billing.py`/`app.backup.py`/`app.db.py`/`migrations/**` 只读纳入）。交付：①`deploy/operator/build_operator_package.py`——仅标准库构建器，把注册传递闭包（两个历史 CLI + backup/db/db_pg + migrations + alembic.ini + 锁定的 pyproject/uv.lock）组装为可复建 operator 制品，`manifest.json` 绑定来源 commit、AST 求出的唯一 Alembic head（多头/缺父 fail）与逐文件 SHA-256，`manifest.sha256` 锁定本体，同树两次构建字节相同；②`deploy/customer-git-rollout.sh` build 段——上下文 `rm -f` backup.py + 违禁路径循环 fail-closed + 镜像内 `! test -e` ×3 与 rglob 实物扫描（命中即构建失败），`server/scripts/` 从未进入客户构建；③`server/tests/test_cw060_operator_isolation.py` 7 用例（可复建/三方绑定/业务面排除/rollout 契约/`app.db`+`app.backup` 反向消费者冻结注册表 9+1 文件/TEST-IMPORT·HISTORY 硬门契约/构建器无 app 导入）；④TEST-IMPORT（`test_sqlite_to_postgres` 的 `require_pg_or_explicit_skip` 硬门）与 TEST-HISTORY（`test_migration_dialect_contract` 纯 AST + `LEGACY_EXEMPTIONS` 不得增长）登记为独立报告。验证：专项 7 passed；交叉回归 rollout（CW-019 契约）+internal_deployment+cw057+TEST-IMPORT 真实 PG 共 93 passed；ruff/format/mypy 全过；零新增 PG 资源。诚实边界：docker build/实物扫描未在真实发布窗执行（脚本契约+既有测试兜底并登记）；无源码主机 one-shot job 形态留 CW-035/036 触发。完整例外清单与制品契约见 `docs/evidence/CW060-EVIDENCE.md`。


## CW-024 — 补齐安全升级和唯一签名发布流程（桌面发行链，2026-09-11）

分支 `feat/customer-v3-cw024-signed-release-secure-upgrade`，基线 `origin/main@ca438b9`（＝CW-023 #25 squash 后；独立 worktree + 原子认领 `.git/codex-task-claims/CW-024/`，开工查重无 cw024 分支/PR/claim）。前置 CW-021/022/023 均已入 main，CW-003/005 决策已签认。复用 CW-021 的 hook 失败阻断与 CI 无签名构建/SHA 归档，仅做三项剩余差额：**①旧安装路径全覆盖+数据保护前置**——CW-003 冻结版本按 git 史实实际有两条旧安装路径（0.1.12/0.1.13/0.1.15 与 0.1.16#92＝`$LOCALAPPDATA\短视频复刻工作台`；0.1.16 W1 品牌替换 1fb997a＝`$LOCALAPPDATA\众墅之家`，均 NSIS currentUser），原 hook 只覆盖前者且无归档前置直接静默卸载；`customer-installer-hooks.nsh` 重写为「存在检测→运行实例守卫（写模式打开旧版 uninstall.exe，运行中映像被锁即失败→Abort 零改动）→CopyFiles 整目录归档到 `%LOCALAPPDATA%\短视频复刻客户云工作台\legacy-backup\`→IfFileExists 校验+写 LEGACY-BACKUP-MANIFEST.txt→才 ExecWait 静默卸载」，任一失败 Abort 且提示备份位置（失败可恢复旧客户或保留可读数据）；凭据命名空间（DPAPI 信封+注册表镜像）在安装目录外零触碰。**②唯一签名发布流程**——新增 `scripts/release/build-customer-signed-release.ps1`（`release:customer` 入口）：缺 `VIDEO_REPLICA_RELEASE_SIGN_THUMBPRINT`/`VITE_API_BASE_URL` 即 throw，六处版本一致性门，签名输入仅经 BOM-free 临时 overlay（certificateThumbprint/sha256/timestampUrl，构建后即删），构建后 `Get-AuthenticodeSignature` 必须 Valid 否则不出制品，产出含 `release-manifest.json`（channel=signed-release + 版本/windows-x86_64/签名/SHA256）；**③未签名包只能标内部测试**——ci.yml 归档目录改 `customer-cloud-internal-test-unsigned` 并写 `RELEASE-CHANNEL.txt` 渠道标签，合同断言 workflow 零签名材料（TAURI_SIGNING_PRIVATE_KEY/certificateThumbprint/signtool），`test_build_contracts` 计数断言零破坏未改 needle。先红后绿：新增 `test_cw024_signed_release_upgrade_contracts.py` 9 用例（纯静态无 PG），RED 8 failed/1 passed（通过项=凭据命名空间，现状本就不触碰）→GREEN；CW-021 既有 hook 字符串合同（ha_smoke sole_default）1 passed 保持；新增运行手册 `docs/客户版桌面升级与签名发布手册.md`（支持版本×旧路径矩阵/升级行为矩阵/回滚与恢复/签名发布步骤/凭据边界/CW-046·CW-051 分界）；ruff/format/mypy 104/secrets 全绿；本机无 makensis，hook 真实 NSIS 编译以 CI Windows 门承载。真实签名实机验收归 CW-046（证书采购为 §16 DESK-04 人工待办）、真实旧数据切换归 CW-051。§14 模板记录与验证明细见 `docs/evidence/CW-024-EVIDENCE.md`。

## CW-029 — 复用账务核心并核验扣费、支付差额（TEST-PG 多连接矩阵，2026-09-11）

分支 `feat/customer-v3-cw029-billing-pg-matrix`，基线 `origin/main@1ad1f31`。按「仅做剩余」复用 `internal_billing.py`（RESERVE/SETTLE/RELEASE + billing_round 幂等）与 `zpay_payments.py`（回调确认）**零生产代码改动**；差额定位为：既有 `test_payments.py`（回调验签/重复/跨单 21 用例）仍跑在内部遗留 SQLite 通道，而 `test_wallet_billing_service.py` 虽在 PG 但未覆盖支付回调面——本任务把支付+任务状态合成矩阵迁上 TEST-PG（专用库 `cw029_billing_pg_test`，隔离容器 5439）。新增 `test_cw029_billing_pg_matrix.py` 7 用例：①支付成功按订单快照精确入账，重复同一签名回调 success 但账面不变，乱序（同单不同 trade_no）409；②六类伪造回调（坏签名/错 pid/非 SUCCESS/金额不符/未启用渠道/未知订单）全部 4xx failure 且 wallet+ledger 与基线逐字节相同、订单保持 PENDING；③同一 provider_trade_no 绑第二用户订单 409 ZPAY_TRADE_ALREADY_BOUND、user_2 零入账；④客户「删除」（CLOSED）订单保留行且迟到支付回调照常结算——客户隐藏不抹对账证据；⑤下单后改 runtime_settings 单价 1000→5000，回调仍按订单 charged_unit_price_fen_snapshot=1000 入账 10 条（历史快照不可被现价改写）；⑥任务结果重复 finalize 幂等（返回已记录 SETTLE 不加行）、终态后乱序失败结果 no-op；⑦CANCELLED 释放一次、重复释放 no-op，未知提交（RUNNING）被悬挂 sweep 显式排除（scanned=0）预留冻结。每用例断言四套账：available=初始+Σavailable_delta、终态轮次 reserved 与 Σreserved_delta 双回零、账务行数精确值。新增 7 passed；wallet_billing+internal_billing 35 passed 零回归；secret/ruff/format/mypy 全绿。真实 ZPay/Provider 回调归 CW-050；口播侧由 CW-010 基线与 test_oral_domain 61 用例承载；未做分叉分支整体 cherry-pick。逐项断言与 §14 记录见 `docs/evidence/CW029-EVIDENCE.md`。

## CW-027 — 复用管理后台并收口权限差额（复验，2026-09-11）

分支 `feat/customer-v3-cw027-admin-console-permission-matrix`，基线 `origin/main@eea767e`。按 V3 规格「无需预设重写后台鉴权；先按保留操作清单核验所有写路由确实使用 AdminWriter/写合同；仅在发现漏接路由时修复」执行。**静态核销表漏项=0**：84 条管理 method-path（control/admin/character-admin 管理面）读、写两轴全部携带管理级权限——WRITE：AdminWriter 22 / control_route_user 6 / settings_admin 7 / character_admin 15；READ：AdminActor 20 / control_route_user 8 / settings_admin 2 / AdminWriter 1（CSV 导出按写级把关，比规格更严）；两条合理豁免（自作用域登出 DELETE admin/session 挂读级即正确且 auditor 可自助登出；会话建立 exchange/password 为全表仅有的无依赖路由）。**未发现漏接路由，零生产代码改动，按规格以验收完成关闭，不另立重构**。动态底线网格（TEST-PG 专用库，8 用例）：管理写成功且 admin_write_idempotency/admin_adjustments 记录真实 actor 与中文 reason、幂等重放 X-Idempotent-Replay 且订单/账务行恰各 1、合同三要素 400、CSRF 缺失/错值 403、auditor 读 200 写 403 AUDITOR_READ_ONLY（自助登出豁免动态钉住，登出后立即 401）、客户凭据对 session 门 401/对角色门 403 双拒、过期交换凭据与登出后会话拒绝、旧 X-Control-Proxy-Token 在非生产 lane 保持 fail-closed 且在客户生产 lane 被完全忽略（401 ADMIN_SESSION_INVALID，永不恢复共享管理员权限）。静态矩阵测试使新增未分类管理路由直接红灯（漏项≠0 即失败）。新增 8 passed；既有管理套件（auth/activation/customer/session/audit/profit/rate/dashboard 215 用例）在 main CI 全量全绿，本机复跑 113 passed 零回归；secret/ruff/format/mypy 全绿。真实生产角色 UAT 归 CW-049；proxy 兼容路径物理删除归 CW-041/042，均未触碰。逐条核销矩阵与 §14 记录见 `docs/evidence/CW027-EVIDENCE.md`。

## CW-026 — 退出内部认证并复验客户 fencing（收敛通道全环境仅认客户会话，2026-09-11）

分支 `feat/customer-v3-cw026-exit-internal-auth-fencing`，基线 `origin/main@e829ad1`（CW-055 #13/CW-054 #14/CW-019 #12 已合入；四项前置 CW-025/009/003/055 均已入 main）。仅做剩余：删除 `authenticate_request` 中「非客户生产 PG 仍接受 internal access token」的 A1 豁免，使收敛 PG 通道在全部环境只接受客户会话 Bearer——缺 Authorization 直接 401 `SESSION_TOKEN_REQUIRED`，`X-Dev-User-Id`、`VIDEO_REPLICA_DESKTOP_USER_ID`、`VIDEO_REPLICA_AUTH_MODE=desktop|development` 的桌面身份旁路在 PG 通道全部不可达（开发头原可经 conftest 默认配置在 PG 通道解析任意用户身份，本次关闭）。SQLite 内部遗留通道（`DB_PATH`）按 CW-021/040/041 排期保留，`test_legacy_sqlite_lane_keeps_internal_and_dev_identity` 反向钉住 CW-026 未越界。增量验收：271 条保留业务 method-path 授权矩阵漏项=0（READ_OWNER 88 / WRITE_FENCE 67 / ADMIN_SESSION 84 / SESSION_LIFECYCLE 19 / PUBLIC_SIGNED 7 / PAYMENT_CALLBACK 2 / PUBLIC_INFRA 4；分类器把「新增未分类路由」变成测试红灯）；TEST-PG 迟到写 fencing 双连接时序三变体（epoch 切换/lease 回拉/注销）全部 401 且 projects/wallet_transactions/customer_fencing_write_evidence 增量均为 0；幂等重放在 fenced 事务内先重验会话——会话被替换后旧令牌同 Idempotency-Key 重放 401 `SESSION_REPLACED`、0 新增订单、封存信封完好，新令牌重放拿 `X-Idempotent-Replay: true` 封存响应。RED→GREEN：还原 `origin/main` 版 `auth.py` 后 5 个新断言精确失败（覆盖四类旧身份豁免与 test_internal_access_tokens 的 false 分支旧行为）；4 个钉住旧行为的既有测试迁移到客户会话/依赖覆盖后断言语义不变（independent_creation saved_prompts、oral_domain 3 用例、internal_access_tokens 参数化）。专项验证 14+148+142+78 passed；secret/ruff/format/mypy 全绿；本机无 ffmpeg/cargo 的环境性非绿与还原 auth.py 的对照运行逐项一致（38 errors 两轮同数），由 CI Linux 门禁承载。管理员 session/CSRF 矩阵与 character-admin 依赖归 CW-027 复验，旧路由最终取消注册归 CW-041，均未触碰。证据层级 `AUTOMATED_VERIFIED`；真实链路归 CW-050。完整矩阵、§14 记录与验证明细见 `docs/evidence/CW026-EVIDENCE.md`。

## CW-021 — 删除桌面本地后端启动与管理资源（2026-09-11，W3 代码与测试增量，AUTOMATED_VERIFIED）

分支 `feat/customer-v3-cw021-remove-local-backend`，基线 `origin/main@6390236`（＝CW-020 PR #18 squash「客户配置唯一默认」合入后的首个后续任务）。独立 worktree `E:/众墅之家爆款短视频创作/.worktrees/CW-021-remove-local-backend`，原子认领 `.git/codex-task-claims/CW-021/`（排班 §3 六层查重：fetch 后无 cw021 分支/PR/worktree/claim，确认独占）。承接 CW-020 在 `Cargo.toml` 留下的「CW-021 retires this feature」承诺与 CW-011 客户构建合同：本地 sidecar 启动链自**源码、配置、脚本、CI 制品检测**四个层面整体退役——`lib.rs` 删除 `local-sidecar` feature 门控全部启动代码（`BackendProcess`/`127.0.0.1:8000` 端口探测/`VIDEO_REPLICA_BOOT_COMMAND` 命令查找/`start_local_services`/退出杀进程；凭据与下载 handler 原样保留），`Cargo.toml` 删除整个 `[features]` 表使任何构建（含刻意 opt-in）都无法再编译回本地后端，删除 `tauri.internal.conf.json` 与 `resources/start-backend.sh/.bat`，`package.json` 删除三个 `:internal` 入口；ci.yml Windows job 收敛为仅构建客户云安装包，制品门禁由「排除本地启动器」**扩大为四层「排除本地后端分发」**（启动脚本/pyvenv.cfg/ffmpeg.exe/ffprobe.exe 文件名、.db/.sqlite/.sqlite3/.pyd 扩展名、server//.venv//ffmpeg/ 目录段、`start-backend`/`VIDEO_REPLICA_BOOT_COMMAND`/`127.0.0.1:8000` 二进制标记 ASCII+UTF-16 双编码扫描）；守卫测试同步重写（`test_build_contracts.py` launcher 执行用例改为「启动器必须保持删除」契约并新增 `test_packaged_local_backend_launchers_are_removed`，`test_customer_ha_smoke.py::test_customer_desktop_build_is_the_sole_default_target` 内部断言全部翻转为「必须不存在」，`test_desktop_artifact_no_pg_dsn.py` 三配置布局改双配置并新增 `test_internal_edition_stays_withdrawn`）。

先红后绿：RED 7 failed（三守卫文件按新契约断言，失败点与 DoD 删除范围逐项对应）→ 实现 → GREEN 15 passed（既有 PG DSN 隔离/origin guard/版本链等不变契约全部保持）。制品检测逻辑本地 PowerShell mock 验证：三类文件违规全命中、ASCII 与 UTF-16LE 二进制标记全命中、合法 devUrl `127.0.0.1:5173` 不误报。静态门禁：`verify_no_secrets.sh` EXIT=0、`ruff check` All checks passed、`ruff format --check` 298 files already formatted、`mypy server/app` Success 104 source files。诚实边界：不触碰旧安装数据（旧内部版卸载迁移钩子保留，升级实机归 CW-046）；`resources/ffmpeg/` 仓库文件物理清理按 CW-001 P1 归 CW-040/042（客户默认 `resources: []` 不打包、无任何构建引用）；cargo 编译与 NSIS 解包检测本机无工具链，以 push 后 CI 三门禁为准（沿 CW-019/CW-020 先例）；全量 pytest 因共享 fixture(5433)被 CW-057 会话在制占用，按排班 §4 PG 资源串行规则等待后执行（结果回填于证据文件）。证据 `docs/evidence/CW-021-EVIDENCE.md`。

## CW-057 — 统一剩余维护与种子CLI的PG入口（2026-09-11，AUTOMATED_VERIFIED）

分支 `feat/customer-v3-cw057-maintenance-seed-cli-pg`，基线 `origin/main@e829ad1`（CW-055 #13 后）；前置 CW-025/053/054 均已合入 main。交付：新增统一 PG 入口 `app.db_pg.resolve_cli_pg_dsn`+`CliDatabaseConfigError`（HTTP 外命令面 PG-01 的唯一 DSN 解析点：`--database-url` 优先、env 回退逐字沿用 CW-025 fail-closed 契约，sqlite:///DB_PATH 残留/错误 scheme 在连接前拒绝、失败零文件创建）；4 个维护 timer CLI 的 DSN 解析块收口（行为与退出码不变）；gate1_bootstrap/gate1_e2e 从 SQLite 转 PG（种子要求 pristine 迁移库 + advisory lock + 摘要脱敏；harness 进程内迁移 + 子进程环境 DATABASE_URL 取代 DB_PATH）；app.backup 与 internal backup systemd unit 注册为 historical-internal-p0（分类横幅 + 客户链零引用守卫；物理退出归 CW-060/040/042，客户包排除归 CW-032）。命令×分类×数据库×角色×写入矩阵以注册表断言固化为机器可核验（scripts/systemd/postgres 工具目录级防漏项扫描）。验证：新增专项 37 用例 + gate1 种子 PG 契约 8 用例（拒绝矩阵于无 PG 环境运行、--dry-run 只读零变化、实写双跑幂等、客户部署链零 SQLite backup 引用、CW-060 两文件零触碰反向钉住）；回归 db_pg/db_portable/bootstrap 门/幂等/ops/pitr 套件 234 passed + security/activation/wallet/fencing 套件 139 passed 零回归；ruff+mypy 全过。诚实边界：Gate-1 Playwright 全链路未实跑（人工门，CW-021 后退役）；全量 pytest 最终判定归 CI。完整矩阵与记录见 [CW057-EVIDENCE.md](evidence/CW057-EVIDENCE.md)。



## CW-022 — 复验原生凭据和旧版本设备身份兼容（2026-09-11，W3 复验，AUTOMATED_VERIFIED）

分支 `feat/customer-v3-cw022-credential-identity-compat`，基线 `origin/main@f193fa7`（＝CW-021 #21 squash 后）。独立 worktree + 原子认领（查重无 cw022 分支/PR/claim）。复验不新造凭据库，三项实质动作：①**修复「Windows DPAPI 专项从未在任何 CI 执行」缺口**——`cargo test` 此前只挂在 Linux 质量门，`#[cfg(windows)]` 的 DPAPI roundtrip/无明文/clear_session/clear_all 4 专项在 Linux 被编译剔除、Windows job 无人执行；windows-nsis job 新增 `cargo test --manifest-path client/src-tauri/Cargo.toml --locked` 步骤（守卫断言先红后绿），DPAPI/注册表 FFI 于 runner 用户会话原生执行。②**补齐注册表迁移路径回归**——`durable_device_instance_id` 的"首次升级启动把 app-data 标识迁入 HKCU"语义此前无测试：新增迁移测试（备份→`durable_identity::clear()`（test-only `RegDeleteKeyValueW` 助手）→legacy id 迁移写入→重启/丢弃 app-data 重装后仍从注册表取回同一标识→恢复），并作为唯一触注册表的测试规避并行竞争。③**损坏 fail-closed 回归锁 + 命名空间钉死**——截断乱序/空信封 → `load` 显式 Err（不泄明文、稳定设备标识幸存、不静默重置）；`customer-credentials.bin`/`device-instance-id` 文件名与 `Software\Xiangshu\VideoReplicaCustomer`/`DeviceInstanceId` 注册表命名空间钉死（CW-003 §6 冻结落实，两常量改 `pub(super)` 供测试引用）。矩阵登记：签名包实机升级/重装归 CW-046（原边界），macOS Keychain 按 CW-002 签认（仅 Windows 10/11 x64）登记为超范围保留，epoch/slot 服务端不变量复用 CW-009/026 既有套件，前端 logout/清理合同（CW-017）由既有 vitest 1296 用例承载（本任务前端零变更）。证据 `docs/evidence/CW-022-EVIDENCE.md`。

## CW-023 — 补齐下载取消、失败与支持平台差额（2026-09-11，W3 代码与测试增量，AUTOMATED_VERIFIED）

分支 `feat/customer-v3-cw023-download-cancel-failure`，基线 `origin/main@1ad1f31`（＝CW-022 #22 squash 后）。独立 worktree + 原子认领（查重无 cw023 分支/PR/claim）。核心差额修复：**下载失败后残留文件无人清理**——`Registry::finish` 成功规则下沉到状态层（真实存在+位于预留路径+非空，与 `on_download` 回调的检查形成防御纵深），失败且事件路径与预留路径一致时尽力删除残留（路径误报不误删任何文件；删除失败不改变 `DOWNLOAD_FAILED` 上报，供手动处置）。能力边界按 DoD 明确登记：保存框取消=支持（None→`cancelled`，不报成功）；**下载中取消=不支持**（`cancel` 对已开始下载返回「下载已开始」，前端仅未开始时调用）；平台=仅 Windows 10/11 x64（CW-002 签认，非 Windows 显式拒绝不静默降级）；外部对象请求 `credentials:"omit"` 无客户 Bearer（既有合同复验保持）。先红后绿：新增 `a_failed_download_cleans_up_its_partial_residue`（真实临时文件夹具：部分写入失败清理/零字节判失败并清理/成功保留/误报不删）在旧实现 FAILED → 新实现 GREEN；既有 2 测试的成功分支夹具升级为真实非空文件（意图不变，新成功规则下虚构路径不再合法）；容器内 `cargo test --locked --lib video_downloads` **10 passed/0 failed**（rust:1 + tauri Linux 系统依赖自建镜像，本机无 Rust 工具链——此法同时前置拦截了 rustfmt 与编译错误）。前端复验零代码变更：`TaskRecordsPanel` pending 锁防重复点击、cancelled/saved/started 三态、失败与未确认文案均为既有确定性恢复；上传 XHR 进度/abort 与 Bearer 边界为既有合同。实机断网/磁盘满写盘故障场景归 CW-046。证据 `docs/evidence/CW-023-EVIDENCE.md`。

## CW-054 — 修复 PG 批量操作、查询类型与异常差额（Segment 1/N：`db_portable.py` PG-lane 契约，2026-09-10）

分支 `feat/customer-v3-cw054-pg-portable-contract`，原始开发基线 `origin/main@e06b13c`（CW-017 merged）；2026-09-10 收尾期间 main **三次前进**，故 **rebase 三次**：先到 `origin/main@eac6f4d`（CW-018 PR #11），再到 `origin/main@91eab3f`（COORD-SCHEDULE PR #10，即排班与认领规程本身入 main），最后到 `origin/main@0d08608`（CW-019 #12，拆分客户与管理员独立构建制品）。前两次入向改动均**零 server 改动**（`git diff --name-only eac6f4d origin/main -- server/` 为空），两回 rebase 前后 `server` 子树哈希均为 `f94eea30` 逐字节相同；第三次 **CW-019 含 server 改动**（`test_bootstrap_all_env_pg_gate.py`、`test_customer_git_rollout.py`），与 CW-054 改动文件零重叠，rebase 仅 `docs/CUSTOMER-TASK-EVIDENCE-V3.md` 一处冲突（CW-019 节与 CW-054 节并排，已手工解决），rebase 后 `server` 子树哈希为 `86cf6e0e`（含 CW-019+CW-054）。全部 pytest 结果对三次 rebase 后的最终 HEAD 依然有效（不变量单独不充分论证见证据 §5.4）。按清单 §2.2 "可分段推进"授权拆为 Segment 1/N，本 PR 只收口 `server/app/db_portable.py` 的 PG-lane 查询、行类型与约束异常契约面（437→650 行），**不新增任何应用模块、不建泛化多后端抽象**（代码开发清单 §14「CW-025/054」映射行已登记该路径，新测试模块按 §14 末条「先在本节登记确切文件名、消费者、入口/打包边界和责任再创建」规则先登记后创建；引用用行内容锚点而非绝对行号，因第二次 rebase 已使原 L451/L462 漂移到 L474/L485）。核销五处 PG lane 静默失效：`executemany` 原为完全 no-op（注释错误声称 psycopg 无 executemany）且返回 `None`，现真实批量执行并返回携带 `rowcount` 的游标，使"落库行数少于入参"从静默部分写变成失败（刻意不写 `if not seq` 守卫：psycopg3 原生接受空序列返回 `rowcount == 0`，而该守卫对恒真的生成器会误判）；`iterdump` 原返回空迭代器 `iter(())`，现遍历 public schema 用户表 yield 真实 INSERT；`set_trace_callback` 原为 no-op 丢弃 callback，现存储并在 `execute`/`executemany` 时调用（每批一次，偏差已文档化），SQL 审计在 PG lane 首次真正可用；约束异常原泄漏 psycopg 原生 `UniqueViolation`，令 `except sqlite3.Error` 的业务调用者完全漏捕（`source_frames.py:538`、`zpay_payments.py:210` 为生产实证），现由 `IntegrityConstraintError` 双继承 `sqlite3.IntegrityError` + `psycopg.IntegrityError`，使同一条语句失败在两 lane 抵达同一 handler 且无需改写调用点，并携带 SQLSTATE（UNIQUE 23505 / FK 23503 / CHECK 23514 / NOT NULL 23502）与约束名；`_NamedRow` 三处背离真实 `sqlite3.Row` 的语义（未加引号标识符的大小写折叠、`keys()` 返回新 `list`、非 int/str/slice 键抛 `IndexError`）逐轴对齐并由 27 条 parity 矩阵钉住。

两层 RED→GREEN：RED-A（`origin/main` 原样 437 行）`2 errors in 0.12s`、EXIT=2，证明契约符号缺失；RED-B（`origin/main` + 仅声明符号的惰性 shim 451 行）`44 failed, 62 passed in 4.01s`、EXIT=1，逐条行为级红——`TestConstraintExceptionMappingPGLane` 8/8、`TestExecutemanyPGLane` 5/5、`TestIterdumpPGLane` 5/6、`TestSetTraceCallbackPGLane` 4/4、`TestRowAccessPGLane` 4/8、TEST-LOGIC parity 11/27 + 具名 3/4 + mapper SQLSTATE 4/4；shim 的 mapper 故意丢弃 `sqlstate`/`constraint_name`，使“保留 SQLSTATE”仍被 RED 覆盖而非白送；还原经 SHA256 逐字节校验 RESTORE OK。GREEN `106 passed in 4.26s`、0 failed、**0 skipped**（真实 PG 16.15 实跑，无 skip 即证明未以跳过冒充通过）：TEST-PG 49 于专用 `cw054_contract_test` 库实跑、TEST-LOGIC 57 **在故意指向死端口 DSN（`localhost:9/nonexistent`）时仍 `57 passed in 0.06s`**，硬证明 PG-free；三组数字交叉核算 62+44=106、57+49=106、上轮 104+新增 2=106 互相印证。静态门禁 `ruff check` All checks passed / `ruff format --check` 296 files already formatted / `mypy server/app` Success 104 source files；`scripts/verify_no_secrets.sh` exit 0（Git Bash，amend/rebase 后各复跑一次）。

收尾全量 server pytest @5434（仓库外脚本指向**本 worktree**，不复用 CW-055 的 runner——后者指向主仓目录而主仓当前 checkout 在 CW-056 分支上）：`29 failed, 2258 passed, 5 skipped, 38 errors in 2324.90s`。**本任务两个测试文件在 FAILED 与 38 条 ERROR-at-setup 清单中零出现**，即 106 条专项在全量套件内同样全绿（无跨套件干扰）。**本 PR 不声称全量全绿**：67 项非绿已逐组归因为本机 Windows 环境并与本改动无关——38 errors 全为 `MediaToolUnavailable`（本机无 ffmpeg，ERROR 行与该异常行 1:1）、`Get-Command ffmpeg` 为空、`VIDEO_REPLICA_FFMPEG_DIR` 未设；22 failed 为 `test_cw033_pitr_drill_validation.py` 子进程调 `.sh` 的 `FileNotFoundError [WinError 2]`，且该文件对 `db_portable` 零引用；剩 7 failed（`test_simple_character.py` 6 条 `assert 503 == 422` / `SIMPLE_CHARACTER_IMAGE_VALIDATION_UNAVAILABLE` + `test_postgres_migrations.py::test_user_identity` 1 条 `unexpected user devuser`）**做了经验基线而非仅靠结构推定**（两文件确实 `import BusinessConnection`）：把 `db_portable.py` 换回 `origin/main` blob（437 行，SHA256 `D7E8E95B…`）后以**逐字相同断言**同样失败（`7 failed, 4 passed`、EXIT=1），还原经 SHA256 `22BA4E5C…` 校验 `RESTORE_OK`——证明这些失败先于本改动存在。本机无 `cargo` 与 `gh`，`check:tauri` 无法本地执行；本任务零前端、零 Rust、零 e2e 改动，由 CI path-filter 与 CI 自带 `Set up Rust` 承载（经用户确认）。

证据层级 `AUTOMATED_VERIFIED`；CW-054 父任务保持 `[~] Segment 1/N`，**不提升** `STAGING_VERIFIED`。CW-055 禁区（`BusinessConnection.commit`/`rollback`/`close`/`transaction`/`__exit__` 的刻意 PG no-op 语义）逐字未触碰，并由 `TestCW055BoundaryUnchanged`（4 测试）在 GREEN 中反向钉住“未被 CW-054 改动”；CW-056 禁区（`server/migrations/`、`test_postgres_migrations.py`，无 revision 改动）、CW-031（`storage.py`/`media_routes.py`）、CW-042（未移除 `translate_to_sqlite`、未改动 SQLite 在线实现）均未触碰。两处如实记录的发现：`%%` 字面百分号是仓库既定约定（生产 10 处依赖，`PostgresBackend.execute` 总传 params 使 psycopg 恒解析占位符）而非缺陷，改实现省略空 params 会造成静默匹配失效回归，故固化为 `TestLiteralPercentInQueryText` 2 条契约测试；`_NamedRow` 大小写折叠属防御性对齐，现存 0 处 camelCase 访问。类型 roundtrip（9 条）与 rowcount/RETURNING（3 条）在 RED-B 中本就全绿，CW-054 对它们是**加锁而非修复**，已诚实标注。旧版证据（随 `5318ce3`）基于一版含伪通过断言的测试草稿（种子未提交致 `raw.rollback()` 连带回滚、`LIKE 'rt-%'` 未转义），数字与实现描述均失真，已整体重写并在 §9 逐条列明作废项。完整 §14 记录、两层 RED 明细与逐类计数见 `docs/evidence/CW054-EVIDENCE.md`。

### CW-054 Section 14 Ledger Record

```text
任务/工作包：CW-054 / W4 代码与测试增量（Segment 1/N — db_portable.py PG-lane 查询、行类型与约束异常契约）
Owner / Reviewer：后端（Qoder 代理，honor.pei 会话 2026-09-10）/ 待 PR 独立评审
分支 / 基线 SHA：feat/customer-v3-cw054-pg-portable-contract / 原始开发基线 origin/main@e06b13c（CW-017 merged）；收尾期间 main 三次前进，故 rebase 三次：先到 origin/main@eac6f4d（CW-018 PR #11），再到 origin/main@91eab3f（COORD-SCHEDULE PR #10），最后到 origin/main@0d08608（CW-019 #12）；前两次入向改动均零 server 改动，第三次 CW-019 含 2 server 测试文件改动（零重叠），rebase 仅一处 docs 冲突（已手工解决）；单提交，父 = 0d08608 = 当前 origin/main（远端分支此前不存在）；**本提交自身 SHA 不写入证据文件**——记录自己 SHA 的提交在写入瞬间即过期（写 SHA → amend → SHA 变 → 改文档 → amend，无法收敛），head SHA 改由 claim.json 与 PR 登记承载（详证据 §5.3）；server 子树哈希经三次 rebase 由 f94eea30 变为 86cf6e0e（含 CW-019+CW-054 改动）；**跨文档绝对行号引用同样不写**，一律用「§号 + 行内容锚点」，因第二次 rebase 使共享文档行号漂移（账本 §18 CW-054 行 L494→L496、代码开发清单 §14 锚点 L451→L474 与 L462→L485、AGENTS.md 第 5 条 L35→L43，详证据 §5.2.1）
上游规格段落：客户版任务清单 V3 §2.2 CW-054 行（含“可分段推进”授权）+ §18 CW-054 行 + §14 证据模板；代码开发清单 V3 §14「CW-025/054」映射行 + §14 末条「若实现确需新的测试/legacy 模块，先在本节登记…再创建」（两个锚点已从 L451/L462 漂移到 L474/L485，故不写行号）；PostgreSQL唯一数据库实施与验收规范 PG-02（查询与类型）、PG-05（独立专项不互删）；开发顺序排班与Worktree协作清单 §3.2/§4/§5/§6/§7；AGENTS.md 标准工作流第 1/3/4/5/8 条（开工检查、push 前核对、Draft PR 取 CI 证据与三门禁、同 PR 更新账本、多任务并行的端口与 PG 隔离）；客户云版任务认领登记 §1（初始化快照，本任务不修改，理由详证据 §5.2.1）
改动文件：server/app/db_portable.py（+227/-13，437→650 行）；server/tests/test_cw054_pg_portable_contract.py（新增 781 行，TEST-PG 9 类 49 用例）；server/tests/test_db_portable.py（+235/-1，TEST-LOGIC 19→57：27 条 parity 矩阵 + 4 具名 + 7 异常契约）；server/tests/pg_test_kit.py（+4，仅 allowlist 追加 cw054_contract_test）；docs/evidence/CW054-EVIDENCE.md（整体重写作废旧版）；docs/CUSTOMER-TASK-EVIDENCE-V3.md（本登记）；docs/客户版任务清单-V3.md §18 CW-054 行；docs/客户版代码开发清单-V3.md CW-054 增量文件映射节
失败测试或回归锁定：两层 RED——RED-A（origin/main 原样 437 行）2 collection errors、EXIT=2；RED-B（origin/main + 仅声明符号的惰性 shim 451 行）44 failed/62 passed、EXIT=1，逐条行为级红（ConstraintExceptionMapping 8/8、Executemany 5/5、Iterdump 5/6、SetTraceCallback 4/4、RowAccess 4/8、parity 11/27 + 具名 3/4 + mapper SQLSTATE 4/4）；shim mapper 故意惰性使“保留 SQLSTATE”仍被 RED 覆盖；还原经 SHA256 逐字节校验 RESTORE OK；两处伪通过已修；%% 陷阱固化为 2 条契约测试（失败面非对称：SQLite lane 接受裸 %、客户 lane 抛错，故只可能在生产暴露）
实现结果：PG-lane executemany 真实批量并返回携带 rowcount 的游标；iterdump yield 真实 INSERT；set_trace_callback 生效；IntegrityConstraintError 双继承两 lane 映射并携带 SQLSTATE/约束名；_NamedRow 三处对齐真实 sqlite3.Row
验证命令与通过数：pytest tests/test_cw054_pg_portable_contract.py tests/test_db_portable.py -q → GREEN 106 passed/0 failed/0 skipped（真实 PG16.15）；分文件复跑 TEST-LOGIC 57 passed（死端口 DSN 下仍全绿）+ TEST-PG 49 passed；交叉核算 62+44=106、57+49=106；ruff check All checks passed、ruff format --check 296 files、mypy server/app Success 104 source files；scripts/verify_no_secrets.sh exit 0；全量 server pytest @5434 → 29 failed/2258 passed/5 skipped/38 errors in 2324.90s，本任务两文件在 FAILED/ERROR 清单零出现，67 项非绿逐组归因为本机环境（38 ffmpeg、22 cw033 子进程 .sh、7 条经 origin/main blob 基线复现同败）
证据层级：AUTOMATED_VERIFIED（真实 PG16 fixture 上执行，0 skip）；父任务保持 [~] Segment 1/N，不提升 STAGING
安全与可观测性：无真实 secret 入代码/日志/夹具（DSN 为本地 devuser 开发凭据）；测试库 allowlist 仅接受登记的 *_test 名，drop 经 assert_safe_test_database 守卫，清理永不触碰业务库；专用 cw054_contract_test 库 + 独立套件锁，不与全量 suite 争用 5434；iterdump 使敏感数据检查读取真实行而非空迭代器，消除假通过；set_trace_callback 使 SQL 审计在 PG lane 真正可用
迁移与回滚：无新迁移、无 revision 改动（CW-056 禁区未触碰）；回滚 = revert 本分支，纯代码+测试+文档，不影响生产数据
外部授权记录：无（未调用真实 ZPay/付费 Provider/生产 COS，未发码/灰度/公网发布）
未测试项：Segment 2/N 逐业务调用者（generation.py/internal_billing.py/analysis.py/characters.py/media.py）的 SQLite 债务核销；constraint_name 在无服务器 Diagnostic 时的取值（diag 只读无 setter 不可 stub，仅真实 PG 上断言）；_pg_literal 对 array/自定义 enum 等扩展类型的渲染；client vitest/biome、e2e、cargo（本机无 cargo，本次零前端零 Rust 改动，CI path-filter 与自带 Rust 环境承载）；本机 67 项环境性非绿（ffmpeg 缺失、Windows 不能子进程调 .sh、本机图片校验工具缺失、容器用户为 devuser）**未修复也不属本任务**，由 CI Linux 门禁承载；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：本 PR squash 后回填
```

## CW-025 — 把现有 PG 保护扩展到全部运行环境（全环境 PG-only 运行入口，2026-09-10）

分支 `feat/customer-v3-cw025-pg-protection-all-env`，基线 `origin/main@9a70918`（CW-009 #108）。`db_pg.resolve_database_config()` 作为单一在线解析入口全环境 fail-closed：`sqlite://`、`VIDEO_REPLICA_DB_PATH`（含与 PG DSN 混配）、缺 DSN 均 `RuntimeError`，不支持 scheme `ValueError`；`bootstrap._run_runtime_bootstrap()` 与 `generation_worker.main()` 删除 SQLite 在线分支，`main._lifespan()` customer lane 走 resolve+validate；新增 `scripts/dev-with-pg.sh` 使开发入口幂等拉起 pg-fixture 并强制注入 PG DSN。逐环境启动拒绝矩阵 5 环境×4 场景×2 入口=40 例（断言无 `.db/.db-wal/.db-shm` 副作用）+ 正向对照 + 桌面制品无 PG DSN 8 例。`npm run check` 全绿：client vitest 1267、biome 199/ruff/mypy 104/tauri cargo check 通过、服务端全量 pytest **2210 passed / 1 skipped / 0 failed**。收尾根因修复 `test_internal_access_tokens.py` 的 A1 `a1_dsn` fixture 连接池单例泄漏（teardown 只删库未 `close_pg_pool()`，陈旧 pool 毒化字母序后续的 `test_postgres_migrations`；源头+消费端双修复，对齐全仓 8+ PG fixture 约定）。零迁移文件改动。

证据层级 `AUTOMATED_VERIFIED`；真实服务器/staging/生产切换、PG HA、双 API/四 Worker 部署未执行，不提升 `STAGING_VERIFIED`。内部 P0 遗留 lane 的请求级 `DB_PATH`/SQLite 通道与 Worker SQLite 业务实现按 CW-025 完工标准（"最终 SQLite 在线实现移除须等 CW-043"）延后 CW-042/043/030/026，本任务不声称已移除。完整 §14 记录、拒绝矩阵与根因分析见 `docs/evidence/CW025-EVIDENCE.md`。

### CW-025 Section 14 Ledger Record

```text
任务/工作包：CW-025（W4）把现有 PG 保护扩展到全部运行环境
Owner / Reviewer：后端（Qoder 代理，hlong026 会话 2026-09-10）/ PR #7 CodeReview + connector
分支 / 基线 SHA：feat/customer-v3-cw025-pg-protection-all-env / 基线 origin/main@9a70918（CW-009 #108）
上游规格段落：收敛详细任务清单 §CW-025（line 335–344）；客户版任务清单-V3.md §18 CW-025（line 486）；PG-01 运行入口合同
改动文件：14（11 改+3 新）——package.json、server/app/{bootstrap,customer_fence,db_pg,generation_worker,main}.py、server/tests/{test_admin_auth,test_db,test_db_pg,test_internal_access_tokens,test_postgres_migrations}.py；新 scripts/dev-with-pg.sh、server/tests/{test_bootstrap_all_env_pg_gate,test_desktop_artifact_no_pg_dsn}.py
失败测试或回归锁定：先红后绿——反转 test_db_pg resolve 全环境 fail-closed、删 test_db SQLite bootstrap 用例；新增拒绝矩阵 test_bootstrap_all_env_pg_gate（51）+ test_desktop_artifact_no_pg_dsn（8）；收尾修复 A1 fixture 池泄漏
实现结果：单一在线解析入口全环境 PG-only fail-closed；bootstrap/worker/lifespan 入口删 SQLite 在线分支；开发入口强制 PG；桌面制品无 PG DSN
验证命令与通过数：npm run check —— client vitest 1267、biome 199、ruff All checks passed、ruff format 293、mypy 104、tauri cargo check 通过、服务端全量 pytest 2210 passed/1 skipped/0 failed（1009.52s）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：无真实密钥/激活码/token 入码入日志入 PR；secret 扫描 exit 0；readiness 日志脱敏 DSN 凭据
迁移与回滚：零迁移文件改动（未触碰冻结 025–030 区间）；回滚 = revert 本分支
外部授权记录：无（不涉及真实 ZPay/付费 Provider/生产 COS/发码/灰度/公网发布）
未测试项：cargo test、npm audit、客户浏览器 E2E、npm run build —— 只在 CI 三门禁执行
Lore 提交 SHA：本 PR squash 后回填
```

## CW-033 — 数据与资产快照恢复演练（2026-09-10，Pre-GA 准备批次，自动化验证完成）

分支 `feat/customer-v3-cw033-snapshot-recovery-drill`，基线 `9a70918`（= CW-009 tip = origin/main）。CW-033 属于 CW-001 §5.5 **GA 触发·冻结**清单，本批次仅覆盖 pre-GA 阶段可自动化的仓库侧准备面：新增 `server/tests/test_cw033_pitr_drill_validation.py`（以子进程真调用 `deploy/postgres/pitr-restore-drill.sh`，锁定 T38 结构性 grep 未触达的 10 条 fail-fast 路径——label 正则、manifest 路径、port 校验、CLI usage、drill env 门禁、recovery root/db/user 校验，共 23 用例含 parametrize 展开）与 `server/tests/test_cw033_evidence_boundary.py`（7 用例自守卫，若证据层级被误升 STAGING_VERIFIED/REAL_CHAIN_VERIFIED/PRODUCTION_GO 而 CW-005 §5 无对应签认，CI 即失败）。drill 脚本本体与 T38 交付的 `pitr-backup.sh`/`pitr-preflight.sh`/`pitr-fetch-wal.sh`/`pitr_recovery_facts.py` 零改动；无 Alembic revision；无外部授权动作。

证据层级 `AUTOMATED_VERIFIED`；**未**升至 `STAGING_VERIFIED`/`REAL_CHAIN_VERIFIED`/`PRODUCTION_GO`——`pg_ctl` 隔离副本恢复、真实 `pg_basebackup --wal-method=stream` 基准备份、WAL 归档 + `assert-wal` 外部取回、`pitr_recovery_facts.py verify` 100 事实跨域核验等 GA-blocked 项**未执行、未宣称**，触发条件是 CW-005 §5 数据批次盘点完成 + A/B/C 路线签认（数据负责人 + 业务负责人 + owner phlong026 三方）；PITR/RTO/RPO 测量归 CW-048，逻辑快照不得冒充 PITR（`客户版部署与灰度手册.md` §PITR 红线）。完整 §14 记录、GA-blocked 清单与交叉引用见 `docs/evidence/CW033-EVIDENCE.md`。

## 管理后台改版 W3–W17（2026-09-05，自动化与本地浏览器验证完成）

分支 `feat/customer-v3-admin-revamp`，实施基线 `67cf008`。完成管理聚合、按秒计费和实际用量成本、个人提示词、客户端参数与钱包、圆滑趋势曲线。最终 `npm.cmd run check` 退出 0：前端 746 通过；后端 1633 通过/1 项因缺少 ffmpeg 跳过；密钥扫描、静态检查、Cargo 和 Mypy 通过。133 个受检源码指纹与最终工作树一致。后台 12 页及客户端 3 个组件完成参考图成对对照，明确保留真实数据及已裁决范围差异。

证据层级 `AUTOMATED_VERIFIED`；真实支付、付费 Provider、生产迁移、安装包及发布未执行。浏览器具体赠送 60 秒激活码写入被自动审批拒绝，未绕过；业务行为有自动化覆盖。完整 §14 记录、接口与迁移说明见 `docs/evidence/admin-revamp/implementation.md`；截图、交互与风险见 `docs/evidence/admin-revamp/browser-qa.md`。

## 视频直链交付与桌面下载反馈（2026-09-04，自动化验证完成）

基于用户最新确认，取消生成视频后的媒体处理，保留授权/审计/结算；精简客户任务 UI，新增真实桌面保存反馈，并将客户删除改为账号级隐藏、管理端永久保留。最终全仓门禁为客户前端 715/715、服务端 1555 通过/1 项因本机无 `ffmpeg` 跳过，Tauri/Ruff/mypy/密钥扫描通过。证据记录：`docs/evidence/VIDEO-DIRECT-DELIVERY-DOWNLOAD-EVIDENCE.md`。当前仅为 `AUTOMATED_VERIFIED`，不宣称生产已更新或桌面安装包已验收。

> Note: This file is the evidence ledger for `docs/客户版任务清单-V3.md`; each task closure must record details per Section 14 template. The task list remains the single source of truth for status.
>
> **Evidence location (M0 review M8 unification, 2026-08-21)**: per-task evidence documents live under `docs/evidence/` (T02–T06 evidence files moved from the repository root; run-fix evidence under `docs/evidence/m0-review-fixes/`). Historical self-references inside those documents to their original root paths are preserved as record snapshots.

## T46 — Character Library Page-to-Provider Closure

| Field | Evidence |
| --- | --- |
| **Task ID** | T46 |
| **Status** | `IN_PROGRESS`（CL-13 待依赖决策，CL-11 待授权） |
| **Baseline / Branch** | `7f08678` / `feat/character-library-page-closure` |
| **Scope** | Character list, five-view and scene generation, IP rewrite, avatar/voice cloning, oral generation, task recovery, provider settings and billing price |
| **Work Plan** | `docs/人物库页面全链路收口计划-2026-09-07.md` |
| **Current Evidence Level** | CL-00–CL-12 and CL-14 are `AUTOMATED_VERIFIED`; CL-13 awaits the post-production renderer dependency and distribution-license decision |
| **External Boundary** | Real Apilio/DeepSeek/Hifly/COS/payment validation remains T40 and requires explicit authorization |

CL-00–CL-12 and CL-14 are complete. Formal pages use the real API path and do not fall back to review/mock fixtures; the character list now uses actor/query-bound keyset pagination instead of returning the full library to the page. The V1.4 workspace now exposes the existing provider/settings backend to administrators and blocks non-admin users at both navigation and content layers. CL-13 is specified but not coded because the bundled FFmpeg is audio-only and the repository has no approved text rasterizer/CJK font distribution. CL-11 remains the authorized real-chain gate. Full evidence is recorded in `docs/evidence/T46-EVIDENCE.md`.

## T45 — Security Defense-in-Depth Closure

| Field | Value |
| --- | --- |
| **Task ID** | T45 |
| **Owner / Reviewer** | Backend/Frontend/Security/QA/Release (Agent); repository self-review |
| **Branch / Base SHA** | `feat/customer-v3-t45-defense-in-depth` / `92bace869c413d4403372e324530d7ff1052804b` |
| **Verified Implementation SHA** | `cc563a54eadf9c746b1683fd15d89b2e530dbe7e`; local PG16 integration fixes `8cebc43` |
| **Upstream Spec Sections** | T45 work order B-2 plus S/D/C/A/E defense-in-depth findings |
| **Failure Test or Regression Lock** | Session replay state gates; account-scoped keyed idempotency; late CLOSED payment; enabled-channel callback; revocable local/COS grants; per-device/preauth/reset limits; fencing dedupe; slot conflict mapping; customer response redaction; byte-identical 404; internal write contract; admin idle/context checks and self-service audit; billing actor split; auditor/reveal audit; executable release preflight |
| **Implementation Result** | All T45 findings are closed in code or an explicit release-policy artifact; simple character and same-machine full-code reinstall remain direct without administrator review |
| **Verification Command and Pass Count** | Local PostgreSQL 16.15 on port 5433; `npm run check`: secret scan, client 592/592, E2E format, Cargo, Ruff/format, Mypy 74 modules all pass; Python 3.12.13 server full 1446 passed / 1 skipped / 0 failed; Playwright customer E2E 4/4 passed |
| **Evidence Level** | `AUTOMATED_VERIFIED`; Docker runtime and staging/real external chains remain unverified |
| **Security and Observability** | No plaintext secrets in audit/idempotency/log output; denial/reveal/auditor/self-service events are traceable; application grants are revocable; deploy preflight reports names and metadata only |
| **Migration and Rollback** | No new migration; behavior is application/configuration level. Rollback must keep the upgraded client before restoring legacy recovery behavior |
| **External Authorization Record** | None; no production DB/server/payment/COS/Provider/code issuance/gray/release action |
| **Untested Items** | Docker runtime (Windows host lacks VirtualMachinePlatform), staging topology, real ZPay/COS/Provider, signed desktop installer and production deployment |

Full evidence: `docs/evidence/T45-EVIDENCE.md`.

---

## T44 — T43 Security Follow-up Remediation

| Field | Value |
| --- | --- |
| **Task ID** | T44 |
| **Owner / Reviewer** | Backend/DB/Security/QA (Agent); repository self-review found no remaining Critical/High/Medium code issue |
| **Branch / Base SHA** | `feat/customer-v3-t44-security-followup` / `3799789588fd0278b8e18691329d60c7e75dfe23` |
| **Verified Implementation SHA** | `0b36c61` |
| **Upstream Spec Sections** | T43 follow-up findings B-1, F-1–F-5, C-1 and P-1 |
| **Failure Test or Regression Lock** | NULL identity-owner backfill/NOT NULL/conflict refusal; customer/auditor internal recharge denial; revoked-device recovery denial; unowned legacy binding denial; auditor cache denial; cross-user first-frame replay denial; traversal/ambiguous object-key refusal; system auto-publish audit policy |
| **Implementation Result** | Revision 051 performs deterministic owner recovery and fails closed on ambiguity; all listed authorization and storage bypasses are closed; per product decision, simple character generation remains direct and records system auto-approval rather than impersonating a human reviewer |
| **Verification Command and Pass Count** | Initial 6 regression locks failed on old behavior then passed after fixes; local PostgreSQL 16.15 migration suite 17/17 passed through revision 051 (including deterministic owner backfill and multi-owner refusal); server full 1446 passed / 1 skipped / 0 failed; client 592/592 and Playwright customer E2E 4/4 passed; Mypy 74 modules, full-server Ruff and format checks passed |
| **Evidence Level** | `AUTOMATED_VERIFIED`; production-snapshot migration and staging remain open |
| **Security and Observability** | No customer data or secrets recorded; authorization runs before idempotent replay/storage network I/O; ambiguous ownership blocks migration |
| **Migration and Rollback** | 051 prefers unique project-derived ownership, falls back to an existing creator, refuses conflicts/unresolved rows, then enforces NOT NULL and RESTRICT; downgrade restores nullable SET NULL shape without undoing safe backfill values |
| **External Authorization Record** | None; no production DB, server, payment, COS, Provider, code issuance or release action |
| **Untested Items** | Staging production-snapshot data preflight, production backup/migration and desktop release |

Full evidence: `docs/evidence/T44-EVIDENCE.md`.

---

## T43 — Tenant Isolation and Activation Security Remediation

| Field | Value |
| --- | --- |
| **Task ID** | T43 |
| **Owner / Reviewer** | Backend/Frontend/Security/QA (Agent); repository self-review |
| **Branch / Base SHA** | `feat/customer-v3-t43-isolation-security` / `e084cf1` |
| **Verified Implementation SHA** | `dddfa70` |
| **Upstream Spec Sections** | Task list T43; activation-code dev doc §3/§5/§6/§11/§12; acceptance spec §2–§3 |
| **Failure Test or Regression Lock** | Cross-account person/persona/version/assets/batches; project-detail IDOR; 100-device activation race; primary-session-only code reset; masked list + audited single reveal; zero-credit activation and DB shape constraints; Windows deep cache path; current first-frame E2E fixture |
| **Implementation Result** | Owner isolation across customer content; explicit pairing for unknown hardware; known-device reinstall recovery retained; primary-device session reset; masked list and audited one-code copy; zero-credit normal issuance with legacy positive-credit compatibility; migration 050 |
| **Verification Command and Pass Count** | Client 592/592; PG16 key suite 157/157; server full 1416 passed / 1 skipped (`ffmpeg` unavailable) / 0 failed; Ruff/format/Mypy/Tauri/secret scan passed |
| **Evidence Level** | `AUTOMATED_VERIFIED`; no staging, real-chain, gray or production claim |
| **Security and Observability** | Plaintext is neither listed, logged nor stored in audit/idempotency snapshots; reveal is AdminWriter-only and audited once; customer reads enforce owner/project access; unknown devices cannot self-bind |
| **Migration and Rollback** | 050 allows zero-value license batches and nullable activation recharge reference; database shape CHECK; downgrade fails closed when incompatible rows exist |
| **External Authorization Record** | None; no production deployment, real payment, COS, Provider or external code issuance |
| **Untested Items** | One local source-frame case requires `ffmpeg`; staging topology, production migration, signed installer, real chain and gray release |

Full evidence: `docs/evidence/T43-EVIDENCE.md`.

---

## T38 — PostgreSQL PITR and Recovery Drill

| Field | Value |
| --- | --- |
| **Task ID** | T38 / OPS-03 (partial) |
| **Owner / Reviewer** | OPS/DB (Agent); repository self-review |
| **Branch / Base SHA** | `feat/customer-v3-t38-pitr-recovery` / `main@3362ad9` |
| **Upstream Spec Sections** | Task list §7 T38, §12.7 OPS-03; code map §3.2/§3.3/§12; deployment runbook §5; PostgreSQL 16 continuous-archiving/PITR contract |
| **Failure Test or Regression Lock** | Red→green contracts: exact 100 cross-domain facts and session-epoch mismatch refusal; fewer than 100 refused; a post-base-backup sample boundary preserved; timeline `.history` WAL naming accepted; manifest is non-overwriting and Linux 0600; no `pg_dump`; physical base backup/WAL/preflight/archive/restore configuration required; staging-only confirmation; backup service must load only `pitr.env`, never application `customer.env`; frozen-map and runbook registration. The initial red runs exposed missing artifacts, Windows-only `fchmod` failure, absent timeline-history allowance, and application-DSN inheritance by the backup service; each has a regression lock. |
| **Implementation Result** | Separate libpq backup identity, explicit PG16/WAL/archive preflight and forced-WAL external-read check; `pg_basebackup --wal-method=stream` plus SHA-256 manifest/`pg_verifybackup`; root-owned archive-helper boundary for encrypted immutable off-site base/WAL handling; isolated staging restore gated by exact environment confirmation, bounded recovery root and dedicated port; `recovery.signal`/`restore_command`/PITR target setup; 100 post-base synthetic activation→order→CHARGE→session-epoch facts captured to a 0600 atomic manifest and rechecked one by one after promotion; PostgreSQL base-backup timer that does not inherit app DSN or app secrets. |
| **Verification Command and Pass Count** | `uv run python -m pytest tests/test_customer_pitr.py -q` → 11 passed; T38 + HA contracts → 46 passed; `ruff check`, `ruff format --check`, and `mypy app scripts/pitr_recovery_facts.py` passed; Git Bash `bash -n` passed for all four PITR scripts; PR #74 Secret scan, Linux quality gate and Windows Tauri/NSIS all passed. |
| **Evidence Level** | Repository-side `AUTOMATED_VERIFIED`; T38/OPS-03 remain `[~]`, not `STAGING_VERIFIED` |
| **Security and Observability** | App DSN/keys never enter the PITR systemd service; password source/archive credentials/helper are out-of-repo protected files; scripts do not print IDs, secrets or object URLs; manifest only contains the restricted recovery facts and digest; recovery can only delete a same-label directory under a non-root staging recovery root. |
| **Migration and Rollback** | No Alembic revision. Rollback is a code/config reversion; do not delete historical external backups. The legacy SQLite timer remains explicitly internal-only. |
| **External Authorization Record** | None; no real PostgreSQL archive, off-site copy, server, COS, ZPay, paid Provider, external code issuance, gray release or public launch. |
| **Untested Items** | Provisioned PostgreSQL 16 archive command/library and helper; independent off-site `assert-wal`/`assert-base` retrieval; isolated staging restore with 100 synthetic post-base records; recorded RPO/RTO; PG HA failover/fault drill (T39); real business chain (T40+) |
| **Lore Commit SHA** | `3245c6fae9fa87a31181ed5f4273d1ac194d742a` (PR #74 squash) |

Full §14 record and staging evidence checklist: `docs/evidence/T38-EVIDENCE.md`.

---

## T39 — Staging Fault Drills

| Field | Value |
| --- | --- |
| **Task ID** | T39 / OPS-04 (partial) |
| **Owner / Reviewer** | QA/OPS (Agent); staging verifier pending |
| **Branch / Base SHA** | `feat/customer-v3-t39-fault-drills` / `main@3245c6f` |
| **Upstream Spec Sections** | Task list §7 T39 and §12.7 OPS-04; test spec §8.2; deployment runbook §6.1 |
| **Failure Test or Regression Lock** | Red→green runbook contract requires a same-SHA staging window, two API services, four Worker services, controlled `systemctl kill` faults, a post-claim crash that leaves the original task in `SUBMISSION_UNCERTAIN` for manual reconciliation while replacement Workers run later work only, post-failover 100-fact verification with the protected libpq service, controlled stub-only dependency faults that retain the same idempotency key, RTO/RPO recording, and a prohibition on real ZPay/COS/paid-Provider calls. |
| **Implementation Result** | A staging-only fault-drill operation card with explicit No-Go/rollback conditions, a complete §14 evidence template and implementation-independent regression lock. It does not inject faults from CI or a developer machine. |
| **Verification Command and Pass Count** | `uv run python -m pytest tests/test_customer_ha_smoke.py -q` → 36 passed; T38 + HA contracts → 47 passed; `ruff check`, `ruff format --check` and `mypy app` passed; PR #75 Secret scan, Linux quality gate and Windows Tauri/NSIS all passed. |
| **Evidence Level** | Repository-side `AUTOMATED_VERIFIED`; T39/OPS-04 remain `[~]`, not `STAGING_VERIFIED` |
| **Security and Observability** | No credentials, customer records, object URLs or external endpoints are stored. Only controlled stubs are permitted; T37 fired/resolved delivery must be evidenced in staging. |
| **Migration and Rollback** | No migration. Any invariant breach is a No-Go: remove changed members from LB, stop expansion, preserve audit facts and use the deployment rollback procedure; never amend wallet or ledger rows directly. |
| **External Authorization Record** | None; no live staging action or real ZPay/COS/paid-Provider request has occurred. |
| **Untested Items** | Actual two-API/four-Worker failure domains, PostgreSQL HA failover, controlled dependency fault injection, RTO/RPO and external alert delivery all need an authorized staging window. |
| **Lore Commit SHA** | `ecf84c392675c4db97dd5d9847173783fcee7df2` (PR #75 squash) |

Full §14 record and staging checklist: `docs/evidence/T39-EVIDENCE.md`.

---

## T37 — Structured Logging, Metrics and P1 Alerts

| Field | Value |
| --- | --- |
| **Task ID** | T37 / OPS-02 (partial) / EXT-02 (partial) |
| **Owner / Reviewer** | OPS/Backend (Agent); independent review `APPROVE` — first pass 0C/0H/1M, final after the regression-locked fix 0C/0H/0M; PR #72 connector ten passes found 10 P1 + 10 P2, all regression-locked and fixed; final re-review of 28156b3 found no major issue and left no open thread |
| **Branch / Base SHA** | `feat/customer-v3-t37-observability` / `main@bb545ca` |
| **Upstream Spec Sections** | Task list §7 T37, §12.6 EXT-02, §12.7 OPS-02; test spec §8.3; code map §3/§12 |
| **Failure Test or Regression Lock** | Structured request-id/field/result/secret contracts, including legacy HTTPException and direct-ingress business codes; private per-process metrics with fixed `UNMATCHED` path and `OTHER` extension-method labels; fencing rejection/wait evidence, failure isolation, matched route-template logging, ISO-`T` cutoff ordering and same-transaction expected/verified epoch facts for a committed stale write; same-epoch double heartbeat and post-successor-LOGIN displaced-epoch heartbeat both fire while pre-switch activity stays clear even when the heartbeat is written in a negative-offset database timezone; read-only fenced requests leave zero write evidence; a cross-user denial rolls back the attempted business change then commits one idempotent audit fact independently, while a successful PostgreSQL owner check commits only a deduplicated domain-separated digest pair and any committed actor≠owner pair is retained; bounded single-owner cluster alert queries use 042's indexed typed timestamps after one-time UTC backfill, derive starvation from waiting work with a left-joined cursor so missing cursors cannot hide it, and preserve cursors with pending work during cleanup; CORS preflight and unhandled 500 both preserve request-id/log/metric correlation; the task-time index leaves the 10k fair-queue lease path on its dedicated status index; the T07 importer derives companion instants from source timestamps; wallet-mismatch and starvation qualification occur before output caps; session-owner/shared-PG-state contracts cover overlapping and staggered hosts and leave prior state retryable after notification failure; PG16 fresh-head execution; admin exchange shared-PG budget; revision 042 index/append-only/downgrade guard |
| **Implementation Result** | Single-line structured HTTP completion logs with an explicit non-secret business-field allowlist; two privately scraped API metric endpoints; local fencing reject/wait metrics; session-lock single-owner PostgreSQL anomaly probe emitting count-only fired/resolved edges for 11 alert classes, including same-epoch double heartbeat and a heartbeat from a displaced epoch after a newer LOGIN, both rejected and actually committed stale writes, and cross-user denial/successful mismatch signals; 042 materializes and indexes absolute timestamps from legacy TEXT facts once, so probes read typed columns without per-query casts; the T07 importer preserves those instants when importing a legacy snapshot; starvation derives from pending work and detects a missing/idle cursor while cleanup preserves pending users; observability wraps CORS and its redacted 500 handler preserves the same request ID; shared PostgreSQL alert state advances only after the probe transaction commits and every edge is flushed; write-only commit evidence, append-only authorization digest pairs, and rollback-independent authorization-denial audits; shared administrator-exchange rate limit; deployment/runbook contracts |
| **Verification Command and Pass Count** | Ops metrics 21; ops alerts 14; alerts + 042 slice 15 on real PG16 fixture; latest shared-state/alerts/042/SQLite→PG slice 51; customer fencing + RBAC 101; combined ops metrics/alerts/fencing/RBAC/fair-queue/migration/reconcile combination 187 (including 10k fair-queue `EXPLAIN`, T07 typed-timestamp import and 500 request-id); customer security 36; health 6; SQLite→PG/reconcile 36; admin auth 72 (including review fix); related admin 93; affected aggregate 255; client 513; Cargo pass; Ruff/format/mypy pass (186/72); remote full gate at 28156b3: Secret scan + Windows Tauri/NSIS + Linux quality all pass, server 1291 passed / 1 skipped / 16 existing warnings, client 513, browser E2E 4, Rust 4; independent review final `APPROVE`, 0C/0H/0M; PR #72 connector ten-pass findings fixed and its final 28156b3 review found no major issue |
| **Evidence Level** | Repository-side `AUTOMATED_VERIFIED`; T37/OPS-02/EXT-02 remain partial, not `STAGING_VERIFIED` |
| **Security and Observability** | No arbitrary payload logging, credential/URL/DSN exposure or high-cardinality metric labels; metrics require a token file and loopback ingress; anomaly output contains fixed alert names and counts only; real collector/receiver and database/external-provider monitoring remain explicitly unverified |
| **Migration and Rollback** | PG-only revision 042 adds hot indexes (including successor-LOGIN lookup), shared mutable `ops_alert_state`, the `admin:exchange:ip` / `session:fencing` vocabulary, a deduplicated append-only committed-write epoch fact table, deduplicated append-only authorization digest pairs, and UTC-backfilled indexed timestamp companions for shared legacy tables; downgrade refuses once new security, authorization, or write evidence exists, otherwise drops ephemeral alert state and restores 041 shape; T07 import/reconcile treats non-empty PG-only tables and the new PG-only companion columns correctly |
| **External Authorization Record** | None; no real server, COS, ZPay, paid Provider, external code issuance, gray release or public launch |
| **Untested Items** | Central log/metrics ingestion; external P1 fired/resolved and missed-timer drill; PG connection/slow-query/replication/backup metrics; complete Provider/COS/ZPay provider-task/cost/error chain; staging/real-chain/production |
| **Lore Commit SHA** | See task PR squash SHA |

See `docs/evidence/T37-EVIDENCE.md` for the full §14 record and evidence-level boundary.

---

## T36 — Customer Staging Topology (OPS-01 / COS-01 / DESK-02)

| Field | Content |
| --- | --- |
| **Owner / Reviewer** | OPS/DB/Backend/Tauri (Agent) / independent Code Reviewer, APPROVE, 0 Critical / 0 High / 0 Medium |
| **Branch / Base SHA** | `feat/customer-v3-t36-staging-topology` / `main@2a298470e20a4cd14d7fd39e83e701468e9978fb` |
| **Upstream Spec Sections** | Task list §7 T36, §12.5 EXT-01/COS-01, §12.6 DESK-02, §12.7 OPS-01, §13–§16; code checklist §11.1/§12; acceptance spec Gate B |
| **Files Changed** | API/Worker runtime gate and probes; guarded empty-customer bootstrap; T36 HA/real-PG contracts; customer env; Nginx/API/Worker/PG deploy assets; customer Tauri config/build guard + CI; frozen file map; deployment/evidence manuals; task/evidence ledgers |
| **Failure Test or Regression Lock** | 7 red tests before implementation; latest T36 23 passed; build-contract + original T36 combined 18/18; latest review-fix slice 136 passed/2 skipped; real PG16 empty-bootstrap integration 1 passed; coverage includes PG TLS/primary-writable/COS bucket gates, private live/ready with same-host sentinel, transport-only business-route retry versus status-based readiness removal, direct Worker, shared character-cache visibility across API replicas with PG-exit-before-COS-I/O ordering, pre-Alembic production DSN validation, atomic first-admin/encrypted-COS bootstrap with a half-initialized-state rejection matrix, separate internal/customer builds, routable origin guard, and customer NSIS payload contract |
| **Implementation Result** | Deployable LB + two API + four Worker repository shape; PG primary-writable/private COS fail-closed readiness/startup; customer-production PG rejects missing/downgrade-capable sslmode and read-only HA endpoints, and the migration script validates the transport boundary before Alembic; a migrated pristine database can atomically seed first admin/wallet/encrypted COS/runtime/audit under SERIALIZABLE + advisory lock while half-initialized targets fail closed; `/ready` is private and passively removes failed backends by readiness status while `/api/` retries transport failures only so business 502/503/504 responses do not quarantine healthy APIs; customer-production character-cache objects use deterministic shared COS keys across API replicas, and authorization/config transactions close before COS HEAD/GET/PUT while internal P0 keeps its local cache; compatibility `/health` is exact-proxied instead of falling into the SPA; same-origin Web/admin uses HTTPS 443; the customer desktop starts on `/customer`; customer builds reject loopback and IPv4/IPv6 non-destination API literals; CI preserves separate internal/customer NSIS packaging gates; new build files are registered in the frozen map |
| **Verification Command and Pass Count** | Client 513; latest T36 23; build+T36 18; PG customer-production TLS gate 13 passed; latest review slice 136 passed/2 skipped with PG fixture; real PG16 migrated-empty bootstrap 1 passed; customer route/health/map/origin 40 passed/2 skipped; PG writable/T36/build/migration 86 passed/2 skipped; character/RBAC/T36/build review-fix slice 122 passed/2 skipped; locked Tauri 2.11.4 config/web/no-default-features app build passed; settings/storage/T36 78; Ruff/format/mypy/default+customer Tauri pass; rebuilt unsigned customer NSIS 1,219,752 bytes, SHA256 recorded; server single full 1200 pass/1 stale contract then affected slices green; independent review 0C/0H/0M; PR connector findings substantively fixed |
| **Evidence Level** | Repository `AUTOMATED_VERIFIED`; T36/OPS-01/COS-01 remain `[~]`; DESK-02 `[x]`; no staging/real-chain/production claim |
| **Security and Observability** | No real credentials; customer PG cannot inherit libpq's plaintext-capable `prefer` default and the template performs certificate/hostname validation; COS readiness HEAD is read-only; generic 503/type-only logs; forwarding headers overwrite-only; T37 external observability remains pending |
| **Migration and Rollback** | No new revision; designated migration host validates customer PG/TLS before existing Alembic head under non-blocking `flock`; migrated empty targets use the guarded one-shot path; rolling/rollback runbook added; PITR belongs to T38 |
| **External Authorization Record** | None; no real server/TLS/COS/ZPay/Provider, external code issuance, gray release or public launch |
| **Untested Items** | Real LB/two API/four Worker failure domains; PG HA failover; private COS full operation/minimum permission; node replacement; distinct real client IP buckets; signed Windows staging build |
| **Lore Commit SHA** | See task PR squash SHA |

Full §14 record and artifact hash: `docs/evidence/T36-EVIDENCE.md`.

---

## T35 — Customer Security Review (SEC-01 / SEC-02)

| Field | Content |
| --- | --- |
| **Owner / Reviewer** | Security/Backend (Agent) / independent Security Reviewer, three rounds; final 0 Critical / 0 High / 0 Medium; PR connector 1 P1 fixed with regression lock |
| **Branch / Base SHA** | `feat/customer-v3-t35-security-review` / `main@d15b3f9` |
| **Upstream Spec Sections** | Task list §7 T35, §12.7 SEC-01/SEC-02, §13–§15; code checklist §11.1/§13 |
| **Files Changed** | `server/app/bootstrap.py`, `main.py`, `security_rate_limit.py`, four IP-consuming route modules; all four product Uvicorn launch paths plus the browser E2E harness; admin/customer/deployment/build-contract tests; customer env, fail-closed secret scan, task/evidence ledgers |
| **Failure Test or Regression Lock** | Production HMAC/AEAD/Fernet/origin/proxy startup gates; exact Host/HTTPS/single XFF/CORS; real Uvicorn `ProxyHeadersMiddleware` rewrite must return 503; missing/mismatched `PUBLIC_BASE_URL` must abort startup |
| **Implementation Result** | Verifiable trusted-proxy boundary; product and browser-E2E launchers enforce `--no-proxy-headers`; route rate limits consume only verified IP; browser/signed-asset/ZPay origins cannot split; all customer-production domain keys fail closed; runtime activation-code/signed-URL scan covers tracked and untracked files |
| **Verification Command and Pass Count** | Admin auth 69; customer security 34; launcher contracts 19; client 513; server full 1188 passed / 3 skipped; Ruff/format/mypy/Tauri pass; pip/npm audit 0 known vulnerabilities; independent review 0C/0H/0M; PR P1 fixed |
| **Evidence Level** | SEC-01/SEC-02 `AUTOMATED_VERIFIED`; T35 parent remains `[~]`, no staging/real-chain/production claim |
| **Security and Observability** | Bandit 0 High; B608/B310 Medium rules manually classified and retained visibly; secret, credential, signed URL, redaction and CSV-injection gates pass |
| **Migration and Rollback** | No migration; code revert only, but ingress/secret gates must not be independently removed from a public deployment |
| **External Authorization Record** | None; real ZPay/COS/paid Provider, external code issuance, gray release and public launch not executed |
| **Untested Items** | T36 real LB/two-API/four-worker staging; BILL-01/BILL-02 real ZPay reconciliation; T40–T42 real chain, gray release and Go/No-Go |
| **Lore Commit SHA** | See task PR squash SHA |

Full §14 record and scan classification: `docs/evidence/T35-EVIDENCE.md`.

---

## T01 — Freeze V3 Main Specifications

| Field | Content |
| --- | --- |
| **Owner** | Architecture/Product |
| **Reviewer** | (N/A - spec freeze doesn't require independent reviewer) |
| **Branch / SHA** | `feat/customer-v3-t01-freeze-spec` / `7e75576aaf462b5c492d02651b4256734d2a6334` (PR #28 squash) |
| **Upstream Spec Sections** | `docs/客户版开发计划-V3.md` §1; `docs/客户版任务清单-V3.md` Header & Table T01 |
| **Files Changed** | - Update `docs/客户版任务清单-V3.md` Header status<br>- Update Task Table T01 status `[~]`→`[x]`<br>- Add `docs/客户版任务证据记录-V3.md` (this file as ENG version) |
| **Failure Test or Regression Lock** | N/A for spec freeze tasks |
| **Implementation Result** | User session confirmed V3 execution plan and boundaries; frozen downstream design dependencies on file mapping and document structure |
| **Verification Command and Pass Count** | N/A |
| **Evidence Level** | `CODE_PRESENT` (here refers to documentation freeze) |
| **Security and Observability** | N/A |
| **Migration and Rollback** | R0 preserves current internal P0 release/tag; V3 branch evolves independently |
| **External Authorization Record** | None |
| **Untested Items** | N/A |
| **Lore Commit SHA** | `7e75576aaf462b5c492d02651b4256734d2a6334` |

### Acceptance Evidence

#### Development Plan Conclusion Consistency

- Plan §1 states clearly: "This is not adding a few pages on top of the existing system. This scan identified 33 modules directly depending on `sqlite3` in the current runtime layer... Customer edition must complete PostgreSQL migration first, then build activation codes, device/session, fair queueing, and multi-instance"
- Effort model: 95–165 person-days base effort → risk-adjusted 110–185 person-days management; recommended configuration: 2 backend + 1 frontend/Tauri + 1 QA + 0.5–1 OPS
- Lane division: A(DB/billing)/B(device/auth)/C(worker/queue)/D(customer frontend)/E(security/deployment)
- Milestones M0–M6 clearly defined, especially M0/M1 exit gates constraining subsequent feature development order

#### Unique File Mapping Frozen

Per unique implementation file mappings frozen in `docs/客户版代码开发清单-V3.md` §3:

**Migration themes sequence** (cannot override existing revisions):
- `server/migrations/versions/025_postgres_runtime_compatibility.py`
- `server/migrations/versions/026_customer_security_and_billing.py`
- `server/migrations/versions/027_activation_code_catalog.py`
- `server/migrations/versions/028_customer_devices_and_activations.py`
- `server/migrations/versions/029_customer_sessions_and_idempotency.py`
- `server/migrations/versions/030_user_fair_queue.py`

**Backend business modules**:
`activation_code_service.py`, `activation_code_routes.py`, `customer_device_service.py`, `customer_device_routes.py`, `customer_session_service.py`, `customer_session_routes.py`, `customer_idempotency.py`, `customer_auth.py`, `customer_queue.py`, `security_rate_limit.py`, `admin_auth_routes.py`, `admin_activation_routes.py`, `admin_customer_routes.py`, `admin_device_routes.py`, `admin_session_routes.py`, `admin_audit_routes.py`

**Client directories**:
- `client/src/customer/*.tsx` (ActivationPage/LoginPage/DevicePairingPage/SessionConflictDialog/DeviceManagementPage/useCustomerSession.ts/customer-state.ts)
- `client/src/admin/*.tsx` (ActivationCodeBatchesPage/ActivationCodesPage/DeliveriesPage/CustomersPage/DevicesPage/SessionsPage/AuditEventsPage)
- `client/src-tauri/src/customer_credentials.rs`

**Server tests**:
`t05/postgres_migrations.py`, `test_sqlite_to_postgres.py`, and all customer-domain test files (activation/code/service/routes/devices/sessions/fencing/idempotency/recharge/queue_fairness/admin/auth/security/ha_smoke/real_chain_contracts)

#### Prohibited Parallel Execution Red Lines

Strictly enforce prohibited parallel items from Plan §5:
- ❌ T13 NOT before T08/T10 data constraints completed
- ❌ T20 switch NOT before T19 lease state machine passed  
- ❌ T21 NOT just batch dependency replacement; must verify fencing per write route
- ❌ T25 fair queue NOT SQLite-first then "migrate later"
- ❌ T36 staging NOT single API/Worker health checks pretending to be multi-instance
- ❌ T40 real payments and Provider submissions require manual authorization

#### First Batch Scope Confirmation

Per Plan §12 "Development Start Suggestion": First batch starts only T02–T06; before this batch closes, do not implement first activation business logic (T13) to avoid rework on incorrect transaction model.

---

## Evidence Maintenance Rules

1. **Status sync**: Only update task status (`[ ]/[~]/[x]/[!]`) in `docs/客户版任务清单-V3.md`
2. **Evidence registration**: Detailed evidence for each task registered in corresponding section of this file
3. **SHA recording**: Complete Lore commit SHA recorded in both task list and this file
4. **Blocking markers**: Tasks requiring external authorization/resources marked with `[!]` and documented blocking items

---

## T01 Section 14 Ledger Record

```text
任务/工作包：T01
Owner / Reviewer：架构/产品（Agent 执行）/ chatgpt-codex-connector（PR #28 评审）
分支 / 基线 SHA：feat/customer-v3-t01-freeze-spec / 基线 4f197b4
上游规格段落：docs/客户版开发计划-V3.md §1/§7；docs/客户版代码开发清单-V3.md §3
改动文件：docs/客户版任务清单-V3.md（T01 状态 [~]→[x]、Header）、docs/CUSTOMER-TASK-EVIDENCE-V3.md（本文件）、.gitignore（忽略 .worktrees/ 并行工作区）
失败测试或回归锁定：规格冻结类任务，无失败测试；回归锁定由 T02 基线承担
实现结果：用户 2026-08-20 会话确认 V3 口径；冻结六段迁移主题与唯一文件映射；账本 T01 已关闭
验证命令与通过数：N/A（纯文档）
证据层级：CODE_PRESENT（文档冻结）
安全与可观测性：N/A
迁移与回滚：R0 保留内部 P0 release/tag
外部授权记录：无
未测试项：N/A
Lore 提交 SHA：7e75576aaf462b5c492d02651b4256734d2a6334（PR #28 squash 合并）
```

---

## T02–T06 Evidence Index (M0 review M8 backfill)

Per-task evidence documents (moved to `docs/evidence/` on 2026-08-21; SHAs are
the squash-merge commits on `main`):

| Task | Squash SHA (main) | PR | Evidence document |
| --- | --- | --- | --- |
| T02 | `7b81df86dff0c1e4cb558595e63c712d4ee38979` | #29 | `docs/evidence/T02-EVIDENCE.md` (+ `docs/evidence/t02/` gate artifacts) |
| T03 | `66b520e98f107db143ce23c98ba62d676ac8ef28` | #30 | `docs/evidence/T03-EVIDENCE.md` |
| T04 | `81303219ba4326a0530571a5c3263fdf8bfb7aa5` | #31 | `docs/evidence/T04-SQLITE-INVENTORY.md` |
| T05 | `c152766bbef54e07e7db7b89804ff071c2bf82cb` | #32 | `docs/evidence/T05-EVIDENCE.md` |
| T06 | `d797e6dafaa5356db94c3d36afd12af93d7835af` | #33 | `docs/evidence/T06-EVIDENCE.md` |

M0-review remediation runs (evidence under `docs/evidence/m0-review-fixes/`):

| Run | Scope | PR |
| --- | --- | --- |
| P0 | C1 (revision 025) + H2 (CI PG service) + review P1 downgrade guard + LOW-2 | #35 |
| P1/P2 code | H1 worker exit + H3 alembic DSN + M1–M6 + M7 doc + LOW-1/3 | #36 |
| P2 docs | H4 inventory addendum + M8 evidence unification + M9 ledger correction + H1 exit-gate wording | #37 |

---

## T07 — SQLite to PostgreSQL One-shot Import and Reconciliation

| Field | Content |
| --- | --- |
| **Owner** | DB / Backend |
| **Reviewer** | chatgpt-codex-connector + independent final verification |
| **Branch / Base SHA** | `feat/customer-v3-t07-sqlite-postgres-import` / `main@35e341833e1de3096d1728c98375523d1dd46982` |
| **Verified Implementation SHA** | `c26bc0732d9fe66142dae3c50ac9c908bdf578a8` |
| **Upstream Spec Sections** | Task list §2 T07, §12.1 DB-05/DB-06; code checklist §8.3 |
| **Files Changed** | `server/app/backup.py`; `server/scripts/sqlite_to_postgres.py`; `server/scripts/reconcile_customer_billing.py`; `server/tests/test_sqlite_to_postgres.py`; T07 evidence and ledgers |
| **Failure Test or Regression Lock** | API export mismatch; WAL race; evidence overwrite; 0600 permissions; JSON asset orphans; bounded-memory digest; DSN redaction; advisory lock; atomic publication cleanup |
| **Implementation Result** | Private immutable SQLite snapshot, one-transaction PostgreSQL import, idempotent replay, full table/billing/asset reconciliation, fail-closed preconditions and R0/R1 rollback contract |
| **Verification Command and Pass Count** | Run #189: all three gates succeeded; client 324 passed; server 628 passed / 1 unrelated skip; T07 PG16 module 19 passed; ledger-finalization prerequisite Run #195 also passed all three gates |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | No DSN secret/raw business row/storage URL/token in reports; snapshot mode 0600; failures expose only bounded summaries |
| **Migration and Rollback** | No dual write; all target writes in one PostgreSQL transaction; R0 keeps the old P0 release/tag and source DB; R1 reverts before customer traffic opens |
| **External Authorization Record** | None; no production DB, COS, ZPay, paid Provider, activation-code distribution, rollout or public release invoked |
| **Untested Items** | Real production dataset cutover, staging maintenance-window timing, real-chain and production evidence |
| **Lore Commit SHA** | PR #38 implementation head `c26bc0732d9fe66142dae3c50ac9c908bdf578a8`; final squash SHA is the GitHub merge result |

## T08 — Billing Provider / Pricing Scope Conditional Constraints

| Field | Content |
| --- | --- |
| **Owner** | Billing / DB |
| **Reviewer** | chatgpt-codex-connector |
| **Branch / Base SHA** | `feat/customer-v3-t08-billing-provider-constraints` / `main@9f60eea615ab9dee177eb0892b3789dabda196dd` |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T08-EVIDENCE.md` for blob integrity hashes) |
| **Upstream Spec Sections** | Task list §2 T08, §12.1 DB-07; code checklist §3.1 (frozen migration name `026_customer_security_and_billing`); acceptance spec §7 (provider/price-scope shapes verified by PG check constraints); activation-code dev doc §12.1 |
| **Files Changed** | `server/migrations/versions/026_customer_security_and_billing.py` (new); `server/tests/test_postgres_migrations.py` (+2 tests); 7 test files' head-revision assertions; task list + evidence ledger |
| **Failure Test or Regression Lock** | 4 legal shapes accepted and 12 illegal shapes rejected by PG16 CheckViolation; downgrade guard refuses with customer rows and restores verbatim 022 shapes on an empty ledger; red-green record against the 025 head |
| **Implementation Result** | PG-only revision 026 enforces provider enum (zpay/activation_code/admin_adjustment), pricing_scope enum (INTERNAL/CUSTOMER_STANDARD), scope pairing, paid-on-creation for non-zpay, trade-number presence rules, customer price floor (charged >= base), and min/step ladders limited to zpay |
| **Verification Command and Pass Count** | `pytest tests/test_postgres_migrations.py` → 9 passed; full suite → 636 passed; ruff/format/mypy green; `npm run check` full gate green |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 locally and in the CI Linux gate) |
| **Security and Observability** | Constraints enforced by the database layer, not application code (DB-07 No-Go); SQLite internal runtime untouched |
| **Migration and Rollback** | PG-only append-only revision (025 precedent); guarded downgrade keeps confirmed billing rows intact |
| **External Authorization Record** | None |
| **Untested Items** | Business write paths for activation_code/admin_adjustment orders (T13 activation transaction, T23 adjustment API); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T08 Section 14 Ledger Record

```text
任务/工作包：T08 / DB-07
Owner / Reviewer：Billing/DB（Agent 执行）/ chatgpt-codex-connector（PR 评审）
分支 / 基线 SHA：feat/customer-v3-t08-billing-provider-constraints / 基线 9f60eea615ab9dee177eb0892b3789dabda196dd
上游规格段落：客户版任务清单 V3 §2 T08、§12.1 DB-07；代码开发清单 V3 §3.1；测试与验收规格 V3 §7；激活码开发文档 §12.1
改动文件：server/migrations/versions/026_customer_security_and_billing.py（新增）、server/tests/test_postgres_migrations.py、7 个测试文件 head 断言、任务与证据账本
失败测试或回归锁定：先红后绿——4 组合法形状 + 12 组非法形状 PG16 CheckViolation；downgrade 守卫（有客户行拒绝降级、空账本对称回退）
实现结果：026 PG-only 迁移以 8 条 provider 条件 CHECK 约束扩展账务来源、价格域、客户价下限与 min/step 阶梯适用范围
验证命令与通过数：test_postgres_migrations 9 passed；全量 636 passed；ruff/format/mypy 全绿；npm run check 全仓门禁通过
证据层级：AUTOMATED_VERIFIED
安全与可观测性：约束全部由数据库层强制；SQLite 内部运行时零改动
迁移与回滚：PG-only、downgrade 带数据守卫，空账本对称回退并逐字恢复 022 约束
外部授权记录：无
未测试项：activation_code/admin_adjustment 业务写入路径（T13/T23）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

### T07 Section 14 Ledger Record

```text
任务/工作包：T07 / DB-05 / DB-06
Owner / Reviewer：DB/Backend Agent / chatgpt-codex-connector + independent final verification
分支 / 基线 SHA：feat/customer-v3-t07-sqlite-postgres-import / 35e341833e1de3096d1728c98375523d1dd46982
上游规格段落：客户版任务清单 V3 §2 T07、§12.1 DB-05/DB-06；代码开发清单 V3 §8.3
改动文件：server/app/backup.py、server/scripts/sqlite_to_postgres.py、server/scripts/reconcile_customer_billing.py、server/tests/test_sqlite_to_postgres.py、docs/evidence/T07-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：API 导出、WAL/sidecar、不可覆盖与 0600、JSON 资产引用、增量指纹、DSN 脱敏、advisory lock、事务回滚、发布竞态
实现结果：SQLite 只读不可覆盖快照、单事务 PG 导入、重复执行、全量对账、维护窗与 R0/R1 回滚契约完成
验证命令与通过数：Run #189 三门禁全部成功；客户端 324 passed；服务端 628 passed / 1 unrelated skip；T07 PG16 专项 19 passed；账本写入前置 Run #195 亦全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：0600、敏感值脱敏、报告只含计数/摘要、失败 fail-closed
迁移与回滚：禁止双写；单 PG 事务；源 DB、快照与旧 P0 release/tag 保留
外部授权记录：无；未调用生产数据库、COS、ZPay、付费 Provider、发码、灰度或公网发布
未测试项：真实生产存量库切换、类生产维护窗耗时、STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：PR #38 implementation head c26bc0732d9fe66142dae3c50ac9c908bdf578a8；最终 squash SHA 以 GitHub merge 结果为准
```

## T09 — Per-operator Admin Session/CSRF and Customer-Production Fail-Closed

| Field | Content |
| --- | --- |
| **Owner** | Security/Backend/OPS |
| **Reviewer** | chatgpt-codex-connector |
| **Branch / Base SHA** | `feat/customer-v3-t09-admin-session-csrf` / `main@e50f931` (PR #39 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T09-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §2 T09, §12.1 DB-08; code checklist §3.2 (frozen `admin_auth_routes.py`); activation-code dev doc §15 (`admin_sessions` from revision 026); acceptance spec §8 |
| **Files Changed** | `server/app/admin_auth_routes.py` (new, non-object-JSON guard); `server/scripts/issue_admin_exchange_credential.py` (new); `server/tests/test_admin_auth.py` (new, 39 cases incl. 3 PR-review locks); `server/app/bootstrap.py` (version-aware key discovery + min-length gate); `server/app/main.py`; `server/app/control_auth.py`; `deploy/customer.env.example` (new); `client/vite.config.ts` (Node-25 webstorage test compat); 11 stash-restored tracked files with Windows hardening |
| **Failure Test or Regression Lock** | 39 red→green cases: credential issue/verify/expiry/tamper/single-use (nonce-digest PK collision), non-object JSON bodies rejected as malformed, session whoami/logout/expiry/revocation/disable-invalidation, CSRF missing/mismatch, auditor read-only, secure cookie shape, per-violation + aggregated fail-closed gate, boot with only a rotated `_V2` key, weak key (< 32 B) rejected at boot, runtime legacy-identity 403, PG-unavailable 503 |
| **Implementation Result** | `ASX1` single-use HMAC exchange credential (versioned keys) → HttpOnly `admin_session` cookie (path `/api/control`, strict, secure in production) + per-session CSRF (`X-Admin-CSRF`); SHA-256 digests only in DB; PostgreSQL time the sole clock; AdminReader/AdminWriter RBAC; customer-production gate fails closed in both bootstrap `main()` and API `_lifespan` (legacy single-admin mapping / dev identity / local assets / missing-or-weak HMAC key — any configured `…_VN` version suffices after rotation; SQLite/DSN via T05 gate) |
| **Verification Command and Pass Count** | `pytest tests/test_admin_auth.py` → 39 passed; full suite → 677 passed (PG fixture); ruff/format/mypy green; client check (biome 54 / tsc / vitest 324), check:e2e, check:tauri, verify_no_secrets all green. PR #40 review: 3 Codex P2 findings substantively fixed (see `docs/evidence/T09-EVIDENCE.md` §3) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | digests-only storage; logs record exception class + actor/session ids only; placeholders-only env example; key ≥ 32 bytes with version rotation |
| **Migration and Rollback** | no new migration (reuses published 026 `admin_sessions`); internal SQLite lane behaviour unchanged |
| **External Authorization Record** | None |
| **Untested Items** | admin frontend pages (T32); multi-instance session behaviour (T36); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T09 Section 14 Ledger Record

```text
任务/工作包：T09 / DB-08
Owner / Reviewer：安全/后端/OPS（Agent 执行）/ chatgpt-codex-connector（PR 评审，3 条 P2 意见已逐条实质修复）
分支 / 基线 SHA：feat/customer-v3-t09-admin-session-csrf / 基线 e50f931（PR #39 squash）
上游规格段落：客户版任务清单 V3 §2 T09、§12.1 DB-08；代码开发清单 V3 §3.2；激活码开发文档 §15；测试与验收规格 V3 §8
改动文件：server/app/admin_auth_routes.py（新增，含非对象 JSON 防护）、server/scripts/issue_admin_exchange_credential.py（新增）、server/tests/test_admin_auth.py（新增 39 用例）、server/app/bootstrap.py（版本化密钥发现+长度校验）、server/app/main.py、server/app/control_auth.py、deploy/customer.env.example（新增）、client/vite.config.ts、11 个 stash 事故重建文件、docs/evidence/T09-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——凭据签发/验签/过期/篡改/单次使用（nonce 摘要主键撞唯一约束）/非对象 JSON 拒收、会话全生命周期、CSRF、RBAC、cookie 形状、安全门逐项+聚合（含仅 _V2 可启动、短密钥启动即拒）、运行时 legacy 403、PG 缺失 503
实现结果：ASX1 一次性 HMAC 凭据 → HttpOnly cookie + CSRF（仅摘要入库，PG 唯一时钟）；客户生产安全门双重 fail-closed，五类启动拒绝全部落地；PR #40 评审 3 条 P2 意见逐条实质修复（密钥轮换启动、启动期强度校验、非对象 JSON 401）
验证命令与通过数：test_admin_auth 39 passed；全量 677 passed（PG fixture）；ruff/format/mypy、client check、check:e2e、check:tauri、verify_no_secrets 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：仅摘要入库；日志无凭据/token；密钥≥32字节版本化；env 样例全占位符
迁移与回滚：无新迁移（复用 026）；内部 SQLite 车道零变化
外部授权记录：无
未测试项：T32 管理端页面；T36 多实例；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T10 — Activation Code Catalog Schema (ACT-01, migration 027)

| Field | Content |
| --- | --- |
| **Owner** | DB/Backend |
| **Reviewer** | chatgpt-codex-connector |
| **Branch / Base SHA** | `feat/customer-v3-t10-activation-code-schema` / `main@4cc04b3` (PR #40 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T10-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T10, §12.2 ACT-01; code checklist §3.3 (frozen `027_activation_code_catalog.py`); activation-code dev doc §5/§11.2/§11.3/§12.1 |
| **Files Changed** | `server/migrations/versions/027_activation_code_catalog.py` (new, frozen name: 6 tables incl. the append-only event table + full constraint set); `server/tests/test_activation_code_schema.py` (new, 10 red→green PG cases); `server/tests/test_postgres_migrations.py` (head assertions 026→027; 026 downgrade-guard adapted to the longer chain with `-2` + transactional-rollback lock); `server/scripts/reconcile_customer_billing.py` (`PG_ONLY_TABLES` += six 027 catalog tables); `server/scripts/sqlite_to_postgres.py` (comment); `server/tests/test_sqlite_to_postgres.py` (new empty-catalog-accepted/row-fails-closed contract test + `validate_revision_pair` head); SQLite-lane head assertions 026→027 in `test_db.py`, `test_character_domain.py`, `test_characters.py`, `test_internal_billing.py`, `test_recharge_orders.py`, `test_settings.py` |
| **Failure Test or Regression Lock** | 10 catalog cases: exact column sets per table (no-plaintext red line), batch shapes (status/positive snapshots/expiry window incl. the same-day timestamp-cast case/creator FK), global-unique `code_digest` across batches, six-state machine shape matrix (GENERATED pre-delivery, ISSUED proven, ACTIVE bound+timestamped, SUSPENDED/REVOKED proven + coupling, EXPIRED unactivated-only, unknown states rejected), partial unique index for one current binding per user, delivery traceability (actor FK/non-blank channel), export ciphertext-only (AEAD+SHA256+key version+short expiry), append-only events (typed CHECK + UPDATE/DELETE refused by trigger), one-shot activation facts (code/user/first-charge order each UNIQUE), downgrade refuses existing activation facts and multi-step downgrades roll back atomically; plus the T07 import contract: empty 027 catalog tables accepted, any catalog row fails closed |
| **Implementation Result** | `027_activation_code_catalog` lands `activation_code_batches` (frozen commercial snapshots), `activation_codes` (digest + key version + masked form only, six-state machine CHECK per acceptance spec §2.1), `activation_code_deliveries`, `activation_code_exports` (AEAD ciphertext + SHA-256 + one-time download audit), `activation_code_activations` (triple-unique one-shot fact) and `activation_code_events` (append-only audit trail enforced by a BEFORE UPDATE OR DELETE trigger); PG-only per 025/026 precedent; `first_device_id` FK deferred to the T16 device revision under the append-only fix rule; T07 cutover tooling keeps the catalog PG-only-exempted-but-empty invariant via `PG_ONLY_TABLES` |
| **Verification Command and Pass Count** | `pytest tests/test_activation_code_schema.py tests/test_postgres_migrations.py tests/test_sqlite_to_postgres.py` → 46 passed; full suite + ruff/format/mypy green (recorded at PR); CI three gates green. Pre-PR review: 1 P2 + 2 P3 fixed; PR #41 Codex review: 2 P1 fixed (six-state machine + append-only event table) — all with red→green locks (see `docs/evidence/T10-EVIDENCE.md`) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | no plaintext code in DB (column-set assertions); digest + versioned keys; exports carry AEAD ciphertext + SHA-256 only; every catalog row traces to a real `users.id` |
| **Migration and Rollback** | new frozen-name migration 027; PG-only (SQLite lane unchanged); symmetric downgrade on an empty catalog; fail-loud once activation facts exist |
| **External Authorization Record** | None |
| **Untested Items** | application layer (T11 generation/HMAC/AEAD export, T12 admin API); activation transaction (T13); device FK (T16); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T10 Section 14 Ledger Record

```text
任务/工作包：T10 / ACT-01
Owner / Reviewer：DB/后端（Agent 执行）/ chatgpt-codex-connector（PR 评审）
分支 / 基线 SHA：feat/customer-v3-t10-activation-code-schema / 基线 4cc04b3（PR #40 squash）
上游规格段落：客户版任务清单 V3 §3 T10、§12.2 ACT-01；代码开发清单 V3 §3.3（027_activation_code_catalog.py 冻结名）；激活码开发文档 §5/§11.2/§11.3/§12.1
改动文件：server/migrations/versions/027_activation_code_catalog.py（新增 5 表全约束）、server/tests/test_activation_code_schema.py（新增 9 用例）、server/tests/test_postgres_migrations.py（head 断言与 downgrade guard 适配 027 链）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 纳入 5 张 027 目录表）、server/scripts/sqlite_to_postgres.py（注释）、server/tests/test_sqlite_to_postgres.py（新增空目录接受/有行拒收合同测试 + validate_revision_pair head 027）、test_db/test_character_domain/test_characters/test_internal_billing/test_recharge_orders/test_settings 六个 SQLite 车道套件 head 断言 026→027 联动
失败测试或回归锁定：先红后绿——9 用例锁定 5 表精确列集（无明文列红线）、批次形状、码摘要全局唯一、状态机形状矩阵、当前有效绑定一户一码（部分唯一索引）、发放可追溯、导出仅密文（AEAD+SHA256+短时效+key version）、激活事实三重唯一（code/user/首充订单）、downgrade 拒绝已有激活事实且多步降级事务性回滚；T07 导入合同测试锁定空目录表接受、目录有行 fail closed
实现结果：027_activation_code_catalog 落地批次/码/发放/导出/激活事实 5 表（PG-only），全部不变量由数据库约束证明；码仅存 HMAC 摘要+key version+掩码；激活事实链禁止 downgrade 删除；first_device_id 留待 T16 设备迁移按追加修复规则补 FK；T07 导入工具保持“目录表 PG-only 豁免但必须为空”不变量
验证命令与通过数：test_activation_code_schema 9 passed + 迁移套件 19 passed + test_sqlite_to_postgres 26 passed；全量与 lint 数字见 PR；CI 三门禁全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：无明文激活码入库（列集断言锁定）；摘要+版本化 key；导出仅 AEAD 密文+SHA256；所有操作行追溯真实 users.id
迁移与回滚：新迁移 027（冻结名）；PG-only（SQLite 车道零变化）；空目录 downgrade 对称；有激活事实时 fail-loud
外部授权记录：无
未测试项：应用层（T11/T12）；激活事务链路（T13）；设备 FK（T16）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T11 — Activation Code Generation, Versioned Digests and AEAD Export (ACT-02 + ACT-03)

| Field | Content |
| --- | --- |
| **Owner** | Backend/Security |
| **Reviewer** | session-internal code review (0 P1 / 2 P2 / 5 P3, all substantively fixed) + PR #42 chatgpt-codex-connector (2 P1 + 1 P2, all substantively resolved) |
| **Branch / Base SHA** | `feat/customer-v3-t11-activation-code-service` / `main@570cd42` (PR #41 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T11-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T11, §12.2 ACT-02/ACT-03; code checklist (frozen `activation_code_service.py`); activation-code dev doc §5/§12.1; acceptance spec §2.1 |
| **Files Changed** | `server/app/activation_code_service.py` (new, 460 lines: normalization/CSPRNG/masking, versioned HMAC/AEAD key resolution, rotation-window digests, six-state matrix, AES-GCM envelope, batch generation + one-time audited export); `server/tests/test_activation_code_service.py` (new, 22 red→green cases: 12 unit + 10 PG on a dedicated migrated fixture database); `deploy/customer.env.example` (T11 key families registered with generation commands and `_V2` rotation comments) |
| **Failure Test or Regression Lock** | 22 cases: entropy floor (140 bit ≥128) + full-alphabet + confusable-free + 500-code uniqueness, human-variant normalization (deterministically seeded confusables), malformed-format rejection, stable masking (prefix + first/last 4 visible, middle 20 hidden), digest determinism/keyed-ness/64-hex, HMAC key env resolution (V2/un-suffixed V1/short-key rejected/missing explicit), key-rotation verification window (old versions verifiable, highest first), full 6×6 transition matrix, AEAD roundtrip with no plaintext in ciphertext, tamper + wrong-batch rejection, AEAD key resolution (invalid base64/short/48-byte rejected — exactly 32 required), GENERATED landing + events + no plaintext in catalog, unknown batch rejected, budget overrun rejected (frozen `quantity` snapshot), concurrent generation serialized by batch-row FOR UPDATE (lock-timeout red test), cross-batch digest uniqueness (60+60), one-time audited download (FOR UPDATE + conditional UPDATE + whole-life caplog plaintext scan), expiry rejected (`downloaded_at` stays NULL), EXPORTED events, cross-batch export refused, unknown export rejected |
| **Implementation Result** | `XS04` 140-bit Crockford-base32 codes (CSPRNG, injectable `rng` for fixtures only); HMAC-SHA256 versioned digests stored as digest + key version + masked form only; AES-256-GCM export envelope bound to its batch via AAD with SHA-256 integrity and short TTL; `fetch_export_package` is the single one-time audited download path; six-state matrix exported for T12/T13; batch `quantity` enforced as the frozen issuance budget |
| **Verification Command and Pass Count** | `pytest tests/test_activation_code_service.py` → 22 passed; full suite → 710 passed (PG fixture); ruff/format/mypy green. Session-internal review: 2 P2 + 5 P3 all fixed with red tests; PR #42 Codex review: 2 P1 + 1 P2 substantively resolved — budget race locked with `FOR UPDATE` (+ red concurrency test), private-COS delivery scoped to T36/COS-01 with code-level hand-off comments, AEAD key validated as exactly 32 bytes (see `docs/evidence/T11-EVIDENCE.md` §PR #42 Review Fixes) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | no predictable codes (CSPRNG + entropy-floor lock); no reversible DB fields (digest + mask only); no plaintext in columns/events/logs (whole-life caplog scan); exports carry AEAD ciphertext + SHA-256 only; download actor persisted; keys ≥32 bytes, versioned rotation with an old-version verification window |
| **Migration and Rollback** | no new migration (application layer over published 027); SQLite lane unchanged |
| **Untested Items** | admin API routes (T12); first-activation atomic transaction (T13); AEAD idempotent recovery (T14); shared rate limiting / anti-enumeration (T15); real private-COS object delivery (T36+); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T11 Section 14 Ledger Record

```text
任务/工作包：T11 / ACT-02 + ACT-03
Owner / Reviewer：后端/安全（Agent 执行）/ 会话内代码评审（0 P1、2 P2 + 5 P3 全部实质修复）+ PR #42 chatgpt-codex-connector（2 P1 + 1 P2 全部实质处置：FOR UPDATE 预算串行化+并发红测试、私有 COS 投递 T36/COS-01 边界论证+代码移交注释、AEAD 密钥恰 32 字节）
分支 / 基线 SHA：feat/customer-v3-t11-activation-code-service / 基线 570cd42（PR #41 squash）
上游规格段落：客户版任务清单 V3 §3 T11、§12.2 ACT-02/ACT-03；代码开发清单 V3（activation_code_service.py 冻结名）；激活码开发文档 §5/§12.1；测试与验收规格 §2.1
改动文件：server/app/activation_code_service.py（新增 460 行）、server/tests/test_activation_code_service.py（新增 22 用例：12 单元 + 10 PG）、deploy/customer.env.example（T11 双密钥族登记）、docs/evidence/T11-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——熵结构（140 bit）、碰撞（500 码唯一+跨批次 60+60）、掩码、旧 key 验证窗、6×6 转移矩阵、AEAD 无明文、篡改/错批次拒、一次性下载审计（caplog 全生命周期）、过期拒、超发拒、跨批次导出拒、未知批次/导出拒
实现结果：XS04 140-bit 码 + 版本化 HMAC 摘要 + 批次绑定 AEAD 导出 + 六态矩阵；批次 quantity 冻结预算；明文仅存于返回值与内存
验证命令与通过数：test_activation_code_service 22 passed；全量 710 passed（PG fixture）；ruff/format/mypy 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：无可预测码、无可逆字段、列/事件/日志全链路无明文、导出仅密文+SHA256、下载 actor 落审计、密钥版本化轮换
迁移与回滚：无新迁移；SQLite 车道零变化
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/发码/灰度/公网发布
未测试项：T12 管理 API；T13 激活事务；T14 AEAD 幂等恢复；T15 限流/防枚举；私有 COS 真实投递；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T12 — Admin Activation Code Management API (ACT-04)

| Field | Content |
| --- | --- |
| **Owner** | Backend/Admin |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t12-admin-activation-routes` / `main@d7e293d` (T11, PR #42 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T12-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T12, §12.2 ACT-04; code checklist (frozen `admin_activation_routes.py`); activation-code dev doc §11.3 (idempotency invariant), §15 (admin write contract); acceptance spec §2 |
| **Files Changed** | `server/app/admin_activation_routes.py` (new, 894 lines: §15 write contract, revision-031 idempotency snapshot layer, 8 routes); `server/migrations/versions/031_admin_write_idempotency.py` (new, PG-only); `server/app/activation_code_service.py` (key-version helpers appended); `server/app/main.py` (router mount); `server/tests/test_admin_activation_routes.py` (new, 38 red→green cases on a dedicated migrated fixture DB); 9 existing test files (head assertions 027→031, downgrade-guard step counts +1); `server/scripts/reconcile_customer_billing.py` + `server/scripts/sqlite_to_postgres.py` (`admin_write_idempotency` in `PG_ONLY_TABLES`) |
| **Failure Test or Regression Lock** | 38 cases: unauthenticated 401 / auditor write 403 / auditor read 200 / CSRF rejected; batch payload validation (400 `BATCH_VALIDATION_FAILED`); generate (unknown batch 404 / closed batch 409 / budget overrun 409 with zero stray rows / missing HMAC or AEAD key 503 with no DB rows); one-time download (second download 409 / expired 409 / unknown 404 / no snapshot row ever persists the plaintext / reason + request id persist on the export audit columns); deliver (channel validation / six-state matrix / duplicate 409); suspend/resume/revoke (shape matrix incl. `suspended_at` cleared on resume, no SUSPENDED→ISSUED edge, events carry reason + request id); listing filters; write-contract order (key → confirm → reason); concurrent same-key two-thread barrier → single batch row + single snapshot row + replay header; same key + same body against a different resource → 409 `IDEMPOTENCY_CONFLICT` with the second resource untouched (PR #43 review P2) |
| **Implementation Result** | §15 admin write contract (CSRF + reason + Idempotency-Key + request id) on the T09 session stack; revision-031 idempotency snapshot layer (unique (actor, route, key digest), request_hash freeze incl. concrete path params, placeholder-then-backfill in the same transaction, business failure rolls the key back); plaintext codes only ever live in the one-time download response (that route bypasses the snapshot layer; `downloaded_at` one-shot is the anti-replay; the download reason + request id persist on the export audit columns — PR #43 review P1); SQLite lane fails closed 503 |
| **Verification Command and Pass Count** | `pytest tests/test_admin_activation_routes.py` → 38 passed; full suite → 748 passed (PG fixture); ruff/format/mypy green (see `docs/evidence/T12-EVIDENCE.md`) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | every admin write traceable to a real `users.id` (batch, snapshot, events); idempotency keys stored as sha256 digests only; plaintext never in DB/snapshot/logs; writer/reader RBAC split; CSRF enforced; request id on every response and event |
| **Migration and Rollback** | new migration 031 (numbered past the frozen 028–030 suggested window per PR #43 review P1; the device/session/queue frozen themes chain off the then-current head); PG-only (SQLite lane unchanged, revision only); symmetric downgrade (snapshots are replay caches, not business facts); T07 cutover keeps `admin_write_idempotency` PG-only-exempted-but-empty |
| **External Authorization Record** | None; PRICE-01 decision unfrozen — no external sales batches may be generated (process red line registered) |
| **Untested Items** | first-activation atomic transaction (T13); AEAD idempotent recovery (T14); shared rate limiting / anti-enumeration (T15); admin frontend pages (T32); multi-instance topology (T36+); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T12 Section 14 Ledger Record

```text
任务/工作包：T12 / ACT-04
Owner / Reviewer：后端/管理（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t12-admin-activation-routes / 基线 d7e293d（T11 PR #42 squash）
上游规格段落：客户版任务清单 V3 §3 T12、§12.2 ACT-04；代码开发清单 V3（admin_activation_routes.py 冻结名）；激活码开发文档 §11.3 幂等不变量、§15 管理写合同；测试与验收规格 §2
改动文件：server/app/admin_activation_routes.py（新增 894 行：写合同+幂等快照层+8 路由）、server/migrations/versions/031_admin_write_idempotency.py（新增，PG-only，含 download 审计耦合 CHECK；编号 031 避开冻结的 028–030 建议区间，PR #43 评审 P1 修复）、server/app/activation_code_service.py（追加 4 个密钥版本解析函数；fetch_export_package 增 download_reason/download_request_id 必填参数）、server/app/main.py（挂载）、server/tests/test_admin_activation_routes.py（新增 38 用例，专用迁移 fixture 库）、9 个既有测试文件（head 断言 027→031，downgrade 守卫步数 +1）、server/scripts/reconcile_customer_billing.py + sqlite_to_postgres.py（PG_ONLY_TABLES 纳入 admin_write_idempotency）、docs/evidence/T12-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——未登录 401/auditor 写 403/auditor 读 200/CSRF 拒；批次校验（名称/面值/额度/数量/有效期 400）；生成（未知批次 404/关闭批次 409/超发 409 零残留/密钥缺失 503 零写入）；下载（一次性/过期/未知/明文不入快照/downloaded_at+reason+request id 审计元组落库）；发放（渠道校验/状态机/重复发放 409）；暂停/恢复/作废（六态矩阵+suspended_at 形状+事件含 reason 与 request id）；幂等（同键同参回放+replay 头/同键异参 409/同键跨资源 409 仅目标 A 生效/并发双线程 barrier 串行化单批次）；写合同（key/confirm/reason 顺序报错）
实现结果：§15 管理写合同（CSRF+reason+Idempotency-Key+request id）+ 028 幂等快照层（actor/route/key digest 唯一、request_hash 冻结、同事务占位-回填、业务失败回滚释放键）+ 8 条路由（批次/生成/下载/发放/暂停/恢复/作废/列表）；明文码仅存于一次性下载响应（绕过快照层，downloaded_at 一次性约束防重放）；SQLite 车道 fail-closed 503
验证命令与通过数：test_admin_activation_routes 38 passed；全量 748 passed（PG fixture）；ruff/format/mypy 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：管理写全链路 actor 可追溯（批次/快照/事件均落 users.id）；幂等键仅存 sha256 摘要；明文不入库不入快照不入日志；RBAC 写/读分离；CSRF 强制；request id 全响应+全事件
迁移与回滚：新迁移 031（避开冻结的 028–030 建议编号区间，PR #43 评审 P1；设备/会话/队列冻结主题将来从当日 head 顺延链接）；PG-only（SQLite 车道零变化，仅 revision 推进）；downgrade 对称（快照为重放缓存非业务事实）；T07 导入工具保持 admin_write_idempotency PG-only 豁免但必须为空
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布；PRICE-01 决议未冻结前不得生成对外销售批次（流程红线已登记）
未测试项：首次激活原子事务（T13）；AEAD 幂等恢复（T14）；共享限流与防枚举（T15）；管理端前端页面（T32）；多实例部署形态（T36+）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```


## T13 — First-Activation Atomic Transaction (ACT-05 / ACT-06)

| Field | Content |
| --- | --- |
| **Owner** | Backend/Billing |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t13-first-activation` / `main@83a5bb2` (T12, PR #43 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T13-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T13, §12.2 ACT-05/ACT-06; code checklist (frozen `activation_code_routes.py`, migrations `028_customer_devices_and_activations` / `029_customer_sessions_and_idempotency`); activation-code dev doc §11.2 (idempotency envelope), §11.3 (concurrency invariants), §12.1 (first-activation transaction), §7 (key red lines); acceptance spec §2 |
| **Files Changed** | `server/app/activation_code_routes.py` (new, 742 lines: versioned key resolution, keyed digests, AES-GCM envelope, one-transaction activation chain, unified anti-enumeration); `server/migrations/versions/028_customer_devices_and_activations.py` (new, PG-only, frozen name: two-slot `customer_devices` + digest/version columns + partial unique indexes `uq_customer_devices_slot`/`uq_customer_devices_fingerprint` + 027 deferred `first_device_id` FK attach + activated-guard downgrade refusal); `server/migrations/versions/029_customer_sessions_and_idempotency.py` (new, PG-only, frozen name: `customer_session_state` single-session invariant + epoch monotonic trigger, `customer_session_events` append-only trigger, `customer_idempotency_envelopes` unique (operation, scope, key_digest) + three-state coupling CHECK + purged_at); `server/app/main.py` (router mount; PR #44 review P1 — CORS `allow_headers` adds `Idempotency-Key`/`X-Request-Id`, `expose_headers` adds `X-Request-Id`/`X-Idempotent-Replay`); `server/tests/test_activation_code_routes.py` (new, 24 red→green cases: 16 original + 5 session-review + 3 PR #44-review regressions); `server/tests/test_customer_activation.py` (new, 5 concurrency cases incl. ACT-06 100 threads); `server/tests/test_activation_code_schema.py` (2 T10 cases adapted + 3 PR #44-review trigger cases); `server/tests/test_admin_activation_routes.py` (TRUNCATE covers new tables); 9 test files + `server/scripts/sqlite_to_postgres.py` + `server/scripts/reconcile_customer_billing.py` (head 031→029, PG_ONLY_TABLES + four new tables) |
| **Failure Test or Regression Lock** | 32 cases: atomic happy path (201, full chain incl. wallet balance + epoch-1 90 s lease); request-id echo; missing Idempotency-Key 400; unified 400 ×7 (unknown/malformed/expired/suspended/revoked/active/generated); same fingerprint second code 409 `USER_ALREADY_ACTIVATED`; same key + same body replays identical identity (replay header + original request id); same key + different body 409; SQLite fail-closed 503 (runtime checked before keys); log scan — no plaintext code/token; 100 threads/one barrier/one code → one 201 + 99 × 400 with exactly one of each fact row; concurrent same-key recovery → identical username/device token/session token, one CHARGE; concurrent same-fingerprint cross-code → one 201 + one 409, losing code untouched; business failure releases the key; username collision regenerates in-transaction (savepoint); session-review regressions (naive batch expiry → unified 400, envelope row shape, recovery-window env override, expired-window refusal, whitespace-padding replay); PR #44-review regressions (CORS preflight permits `Idempotency-Key`/`X-Request-Id` + actual response exposes replay markers; rotation-window fingerprint check — V1-bound device + V2 added → second code still 409 with one user/CHARGE; envelope scoped under V1 still replays after V2 is added; both 029 triggers installed — epoch decrease and audit-table UPDATE/DELETE rejected) |
| **Implementation Result** | `POST /api/customer/activate` creates the whole customer chain in exactly one `pg_transaction()`: FOR UPDATE code lock → server-generated `customer` user (savepoint retry ≤5) → funded wallet → slot-1 device (keyed digests + versions) → PAID `provider=activation_code` order (frozen batch price, base=charged per PRICE-01) → unique CHARGE (`activation_code:charge:{order_id}`) → activation fact (attaches 027's dangling FK) → ACTIVE code + ACTIVATED event → epoch-1 session + 90 s lease; idempotency envelope per revision 029 (sha256 key digest only, AAD-bound AES-GCM sealed response, 24 h recovery window, business failure rolls the placeholder back); unified 400 anti-enumeration; PG runtime fails closed 503 before key resolution |
| **Verification Command and Pass Count** | `pytest tests/test_activation_code_routes.py tests/test_customer_activation.py tests/test_activation_code_schema.py` → 42 passed; full suite → 780 passed (PG fixture; the three previously-skipped network-dependent cases also ran green); ruff/format/mypy green; client workspace → 324 passed; biome e2e clean; cargo fmt+check clean; secret-scan patterns clean (see `docs/evidence/T13-EVIDENCE.md`) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | plaintext code/device token/session token only in the HTTP response and the AEAD envelope column (log-scan test); idempotency keys stored as sha256 digests; fingerprint + credential digests keyed HMAC with versioned rotation; unified anti-enumeration rejections; request id on every response, event and replay |
| **Migration and Rollback** | new migrations 028/029 (frozen names, chained off the live head 031; 030 stays reserved for T25); PG-only (SQLite lane advances the revision only); 028 refuses downgrade while an activation fact exists (audit-chain guard); 029 downgrade symmetric (runtime caches, not business facts); T07 cutover keeps the four new tables PG-only-exempted-but-empty |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | AEAD recovery completion + expired-envelope cleanup (T14/ACT-07); shared rate limiting + timing parity (T15/ACT-08); second device + pairing (T16–T18); session lifecycle (T19–T20); ZPay coexistence recharge (T22/BILL-01); frontend/desktop (T28+); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T13 Section 14 Ledger Record

```text
任务/工作包：T13 / ACT-05 + ACT-06
Owner / Reviewer：后端/账务（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t13-first-activation / 基线 83a5bb2（T12 PR #43 squash）
上游规格段落：客户版任务清单 V3 §3 T13、§12.2 ACT-05/ACT-06；代码开发清单 V3（activation_code_routes.py、028/029 迁移冻结名）；激活码开发文档 §11.2 幂等信封、§11.3 并发不变量、§12.1 首次激活事务、§7 密钥红线；测试与验收规格 §2
改动文件：server/app/activation_code_routes.py（新增 742 行）、server/migrations/versions/028_customer_devices_and_activations.py（新增，PG-only，冻结名）、server/migrations/versions/029_customer_sessions_and_idempotency.py（新增，PG-only，冻结名）、server/app/main.py（挂载+CORS 幂等头/expose 头）、server/tests/test_activation_code_routes.py（新增 24 用例，含会话评审 5 条+PR #44 评审 3 条回归）、server/tests/test_customer_activation.py（新增 5 并发用例含 ACT-06 100 并发）、server/tests/test_activation_code_schema.py（2 用例适配+3 条触发器回归）、server/tests/test_admin_activation_routes.py（TRUNCATE 纳新表）、9 个既有测试文件+2 个脚本（head 断言 031→029、PG_ONLY_TABLES 纳四张新表）、docs/evidence/T13-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——契约 24 例（含会话评审 P2/P3 修复回归：naive 过期统一 400、信封形状、恢复窗 env、过期拒绝重放、空白填充重放；PR #44 评审回归：CORS 预检允许幂等头/实际响应 expose 回放标记、轮换窗口指纹检查 V1 绑定设备+V2 新增二码仍 409 且一 user/一 CHARGE、V1 作用域信封 V2 新增后仍可重放）（原子全链/请求 id 回显/幂等键必填/统一 400 七场景/同指纹二码 409/同键同体重放+replay 头/同键异体 409/SQLite fail-closed 503/日志无明文）；并发 5 例（100 并发恰一成功+全库恰一份事实/同键并发恢复同一身份且仅一笔 CHARGE/同指纹跨码并发一胜一 409/业务失败释放幂等键/用户名碰撞事务内保存点重试）；schema 2 例适配+3 例触发器回归（两触发器存在、epoch 降低拒绝、审计表 UPDATE/DELETE 拒绝）
实现结果：单事务激活链（user+wallet+slot1+PAID order+CHARGE+activation+ACTIVE code+事件+epoch-1 session/90s 租约）全有或全无；幂等信封 029（摘要入库/AAD 绑定/24h 恢复窗/业务失败回滚释放键）；统一 400 防枚举；PG fail-closed 先于密钥检查
验证命令与通过数：专项 42 passed；全量 780 passed（PG fixture，含此前 3 个 skip 的网络依赖用例）；ruff/format/mypy 全绿；client 324 passed；biome/cargo/secret 扫描 clean
证据层级：AUTOMATED_VERIFIED
安全与可观测性：明文只存在于 HTTP 响应与 AEAD 信封列；幂等键仅存摘要；指纹/凭据 keyed HMAC 版本化；统一防枚举；request id 全链路
迁移与回滚：028/029 冻结名从 head 031 顺延；PG-only；028 激活存在拒绝降级；029 对称；T07 导入新表必须为空
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T14 幂等恢复完善；T15 限流/防枚举；T16-T18 设备；T19-T20 会话；T22 ZPay 续充；T28+ 前端；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```


## T14 — AEAD Idempotency Recovery & Expired-Envelope Cleanup (ACT-07)

| Field | Content |
| --- | --- |
| **Owner** | Backend/Security |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t14-idempotency-recovery` / `main@fec36c7` (T13, PR #44 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T14-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T14, §12.2 ACT-07; code checklist §9.1 (frozen `customer_idempotency.py` / `test_customer_idempotency.py`), §10.1 (purge CLI), §12 (maintenance service/timer); activation-code dev doc §11.2 (idempotency envelope), §7 (key red lines); acceptance spec §2 |
| **Failure Test or Regression Lock** | 18 cases: module units 8 (request hash whitespace-stable + param-distinguishing; key digest hides the raw key; seal/open round-trip; wrong AAD rejected; ciphertext holds no plaintext secret — ACT-07; AEAD rotation window resolves V1/V2; retired key version fails closed; recovery-window env override); PG integration 7 on the dedicated migrated fixture DB (envelope lifecycle insert→complete→load→open; same-key different-hash conflict evidence; expired window visible; purge clears only expired — expired/live/already-purged triple, second run returns 0; purged envelope no longer recoverable; the 029-reserved recovery index exists); CLI 3 (real purge, dry-run keeps rows, missing DSN exits 1). T13's 42 activation cases kept green as the refactor regression lock (incl. the 100-thread ACT-06 race and same-key recovery) |
| **Implementation Result** | The envelope engine extracted from T13's route into the frozen shared module `server/app/customer_idempotency.py` (operation-generalized for T17/T19/T22 reuse): versioned AEAD keys, sha256 key digest + normalized request hash, AES-256-GCM seal/open with `operation/scope/key_digest` AAD binding, envelope persistence, and the T14 cleanup story — `count_expired_envelopes` / `purge_expired_envelopes` null the ciphertext triple under `purged_at` in one UPDATE walking the 029-reserved recovery index (CHECK coupling keeps a purged row payload-free; idempotent re-run). The maintenance CLI (`scripts/purge_idempotency_envelopes.py`, dry-run / fail-closed DSN, counts-only output) runs from the new sandboxed `video-replica-maintenance.service` daily timer (04:10, staggered against the 03:20 backup). `activation_code_routes.py` refactored onto the module with zero behaviour change; expired windows answer 409 (key spent); retired key versions inside the window answer 503 |
| **Verification Command and Pass Count** | `pytest tests/test_customer_idempotency.py` → 18 passed; four-file T13+T14 special → 60 passed; full suite → 795 passed + 3 skipped (798 collected = 780 baseline + 18 new, PG fixture, zero regressions; the 3 skips are the pre-existing Windows-environment bash cases — POSIX launcher ×2 + secret-scan shell — which run on the Linux CI gates; one gate1_e2e thread-timing flaky in an earlier run was isolated and passed on re-run); ruff/format/mypy green (142 files formatted, 58 source files typed); post-review fix re-run: special 60 passed + full 795/3 re-confirmed; CLI verified end-to-end (--help, missing-DSN exit 1, unmigrated database fails loud) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | No directly usable plaintext secret in the envelope (sealed ciphertext is the only persisted copy of the one-time response; tests lock both the key name and the value out of the ciphertext); raw idempotency key stored as sha256 digest only; AAD binding prevents cross-row replay; purge output counts only; retired key versions fail closed 503 inside the recovery window; expiry decided on the server clock only |
| **Migration and Rollback** | No new migration (029 already reserved `purged_at`, the payload three-state coupling CHECK and the recovery index — T14 ships the job that uses them); rollback = revert code (envelope schema unchanged; the purge is safely interruptible and re-runnable) |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | T17/T19/T22 reuse of the shared engine (delivered by those tasks); real systemd environment for the maintenance timer (ops acceptance lands with the T36 deployment manual); shared rate limiting + timing parity (T15/ACT-08); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T14 Section 14 Ledger Record

```text
任务/工作包：T14 / ACT-07
Owner / Reviewer：后端/安全（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t14-idempotency-recovery / 基线 fec36c7（T13 PR #44 squash）
上游规格段落：客户版任务清单 V3 §3 T14、§12.2 ACT-07；代码开发清单 V3 §9.1 customer_idempotency.py/test_customer_idempotency.py、§10.1 purge CLI、§12 maintenance service/timer 冻结名；激活码开发文档 §11.2 幂等信封、§7 密钥红线；测试与验收规格 §2
改动文件：server/app/customer_idempotency.py（新增 334 行冻结名）、server/scripts/purge_idempotency_envelopes.py（新增维护 CLI）、server/app/activation_code_routes.py（重构接入共享模块，行为零变化）、server/tests/test_customer_idempotency.py（新增 18 用例）、deploy/systemd/video-replica-maintenance.service/.timer（新增冻结名；OnFailure=告警挂钩）、deploy/systemd/video-replica-maintenance-alert.service（新增：purge 重试预算耗尽的 ALERT 级 journald 告警单元，PR #45 评审 P2）、deploy/customer.env.example（补 T13 设备域密钥占位+T14 AEAD 密钥/恢复窗口占位）、docs/evidence/T14-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 18 例（模块级 8+PG 集成 7+CLI 3）；T13 42 例作为重构回归锁定全部保持绿（含 ACT-06 100 并发与同键恢复）
实现结果：幂等信封引擎提取为共享模块（operation 泛化供 T17/T19/T22 复用）；恢复窗口到期后同 key 409；purge 单条 UPDATE 清空密文三列并记 purged_at（029 CHECK 耦合、幂等重跑为 0）；maintenance timer 每日清理；路由重构后信封行为与 T13 完全一致
验证命令与通过数：专项 18 passed；T13+T14 四文件 60 passed；全量 795 passed + 3 skipped（总数 798，零回归；3 个 skip 为既存 Windows 环境性 bash 用例，Linux CI 上全跑；含一次 gate1_e2e 线程时序 flaky 的隔离重跑）；评审修复后终跑专项 60 + 全量 795/3 复确认；ruff/format/mypy 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：信封不保存可直接使用的明文 secret（密文为唯一持久化副本，测试锁定）；原始幂等键仅存 SHA-256 摘要；AAD 绑定防跨行重放；purge 输出仅计数；退役密钥版本 503 fail-closed；到期判定只用服务器时钟
迁移与回滚：无新迁移（029 已预留全部结构）；回滚=还原代码，purge 可安全中止与重跑
外部授权记录：无
未测试项：T17/T19/T22 共享引擎复用；maintenance timer 真实 systemd 环境（随 T36）；T15；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```


## T15 — Shared Multi-Instance Rate Limiting & Anti-Enumeration (ACT-08)

| Field | Content |
| --- | --- |
| **Owner** | Security/Backend |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t15-rate-limit-anti-enumeration` / `main@c206323` (T14, PR #45 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T15-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T15, §12.2 ACT-08; code checklist §3 (frozen `security_rate_limit.py`), §11.1 (frozen `test_customer_security.py`); activation-code dev doc §11.3 (concurrency & abuse), §7 (key red lines); acceptance spec §6 |
| **Failure Test or Regression Lock** | 18 cases: module units 3 (bucket key; env overrides + non-numeric/non-positive fall back to safe defaults — an env typo can never disable the limits; measurable constant anti-enumeration delay baseline); PG integration 8 on the dedicated migrated fixture DB `t15_customer_security_test` (window allows then blocks; window resets after expiry; dimensions independent; **two independent connections — two API instances — share one budget atomically**; failure record + trailing-window metrics; metrics window scoping; alert threshold; the failure record holds no plaintext code); route integration 5 (429 with Retry-After; malformed requests share the IP budget; the code dimension blocks a single-code burst across IPs; every unified rejection records a failure event with the digest identifier; unknown/malformed/expired rejections all apply the constant delay); review-fix locks 2 (a fully validated idempotent replay spends no rate-limit budget while the next fresh attempt still trips the limiter; downgrade refuses once failure events exist, empty schema round-trips 032 → 029 → head). T13/T14 five-file 78 passed as the regression lock |
| **Implementation Result** | The shared limiter lands in PostgreSQL (one atomic UPSERT per consumption — no Redis, no message queue, per the architecture red lines): the activation route spends the IP dimension on *every* attempt (malformed included) and the code dimension (keyed digest, never the plaintext) only for well-formed codes; exceeding either answers 429 `RATE_LIMITED` with `Retry-After`; windows reset after expiry. Every code-side rejection appends an append-only (trigger-guarded) failure event aggregatable into trailing-window metrics, crossing the operator threshold emits an ERROR-level log record — the T37/OPS-02 hook; the unified rejection path burns a constant PBKDF2-SHA256 cost so unknown/expired/suspended/revoked/already-active codes share one latency profile with the T13 unified 400 body. A read-only probe ahead of the limiter lets a fully validated idempotent replay short-circuit with zero budget (T14 retry contract preserved) |
| **Verification Command and Pass Count** | `pytest tests/test_customer_security.py` → 18 passed; five-file T13/T14+T15 special → 78 passed; full suite → 816 passed (814 after the implementation round + 2 review-fix tests, PG fixture, zero regressions); ruff/format/mypy green (145 files formatted, 59 source files typed); post-review-fix re-run: special 78 + full 816 re-confirmed |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | code-dimension counters and failure events store the keyed digest only (test-locked: no plaintext code in the audit table); append-only trigger refuses any rewrite of the failure audit; the anti-enumeration delay runs even when the audit write fails (the timing profile never depends on database health); windows and metrics decided on the server clock only; 429 `Retry-After` rides the exception path; alert-threshold crossing emits an ERROR-level structured log for the T37/OPS-02 pipeline |
| **Migration and Rollback** | New migration 032 (chains off head 029; 030 stays reserved for T25 — T12/031 numbering precedent); PG-only (SQLite advances the revision only); downgrade refuses once failure events exist (audit must survive a rollback — 026/028 guard precedent); retention cleanup must not be a plain DELETE against the append-only trigger (session-identifier exemption or partition drops — documented in the migration for the future OPS task); `PG_ONLY_TABLES` in the T07 import/reconcile tool registers both tables keeping the fail-closed contract |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | T19 login-lane reuse (login:ip / login:account dimensions tabled but unrouted); real multi-instance load-balancer topology (T36); reverse-proxy real-client-IP delivery verification (ops acceptance with T36); T37/OPS-02 alerting-pipeline consumption; STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T15 Section 14 Ledger Record

```text
任务/工作包：T15 / ACT-08
Owner / Reviewer：安全/后端（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t15-rate-limit-anti-enumeration / 基线 c206323（T14 PR #45 squash）
上游规格段落：客户版任务清单 V3 §3 T15、§12.2 ACT-08；代码开发清单 V3 §3 security_rate_limit.py、§11.1 test_customer_security.py 冻结名；激活码开发文档 §11.3 并发与滥用、§7 密钥红线；测试与验收规格 §6
改动文件：server/migrations/versions/032_security_rate_limits.py（新增：counters+append-only failures 两表、维度 CHECK 词表、downgrade 守卫、保留期约束注释）、server/app/security_rate_limit.py（新增 308 行冻结名：共享固定窗口消费 UPSERT、失败审计+指标+告警阈值、常数防枚举时延、env 安全回退）、server/app/activation_code_routes.py（限流接入：IP 维度含 malformed、code 维度仅合法格式码、统一失败审计+告警日志+常数时延、429+Retry-After、限流前只读 replay 预检）、server/tests/test_customer_security.py（新增 18 用例）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 登记 032 两表）、server/scripts/sqlite_to_postgres.py（注释同步）、deploy/customer.env.example（4 个限流变量+反代 IP 部署指导）、11 个测试文件（head 断言 029→032 约 20 处、downgrade 守卫测试改绝对 revision、T13 路由测试限流预算 env 提升+security 表 truncate）、docs/evidence/T15-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 18 例——模块级 3+PG 集成 8（专用迁移库 t15_customer_security_test）+路由集成 5+评审锁定 2；T13/T14 五文件 78 passed 作为回归锁定
实现结果：多 API 实例共享限流落地 PG（单 UPSERT 原子消费，无 Redis/MQ）；激活接口 IP 维度全部尝试（含 malformed）计数、code 维度按 HMAC 摘要计数（明文永不过库）；超限 429 RATE_LIMITED+Retry-After；窗口过期自动重置；每次拒绝追加 append-only 审计事件并聚合成指标，超阈值打 ERROR 告警日志（T37/OPS-02 消费）；未知/过期/作废等全部拒绝共享统一 400 响应体+常数 PBKDF2 时延（关闭时序侧信道）；幂等 replay 只读预检零预算短路（T14 重试无副作用契约保持）
验证命令与通过数：专项 18 passed；五文件 78 passed；全量 816 passed（814 实现轮 + 2 评审修复新增，零回归，PG fixture）；ruff/format/mypy 全绿（145 files formatted，59 source files typed）；实现轮全量 814 + 静态全绿后经代码评审修复 3 条（1 P2+2 P3）复跑专项 18 + 回归 60 + 静态全绿 + 全量 816 复确认
证据层级：AUTOMATED_VERIFIED
安全与可观测性：计数器与审计表 code 维度只存 keyed 摘要（测试锁定明文不出现）；append-only 触发器拒绝改写审计；审计写失败时时延照常（时序剖面不依赖数据库健康）；窗口与指标只用服务器时钟；env 非法值回退安全默认；429 头经异常路径携带；告警阈值 ERROR 日志为 T37 管道挂钩
迁移与回滚：新迁移 032（避开冻结 028–030 区间，从 head 029 顺延；030 仍留给 T25）；PG-only（SQLite 仅 revision 推进）；downgrade 非空守卫；T07 导入工具登记 PG_ONLY_TABLES 保持 fail-closed 契约；回滚=032 downgrade（空表时对称）+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T19 登录车道复用；真实多实例负载均衡拓扑联测（T36）；反代 IP 传递的真实部署验证（随 T36）；T37/OPS-02 告警管道消费；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```


## T16 — Two Device Slots, Credentials & Unbind History (DEV-01)

| Field | Value |
| --- | --- |
| **Task ID** | T16 / DEV-01 |
| **Owner / Reviewer** | Backend/DB (Agent) / session-internal code review |
| **Branch / Base SHA** | feat/customer-v3-t16-device-slots-unbind / base 517e1d2 (T15, PR #46 squash) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T16-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §3 T16, §12.3 DEV-01; code checklist §3.2 (frozen customer_device_service.py / customer_device_routes.py), §3.3 (frozen test_customer_devices.py); dev doc §3.2 device rules, §6.1 API table, §12.4 fencing, §13.2 error codes; acceptance spec §2.2/§3.3 |
| **Failure Test or Regression Lock** | 22 cases: module units 2 (digest determinism; slot-count constant) + credential & two-slot view 7 (three-state 401s; slot view after activation) + unbind semantics 7 (slot reuse + history preserved; atomic session revocation; 404 missing/IDOR; 409 already-unbound; other-device unbind leaves own slot/session intact) + third-device block 2 (next_free_slot state machine; PG-level UniqueViolation/CheckViolation) + key rotation 1 (V2-issued credential under dual-version config; V2 retired → 401) + review-fix locks 3 (PG fail-closed 503; unconfigured keys → 503 not 401; REVOKED row → 401 DEVICE_REVOKED) + idempotent unbind 3 (PR #47 Codex P2: missing key → 400 with the row untouched; lost-204 own-device retry replays the sealed 204 with zero re-execution; same key + different target → 409 IDEMPOTENCY_CONFLICT) |
| **Implementation Result** | The two current device slots, credentials, unbind history and the third-device block land as the application layer over the 028 schema (no new migration): the device credential authenticates via Authorization: Bearer with keyed HMAC-SHA256 digests probed across every configured key version; GET /api/customer/devices answers the two-slot status plus release history; DELETE flips BOUND→UNBOUND (row never deleted, slot immediately reusable — immune to the DEV-01 No-Go by the partial unique index) with a mandatory Idempotency-Key (PR #47 Codex P2: the T14 envelope engine seals the audit payload, a lost 204 replays with the same key + same target, the recovery probe runs before credential authentication, the same key on a different target answers 409 IDEMPOTENCY_CONFLICT) and atomically revokes the session riding the released device (epoch+1, immediately-expired lease with full microsecond precision + a 1µs GREATEST backstop, LOGOUT device_unbound event); both slots full → next_free_slot is None and PostgreSQL refuses a third BOUND row; stable error codes 401 REQUIRED/INVALID/REVOKED, 404 (missing = foreign, no IDOR oracle), 409 ALREADY_UNBOUND, 503 fail-closed (missing PG runtime / key misconfiguration — never a misleading 401 or a 500); unbind clock sampled from SELECT now() inside the transaction (SES-01) |
| **Verification Command and Pass Count** | pytest tests/test_customer_devices.py → 22 passed; full suite → 839 passed + 1 time-boundary flaky re-run green → 840 confirmed (the flaky is test_e2e_fake_provider.py, storage-signature x-expires second rollover, SQLite generation lane — unrelated to this task's files); ruff/format/mypy all green (148 files formatted, 61 source files typed); implementation round 16 red→green + static green + full 834, then session review fixes (1 P2 + 3 P3) re-verified: special 19 + full 837, then PR #47 Codex review fixes (3 P2: lease microsecond precision / DELETE idempotency key + envelope recovery / ledger escape corruption) re-verified: special 22 + static green + full 840 |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | the device token reaches the database only as a keyed digest (rotation-window probing); logs and events carry identifiers only; IDOR answers 404 identically for missing and foreign devices; the INVALID/REVOKED 401 distinction is the §13.2 client wipe signal (tokens are 256-bit random, not enumerable); key misconfiguration answers 503 rather than a misleading 401/500; the unbind audit event carries the request id; the success log prints after commit |
| **Migration and Rollback** | no new migration (028's customer_devices / customer_session_state / customer_session_events schema fully ready); rollback = code revert (no schema change) |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | device endpoints not behind the shared limiter (registered for the T19 review; device tokens are 256-bit, brute-force infeasible); thread-level same-key concurrent-DELETE proof (envelope ON CONFLICT + FOR UPDATE semantics cover it, T13/T14 precedent); T17 enroll wiring of next_free_slot; client OpenAPI regeneration (frontend-integration gate); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T16 Section 14 Ledger Record

```text
任务/工作包：T16 / DEV-01
Owner / Reviewer：后端/DB（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t16-device-slots-unbind / 基线 517e1d2（T15 PR #46 squash）
上游规格段落：客户版任务清单 V3 §3 T16、§12.3 DEV-01；代码开发清单 V3 §3.2 customer_device_service.py/customer_device_routes.py、§3.3 test_customer_devices.py 冻结名；激活码开发文档 §3.2 设备规则、§6.1 API 表、§12.4 fencing、§13.2 错误码；测试与验收规格 §2.2/§3.3
改动文件：server/app/customer_device_service.py（新增：跨密钥版本凭据解析、两槽视图、next_free_slot、unbind_device 原子会话吊销）、server/app/customer_device_routes.py（新增：Bearer 设备凭据鉴权、GET/DELETE 两路由、稳定错误码、503 fail-closed、PG 事务内时钟、提交后日志、幂等键+信封恢复）、server/tests/test_customer_devices.py（新增 22 用例，专用迁移库 t16_customer_devices_test）、server/app/main.py（路由挂载 +2 行）、docs/evidence/T16-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 22 例——模块级 2（摘要确定性/槽数常量）+凭据与视图 7（三态 401、激活后槽视图）+解绑语义 7（槽复用+历史保留、原子会话吊销、404 缺失/IDOR、409 重复、他设备解绑自身不受扰）+第三设备阻断 2（next_free_slot 状态机 + PG 层 UniqueViolation/CheckViolation）+密钥轮换 1（V2 签发双版本可鉴权、V2 退役后 401）+评审锁定 3（PG fail-closed 503、密钥未配置 503 非 401、REVOKED 行 401）+幂等解绑 3（PR #47 Codex P2：无键 400+行未动、丢 204 同键重试重放零重执行、同键异目标 409）
实现结果：两当前设备槽+凭据+解绑历史+第三设备阻断落地应用层（028 schema 无新迁移）：设备凭据 Bearer 鉴权（keyed digest 跨版本探测，明文永不过库）；GET /api/customer/devices 返回两槽状态+释放历史；DELETE 解绑（BOUND→UNBOUND+unbound_at，行不删除，槽立即可复用——partial unique index 免疫 DEV-01 No-Go）携带强制 Idempotency-Key（PR #47 Codex P2：T14 信封引擎密封审计载荷，丢失 204 同键同目标可重放，预检在凭据鉴权前，同键异目标 409 IDEMPOTENCY_CONFLICT，无键 400）；解绑原子吊销所骑会话（epoch+1+立即过期租约（全微秒精度+GREATEST 1µs 兑底，PR #47 Codex P2）+LOGOUT device_unbound 事件）；两槽满 next_free_slot=None+数据库拒绝第三行；错误码 401 REQUIRED/INVALID/REVOKED、404（缺失=他人，无 IDOR 预言）、409 ALREADY_UNBOUND、503 fail-closed（无 PG/密钥未配置）；解绑时钟取 PG 事务内 now()（SES-01）
验证命令与通过数：专项 22 passed；全量 839 passed + 1 时间边界 flaky 单独复跑通过→ 840 确认（flaky 为 test_e2e_fake_provider.py 存储签名 x-expires 秒翻转，SQLite 生成 lane，与本任务文件无依赖）；ruff/format/mypy 全绿（148 files formatted，61 source files typed）；实现轮 16 红→绿 + 静态全绿 + 全量 834 后经会话内代码评审修复 1 P2+3 P3 复跑专项 19 + 全量 837，再经 PR #47 Codex 评审修复 3 P2（lease 微秒精度/DELETE 幂等键+信封恢复/账本转义）复跑专项 22 + 静态全绿 + 全量 840 复确认
证据层级：AUTOMATED_VERIFIED
安全与可观测性：设备凭据只以 keyed HMAC-SHA256 摘要过库（跨版本探测兼容轮换窗口）；日志与事件仅含标识符；IDOR 统一 404（缺失=他人同应答）；401 INVALID 与 REVOKED 的区分是 §13.2 客户端擦除信号（token 为 256-bit 随机+keyed digest，不可枚举构造）；密钥配置故障 503 而非误导 401/500；解绑审计事件携带 request_id；成功日志提交后打印
迁移与回滚：无新迁移（028 的 customer_devices/customer_session_state/customer_session_events schema 完全就绪）；回滚=还原代码（无 schema 变更）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：设备端点未接入共享限流（登记为 T19 评审项；设备 token 256-bit 不可暴破）；同键并发双 DELETE 线程级证明（信封 ON CONFLICT + FOR UPDATE 语义覆盖，T13/T14 同前例）；T17 enroll 接入 next_free_slot 的路由级联测；客户端 OpenAPI 重新生成（前端接入任务门禁）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T17 — Second-Device Enroll, One-Shot Pairing & First-Device Approval (DEV-02)

| Field | Value |
| --- | --- |
| **Task ID** | T17 / DEV-02 |
| **Owner / Reviewer** | Backend (Agent) / Codex independent review (PR #49 REQUEST_CHANGES: 1 P1 + 6 P2 + 1 P3, all substantively fixed) + GitHub connector review (P1 approval lane not restricted to the first device, fixed) |
| **Branch / Base SHA** | feat/customer-v3-t17-second-device-pairing / base e30ea64 (main after PR #48; the branch merged origin/main — PR #48's 033–036 chain landed first, so the T17 migration was renumbered 033→037 and revision 034's canonical probe key was adopted) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T17-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §4 T17, §12.3 DEV-02; code checklist §3.2 (frozen customer_device_service.py / customer_device_routes.py), §3.3 (frozen test_customer_devices.py), migration theme 037 (renumbered from 033 after PR #48's 033–036 chain landed on main first); dev doc §12.2 six-step contract, §6.1 API table, §13.2 error codes; acceptance spec §6 |
| **Failure Test or Regression Lock** | 30 cases: enroll contract 7 (mandatory Idempotency-Key; PENDING created bound to the candidate V2-keyed digest with shape-coupled columns empty; same-key retry returns the same pairing id with zero envelopes; unknown code → unified 400 PAIRING_UNAVAILABLE; ISSUED-never-activated code → the same unified 400; already-bound fingerprint → 409 USER_ALREADY_ACTIVATED; both slots BOUND → 409 DEVICE_SLOTS_FULL with no pairing row) + full six-step flow 3 (enroll → approve → enroll again answers 201 with slot-2 credentials, pairing CONSUMED with consumed_at/consumed_device_id, device row BOUND on slot 2 with the candidate's name/platform and the owner's user_id, and no second charge — 1 recharge order / 1 wallet transaction / 1 customer user; lost-201 retry replays the sealed credentials with zero re-execution; same key + different body → 409 IDEMPOTENCY_CONFLICT) + concurrency & expiry 5 (two approved rivals race the consume branch behind a 2-thread barrier: exactly one 201 + one 409 DEVICE_SLOTS_FULL, one CONSUMED row, two BOUND devices; lapsed PENDING flips EXPIRED and a fresh request is created; lapsed APPROVED restarts fresh with the lapsed approval kept visible in the audit; consumption against two BOUND slots answers 409 and the pairing row stays APPROVED) + approve contract 7 (missing Bearer → 401; happy path records approved_at + approved_by_device_id; missing / random / cross-code pairings all answer one 404 with the foreign pairing untouched — IDOR; repeated approval idempotent with identical body; after consumption → 409 PAIRING_ALREADY_CONSUMED; lapsed → 409 PAIRING_EXPIRED with the row flipped EXPIRED; a pairing naming the approver's own digest → 409 PAIRING_SELF_APPROVAL) + transferability 1 (a different fingerprint enrolling the same code gets its own PENDING pairing — the approved row stays APPROVED untouched and no second device row appears) + Codex review regression locks 6 (PR #49: mid-flight unbind — a stale authenticated device object fed to the service layer answers revoked with the pairing untouched, the P1; key rotation between 202 and 201 keeps the stored (digest, version) pair truthful; a lapsed APPROVED flips EXPIRED on the approve path and a fresh request + approval succeeds; the shape CHECK rejects a PENDING-with-approved_at and an EXPIRED-with-lone-approved_at; the partial unique blocks a second active row while EXPIRED/CONSUMED rows free the slot for reuse; the 037 downgrade refuses once pairing rows exist and downgrades symmetrically once emptied) + strengthened: the slots-full 409 leaves zero device_enroll envelopes and the very same key finishes the consumption once a slot is freed + GitHub review locks 2 (PR #49 connector P1: while the first device is bound the slot-2 device cannot approve — 403 PAIRING_APPROVER_FORBIDDEN with the pairing staying PENDING, the first device's own approval still works, and the authorization precedes the state machine; after the first device is unbound, the surviving slot-2 device is still refused — the T18 administrator verification is the only lane) |
| **Implementation Result** | The second-device enroll / first-device approval / one-shot pairing land as revision 037 (drafted as 033 on the 032 head, renumbered when PR #48's 033–036 chain merged to main first; Alembic order comes from down_revision, never the file name) plus the application layer: the pairing row binds the keyed digest of the candidate fingerprint (the raw value never reaches the database; the partial unique index on (code, digest) WHERE active keeps at most one active row); the four-state machine PENDING/APPROVED/CONSUMED/EXPIRED is shape-coupled by a CHECK whose EXPIRED arm requires the approval columns to arrive as a pair (PR #49 Codex P2); the single enroll route is state-driven two-phase (PENDING → 202 with no envelope so the key never burns; APPROVED → the consumption branch seals the 201 one-time credential with the T14 AEAD engine, operation device_enroll, scope = the candidate digest); the consumption runs under the code-row lock with the code's BOUND device rows locked after it (lock order code → devices → pairing, PR #49 Codex P2) and next_free_slot picking the empty slot — both slots full answers 409 with the pairing row kept APPROVED (an unbind inside the expiry window still consumes); concurrent slot-2 rivals produce exactly one winner (code-row serialization + the _PairingRaceLost loser re-reading the winner's row, now mirroring the winner's real status — PR #49 Codex P3); approve authenticates the first device via Bearer and re-locks the approver row to re-validate BOUND inside the transaction before touching the pairing row (the TOCTOU fix — a concurrent unbind winner yields the same 401 DEVICE_REVOKED a fresh request would get, PR #49 Codex P1), then validates the approver against activation_code_activations.first_device_id *before* the state machine (the approval lane belongs to the first currently-bound device; once it is unavailable the lane moves to the T18 administrator verification, never down to the surviving slot 2 — PR #49 GitHub connector P1, 403 PAIRING_APPROVER_FORBIDDEN for a non-first device, including re-approval of an APPROVED pairing), the state machine itself as the idempotency (no envelope by design); approval is not transferable (digest binding; a different fingerprint gets its own PENDING); the second device never re-charges (no wallet columns on the pairing table + full-flow count locks); key rotation between 202 and 201 keeps the stored (digest, version) pair truthful while the fresh token stays keyed with the current highest version (PR #49 Codex P2); lapsed APPROVED rows flip EXPIRED on both the lookup and approve paths, freeing the active index (PR #49 Codex P2); the enroll shares the activation lane's anti-enumeration surface |
| **Verification Command and Pass Count** | pytest tests/test_customer_devices.py → 52 passed (22 T16 + 30 T17); full suite → 870 passed zero regression (T16 baseline 840 + 30 new; the first full run surfaced the 033-FK TRUNCATE cascade: test_admin_activation_routes.py's fixture truncate had to list device_pairing_requests — 38 fixture errors → 38 passed after the fix, 862 pre-review); ruff/format/mypy all green (149 files formatted, 61 source files typed); head-assertion sweep 032→033 re-verified: 135 + 69 passed across the ten affected files; PR #49 Codex review (REQUEST CHANGES: 1 P1 + 6 P2 + 1 P3) — every finding substantively fixed with a regression lock, re-verified: special 50 + full 868 + static green; PR #49 GitHub connector review (P1: the approval lane was not restricted to the first device) — fixed with 2 regression locks, re-verified: special 52 + full 870 + static green; post-merge re-verification (origin/main merged, PR #48's 033–036 chain landed first, migration renumbered 033→037, revision 034's canonical probe key adopted): full suite 910 passed zero regression + ruff/format/mypy green (155/61) + npm run check frontend 324 tests/biome green + cargo fmt/check green |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | the candidate fingerprint reaches the database only as a keyed HMAC-SHA256 digest (cross-version probing for the rotation window); pairing/approval/consumption logs carry identifiers only; missing and cross-code pairings answer one 404 (no IDOR oracle); the enroll shares the activation lane's anti-enumeration surface (unified 400 + audited failure events + alert threshold + constant delay; same activate:ip / activate:code budgets; a blocked IP mints no code-dimension row; malformed codes do not short-circuit the limiter); the one-time credential is a secrets.token_urlsafe(32) value stored only as a keyed digest and sealed with the T14 AEAD envelope (AAD binds operation/scope/key_digest); key misconfiguration answers 503 fail-closed; the pairing TTL and binding timestamps share the in-transaction PostgreSQL clock (SES-01) |
| **Migration and Rollback** | new PG-only revision 037_device_pairing_requests (four-state shape-coupled status, partial unique active index on (activation_code_id, candidate_fingerprint_hmac), downgrade refuses once pairing rows exist — approval-lineage audit evidence, the 027/028/032 guard precedent; SQLite early-returns, the 025–036 precedent; renumbered from 033 after the PR #48 origin/main merge, revising 036_low_review_constraint_guards; the consumption INSERT and the enroll bound-probe adopt revision 034's fingerprint_canonical cross-version probe key — the activation-route M2 precedent); rollback = downgrade 037 + code revert |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | the admin-verification approval lane (T18 writes through the same state machine); login-dimension rate limiting (T19); client OpenAPI regeneration (T28 frontend gate); thread-level approve-vs-unbind concurrent proof (the FOR UPDATE re-validation semantics cover it); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T17 Section 14 Ledger Record

```text
任务/工作包：T17 / DEV-02
Owner / Reviewer：后端（Agent 执行）/ Codex 独立评审（1 P1+6 P2+1 P3 逐条实质修复）+ GitHub connector 评审（P1 批准权未限定首设备，已修复）
分支 / 基线 SHA：feat/customer-v3-t17-second-device-pairing / 基线 e30ea64（main，PR #48 合入后 T17 分支 merge origin/main：PR #48 的 033–036 链先落地，T17 迁移重编号 033→037，并采纳 034 canonical 探测键）
上游规格段落：客户版任务清单 V3 §4 T17、§12.3 DEV-02；代码开发清单 V3 §3.2 customer_device_service.py/customer_device_routes.py、§3.3 test_customer_devices.py 冻结名、迁移主题 037（原 033，PR #48 落地 033–036 链后重编号）；激活码开发文档 §12.2 六步契约、§6.1 API 表、§13.2 错误码；测试与验收规格 §6
改动文件：server/migrations/versions/037_device_pairing_requests.py（新增：四态状态机+形状耦合+partial unique active 索引+downgrade 守卫，PG-only，原 033 重编号）、server/app/customer_device_service.py（T17 小节：fingerprint_digests_for/lookup_active_pairing/create_pairing_request/approve_pairing_request/consume_pairing_request 含 fingerprint_canonical）、server/app/customer_device_routes.py（enroll 两阶段 202/201 路由+approve 路由，enroll 探测含 canonical、UniqueViolation 双约束名映射）、server/tests/test_customer_devices.py（+30 用例）、10 个测试文件 22 处 head 断言 036→037（合并 main 后重扫）、server/tests/test_admin_activation_routes.py（TRUNCATE 列表补 device_pairing_requests——037 FK 引用连锁，保持在 036 replica-role 守卫下）、docs/evidence/T17-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 30 例——enroll 契约 7（幂等键必需/PENDING 创建绑定候选摘要/同键重试同 pairing_id 零信封/未知码统一 400/未激活码统一 400/已绑指纹 409/两槽满 409）+完整六步流 3（slot2 绑定+无充值计数锁定/丢 201 密封凭据重放/同键异参 409）+并发与过期 5（双线程 barrier slot2 单赢家 1×201+1×409+单 CONSUMED+双 BOUND/过期 PENDING 翻转新建/APPROVED 过期重启留审计/消费时槽满保持 APPROVED）+approve 契约 7（Bearer 必需/批准人 lineage/缺失跨码统一 404 IDOR/重复幂等/已消费 409/过期 409/self-approval 409）+不可转用 1（异指纹得自身 PENDING，批准行不动，无新设备行）+Codex 评审回归锁定 6（中途解绑 stale approver→revoked/202-201 间轮换存储真实 (digest,version) 对/过期 APPROVED 经 approve 翻转释放占用/形状 CHECK 拒半写批准列/partial unique 活跃阻塞+终态复用/037 downgrade 非空守卫+空库对称降级）+GitHub connector 评审锁定 2（首设备存活时 slot-2 不能批准 403+授权先于状态机，首设备解绑后 slot-2 仍被拒——T18 管理员核验是唯一通道）
实现结果：第二设备 enroll/第一设备批准/一次性配对落地（迁移 037+应用层）：配对行绑定候选指纹 keyed digest（明文永不过库，partial unique (code,digest) WHERE active 保单活跃行）；四态状态机 PENDING/APPROVED/CONSUMED/EXPIRED 形状耦合 CHECK；enroll 单路由状态驱动两阶段（PENDING→202 无信封不烧键，APPROVED→消费分支密封 201 一次性凭据）；消费持码行锁+next_free_slot 选空槽，两槽满 409 配对行保持 APPROVED（解绑后过期窗内仍可消费）；并发 slot2 恰一成功（码行锁串行化+_PairingRaceLost 输家重读赢家行）；approve Bearer 第一设备鉴权：事务内重锁 approver 验证 BOUND（TOCTOU，PR #49 Codex P1）+对照 activation_code_activations.first_device_id 验证首设备且先于状态机（GitHub connector P1：批准权限定首设备，首设备不可用时走 T18 管理员核验而非降级到 slot-2，非首设备含已 APPROVED 重批准一律 403 PAIRING_APPROVER_FORBIDDEN）+状态机即幂等（无 secret 无信封）；批准不可转用（绑定摘要+异指纹新 PENDING）；第二设备零充值（配对表无钱列+全流计数锁定）；防枚举复用 T15 维度预算（activate:ip/code 同池，malformed 不短路统一拒绝+审计+常数时延，IP blocked 不消费 code 维度）；只读回放预检免限流预算
验证命令与通过数：专项 52 passed（T16 22+T17 30）；全量 870 passed 零回归（T16 基线 840+新增 30；首轮暴露 033 FK 连锁：test_admin_activation_routes.py TRUNCATE 补 device_pairing_requests 后 38 errors→38 passed，评审前 862）；ruff/format/mypy 全绿（149 files formatted，61 source files typed）；head 断言迁移专项 135+69 复验通过；Codex 独立评审（REQUEST CHANGES：1 P1+6 P2+1 P3）逐条实质修复+回归锁定后专项 50/全量 868 复验；GitHub connector 评审 P1（批准权未限定首设备）修复+2 例回归锁定后专项 52/全量 870 复验；合并 main（PR #48 落地 033–036 链，迁移重编号 033→037+采纳 034 canonical）后全量复验 910 passed 零回归+ruff/format/mypy 全绿（155/61）+npm check 前端 324 tests/biome 绿+cargo fmt/check 绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：候选指纹只以 keyed HMAC-SHA256 摘要过库（跨版本探测）；配对/批准/消费事件日志仅含标识符；缺失=跨码统一 404（无 IDOR 预言）；enroll 与 activate 共享防枚举面（统一 400+失败审计+告警阈值+常数 PBKDF2 时延）；一次性凭据 secrets.token_urlsafe(32)+keyed digest 存储，AEAD 信封密封（AAD 绑定 operation/scope/key_digest）；密钥配置故障 503 fail-closed；配对过期与绑定时间戳共用 PG 事务内时钟（SES-01）
迁移与回滚：037 PG-only（SQLite early return，025-036 先例）；downgrade 非空守卫（配对行是批准 lineage 审计证据，027/028/032 先例）空库对称降级；回滚=降级 037+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：管理员核验批准通道（T18 经同一状态机写穿）；login/租约维度限流（T19）；客户端 OpenAPI 重新生成（T28 前端门禁）；approve-vs-unbind 双线程并发证明（FOR UPDATE 重新验证语义覆盖）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T18 — Administrator Verified Approval, Unbind & Credential Revocation (DEV-03)

| Field | Value |
| --- | --- |
| **Task ID** | T18 / DEV-03 |
| **Owner / Reviewer** | Backend/Management (Agent) / independent code-review subagent (APPROVE: 0 P1 / 0 P2 / 2 P3, both substantively fixed — the deferred-409 same-key replay regression lock and the three-valued admin_lane label — with the re-verified 64-test run) + GitHub connector review on PR #50 (P2: the verification view must include the delivery records — fixed with a regression test, 65 re-verified) |
| **Branch / Base SHA** | feat/customer-v3-t18-admin-verified-unbind-revoke / base ed65a03 (main, PR #49 squash) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T18-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §4 T18, §12.3 DEV-03; code checklist §3.2 (frozen admin_device_routes.py), §3.3 (frozen test_customer_devices.py), migration theme 038; dev doc §6.1/§6.2 management API tables, §9.2 device revocation atomic session invalidation, §12.2 step 3 admin verification lane (first device unavailable), §13.2 error codes, §15 real operator identity; acceptance spec §2 |
| **Failure Test or Regression Lock** | 13 cases: verification view 1 (the §12.2 step-3 evidence bundle: pairing + masked code + the delivery records (channel / external order / recipient / delivered-by) + activation fact + first-device status + admin_lane=CLOSED_FIRST_DEVICE_BOUND while the first device stays bound; unknown pairing → 404 — the PR #50 connector P2 regression lock) + gate & write contract 4 (no admin cookie → 401; auditor role → 403 AUDITOR_READ_ONLY even on a valid session; missing Idempotency-Key → 400; confirm=false or blank reason → 400 — zero audit rows, zero state change) + verified approval 3 (while the first device stays BOUND → 403 PAIRING_FIRST_DEVICE_AVAILABLE, the pairing stays PENDING with zero audit rows — the admin must not shortcut a live first device; after the first device is unbound → 200, the pairing APPROVED with approved_by_admin_user_id + exactly one PAIRING_ADMIN_APPROVED audit row carrying the real admin actor / target user / reason / request-id, and the candidate can then consume it — the full §12.2 step-3 recovery; repeated approval → 200 idempotent, no second audit row) + admin unbind 2 (releases the device UNBOUND + unbound_at with the slot freed and revokes the riding session with the administrator as the LOGOUT actor (epoch +1, lease pulled into the past) + one DEVICE_ADMIN_UNBOUND audit row; a second unbind → 409 DEVICE_ALREADY_RELEASED, unknown device → 404) + credential revocation 2 (writes the terminal REVOKED + revoked_at with unbound_at staying NULL (the 028 shape) + the riding-session revocation + one DEVICE_CREDENTIAL_REVOKED audit row; the released credential then answers 401 DEVICE_REVOKED on the customer lane — the client-side wipe signal) + idempotency 1 (the same key replays the unbind response X-Idempotent-Replay: true with one audit row and no double state change; the deferred-409 same-key replay answers the identical 409; the same key + a different body → 409 IDEMPOTENCY_CONFLICT) + migration invariants 2 (ck_admin_device_events_target_shape rejects an unbind event without a device and an approval event without a pairing; the 038 downgrade refuses once an audit row exists — the version stays at head through the single-transaction chain — and downgrades symmetrically once emptied, both tables truncated together behind the replica role because the 038 FK pairs them) |
| **Implementation Result** | The administrator verified approval / unbind / credential revocation lands as revision 038_admin_device_operations (the admin lineage column approved_by_admin_user_id on the frozen 037 pairing table, mutually exclusive with the device lane by the regenerated _APPROVAL_LINEAGE CHECK: approved_at non-null proves exactly one approver) plus the append-only admin_device_events audit table (event/target shape CHECK, 029 UPDATE/DELETE trigger + 036 shared TRUNCATE guard, downgrade refuses once audit rows or admin-approval lineage exist) and the application layer: the three write routes run behind the T09 admin session/CSRF/RBAC gate (AdminWriter role=admin, auditor 403) and the T12 shared admin write contract (Idempotency-Key + confirm=true + non-blank reason, contract violations 400 before any transaction opens) with the device lane's own 503 fail-closed code DEVICE_SERVICE_UNAVAILABLE (the newly parameterized unavailable_code — §13.2 keeps one code per domain); the real actor lands in the audit row from the authenticated AdminActor, never from the request body (dev doc §15); the approval lane locks the first_device_id row FOR UPDATE first (still BOUND → 403, a missing row counts as unavailable — the recovery lane) then re-locks the pairing row and replays the T17 state machine (not_found / already_consumed / expired with the lazy flip / already_approved → PENDING becomes APPROVED with approved_at + approved_by_admin_user_id + the PAIRING_ADMIN_APPROVED audit row; lock order devices → pairing, the tail of the enroll route's code → devices → pairing order); the admin unbind and the credential revocation reuse the T16 _revoke_session_riding_device core (epoch bump, past lease, LOGOUT event — extracted from the T16 unbind tail, now parameterized by the acting user so the administrator is the actor) with the revocation lane writing the terminal REVOKED state (the 028 shape); the expired 409 goes through the new DeferredHTTPWriteError — the idempotency layer snapshots the error response, commits the lazy PENDING/APPROVED → EXPIRED flip and re-raises after the commit (the replay answers the same 409), while side-effect-free branches (404/403/already-consumed/not-bound) keep the plain raise so the key stays free for a retry (the T12 precedent); reads are AdminReader (auditor-accessible): the device list (filters + bounded pagination, display metadata only) and the pairing verification view (code masked, activation fact, first-device status, admin_lane OPEN / CLOSED_FIRST_DEVICE_BOUND / CLOSED_NO_ACTIVATION) — the write path re-validates under lock |
| **Verification Command and Pass Count** | pytest tests/test_customer_devices.py → 65 passed (22 T16 + 30 T17 + 13 T18, incl. the review-added deferred-409 same-key replay lock and the verification-view evidence-bundle lock); full suite → 923 passed zero regression (T17 re-verified baseline 910 + 13 new; the first full run surfaced the 038 FK/TRUNCATE chain — test_admin_activation_routes.py's fixture truncate had to list admin_device_events — and the head-assertion sweep 037→038: 22 sites across ten files incl. the "Twelve steps" chain comments; the sqlite→PG import/reconcile suite then needed admin_device_events registered in PG_ONLY_TABLES — 5 failures → 35 passed after the fix); ruff/format/mypy all green (157 files formatted, 62 source files typed); npm run check full-repo gate green (secret scan + client biome/vitest/tsc + e2e + cargo fmt/check + server static + the full suite as its final step) |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | admin writes are admin-role-only (cookie + CSRF + write-method checks); actor/reason/request-id flow into the audit rows and logs end-to-end; the audit table is append-only (UPDATE/DELETE trigger refuses, TRUNCATE guard refuses); fingerprints and token digests never leave the store (the list and verification views carry display metadata and states only); the idempotency snapshot layer replays or 409-conflicts by request hash; key-configuration failures answer 503 fail-closed; the unified 404 gives no cross-user enumeration oracle; the revoked credential answers 401 DEVICE_REVOKED — the client-side wipe signal |
| **Migration and Rollback** | new PG-only revision 038_admin_device_operations (approved_by_admin_user_id FK users + admin_device_events with the event/target shape CHECK; downgrade refuses once any audit row exists or any pairing carries an admin approval — the operator lineage must survive any rollback; SQLite early-returns, the 025–037 precedent); rollback = downgrade 038 + code revert |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | a dedicated admin-lane rate limit (the T37/OPS-02 hardening pass); the T33 management UI consuming these APIs (frontend task); real ops-ticket integration (a human process); thread-level admin-vs-customer concurrent approval proof (the FOR UPDATE serialization covers it); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T18 Section 14 Ledger Record

```text
任务/工作包：T18 / DEV-03
Owner / Reviewer：后端/管理（Agent 执行）/ 独立评审子代理（APPROVE：0 P1/0 P2/2 P3，均实质修复后专项 64 复验通过）+ GitHub connector 评审（PR #50 P2：核验视图须含发放记录，已修复+回归锁定，专项 65 复验通过）
分支 / 基线 SHA：feat/customer-v3-t18-admin-verified-unbind-revoke / 基线 ed65a03（main，PR #49 squash）
上游规格段落：客户版任务清单 V3 §4 T18、§12.3 DEV-03；代码开发清单 V3 §3.2 admin_device_routes.py 冻结名、§3.3 test_customer_devices.py 冻结名、迁移主题 038；激活码开发文档 §6.1/§6.2 管理端 API 表、§9.2 设备撤销原子吊销、§12.2 step 3 首设备不可用的管理员核验通道、§13.2 错误码、§15 管理端真实操作人；测试与验收规格 §2
改动文件：server/migrations/versions/038_admin_device_operations.py（新增：approved_by_admin_user_id+APPROVAL_LINEAGE 状态形状重生成+admin_device_events 审计表+事件/目标形状 CHECK+append-only+TRUNCATE guard+downgrade 守卫，PG-only）、server/app/customer_device_service.py（T18 小节：_revoke_session_riding_device 提取共享+_insert_admin_device_event+admin_approve_pairing_request/admin_unbind_device/revoke_device_credential）、server/app/admin_device_routes.py（新增冻结名：设备列表+配对核验视图+approve/unbind/revoke-credential 三写路由）、server/app/admin_activation_routes.py（DeferredHTTPWriteError+_write_with_idempotency 支持 deferred 409 提交后重抛+unavailable_code 参数化）、server/app/main.py（挂载+2 行）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 注册 admin_device_events——T07 导入源无 SQLite 对应表，空表预期/非空仍 fail-closed）、server/tests/test_customer_devices.py（+13 用例，admin 会话 helper、route_state TRUNCATE 补 admin_device_events、_insert_pairing_row 补 approved_by_admin_user_id、037 downgrade 锁定测试适配 038 head）、server/tests/test_admin_activation_routes.py（fixture TRUNCATE 补 admin_device_events——038 FK 引用连锁）、10 个测试文件 22 处 head 断言 037→038（含 3 处链注释 Twelve steps）、docs/evidence/T18-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 13 例——核验视图 1（§12.2 step-3 证据包：配对+掩码码+发放记录（channel/external_order/recipient/delivered-by）+激活事实+首设备状态+首设备存活时 admin_lane=CLOSED_FIRST_DEVICE_BOUND；未知配对 404（PR #50 connector P2 回归锁定））+门禁与写契约 4（无 cookie 401/auditor 403 只读/缺幂等键 400/confirm=false 或空 reason 400 且零审计行零状态变化）+核验批准 3（首设备存活 403 配对保持 PENDING 零审计行/首设备解绑后 200 approved_by_admin_user_id+PAIRING_ADMIN_APPROVED 审计行（actor/target/reason/request-id）+候选随后可消费/重复批准 200 幂等无第二审计行）+管理解绑 2（UNBOUND+unbound_at+骑乘会话吊销 actor=管理员 epoch+1 lease 过去+DEVICE_ADMIN_UNBOUND 审计行/二次解绑 409+未知设备 404）+凭据撤销 2（REVOKED+revoked_at 且 unbound_at 保持 NULL（028 形状）+骑乘会话吊销+DEVICE_CREDENTIAL_REVOKED 审计行/撤销后凭据在客户道 401 DEVICE_REVOKED）+幂等 1（同键重放 X-Idempotent-Replay+单审计行+无双重状态变化+deferred 409 同键重放同 409/同键异参 409）+迁移不变量 2（ck_admin_device_events_target_shape 拒无 device 的 unbound 事件/038 downgrade 有审计行拒绝版本保持 head+清空后对称降级）
实现结果：管理员核验批准/解绑/凭据撤销落地（迁移 038+应用层）：三写路由全部走 T09 admin 会话/CSRF/RBAC 门（AdminWriter role=admin，auditor 403）+T12 共享写契约（幂等键+confirm+reason，契约违规在事务开启前 400）+幂等快照层（设备域自有 503 DEVICE_SERVICE_UNAVAILABLE）；真实 actor 从认证态 AdminActor 落审计行（永不取自请求体）；每成功变更恰一条 append-only admin_device_events（029 UPDATE/DELETE 触发器+036 共享 TRUNCATE guard）；批准通道先锁 first_device_id 行 FOR UPDATE（BOUND→403 不短路存活首设备，缺失→恢复通道）再锁配对行复用 T17 状态机（approved_by_admin_user_id lineage 与设备批准互斥）；解绑/撤销复用 T16 会话吊销核心（actor 参数化=管理员）；过期 409 经 DeferredHTTPWriteError 提交后重抛（lazy 翻转保留+同键重放同 409，无副作用分支普通 raise 回滚键保持可重试）
验证命令与通过数：专项 65 passed（T16 22+T17 30+T18 13，含评审修复后新增 deferred 409 同键重放锁定+核验视图证据包锁定）；全量 923 passed 零回归（T17 重验基线 910+新增 13；首轮暴露 038 FK/TRUNCATE 连锁——test_admin_activation_routes.py 补 admin_device_events+head 断言扫 22 处（含 Twelve steps 链注释）+sqlite→PG 对账套件 PG_ONLY_TABLES 注册 admin_device_events 后 5 failed→35 passed）；ruff/format/mypy 全绿（157 files formatted，62 source files typed）；npm check 全仓门禁绿（secret 扫描+前端 biome/vitest/tsc+e2e+cargo fmt/check+服务端静态+全量为末步）；独立评审子代理 APPROVE（0 P1/0 P2/2 P3：deferred 409 同键重放回归锁定+admin_lane 三分支标签 CLOSED_NO_ACTIVATION，均已修复复验）；GitHub connector 评审 P2（核验视图含发放记录 activation_code_deliveries——channel/external_order_ref/recipient_ref/delivered_by_user_id/delivered_at 时间序，§12.2 step-3 发放证据完整，表内无明文码）已修复+回归锁定复验
证据层级：AUTOMATED_VERIFIED
安全与可观测性：管理写仅 admin 角色（cookie+CSRF+写方法校验）；actor/reason/request-id 全链路入审计与日志；审计表 append-only（UPDATE/DELETE 触发器拒绝+TRUNCATE guard）；指纹与 token 摘要永不出库（列表/核验视图仅显示元数据与状态）；幂等快照层按请求哈希重放或 409；密钥配置故障 503 fail-closed；统一 404 无跨用户枚举预言；撤销后凭据 401 DEVICE_REVOKED（客户端擦除信号）
迁移与回滚：038 PG-only（SQLite early return，025-037 先例）；downgrade 有审计行或管理批准 lineage 时拒绝（操作人审计必须存活），空库对称降级；回滚=降级 038+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：管理道独立限流（T37/OPS-02 安全硬化统一收口）；T33 管理端页面消费这些 API（前端任务）；真实运维工单系统联动（人工流程）；管理-vs-客户双线程并发批准证明（FOR UPDATE 串行化覆盖）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T19 — Session Login, Heartbeat, Logout & the 30/90-Second Database Lease (SES-01)

| Field | Value |
| --- | --- |
| **Task ID** | T19 / SES-01 |
| **Owner / Reviewer** | Backend (Agent) / Qoder CodeReview subagent + security self-review (conclusions in the Section 14 record and the commit message) + GitHub connector review on PR #51 (P2: the envelope recovery-window verdict must use the in-transaction PostgreSQL clock — fixed with a regression lock, 35 re-verified) |
| **Branch / Base SHA** | feat/customer-v3-t19-session-lease / base ed65a03 (main after PR #49) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T19-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §4 T19, §12.3 SES-01; code checklist §3.2 (frozen customer_session_service.py / customer_session_routes.py), §3.3 (frozen test_customer_sessions.py); dev doc §12.3 login state machine, §6.1 API table, §6.3 idempotency, §13.2 error codes; acceptance spec §2.3 / §3.4 |
| **Failure Test or Regression Lock** | 35 cases (29 fail-first + 6 review regression locks): module units 3 (the frozen 90-second lease constant; device-name masking incl. empty/short names) + login contract gates 5 (missing Bearer → 401 DEVICE_CREDENTIAL_REQUIRED; unknown credential → 401 DEVICE_CREDENTIAL_INVALID; released credential → 401 DEVICE_REVOKED; missing Idempotency-Key → 400; login:ip budget 2 → the third attempt 429 RATE_LIMITED with Retry-After) + §12.3 state machine 5 (same device + valid session token → 200 renewal with identical token/epoch/session_id and the lease pushed out; same device without a usable token → 201 recovery, epoch 2, fresh token, the stale token's heartbeat 401 SESSION_REPLACED; another device online → 409 OTHER_DEVICE_ONLINE with the masked hint and zero database changes — the full names "Office"/"MacBook" never appear in the response and no LOGIN event is recorded; lapsed lease → 201 takeover, epoch 2, the system TIMEOUT event with actor_user_id NULL, the stale token 401 SESSION_REPLACED; missing session row → defensive epoch-1 establish) + idempotency envelopes 3 (a lost 201 replays with the same token/epoch and X-Idempotent-Replay with zero second LOGIN events; same key + different body → 409 IDEMPOTENCY_CONFLICT; a business 409 rolls back with the transaction — the same key succeeds after the lease actually lapses) + heartbeat 5 (missing Bearer → 401 SESSION_TOKEN_REQUIRED; renewal keeps epoch and session_id aligned with the database row; forged token → 401 SESSION_REPLACED; lapsed lease → 401 SESSION_EXPIRED with no HEARTBEAT event and no resurrection; post-logout heartbeat → 401 SESSION_EXPIRED) + logout 6 (missing Idempotency-Key → 400; the lease lands in the past with the LOGOUT event on the audit trail; the other device logs in immediately (201); a lost 204 replays via the sealed envelope with no second LOGOUT event; a late logout after takeover → 401 SESSION_REPLACED leaving the new session's device and lease byte-identical; a lapsed lease → 401 SESSION_EXPIRED) + concurrency 1 (two threads race from a lapsed state behind the same fixture: exactly one 201 + one 409, epoch advanced exactly once to 2, one current device, the live lease in the future) + credential red line 1 (the append-only event table never contains the plaintext session or device tokens) + review regression locks 6 (login:ip env non-positive/garbage values fall back to the safe default 10 while a valid 7 applies; a fully validated idempotent replay spends zero rate-limit budget — budget exhausted → fresh key 429 → same-key retry still replays the cached 201; heartbeat without device-domain keys answers 503 SESSION_SERVICE_UNAVAILABLE; user-driven LOGIN/HEARTBEAT/LOGOUT events record the acting user while only the system TIMEOUT stays actor-less; two concurrent first-writers on a missing row both answer 201 with exactly one LOGIN event each — the loser re-drives the winner's committed row, never a 500; an API node whose process clock runs a decade ahead of PostgreSQL still replays a still-valid lost-response 201 — the PR #51 connector P2 lock (the recovery-window verdict uses the in-transaction SELECT now(), never the process clock)) |
| **Implementation Result** | The §12.3 single-online session lands on revision 029's structures (no new migration): login locks the user's single customer_session_state row FOR UPDATE **scoped to the authenticated user_id** and decides renewal (same token/epoch, 200) / recovery (epoch + 1, fresh token) / takeover (epoch + 1 + the system TIMEOUT event bound to the timed-out session's binding columns) / conflict (409 with the masked device hint + remaining lease, zero writes — never a silent kick); heartbeat locates the row by the presented token's digests probed across every configured key version (the PR #44 rotation-window rule) and renews the lease with epoch untouched, answering one unified 401 SESSION_REPLACED for forged and replaced tokens (no oracle) and a terminal 401 SESSION_EXPIRED under a lapsed lease (never resurrected); logout reuses the T16 unbind pattern — GREATEST(now, created_at + 1µs) with the full microsecond precision of the transaction clock (the PR #47 P2 lesson) plus a reason-tagged LOGOUT event, releasing the slot immediately; login/logout carry mandatory Idempotency-Keys with the replay probe **before** authentication (the unbind precedent), the envelope scope probed across key versions (the enroll precedent), business failures rolling back so the key stays reusable, and the sealed login payload carrying the state-machine outcome so replays reproduce the original 200/201; login draws the T15 shared login:ip budget (env VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP, default 10) answering 429 + Retry-After; stable error codes per §13.2 plus the T16 REQUIRED/INVALID precedent; PG/AEAD/device-key misconfiguration answers 503 fail-closed (never a misleading 401 that would wipe the client's stored credentials); the independent CodeReview audit (REQUEST_CHANGES: 1 P1 + 2 P2 + 5 P3, all substantively fixed) added — all three routes sample SELECT now() inside the business transaction and pass it down as now= (the SES-01 clock discipline, the unbind/activation precedents; the P1), the fully validated replay probe precedes the login:ip limiter (the activate T15-review rule), the defensive no-row first-write races INSERT ... ON CONFLICT (user_id) DO NOTHING with the loser re-reading the winner's committed row and re-driving the state machine, and user-driven LOGIN/HEARTBEAT/LOGOUT events carry actor_user_id (only the system TIMEOUT stays actor-less); the PR #51 connector P2 extended the same clock discipline to the envelope recovery-window verdict — _find_envelope samples the PostgreSQL now() in the envelope-read transaction so a skewed API node neither rejects a still-valid replay nor accepts an expired one |
| **Verification Command and Pass Count** | pytest tests/test_customer_sessions.py → 35 passed (29 fail-first + 6 review regression locks); full suite → 958 passed zero regression (T18 merged base 923 + 35 new; the 2 warnings are the pre-existing httpx deprecation and a Windows GBK subprocess-reader artifact); ruff check → all green; ruff format --check → 160 files already formatted; mypy app → success, 64 source files; the independent CodeReview audit returned REQUEST_CHANGES (1 P1 + 2 P2 + 5 P3) — every finding substantively fixed with a regression lock where testable (R1-R8 tabulated in docs/evidence/T19-EVIDENCE.md), re-verified by the special 35 + full 958 runs; the PR #51 connector P2 (envelope recovery-window clock) fixed with a regression lock and re-verified on the same runs; the in-development defects caught by the real PG run (missing user_id row-lock scope, whole-second heartbeat CHECK violation, the text-vs-timestamptz GREATEST, the TIMEOUT event's binding columns) are each documented there as well |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | session tokens reach the database only as keyed digests (cross-version probing); the 409 conflict hint masks the device name (first two characters + "**"; empty stays empty); user-driven LOGIN/HEARTBEAT/LOGOUT events record the acting user (only the system TIMEOUT carries no actor); the event table is append-only (029 triggers refuse UPDATE/DELETE) with a test locking that no plaintext credential ever appears; the envelope stores the AEAD ciphertext (AAD binds operation/scope/key_digest) and the request hash covers the session token's sha256 — never the raw secret; the lease verdict and the envelope recovery window share the in-transaction PostgreSQL clock (the client's local time is never a truth source; the PR #51 connector P2 lock proves a decade-ahead process clock still replays); forged and replaced tokens get one indistinguishable 401; key misconfiguration answers 503 fail-closed per the §13.2 client contract |
| **Migration and Rollback** | No new migration — revision 029 (T13) already provides customer_session_state (epoch-monotonic trigger), customer_session_events (append-only) and customer_idempotency_envelopes; rollback = code revert (session rows may remain; lapsed leases release naturally) |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | explicit atomic switch and session-epoch fencing (T20 SES-02/SES-03); in-transaction fencing on the business write routes (T21 SES-04/SES-05); the client-side 30-second heartbeat contract and OpenAPI regeneration (T28 frontend gate); process-level proof of the multi-API-instance same-lease race (the PG row-lock semantics cover it, the T13 100-concurrency precedent); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T19 Section 14 Ledger Record

```text
任务/工作包：T19 / SES-01
Owner / Reviewer：后端（Agent 执行）/ Qoder CodeReview 子代理 + 安全自评审（结论见 commit message）+ PR #51 chatgpt-codex-connector 二轮评审（1 P2：信封 recovery 窗口时钟，已修复含回归锁定）
分支 / 基线 SHA：feat/customer-v3-t19-session-lease / 基线 ed65a03（main，PR #49 合入后）
上游规格段落：客户版任务清单 V3 §4 T19、§12.3 SES-01；代码开发清单 V3 §3.2 customer_session_service.py/customer_session_routes.py、§3.3 test_customer_sessions.py 冻结名；激活码开发文档 §12.3 登录状态机、§6.1 API 表、§6.3 幂等、§13.2 错误码；测试与验收规格 §2.3/§3.4
改动文件：server/app/customer_session_service.py（新增：login/heartbeat/logout 状态机+租约常量+脱敏+事件写入）、server/app/customer_session_routes.py（新增：三路由+Bearer 设备/会话双层鉴权+幂等信封+login:ip 限流+稳定错误码）、server/app/main.py（挂载 customer_session_router）、server/app/security_rate_limit.py（login_ip_limit() 对齐 _positive_int_env 先例，评审 P3 修复）、server/tests/test_customer_sessions.py（新增 35 用例：29 fail-first+6 评审回归锁定）、docs/evidence/T19-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：35 例（29 fail-first+6 评审回归锁定）——模块单元 3（90 秒租约常量冻结/设备名脱敏 2 含空名与短名）+login 契约门 5（Bearer 必需/未知凭据/已释放凭据 401 DEVICE_REVOKED/幂等键必需/login:ip 限流 429+Retry-After）+§12.3 状态机 5（同设备有效 token 续租 200 同 token 同 epoch 同 session_id/同设备无 token 恢复 epoch+1 旧 token 401 SESSION_REPLACED/异设备在线 409 脱敏提示零库变更且全名不出现零 LOGIN 事件/租约过期接管 TIMEOUT actor NULL 旧 token 401/无行防御 epoch-1）+幂等信封 3（丢响应重放同 token 同 epoch 零第二 LOGIN/同键异参 409/业务 409 不烧键过期后同键成功）+heartbeat 5（Bearer 必需/续租 epoch 不变 session_id 对齐 DB 行/伪造 token 401 SESSION_REPLACED/过期租约 401 SESSION_EXPIRED 不复活无事件/logout 后 401）+logout 6（幂等键必需/租约置过去+LOGOUT 事件/对方设备立即可登录/丢响应重放 204+REPLAY 头无第二 LOGOUT/接管后迟到 logout 401 不触碰新会话 lease 不变/过期租约 401）+并发 1（双线程 lapsed 起单赢家 201+409 epoch 恰 2 单当前设备 lease 未来）+红线 1（事件表无明文凭据）+评审回归锁定 5（login:ip env 非正值/乱码回落安全默认 10 而合法 7 生效/完全校验幂等重放零限流预算——预算耗尽→新键 429→同键重放仍还原 201/缺设备域密钥 heartbeat 503 SESSION_SERVICE_UNAVAILABLE/用户驱动事件记录 actor_user_id 仅 TIMEOUT 无 actor/缺行并发双首写均 201 恰一条 LOGIN——输家重读赢家行重驱状态机永不 500/信封 recovery 窗口判定用事务内 PG 时钟——进程时钟偏移十年仍可重放密封 201，PR #51 connector P2）
实现结果：§12.3 单在线会话落地（029 结构，无新迁移）：login 按认证 user_id 加 FOR UPDATE 行锁（修复了无 user_id 过滤锁错行的缺陷）后判定续租（同 token 同 epoch 200）/恢复（epoch+1 新 token）/接管（epoch+1+系统 TIMEOUT 事件绑定旧会话列）/冲突（409 脱敏设备名+剩余租约零写入，永不静默踢人）；heartbeat 跨密钥版本 digest 探测定位行锁，epoch 不变续租，伪造与被替换 token 统一 401 SESSION_REPLACED（无预言机），lapsed 401 SESSION_EXPIRED 不复活；logout 复用 T16 unbind 模式 GREATEST(now, created_at+1µs) 全微秒精度事务时钟（PR #47 P2 教训）+reason=user_logout LOGOUT 事件，槽位立即释放；login/logout 必带幂等键：鉴权前重放探测（unbind 先例）、scope 跨版本探测（enroll 先例）、业务 409 随事务回滚不烧键、密封 payload 携带 outcome 还原 200/201；login:ip 共享限流（T15 计数器，默认 10/窗口，429+Retry-After）；错误码对齐 §13.2+T16 先例；密钥/PG 故障 503 fail-closed（不误报 401 触发客户端擦凭据）；CodeReview 独立评审（REQUEST_CHANGES：1 P1+2 P2+5 P3）逐条实质修复：三路由业务事务内采样 SELECT now() 以 now= 传入（SES-01 时钟纪律，unbind/激活先例，P1）、完全校验的重放探测先于 login:ip 限流（activate T15 评审规则）、缺行防御首写 INSERT ON CONFLICT (user_id) DO NOTHING 输家重读赢家已提交行重驱状态机、用户驱动 LOGIN/HEARTBEAT/LOGOUT 事件记录 actor_user_id、login_ip_limit() 对齐 _positive_int_env 语义
验证命令与通过数：专项 35 passed；全量 958 passed 零回归（T18 合并后基线 923+新增 35；2 个既存 warning：httpx deprecation+Windows GBK subprocess reader）；ruff/format/mypy 全绿（160 files formatted，64 source files typed）；CodeReview 评审 REQUEST_CHANGES（1 P1+2 P2+5 P3）逐条实质修复含 5 例回归锁定+PR #51 connector P2（信封 recovery 窗口时钟）修复含 1 例回归锁定后专项 35+全量 958 复确认；开发中真实 PG 运行揭出的缺陷（行锁缺 user_id/heartbeat 整秒截断违反 CHECK/GREATEST 类型错位/TIMEOUT 事件绑定列错位）均修复并记入 T19-EVIDENCE.md
证据层级：AUTOMATED_VERIFIED
安全与可观测性：session token 只以 keyed digest 过库（跨版本探测）；409 脱敏（首两字符+**，空名保持空）；用户驱动 LOGIN/HEARTBEAT/LOGOUT 事件记录 actor_user_id，仅系统 TIMEOUT 无 actor；事件表 append-only（029 触发器）且测试锁定无明文凭据；信封 AEAD 密文（AAD 绑定 operation/scope/key_digest）+request_hash 用 sha256 替代明文 secret；租约判定与信封 recovery 窗口共用事务内 PG 时钟（客户端本地时间非真源；connector P2 回归锁定证明进程时钟偏移十年仍可重放）；伪造与被替换 token 统一 401 无预言机；密钥配置故障 503 fail-closed
迁移与回滚：无新迁移（029 已预留全部结构）；回滚=还原代码（会话行可保留，租约到期自然释放）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：显式原子 switch 与 epoch fencing（T20）；业务写路由事务内 fencing（T21）；客户端 30 秒心跳合同与 OpenAPI 重新生成（T28）；多 API 实例同租约竞态进程级证明（PG 行锁语义覆盖，T13 100 并发先例）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T20 — Explicit Atomic Switch & Session-Epoch Fencing (SES-02/SES-03)

| Field | Value |
| --- | --- |
| **Task ID** | T20 / SES-02 / SES-03 |
| **Owner / Reviewer** | Backend (Agent) / PR #52 chatgpt-codex-connector review (1 P1 + 2 P2, all substantively fixed with 4 regression locks: the code-status gate FOR UPDATE serialization, the clock_timestamp() post-lock lease verdict, and the expected_lease_until snapshot re-comparison) + security self-review |
| **Branch / Base SHA** | feat/customer-v3-t20-session-switch-fencing / base 939c305 (main, T19 PR #51 squash) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T20-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §4 T20, §12.3 SES-02/SES-03; code checklist §9.2 (frozen customer_auth.py), §3.3 (frozen test_customer_fencing.py); dev doc §12.3 fifth line (explicit switch), §12.4 (in-transaction fencing), §6.1 API table, §6.3 idempotency, §13.2 error codes; acceptance spec §2.3 / §3.4 |
| **Failure Test or Regression Lock** | 36 cases (19 in test_customer_sessions.py + 17 in test_customer_fencing.py): switch cases 13 (explicit atomic takeover of a live other-device lease — SWITCH event + epoch bump + fresh token committed together, the displaced token fenced everywhere; same-device renewal only; same-device recovery epoch+1; lapsed takeover with TIMEOUT event; missing-row establish; missing Bearer → 401; unknown credential → 401; missing Idempotency-Key → 400; lost-response replay returns the same token/epoch; switch draws the login:ip budget — spent budget → 429 Retry-After; two concurrent switches serialize to one winner; no wallet charge; suspended code → 403 CODE_SUSPENDED) + code-gate/revocation-propagation 6 (a suspended/revoked code never establishes a session; the verifier rejects a suspended code / a revoked code / a released device even with a live lease; the admin suspend/revoke paths propagate the session revocation in the same transaction) + fencing 13 (minimal six-field context; unknown token; replaced token after switch; lapsed lease; logged-out session; suspended code with a live lease; revoked code with a live lease; released device with a live lease; expected session_id / epoch / device / user re-comparison each fenced; never leaks the token or digest) + PR #52 review regression locks 4 (the matching expected lease snapshot still verifies; a changed expected lease → SESSION_REPLACED even with the epoch unchanged; a verifier that waits on the row lock past a 2 s lease answers SESSION_EXPIRED — the clock_timestamp() verdict; a concurrent write to the code row blocks on the gate's FOR UPDATE until establishment commits — the code-status serialization) |
| **Implementation Result** | The explicit atomic switch lands as `POST /api/customer/sessions/switch`: `switch_session` drives the §12.3 state machine with `takeover=True` — only the explicit confirmed client flow reaches the takeover branch, a plain login still answers 409 (the no-auto-kick red line), and the SWITCH event + epoch bump + fresh token commit in one transaction so the displaced token answers 401 SESSION_REPLACED on every API instance; the switch reuses the login idempotency machinery with its own `session_switch` envelope operation (distinct AAD) and draws the same login:ip budget (not bypassable by switching). The SES-03 propagation generalizes the T16 session-revocation core into `customer_session_service.revoke_session` (epoch bump + GREATEST lease pull + reason-tagged LOGOUT event; device-scoped for the T16/T18 unbind/revoke delegation, user-wide for the admin code suspend/revoke paths which now call it so a disabled account loses its session in the same transaction) plus the code-status gate (403 CODE_SUSPENDED/CODE_REVOKED on login and switch — a disabled account never establishes a session). The new `customer_auth.verify_session_context` is the in-transaction fencing verifier for the T21 write routes: it row-locks the live session, re-compares the request's expected user/device/session/epoch against the row (a switch in between fences the stale write), judges the lease on the transaction's PostgreSQL clock, and re-checks the activation-code and device status (defence in depth), answering stable SESSION_REPLACED / SESSION_EXPIRED; the returned `CustomerSessionContext` is exactly six fields and never carries a credential |
| **Verification Command and Pass Count** | pytest tests/test_customer_sessions.py tests/test_customer_fencing.py → 71 passed (54 session + 17 fencing incl. the 4 PR #52 locks); full suite → 994 passed zero regression (990 + 4 review locks); ruff check → all green; ruff format --check → 162 files already formatted; mypy app → success, 65 source files |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | login/switch share the code-status gate (a suspended/revoked account never establishes a session); the revocation paths propagate through the shared revoke_session core (device unbind/revoke and admin code suspend/revoke) with epoch bump + past lease + LOGOUT event naming the acting user — server-side revocation, never a client-side token wipe; the fencing verifier serializes switch/takeover/revocation writes with a row lock and re-checks the authority chain in-transaction; expected_* re-comparison blocks stale writes (epoch can never come back); tokens reach the database only as keyed digests and the verifier context never carries credential material; every new lease/revocation judgment uses the in-transaction PostgreSQL clock (SES-01); stable error codes (401 SESSION_REPLACED/EXPIRED, 403 CODE_SUSPENDED/REVOKED, 409 OTHER_DEVICE_ONLINE, 429 RATE_LIMITED, 503 fail-closed) |
| **Migration and Rollback** | No new migration — revision 029 (T13) already provides customer_session_state / customer_session_events / customer_idempotency_envelopes; rollback = code revert (session rows may remain; lapsed leases release naturally) |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | the fencing verifier wired into the business write routes (T21 SES-04/SES-05); the client switch-confirmation flow and OpenAPI regeneration (T30/T28 frontend gates); process-level proof of the multi-API-instance switch race (the PG row-lock semantics cover it, the T13 100-concurrency precedent); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T20 Section 14 Ledger Record

```text
任务/工作包：T20 / SES-02、SES-03
Owner / Reviewer：后端（Agent 执行）/ PR #52 chatgpt-codex-connector 评审（1 P1+2 P2，逐条实质修复含 4 例回归锁定：码状态门 FOR UPDATE 串行化建立/租约判定用 clock_timestamp() 锁后实际时钟/expected_lease_until 快照重比对）+ 安全自评审（结论见 commit message 与证据账本）
分支 / 基线 SHA：feat/customer-v3-t20-session-switch-fencing / 基线 939c305（main，T19 PR #51 squash）
上游规格段落：客户版任务清单 V3 §4 T20、§12.3 SES-02/SES-03；代码开发清单 V3 §9.2 customer_auth.py 冻结名、§3.3 test_customer_fencing.py 冻结名；激活码开发文档 §12.3 第五行显式 switch、§12.4 事务内 fencing、§6.1 API 表、§6.3 幂等、§13.2 错误码；测试与验收规格 §2.3/§3.4
改动文件：server/app/customer_session_routes.py（switch 端点+operation/now 双参数信封重放+码状态门 403）、server/app/customer_session_service.py（switch_session=login_session takeover=True+revoke_session 通用化 T16 吊销核心）、server/app/customer_auth.py（新增：verify_session_context 事务内 fencing 校验器）、server/app/customer_device_service.py（_revoke_session_riding_device 委托 revoke_session）、server/app/admin_activation_routes.py（suspend/revoke 调 revoke_session 传播+_transaction_now_iso）、server/tests/test_customer_sessions.py（+19 用例）、server/tests/test_customer_fencing.py（新增 17 用例：13 原始+4 PR #52 评审回归锁定）、docs/evidence/T20-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 36 例——switch 13（显式原子顶替：SWITCH 事件+epoch bump+新 token 单事务/同设备续租/同设备恢复 epoch+1/过期接管 TIMEOUT 事件/缺行建立/Bearer 必需/未知凭据 401/幂等键必需 400/丢响应重放同 token 同 epoch/共享 login:ip 预算 429 Retry-After/双设备并发串行化单赢家/零钱包扣费/停用码 403 CODE_SUSPENDED）+码状态门与撤销传播 6（停用/撤销码不建立会话/验证器拒停用码、撤销码、释放设备即使租约存活/管理 suspend/revoke 同事务传播会话失效）+fencing 13（最小六字段上下文/未知 token/switch 后旧 token 被拒/租约过期/logout 后/停用码活租约/撤销码活租约/释放设备活租约/expected session_id、epoch、device、user 四类二次比对各 1/不泄漏 token 或 digest）+PR #52 评审回归锁定 4（expected_lease_until 匹配快照通过/不匹配快照 SESSION_REPLACED/锁等待跨租约后 clock_timestamp 判定 SESSION_EXPIRED/码状态门 FOR UPDATE 使并发写码行阻塞到建立提交）
实现结果：显式原子 switch 落地（switch_session 复用 §12.3 状态机 takeover=True：仅显式确认流程进入顶替分支，普通 login 保持 409 无自动踢人；SWITCH 事件+epoch bump+新 token 单事务原子提交，旧 token 全实例 401 SESSION_REPLACED）；switch 幂等信封独立 session_switch operation（AAD 区分）、重放探针先于鉴权先于限流、共享 login:ip 预算（switch 不能绕过限流）；码状态门（login/switch 403 CODE_SUSPENDED/CODE_REVOKED，停用账户不建立会话）；撤销传播（T16 吊销核心通用化为 revoke_session：device_id 限定设备解绑/撤销委托，None=用户全量，管理端 suspend/revoke 同事务调用使 session 立即失效，服务端撤销而非客户端删 token）；事务内 fencing 校验器 customer_auth.verify_session_context（行锁+expected_* 二次比对+PG 时钟租约+码/设备状态 defense-in-depth 复查，稳定 SESSION_REPLACED/SESSION_EXPIRED，CustomerSessionContext 仅 6 字段最小权限面）；全部新判定用事务内 PG 时钟（SES-01）
验证命令与通过数：专项 71 passed（54 session+17 fencing）；全量 994 passed 零回归（990+4 例 PR #52 评审回归锁定）；ruff/format/mypy 全绿（162 files formatted，65 source files typed）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：login/switch 共享码状态门（停用账户不建立会话）；撤销路径经共享 revoke_session 核心传播（设备解绑/撤销与管理码 suspend/revoke）——epoch bump+租约拉过去+LOGOUT 事件记 actor，服务端撤销而非客户端删 token；fencing 校验器行锁串行化 switch/接管/撤销写入并在事务内复查权威链；expected_* 二次比对拦截迟到写（epoch 永不回跳）；token 只以 keyed digest 过库，校验器上下文永不携带凭据；租约/吊销判定全部用事务内 PG 时钟（SES-01）；稳定错误码（401 SESSION_REPLACED/EXPIRED、403 CODE_SUSPENDED/REVOKED、409 OTHER_DEVICE_ONLINE、429 RATE_LIMITED、503 fail-closed）
迁移与回滚：无新迁移（029 已预留全部结构）；回滚=还原代码（会话行可保留，租约到期自然释放）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：fencing 校验器接入业务写路由（T21 SES-04/SES-05）；客户端 switch 确认流程与 OpenAPI 重新生成（T30/T28 前端门禁）；多 API 实例同 switch 竞态进程级证明（PG 行锁语义覆盖，T13 100 并发先例）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T23 — Audited Admin Adjustments (BILL-02)

| Field | Value |
| --- | --- |
| **Task ID** | T23 / BILL-02 |
| **Owner / Reviewer** | Backend (Agent) / CodeReview sub-agent pass (0 P1/0 P2/4 P3, all substantively fixed with regression locks) + PR #54 chatgpt-codex-connector pass (1 P2, fixed with a regression lock) + security self-review |
| **Branch / Base SHA** | feat/customer-v3-t23-admin-adjustment / base af0308f (main, T20 PR #52 squash) |
| **Date** | 2026-08-24 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T23-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §5 T23, §12.5 BILL-02; code checklist §9.1/§9.3 (frozen admin_customer_routes.py); dev doc §15 admin write contract; acceptance spec zero-ledger-difference |
| **Failure Test or Regression Lock** | 24 red→green cases: schema shape + FK-unique audit-order link 2; revision 039 CHECK constraints (four violating shapes raise CheckViolation from PostgreSQL itself) 1; append-only UPDATE/DELETE/TRUNCATE all raise RaiseException with the survivor count intact 1; the happy path asserting all four tables (PAID admin_adjustment order with no third-party trade number + exactly one CHARGE row + wallet increment + the audit row naming the real admin) 1; INTERNAL pricing scope for an activation-less user 1; the write contract 5 (missing Idempotency-Key 400 / missing confirm 400 CONFIRMATION_REQUIRED / blank reason 400 REASON_REQUIRED / auditor write 403 AUDITOR_READ_ONLY while read stays 200 / anonymous write 401); business validation 4 (non-positive credits / int4-overflowing credits / unknown user 404 / wallet-less user 404); invalid source-document enum + blank ref 400 1; unit-price snapshot frozen on the order across a mid-test settings change 1; idempotency 4 (same key + same params replays the sealed 201 with X-Idempotent-Replay and charges exactly once / same key + different params 409 IDEMPOTENCY_CONFLICT / same key against a different target user 409 — the path-params-in-fingerprint PR #43 lesson / a failed 404 write rolls the placeholder back so the key stays reusable); zero ledger difference (summed CHARGE deltas reconcile the wallet balance exactly) 1; SQLite/missing-DSN fail-closed 503 ADJUSTMENT_SERVICE_UNAVAILABLE 1; the audit listing with pagination 2. Plus 5 review locks: the half-committed placeholder answers 409 not 500 (sub-agent P3 R1); the response balance is the post-UPDATE row via RETURNING (P3 R2); SUSPENDED prices as CUSTOMER_STANDARD while REVOKED does not (P3 R3); the RESTRICT FKs refuse every cascade delete with the audit row surviving (P3 R4); a wallet parked at the int4 ceiling plus credits=1 answers a stable 400 with the transaction fully rolled back — no balance/order/CHARGE/audit residue (PR #54 connector P2) |
| **Implementation Result** | The audited admin adjustment lands as one atomic PostgreSQL transaction: a `provider='admin_adjustment'` `status='PAID'` recharge order (revision 026 shapes — created PAID by double confirmation, no third-party trade number, paid_at from the transaction clock), a wallet `CHARGE` ledger row (task_id/billing_round NULL, idempotency key `admin_adjustment:charge:{order_id}`), the atomic wallet increment, and one append-only `admin_adjustments` audit row naming the real acting administrator — all four writes commit or roll back together, so no balance mutation can exist without its ledger row (禁止直接改余额). Amount discipline: amount_fen = credits × internal_base_unit_price_fen frozen on the order (charged == base, PRICE-01 holds by construction); the min/step ladder deliberately does not apply (revision 026 scopes it to zpay orders; an adjustment's amount is defined by its source document); an int4 overflow guard refuses credits whose derived amount exceeds 2^31-1 before the INSERT, and the atomic wallet increment carries the int4 ceiling bound in its WHERE clause (`available_credits <= 2147483647 - credits` — the PR #54 connector P2 fix) so a wallet-side overflow answers a stable 400 instead of a PostgreSQL NumericValueOutOfRange 500, with the wallet-still-there vs vanished distinction preserved (400 vs 404). Pricing scope inferred from the activation binding (CUSTOMER_STANDARD vs INTERNAL). The route runs behind the T09 admin gate (AdminWriter; auditors read-only) and the T12 idempotency snapshot layer: canonical route + path params + body fingerprint (a key replayed against a different target user answers 409, never a silent cross-resource replay), same-key replay of the sealed response, business failures rolling the placeholder back. Revision 039 adds the append-only `admin_adjustments` table (source-document enum + non-blank CHECKs, one unique audit row per order, rewrite-refusing trigger, the shared 036 TRUNCATE guard, and a lineage-preserving downgrade guard). The audit-trail listing serves operators and auditors with bounded pagination. SQLite/missing DSN answers 503 fail-closed; every timestamp samples the in-transaction PostgreSQL clock (SES-01) |
| **Verification Command and Pass Count** | pytest tests/test_admin_customer_routes.py → 29 passed (24 red→green + 5 review locks); full suite → 1023 passed (994 base + 29 new, zero regression); ruff check → all green; ruff format --check → 165 files already formatted; mypy app → success, 66 source files |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | every adjustment passes the T09 admin-session gate with the real actor persisted in the append-only audit row (auditors 403 AUDITOR_READ_ONLY); the §15 write contract fully enforced (Idempotency-Key / confirm / reason / request id); the idempotency snapshot layer blocks same-key cross-resource replays (409 on different params or a different target user — the PR #43 lesson regression-locked); balance changes are atomically bound to CHARGE evidence with the zero-ledger-difference assertion; admin_adjustments is triple append-only (row trigger + shared 036 TRUNCATE guard + downgrade lineage guard); amount overflow and source-document shape rejected at the route (400) with the PG CHECK constraints as defense in depth; no secrets in logs |
| **Migration and Rollback** | New revision 039_admin_adjustments (down_revision 038); the downgrade refuses once any audit row exists (operator lineage must survive rollbacks); a rollback requires manually exporting the audit trail first |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | the T33 management page (the read endpoint is ready); the joint real-chain reconciliation of ZPay recharges and adjustments (BILL-01/BILL-02 real-chain work rides with T35+); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T23 Section 14 Ledger Record

```text
任务/工作包：T23 / BILL-02（账务/管理）
Owner / Reviewer：后端（Agent 执行）/ CodeReview 子代理评审（0 P1/0 P2/4 P3 逐条实质修复含 4 例回归锁定：半提交占位符 409 而非 500/响应余额用 RETURNING 后的真实行/SUSPENDED 计入当前绑定定价/039 FK 全 RESTRICT）+ PR #54 connector 评审（1 P2 已实质修复含回归锁定：钱包余额 int4 溢出——amount_fen 检查只看 credits×单价看不到 available_credits+credits 侧溢出，PG 抛 NumericValueOutOfRange 变 500；修复为原子增量 UPDATE 的 WHERE 带界 available_credits <= 2147483647 - credits，钱包仍在但越界答稳定 400，钱包消失仍 404，锁定测试 test_wallet_balance_overflow_is_rejected_not_500 断言余额/订单/CHARGE/审计零残留）+ 安全自评审
分支 / 基线 SHA：feat/customer-v3-t23-admin-adjustment / 基线 af0308f（main，T20 PR #52 合并后）
上游规格段落：客户版任务清单 V3 §5 T23、§12.5 BILL-02；代码开发清单 V3 §9.1/§9.3 admin_customer_routes.py 冻结名；激活码开发文档 §15 管理写契约；测试与验收规格账本差额为零
改动文件：server/migrations/versions/039_admin_adjustments.py（新增：append-only 审计表+trigger+036 共享 TRUNCATE guard+血统保 downgrade）、server/app/admin_customer_routes.py（新增：调账创建+审计列表，原子四写：PAID order+CHARGE+钱包增量+审计行）、server/app/main.py（挂载路由）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 加 admin_adjustments——T07 导入对账契约适配，T18 先例）、10 个迁移测试文件 head 断言 038→039（每迁移标准维护，22 处；038 guard 消息匹配与 validate_revision_pair 字面参数保持不动）、server/tests/test_admin_customer_routes.py（新增 29 用例：24 红→绿 + 5 评审回归锁定）、docs/evidence/T23-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 24 例（schema 形状+唯一索引 2；CHECK 约束四形状 CheckViolation 1；append-only UPDATE/DELETE/TRUNCATE RaiseException+幸存计数 1；成功流四表断言 1；内部 scope 1；写契约 5：幂等键/确认/reason/auditor 只读/未认证；业务校验 4：非正数/int4 溢出/未知用户/无钱包；来源单枚举+空 ref 1；价格快照冻结 1；幂等 4：同键重放单次入账/同键异参 409/同键异目标 409/失败不烧键；账本差额为零 1；fail-closed 503 1；审计列表+分页 2）+ 评审回归锁定 5 例（半提交占位符 409 非 500；响应余额为 RETURNING 后真实行；SUSPENDED 计入/REVOKED 不计入当前绑定定价；RESTRICT FK 元数据+删除拒绝+审计行幸存；PR #54 connector P2 钱包余额 int4 溢出拒 400 非 500——余额停在 2147483647 加 credits=1 答稳定 400 且事务完整回滚零残留）
实现结果：后台调账作为单个原子事务落地（双确认+来源单+幂等快照+真实 actor 审计行四写同事务提交或回滚）；金额纪律 credits×内部单价快照冻结（PRICE-01 由构造成立，min/step 仅管 zpay 不适用调账——026 约束口径）；int4 溢出应用层防护；pricing_scope 按 027 当前绑定口径推导（ACTIVE/SUSPENDED 计入，REVOKED 仅审计）；钱包增量用 RETURNING 后真实行；admin_adjustments append-only（039 trigger+036 共享 TRUNCATE guard+全 RESTRICT FK+downgrade 血统保护）；审计列表供 operator/auditor（auditor 只读经 T09 门）；缺 PG 配置 503 fail-closed；SES-01 全部时间戳用事务内 PG 时钟
验证命令与通过数：专项 29 passed（24 红→绿 + 5 评审锁定）；全量 1023 passed（994 基线+29 新增，零回归；2 警告为既有环境噪声）；ruff/format/mypy 全绿（165 files formatted，66 source files typed）；npm run check 全仓门禁绿（secret/client 324/e2e/tauri/server）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：调账永远经 T09 admin session 门（真实 actor 写入审计行；auditor 403 AUDITOR_READ_ONLY）；§15 写契约全量执行（Idempotency-Key/confirm/reason/request id）；幂等快照层同键异参/异目标 409 防止跨资源重放（PR #43 教训回归锁定）；余额变更与 CHARGE 凭据原子绑定+账本差额为零断言（禁止直接 UPDATE 余额）；admin_adjustments 三重 append-only 防护；金额溢出与来源单形状在应用层 400 拒绝（PG CHECK 为纵深防御）；密钥/凭据不入日志
迁移与回滚：迁移 039_admin_adjustments（down_revision=038）；有审计数据时 downgrade 拒绝（保操作员血统）；回滚需先人工导出审计
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T33 管理页面（读端点已就绪）；真实 ZPay 续充与调账的联合对账（BILL-01/BILL-02 真实链路随 T35+）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T28 — Customer API Adapter from the Regenerated OpenAPI Contract (FE-01)

| Field | Value |
| --- | --- |
| **Task ID** | T28 / FE-01 |
| **Owner / Reviewer** | Frontend (Agent) / CodeReview sub-agent pass (1 P1 + 3 P3: the P1 and two P3s substantively fixed with regression locks, one P3 confirmed no-change) + chatgpt-codex-connector PR #55 pass (4 P2, all substantively fixed with regression/contract locks — C1 suspended-code event isolation, C2 X-Request-Id end-to-end, C3 login/switch 200 renewal declaration, C4 enroll phantom-200 removal) + security self-review |
| **Branch / Base SHA** | feat/customer-v3-t28-customer-api-adapter / base b3fe6d3 (main, PR #53 merged) |
| **Date** | 2026-08-24 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T28-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §6 T28, §12 FE-01, §10.1 (three lifecycle events); code checklist FE-01 file mapping; dev doc §6.1 API table, §6.3 idempotency, §7 security boundary (explicit credential passing), §3.3 (OTHER_DEVICE_ONLINE masked hint) |
| **Failure Test or Regression Lock** | 24 cases: drift lock 1 (the eight customer schemas locked by typed literals — any regeneration that changes a field set fails `tsc -b`, so hand-written shapes cannot drift into integration) + request-shape 10 (activate carries the idempotency key and no bearer; login with the device credential reports the 200-renewed/201-established outcome; renew+replay reads X-Idempotent-Replay; switch rides the same lane; heartbeat sends the session credential and no idempotency key; logout with key; device listing; unbind; enroll surfaces both pairing outcomes 202-pending/201-consumed; approve with the first device's credential) + error-state 9 (the login conflict carries its masked device hint + slot + lease expiry; the idempotency conflict stays separate from the device conflict; the rate limit keeps its Retry-After seconds; 503 → service-unavailable; the 403 code-status gate; transport network/timeout — no status to derive from, decided at construction; a non-JSON body falls back to determinate unknown without hiding the status; the three 401 lifecycle outcomes dispatch their three dedicated events; a mere invalid credential dispatches nothing) + review locks 2 (the 400 anti-enumeration rejections ACTIVATION_UNAVAILABLE/PAIRING_UNAVAILABLE stay bad-request — never the outage state, the R1 P1 lock; USER_ALREADY_ACTIVATED/DEVICE_SLOTS_FULL group under determinate conflict) + PR #55 locks 2 (a suspended 403 CODE_SUSPENDED dispatches no event while 403 CODE_REVOKED still fires revoked — the reversible/permanent split; every request stamps X-Request-Id, the error retains it, an explicit id travels through) |
| **Implementation Result** | The customer lane lands as the fourth transport lane in api.ts next to the three internal ones, every type cut from the regenerated generated/api.ts (194KB→291KB, 126 paths/130 schemas — the 2026-08-20 artifact predated T13/T17/T19/T20/T23, so none of the eight customer schemas existed). The server side fills the T17 enroll OpenAPI gap: DeviceEnrollPendingResponse (202) and DeviceEnrollConsumedResponse (201) declared through the responses= parameter for the client types only, while the route keeps its raw JSONResponse answers (the two bodies differ by design; the models never gate the runtime); three contract tests (enroll/sessions/activate) refuse a silent response_model drop. 401/403/409/429/idempotency errors all resolve to exactly one of the 18 CustomerApiErrorKind values (code-exact matches first, status fallback second; transport failures carry a private transportKind decided at construction) — the UI never parses a raw status line. OTHER_DEVICE_ONLINE carries the masked device hint + slot + lease expiry; RATE_LIMITED keeps Retry-After. The three §10.1 lifecycle events (expired/replaced/revoked) replace the internal lane's single SESSION_EXPIRED_EVENT so the workspace shows the sentence matching what actually happened; only lifecycle terminal states dispatch — a mere DEVICE_CREDENTIAL_INVALID never tears down the UI. Credentials travel as an explicit discriminated union ({kind:"device"}|{kind:"session"}) — no global plaintext variable (dev doc §7); desktop persistence is T29's Tauri layer. Idempotency per contract: activate/login/switch/logout/unbind/enroll carry Idempotency-Key, heartbeat (naturally idempotent) does not; X-Idempotent-Replay is read into a replayed flag; login surfaces 200-renewed vs 201-established; enroll returns the 202-pending/201-consumed discriminated union |
| **Verification Command and Pass Count** | vitest src/customerApi.test.ts → 24 passed (20 red→green + 2 sub-agent locks + 2 PR-review locks); client full vitest → 348 passed (324 base + 24 new, zero regression); server PG three-file sweep (devices/sessions/activation) → 128 passed incl. the 3 new contract locks (assertions extended: login/switch 200 $ref, enroll no-200); full suite → 1026 passed (1023 base + 3 new, zero regression; the 2 warnings are the pre-existing environment artifacts); ruff check → all green; ruff format --check → 165 files already formatted; mypy app → success, 66 source files; npm run check → green (secret scan / client biome+tsc+vitest 348 / e2e / tauri cargo / server gates) |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | credentials never land in a global variable or Web Storage (explicit arguments, dev doc §7; desktop persistence is T29's scope); the error envelope parser never hides the HTTP status (a non-JSON body still carries status into the determinate unknown state); the anti-enumeration 400s stay user-fixable (bad-request) instead of masquerading as outages — the R1 P1 fix with its regression lock; lifecycle events fire only on true session-terminal outcomes, so an input error never tears down the workspace, and a reversible CODE_SUSPENDED never fires the revoked event (PR #55 C1 — the device credential survives an admin's suspension window); every request carries X-Request-Id and CustomerApiError retains it for the §13.2 IDEMPOTENCY_CONFLICT report (PR #55 C2); path parameters pass through encodeURIComponent (the R2 P3 fix); no secrets or credentials in logs or fixtures |
| **Migration and Rollback** | No new migration (the enroll responses= declaration is OpenAPI-documentation-only; runtime behaviour unchanged); a rollback is a code revert |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | Desktop credential persistence and restart recovery (T29); the second-device pairing/conflict/switch UI flows (T30); device management and heartbeat interactions (T31); the full browser E2E chain (T34); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T28 Section 14 Ledger Record

```text
任务/工作包：T28 / FE-01（客户前端）
Owner / Reviewer：前端（Agent 执行）/ CodeReview 子代理评审（1 P1 + 3 P3：P1 与 2 P3 逐条实质修复含 2 例回归锁定，1 P3 确认无需修改）+ chatgpt-codex-connector PR #55 评审（4 P2 逐条实质修复：C1 CODE_SUSPENDED 移出 revoked 事件道——可逆暂停不清设备凭据；C2 X-Request-Id 全链路并保留在 CustomerApiError.requestId；C3 login/switch 契约补 200 续期响应模型；C4 enroll 幽灵 200 消除——status_code=202 对齐）+ 安全自评审
分支 / 基线 SHA：feat/customer-v3-t28-customer-api-adapter / 基线 b3fe6d3（main，PR #53 合并后）
上游规格段落：客户版任务清单 V3 §6 T28、§12 FE-01、§10.1（三事件拆分）；代码开发清单 V3 FE-01 文件映射；激活码开发文档 §6.1 API 表、§6.3 幂等、§7 安全边界（凭据显式传参，禁止全局明文变量模拟持久会话）、§3.3（OTHER_DEVICE_ONLINE 掩码提示）
改动文件：client/src/generated/api.ts（再生成 194KB→291KB，126 paths/130 schemas，8 个 customer schema 进入契约）、client/src/api.ts（新增 customer 车道 ~500 行：CustomerCredential 判别联合+CustomerApiError 18 kind+三生命周期事件+requestCustomer 传输层+9 个 API 函数）、client/src/customerApi.test.ts（新增 22 用例）、server/app/customer_device_routes.py（enroll 双响应 OpenAPI 契约：responses= 参数文档化 202/201 双模型，运行时仍裸 JSONResponse）、server/tests/test_customer_devices.py / test_customer_sessions.py / test_customer_activation.py（3 个 OpenAPI 契约锁定测试）、docs/evidence/T28-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 20 例（漂移锁 1+请求形状 10+错误状态 9）+ 评审回归锁定 2 例（400 反枚举不进 outage 保持 bad-request；USER_ALREADY_ACTIVATED/DEVICE_SLOTS_FULL 归确定 conflict）+ PR #55 锁定 4 例（403 CODE_SUSPENDED 不派发事件且 CODE_REVOKED 仍派 revoked；X-Request-Id 每请求携带+错误保留+显式透传；服务端契约断言扩展 login/switch 200 $ref 与 enroll 无 200）
实现结果：customer API/error/credential adapter 全部从再生成 OpenAPI 契约切出（漂移锁让字段漂移在 tsc 编译期失败）；enroll 补齐 202/201 双响应模型（T17 遗留 OpenAPI 空缺，运行时零变化）；401/403/409/429/幂等错误全部进入 18 个确定 CustomerApiErrorKind（UI 永不解析裸状态行）；三事件拆分（§10.1）仅生命周期终态派发；凭据显式判别联合传参（§7 红线）；幂等语义按契约逐端点执行，X-Idempotent-Replay 读为 replayed，login 200/201 与 enroll 202/201 双判别联合
验证命令与通过数：client 专项 24 passed（20 红→绿+2 评审锁定+2 PR #55 锁定）；client 全量 vitest 348 passed（324 基线+新增，零回归）；服务端 PG 三文件 128 passed（含 3 新契约锁，断言扩展覆盖 200/无 200）；全量 pytest 1026 passed（1023 基线+3 新增，零回归；2 警告为既有环境噪声）；ruff/format/mypy 全绿（165 files formatted，66 source files typed）；npm run check 全仓门禁绿（secret/client biome+tsc+vitest/e2e/tauri/server）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：凭据永不落全局变量或 Web Storage（显式参数传递）；错误信封解析不吞 HTTP 状态；反枚举 400 保持用户可修正不误导为服务中断（P1 修复+回归锁定）；生命周期事件只在会话真正终态派发，可逆 CODE_SUSPENDED 不触发 revoked（PR #55 C1）；X-Request-Id 每请求携带且保留在错误对象上供 §13.2 上报（PR #55 C2）；路径参数 encodeURIComponent；无密钥/凭据入日志或夹具
迁移与回滚：无新迁移（OpenAPI 文档面变更，运行时行为不变）；回滚即还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：凭据桌面持久化与重启恢复（T29）；第二设备配对/冲突/切换 UI（T30）；设备管理/heartbeat 交互（T31）；浏览器全链路 E2E（T34）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

---

## T22 - Customer Session Recharge (ZPay Top-up)

| Field | Content |
| --- | --- |
| **Owner** | Backend/QA |
| **Reviewer** | Codex + connector review (PR #59 12 findings all fixed; PR #65 4 findings fixed incl. P1 fresh-connection 500 / P1 OpenAPI regen / P2 poll resume) |
| **Branch / SHA** | `feat/customer-wallet` / PR #65 (`5e6373d` + follow-up fixes); base = main `12f1094` |
| **Upstream Spec Sections** | `docs/客户版任务清单-V3.md` §3 T22; `docs/客户版激活码完整开发文档-V3.md` §12.5 BILL-01 |
| **Files Changed** | - Migration 040: `server/migrations/versions/040_fix_provider_settings_constraint.py`
- Implementation: `server/app/recharge_routes.py` (`create_customer_recharge_order` + customer-lane wallet reads: GET `/api/customer/wallet`, `/api/customer/wallet/transactions`, `/api/customer/recharge-orders` list, `/api/customer/recharge-orders/{order_no}`)
- Frontend: `client/src/customer/CustomerWalletPanel.tsx` (new), `client/src/api.ts` customer lane +5 functions, `client/src/generated/api.ts` regenerated (OpenAPI 133 paths), `App.tsx`/`WorkspaceShell`/`CustomerWorkspace.tsx` wallet wiring
- Test suite: `server/tests/test_customer_recharge.py` (18 tests incl. fresh tuple-row pool regression), `client/src/customer/CustomerWalletPanel.test.tsx` (3 tests), `client/src/customerApi.test.ts` / `DeviceManagementPage.test.tsx` (contract drift fixtures)
- Evidence: `docs/evidence/T22-EVIDENCE.md` |
| **Failure Test or Regression Lock** | 2 core tests (state preservation + amount validation) → 18 recharge tests (red→green); fresh pooled PG connection tuple-row 500 regression lock (Codex P1); pending-payment poll resume test (Codex P2); client 513 tests all green |
| **Implementation Result** | Customer-session recharge (T22) + wallet view (task #7): POST `/api/customer/recharge-orders` creates PENDING order with ZPay payment form; credits enter the same wallet only after PAID callback; customer-lane read endpoints answer balance/billing, ledger and order list under the fenced session (internal wallet API 401'd a customer session — root cause of the task #7 defect); front-end CustomerWalletPanel shows balance/recharge/orders/ledger without a second main-code entry |
| **Verification Command and Pass Count** | `pytest server/tests/test_customer_recharge.py`: 18 passed in ~15s; `pytest` full server suite: green; `cd client && npx vitest run`: 513 passed; `npx tsc -b`: clean; ruff format/check: clean; biome: clean; generated/api.ts regenerated from live `app.openapi()` (133 paths); `npm run test:customer-e2e`: 4 passed (activation×2, pairing, recharge — PR #66) |
| **Evidence Level** | `AUTOMATED_VERIFIED` (specialized + contract-drift-locked tests passing; awaiting STAGING_VERIFIED pending fake ZPay sandbox authorization) |
| **Security and Observability** | Credentials Fernet encrypted; no hardcoded secrets; parameterized SQL; customer reads owner-isolated (another user's order 404, no session 401); fenced session re-verified in-transaction; named-row factory installed by the route itself (fresh-connection safe) |
| **Migration and Rollback** | Migration 040 upgrade/downgrade/re-upgrade three-phase verified; wallet extension adds no migration |
| **External Authorization Record** | None (fake ZPay simulation only; real chain requires external authorization) |
| **Untested Items** | Real ZPay sandbox callback E2E (STAGING); browser conflict/switch UI E2E (needs Tauri vault, covered by unit + backend chain instead); stale order cleanup; load testing; SEC-01专项审查 |
| **Blocking Dependencies** | STAGING_VERIFIED blocked by fake ZPay sandbox; PRODUCTION_GO blocked by SEC-01 + T40 real ZPay + legal approval |
| **Lore Commit SHA** | `5e6373d` (PR #65 feat/customer-wallet) |

### T22 Section 14 Ledger Record

```text
任务/工作包：T22 / BILL-01（+ 任务 #7 客户钱包视图接线）
Owner / Reviewer：账务/后端（Agent 执行）/ Codex + connector 评审（PR #65）
分支 / 基线 SHA：feat/customer-wallet / main@12f1094（PR #65）
上游规格段落：docs/客户版任务清单-V3.md §3 T22; docs/客户版激活码完整开发文档-V3.md §12.5 BILL-01
改动文件：server/app/recharge_routes.py（+4 客户 lane 读端点：wallet/wallet-transactions/recharge-orders 列表/单号）、server/tests/test_customer_recharge.py（18 专项含 fresh tuple-row 回归）、client/src/customer/CustomerWalletPanel.tsx（新增）、client/src/api.ts（客户 lane +5 函数）、client/src/generated/api.ts（重生成 133 paths）、client/src/customer/CustomerWalletPanel.test.tsx（3 用例）、docs/evidence/T22-EVIDENCE.md
失败测试或回归锁定：Codex P1 fresh PG 池连接 tuple-row 500（列表端点先在 fresh 连接 500，回归测试锁定）→ 修复（路由自行安装命名行工厂）；Codex P2 待支付订单重开钱包不恢复轮询 → 修复（从订单列表派生恢复）+ 回归测试；client 全量 513 用例
实现结果：客户 session 续充（PENDING 订单 + ZPay 表单，PAID 回调后同钱包入账）+ 钱包视图读端点与前端（CustomerWalletPanel：余额/续充/订单/流水），修复客户 lane 钱包 401 缺陷
验证命令与通过数：pytest tests/test_customer_recharge.py → 18 passed；client npx vitest run → 513 passed；npx tsc -b clean；ruff/format/biome clean
证据层级：AUTOMATED_VERIFIED（专项 + 契约漂移锁）→ STAGING_VERIFIED（需 fake ZPay sandbox 授权）
安全与可观测性：fenced session 事务内重验；owner 隔离读（他人单 404）；命名行工厂路由自装；参数化查询；Fernet 加密
迁移与回滚：无新迁移（040 已三阶段验证）；wallet 扩展零 schema 变更
外部授权记录：None（real chain 待法务/商务授权）
未测试项：REAL_CHAIN_VERIFIED、PRODUCTION_GO（T35 + T40）、浏览器冲突/切换 UI E2E（需 Tauri vault；续充浏览器 E2E 已交付 PR #66，`npm run test:customer-e2e` 4 用例）
Lore 提交 SHA：5e6373d（PR #65 feat/customer-wallet）
```


## CW-031 云端资产与授权下载回退核销（代码与测试证据）

- 任务：CW-031（W4）核销云端资产与授权下载的剩余回退；分支 feat/customer-v3-cw031-cloud-asset-no-local-fallback，基线 origin/main@e829ad1。
- 交付：`get_media_storage` 正式服务双闸门（active_storage_provider 主 + is_customer_production 兜底，缺 COS 配置 503 STORAGE_PROVIDER_FORBIDDEN，绝不回退本地持久盘）；`storage_for_asset` 历史 local URI 客户生产拒绝/非客户生产只读追溯（CW-037 标记）；`bootstrap._probe_formal_service_write_path` 启动期写路径探测；新增 `server/tests/test_storage_cross_instance.py` 27 用例（跨实例三独立连接/adapter、COS put/get/sign 故障本地零新增、授权签名矩阵、缓存目录删除后 COS 恢复、启动探测）。
- 验证：专项 27 passed / 0 skip；缺 PG 硬门 3 passed / 24 errors / 0 skipped rc=1；签名护栏 ×4 全绿；既有回归 265 passed / 8 failed（ffmpeg 环境缺陷，stash 基线对照证实与本任务零关系）；ruff/format/mypy --strict 全过。
- 证据层级：AUTOMATED_VERIFIED（真实 PG 16 + 共享内存 COS 替身；真实凭据链归 CW-050、生产执行归 CW-051，需人工授权）。
- 详细证据：`docs/evidence/CW031-EVIDENCE.md`（§2 闸门口径、§5 跨实例披露、§6 权限矩阵、§8 交接披露）。

## 本地实现核查与去重定义V3（文档证据）

- 输入：V2定义bf6aab8，应用基线bffc341；新输出与57项任务见 outputs/customer-cloud-convergence-analysis-2026-09-08/v3/。
- 原60项逐项核对：39项有可复用代码/测试/工具，2项目标结构尚缺；准备/条件与现场任务另计。CW-006/008/011合并到具体实施及通用要求，不作为已验收完成。
- 本轮17项定向纯逻辑/配置合同通过（5项AST/preflight、12项生产配置；另46项未选）；未执行PG业务测试、实际迁移、真实付费或生产切换。
- 静态一致性、原源码指纹、固定分支对象、依赖/链接/CSV/账本及独立文档复核结果以 v3/validation.json 为准。应用实际证据级别未提升，57项实施状态仍待相应工作与验收。

## CW-068 C5 发布管理第一阶段·发布账号授权（代码与测试证据）

- 任务：CW-068（C5 第一阶段＝账号授权最小闭环，正式发布链路留第二阶段另立任务）；分支 feat/customer-v3-cw068-publish-accounts，基线 origin/main@a093f61；既有 origin/feat/c5-publish-module@7e5450a **只作裁剪来源只读参考，不派生不堆叠**。治理四项与代码同一 PR：CW002 §7 追加 2026-09-11 范围变更签认（保留 09-09 原文不篡改）、新增 §8 发布管理专用验收矩阵 A1–A16 与 §8.2 明确不验收 4 条、拆解账本 C5 行 `⏸ 暂缓`→`🔨 开发中（第一阶段：账号授权）`、认领登记 L20/L28。
- 交付：新增迁移 head `082_publish_accounts`（down_revision 081，只建 `publish_accounts`，三条 CHECK，downgrade 只 drop 该表）；`publish.py` 978→**392** 行（records 模型/段全删、`PublishLease.kind` 收窄为 `Literal["account_verify"]`、`_quarantine_expired_publishes` 拆出 `_quarantine_expired_verifies` 与 records 解耦）；`publish_routes.py` 108→**76** 行只留 4 个 accounts 端点；`publish_worker.py` 289→**160** 行只留 verify round；`publishers/` 21 文件（含 vendor）整体搬入；`test_publish_accounts.py` **11** 用例（SQLite→真实 PG 夹具改造）；前端 6 文件接线（`MainPages.tsx` +327/−47 移除两处未接通占位、`live.ts`/`api.ts`/`types.ts`/`studio.css`、5 个前端用例）。**计划外修复两项真实缺陷**：① `publish_worker.py` 携带 `from app.db import connect_database` 违反 CW-060「历史 SQLite 表面反向消费者只减不增」注册表 → 删除整条 SQLite lane，形态逐字对齐 `generation_worker.py` 的 CW-025/CW-030 治理结论（防御性 RuntimeError + `try/finally close_pg_pool()`）；② 分支遗留的 `requestSeq` 并发竞态（初次加载的在途响应晚归会用旧快照覆盖刚连接/刚解绑的账号）→ 引入单调序号守卫 + 幂等 upsert + 卸载 clearTimeout。
- 验证：专项 `test_publish_accounts.py` **11 passed**、`test_cw060_operator_isolation.py` **7 passed**（修复前 1F/6P）；ruff check All checks passed / ruff format 321 files / mypy 120 files Success / `uv sync --locked` RC=0 / `verify_no_secrets.sh` RC=0 / `build-test-shards.py --check-coverage` 117 files RC=0；client biome 202 files、`tsc -b` RC=0、vitest **80 files 1301 tests passed**、check:e2e 15 files；desktop build + build:admin + verify:customer-bundle 全 RC=0（阳性对照 6/6 自证非空洞）；pytest 四片 shard-0 685P / shard-1 **585P**（并行首轮挂起后串行独占重跑）/ shard-2 572P / shard-3 736P。**31 个既有失败经 `a093f61` baseline worktree 逐项复现归因**（cw033×22 Windows 无 `.sh` 解释器关联、cw009×4 与 security_contracts×1 `read_text()` 缺 `encoding=` 致 GBK 解码失败、storage×3 PG lane 按 CW-026/CW-031 设计拒绝 `X-Dev-User-Id`、analytics×1 日期敏感），全部与本任务零关联；其中 storage×3 与 analytics×1 已被并行任务 CW-043（`2be7c3d`，PR #46）独立修复，其修复注释逐字印证本任务归因。A5 密文落库与 A15 前端凭据不回显均经 **mutation 验证**非空洞。
- 证据层级：**AUTOMATED_VERIFIED**（真实 PG 16 容器 vs-pg-cw068@5441、专属库 cw068_publish_accounts_test，不触碰他任务端口）。真实平台探测/发布链路**不得标 PRODUCTION_GO**——按硬红线需真实凭据人工授权，本轮全部使用合成凭据且 `_dispatch_probe` 被 monkeypatch 替换**不触网**；整个 `server/app/publishers/`（21 文件）无测试直接执行，如实登记为 **CODE_PRESENT**。
- 详细证据：`docs/evidence/CW068-EVIDENCE.md`（§2.3 CW-060 修复与注释字面量陷阱、§3.3 CW-056 冻结矩阵四项常量实质同步 + digest 真实 PG 重算 `a23fa275…`、§4.2 A1–A12 映射、§6.5 baseline 归因表、§8.1 计划偏离 **24** 项、§8.2 CODE_PRESENT 面、**§8.5 基线已过时的合并前置动作清单**——origin/main 已前进到 `2be7c3d`，冲突面精确 5 文件：`pg_test_kit.py` 双方同一插入点需保留两侧注册项、4 个 `shard-*.txt` 双方均重生成故**不可手工合并**，须在合并后树上重跑 `build-test-shards.py --shards 4` 使覆盖数达 **119**，否则 CW-061 的 CI-7 fail-closed 守卫报红）。本分支**未 commit、未 push**（owner 未授权）。



## FIX-W15-20260912 / 第一组 W15 / ADM-08、ADM-09：上海业务日与一致的 CSV 导出

AUTOMATED_VERIFIED（本地）；独立只读评审 PASS；完整本地质量门通过：服务端 2879 passed、1 原有 TLS 场景跳过；前端 1350 passed；secret、Biome、TypeScript、e2e lint、Tauri fmt/check、ruff、format、mypy 均通过。远程 CI、PR 与合并待完成；人工联合调试全部留第二部分。[任务证据](evidence/FIX-W15-20260912.md)。

已通过独立只读复审，完整本地门保留原实测基线；账号主干791fd66整合专项后端134/前端53和静态通过。W13已正常合并PR #84（cd8bccf），本任务合入该主干至1839c86后权限、日期、导出及幂等专项后端322/前端141和静态全部通过。远程PR #85即将更新，必须以更新后当前SHA三门禁成功为合并条件；人工联合调试仍留第二部分。

## FIX-W19-20260912 / W19

AUTOMATED_VERIFIED（本地）；独立只读评审及信号修复复审 PASS；main@9bfe593 整合代码 d515e02 完整本地门通过：后端 2950 passed、1 原有 TLS 跳过，前端 1348 passed，secret、Biome、TypeScript、e2e lint、Tauri fmt/check、ruff、format、mypy 全部通过。PR、远程 CI 和合并待完成；全部人工联合调试留第二部分。[任务证据](evidence/FIX-W19-20260912.md)。

## FIX-W20-20260912 / W20

AUTOMATED_VERIFIED（本地）；独立只读评审 PASS；完整本地质量门通过：服务端 2858 passed、1 原有 TLS 场景跳过；前端 1344 passed；secret、Biome、TypeScript、e2e lint、Tauri fmt/check、ruff、format、mypy 均通过。远程 CI、PR 与合并待完成；人工联合调试全部留第二部分。[任务证据](evidence/FIX-W20-20260912.md)。

## FIX-TESTBASE-20260912 / 独立前置：日期敏感测试夹具

独立评审 PASS；完整本地静态门通过（前端 1344 passed、TypeScript、Biome、Tauri、ruff、format、mypy）；服务端四个独占 PG16 分片合计 2845 passed、1 原有 TLS 场景跳过，覆盖检查通过、退出码均为 0。此前中断的慢速分片保留日志，不记作通过。最终 PG 使用临时内存盘，fsync 和 synchronous_commit 保持默认开启；未执行生产或真实服务验收。 [任务证据](evidence/FIX-TESTBASE-20260912.md)。

## FIX-W18-20260912 / W18

AUTOMATED_VERIFIED（本地）；独立只读评审 PASS；完整本地质量门通过：服务端 2875 passed、1 原有 TLS 场景跳过；前端 1344 passed；secret、Biome、TypeScript、e2e lint、Tauri fmt/check、ruff、format、mypy 均通过。远程 CI、PR 与合并待完成；人工联合调试全部留第二部分。[任务证据](evidence/FIX-W18-20260912.md)。

## FIX-WALLETSTATUS-20260912 / 钱包提示竞态前置修复

AUTOMATED_VERIFIED（本地）；独立只读评审 PASS；完整本地质量门通过：服务端 2845 passed、1 原有 TLS 场景跳过；前端 1348 passed；secret、Biome、TypeScript、e2e lint、Tauri fmt/check、ruff、format、mypy 均通过。PR #81 三门禁全成功后已合并为 `4d2e598`；人工联合调试全部留第二部分。[任务证据](evidence/FIX-WALLETSTATUS-20260912.md)。

## FIX-ADM02-20260912 / W12

[每日售价重放证据](evidence/FIX-ADM02-20260912.md)：独立评审 PASS；最新主分支集成后完整本地门通过，前端 1344 passed、服务端 2847 passed 和 1 原有 TLS 场景跳过；覆盖检查及全部退出码为 0。PR #80 当前 SHA 三门禁全成功，已 squash 合并为 `47c9ffb`；人工联调留用户团队第二部分。

## FIX-W13-20260912 / W13

AUTOMATED_VERIFIED（本地）；独立只读评审 PASS；完整本地质量门通过：服务端 2881 passed、1 原有 TLS 场景跳过；前端 1347 passed；secret、Biome、TypeScript、e2e lint、Tauri fmt/check、ruff、format、mypy 均通过。远程 CI、PR 与合并待完成；人工联合调试全部留第二部分。[任务证据](evidence/FIX-W13-20260912.md)。


## UC-BATCH-02（UC-06—10）

第一批前置 PR #79 已合并 @37a2633；第二批个人中心前后端联调及 38 项后端专项通过；最终集成 PG 2906 passed/1 原有跳过、真实浏览器 4 passed，静态门及评审恢复增量见证据。用户去重反馈已纳入五页签及无设备展示。[完整证据](evidence/UC-BATCH02-PERSONAL-CENTER.md)。后续积分计价/来源归属/管理员加分尚未验收。

## FIX-TESTREADY-20260912 / 自动化测试前置

AUTOMATED_VERIFIED（本地）；独立只读 review_w13 PASS；完整本地质量门在 a44672a（已整合主干 cd8bccf）通过：后端 2957 passed、1 个原有 TLS 跳过；前端 1350 passed；secret、Biome、TypeScript、e2e lint、Tauri fmt/check、ruff、format、mypy 全部通过。PR、当前提交远程 CI 和合并待完成；产品文件未改动，F06另行发现的产品竞态未在此修复。人工联合调试全部留第二部分。[证据](evidence/FIX-TESTREADY-20260912.md)。

## UC-BATCH-03 / UC-11—15

第三批从 main@791fd66 开工并集成 820c3d8；后台积分价与前端真实读价、Token 来源及共享钱包、消费筛选/跳转、充值快照已实现。完整门禁及收尾自检中；前两批 #79/#86 三门禁通过且已合并，旧状态以本次回填为准。[第三批证据](evidence/UC-BATCH03-POINTS-PRICING.md)。

第三批最终本地：2972 PG passed / 1 原有 TLS skip，1354 前端 passed，完整静态门全绿，自检发现项已修复。证据层级 AUTOMATED_VERIFIED，PR/CI/合并待完成。

## W15 主干前置合并与第二部分隔离联调补充

2026-09-13 最新记录：PR #88 已以 820c3d8 合入主干，本任务整合为 b2f37a9，独立只读 review_w12 PASS。生产代码未变，新增前端受影响复验117 passed，secret/Biome/TypeScript通过（W15-testready-frontend.log）；此前完整门及322后端/141前端结果保留实际基线。PR #85 将更新，当前远程门禁待新提交结果，不引用旧绿灯代替。

按用户新授权开展第二部分：真实 Chromium—Vite—uvicorn—专属PG验证3项通过：上海日界5条中命中3条，列表与真实CSV订单/流水一致；5001条导出5000条及响应头/页面截断提示一致。证据在仓库上级 outputs/remediation-20260912/joint-round2/w15/。本轮使用仓库既定本地代理认证车道，503 CONTROL_AUTH_NOT_CONFIGURED首次环境记录保留；并未核销生产模式Cookie路由或目标数据库时区验收。管理员设密/恢复/角色9项实际会话检查另有记录，不与旧代理车道混计。代码未因联调修改。

## W19 主干前置合并与第二部分隔离联调补充

2026-09-13 最新记录：PR #88 已以820c3d8合入main，本任务整合为8cca2e1。独立只读review_w13确认Worker实现无回退、W13认证及TESTREADY与main一致；认领记录“尚无PR”已修正。新主干相关前端142 passed及secret/Biome/TypeScript通过，后端认证/Worker/发布143 passed（W19-testready-{frontend,backend}.log）。首次专项因新建数据库名未列入仓库白名单而拒绝，64 passed/79 setup errors的环境失败日志保留；使用同一独享新容器内已登记customer_v3_test后通过，未修改测试白名单。完整门仍对应此前记录的实际基线，PR #87当前门禁待更新提交。

用户授权第二部分后，真实四generation CLI并发启动使用同一逻辑标签，4个实例ID均唯一，均PG就绪并--once退出0；真实publish CLI配置30秒空闲间隔时收到SIGTERM后0.214秒退出0（未直接探测具体等待阶段），PG连接恢复基线0。3项进程检查通过，证据在仓库上级 outputs/remediation-20260912/joint-round2/w19/。首次缺COS配置时4实例失败记录保留；后续仅写合成配置、空队列验证，没有调用供应商或真实平台。不能据此声明实际平台探测、带任务租约恢复、生产编排或容量通过。


2026-09-13 W15整合续记：W19已正常合并PR #87，当前7ce8502三门禁全部成功，squash e8445c4。上文未合并状态为历史记录。W15合入该已合并主干，四份共享文档分别保留两个任务的事实，不按勾选并集推定完成；业务文件没有文字冲突，整合专项与当前提交CI待记录。


### W15 合并Worker主干后复核

W19主干整合最终复验：PR87正常合并为main e8445c4，本任务整合提交fbbcbfe。独立只读review_w12 PASS：W19三个生产文件及两个测试与main完全一致，W15业务和测试未改变；四共享文档保留双方事实与真实合并状态。新专项test_customer_ha_smoke/test_publish_accounts/test_admin_customer_routes共121 passed，服务端ruff/format/mypy通过（W15-w19-integration-backend.log、W15-w19-integration-static.log）。未因仅后端主干增量重复全量前端；此前117前端及完整本地门保留实际基线。PR85当前更新提交的CI需另行通过。

第二部分补验：W15实际浏览器/HTTP/CSV三个场景在UTC、Asia/Tokyo、America/Los_Angeles数据库连接会话时区均通过，五条日界记录命中三条、导出5001/5000提示一致；timezone-results.json按时区归档，不重复累加为新场景。本地代理认证车道不冒充生产Cookie入口。最终联调统计和人工输入见JT2独立证据。


## UC-BATCH-04 / UC-16—20

前置第三批 PR #90 head d9c328d 三门禁成功，squash 40e261c 已在 origin/main。本批主干 worktree 实现及本地验收完成，并整合 #91；完整静态门含前端 1362 passed，真实 PG 全量 1910 passed/1 原有 TLS skip，三路并发与正式前后台积分链路通过，进入 PR 收尾。[证据](evidence/UC-BATCH04-ACCOUNT-CREDIT-OPERATIONS.md)与[上线前评估](evidence/UC-ACCOUNT-PRELAUNCH-REPORT.md)登记 Docker 授权恢复、真实页面缺陷修复和完整日志；外部付费/生产验收仍单列。企业成员 Token 延期，无真实支付/付费 Provider/生产变更。


## FE-PRELAUNCH-20260913

[任务证据](evidence/FE-PRELAUNCH-20260913.md)：本机官方扫码与账号目录隔离、原生平台 Logo、云端发布草稿、搜索、任务通知及退出错误修复。本地最终静态门通过（前端 1378），后端全量 1914 passed / 1 原有 TLS skip；已核验 PR #94 三门禁通过并合并结果以对应 PR 记录为准，真实扫码及发布未验收。

## FE-VIDEO-PREVIEW-20260913

视频预览统一 9:16，横屏完整显示并使用模糊背景；范围、失败修复、播放证据及验证进度见[任务证据](evidence/FE-VIDEO-PREVIEW-20260913.md)。

## LOCAL-JOINT-20260913：本地联调与管理操作调整

用户持续联调反馈纳入本任务：品牌统一、自动积分单号、去设备/激活码/在线会话展示、客户详情精简和 ID 复制、经营筛选布局、单输入充值换算、API 成本售价表格、客户供应商信息隐藏、响应式导航与字体、管理员默认支付通道及官方支付标识。新增 H3 多账号轮询，每个账号独立填写并发额度，新账号不预设 10；任务与提交账号持久绑定。文件与验证记录见 [LOCAL-JOINT-20260913](evidence/LOCAL-JOINT-20260913.md)。

本地四服务运行正常；两次增量迁移和重启保留账号、会话、积分余额及凭据。最终 Linux 静态门与前端 1382 项通过。后端最终四片全量 1959 passed / 1 skipped，93 个测试文件全部覆盖，每片独立 PostgreSQL；此前导入与分片登记失败均已修复，原始记录保留。相关 32 项及迁移守卫通过。本地功能已交付，远程 CI 与评审待 PR。Owner 为当前 Codex 任务；Reviewer 待 PR 评审，不冒称独立评审。不改变既有 CW 生产验收结论，不进行真实支付、付费生成或公网发布。

本地实现提交 `cc247c1`；推送被自动审批阻止，待用户明确确认远程发送后创建 Draft PR。未推送、未合并，本地联调服务保持运行。

### 管理端表格、积分单位与支付配置收尾（2026-09-13）

客户表格精简为用户名、客户 ID、注册时间、状态、可用额度、累计消耗、生成情况和操作；删除姓名、冻结和待关注展示。筛选按提交应用，导出使用同一条件，迟到请求不覆盖新结果。成本与售价统一为积分，成本按充值换算展示并保留货币底账；提交校验换算版本，避免旧比例写入。表格正文 14px、辅助标签 13px，窄屏客户表格与其他横向滚动数据表分别适配。

支付默认通道可连同当前商户信息一起保存，空密钥保留；缺少公网 HTTPS 回调域名仅显示部署提示，真实充值下单仍严格检查。支付、客户售价和运行配置弹窗直接确认，自动记录操作说明，失败显示在弹窗内。已在本地浏览器验证保存 ZPay 默认通道成功，没有创建支付订单或调用外部网关。用户当前配置页面和持久化数据保留。

完整静态门及前端 **1390 passed**；后端四片 **1961 passed / 1 skipped**，官方清单覆盖全部 93 个测试文件。0、3 片在中断前完成；后端代码内容核对未变后补跑 1、2 片，均独立 PostgreSQL 和锁文件，中断日志未算作通过。最终日志与代码审查详见 [任务证据](evidence/LOCAL-JOINT-20260913.md) 和 [管理端前后端分析](evidence/LOCAL-JOINT-20260913-ADMIN-REVIEW.md)。

本轮实现提交 `c06dcde`；无新增迁移，api.ts / api.admin.ts 保持不变。仪表盘与经营分析核算来源不一致、八个旧入口模块、列表聚合范围和 CSS 叠加等后续事项已分级记录，不冒称全部清理。状态为本地 AUTOMATED_VERIFIED；远程发送仍待此前的明确授权，未推送、未开 PR、未合并。Owner 当前 Codex 任务，Reviewer 待 PR 独立评审。


### 报告建议第一阶段：核算读取统一（2026-09-13）

用户批准按管理端分析报告逐步修改，本阶段继续 LOCAL-JOINT 原任务与工作树。开工 fetch 后 origin/main 为 f401a0b，开放 PR 列表为空；PR #96 已合并，其他在制人物/视频链接任务不重叠。仪表盘复用经营分析读取层，显式保留历史未关联成本/SETTLE 覆盖和待核对状态；移除闲置设备/配对/激活码统计，成本待配置转向当前逐项价格表。api.admin.ts 仅更新返回类型，无新增迁移。详见 [分析报告](evidence/LOCAL-JOINT-20260913-ADMIN-REVIEW.md) 与 [阶段证据](evidence/LOCAL-JOINT-20260913.md#报告建议第一阶段核算读取统一)。

完整 Linux 静态门与前端 **1391 passed**；后端四片 **1964 passed / 1 skipped**，93 个测试文件全部覆盖。 实现本地提交 `d9c746e`，本阶段为本地 AUTOMATED_VERIFIED。此前远程推送的自动审批拒绝仍未解除，本阶段不推送、不创建 PR。P2 查询、旧模块退役、商户配置和 P3 样式仍为后续阶段。


## LOCAL-JOINT 授权环境同步 2026-09-14

用户明确授权本任务复用联调私有配置，保留管理端成果、账号和数据，同步并重启 5173 / 5174 / 8000 / Worker。
本地整合分支 `chore/local-joint-sync-20260914` 从 main `11c3de1` 创建，合并既有 LOCAL-JOINT `548aed7`；保留两侧提交，不向远程发布本地管理端成果。
代码自检通过：人物与素材分组、爆款双按钮、文案来源传递、媒体预检采用主线实现；管理端展示、支付路由、H3 账号池、历史账务待核对与新采集费用语义共同保留。
数据库新增 `20260914T0000_local_joint_merge`，只连接已存在的两个版本头，历史迁移原样保留。合并清单元组经过 JSON 后变为数组的问题已用失败回归锁定并修复；未登记合并和父节点篡改仍会失败。
验证：Linux 静态门及前端 1420 / 99 文件通过。完整后端 2037 passed、4 failed、1 既有 skipped；三项失败由测试快照缺少 Git HEAD 引起，一项为旧完全线性断言，修复后对应三个测试文件 35 passed。所有原始失败日志保留，不把首轮全量记成全绿。
原联调库私有备份恢复到无开放端口的本机专属 PG，升级演练核对 89 张原有表或视图的全部原有字段与记录完全一致。新的 Windows 专属运行环境按锁文件安装；API 模块及 curl-cffi / OpenCV 导入通过。新主线三门禁均为 success。
此条为切换前验收；实际切换、最终源码提交及数据一致性见工作区 `outputs/local-joint-sync-20260914/cutover.json`。本次不进行真实收费诊断、图片/视频生成、支付或公网发布。

## LOCAL-JOINT 工作树整理交付 2026-09-14

按用户的 worktree 整理与合并指令，从 `origin/main@11c3de1` 建立独立交付分支，迁入冻结的 99 文件净改动；运行中的联调目录与开放的 ASR PR #101 保持独立。独立评审发现并修复关闭订单晚回调时商户身份可能被提前更换的问题。最终前端 1420 passed，后端四片 2041 passed / 1 skipped，静态门及秘密扫描通过。完整边界与评审见 [任务证据](evidence/LOCAL-JOINT-20260913.md#2026-09-14-工作树整理交付)。

## FIX-R02-DELIVERY-20260914

支付回调原子性修复已从旧 worktree 迁到 `origin/main@281a828` 的独立交付分支。两渠道保存点回滚、订单锁、异步线程池及当前主线约束异常映射已通过专项 129 项、全仓静态门和后端 2038 项 / 1 项既有跳过；独立复审 PASS。变基到已合并联合交付的主线后将重新执行支付专项；未执行真实支付或生产账本操作，远程状态见[交付证据](evidence/FIX-R02-DELIVERY-20260914.md)。


## BUSINESS-REMEDIATION-20260914

独立分支 `fix/business-remediation-20260914` 从 main@06a5333 创建；Owner 当前 Codex 任务，Reviewer 代码自检及 PR 门禁。开工已核对远程 PR、分支、worktree 与共享 claim。范围为真实业务复测发现的上传、Hifly、Windows 音频、会话签名、场景/播放刷新及费用明细；端口 5199/5200/18099、独立 PG 与存储，不改原联调环境。前端 1429、专项后端 55、新增管理关联 1 项及静态检查通过；全量 PG 2094 passed / 1 既有 skip；收尾新增上限回归另验。分身、声音、口播成功；视频 9.08 秒完整播放，个人中心累计消费 167/余额 833 与后台一致。管理端已实际核对 23 条流水；生成记录单位与云存储启用入口已补修，85 项相关测试通过。已按授权补齐 CORS（预检 200）；总览及客户口播统计已实际验证 1 次/9.08 秒，PG 2095 passed / 1 skipped。云上传校验阶段连接重置，已修复脱敏 503 提示并通过 76 项相关回归，已定位缺少 verified-uploads/ 权限，用户授权补齐后云上传及自动分析成功；连接测试补齐五目录，40 项合同通过；云分析/首帧/H3 全流程尚未完成，不作整体验收通过。已授权远程推送及 PR，不合并部署。详见[整改清单](evidence/BUSINESS-REMEDIATION-20260914-CHECKLIST.md)及[复测证据](evidence/BUSINESS-REMEDIATION-20260914.md)。

## IMAGE-CONSENT-20260914

人物图片上传前声明、后端拒绝缺失授权及任务审计已实现。专项前端 33、后端 10 通过；真实组件浏览器验证未勾选禁用、取消后重新确认。本地完整静态门 1422 前端通过；PG 全量初次 2070 passed / 3 环境失败 / 1 既有 skip，补齐隔离 Git 元数据后对应 7 项复验通过，唯一覆盖 2073 passed / 1 skip；已按后续明确授权创建 [PR #106](https://github.com/peihr666-max/xiangshu-video-replica-/pull/106)，远程门禁待核验。详见[独立证据](evidence/IMAGE-CONSENT-20260914.md)。未部署到在用业务环境。

## FIX-R04-DELIVERY-20260914

赠送积分未知结果安全重试已从旧 R04 worktree 迁到 `origin/main@50059bc1` 的独立交付分支。待确认意图在 POST 前按管理员与客户隔离写入浏览器会话，写入失败时不发送；关闭弹窗、返回列表、页面重建或请求仍在途时卸载后，仍冻结完整参数并原样重放。首次明确 4xx 拒绝才释放意图，出现未知结果后的 4xx 仍保留原意图，直到成功；迟到响应按 key 与 attempt ID 条件清理，已卸载组件不回写父页面。本地前端 99 个文件、1424 项测试及 e2e lint 通过；未执行真实赠送、支付或生产账本操作。范围与验证见[交付证据](evidence/FIX-R04-DELIVERY-20260914.md)。



2026-09-14 20:52 BUSINESS-REMEDIATION-20260914 / R28：首帧延迟优化真实复测通过交付与结算，检查点接管后约 61 秒，2 张图片、原计费 2 积分；余额 827、冻结 0、累计 173，管理端与个人中心相符。质检失败/未完成仍标注待人工确认，H3 等剩余验收继续保留。代码与证据仍在同一 worktree 和 PR #108；相关专项 76、全静态前端 1439 通过。详见首帧 R28 证据。


R28 收尾：完整 PostgreSQL 四分片为 486 + 505 + 612 + 510 = 2113 passed / 1 既有 skipped。上一提交远程 Linux 门失败定位为 CharacterLibrary 场景图片异步签名尚未加载，测试提前同步断言；已改为等待实际图片出现，不放宽图片内容断言，人物库与首帧 56 项专项通过。完整静态门此前 1439 项通过，后续仅这项测试等待方式调整。新提交 CI 单独核验，不沿用旧提交失败或成功状态。


2026-09-14 23:10 BUSINESS-REMEDIATION-20260914 / R29—R33：场景形象独占外观、人工三图确认、异步回执恢复及 Data URI 兼容已实现；首帧画幅支持 9:16 等五种比例。真实任务 a8c8dcdb-a39e-4e25-82f9-1ef810d6377e 在约 62 秒交付 1008×1792 图片并完成前端确认。供应商回执成功/38 秒/0.060 ⚡；后台、管理流水、个人中心均实扣 2，可用 821/冻结 0/累计 179。当前前端 1443、隔离 PG 2142 passed/1 既有 skip，静态门完成。评审为执行者自检，PR #108 当前提交 CI 另验；H3、精选、下载落盘、历史 UNKNOWN 成本及人民币对账未完成，不合并部署。详见 R29—R33 主证据及整改清单。

2026-09-15 06:25 BUSINESS-REMEDIATION-20260914 / R34—R42：人工首帧确认与视频编译已衔接，H3 真实 4 秒任务成功（实际媒体 4.458 秒，40 积分），成片已保存至 COS 素材库并用于发布草稿。修复任务预览被轮询重置、归档入口缺失、素材复用名称陈旧、发布草稿恢复 404/缺少播放地址、详情显示 UUID、历史时间时区错误。管理端三页 43 条流水已逐页查看，余额 781/冻结 0/累计净消费 219；归档、重命名、分组、草稿及复制未新增生成费用。用户最新明确授权验收及 CI 通过后正常 PR squash 合并主分支，覆盖此前不合并授权；不部署、不真实充值、不对外发布。评审为执行者自检，未冒称独立评审。当前完整前端 1449 项通过，Ruff 与 mypy 147 文件通过，后端四个物理 PG 全量门正在执行；完整静态门剩余段和当前 PR CI 尚待核验。爆款 COS 两目录权限待确认、飞影与秘塔实际人民币账单待登录核对、H3 扩展模式及音频口播真实复测未完成，不能标全部业务验收通过。详见主证据及逐项清单。


2026-09-15 06:47 BUSINESS-REMEDIATION-20260914：R34—R45 本地完整门通过（前端 1450、PG 2155 passed/1 既有 skip），真实新版 Prompt 编译锁定后就绪 3/3，余额 781/冻结 0；管理估算成本标注已核对。当前提交评审/CI 与剩余权限、供应商核账及真实分支验收继续，未合并部署。详见任务独立证据。


2026-09-15 08:05 BUSINESS-REMEDIATION-20260914 / R46—R50：已授权删除备份 CAM 历史版本 1 并追加爆款两目录，当前版本 6 保留 2—5，七目录检测通过。修复口播归档目录后原 AUDIO 任务 24 秒成片完整播放，预扣 50/实扣 48/退回 2；四端余额 733、冻结 0、累计 267。飞影新增 217、累计 809 供应商积分，秘塔 46.5、Apilio 0.880704 算力单位已核对，现金换算保守预算另记。用户新增后台复杂表格重设计已实现，详情/搜索实际验证；修复手动精选遗漏普通目录、历史 local 恢复后迁移 COS、非生产客户预览地址。专项通过，当前全量门进行中；所有剩余流程验收及 PR 当前 CI 通过后才按用户授权正常 squash 合并，不部署、不充值、不对外发布。详见任务证据 R46—R50。

2026-09-15 BUSINESS-REMEDIATION-20260914 / R51：视频号详情补采接入管理页，按页进度、缓存/失败保留、幂等/只读控制、稳定大整数ID及专用45秒等待已实现。真实单条四字段、无重复调用及平台计量通过；150条分批补采与失败复测仍在进行。前端1453、最终四PG2175 passed/1原有skip，静态门通过。15秒H3四端预扣/结算150一致，余额583/冻结0/累计417。当前独立环境无采集专用worker且关键词为空，视频号自动归档待受控验收。详见证据R51，整体验收未完成，不合并部署。

## PUBLISH-INLINE-QR-20260916

个人中心三平台内嵌扫码增量，独立分支 `fix/publish-inline-qr-20260916`，基线 `f0b45b2`，Owner 当前 Codex；Reviewer 执行者自检及 PR 门禁。点击无账号的平台直接在当前面板加载二维码，桌面扫码窗口默认隐藏，成功后直接加入对应平台列表；已有账号平台仅切换列表。本地完整 Linux 静态门（1582 前端测试）及 PostgreSQL 2389 passed / 1 既有 skipped 通过，三平台实际取码和组件浏览器验证通过。实现提交 `d6564d7`，提交 Draft PR；未合并部署、未声称手机真实登录验收。详见[独立证据](evidence/PUBLISH-INLINE-QR-20260916.md)。

## 2026-09-16 UI-TYPOGRAPHY-BRAND-20260916

本地实现及独立评审完成，远程凭据不可用，未推送、合并或部署。范围：统一字号/用户名入口/透明图标/任务中心操作、文案工坊纵向流程、人物IP档案、自定义二创及1–5000文案字数、DeepSeek统一文本通道、复刻时长和数量明示。前端1594测试；后端全量2391通过/1既有skip/5个Git路径环境错误，5项修正环境后全部复验通过；最终新增7项专项通过，静态检查通过。无真实付费调用，测试Docker资源已清理。独立评审review_copy_ip的恢复隔离意见已修复。具体范围、限制及原始结果见 [独立证据](evidence/UI-TYPOGRAPHY-BRAND-20260916.md)。

2026-09-16 UI-TYPOGRAPHY-BRAND 后续评审修复：四项问题已处理（Logo生产资源、云端/本地任务恢复、总超时与有效租约、JSON修复统一DeepSeek），并补齐跨设备重试及终态失败清理。最终前端1600通过，相关后端148通过、最终租约/构建契约2项复验通过，静态检查及独立复审通过。新增DeepSeek内部成本科目隔离旧Apilio费率，不向用户新增收费；新分析须配置Apilio与DeepSeek。仍未远程推送/合并/部署，正式安装包与付费5000字验收未做。详见 [修复复验记录](evidence/ui-typography-brand/review-fixes.md)。

## 2026-09-16 PUBLISH-INLINE-QR-20260916

个人中心三平台内嵌扫码增量，独立分支 `fix/publish-inline-qr-20260916`，基线 `f0b45b2`，Owner 当前 Codex；Reviewer 执行者自检及 PR 门禁。点击无账号的平台直接在当前面板加载二维码，桌面扫码窗口默认隐藏，成功后直接加入对应平台列表；已有账号平台仅切换列表。本地完整 Linux 静态门（1582 前端测试）及 PostgreSQL 2389 passed / 1 既有 skipped 通过，三平台实际取码和组件浏览器验证通过。实现提交 `d6564d7`，提交 Draft PR；未合并部署、未声称手机真实登录验收。详见[独立证据](evidence/PUBLISH-INLINE-QR-20260916.md)。

## 2026-09-16 PROMPT-OPTIMIZE-20260916 · 证据登记

用户已授权 Codex 接手 Claude 会话 fbeae78e 的既有独立 worktree/分支，原进程已停止且成果已备份；不重复开发。H01—H14 实现与代码自检完成；本地静态门和后端全量后的全部失败项/新增专项通过，H15 已提交 [PR #125](https://github.com/peihr666-max/xiangshu-video-replica-/pull/125) 并触发 CI，当前为 REVIEW 交付状态。改动范围、规格映射、评审与测试见 [H3 实施证据](evidence/PROMPT-OPTIMIZE-20260916-EVIDENCE.md)。AUTOMATED_VERIFIED（本地）；远程三门禁以 PR 当前 head 的 Checks 为准，未宣称独立评审、真实链路、合并或部署完成。

## CREATION-WORKFLOW-UI-20260916 · 创作界面与复刻准备流程

用户已确认“一起修改实际页面”。Owner 为 Codex 会话 01a0aa65；Reviewer 为执行者自检及待进行的 PR 评审。独立分支 `feat/creation-workflow-ui-20260916`，基线 `ba616f46`，锁定 worktree `.worktrees/CREATION-WORKFLOW-UI-20260916`，已检查共享 claim、本地/远程分支及在制 PR；复用已合并 #125，不重复实现优化器，不修改 #126 口播范围。实现顶层导航合并、去面包屑、横向参数与加号上传、单框文案改写、提示词/首帧双素材交接及场景替换设置。前端完整 1621 项和最终专项 241 项、静态门已通过；后端全量 2432 通过、1 既有跳过；唯一脚本执行权限环境失败恢复后补验 1 通过。AUTOMATED_VERIFIED（本地），已提交 [Draft PR #128](https://github.com/peihr666-max/xiangshu-video-replica-/pull/128)，状态 REVIEW；未合并/部署。测试独立 PostgreSQL 与质量容器均使用 --rm，已清理且临时卷无残留；本地预览 5217。详见[独立证据](evidence/CREATION-WORKFLOW-UI-20260916.md)。

2026-09-16 布局续改：文/图生视频按新参考图改为左侧首尾帧/描述/横向参数、右侧整列预览、底部操作栏；参考页维持已有布局。前端 210 项经旧布局断言更新后全部通过，桌面/手机视觉检查通过，仍随 PR #128 待评审。详见独立证据与 design-qa.md。

2026-09-17 参考页续改：用户要求参考生视频与图生视频统一，现已共用左侧素材/描述/参数、右侧大预览及底部操作栏；多素材选择、预览切换与大加号上传保留。前端 204 项、类型及样式检查通过，桌面/手机复核通过，继续交付 PR #128。

2026-09-17 画幅续改：新草稿默认 9:16；图片占位、首尾帧、参考缩略图与预览按生成参数所选比例显示，完整呈现图片。完整前端 106 文件 / 1631 项、TypeScript 与修改文件 Biome 通过，桌面多画幅和手机检查通过；详见 CREATION-WORKFLOW-UI-20260916 独立证据与 design-qa.md。继续随 PR #128 待评审，未合并/部署。

2026-09-17 最终画幅澄清：用户明确按上传图片原始比例显示，横图自动变横框、空占位默认 9:16；替代上一条生成参数控制展示框的规则。最终前端 106 文件 / 1634 项，收尾专项 107 项及 TypeScript / Biome 通过，桌面和手机验证通过。仍随 PR #128 待评审，未合并/部署。

## 2026-09-16 ORAL-VIDEO-TTS-20260916

视频口播流程与独立声音档案改造；局部 AUTOMATED_VERIFIED，未调用付费飞影链路、未合并部署。前端 1565 passed，口播/素材专项 205 passed，设计 QA passed；全量 PG 及远程提交状态见 [任务证据](evidence/ORAL-VIDEO-TTS-20260916.md)。基线 f0b45b2、分支 feat/oral-video-tts-20260916。仅本任务增量登记，不核销其他业务工作包。


## 2026-09-17 REPLICA-FINAL-PROMPT-20260917

REVIEW / AUTOMATED_VERIFIED（本地）：最终提示词后置、确认文案绑定、首帧三图人工选择及文生/参考生视频链路核验。Owner Codex / 01a0aae7-8864-7c41-a255-4c8f62adc5e3，Reviewer 执行者自检与 PR 门禁；不声称独立代理评审。独立分支 fix/replica-final-prompt-20260917，从 origin/main@d95a8db4 创建 .worktrees/REPLICA-FINAL-PROMPT-20260917；共享 claim 已认领。没有执行真实付费出片、生产功能开关或部署。本地完整范围及失败项补验已通过，已合入 main 的 #128 页面增量；待 PR 当前 head 三门禁与评审，未合并部署。[详细证据](evidence/REPLICA-FINAL-PROMPT-20260917.md)。

## 2026-09-17 PUBLISH-DELIVERY-20260917

发布链路第二阶段 PR-A。新增迁移 `20260917T1000_publish_records`（`publish_records` 表 + `publish_browser_accounts.status/error_message/source`）、`app/publish_records.py` / `publish_record_routes.py` / `publish_credentials.py` / `publish_delivery.py`，`publish_worker` 每轮追加一条发布投递；桌面 `publish_accounts.rs` connected 时导出 storage_state 并由前端 `POST /publish/browser/accounts/import` 加密入库；发布页立即/定时发布、`PublishRecordsPanel`、首页真实 `published_total`；compose/systemd/基础镜像（Node.js）接入 `worker-publish`。本地 ruff/format/mypy、cargo test 27、client check 105 文件 1617 passed，专项与迁移矩阵见证据；全量 PostgreSQL 四分片 2452 passed / 34 failed / 1 skipped，34 项均为 Windows 本机既有失败（对照未改动主检出复现），详见证据末段。AUTOMATED_VERIFIED；全部用例合成凭据与假投递器，未触网、未真实发布、未合并部署。详见[独立证据](evidence/PUBLISH-DELIVERY-20260917.md)。

## WORKSPACE-STRUCTURE-20260917 目录与导航维护

2026-09-17：按用户确认完成设计资料、历史工具发行包和开发导航整理。1693 个既有文件全部保留、168 个资料文件迁移，825 个既有链接目标保留，21 项图片校验通过；V1.3 图册 28 页及 V1.4 测试 7/7 通过，秘密检查通过。业务源码与配置保持原合同；此为资料维护验证，不改变业务及生产验收等级。详见[独立证据](evidence/WORKSPACE-STRUCTURE-20260917.md)。

## 2026-09-17 PUBLISH-LOGIN-RECOVERY-20260917

Mac 桌面复用已有认证云端扫码及加密保存；Windows 读取失败暂停后，打开官方窗口恢复当前会话检测。后续用户反馈抖音手机确认仍不保存，追加抖音成功码兼容、迟到失败不覆盖、同源后台主动身份核验及取消清理回归。各缺陷先红后绿，完整静态门通过（前端 1646 项），最终账号相关 103 项、后端全量 2508 passed / 1 既有 skip、两轮独立复审通过，AUTOMATED_VERIFIED。同版 Mac 视频号添加成功由用户实测；未执行真实发布，抖音服务端修复尚未部署；[独立证据](evidence/PUBLISH-LOGIN-RECOVERY-20260917.md)。

## 2026-09-17 REPLICA-SIMPLE-FLOW-20260917

REVIEW_READY / 本地自动化通过，待 PR 评审及合并。Owner Codex 会话 `01a0ad07-554c-7a13-b90b-309b3aaef984`；Reviewer 为独立代理交叉复核和主执行者整合自检。基线 `origin/main@3932d528`，分支 `feat/replica-simple-flow-20260917`，独立 worktree `.worktrees/REPLICA-SIMPLE-FLOW-20260917`；开工已 fetch 并核对远程分支、开放 PR、工作树和共享 claim。范围：Studio 三步复刻、文案后置、分镜编辑整合、单场景参考、去字幕而保留实体文字、切镜候选、MiniMax 结构和中文默认作品名。隔离预览 5227、专项 PG 5547、四片 PG 5551—5554。不核销旧任务验收；未付费调用、合并或部署。详见[独立证据](evidence/REPLICA-SIMPLE-FLOW-20260917.md)。


## BRAND-IDENTITY-20260917 品牌展示维护

放大工作台与创作页品牌，个人中心共用金色 Logo，桌面图标加入公司及软件名两排。基线 `2f3815e0` 的完整静态门通过（前端 1675 项）；PG 和 PR 结果见[独立证据](evidence/BRAND-IDENTITY-20260917.md)。不改变客户业务验收等级；未合并部署或更新已安装客户端。


## REPLICA-SUBJECT-LAYOUT-20260917 · 截图反馈维护增量

多人首帧只替换主要人物，移除旧人数门禁，继续人工三图选择；拆解预览按原画幅调整左右区域，所有页面复用紧凑侧栏，创作内容区不重复显示 Logo。Owner Codex 当前会话；独立 worktree / 分支从 `41fc171d` main 创建。已通过本地完整静态门、前端 1680 项、PG 2548 项（1 既有跳过）及九组合浏览器布局检查；远程 CI 随 PR 核验。无生产调用、合并或部署。此段登记 §12 工作包 / §18 维护任务增量，不核销其他任务状态。详见[任务证据](evidence/REPLICA-SUBJECT-LAYOUT-20260917.md)。


同会话追加：文案/提示词输入框翻倍；克隆声音支持常见音频和WMV抽音轨并修复结果跳动；首尾帧改大尺寸加号卡片；参考素材改上添加、下展示并可单击预览。最终本地前端1716通过、PG分片2580通过/1既有skip，评审后PG专项39通过；仅人工三图选择，不恢复图片AI质检。PR #139 继续承载该批反馈，未合并部署。完整证据见上述同名文件。


## PROD-DEPLOY-20260917 现有生产环境版本升级

`main@80fee758` 主线三门禁全部通过；2026-09-17 通过用户已授权的宝塔服务器完成生产发布，脚本 SUCCESS / exit 0，双 API、六个 Worker 和两套静态制品版本核验通过，数据库 head 保持不变。备份校验、生产 true、PG TLS verify-full、公网与逐实例就绪、未认证管理 API 拒绝均已复验。无源码变更、无真实付费验收；不提升完整 PRODUCTION_GO。见[完整部署证据](evidence/PROD-DEPLOY-20260917.md)。

PROD-DEPLOY 补充：既有维护任务缺少 scripts 模块的问题已通过同版四脚本只读挂载和 systemd drop-in 修复；只读演练、正式执行及 timer 均复验成功。未改业务源码，详见同一部署证据。

## RELEASE-FEEDBACK-20260917 · 生产升级记录

用户授权通过宝塔终端以 Git 拉取方式部署。已将服务端与客户/管理网页从 `80fee75` 升级至已合并 PR #139 的 `9ec6f566`；精确 main 三门禁成功。发布状态 SUCCESS、外层退出码 0，八个应用容器版本一致，双 API ready、公网与资源、四种合成音频转码、三份备份校验通过；任务写冻结已解除，数据库 head 未变。5 条历史首帧待核对记录无在途任务或有效租约，未修改。旧镜像及备份保留；未更新已安装桌面端，未执行付费链路或账号修改，不提高整体生产验收等级。见[生产部署证据](evidence/RELEASE-FEEDBACK-20260917.md)。本条同时更新 §12 / §18 维护任务状态；旧条目保留作为历史快照。

## SIDEBAR-TOGGLE-20260917 · 工作台侧边栏折叠/展开开关

桌面端左侧栏新增折叠/展开开关：顶栏按钮（≥801px 显示，≤800px 保持抽屉）、localStorage 记忆、默认展开；原 88px 图标栏样式由"按屏宽强制"改为 class 驱动收起态。Owner Claude 当前会话；基线 `origin/main@0ce6ed28`，分支 `feat/sidebar-toggle-20260917`，独立 worktree `.worktrees/SIDEBAR-TOGGLE-20260917`。测试先行（先红后绿），StudioWorkspace 100 项、前端全量 1719 项通过；完整门禁与 PR 结果见[独立证据](evidence/SIDEBAR-TOGGLE-20260917.md)。不核销其他任务状态；未合并部署。
## LINK-FORMAT-20260917 · 抖音/小红书链接入口全格式支持

服务端链接规范化：裸域名分享文本提取、短链 302 还原（SSRF 防护、3 跳上限、失败降级原链直传）、任意入口形态提取内容 ID 重写网关已知稳定形态（抖音 `/jingxuan?modal_id=`、小红书 `/explore/{id}` 保留 xsec_token）、网关 ID 校验放宽 15–22 位；计费与路由口径不变。用户明确不做真实付费探针，29 项离线表驱动测试先红后绿，既有链接测试全部保持通过。Owner Claude 当前会话；基线 `origin/main@0ce6ed28`，分支 `feat/link-format-canonical-20260917`，独立 worktree `.worktrees/LINK-FORMAT-20260917`。本地最终门禁一次通过：静态门 + 四分片 2609 passed / 1 既有 skipped；PR 与 CI 结果见[独立证据](evidence/LINK-FORMAT-20260917.md)。未合并部署。

## RECHARGE-PACKAGES-20260922 · 管理员可配置充值套餐与消费折扣权益

充值档位不再写死，管理员可配置套餐（金额/赠送积分/折扣接口与折扣率/启停排序），支持充值赠送、视频生成折扣与无优惠三种形态；客户按套餐下单冻结快照，结算按快照授予折扣并接入报价/计费/流水；低于生效起充的套餐下单返回 422，授予走用户级 advisory lock，账目按生效折扣归因。Owner Qoder session 代 honor.pei；基线 `origin/main@8e9e69be`，分支 `feat/recharge-packages-20260922`，独立 worktree `.worktrees/RECHARGE-PACKAGES-20260922`。测试先行（先红后绿），评审 H-1/M-1/M-2/L-1/L-2/L-3 逐条修复；两轮本地门禁全绿（静态门 vitest 1770、四分片 2922 passed / 1 既有 skip、客户 E2E 修复后全套 6 passed），AUTOMATED_VERIFIED。详见[独立证据](evidence/RECHARGE-PACKAGES-20260922.md)。不核销其他任务状态；未合并部署。
