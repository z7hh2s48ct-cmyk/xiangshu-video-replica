#!/usr/bin/env python3
"""Compose the complete I2V prompt from the host-authored temporal analysis."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    from .contracts import (
        AnalysisValidationError,
        REQUIRED_PROMPT_SECTIONS,
        validate_analysis,
    )
except ImportError:
    from contracts import AnalysisValidationError, REQUIRED_PROMPT_SECTIONS, validate_analysis


class PromptBuildError(RuntimeError):
    pass


def _required_text(shot: Dict[str, Any], field: str) -> str:
    value = str(shot.get(field, "")).strip()
    if not value:
        raise PromptBuildError("shot %s is missing %s" % (shot.get("index", "?"), field))
    return value


def _motion_lines(shot: Dict[str, Any]) -> List[str]:
    if not shot["character_visible"]:
        return []

    motion = shot["motion_observation"]
    state = str(motion["state"]).strip().upper()
    confidence = str(motion["confidence"]).strip().upper()
    evidence = [float(value) for value in motion["evidence_timestamps"]]
    start_pose = _required_text(motion, "start_pose")
    trajectory = _required_text(motion, "trajectory")
    body_mechanics = _required_text(motion, "body_mechanics")
    hand_action = _required_text(motion, "hand_action")
    end_pose = _required_text(motion, "end_pose")
    phase_lines: List[str] = []
    for phase in motion["action_phases"]:
        phase_start = float(phase["start"])
        phase_end = float(phase["end"])
        body_action = _required_text(phase, "body_action")
        phase_hand_action = _required_text(phase, "hand_action")
        phase_lines.append(
            "- 动作阶段 %.3f–%.3f秒：身体：%s；手部：%s。"
            % (phase_start, phase_end, body_action, phase_hand_action)
        )
    evidence_text = "、".join("%.3f" % value for value in evidence)
    return [
        "- 动作来源：参考视频连续帧；动作状态：%s；置信度：%s；证据时间点：%s秒。"
        % (state, confidence, evidence_text),
        "- 动作起始与结束：%s → %s。" % (start_pose, end_pose),
        "- 人物移动路径：%s。" % trajectory,
        "- 手部动作：%s。" % hand_action,
        "- 身体力学：%s。" % body_mechanics,
        *phase_lines,
    ]


def compose_prompt(analysis: Dict[str, Any]) -> str:
    try:
        validate_analysis(analysis)
    except AnalysisValidationError as exc:
        raise PromptBuildError(str(exc))
    shots = analysis["shots"]
    duration = float(analysis["duration"])
    aspect_ratio = str(analysis["aspect_ratio"])
    first_shot = str(analysis["first_shot"])
    identity = str(analysis["character_identity"])
    costume = str(analysis["character_costume"])
    reference_mode = str(analysis["character_reference_mode"])
    style = str(analysis["visual_style"])

    lines: List[str] = [
        "# Image-to-Video Prompt",
        "",
        "## %s" % REQUIRED_PROMPT_SECTIONS[0],
        "基于 `first_frame.png` 生成一条 %.3f 秒、%s 画幅的视频。严格复刻参考视频的分镜顺序、节奏、人物动作、摄影机运动、相对运动和声音结构。" % (duration, aspect_ratio),
        "",
        "## %s" % REQUIRED_PROMPT_SECTIONS[1],
        "首帧是全片唯一视觉起点：%s。不得重新设计首帧中的人物、建筑、场景、机位、构图、透视或光线。" % first_shot,
        "",
        "## %s" % REQUIRED_PROMPT_SECTIONS[2],
        "- 人物身份：%s" % identity,
        "- 人物服饰：%s" % costume,
        "- 人物参考策略：%s" % reference_mode,
        "- %s" % style,
        "- 人物动作严格来自参考视频连续帧，不得固定为默认走路、默认手势或默认静止。参考视频有位移或手势时不得降级为僵立；参考视频明确静止时不得凭空增加走路或手势。",
        "- 动作复刻必须区分左右手、动作起始、动作过程和动作结束；只有参考视频确实出现行走时，才要求真实脚掌着地、重心转移和双臂自然摆动。",
        "- 若没有人物参考图，继续使用首帧中已经生成的角色；不得在后续镜头随机换人。",
        "- 禁止字幕、水印、Logo、招牌、春联、灯笼文字、品牌字样、平台 UI 和任何可读文字。",
        "",
        "## %s" % REQUIRED_PROMPT_SECTIONS[3],
    ]

    for position, shot in enumerate(shots, start=1):
        try:
            start = float(shot["start"])
            end = float(shot["end"])
        except (KeyError, TypeError, ValueError):
            raise PromptBuildError("shot %s has invalid timing" % position)
        if end <= start:
            raise PromptBuildError("shot %s end must be after start" % position)
        scene = _required_text(shot, "scene")
        camera_motion = _required_text(shot, "camera_motion")
        relative_motion = _required_text(shot, "relative_motion")
        motion_lines = _motion_lines(shot)
        if shot["character_visible"]:
            motion = shot["motion_observation"]
            subject_motion = "从%s开始，沿%s；%s；手部%s；最终%s" % (
                _required_text(motion, "start_pose"),
                _required_text(motion, "trajectory"),
                _required_text(motion, "body_mechanics"),
                _required_text(motion, "hand_action"),
                _required_text(motion, "end_pose"),
            )
        else:
            subject_motion = _required_text(shot, "subject_motion")
        sound_role = str(shot.get("sound_role", "NO_SPEECH")).strip().upper()
        spoken_text = str(shot.get("spoken_text", "")).strip()
        lip_sync = bool(shot.get("lip_sync_required", False))

        lines.extend(
            [
                "",
                "### %.3f–%.3f秒｜镜头 %02d" % (start, end, position),
                "- 场景：%s" % scene,
                "- 人物动作：%s" % subject_motion,
                "- 摄影机运动：%s" % camera_motion,
                "- 人物与摄影机相对运动：%s" % relative_motion,
                *motion_lines,
                "- 声音策略：%s；口型同步：%s。" % (sound_role, "需要" if lip_sync else "不需要"),
            ]
        )
        if spoken_text:
            label = "口播" if sound_role == "ON_CAMERA_SPEECH" else "画外音"
            lines.append("- %s：**%s**" % (label, spoken_text))
        elif sound_role == "VOICE_OVER":
            lines.append("- 画外音：延续上一镜头同一句话，不重新起音或更换音色。")

    lines.extend(
        [
            "",
            "## %s" % REQUIRED_PROMPT_SECTIONS[4],
            "人物出镜讲话段落使用 ON_CAMERA_SPEECH，并与对应中文口播逐字口型同步；人物不出镜的 B-roll 使用同一说话者的 VOICE_OVER 画外音，音色和语气连续。NO_SPEECH 段落不得凭空增加台词。",
            "",
            "## %s" % REQUIRED_PROMPT_SECTIONS[5],
            "禁止人物换脸、身份漂移、年龄变化、服饰随机改变、多人增生、肢体畸形、嘴部畸形和口型错位；禁止把参考视频中的走动、转身、手势或互动降级成原地僵立，也禁止给原本静止的人物添加默认走路、默认抬手或固定手势；禁止左右手混淆、动作起止丢失、下半身冻结、脚底滑动、原地踏步、机械摆臂、手掌瞬移、手腕反折、手指粘连和多余手指；禁止建筑变形、楼层漂移、门窗随机、材质融化、家具跳变；禁止字幕、水印、Logo、招牌、门牌、广告、春联、灯笼文字、包装文字、乱码、平台 UI；禁止报告、信息图、拼图、边框、测试文字；禁止夸张推拉、快速旋转、360 度环绕、无人机运镜、剧烈抖动、慢动作和不符合参考视频的电影特效。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        value = json.loads(args.analysis.read_text(encoding="utf-8"))
        prompt = compose_prompt(value)
    except (OSError, json.JSONDecodeError, PromptBuildError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(prompt, encoding="utf-8")
    except OSError as exc:
        print("ERROR: cannot write prompt: %s" % exc, file=sys.stderr)
        return 1
    print(str(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
