# FIVE-VIEW-2K — 五视图真实感：2K 尺寸落地与提示词改写验证

## Task Information

| 项 | 值 |
|---|---|
| 分支 | `viral-biz-2k-photo-realism-20260922` |
| 基线 | `02ac56a4`（= 当时的 `origin/main` HEAD，分支零落后） |
| worktree | `.worktrees/viral-biz-2k-photo-realism-20260922` |
| 本任务提交 | `bbbffba1`（初版 2K，实为空操作）、`15de0eb9`、`62dd9da3`、`3dbee0cf`（提示词改写与测试）、`148edf6c`、`18438a20`（本次修复） |
| 计划文档 | `docs/superpowers/plans/2026-09-22-five-view-2k-photo-realism.md`（Task 1-5；**当前未纳入版本管理**，仅存在于本 worktree） |
| 付费 Provider 授权 | 用户明确指示「帮我测试是否能支持 2K」「测试 2K 竖屏尺寸」「按优先级补充完整」「测试 nano 尺寸比例问题」；合计**约 24 张成功出图 + 1 次网关侧任务失败 + 1~2 次因探针网络中断未取回（可能已计费）**，费用请据此核对 |
| 证据层级 | `REAL_CHAIN_VERIFIED`（真实 apilio 请求、真实出图、真实像素测量）；**未达** `PRODUCTION_GO`（未上生产链路复验） |

## 需求 → 交付对照（计划 Task 1-5）

| Task | 内容 | 本次交付后状态 |
|---|---|---|
| 1 | 强制 2K 输出 | ✅ 首帧链路 `148edf6c`、五视图链路 `18438a20`，10 档尺寸全部实测命中 |
| 2 | 重写基础五视图提示词 | ✅ `15de0eb9`（本次未改动） |
| 3 | 重写场景五视图提示词 | ✅ `62dd9da3`（本次未改动） |
| 4 | 更新测试断言 | ✅ `3dbee0cf` + 本次新增三道护栏 |
| 5 | 端到端集成验证 | ✅ 本文件「实测证据」§3-§4 |

## 实测证据

### 1. `image_size` 是空操作（推翻初版实现的前提）

同源图、同参数、真实请求 `POST https://api.apilio.ai/v1/images/edits`：

| 变体 | 发送字段 | 实际出图 |
|---|---|---|
| A | `size=1792x1008` | 1792x1008 |
| B | `image_size=2K` + `size=1792x1008` | **1792x1008**（与 A 逐像素同尺寸） |
| E | `size=2048x1152` | **2048x1152** |

即 `image_size` 不生效也不报错（提交 `bbbffba1` 担心的 HTTP 400 未发生）——最坏的失败模式：静默无效果。尺寸必须写进协议字段 `size`。

### 2. 2K 档位表 10 档全部命中

逐档真实出图并核对像素与 `%16==0`，**零不符、零遗漏**：

9:16→1152x2048、16:9→2048x1152、1:1→2048x2048、3:4→1536x2048、4:3→2048x1536、2:3→1360x2048、3:2→2048x1360、4:5→1632x2048、5:4→2048x1632、21:9→2048x880。

（注：初稿提案中 4:5 与 5:4 曾写作 `2040` 高度，2040 不能被 16 整除、违反网关约束，已修正并由新增的约束测试兜住。）

### 3. 五视图链路此前完全没拿到 2K

`simple_character._generate_contact_sheet_content` 调 `provider.edit` 时**从未传 aspect_ratio**（该文件 `aspect_ratio` 出现次数为 0），于是走 `size=auto`。单格宽按整列白度检测：

| 配置 | 整图 | 三个全身格 | 右侧近景列 | 全身格是否达标(500-700px) |
|---|---|---|---|---|
| gpt-image-2 `size=auto`（修复前真实行为） | 1815x866 | 最窄 405px | 599px | ✗ |
| gpt-image-2 16:9 档 2048x1152 | 2048x1152 | 最窄 386px | 821px | ✗（比 auto 还窄） |
| gpt-image-2 **2560x1440**（本次选定） | 2560x1440 | **548~573px** | 813px | **✓** |

机制：模型把约 815px 固定留给近景列，剩余宽度才分给三个全身格。整图不够宽时，多出的宽度优先喂近景列，全身格反而变窄——所以「请求 2K」在 2048 档下不仅没帮到全身格，还略有倒退。只有 2560x1440 落进验收区间，且未越网关实验档位（上限即 2560x1440）。

