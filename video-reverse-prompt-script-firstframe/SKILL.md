---
name: video-reverse-prompt-script-firstframe
description: 输入一个参考视频，自动拆解完整分镜、人物与摄影机运动、口播/画外音和时间轴，使用 Codex 宿主内置 ImageGen 生成并自动审查干净首帧，最终只交付 first_frame.png 与 image_to_video_prompt.md。用于短视频复刻、视频拍法反向工程、参考视频转图生视频提示词、首帧重建和三层乡村别墅视觉升级。
---

# 短视频复刻 Skill V0.6

> 版本说明：`V0.6` 是本 Skill 的**交付契约版本**，决定分镜 JSON 结构、`SCHEMA_VERSION`、双文件交付物与 Release Gate 校验。外层一键安装包版本为 V0.6.4，仅负责 Windows/Linux/macOS 环境引导与安装（WinGet 装 Python/FFmpeg），**不改变本契约**。两者是不同层级的版本，发布版本变化不代表交付契约升级；历史分析 JSON 的 `version` 字段仍必须为 `0.6`。

## 唯一产品目标

用户只需提供一个视频文件。自动完成：

`参考视频 → 时序拆解 → 口播策略 → 首帧生成 → 多模态视觉 QA → 图生视频 Prompt → Release Gate`

成功运行时，`outputs/` 必须严格只有 `first_frame.png` 和 `image_to_video_prompt.md`。所有元数据、抽帧、分镜、ASR、分析 JSON、ImageGen 请求、候选图、QA 和 Gate 报告只能进入同级 `internal/debug/`。

## 硬边界

- 唯一必需输入是一个视频。人物参考图、自定义口播、主题都是可选项；缺失时必须自动继续。
- 首帧必须由 Codex/宿主内置 ImageGen 生成，不调用独立 Image API，不读取 `OPENAI_API_KEY`。
- 首帧 QA 必须由宿主多模态能力直接看图完成；Python 只校验 JSON 与执行 Gate。
- QA 不通过必须重新生成，最多 3 次。不得裁剪报告图、拼图或信息图来"抢救"首帧。
- 任何可读文字都是首帧硬失败：字幕、水印、Logo、平台 UI、招牌、门牌、广告、春联、灯笼文字、衣服文字、包装文字和乱码。
- `NOT_RUN`、`UNKNOWN`、缺字段或含糊判断一律不算 PASS。
- 人物动作必须从参考视频连续帧动态提取，不得固定为走路、抬手、站立或任何样例动作；原片有动作不能降级为静止，原片静止也不能擅自增加动作。
- 首帧方向必须与源视频一致（竖屏源出竖屏，横屏源出横屏，方形源出方形）。
- 最终视频生成不属于本 Skill；成功边界止于两个交付物。

## 执行流程

下方命令以 `python3` 为例；Windows 若仅有 Python Launcher，等价使用 `py -3`。

### 1. 建立隔离运行目录

在当前工作区新建 `video_reverse_runs/<video_stem>_<timestamp>/`，含 `outputs/` 与 `internal/debug/`。不要复用已有运行目录——脚本会拒绝覆盖已分析的 `run_manifest.json`。

```bash
python3 scripts/preprocess_video.py <video> <run_dir>
```

一条命令完成全部证据准备：源首帧、镜头切点、按镜头分组的时序抽帧、抽帧总览图、视频元数据和音轨。产物中最重要的是 `internal/debug/shot_segments.json`——它已经把每一帧归属到具体镜头并标好 `head`/`mid`/`tail` 与真实时间戳。FFmpeg 报错时记录在 `internal/debug/` 后停止，不得伪造分析。

### 2. 用宿主多模态能力做完整视频拆解

先读 `shot_segments.json` 建立镜头结构，再看 `contact_sheet.jpg` 一眼掌握全片节奏，然后**只对人物出镜的镜头逐张细看** `sample_frames/` 里对应的帧。最后结合 `source_first_frame.png` 与 `video_metadata.json` 确认首帧场景。不要不分主次地平铺全部帧，也不要仅凭"视频主题"推断动作。

人物出镜镜头必须先建立**连续动作证据**：至少查看两个不同时间点，优先取镜头首、中、尾三处；动作不清楚时继续增加相邻帧，不得默认判为静止。必须记录动作起始、移动路径、身体力学、左右手分别做什么、分阶段动作和动作结束。

`shot_segments.json` 里每帧的 `timestamp` 就是可直接写入 `motion_observation.evidence_timestamps` 的真实时间，不要自己估算时间点。注意 `contact_sheet.jpg` 只是分析用的总览图，与"首帧严禁拼图"的交付约束无关。

把结果写入 `internal/debug/video_analysis.json`。**完整字段、取值枚举和填写规则见 [references/analysis-schema.md](references/analysis-schema.md)，可直接复制 [templates/video_analysis.json](templates/video_analysis.json) 作为骨架。**

人物镜头的 `subject_motion` 不是事实源；`compose_prompt.py` 从结构化 `motion_observation` 自动生成动作描述，B-roll 才使用 `subject_motion`。人物出镜却缺少连续动作证据时，Prompt 构建会直接失败——不得用"轻微点头""自然走动"等通用句子补齐。

### 3. 自动决定口播

默认 `AUTO`：

