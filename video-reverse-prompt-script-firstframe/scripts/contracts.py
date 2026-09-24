"""Single source of truth for V0.6 delivery and QA contracts."""

from typing import Any, Dict, Iterable, List, Set


SCHEMA_VERSION = "0.6"

DELIVERY_FILES: Set[str] = {"first_frame.png", "image_to_video_prompt.md"}

SQUARE_TOLERANCE = 0.05
ORIENTATIONS: Set[str] = {"PORTRAIT", "LANDSCAPE", "SQUARE"}
ORIENTATION_RULES = {
    "PORTRAIT": (
        "输出必须是竖构图，高明显大于宽，与源视频的竖屏画幅一致；"
        "不得输出横构图或方形构图，也不得为了容纳更多场景而改变画幅。"
    ),
    "LANDSCAPE": (
        "输出必须是横构图，宽明显大于高，与源视频的横屏画幅一致；"
        "严禁按短视频惯例改成竖屏，也不得输出方形构图。"
    ),
    "SQUARE": (
        "输出必须是方形构图，宽高基本一致，与源视频画幅一致；"
        "不得输出明显的横构图或竖构图。"
    ),
}

MOTION_STATES: Set[str] = {
    "STATIONARY",
    "WALKING",
    "RUNNING",
    "TURNING",
    "SITTING",
    "STANDING_UP",
    "SITTING_DOWN",
    "GESTURING",
    "INTERACTING",
    "BACKGROUND",
    "MIXED",
}
MOTION_CONFIDENCE: Set[str] = {"HIGH", "MEDIUM", "LOW"}
SOUND_ROLES: Set[str] = {"ON_CAMERA_SPEECH", "VOICE_OVER", "NO_SPEECH"}
CHARACTER_REFERENCE_MODES: Set[str] = {
    "SOURCE_ONLY",
    "OPTIONAL_REFERENCE_IDENTITY",
    "OPTIONAL_REFERENCE_IDENTITY_AND_COSTUME",
    "SOURCE_OR_OPTIONAL_REFERENCE",
}
MOTION_POLICY_TERMS = (
    "走路",
    "走动",
    "走两步",
    "迈步",
    "抬手",
    "抬右手",
    "抬左手",
    "挥手",
    "手势",
    "指向",
    "静止",
    "原地不动",
    "站立不动",
    "固定动作",
    "默认动作",
    "每个镜头",
    "所有镜头",
    "所有出镜镜头",
    "每段",
)

REQUIRED_PROMPT_SECTIONS = (
    "生成目标",
    "首帧一致性",
    "全局风格与人物一致性",
    "完整分镜时间轴",
    "声音与口型策略",
    "负面约束",
)

BASE_VISUAL_CHECKS: Set[str] = {
    "scene_match",
    "reference_composition_match",
    "character_type_match",
    "pose_costume_camera_match",
    "motion_ready_pose_match",
    "text_free",
    "subtitle_free",
    "watermark_free",
    "signage_text_free",
    "couplet_text_free",
    "lantern_text_free",
    "standalone_image",
    "no_report_or_collage_layout",
    "photorealistic",
    "image_to_video_ready",
}

VILLA_VISUAL_CHECKS: Set[str] = {
    "villa_is_rural_not_urban",
    "villa_is_three_storey",
    "three_storey_visibility",
    "villa_premium_quality_floor",
    "architecture_proportion_mature",
    "facade_material_layering",
    "doors_windows_eaves_reasonable",
    "courtyard_landscape_reasonable",
    "construction_value_visible",
    "architectural_realism",
    "not_palatial_or_overluxury",
    "not_cg_render",
}


class AnalysisValidationError(ValueError):
    pass


