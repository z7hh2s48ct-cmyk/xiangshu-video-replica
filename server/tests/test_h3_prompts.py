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
    # 动作+motion 合成中文叙事句，裸枚举不再泄漏进正文（官方指南写法）。
    assert "动作与运动：抬手；人物朝镜头方向行走；镜头拉远，主体占比保持不变" in text
    assert "左侧柔光" in text
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
    assert (
        "动作与运动参考（受场景替换规则约束）：人物抬手指向源视频售楼部沙盘；"
        "人物行走；人物绕过源沙盘后靠近镜头" in text
    )
    assert "动作：人物抬手指向源视频售楼部沙盘" not in text
    # motion 不再以 key: value 形式泄漏进正文。
    assert "relative_motion:" not in text and "subject_motion_state:" not in text
    assert "源售楼部广播" not in text
    assert "overall_soundscape: 按已确认首帧的最终场景适配非语义环境音" in text
    assert "不得恢复源场景广播、固定道具声音或任何源人声" in text


def test_confirmed_first_frame_sources_preserve_scene_replacement_flag(monkeypatch) -> None:
    import json

    from app import generation

    monkeypatch.setattr(generation, "require_confirmed_first_frame", lambda *args, **kwargs: None)

    # 历史版本放开：候选版本按 selection 指向的 id 查询（不再回落最新版本），
    # 本测试让 selection 指向 candidates-v1，并让连接返回它的 payload。
    candidates_row = {
        "id": "candidates-v1",
        "payload_json": json.dumps({"replace_scene": True}),
    }
    monkeypatch.setattr(
        generation,
        "latest_version",
        lambda _conn, project_id, kind: {
            "id": "selection-v1",
            "payload_json": json.dumps({"first_frame_candidates_version_id": "candidates-v1"}),
        },
    )

    class _Cursor:
        def fetchone(self):
            return candidates_row

    class _Conn:
        """只承载 selection 指向的候选版本查询，其余 SQL 不参与本测试。"""

        def execute(self, *_args, **_kwargs):
            return _Cursor()

    sources = generation.confirmed_first_frame_sources(
        _Conn(), project_id="project-1", first_frame_asset_id="frame-1"
    )

    assert sources["first_frame_replace_scene"] is True
    assert sources["first_frame_candidates_version_id"] == "candidates-v1"


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
    # 拉长是节奏指令，API 参数表达不了，必须留在正文；但不再一律「等比放慢」
    # （那会把整条成片压成慢镜头，正是「偏静止」的来源）。
    assert "用同风格的连续动作铺满整条成片" in text
    assert "等比放慢" not in text
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


def test_optimizer_never_auto_accepts_changed_dialogue() -> None:
    import json

    from app.prompt_optimizer import validate_result

    original = "integrated_multimodal_description: [Shot 1] <d>[Chinese] 原句</d>\n"
    original += "overall_soundscape: N/A\nnon_diegetic_music: N/A"
    result, state = validate_result(
        json.dumps({"prompt_text": original.replace("原句", "被改写的台词"), "warnings": []}),
        snapshot={
            "prompt_text": original,
            "context": {"mode": "T2VA", "duration_seconds": 8, "generation_assets": []},
        },
    )
    assert state == "FAILED"
    assert result["validation_status"] == "invalid"
    assert result["issues"][0]["code"] == "DIALOGUE_CHANGED"


def test_optimizer_applies_prompt_with_assumption_list() -> None:
    """warnings 现在是假设清单：记录推断并随结果回传，不再阻断应用。

    用户要的是先拿到标准提示词，再在核对区逐条看推断；把「有推断」当成
    失败会让每个需求都被打回，等于没有生成能力。
    """
    import json

    from app.prompt_optimizer import validate_result

    original = "integrated_multimodal_description: [Shot 1] 原句\n"
    original += "overall_soundscape: N/A\nnon_diegetic_music: N/A"
    warnings = [{"code": "UNCERTAIN", "message": "需核对动作"}]
    result, state = validate_result(
        json.dumps({"prompt_text": original, "warnings": warnings}),
        snapshot={
            "prompt_text": original,
            "context": {
                "mode": "T2VA",
                "duration_seconds": 8,
                "generation_assets": [],
                "needs_confirmation": [{"code": "AUDIO_PURPOSE_INFERRED", "message": "音频用途"}],
            },
        },
    )
    assert state == "SUCCEEDED"
    assert result["validation_status"] == "valid"
    assert result["assumptions"] == warnings
    assert result["needs_confirmation"] == [
        {"code": "AUDIO_PURPOSE_INFERRED", "message": "音频用途"}
    ]


