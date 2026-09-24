# 视频分析数据契约

`video_analysis.json` 由 Codex 基于连续时序证据填写。最小结构：

```json
{
  "version": "0.6",
  "duration": 15.082,
  "aspect_ratio": "9:16",
  "first_shot": "人物居中站在完成的乡墅正门前，正面中景",
  "three_storey_rural_villa": false,
  "visual_style": "真实手机竖屏、自然光、轻微手持",
  "character_identity": "保持首帧中同一人物身份、脸部、年龄感和体型",
  "character_costume": "保持参考视频或可选人物参考图确定的服饰",
  "character_reference_mode": "SOURCE_OR_OPTIONAL_REFERENCE",
  "shots": [
    {
      "index": 1,
      "start": 0.0,
      "end": 3.0,
      "scene": "完成的乡墅正门",
      "subject_motion": "人物原地自然口播",
      "camera_motion": "摄影机基本固定，仅轻微手持",
      "relative_motion": "人物与摄影机相对位置稳定",
      "character_visible": true,
      "motion_observation": {
        "source": "REFERENCE_VIDEO",
        "state": "STATIONARY",
        "confidence": "HIGH",
        "evidence_timestamps": [0.0, 1.5, 2.9],
        "start_pose": "人物正面站立，双手自然位于身体两侧",
        "trajectory": "人物位置与画面尺度保持稳定",
        "body_mechanics": "只有连续帧中可见的呼吸、眨眼和轻微重心变化",
        "hand_action": "左右手均无主动手势",
        "end_pose": "人物仍在原位正面站立",
        "action_phases": [
          {
            "start": 0.0,
            "end": 3.0,
            "body_action": "保持原地口播",
            "hand_action": "左右手均无主动手势"
          }
        ]
      },
      "sound_role": "ON_CAMERA_SPEECH",
      "spoken_text": "这里填写逐字口播。",
      "lip_sync_required": true
    }
  ]
}
```

规则：

- 时间连续、无重叠、覆盖完整视频。
- `subject_motion`、`camera_motion`、`relative_motion` 必须分别判断，不能合成一句“镜头移动”。
- 动作判断必须来自连续动作证据，不能从一张静态图臆测；人物出镜镜头至少记录两个不同的 `evidence_timestamps`，优先使用首、中、尾三处或更多证据。
- `motion_observation.source` 必须是 `REFERENCE_VIDEO`。动作状态只允许 `STATIONARY`、`WALKING`、`RUNNING`、`TURNING`、`SITTING`、`STANDING_UP`、`SITTING_DOWN`、`GESTURING`、`INTERACTING`、`BACKGROUND` 或 `MIXED`。
- 不得固定为走路、抬手或静止。参考视频人物走动就记录方向、速度、重心与步态；参考视频人物静止就明确记录静止，不得为了“更生动”增加动作。
- 必须分别描述左右手。记录动作起始、移动路径、身体力学、手部动作、动作阶段和动作结束；没有主动手势也要明确写出。
- 人物镜头的最终 `人物动作` 由 `motion_observation` 自动生成；自由文本 `subject_motion` 不能覆盖结构化动作事实。B-roll 继续使用 `subject_motion`。
- `character_identity`、`character_costume` 只负责人物身份与服装一致性，不得夹带走路、抬手、静止等动作模板。
- `character_reference_mode` 只允许 `SOURCE_ONLY`、`OPTIONAL_REFERENCE_IDENTITY`、`OPTIONAL_REFERENCE_IDENTITY_AND_COSTUME` 或 `SOURCE_OR_OPTIONAL_REFERENCE`。默认使用 `SOURCE_OR_OPTIONAL_REFERENCE`：只有用户主动提供人物图时才用它锁定身份；没有人物图也必须继续完成。
- 人物出镜讲话才可设 `lip_sync_required=true`。
- B-roll 的 `spoken_text` 属于画外音，不要让不存在的人物做口型。
- `first_shot` 只描述第一有效镜头，不得被整条视频主题污染。