### 4. 提示词与尺寸的贡献已隔离

| 对照 | 提示词 | 整图 | 面部原生像素 | 备注 |
|---|---|---|---|---|
| 真·改动前 | 旧 | 1672x941 | 314x328 = 10.3 万 | |
| 只换提示词 | 新 | 1815x866 | 341x302 = 10.3 万 | 同时长下新提示词的毛毡纤维、发丝分缕、耳环细节明显更清晰 |
| 只换尺寸 | 旧 | 2048x1152 | — | |
| 改动后 | 新 | 2560x1440 | **480x501 = 24.0 万** | 面部像素 **2.34×**，命中计划「人脸面积约翻倍」 |

两张对照图的人工判读：新提示词 + 2560x1440 的出图在贝雷帽毛毡纤维、发丝分缕、耳环蕾丝纹样上细节显著优于旧版；**但皮肤质感仍偏干净**，不能据此宣称「塑料感已消除」。严格的观感结论需由人工在同等分辨率下盲评。

### 5. nano-banana-pro-2k 旧行为给横版五视图发的是方图比例（静默的排版畸变）

| 变体 | 发送 | 实际出图 | 三个全身格宽 |
|---|---|---|---|
| M（复现旧行为） | `aspect_ratio` 由源图推得 | **2048x2048（方图）** | **548 / 454 / 358（严重不等）** |
| N（修复后显式声明） | `aspect_ratio=16:9` | **2752x1536（横版）** | **685 / 684 / 685（等宽）** |

用应用自身的 `image_aspect_ratio()` 对该源图（1077x1077 方图）推算，结果**正是 `1:1`**——即旧代码给这张源图发的就是 1:1，横版五视图被塞进方形画布。

**关键点：方图那版能通过应用自己的五视图裁剪校验**（`_require_contact_sheet_views` 裁出全部 5 格，无缺失）。原因是校验只要求「能切出 5 格且每格 ≥16px」，拦不住排版畸变。肉眼复核确认方图版三个全身格宽度与人物比例/位置均不一致，与提示词要求的「等宽、同机位、同比例、头脚对齐」相冲；横版版则三格近乎完全等宽、人物对齐。属同一类「静默出坏图」模式——不报错、不拦截、直接发布。

附带结论：nano 声明 16:9 后全身格 685px，**落在计划 500-700px 验收区间内**，且优于 gpt-image-2 选定档位（2560x1440 实测 548~573px）。

**nano 对各比例的保真度**（显式声明后逐档实测，出图约 4.2MP 固定像素预算、形状随比例）：

| 发送 `aspect_ratio` | 实际出图 |
|---|---|
| 1:1（旧行为按源图推得） | 2048x2048 |
| 16:9 | 2752x1536 |
| 9:16 | 1536x2752 |
| 3:4 | 1792x2400 |

**`image_size=2K` 对 nano 同样不起作用（就输出尺寸而言）**：同发 `aspect_ratio=16:9`，带与不带该字段出图均为 2752x1536。**范围界定**：本结论仅证明输出尺寸不变；该字段是否让网关「先按 2K 渲染再降采样」从而在不改尺寸的前提下影响画质，单样本随机生成无法排除，故未改动 `else` 分支的该行（详见遗留项 1）。

### 6. 偶发失败记录（非确定性）

旧提示词 + 2048x1152 首次请求返回网关 `FAILURE`（13s），**同参数重试成功**（2048x1152）。属网关侧偶发，非本改动引入；应用侧既有轮询/失败路径已覆盖，本次未改动该路径。

## Exit-Gate Verification

| 检查 | 命令 | 结果 |
|---|---|---|
| ruff lint | `ruff check`（4 个改动文件） | All checks passed |
| ruff 格式 | `ruff format --check`（同上） | 4 files already formatted |
| 类型 | `mypy app` | Success: no issues found in 175 source files |
| 专项测试 | `pytest tests/test_apilio_image_provider.py tests/test_character_five_view_contract.py -q` | **86 passed** |
| 相邻专项 | `pytest tests/test_net_safety.py tests/test_business_preflight.py -q` | 16 passed |
| 探针自检 | 与生产 `build_apilio_edit_multipart` 字段序列逐字段比对 | 通过（序列已为 `… n, size`，无 image_size） |