def test_independent_r2v_rejects_replica_draft_and_freeform_prompt() -> None:
    """R2V 与复刻流是两套提示词实现：复刻稿和随手写的短句都不得进供应商。"""
    from app.h3_prompts import is_replica_draft, prompt_issues

    replica = (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> "
        "(from [Shot 1]) is fully referenced.\n\n"
        "integrated_multimodal_description: [Shot 1]\n主讲人绑定：<Picture 1> 中的主体"
    )
    assert is_replica_draft(replica)
    issues = prompt_issues(replica, mode="Ref2VA", duration=8, labels=["<Picture 1>"], strict=True)
    assert issues and issues[0].code == "H3_STRUCTURE_INVALID"
    freeform = prompt_issues(
        "视频<Video 1>的人物用图片<Picture 1>替换。",
        mode="Ref2VA",
        duration=8,
        labels=["<Video 1>", "<Picture 1>"],
        strict=True,
    )
    assert [issue.code for issue in freeform] == ["H3_STRUCTURE_INVALID"] * len(freeform)


# --- 爆款复刻文案回填：空 original_script 用分段台词兜底 -------------------------
# 与 generation.py「full_text = original_script or "".join(spoken_texts)」的编译兜底
# 语义对齐；落库时回填后，前端展示、提示词编译与历史恢复都自动拿到文案。


def _analyze_with_facts(facts: dict, *, duration_seconds: float):
    """Drive analyze_video through a provider whose response carries ``facts``."""
    import json

    from app.analysis import ApilioGemini, analyze_video

    class Transport:
        def post(self, url, *, headers, body):
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
                                            "mode": None,
                                            "prompt_text": None,
                                            "issues": [],
                                        },
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode(), {}

    return analyze_video(
        video_uri="https://example.test/source.mp4",
        video_duration_seconds=duration_seconds,
        provider=ApilioGemini(api_key="test", transport=Transport()),
    )


def _fake_facts(**overrides: object) -> dict:
    import json

    from app.analysis import FakeGemini

    facts = json.loads(FakeGemini().analyze(video_uri="fake", duration_seconds=8).text)
    facts.update(overrides)
    return facts


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_original_script_is_backfilled_from_shot_spoken_text(blank: str) -> None:
    """服务商返回空 original_script 时必须用分段台词回填。

    否则爆款复刻页文案栏空白：前端 readAnalysisPayload 直用空串，提示词编译也
    拿不到原文。
    """
    facts = _fake_facts(original_script=blank)
    facts["shots"][0]["spoken_text"] = "第一句"
    facts["shots"][1]["spoken_text"] = "第二句"

    result = _analyze_with_facts(facts, duration_seconds=8)

    assert result.analysis.original_script == "第一句第二句"


def test_provided_original_script_is_never_overwritten_by_the_backfill() -> None:
    facts = _fake_facts(original_script="服务商给出的完整口播")
    facts["shots"][0]["spoken_text"] = "分段一"
    facts["shots"][1]["spoken_text"] = "分段二"

    result = _analyze_with_facts(facts, duration_seconds=8)

    assert result.analysis.original_script == "服务商给出的完整口播"


def test_silent_source_keeps_an_empty_original_script() -> None:
    """无口播的源片不得被回填成假文案。"""
    facts = _fake_facts(original_script="")
    for shot in facts["shots"]:
        shot["spoken_text"] = ""

    result = _analyze_with_facts(facts, duration_seconds=8)

    assert result.analysis.original_script == ""


# --- 拆解侧动态义务：并列主态与「不得默认静止」 --------------------------------


def test_compound_subject_motion_state_is_accepted() -> None:
    """subject_motion_state 允许并列主态（边走边做手势）。"""
    from app.analysis import ShotMotion

    motion = ShotMotion(
        subject_motion_state="WALKING+GESTURING_ONLY",
        subject_direction="toward_camera",
        subject_displacement="向镜头走近两三步",
        hand_action="双臂随步态摆动并在胸前做讲解手势",
        camera_motion="HANDHELD_TRACKING",
        relative_motion="人物逐渐靠近镜头，画面占比增大",
    )

    assert motion.subject_motion_state == "WALKING+GESTURING_ONLY"


@pytest.mark.parametrize("bad", ["FLYING", "WALKING+FLYING", "WALKING+WALKING", ""])
def test_compound_motion_state_does_not_relax_the_enum(bad: str) -> None:
    """并列主态只放宽「可并列」，未知分量、重复分量与空值仍必须失败。"""
    from app.analysis import ShotMotion

    with pytest.raises(ValidationError):
        ShotMotion(
            subject_motion_state=bad,
            subject_direction="in_place",
            subject_displacement="无位移",
            hand_action="无",
            camera_motion="STATIC",
            relative_motion="无相对运动",
        )


