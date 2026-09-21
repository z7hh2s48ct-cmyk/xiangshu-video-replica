from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.generation import GenerationBatchRequest
from app.h3_prompts import GenerationContext, analysis_prompt_result, prompt_issues


def test_replica_final_prompt_uses_confirmed_script_frame_and_real_cuts() -> None:
    from app.h3_prompts import compile_replica_final_text, dialogue

    shots = [
        {
            "start_time": 0,
            "end_time": 2,
            "segment_kind": "ACTION_BEAT",
            "wardrobe_pose_detail": "红衣站立",
            "spoken_text": "欢迎看房",
            "action": "抬手",
            "scene_lighting": "左侧柔光",
            "motion": {
                "subject_motion_state": "WALKING",
                "subject_direction": "toward_camera",
                "camera_motion": "PULL_BACK",
                "relative_motion": "主体占比保持不变",
            },
        },
        {
            "start_time": 2,
            "end_time": 4,
            "segment_kind": "SHOT_CUT",
            "action": "指向庭院",
        },
    ]
    text = compile_replica_final_text(
        shot_payload={"shots": shots},
        script_text="今天带你看庭院",
        duration=4,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=0,
    )
    assert dialogue(text) == "今天带你看庭院"
    assert "欢迎看房" not in text and "红衣" not in text
    # 时长由 API 的 duration 参数承载，写进正文是冗余；见 docs/prompt-spec
    # 《视频复刻参考生视频方案与提示词修改意见》诊断列「分辨率/时长写进正文是冗余」。
    assert "目标成片时长" not in text
    assert "生成数量" not in text and "生成 1 条" not in text
    assert text.startswith(
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        "integrated_multimodal_description: [Shot 1]"
    )
    assert "[Shot 2] At 00:02.000" in text
    assert "At 00:02.000 [Shot 2]" not in text
    assert text.count("[Shot ") == 3  # first-frame instruction plus two shot markers
    assert "(S1) says: <d>[Chinese] 今天带你看庭院</d>" in text
    assert text.count("今天带你看庭院") == 1
    assert "PULL_BACK" in text and "toward_camera" in text and "左侧柔光" in text
    assert not prompt_issues(text, mode="I2VA", duration=4, labels=["<Picture 1>"])


