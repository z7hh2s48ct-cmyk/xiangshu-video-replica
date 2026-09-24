import unittest

from scripts.compose_prompt import PromptBuildError, compose_prompt
from scripts.contracts import REQUIRED_PROMPT_SECTIONS, VILLA_VISUAL_CHECKS
from scripts.create_qa_template import create_template


def analysis():
    return {
        "version": "0.6",
        "duration": 6.0,
        "aspect_ratio": "9:16",
        "first_shot": "人物居中站在乡墅入口",
        "three_storey_rural_villa": False,
        "visual_style": "真实手机拍摄质感",
        "character_identity": "保持首帧中的同一人物身份、脸部、年龄感和体型",
        "character_costume": "保持参考视频或可选人物参考图确定的服饰连续一致",
        "character_reference_mode": "SOURCE_OR_OPTIONAL_REFERENCE",
        "shots": [
            {
                "index": 1,
                "start": 0.0,
                "end": 3.0,
                "scene": "乡墅入口",
                "subject_motion": "人物自然走向镜头",
                "camera_motion": "摄影机同步后退",
                "relative_motion": "人物大小基本稳定",
                "character_visible": True,
                "motion_observation": {
                    "source": "REFERENCE_VIDEO",
                    "state": "WALKING",
                    "confidence": "HIGH",
                    "evidence_timestamps": [0.0, 1.5, 2.9],
                    "start_pose": "人物从远处正面迈步",
                    "trajectory": "沿中轴线向摄影机靠近",
                    "body_mechanics": "双腿交替迈步并真实转移重心",
                    "hand_action": "双臂随步伐自然反向摆动，无额外手势",
                    "end_pose": "继续面向镜头行走",
                    "action_phases": [
                        {
                            "start": 0.0,
                            "end": 3.0,
                            "body_action": "持续自然向前行走",
                            "hand_action": "双臂随步伐反向摆动",
                        }
                    ],
                },
                "sound_role": "ON_CAMERA_SPEECH",
                "spoken_text": "第一句话。",
                "lip_sync_required": True,
            },
            {
                "index": 2,
                "start": 3.0,
                "end": 6.0,
                "scene": "建筑外观",
                "subject_motion": "建筑静止",
                "camera_motion": "摄影机缓慢右移",
                "relative_motion": "逐渐露出庭院",
                "character_visible": False,
                "sound_role": "VOICE_OVER",
                "spoken_text": "第二句话。",
                "lip_sync_required": False,
            },
        ],
    }