def test_analysis_rules_make_displacement_the_first_class_output() -> None:
    """拆解侧规则：位移是首要产出，边界不得默认静止，运动转换应当分段。"""
    from pathlib import Path

    rules = (
        Path(__file__).resolve().parents[1] / "app" / "prompt_rules" / "analysis.txt"
    ).read_text(encoding="utf-8")

    assert "首要产出" in rules
    assert "不得默认" in rules and "静止" in rules
    assert "应当分段" in rules
    assert "WALKING+GESTURING_ONLY" in rules


def test_legacy_analysis_instruction_carries_the_same_dynamic_obligation() -> None:
    """另一条 instruction 路径同样不得默认静止，否则换个 provider 就退回老行为。"""
    from app.analysis import analysis_instruction

    instruction = analysis_instruction(15)

    assert "首要产出" in instruction
    assert "WALKING+GESTURING_ONLY" in instruction


# --- 合成侧：动作+motion 编译成叙事句，置于静态字段之前 ------------------------


_MOTION_SHOT = {
    "start_time": 0,
    "end_time": 4,
    "segment_kind": "ACTION_BEAT",
    "shot_type": "中景",
    "composition": "人物居中",
    "action": "拿起文件夹后走向镜头",
    "motion": {
        "subject_motion_state": "WALKING",
        "subject_direction": "toward_camera",
        "subject_displacement": "向镜头走近两三步",
        "hand_action": "双臂随步态自然摆动",
        "camera_motion": "PULL_BACK",
        "relative_motion": "人物逐渐靠近镜头，画面占比增大",
    },
}


def test_action_and_motion_compile_into_a_narrative_before_static_fields() -> None:
    """动作+motion 合成官方指南式叙事句（起点→连续发展→结束），放在静态字段之前。"""
    from app.h3_prompts import compile_replica_final_text

    text = compile_replica_final_text(
        shot_payload={"shots": [dict(_MOTION_SHOT)]},
        script_text="",
        duration=4,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    narrative = (
        "动作与运动：拿起文件夹后走向镜头；"
        "人物朝镜头方向行走，向镜头走近两三步，双臂随步态自然摆动；"
        "镜头拉远，人物逐渐靠近镜头，画面占比增大"
    )
    assert narrative in text
    # 模型先读到「人物在动」，再读到景别/构图等静态参数。
    assert text.index(narrative) < text.index("shot_type: 中景")
    # 裸枚举不是官方指南写法：正文用中文叙事，不再泄漏枚举字面量。
    assert "WALKING" not in text
    assert "PULL_BACK" not in text and "toward_camera" not in text
    assert not prompt_issues(text, mode="I2VA", duration=4, labels=["<Picture 1>"])


def test_compound_motion_state_keeps_both_clauses() -> None:
    """并列主态在合成侧不得只留其一。"""
    from app.h3_prompts import compile_replica_final_text

    text = compile_replica_final_text(
        shot_payload={
            "shots": [
                {
                    "start_time": 0,
                    "end_time": 4,
                    "segment_kind": "ACTION_BEAT",
                    "action": "边走边讲解",
                    "motion": {
                        "subject_motion_state": "WALKING+GESTURING_ONLY",
                        "subject_direction": "toward_camera",
                        "camera_motion": "HANDHELD_TRACKING",
                    },
                }
            ]
        },
        script_text="",
        duration=4,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    assert "朝镜头方向行走" in text
    assert "仅做手势" in text


def test_legacy_shot_without_motion_is_warned_not_silently_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """旧拆解结果缺 motion 时合成必须留痕，否则静默产出「偏静止」提示词无人知晓。"""
    import logging

    from app.h3_prompts import compile_replica_final_text

    with caplog.at_level(logging.WARNING, logger="app.h3_prompts"):
        text = compile_replica_final_text(
            shot_payload={
                "shots": [
                    {
                        "start_time": 0,
                        "end_time": 2,
                        "segment_kind": "ACTION_BEAT",
                        "action": "抬手",
                    },
                    {"start_time": 2, "end_time": 4, "segment_kind": "ACTION_BEAT"},
                ]
            },
            script_text="",
            duration=4,
            source_duration=4,
            timeline_policy="preserve",
            source_frame_time=0,
        )

    assert "动作与运动：抬手" in text
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, "每次合成只给一条警告，列出全部缺 motion 的段"
    message = warnings[0].getMessage()
    assert "motion" in message
    assert "s2" in message


def test_short_source_fills_with_continuous_action_not_uniform_slow_motion() -> None:
    """源片短于目标时长时用同风格连续动作自然填满，不再一律「等比放慢」。"""
    from app.h3_prompts import compile_replica_final_text

    text = compile_replica_final_text(
        shot_payload={"shots": [{"start_time": 0, "end_time": 4, "segment_kind": "ACTION_BEAT"}]},
        script_text="",
        duration=10,
        source_duration=4,
        timeline_policy="preserve",
        source_frame_time=0,
    )

    assert "同风格的连续动作" in text
    assert "不新增剧情事件" in text
    assert "等比放慢" not in text
