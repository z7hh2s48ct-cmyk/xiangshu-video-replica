# 首帧多模态 QA 规则

Codex 必须同时看 `source_first_frame.png` 和当前候选图，逐项给出 `PASS` 或 `FAIL`。看不清或不确定时判 `FAIL`。

## 通用硬门

- `scene_match`：第一镜头场景与源帧逻辑一致。
- `reference_composition_match`：机位、景别、主体位置、透视关系一致。
- `character_type_match`：无人物参考时，角色类型/年龄段/气质合理复刻；有人物参考时，身份匹配。
- `pose_costume_camera_match`：姿态、服饰、朝向和摄影机关系匹配。
- `motion_ready_pose_match`：首帧姿态与参考视频第一镜头的动作起始状态一致。参考人物正在迈步、转身、抬手或准备互动时，必须保留对应重心和非对称四肢关系；参考人物静止时不得凭空制造动作准备姿势。
- `text_free`：画面完全没有可读字符。
- `subtitle_free`、`watermark_free`、`signage_text_free`、`couplet_text_free`、`lantern_text_free`：分别检查对应文字来源。
- `standalone_image`：一张独立照片。
- `no_report_or_collage_layout`：无报告、拼图、UI、标题、边框和多面板。
- `photorealistic`：真实摄影，不是插画或 CG。
- `image_to_video_ready`：主体完整、构图可延续、没有阻碍动画的错误。

只要看到一个可辨认字符，就把具体内容或位置写入 `detected_readable_text`，`overall=FAIL`。

## 三层乡墅额外硬门

还必须检查：

- 真实乡村环境，不是城市豪宅。
- 明确三层，三层在画面中可辨。
- 达到轻高端改善型质量下限。
- 建筑比例成熟，立面材料有层次。
- 门窗、檐口、庭院合理。
- 有真实造价感和可建性。
- 不豪宅宫殿化，不 CG 效果图化。

任何一项 FAIL 都必须重生，不能靠文字说明放行。