class PromptTests(unittest.TestCase):
    def test_prompt_contains_all_sections_and_shots(self):
        prompt = compose_prompt(analysis())
        for section in REQUIRED_PROMPT_SECTIONS:
            self.assertIn("## %s" % section, prompt)
        for value in ["0.000–3.000秒", "3.000–6.000秒", "人物动作", "摄影机运动"]:
            self.assertIn(value, prompt)

    def test_prompt_contains_spoken_text_and_roles(self):
        prompt = compose_prompt(analysis())
        for value in ["第一句话。", "第二句话。", "ON_CAMERA_SPEECH", "VOICE_OVER", "口型同步"]:
            self.assertIn(value, prompt)

    def test_visible_character_motion_is_rendered_from_reference_observation(self):
        prompt = compose_prompt(analysis())
        for value in [
            "动作来源：参考视频连续帧",
            "动作状态：WALKING",
            "沿中轴线向摄影机靠近",
            "双臂随步伐自然反向摆动",
            "0.000–3.000秒",
        ]:
            self.assertIn(value, prompt)

    def test_stationary_reference_does_not_invent_walking_or_gesture(self):
        value = analysis()
        motion = value["shots"][0]["motion_observation"]
        motion.update(
            {
                "state": "STATIONARY",
                "start_pose": "人物原地正面站立",
                "trajectory": "人物位置与尺度保持稳定",
                "body_mechanics": "只有参考视频中可见的呼吸和眨眼",
                "hand_action": "双手自然垂落，无主动手势",
                "end_pose": "人物仍在原位",
                "action_phases": [
                    {
                        "start": 0.0,
                        "end": 3.0,
                        "body_action": "保持原地口播",
                        "hand_action": "无主动手势",
                    }
                ],
            }
        )
        prompt = compose_prompt(value)
        self.assertIn("动作状态：STATIONARY", prompt)
        self.assertIn("双手自然垂落，无主动手势", prompt)
        shot_text = prompt.split("### 0.000–3.000秒", 1)[1].split("### 3.000–6.000秒", 1)[0]
        self.assertNotIn("人物自然走向镜头", shot_text)
        self.assertNotIn("走两步", shot_text)

    def test_visible_character_requires_motion_observation(self):
        value = analysis()
        del value["shots"][0]["motion_observation"]
        with self.assertRaisesRegex(PromptBuildError, "motion_observation"):
            compose_prompt(value)

    def test_motion_requires_multiple_reference_timestamps(self):
        value = analysis()
        value["shots"][0]["motion_observation"]["evidence_timestamps"] = [0.0]
        with self.assertRaisesRegex(PromptBuildError, "evidence_timestamps"):
            compose_prompt(value)

    def test_motion_phases_must_cover_the_visible_character_shot(self):
        value = analysis()
        value["shots"][0]["motion_observation"]["action_phases"] = [
            {
                "start": 0.0,
                "end": 1.0,
                "body_action": "先迈出一步",
                "hand_action": "双臂自然摆动",
            }
        ]
        with self.assertRaisesRegex(PromptBuildError, "cover the full shot"):
            compose_prompt(value)

    def test_motion_state_comes_from_analysis_instead_of_a_default(self):
        value = analysis()
        value["shots"][0]["motion_observation"]["state"] = "TURNING"
        value["shots"][0]["motion_observation"]["trajectory"] = "原地向左转身约四十五度"
        prompt = compose_prompt(value)
        self.assertIn("动作状态：TURNING", prompt)
        self.assertIn("原地向左转身约四十五度", prompt)

    def test_empty_voice_over_segment_is_marked_as_continuation(self):
        value = analysis()
        value["shots"][1]["spoken_text"] = ""
        prompt = compose_prompt(value)
        self.assertIn("延续上一镜头同一句话", prompt)

    def test_prompt_contains_all_text_bans(self):
        prompt = compose_prompt(analysis())
        for value in ["字幕", "水印", "招牌", "春联", "灯笼文字", "平台 UI"]:
            self.assertIn(value, prompt)

    def test_empty_shots_fail(self):
        value = analysis()
        value["shots"] = []
        with self.assertRaises(PromptBuildError):
            compose_prompt(value)

    def test_missing_motion_field_fails(self):
        value = analysis()
        del value["shots"][0]["relative_motion"]
        with self.assertRaisesRegex(PromptBuildError, "relative_motion"):
            compose_prompt(value)

    def test_invalid_timing_fails(self):
        value = analysis()
        value["shots"][0]["end"] = 0
        with self.assertRaisesRegex(PromptBuildError, "after start"):
            compose_prompt(value)


class QATemplateTests(unittest.TestCase):
    def test_template_never_pretends_qa_ran(self):
        qa = create_template(False, 1, "a" * 64)
        self.assertEqual(qa["overall"], "NOT_RUN")
        self.assertTrue(all(value == "NOT_RUN" for value in qa["checks"].values()))

    def test_villa_template_adds_all_quality_checks(self):
        qa = create_template(True, 1, "a" * 64)
        self.assertTrue(VILLA_VISUAL_CHECKS.issubset(qa["checks"]))

    def test_non_villa_template_does_not_add_villa_checks(self):
        qa = create_template(False, 1, "a" * 64)
        self.assertTrue(VILLA_VISUAL_CHECKS.isdisjoint(qa["checks"]))


if __name__ == "__main__":
    unittest.main()