def test_replacement_first_frame_neutralizes_source_presenter_identity() -> None:
    from app.h3_prompts import compile_replica_final_text

    text = compile_replica_final_text(
        shot_payload={
            "camera_language": "从争执全景切入女主持人的中景口播",
            "shots": [
                {
                    "start_time": 0,
                    "end_time": 2,
                    "segment_kind": "ACTION_BEAT",
                    "composition": "女主持人位于画面中央，村民站在两侧",
                    "action": "女主持人拿着文件夹讲解，村民保持静止",
                    "ambient_sound": "女主持人的清晰女性声音",
                    "motion": {"hand_action": "主持人单手做手势"},
                }
            ],
        },
        script_text="宅基地问题可以依法处理",
        duration=4,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    assert "女主持人" not in text
    assert "女性声音" not in text
    assert "主持人单手" not in text
    assert "<Picture 1> 中的主体是全片唯一主讲人身份参考" in text
    assert "首帧中的主讲人位于画面中央，村民站在两侧" in text
    assert "多人场景角色分层：<Picture 1> 主讲人是唯一主要角色" in text
    assert "村民等人物属于背景配角" in text
    assert "配音一致性：全部清晰口播只属于 <Picture 1> 主讲人" in text
    assert "声线的性别呈现、年龄感须与首帧人物一致" in text
    assert "不得出现异性声线替换、多人同时口播或中途变声" in text
    assert "确认文案必须从第一个字到最后一个字全部读出" in text
    assert "源视频只用于人物动作、镜头运动、节奏、构图和空间互动参考" in text
    assert "字幕、标题、贴纸、水印、Logo、账号名" in text
    assert "女主持人的清晰女性声音" not in text
    assert "不得复用源视频的人声、对白、口播、旁白或原说话人音色" in text
    assert "non_diegetic_music: N/A" in text
    assert "不得恢复源视频主持人的外观、性别或音色" in text


@pytest.mark.parametrize("length", [5, 59, 60, 91, 200])
def test_narration_length_is_never_gated_at_fifteen_seconds(length: int) -> None:
    """口播字数不设门禁：15 秒下任意长度都必须编译成功。

    60–90 字区间是 H3 只支持 4/15 两档时的遗留，只在 duration == 15 生效，
    造成 14 秒放行、15 秒硬拦的一秒断崖；其上限 90 还与通用上限 duration*6
    完全重合。同类产品（HeyGen/Synthesia/Veo 生态）对文案长度只给估算建议、
    不设准入门槛，长度与时长的匹配改由时间轴对齐承担。
    """
    from app.h3_prompts import compile_replica_final_text, dialogue

    script = "字" * length
    text = compile_replica_final_text(
        shot_payload={"shots": [{"start_time": 0, "end_time": 15, "segment_kind": "ACTION_BEAT"}]},
        script_text=script,
        duration=15,
        source_duration=15,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    assert dialogue(text) == script


@pytest.mark.parametrize("duration,length", [(4, 100), (12, 200), (8, 400)])
def test_narration_length_is_never_gated_on_other_durations(duration: int, length: int) -> None:
    """通用上限 duration*6 同样取消：塞不塞得下由时间轴对齐与提示表达，不再拒绝编译。"""
    from app.h3_prompts import compile_replica_final_text, dialogue

    script = "字" * length
    text = compile_replica_final_text(
        shot_payload={
            "shots": [{"start_time": 0, "end_time": duration, "segment_kind": "ACTION_BEAT"}]
        },
        script_text=script,
        duration=duration,
        source_duration=duration,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    assert dialogue(text) == script


def test_reference_video_exclusions_are_inserted_before_sound_sections() -> None:
    from app.h3_prompts import (
        REFERENCE_VIDEO_DIALOGUE_RULE,
        REFERENCE_VIDEO_VISUAL_ONLY_RULE,
        enforce_reference_video_exclusions,
    )

    prompt = (
        "subject_definitions: S1 is the presenter.\n"
        "summary: reference generation.\n"
        "retention_analysis: Keep motion.\n"
        "detailed_description: [Shot 1] Recreate the camera movement.\n"
        "overall_soundscape: Natural ambience.\n"
        "non_diegetic_music: None."
    )
    constrained = enforce_reference_video_exclusions(prompt)

    assert REFERENCE_VIDEO_VISUAL_ONLY_RULE in constrained
    assert REFERENCE_VIDEO_DIALOGUE_RULE in constrained
    assert constrained.index(REFERENCE_VIDEO_VISUAL_ONLY_RULE) < constrained.index(
        "overall_soundscape:"
    )
    assert not prompt_issues(constrained, mode="Ref2VA", duration=8, labels=[])
    assert enforce_reference_video_exclusions(constrained) == constrained


def test_replaced_scene_prompt_uses_confirmed_first_frame_environment() -> None:
    from app.h3_prompts import compile_replica_final_text

    text = compile_replica_final_text(
        shot_payload={
            "shots": [
                {
                    "start_time": 0,
                    "end_time": 4,
                    "shot_type": "中景",
                    "composition": "人物居中",
                    "scene": "源视频售楼部",
                    "scene_dressing": "源视频沙盘",
                    "scene_lighting": "源视频冷光",
                    "camera_motion": "缓慢推进",
                    "action": "人物抬手指向源视频售楼部沙盘",
                    "ambient_sound": "源售楼部广播",
                    "motion": {
                        "subject_motion_state": "WALKING",
                        "relative_motion": "人物绕过源沙盘后靠近镜头",
                    },
                }
            ]
        },
        script_text="带你看看",
        duration=4,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=0,
        replace_scene=True,
    )

    assert "全片场景以已确认首帧为准" in text
    assert "scene: 源视频售楼部" not in text
    assert "scene_dressing: 源视频沙盘" not in text
    assert "scene_lighting: 源视频冷光" not in text
    assert "中景" in text and "人物居中" in text and "缓慢推进" in text
    assert "动作参考（受场景替换规则约束）：人物抬手指向源视频售楼部沙盘" in text
    assert "动作：人物抬手指向源视频售楼部沙盘" not in text
    assert "运动参考（受场景替换规则约束）：relative_motion: 人物绕过源沙盘后靠近镜头" in text
    assert "源售楼部广播" not in text
    assert "overall_soundscape: 按已确认首帧的最终场景适配非语义环境音" in text
    assert "不得恢复源场景广播、固定道具声音或任何源人声" in text


def test_confirmed_first_frame_sources_preserve_scene_replacement_flag(monkeypatch) -> None:
    import json

    from app import generation

    monkeypatch.setattr(generation, "require_confirmed_first_frame", lambda *args, **kwargs: None)

    def version(_conn, project_id, kind):
        if kind == "first_frame_selection":
            return {"id": "selection-v1"}
        return {
            "id": "candidates-v1",
            "payload_json": json.dumps({"replace_scene": True}),
        }

    monkeypatch.setattr(generation, "latest_version", version)

    sources = generation.confirmed_first_frame_sources(
        object(), project_id="project-1", first_frame_asset_id="frame-1"
    )

    assert sources["first_frame_replace_scene"] is True


def test_final_prompt_requires_explicit_timing_and_start_alignment() -> None:
    from fastapi import HTTPException

    from app.h3_prompts import compile_replica_final_text

    base = dict(
        shot_payload={"shots": [{"start_time": 0, "end_time": 15}]},
        script_text="",
        duration=4,
        source_duration=15,
        timeline_policy="preserve",
        source_frame_time=0,
    )
    with pytest.raises(HTTPException) as exc:
        compile_replica_final_text(**base)
    assert exc.value.detail["code"] == "TIMELINE_CONFIRMATION_REQUIRED"
    base.update(timeline_policy="scale_confirmed", source_frame_time=7)
    with pytest.raises(HTTPException) as exc:
        compile_replica_final_text(**base)
    assert exc.value.detail["code"] == "FIRST_FRAME_ALIGNMENT_REQUIRED"


def test_longer_target_automatically_scales_timeline_to_full_duration() -> None:
    from app.h3_prompts import compile_replica_final_text

    text = compile_replica_final_text(
        shot_payload={
            "pace": "快节奏",
            "shots": [
                {"start_time": 0, "end_time": 2, "segment_kind": "ACTION_BEAT"},
                {"start_time": 2, "end_time": 4, "segment_kind": "SHOT_CUT"},
            ],
        },
        script_text="",
        duration=15,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    assert "[Shot 2] At 00:07.500" in text
    # 官方格式只有切镜时间戳；逐镜头起止时间不进正文。
    assert "阶段" not in text
    # 放慢是节奏指令，API 参数表达不了，必须留在正文；但不再复述目标时长。
    assert "人物动作、镜头运动和口播间隔等比放慢" in text
    assert "目标时长" not in text
    assert "pace: 快节奏" not in text


def test_silent_prompt_drops_narration_rules_that_contradict_no_voice_over() -> None:
    """没有确认文案时，正文已经写明「无口播」，再要求「确认文案必须全部读出」是自相矛盾。"""
    from app.h3_prompts import compile_replica_final_text

    text = compile_replica_final_text(
        shot_payload={"shots": [{"start_time": 0, "end_time": 4}]},
        script_text="",
        duration=4,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    assert "无口播，不添加台词或人声旁白。" in text
    assert "口播完整性：" not in text
    assert "配音一致性：" not in text
    assert "多人场景角色分层：" in text
    assert "源视频排除：" in text
    # 背景人声改由音景一条统一兜住，去掉配音规则不会放开源人声。
    assert "不得复用源视频的人声、对白、口播、旁白或原说话人音色" in text


def test_customer_duration_options_match_what_the_provider_accepts() -> None:
    """档位曾被收窄成 {4, 15}，导致 12 秒的源视频被拉成 15 秒或压成 4 秒（见
    docs/prompt-spec 方案文档问题 3）。provider 与报价端点都支持 4–15 的每一秒，
    客户档位不应再自行收窄；把两者绑在一起，防止日后又被改回去。"""
    from app.generation import CUSTOMER_DURATION_OPTIONS, validate_h3_request

    assert set(CUSTOMER_DURATION_OPTIONS) == set(range(4, 16))
    for seconds in sorted(CUSTOMER_DURATION_OPTIONS):
        validate_h3_request(
            {
                "model": "MiniMax-H3",
                "content": [{"type": "text", "text": "正文"}],
                "resolution": "768P",
                "duration": seconds,
                "ratio": "9:16",
            }
        )


def test_final_prompt_keeps_duration_out_of_the_body() -> None:
    """时长是 API 参数；正文只保留 H3 规范要求的镜头时间轴。"""
    from app.h3_prompts import compile_replica_final_text

    text = compile_replica_final_text(
        shot_payload={
            "shots": [
                {"start_time": 0, "end_time": 5, "segment_kind": "ACTION_BEAT"},
                {"start_time": 5, "end_time": 10, "segment_kind": "SHOT_CUT"},
            ]
        },
        script_text="",
        duration=10,
        source_duration=10,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    assert "目标成片时长" not in text
    assert "所有动作与运镜在此时长内完成" not in text
    # H3 规范要求的镜头编号与切镜时间戳不受影响；逐镜头起止时间不进正文。
    assert "[Shot 2] At 00:05.000" in text
    assert "阶段" not in text
    assert not prompt_issues(text, mode="I2VA", duration=10, labels=["<Picture 1>"])


@pytest.mark.parametrize("source_time", [7, -1])
def test_sitting_frame_requires_and_uses_human_opening_plan(source_time: float) -> None:
    from app.h3_prompts import compile_replica_final_text, dialogue

    text = compile_replica_final_text(
        shot_payload={
            "shots": [
                {
                    "start_time": 0,
                    "end_time": 4,
                    "action": "从站立开始坐下",
                    "motion": {"subject_motion_state": "WALKING"},
                }
            ]
        },
        script_text="今天介绍[庭院]",
        duration=4,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=source_time,
        opening_action="从首帧坐姿开始，坐着转头看向庭院。",
    )
    assert "从站立开始坐下" not in text and "WALKING" not in text
    assert "从首帧坐姿开始" in text
    assert dialogue(text) == "今天介绍[庭院]"


def test_optimizer_protects_plain_dialogue_and_explicit_silence() -> None:
    import json

    from app.prompt_optimizer import validate_result

    result, status = validate_result(
        json.dumps(
            {
                "prompt_text": "integrated_multimodal_description: [Shot 1] <d>修改后的话</d>\n"
                "overall_soundscape: N/A\nnon_diegetic_music: N/A",
                "warnings": [],
            }
        ),
        snapshot={
            "prompt_text": "台词：原稿",
            "protected_dialogue": "原稿",
            "context": {"mode": "T2VA", "duration_seconds": 4, "generation_assets": []},
        },
    )
    assert status == "FAILED"
    assert any(w["code"] == "DIALOGUE_CHANGED" for w in result["warnings"])


def test_final_text_and_legacy_version_are_exclusive() -> None:
    base = dict(
        quantity=1,
        first_frame_asset_id="frame",
        output_duration_seconds=15,
        idempotency_key="click",
    )
    for extra in ({}, {"prompt_text": " "}, {"prompt_text": "text", "prompt_version_id": "old"}):
        with pytest.raises(ValidationError):
            GenerationBatchRequest(**base, **extra)
    text = "🎥" * 7000
    assert GenerationBatchRequest(**base, prompt_text=text).prompt_text == text
    with pytest.raises(ValidationError):
        GenerationBatchRequest(**base, prompt_text=text + "x")


def test_modes_follow_real_frame_roles() -> None:
    assert GenerationContext(route="text_image").mode() == "T2VA"
    assert GenerationContext(route="text_image", last_frame_asset_id="tail").mode() == "L2VA"
    assert (
        GenerationContext(first_frame_asset_id="first", last_frame_asset_id="tail").mode()
        == "FL2VA"
    )
    assert GenerationContext(route="reference").mode() == "Ref2VA"
    with pytest.raises(ValidationError):
        GenerationContext(duration_seconds=4.5)


def test_manual_text_is_allowed_but_invented_reference_is_not() -> None:
    assert prompt_issues("人物挥手", mode="T2VA", duration=8, labels=[], strict=False) == []
    assert (
        prompt_issues("<Video 1> 人物挥手", mode="T2VA", duration=8, labels=[], strict=False)[
            0
        ].code
        == "REFERENCE_NOT_BOUND"
    )


def test_analysis_keeps_action_beats_in_one_shot_and_dialogue_once() -> None:
    context = {
        "mode": "T2VA",
        "duration_seconds": 8,
        "generation_assets": [],
        "context_hash": "hash",
    }
    analysis = {
        "duration_seconds": 8,
        "shots": [
            {"spoken_text": "你好", "segment_kind": "ACTION_BEAT"},
            {"spoken_text": "世界", "segment_kind": "ACTION_BEAT"},
        ],
    }
    text = (
        "integrated_multimodal_description: [Shot 1] (S1) <d>[Chinese] 你好世界</d>\n"
        "overall_soundscape: N/A\n"
        "non_diegetic_music: N/A"
    )
    assert (
        analysis_prompt_result({"prompt_text": text}, context=context, analysis=analysis)["status"]
        == "READY"
    )
    result = analysis_prompt_result(
        {"prompt_text": text.replace("你好世界", "你好")}, context=context, analysis=analysis
    )
    assert result["status"] == "NEEDS_REVIEW"
    assert result["issues"][0]["code"] == "DIALOGUE_MISMATCH"


def test_missing_context_preserves_analysis_without_false_ready() -> None:
    result = analysis_prompt_result(
        None,
        context={
            "mode": "I2VA",
            "context_hash": "hash",
            "issues": [{"code": "GENERATION_ASSET_REQUIRED", "message": "缺少首帧"}],
        },
        analysis={},
    )
    assert result["status"] == "NEEDS_CONTEXT"
    assert result["prompt_text"] is None


def test_analysis_one_call_keeps_facts_when_prompt_is_invalid() -> None:
    import json

    from app.analysis import ApilioGemini, FakeGemini, analyze_video

    facts = json.loads(FakeGemini().analyze(video_uri="fake", duration_seconds=8).text)

    class Transport:
        calls = 0

        def post(self, url, *, headers, body):
            self.calls += 1
            request = json.loads(body)
            assert "analysis-h3.v1" in request["messages"][0]["content"]
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "schema_version": "analysis-h3.v1",
                                        "analysis": facts,
                                        "generation_prompt": {
                                            "mode": "T2VA",
                                            "prompt_text": "bad",
                                            "issues": [],
                                        },
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode(), {}

    transport = Transport()
    result = analyze_video(
        video_uri="https://example.test/source.mp4",
        video_duration_seconds=8,
        provider=ApilioGemini(api_key="test", transport=transport),
        generation_context={
            "mode": "T2VA",
            "duration_seconds": 8,
            "generation_assets": [],
            "context_hash": "h",
            "issues": [],
            "media_info": {"fps": 25, "resolution": "720x1280", "aspect_ratio": "9:16"},
        },
    )
    assert transport.calls == 1
    assert result.analysis.shots
    assert result.analysis.fps == 25
    assert result.generation_prompt["status"] != "READY"


def test_bad_json_never_triggers_paid_repair() -> None:
    from app.analysis import AnalysisProviderFailed, FakeGemini, analyze_video

    provider = FakeGemini(analysis_json="bad")
    with pytest.raises(AnalysisProviderFailed):
        analyze_video(video_uri="fake", video_duration_seconds=8, provider=provider)
    assert not hasattr(provider, "repair_json")


def test_rule_fields_match_parser_enums() -> None:
    from app.analysis import ShotMotion

    motion = ShotMotion(
        subject_motion_state="UNKNOWN",
        subject_direction="unknown",
        subject_displacement="无法判断",
        hand_action="手部被遮挡",
        camera_motion="ORBIT",
        relative_motion="无法判断",
    )
    assert motion.camera_motion == "ORBIT"


@pytest.mark.parametrize("case", ["dialogue", "warning"])
def test_optimizer_never_auto_accepts_changed_dialogue_or_uncertainty(case: str) -> None:
    import json

    from app.prompt_optimizer import validate_result

    original = "integrated_multimodal_description: [Shot 1] <d>[Chinese] 原句</d>\n"
    original += "overall_soundscape: N/A\nnon_diegetic_music: N/A"
    output = original.replace("原句", "被改写的台词") if case == "dialogue" else original
    warnings = [{"code": "UNCERTAIN", "message": "需核对动作"}] if case == "warning" else []
    result, state = validate_result(
        json.dumps({"prompt_text": output, "warnings": warnings}),
        snapshot={
            "prompt_text": original,
            "context": {"mode": "T2VA", "duration_seconds": 8, "generation_assets": []},
        },
    )
    assert state == ("FAILED" if case == "dialogue" else "NEEDS_INPUT")
    assert result["validation_status"] != "valid"
    assert result["warnings"][0]["code"] == (
        "DIALOGUE_CHANGED" if case == "dialogue" else "UNCERTAIN"
    )
