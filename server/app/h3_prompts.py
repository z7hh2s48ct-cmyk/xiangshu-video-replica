"""Shared H3 content rules and deterministic checks; no provider calls or billing."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

FORMATTER_VERSION = "h3-format.v1"
MAX_PROMPT_CHARS = 7000
Mode = Literal["T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA"]
RULES = Path(__file__).with_name("prompt_rules")

_PRESENTER_ROLE_PATTERN = re.compile(r"(?:女性|男性|女|男)?(?:主持人|主讲人|讲解员|出镜人)")
_VOICE_GENDER_PATTERN = re.compile(r"(?:女性|男性|女|男)(?:声音|声线|嗓音|人声)")

REFERENCE_VIDEO_VISUAL_ONLY_RULE = (
    "Source-video exclusion: reference videos provide only body motion, camera motion, "
    "timing, framing, and spatial interaction. Do not copy, recreate, paraphrase, or retain "
    "any source-video dialogue, narration, spoken words, voice identity, subtitles, captions, "
    "titles, stickers, watermarks, logos, UI text, or other visible text."
)
REFERENCE_VIDEO_DIALOGUE_RULE = (
    "Dialogue authority: only the current user-confirmed script may be spoken. If no new script "
    "is supplied, generate no intelligible speech. Background characters must not repeat source "
    "speech. An independently bound Audio reference may be used only for its explicitly assigned "
    "purpose; never recover audio from a reference video."
)


def enforce_reference_video_exclusions(text: str) -> str:
    """Make Ref2VA visual-only even when an older optimizer omits the exclusion rules."""

    missing = [
        rule
        for rule in (REFERENCE_VIDEO_VISUAL_ONLY_RULE, REFERENCE_VIDEO_DIALOGUE_RULE)
        if rule not in text
    ]
    if not missing:
        return text
    block = "\n".join(missing)
    soundscape = re.search(r"(?m)^overall_soundscape\s*:", text)
    if soundscape is None:
        return f"{text.rstrip()}\n{block}"
    return f"{text[: soundscape.start()].rstrip()}\n{block}\n{text[soundscape.start() :]}"


def _anchor_presenter_to_first_frame(value: object) -> str:
    """Remove source-presenter identity labels from retained shot descriptions.

    The shot analysis describes the source video, so labels such as ``女主持人``
    become contradictory after the user confirms a replacement first frame.  H3
    still needs the source action and composition, but the performer must resolve
    to Picture 1 instead of the source person's gender or appearance.
    """

    text = str(value)
    marker = "__FIRST_FRAME_PRESENTER__"
    text = text.replace("首帧中的主讲人", marker)
    text = _PRESENTER_ROLE_PATTERN.sub(marker, text)
    text = _VOICE_GENDER_PATTERN.sub("与首帧主讲人一致的声线", text)
    return text.replace(marker, "首帧中的主讲人")


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=200)


class GenerationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: Literal["text_image", "reference", "replica"] = "replica"
    duration_seconds: int = Field(default=15, ge=4, le=15, strict=True)
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] = "adaptive"
    # 分辨率仅作为优化器的清晰度预期上下文（见 prompt_rules/optimizer.txt），
    # 不写进提示词正文、不据此增删剧情；复刻流等旧调用方不传时为 None。
    resolution: Literal["768P", "2K"] | None = None
    project_id: str | None = None
    analysis_version_id: str | None = None
    shot_card_version_id: str | None = None
    script_version_id: str | None = None
    source_asset_id: str | None = None
    first_frame_asset_id: str | None = None
    last_frame_asset_id: str | None = None
    references: list[Reference] = Field(default_factory=list, max_length=12)
    instructions: str = Field(default="", max_length=2000)

    def mode(self) -> Mode:
        if self.route == "reference":
            return "Ref2VA"
        if self.first_frame_asset_id and self.last_frame_asset_id:
            return "FL2VA"
        if self.first_frame_asset_id or self.route == "replica":
            return "I2VA"
        return "L2VA" if self.last_frame_asset_id else "T2VA"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def mode_rules(mode: Mode) -> str:
    common = (
        "完整正文，不使用代码围栏。镜头依次标记 [Shot 1]、[Shot 2]；"
        "仅真实切镜新增 Shot，切镜标记 At MM:SS.mmm，时间严格递增且小于目标时长。"
        "对白只出现一次，使用 <d>[Chinese] 原文</d>；无口播时不写对白标签。\n"
    )
    if mode != "Ref2VA":
        common += (
            "依次输出三个基础段落，段落名必须位于行首：\n"
            "integrated_multimodal_description:\noverall_soundscape:\nnon_diegetic_music:\n"
        )
    return common + (RULES / f"{mode}.txt").read_text(encoding="utf-8")


def protected_dialogue(text: str) -> str | None:
    """Legacy plain Chinese scripts remain protected before H3 tags exist."""
    if "<d>" in text:
        return dialogue(text)
    parts = re.findall(r"(?m)^\s*(?:台词|口播|确认文案)[：:]\s*(.+)$", text)
    return re.sub(r"\s", "", "".join(parts)) if parts else None


# 供应商指南要求镜头描述是自然叙事句，而不是把枚举堆成 ``key: value`` 标签
# （「Camera motion should be written as a natural English action within the
# shot, rather than stacked as separate labels」）。下面这组映射把 motion 枚举
# 确定性渲染成中文叙事片段。
# 注意：generation.py 的 render_shot_motion_clause 是面向另一套模板的祈使句
# （「不得僵立原地」），两者服务不同模板，措辞不可互换，故分开维护。
_MOTION_STATE_TEXT = {
    "STATIC": "保持静止",
    "WALKING": "行走",
    "RUNNING": "奔跑",
    "TURNING": "转身",
    "GESTURING_ONLY": "仅做手势",
    "OBJECT_MOTION": "物体位移",
    "NO_PERSON": "无人物出镜",
    "UNKNOWN": "运动状态无法判断",
}
# 只有位移类状态可以前置方向（「朝镜头方向行走」）。静止/无人物/物体位移这类
# 状态自带完整语义，前置方向会读成病句（「无位移方向无人物出镜」），因此不在
# 白名单里——白名单本身就是「不给这些状态加方向」的实现。
_DIRECTION_PREFIXABLE_STATES = frozenset({"WALKING", "RUNNING", "TURNING"})
_MOTION_DIRECTION_TEXT = {
    "toward_camera": "朝镜头方向",
    "away_from_camera": "背离镜头方向",
    "left": "向画面左",
    "right": "向画面右",
    "lateral": "横向",
    "in_place": "原地",
    "none": "",
    "unknown": "",
}
_CAMERA_MOTION_TEXT = {
    "STATIC": "固定机位",
    "PUSH_IN": "推近",
    "PULL_BACK": "拉远",
    "HANDHELD_TRACKING": "手持跟拍",
    "PAN": "横摇",
    "TILT": "纵摇",
    "FOLLOW": "跟随",
    "ORBIT": "环绕",
    "CRANE_UP": "升镜",
    "CRANE_DOWN": "降镜",
    "ZOOM_IN": "变焦推近",
    "ZOOM_OUT": "变焦拉远",
    "UNKNOWN": "机位运动无法判断",
}


def _subject_motion_clause(motion: dict[str, Any]) -> str:
    """把人物的运动状态（含并列主态）与位移方向渲染成一个叙事短语。"""
    states = [part for part in str(motion.get("subject_motion_state") or "").split("+") if part]
    raw_direction = str(motion.get("subject_direction") or "")
    direction = _MOTION_DIRECTION_TEXT.get(raw_direction, "")

    clauses: list[str] = []
    for index, state in enumerate(states):
        text = _MOTION_STATE_TEXT.get(state, "")
        if not text:
            continue
        if index == 0 and direction and state in _DIRECTION_PREFIXABLE_STATES:
            text = f"{direction}{text}"
        clauses.append(text)
    if not clauses and direction:
        clauses.append(direction)
    # 并列主态用「，同时」连接，读起来仍是一句话而不是标签堆叠。
    return "，同时".join(clauses)


def _shot_action_motion_narrative(shot: dict[str, Any]) -> str:
    """动作 + motion 合成官方指南式叙事句：动作起点 → 连续发展 → 结束状态。

    官方指南推荐的镜头结构是「首帧锚点 → 动作起点 → 连续发展 → 结果或反应」；
    这里把 action 与结构化 motion 合成一句话，供 ``compile_replica_final_text``
    放在静态字段之前下发，模型因此先读到「人物在动」再读到景别/构图等参数。
    缺少 motion 的旧拆解结果只渲染 action，不劣化既有行为。
    """
    segments: list[str] = []
    action = str(shot.get("action") or "").strip()
    if action:
        segments.append(_anchor_presenter_to_first_frame(action))

    motion = shot.get("motion")
    if isinstance(motion, dict):
        head = _subject_motion_clause(motion)
        person_parts = [head] if head else []
        for key in ("subject_displacement", "hand_action"):
            value = str(motion.get(key) or "").strip()
            if value:
                person_parts.append(_anchor_presenter_to_first_frame(value))
        if person_parts:
            segments.append("人物" + "，".join(person_parts))

        camera_parts: list[str] = []
        camera = _CAMERA_MOTION_TEXT.get(str(motion.get("camera_motion") or ""), "")
        if camera:
            camera_parts.append(f"镜头{camera}")
        relative = str(motion.get("relative_motion") or "").strip()
        if relative:
            camera_parts.append(_anchor_presenter_to_first_frame(relative))
        if camera_parts:
            segments.append("，".join(camera_parts))

    return "；".join(segments)


def compile_replica_final_text(
    *,
    shot_payload: dict[str, Any],
    script_text: str,
    duration: int,
    source_duration: float,
    timeline_policy: str,
    source_frame_time: float,
    opening_action: str = "",
    replace_scene: bool = False,
) -> str:
    """Deterministic final composition, after confirmed inputs; no paid model call."""

    def conflict(code: str, message: str) -> None:
        raise HTTPException(409, detail={"code": code, "message": message})

    if source_duration > duration and timeline_policy != "scale_confirmed":
        conflict(
            "TIMELINE_CONFIRMATION_REQUIRED",
            "源时间轴超过目标时长，请调整时长或明确确认压缩动作节奏。",
        )
    if (source_frame_time < 0 or source_frame_time > 0.25) and not opening_action.strip():
        conflict(
            "FIRST_FRAME_ALIGNMENT_REQUIRED",
            "首帧来自视频中段或缺少开场时间记录，请选择开场帧或填写动作衔接方案。",
        )
    # 口播长度不设门禁。曾有两条：15 秒专属的 60–90 字区间，以及通用的
    # duration*6 上限。前者是 H3 只支持 4/15 两档时的遗留，只在 duration == 15
    # 生效，造成 14 秒放行、15 秒硬拦的一秒断崖，其上限 90 还与后者在 15 秒处
    # 完全重合；同类产品对文案长度一律只给估算建议、不设准入。长度与时长的
    # 匹配改由 _narration_lines() 的时间轴对齐承担，而不是拒绝编译。
    if re.search(r"</?d>|<(?:Picture|Video|Audio)\s", script_text):
        conflict("SCRIPT_TAG_INVALID", "确认文案请使用纯文本，不包含提示词标签。")
    scale = duration / source_duration if source_duration != duration else 1.0
    lines = [
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.",
        "",
        "integrated_multimodal_description: [Shot 1]",
        # 时长由 API 的 duration 参数承载，复述进正文是冗余；它在这里只作为时间轴的
        # 缩放依据，仍体现在下方的镜头时间戳里。
        "主讲人绑定：<Picture 1> 中的主体是全片唯一主讲人身份参考；"
        "所有分镜里的主持人、主讲人、讲解者和口播者均指首帧中的主讲人。",
        "首帧中的主讲人，其性别、面部、发型、身形、服装和配饰只能取自 <Picture 1>；"
        "不得恢复源视频主持人的外观、性别或音色。",
        "多人场景角色分层：<Picture 1> 主讲人是唯一主要角色；村民等人物属于背景配角，"
        "按分镜保留人数、位置、动作和互动，面部与服装彼此不同；不得复制主讲人的脸、"
        "服装或身份，不得互换主配角。",
    ]
    # 无口播时这两条会和下方的「无口播，不添加台词或人声旁白。」自相矛盾，
    # 只有存在确认文案时才下发；背景人声由 overall_soundscape 一条统一约束。
    if script_text.strip():
        lines.append(
            "配音一致性：全部清晰口播只属于 <Picture 1> 主讲人；声线的性别呈现、年龄感须与"
            "首帧人物一致，全片保持同一说话者和同一音色。主讲人未出镜时使用同一人的画外音；"
            "背景配角只保留无明确语义的环境人声，不得朗读主讲人台词，不得出现异性声线替换、"
            "多人同时口播或中途变声。"
        )
        lines.append(
            "口播完整性：确认文案必须从第一个字到最后一个字全部读出，不得漏句、改写、重复、"
            "合并或截断；在目标时长内通过统一语速和自然停顿完成全部内容。"
        )
    lines.append(
        "源视频排除：源视频只用于人物动作、镜头运动、节奏、构图和空间互动参考；不得复制、"
        "恢复、改写或近义复述源视频中的口播、对白、旁白、原说话人音色，以及字幕、标题、"
        "贴纸、水印、Logo、账号名、界面文字或其他可读文字。只有当前确认文案可以作为台词。"
    )
    if source_duration < duration:
        # 拉长是节奏指令，API 参数表达不了，必须留在正文；措辞不复述目标时长。
        # 不再一律「等比放慢」：把整条成片压成慢镜头正是「偏静止」的来源。改为
        # 允许用同风格的连续动作自然填满时长，同时守住不新增剧情事件的边界。
        lines.append(
            "人物动作、镜头运动与口播间隔按原节奏自然延展，用同风格的连续动作"
            "铺满整条成片，不新增剧情事件、镜头或人物。"
        )
    if replace_scene:
        lines.append("全片场景以已确认首帧为准，不得恢复源视频的环境、陈设或光照。")
        lines.append(
            "场景替换规则：以下动作与运动仅作为人物动作和镜头运动参考；"
            "提及的原环境或固定道具，只有已存在于确认首帧时才可使用，"
            "严禁添加源场景物体；不复用源场景声音。"
        )
    style_keys: tuple[str, ...] = (
        ("pace", "camera_language")
        if replace_scene
        else (
            "visual_style",
            "color_tone",
            "pace",
            "camera_language",
        )
    )
    if source_duration < duration:
        style_keys = tuple(key for key in style_keys if key != "pace")
    for key in style_keys:
        if shot_payload.get(key):
            lines.append(f"{key}: {_anchor_presenter_to_first_frame(shot_payload[key])}")
    shot_number = 1
    # 旧拆解结果没有 motion，合成只能退回 action 文本；静默跳过会让「偏静止」
    # 的提示词无人察觉，所以逐段收集后在末尾给一条警告。
    legacy_without_motion: list[str] = []
    # 官方格式（docs/reference/minimax-h3-prompt-guide）只有真实切镜的
    # [Shot N] At MM:SS.mmm 标记（[Shot 1] 不带时间）；逐镜头起止时间、
    # 总时长等一律不进正文，成片时长由 H3 请求的 duration 参数承载。
    for index, shot in enumerate(shot_payload["shots"]):
        if index > 0 and shot.get("segment_kind") == "SHOT_CUT":
            shot_number += 1
            start = float(shot["start_time"]) * scale
            lines.append(f"[Shot {shot_number}] At {int(start // 60):02d}:{start % 60:06.3f}")
        # 官方指南的镜头结构是「首帧锚点 → 动作起点 → 连续发展 → 结束状态」，
        # 动作与运动因此在静态字段之前下发：模型先读到人物在动，再读到景别、
        # 构图、场景等画面参数，避免整段被读成静止画面。
        if index == 0 and opening_action.strip():
            lines.append(
                f"用户确认的开场衔接：{_anchor_presenter_to_first_frame(opening_action.strip())}"
            )
        else:
            narrative = _shot_action_motion_narrative(shot)
            if narrative:
                label = "动作与运动参考（受场景替换规则约束）" if replace_scene else "动作与运动"
                lines.append(f"{label}：{narrative}")
        shot_keys = (
            (
                "shot_type",
                "composition",
                "camera_motion",
            )
            if replace_scene
            else (
                "shot_type",
                "composition",
                "scene",
                "scene_dressing",
                "scene_lighting",
                "camera_motion",
            )
        )
        for key in shot_keys:
            if shot.get(key):
                lines.append(f"{key}: {_anchor_presenter_to_first_frame(shot[key])}")
        if not isinstance(shot.get("motion"), dict):
            legacy_without_motion.append(str(shot.get("shot_id") or f"s{index + 1}"))
    if legacy_without_motion:
        logger.warning(
            "Replica prompt compiled without motion for %d shot(s) (%s): "
            "legacy analysis carries no motion, so movement cues fall back to "
            "the action text alone.",
            len(legacy_without_motion),
            ", ".join(legacy_without_motion),
        )
    if script_text:
        lines.append(
            f"The on-screen person shown in <Picture 1> (S1) says: <d>[Chinese] {script_text}</d>"
        )
    else:
        lines.append("无口播，不添加台词或人声旁白。")
    soundscape = (
        "按已确认首帧的最终场景适配非语义环境音；不得恢复源场景广播、固定道具声音或任何源人声。"
        if replace_scene
        else (
            "仅生成与当前画面匹配的非语义环境音；不得复用源视频的人声、对白、"
            "口播、旁白或原说话人音色。"
        )
    )
    lines.append(f"overall_soundscape: {soundscape}")
    lines.append("non_diegetic_music: N/A")
    return "\n".join(lines)


def prompt_issues(
    text: str, *, mode: Mode, duration: int, labels: list[str], strict: bool = True
) -> list[Issue]:
    issues: list[Issue] = []

    def add(code: str, message: str) -> None:
        issues.append(Issue(code=code, message=message))

    if not text.strip() or len(text) > MAX_PROMPT_CHARS:
        add("PROMPT_LENGTH_INVALID", "提示词须为 1–7000 字，且不能全为空白。")
    for kind, number in re.findall(r"<(Picture|Video|Audio)\s+(\d+)>", text):
        if f"<{kind} {number}>" not in labels:
            add("REFERENCE_NOT_BOUND", f"{kind} {number} 未绑定当前素材，请重新选择。")
    if re.search(r"(?<![A-Za-z0-9_@])@\d+", text):
        add("REFERENCE_ALIAS_UNRESOLVED", "请在提交预览中将 @编号绑定为当前素材标签。")
    if not strict:
        return issues
    fields = (
        ("subject_definitions", "summary", "retention_analysis", "detailed_description")
        if mode == "Ref2VA"
        else ("integrated_multimodal_description",)
    ) + ("overall_soundscape", "non_diegetic_music")
    positions = [re.search(rf"(?m)^\s*{field}\s*:", text) for field in fields]
    if any(match is None for match in positions):
        add("H3_STRUCTURE_INVALID", "提示词缺少当前模式的必要段落。")
    elif [match.start() for match in positions if match] != sorted(
        match.start() for match in positions if match
    ):
        add("H3_STRUCTURE_INVALID", "提示词段落顺序不正确。")
    if "```" in text or "[Shot 1]" not in text:
        add("H3_STRUCTURE_INVALID", "请使用完整 H3 正文和镜头标记，不带代码围栏。")
    if mode in ("I2VA", "FL2VA", "L2VA"):
        first_line = text.splitlines()[0] if text else ""
        if "Picture 1" not in first_line or (mode == "FL2VA" and "Picture 2" not in first_line):
            add("FRAME_ALIGNMENT_REQUIRED", "缺少当前首尾帧的对齐说明。")
    # Alignment instructions can reference shot numbers too. Validate only the
    # timeline body so the official I2VA first-line reference is not a duplicate.
    if mode == "Ref2VA":
        narrative = text.split("detailed_description:", 1)[-1]
    else:
        narrative = text.split("integrated_multimodal_description:", 1)[-1]
        narrative = narrative.split("overall_soundscape:", 1)[0]
    shot_numbers = [int(value) for value in re.findall(r"\[Shot (\d+)\]", narrative)]
    if shot_numbers != list(range(1, len(shot_numbers) + 1)):
        add("SHOT_SEQUENCE_INVALID", "镜头编号应连续，动作阶段不能重复新增镜头。")
    times = [int(m) * 60 + float(s) for m, s in re.findall(r"At (\d+):(\d+(?:\.\d+)?)", narrative)]
    if any(t <= 0 or t >= duration for t in times) or times != sorted(set(times)):
        add("TIMELINE_CONFLICT", "镜头时间须递增且位于生成时长内。")
    if text.count("<d>") != text.count("</d>"):
        add("DIALOGUE_INVALID", "台词标签不完整。")
    if "听不清" in text or "无法辨识" in text:
        add("CONTENT_REVIEW_REQUIRED", "存在无法辨识的内容，请核对。")
    return issues


def dialogue(text: str) -> str:
    return "".join(
        re.sub(r"<[^>]+>|\s", "", re.sub(r"^\s*\[(?:Chinese|English|Mandarin)\]\s*", "", part))
        for part in re.findall(r"<d>(.*?)</d>", text, flags=re.S)
    )


def analysis_prompt_result(
    candidate: Any, *, context: dict[str, Any], analysis: dict[str, Any]
) -> dict[str, Any]:
    mode: Mode = context["mode"]
    issues = [Issue.model_validate(item) for item in context.get("issues", [])]
    status = "NEEDS_CONTEXT" if issues else "INVALID"
    text = candidate.get("prompt_text") if isinstance(candidate, dict) else None
    if isinstance(candidate, dict) and candidate.get("mode", mode) != mode:
        issues.append(Issue(code="MODE_MISMATCH", message="生成模式与当前素材不一致。"))
        status = "INVALID"
    if not issues and isinstance(text, str):
        issues.extend(
            prompt_issues(
                text,
                mode=mode,
                duration=context["duration_seconds"],
                labels=[a["label"] for a in context["generation_assets"]],
            )
        )
        status = "INVALID" if issues else "READY"
        source_text = "".join(str(shot.get("spoken_text", "")) for shot in analysis["shots"])
        if re.sub(r"\s", "", source_text) != dialogue(text):
            issues.append(Issue(code="DIALOGUE_MISMATCH", message="台词与分析不一致，请核对。"))
            if status == "READY":
                status = "NEEDS_REVIEW"
        cuts = 1 + sum(s.get("segment_kind") == "SHOT_CUT" for s in analysis["shots"][1:])
        narrative = text.split("detailed_description:", 1)[-1]
        if len(re.findall(r"\[Shot \d+\]", narrative)) != cuts:
            issues.append(Issue(code="SHOT_MISMATCH", message="切镜数与分析不一致，请核对。"))
            if status == "READY":
                status = "NEEDS_REVIEW"
        original = re.sub(r"\s", "", str(analysis.get("original_script", source_text)))
        if original != re.sub(r"\s", "", source_text):
            issues.append(
                Issue(code="SOURCE_DIALOGUE_CONFLICT", message="全片口播与分段台词不一致，请核对。")
            )
            if status == "READY":
                status = "NEEDS_REVIEW"
        cut_times = [
            float(s["start_time"])
            for s in analysis["shots"][1:]
            if s.get("segment_kind") == "SHOT_CUT" and "start_time" in s
        ]
        prompt_times = [
            int(m) * 60 + float(sec)
            for m, sec in re.findall(r"At (\d+):(\d+(?:\.\d+)?)", narrative)
        ]
        if cut_times and (
            len(cut_times) != len(prompt_times)
            or any(abs(a - b) > 0.05 for a, b in zip(cut_times, prompt_times, strict=False))
        ):
            issues.append(
                Issue(code="CUT_TIME_MISMATCH", message="切镜时间与源分析不一致，请核对。")
            )
            if status == "READY":
                status = "NEEDS_REVIEW"
        if analysis["duration_seconds"] > context["duration_seconds"]:
            issues.append(Issue(code="TIMELINE_CONFLICT", message="源视频超过目标时长，请调整。"))
            if status == "READY":
                status = "NEEDS_REVIEW"
        if isinstance(candidate, dict) and candidate.get("issues"):
            issues.extend(Issue.model_validate(item) for item in candidate["issues"])
            if status == "READY":
                status = "NEEDS_REVIEW"
    return {
        "mode": mode,
        "prompt_text": text if isinstance(text, str) else None,
        "status": status,
        "issues": [i.model_dump() for i in issues],
        "context_hash": context["context_hash"],
        "formatter_version": FORMATTER_VERSION,
    }
