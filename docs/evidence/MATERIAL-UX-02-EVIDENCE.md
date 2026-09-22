# MATERIAL-UX-02 卡片尺寸修复与图片缩略图 证据

> 素材库改进任务清单第二项（P0）：网格卡片不再裁切竖屏主体（素材库以竖屏为主，140px 视窗 + cover 只显示竖屏中段影响大多数卡片）；图片网格走派生缩略图；视频/音频时长角标常显。

## §14 证据记录

```text
任务/工作包：MATERIAL-UX-02-20260922（素材库改进任务清单 P0 第二项；方案 §6 P0）
Owner / Reviewer：Qoder session (honor.pei) 代 honor.pei / 待 PR 评审分配
分支 / 基线 SHA：feat/material-ux-02-20260922 / origin/main@7b772ac4
上游规格段落：素材库改进工作任务清单-2026-09-22.md §MATERIAL-UX-02；
  素材库重分析与改进方案-2026-09-22.md §6 P0（根因拆解 §6.1）
改动文件：client/src/VideoPreview.tsx（fitContainer 挂 `video-preview--fit` 类）；
  client/src/video-preview.css（fit 模式前景 contain 覆盖，不声明 aspect-ratio）；
  client/src/studio/content.css（媒体区 140→160px、`.content-asset .studio-media`
  显式 aspect-ratio:auto）；client/src/studio/ui.tsx（视频时长角标常显、音频角标）；
  client/src/studio/ContentPages.tsx（图片瓦片走缩略图 URL，详情仍原图）；
  client/src/VideoPreview.test.tsx（+4 用例）；server/app/material_thumbs.py
  （image 抽帧链路 + 图片专用限宽 extract_image_thumbnail_jpeg /
  THUMBNAIL_MAX_WIDTH=960 + 按需派生整读源对象：_is_still_image 判定 +
  _source_bytes_for_thumbnail head_probe 开关）；server/app/materials.py（probe
  图片分支派生 + attach 放宽 image 并按类型选抽帧）；server/app/rbac_routes.py
  （批量授权对 image 签发 + 直连注释消歧义）；server/tests/test_material_thumbs.py
  （+10 用例，含参数化）；docs/客户云版任务认领登记.md
失败测试或回归锁定：三组反向验证——①基础链路（19 实例版本）：`git stash push`
  行为文件（materials.py / rbac_routes.py）后 test_batch_signs_thumbnail_url_for_
  image_materials / test_image_upload_probe_derives_thumbnail_jpeg /
  test_attach_records_thumbnail_key_for_image_assets 3 failed，`git stash pop`
  恢复 19 passed；②CodeReview 修复（23 实例版本）：`git stash push` 修复文件
  （material_thumbs.py / materials.py）后 6 failed / 17 passed（3 个新用例 +
  参数化 2 实例 + attach 用例；失败形态含新常量/helper 导入收集错误），
  `git stash pop` 恢复 23 passed；③行为级实验（不依赖测试框架）：同一 JPEG 源
  按旧策略（8MiB 头探测 + 视频式抽帧）与新策略（整读 + 图片专用抽帧）分别派生
  → 10826B ≠ 17562B（identical=False）；22.4MiB 随机像素 JPEG 实测完整派生
  34045B vs 前 8MiB 派生 16648B，确认截断源仍被 ffmpeg 宽容解出残缺画面
  （returncode=0）。前端 ui.test.tsx 3 个既有契约用例保持
  （「封面已就位时不叠加占位标记」在实现初版一度回归，裁决为时长常显但
  「视频预览图」兜底文案仍只限无封面，修正实现后 3 用例全过）
实现结果：①竖屏裁切真因是 content.css `.content-asset--video .studio-media.video-preview
  img/video { object-fit: cover }`（(0,3,1)）压过前端基线 contain；fit 类
  `.video-preview--fit.video-preview.video-preview > .video-preview__foreground
  { object-fit: contain }`（(0,4,0)）恒赢且不声明 aspect-ratio（不与
  source-frame 宿主 9:16 规则争优先级）；②媒体区 140→160px（网格行高 / img /
  .studio-media / .content-asset--video 同步）；③视频角标条件
  `asset.duration || !asset.poster`——时长不再被封面抑制、「视频预览图」兜底仍只
  在无封面出现；音频瓦片恒显时长；④图片缩略图：上传 probe 即时派生
  `<object_key>.thumb.jpg`（≤480 高 / ≤960 宽 JPEG）+ attach 放宽 image + 批量授权对
  video/image 签发 + 前端网格图片走缩略图；存量按需派生不跑批；任一派生失败
  返回 None 降级、不阻塞原链路；⑤视觉实测（竖屏为主，复现页）——修复前
  140px/cover/竖屏可见区仅 31.6%；修复后容器 248.7×160、四类卡 100% 可见、
  角标可见（视频 00:02 / 音频 00:03）、音频控件完整、横竖混排填充率正确
  （方图 64.3% / 横图 87.4%）；⑥CodeReview 修复（Major）：
  图片按需派生改整读源对象（head_probe=False + _is_still_image 判定）——8–10MiB
  手机照片落在头探测阈值区间，截断源被 ffmpeg 宽容解码产出残缺缩略图并永久
  落盘；同时补图片宽度上界（旧只限高对「高 ≤480 但超宽」图不缩，2000×400 原样
  落盘 2000px 宽，现 ≤960）。视频链路零变化：统一限宽方案因 16:9 输出
  854→853 的 1px 差异被否决，改图片专用表达式；评审 Minor 处置——rbac_routes.py
  直连注释消歧义（图片缩略图直连为有意接受的低敏感窗口）、补 ensure 按需派生
  路径测试 ×2、.tmp-repro/ 归档 .verify 后清理
验证命令与通过数：server 专项 `uv run python -m pytest tests/test_material_thumbs.py -q`
  → 23 passed（13 既有 + 10 新增，含参数化实例）；前端专项 `npx vitest run src/studio/ui.test.tsx
  src/VideoPreview.test.tsx` → 22 passed；`npm run check:static` 全绿（前端 vitest
  全量 1774 passed、tauri cargo fmt/check、ruff 418 files、mypy 173 source files）；
  `bash scripts/ci/run-pytest-shards.sh` 四片全绿 821+737+644+730 =
  2932 passed / 1 skipped（每片独立 PG 容器端口 5433+i）
证据层级：AUTOMATED_VERIFIED（本地自动化 + 浏览器视觉实测；真实存储/真实客户
  环境验收归 staging 推进）
安全与可观测性：缩略图签发沿用既有 7 天签名通道与属主校验前置
  （_grant_download_for_asset），仅放宽 content_type 条件；音频残留键不签发；
  无新增密钥/端点/参数；派生失败仅损失缩略图本身
迁移与回滚：零迁移（缩略图键记 assets.metadata_json）；还原本 PR 文件即回滚，
  已派生缩略图对象为附加物、原对象与既有视频链路不受影响
外部授权记录：无（未触碰生产 COS / 付费 Provider / 发码）
未测试项：真实 COS 存储链路与真实客户 Tauri 环境（归 staging）；存量素材
  按需派生于真实数据规模的耗时；Windows NSIS 门禁归 CI 三门禁
Lore 提交 SHA：以 PR 当前 head 为准
```

## 门禁执行记录

- 2026-09-22：`npm run check:static` 全绿（前端 vitest 全量 1774 passed /
  112 test files；tauri cargo fmt/check；ruff 418 files already formatted；
  mypy 173 source files no issues）——含 CodeReview 修复后重跑。
- 2026-09-22：`bash scripts/ci/run-pytest-shards.sh` 四片全绿（821+737+644+730 =
  2932 passed / 1 skipped；每片独立 PG 容器，跑完自动 teardown）——含修复后
  新增的 4 个测试实例（shard 1 由 733 增至 737）。
- 2026-09-22：server 专项 `uv run python -m pytest tests/test_material_thumbs.py -q`
  → 23 passed（stash pop 后复跑确认恢复态）。
- 视觉校验本地归档：`.verify/material-ux-02-20260922/`（after-fix.png /
  before-fix.png / card-repro.html / portrait.mp4 / tone.mp3 + 竖屏与横竖混排
  素材；`.verify/` 按仓库约定 gitignored，不进版本库）。