def orientation_of(width: int, height: int) -> str:
    """Classify source geometry once for both generation and release checks."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    longest = max(width, height)
    if abs(width - height) / longest <= SQUARE_TOLERANCE:
        return "SQUARE"
    return "PORTRAIT" if height > width else "LANDSCAPE"


def _required_text(value: Dict[str, Any], field: str, label: str) -> str:
    text = str(value.get(field, "")).strip()
    if not text:
        raise AnalysisValidationError("%s is missing %s" % (label, field))
    return text


def _validate_character_metadata(analysis: Dict[str, Any]) -> None:
    for field in ("character_identity", "character_costume"):
        text = str(analysis[field])
        found = [term for term in MOTION_POLICY_TERMS if term in text]
        if found:
            raise AnalysisValidationError(
                "%s must describe identity or costume only; motion policy belongs in motion_observation"
                % field
            )
    mode = str(analysis["character_reference_mode"]).strip()
    if mode not in CHARACTER_REFERENCE_MODES:
        raise AnalysisValidationError("character_reference_mode is invalid")


def _validate_visible_motion(shot: Dict[str, Any], start: float, end: float, label: str) -> None:
    motion = shot.get("motion_observation")
    if not isinstance(motion, dict):
        raise AnalysisValidationError("%s is missing motion_observation" % label)
    if motion.get("source") != "REFERENCE_VIDEO":
        raise AnalysisValidationError(
            "%s motion_observation.source must be REFERENCE_VIDEO" % label
        )

    state = str(motion.get("state", "")).strip().upper()
    if state not in MOTION_STATES:
        raise AnalysisValidationError("%s has invalid motion state" % label)
    confidence = str(motion.get("confidence", "")).strip().upper()
    if confidence not in MOTION_CONFIDENCE:
        raise AnalysisValidationError("%s has invalid motion confidence" % label)

    timestamps = motion.get("evidence_timestamps")
    if not isinstance(timestamps, list) or len(timestamps) < 2:
        raise AnalysisValidationError(
            "%s motion evidence_timestamps requires at least two frames" % label
        )
    try:
        evidence = [float(item) for item in timestamps]
    except (TypeError, ValueError):
        raise AnalysisValidationError("%s has invalid motion evidence_timestamps" % label)
    if evidence != sorted(evidence) or len(set(evidence)) < 2:
        raise AnalysisValidationError(
            "%s motion evidence_timestamps must be ordered and distinct" % label
        )
    if evidence[0] < start or evidence[-1] > end:
        raise AnalysisValidationError(
            "%s motion evidence_timestamps must stay inside the shot" % label
        )

    for field in ("start_pose", "trajectory", "body_mechanics", "hand_action", "end_pose"):
        _required_text(motion, field, "%s motion_observation" % label)

    phases = motion.get("action_phases")
    if not isinstance(phases, list) or not phases:
        raise AnalysisValidationError("%s motion action_phases must not be empty" % label)
    expected_start = start
    for position, phase in enumerate(phases, start=1):
        phase_label = "%s motion phase %s" % (label, position)
        if not isinstance(phase, dict):
            raise AnalysisValidationError("%s must be an object" % phase_label)
        try:
            phase_start = float(phase["start"])
            phase_end = float(phase["end"])
        except (KeyError, TypeError, ValueError):
            raise AnalysisValidationError("%s has invalid timing" % phase_label)
        if (
            phase_start < start
            or phase_end > end
            or phase_end <= phase_start
            or abs(phase_start - expected_start) > 0.001
        ):
            raise AnalysisValidationError(
                "%s motion action_phases must cover the full shot without gaps or overlaps"
                % label
            )
        _required_text(phase, "body_action", phase_label)
        _required_text(phase, "hand_action", phase_label)
        expected_start = phase_end
    if abs(expected_start - end) > 0.001:
        raise AnalysisValidationError(
            "%s motion action_phases must cover the full shot without gaps or overlaps"
            % label
        )


def validate_analysis(analysis: Dict[str, Any]) -> None:
    if not isinstance(analysis, dict):
        raise AnalysisValidationError("video analysis must be a JSON object")
    if analysis.get("version") != SCHEMA_VERSION:
        raise AnalysisValidationError(
            "video analysis version must be %s" % SCHEMA_VERSION
        )
    try:
        duration = float(analysis["duration"])
    except (KeyError, TypeError, ValueError):
        raise AnalysisValidationError("video analysis duration must be positive")
    if duration <= 0:
        raise AnalysisValidationError("video analysis duration must be positive")
    for field in (
        "aspect_ratio",
        "first_shot",
        "visual_style",
        "character_identity",
        "character_costume",
        "character_reference_mode",
    ):
        _required_text(analysis, field, "video analysis")
    _validate_character_metadata(analysis)
    if not isinstance(analysis.get("three_storey_rural_villa"), bool):
        raise AnalysisValidationError("video analysis three_storey_rural_villa must be boolean")

    shots = analysis.get("shots")
    if not isinstance(shots, list) or not shots:
        raise AnalysisValidationError("video analysis must contain at least one shot")
    expected_start = 0.0
    for position, shot in enumerate(shots, start=1):
        label = "shot %s" % position
        if not isinstance(shot, dict):
            raise AnalysisValidationError("%s must be an object" % label)
        try:
            start = float(shot["start"])
            end = float(shot["end"])
        except (KeyError, TypeError, ValueError):
            raise AnalysisValidationError("%s has invalid timing" % label)
        if end <= start:
            raise AnalysisValidationError("%s end must be after start" % label)
        if abs(start - expected_start) > 0.001:
            raise AnalysisValidationError(
                "video analysis shots must cover the full video without gaps or overlaps"
            )
        for field in ("scene", "camera_motion", "relative_motion"):
            _required_text(shot, field, label)
        visible = shot.get("character_visible")
        if not isinstance(visible, bool):
            raise AnalysisValidationError("%s is missing boolean character_visible" % label)
        if visible:
            _validate_visible_motion(shot, start, end, label)
        else:
            _required_text(shot, "subject_motion", label)
        sound_role = str(shot.get("sound_role", "")).strip().upper()
        if sound_role not in SOUND_ROLES:
            raise AnalysisValidationError("%s has invalid sound_role" % label)
        if not isinstance(shot.get("lip_sync_required"), bool):
            raise AnalysisValidationError("%s lip_sync_required must be boolean" % label)
        if not isinstance(shot.get("spoken_text", ""), str):
            raise AnalysisValidationError("%s spoken_text must be text" % label)
        expected_start = end
    if abs(expected_start - duration) > 0.001:
        raise AnalysisValidationError(
            "video analysis shots must cover the full video without gaps or overlaps"
        )


def required_visual_checks(three_storey_rural_villa: bool) -> Set[str]:
    checks = set(BASE_VISUAL_CHECKS)
    if three_storey_rural_villa:
        checks.update(VILLA_VISUAL_CHECKS)
    return checks


def _failure_text(failures: Iterable[str]) -> str:
    failures = [str(item).strip() for item in failures if str(item).strip()]
    if not failures:
        return "无。"
    return "；".join(failures)


def build_first_frame_request(
    analysis: Dict[str, Any],
    attempt: int,
    previous_failures: List[str],
    orientation: str = "PORTRAIT",
) -> Dict[str, Any]:
    """Build a host-only ImageGen request; never select an external API/model."""
    validate_analysis(analysis)
    if orientation not in ORIENTATIONS:
        raise AnalysisValidationError("orientation is invalid")
    if attempt < 1 or attempt > 3:
        raise AnalysisValidationError("attempt must be between 1 and 3")
    first_shot = str(analysis.get("first_shot", "严格参照源视频第一有效镜头"))
    is_villa = bool(analysis.get("three_storey_rural_villa", False))

    prompt_parts = [
        "只生成一张独立、纯净、可直接用于图生视频的首帧照片。",
        ORIENTATION_RULES[orientation],
        "必须把提供的 source_first_frame.png 作为场景、构图、人物位置、姿态、服饰、机位、透视和光线的第一视觉依据。",
        "若没有人物参考图，直接复刻源视频中的角色类型、年龄段、姿态、服饰和镜头关系；不得要求用户补充任何图片。",
        "若存在人物参考图，只允许替换人物身份，不得让人物参考图污染源场景、服饰、姿态或机位。",
        "第一镜头摘要：" + first_shot,
        "严禁任何可读文字：字幕、水印、Logo、平台 UI、招牌、门牌、广告、春联、灯笼文字、衣服文字、包装文字和乱码全部必须消失并自然补全背景。",
        "严禁报告、信息图、拼图、故事板、对比图、边框、标题、提示词、表格、测试状态或多面板布局。",
        "保持真实手机摄影质感、自然肤色、真实材料和正确透视；不要海报化、插画化或 CG 化。",
    ]

    shots = analysis.get("shots")
    if isinstance(shots, list) and shots:
        first = shots[0]
        motion = first.get("motion_observation") if isinstance(first, dict) else None
        if (
            isinstance(first, dict)
            and first.get("character_visible") is True
            and isinstance(motion, dict)
        ):
            start_pose = str(motion.get("start_pose", "")).strip()
            if motion.get("source") == "REFERENCE_VIDEO" and start_pose:
                prompt_parts.extend(
                    [
                        "首帧人物动作准备姿态必须来自参考视频连续帧：" + start_pose,
                        "若参考视频显示正在迈步、转身、抬手或准备互动，必须保留对应非对称重心和四肢关系；若参考视频明确静止，则保持静止。不得改成默认对称站姿，也不得凭空增加动作。",
                    ]
                )

    if is_villa:
        prompt_parts.extend(
            [
                "检测到三层乡村别墅，自动启用 UPSCALE_RURAL_VILLA 视觉质量下限。",
                "建筑必须是轻高端改善型乡墅：建筑比例成熟，三层关系清晰可见，立面材质有层次，门窗尺度、檐口、台阶和庭院关系合理，整体有真实造价感。",
                "保持中国乡村低密度环境与可建造性，可以升级材质、门窗、檐口和庭院完成度，但不能豪宅宫殿化、欧式城堡化、罗马柱堆砌或金色装饰泛滥。",
                "采用真实建筑摄影，不得生成售楼处、酒店、办公楼、城市豪宅庄园或 CG 效果图。",
                "默认采用中全景或中远景，让三层主体可辨；人物如存在，占画面高度宜约 35%–50%。",
            ]
        )

    if previous_failures:
        prompt_parts.append("上一轮视觉 QA 失败原因，必须逐项修正：" + _failure_text(previous_failures))

    prompt_parts.append("输出只能是一张照片，不得在图片中解释任务。")

    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "codex_host_imagegen",
        "attempt": attempt,
        "orientation": orientation,
        "source_image_required": True,
        "character_references_optional": True,
        "prompt": "\n".join(prompt_parts),
        "required_qa_checks": sorted(required_visual_checks(is_villa)),
    }