1. 本轮明确提供自定义口播 → `CUSTOM`。
2. 否则尝试从真实音轨获得可靠口播；画面字幕只能辅助校验，不能冒充音频转写。
3. 宿主/本地无法可靠转写 → 自动选 `AI`，根据视频主题、总时长和人物/B-roll 区间生成适配文案，不要求用户补充输入。

人物可见且讲话用 `ON_CAMERA_SPEECH` 并要求口型同步；B-roll 用同一声音的 `VOICE_OVER`；没有台词用 `NO_SPEECH`。更新 `run_manifest.json` 的 `shots`、`spoken_segments` 与 `three_storey_rural_villa`；`spoken_segments` 的文字必须逐字出现在最终 Prompt。

### 4. 生成图生视频 Prompt 候选

```bash
python3 scripts/compose_prompt.py \
  <run_dir>/internal/debug/video_analysis.json \
  <run_dir>/internal/debug/image_to_video_prompt.candidate.md
```

复核每个时间段都包含场景、人物动作、摄影机运动、相对运动、口型同步/画外音和原文，且动作来源明确写为参考视频连续帧。**完整约束见 [references/prompt-contract.md](references/prompt-contract.md)。**

### 5. 生成 ImageGen 请求

```bash
python3 scripts/build_imagegen_request.py \
  <run_dir>/internal/debug/video_analysis.json \
  <run_dir>/internal/debug/imagegen_request_attempt_01.json \
  --metadata <run_dir>/internal/debug/video_metadata.json \
  --attempt 1
```

`--metadata` 是必需的：脚本据此判定 `orientation`（PORTRAIT / LANDSCAPE / SQUARE）并写入请求的画幅约束，与 Release Gate 用同一个分类器。**横屏源视频必须输出横构图**，不要按短视频惯例默认出竖图。

检测到三层乡村别墅时，先读 [references/upscale-rural-villa.md](references/upscale-rural-villa.md)，并把 `three_storey_rural_villa` 设为 `true`。

### 6. 调用宿主 ImageGen

读取请求 JSON 的 `prompt`，直接调用当前宿主的图片生成/编辑工具。在 Codex App 中使用宿主内置 ImageGen，不要运行外部 API 脚本。请求里的 `orientation` 字段是硬要求——若宿主支持指定输出尺寸，必须选用与之匹配的画幅。

图片输入顺序：`internal/debug/source_first_frame.png` 必须是第一视觉参考；用户提供的人物参考图按序追加，且只能决定"谁在画面里"；没有人物参考图则不追加任何图片，按源视频角色类型/姿态/服饰/机位继续生成。

若第一镜头人物处于迈步、转身、抬手或准备互动状态，首帧必须保留该动作准备姿态和非对称重心；参考视频明确静止则保持静止。

结果保存到 `internal/debug/candidates/attempt_01.png`。此时不要写入 `outputs/`。

### 7. 宿主多模态视觉 QA 与自动重生

```bash
python3 scripts/create_qa_template.py \
  <run_dir>/internal/debug/run_manifest.json \
  <run_dir>/internal/debug/candidates/attempt_01.png \
  <run_dir>/internal/debug/first_frame_visual_validation.json \
  --attempt 1
```

由宿主同时查看源首帧与候选图，逐项把 `NOT_RUN` 改成 `PASS` 或 `FAIL`。**判定标准见 [references/visual-qa.md](references/visual-qa.md)。**

任一项 FAIL 时：`overall` 写 `FAIL`，在 `regeneration_instructions` 写具体可执行修正，保留失败候选，用 `--previous-qa` 生成下一轮请求再次调用宿主 ImageGen。**最多 3 次**；仍失败则本轮失败，不发布任何交付物。

任何可辨认字符（含春联、灯笼、招牌、装饰物）都必须记录到 `detected_readable_text` 并判 FAIL。`motion_ready_pose_match` 是硬门：候选图姿态必须能自然衔接第一镜头的参考动作起始状态。

### 8. 写入渲染来源

只有宿主 ImageGen 直接返回的一张独立照片才可写 `internal/debug/render_provenance.json`，字段见 [templates/render_provenance.json](templates/render_provenance.json)，其中 `candidate_sha256` 必须是候选图文件的真实 SHA-256。不要为失败图伪造 provenance。

### 9. 发布与 Release Gate

只有最终 QA `overall=PASS` 且所有必需字段显式 `PASS` 时执行：

```bash
python3 scripts/publish_outputs.py \
  <run_dir> \
  <run_dir>/internal/debug/candidates/attempt_XX.png \
  <run_dir>/internal/debug/image_to_video_prompt.candidate.md

python3 scripts/release_gate.py <run_dir>
```

Gate 是 fail-closed 的完整实现，会校验交付物数量与方向、QA 与 provenance 的 SHA-256 是否绑定最终首帧、Prompt 是否能由同一份 `video_analysis.json` 逐字复算，以及所有镜头时间与口播是否齐全。**失败原因以 `internal/debug/release_gate.json` 的 `reason` 为准。** 失败时 `publish_outputs.py` 会自动撤销已复制的对外文件，保持证据留在 `internal/debug/`，不要声明成功。

## 对用户的最终回复

成功时只展示两个交付物：`first_frame.png` 和 `image_to_video_prompt.md`。不要把内部报告、JSON 或调试文件作为交付物。失败时只说明 Gate 的具体阻塞项，不提供不合格首帧。