**未跑**：`npm run check:static` 与 `bash scripts/ci/run-pytest-shards.sh`（AGENTS.md 本地门禁前置原则要求的全量门禁）。本机**无 `cargo`**，`check:tauri` 无法本地执行，属已登记的环境缺口。**不得**把上述专项结果记作全量门禁通过。

**已知环境噪音（与本次改动无关）**：`tests/test_admin_first_frame_reconcile.py` 收集期报 `No module named 'fcntl'`（Windows 无此 Unix 模块）。

## Files Changed

| 文件 | 改动 |
|---|---|
| `server/app/first_frames.py` | 尺寸档位提为模块级 `FIRST_FRAME_IMAGE_SIZES`（10 档 2K）；gpt-image-2 分支删除空操作的 `image_size`；`ImageProvider` 协议补 `aspect_ratio` / `size_override`（原先协议未声明 aspect_ratio，调用方一传即被 mypy 拦）；`build_apilio_edit_multipart` / `submit_edit` / `edit` / `FakeImageProvider` 打通 `size_override` |
| `server/app/simple_character.py` | 新增 `SIMPLE_CONTACT_SHEET_ASPECT_RATIO=16:9` 并在 `provider.edit` 传参；新增 `size_override` 通路与 `CONTACT_SHEET_SIZE=2560x1440`；新增清晰度下限 `CONTACT_SHEET_MIN_SHEET_WIDTH=1600` 及 `_require_contact_sheet_resolution` |
| `server/tests/test_apilio_image_provider.py` | 更新档位期望值；新增「请求体不得含 image_size」回归护栏、10 档网关约束校验、`size_override` 契约测试 |
| `server/tests/test_character_five_view_contract.py` | 新增整图宽度下限测试（按 3x500+815 推导）、清晰度下限拒绝测试 |

**未跟踪的验证工具**（未纳入提交，如需入 PR 可移入 `server/scripts/`）：`probe_apilio_2k.py`（探针，含与生产逐字段一致性自检，`--model` / `--legacy-prompt` / `--custom-size` / `--variants` 可选）、`legacy_five_view_prompts.py`（改写前提示词存档，可从 `git show 02ac56a4:server/app/simple_character.py::SIMPLE_CONTACT_SHEET_PROMPT` 复现）。

## Regression

- **nano-banana-pro-2k 五视图出图会变**：`aspect_ratio` 由「按源图推」改为显式 16:9，出图由方图变为横版，属修复而非回归（见证据 §5），但**会改变存量对比基线**。
- **五视图出图变大**：`size` 由 `auto`（实测 1672~1815 宽）变为显式 2560x1440，按像素外推计费成本上升。
- **新增失败路径**：五视图整图宽度 < 1600px 时返回 502 `CONTACT_SHEET_RESOLUTION_TOO_LOW`。阈值 1600 低于实测的既有行为下界（1672），故不误伤现有出图；本地占位图 2240x1400 亦在阈值之上。
- **首帧 16:9 档位**：仅在 `size_override` 缺省时生效，五视图已改走 override，首帧行为不变（2048x1152）。

## 遗留项（未纳入本次）

1. `nano-banana-pro-2k` 分支的 `image_size=2K`（`first_frames.py` else 分支）**已实测不影响输出尺寸**（同发 `aspect_ratio=16:9`，带与不带均为 2752x1536），但 `test_nano_banana_edit_uses_source_ratio_and_2k_defaults` 仍断言该字段存在。**未删除的理由**：只证明尺寸不变，无法排除「先按 2K 渲染再降采样」的潜在画质影响，而单样本随机生成无法区分；删除它等于替用户改 main 上的供应商契约行为。**建议**：若确认无画质影响，应删除该行并同步修正那个测试的命名与断言。
2. 局部观感结论（毛孔级肤质）需人工盲评。
3. 未跑全量本地门禁（见上）。
4. 未推送、未开 PR；本分支未按 `feat/customer-v3-cwNNN-` 命名，无 CW/T 任务号。

## Section 14 Ledger Record

- 任务号：**未分配**（本分支未按 CW/T 任务命名，需用户指定后方可登记）
- 状态：`CODE_PRESENT` + `AUTOMATED_VERIFIED` + `REAL_CHAIN_VERIFIED`（真实付费 Provider 链路的尺寸与出图已实测）
- 未达：`PRODUCTION_GO`
- 待回填：`docs/客户版任务清单-V3.md` §12、`docs/CUSTOMER-TASK-EVIDENCE-V3.md`
