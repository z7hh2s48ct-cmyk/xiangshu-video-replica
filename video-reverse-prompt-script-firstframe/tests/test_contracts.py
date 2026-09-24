import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from scripts.contracts import (
    AnalysisValidationError,
    BASE_VISUAL_CHECKS,
    DELIVERY_FILES,
    VILLA_VISUAL_CHECKS,
    build_first_frame_request,
    orientation_of,
    required_visual_checks,
)
from scripts.compose_prompt import compose_prompt
from scripts.build_imagegen_request import regeneration_instructions
from scripts.release_gate import ReleaseGateError, validate_release


def write_png(path: Path, width: int = 720, height: int = 1280) -> None:
    def chunk(name: bytes, payload: bytes) -> bytes:
        body = name + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + (b"\xff\xff\xff" * width)
    raw = row * height
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def complete_analysis(villa: bool = False) -> dict:
    return {
        "version": "0.6",
        "duration": 3.0,
        "aspect_ratio": "9:16",
        "first_shot": "人物居中站在乡墅入口",
        "three_storey_rural_villa": villa,
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
                "subject_motion": "此字段不能覆盖结构化动作事实",
                "camera_motion": "摄影机基本固定",
                "relative_motion": "人物与摄影机相对位置稳定",
                "character_visible": True,
                "motion_observation": {
                    "source": "REFERENCE_VIDEO",
                    "state": "STATIONARY",
                    "confidence": "HIGH",
                    "evidence_timestamps": [0.0, 1.5, 2.9],
                    "start_pose": "人物原地正面站立",
                    "trajectory": "人物位置与尺度保持稳定",
                    "body_mechanics": "只有参考视频中可见的呼吸和眨眼",
                    "hand_action": "左右手自然垂落，无主动手势",
                    "end_pose": "人物仍在原位",
                    "action_phases": [
                        {
                            "start": 0.0,
                            "end": 3.0,
                            "body_action": "保持原地口播",
                            "hand_action": "左右手均无主动手势",
                        }
                    ],
                },
                "sound_role": "ON_CAMERA_SPEECH",
                "spoken_text": "你好。",
                "lip_sync_required": True,
            }
        ],
    }


def complete_prompt() -> str:
    return compose_prompt(complete_analysis())


def complete_qa(candidate_sha256: str, villa: bool = False) -> dict:
    checks = {name: "PASS" for name in required_visual_checks(villa)}
    return {
        "schema_version": "0.6",
        "attempt": 1,
        "candidate_sha256": candidate_sha256,
        "checks": checks,
        "detected_readable_text": [],
        "overall": "PASS",
        "regeneration_instructions": [],
    }


class ContractTests(unittest.TestCase):
    def test_previous_qa_regeneration_instructions_must_be_string_list(self):
        self.assertEqual(regeneration_instructions({"regeneration_instructions": ["修正手势"]}), ["修正手势"])
        for value in ["修正手势", [""], [1]]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "regeneration_instructions"):
                    regeneration_instructions({"regeneration_instructions": value})

    def test_delivery_contract_is_exactly_two_files(self):
        self.assertEqual(DELIVERY_FILES, {"first_frame.png", "image_to_video_prompt.md"})

    def test_villa_checks_are_conditional(self):
        self.assertTrue(VILLA_VISUAL_CHECKS.issubset(required_visual_checks(True)))
        self.assertTrue(VILLA_VISUAL_CHECKS.isdisjoint(required_visual_checks(False)))

    def test_image_request_uses_host_imagegen_and_no_api_key(self):
        request = build_first_frame_request(
            analysis=complete_analysis(),
            attempt=1,
            previous_failures=[],
        )
        text = json.dumps(request, ensure_ascii=False)
        self.assertEqual(request["provider"], "codex_host_imagegen")
        self.assertNotIn("OPENAI_API_KEY", text)
        self.assertNotIn("gpt-image", text.lower())

    def test_image_request_forbids_every_kind_of_readable_text(self):
        request = build_first_frame_request(
            analysis=complete_analysis(),
            attempt=1,
            previous_failures=[],
        )
        prompt = request["prompt"]
        for word in ["字幕", "水印", "招牌", "春联", "灯笼", "Logo", "UI"]:
            self.assertIn(word, prompt)

    def test_image_request_preserves_reference_motion_start_pose(self):
        analysis = complete_analysis()
        motion = analysis["shots"][0]["motion_observation"]
        motion["state"] = "WALKING"
        motion["start_pose"] = "左脚正在向前迈出，双臂自然错开"
        request = build_first_frame_request(
            analysis=analysis,
            attempt=1,
            previous_failures=[],
        )
        self.assertIn("左脚正在向前迈出，双臂自然错开", request["prompt"])
        self.assertIn("不得改成默认对称站姿", request["prompt"])

    def test_visual_qa_requires_motion_ready_pose_match(self):
        self.assertIn("motion_ready_pose_match", BASE_VISUAL_CHECKS)

    def test_image_request_rejects_visible_character_without_motion(self):
        analysis = complete_analysis()
        del analysis["shots"][0]["motion_observation"]
        with self.assertRaisesRegex(AnalysisValidationError, "motion_observation"):
            build_first_frame_request(analysis, attempt=1, previous_failures=[])

    def test_character_identity_cannot_contain_fixed_motion_policy(self):
        analysis = complete_analysis()
        analysis["character_identity"] = "同一人物，所有出镜镜头必须向前走两步并抬右手"
        with self.assertRaisesRegex(AnalysisValidationError, "identity or costume only"):
            build_first_frame_request(analysis, attempt=1, previous_failures=[])

    def test_character_costume_cannot_contain_fixed_motion_policy(self):
        analysis = complete_analysis()
        analysis["character_costume"] = "保持反光背心，每个镜头抬右手"
        with self.assertRaisesRegex(AnalysisValidationError, "identity or costume only"):
            build_first_frame_request(analysis, attempt=1, previous_failures=[])

    def test_character_reference_mode_must_be_supported(self):
        analysis = complete_analysis()
        analysis["character_reference_mode"] = "FORCE_PERSONA_AND_DEFAULT_ACTION"
        with self.assertRaisesRegex(AnalysisValidationError, "reference_mode"):
            build_first_frame_request(analysis, attempt=1, previous_failures=[])

    def test_villa_request_contains_quality_floor_and_anti_palace_rules(self):
        request = build_first_frame_request(
            analysis=complete_analysis(villa=True),
            attempt=1,
            previous_failures=[],
        )
        prompt = request["prompt"]
        for phrase in ["轻高端改善型乡墅", "建筑比例成熟", "立面材质", "庭院", "不能豪宅宫殿化", "CG"]:
            self.assertIn(phrase, prompt)

    def test_previous_qa_failures_are_added_to_regeneration_request(self):
        request = build_first_frame_request(
            analysis=complete_analysis(),
            attempt=2,
            previous_failures=["检测到春联文字"],
        )
        self.assertIn("检测到春联文字", request["prompt"])

    def test_orientation_classifier_supports_all_source_shapes(self):
        self.assertEqual(orientation_of(720, 1280), "PORTRAIT")
        self.assertEqual(orientation_of(1920, 1080), "LANDSCAPE")
        self.assertEqual(orientation_of(1000, 970), "SQUARE")

    def test_landscape_request_forbids_portrait_default(self):
        request = build_first_frame_request(
            complete_analysis(), 1, [], orientation="LANDSCAPE"
        )
        self.assertEqual(request["orientation"], "LANDSCAPE")
        self.assertIn("严禁按短视频惯例改成竖屏", request["prompt"])

    def test_more_than_three_generation_attempts_are_rejected(self):
        with self.assertRaisesRegex(AnalysisValidationError, "between 1 and 3"):
            build_first_frame_request(complete_analysis(), 4, [])


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name)
        self.outputs = self.run_dir / "outputs"
        self.internal = self.run_dir / "internal" / "debug"
        self.outputs.mkdir(parents=True)
        self.internal.mkdir(parents=True)
        write_png(self.outputs / "first_frame.png")
        self.image_sha256 = hashlib.sha256(
            (self.outputs / "first_frame.png").read_bytes()
        ).hexdigest()
        (self.outputs / "image_to_video_prompt.md").write_text(complete_prompt(), encoding="utf-8")
        (self.internal / "first_frame_visual_validation.json").write_text(
            json.dumps(complete_qa(self.image_sha256), ensure_ascii=False), encoding="utf-8"
        )
        (self.internal / "render_provenance.json").write_text(
            json.dumps(
                {
                    "host_capability": "codex_host_imagegen",
                    "direct_image_result": True,
                    "salvaged_from_composite": False,
                    "source_first_frame_used": True,
                    "candidate_sha256": self.image_sha256,
                    "candidate_filename": "attempt_01.png",
                    "attempt": 1,
                }
            ),
            encoding="utf-8",
        )
        (self.internal / "run_manifest.json").write_text(
            json.dumps(
                {
                    "version": "0.6",
                    "video_input_count": 1,
                    "three_storey_rural_villa": False,
                    "shots": [{"start": 0.0, "end": 3.0}],
                    "spoken_segments": [{"text": "你好。"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.internal / "video_analysis.json").write_text(
            json.dumps(complete_analysis(), ensure_ascii=False), encoding="utf-8"
        )
        (self.internal / "video_metadata.json").write_text(
            json.dumps({"width": 720, "height": 1280}), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def assert_gate_fails(self, message: str):
        with self.assertRaisesRegex(ReleaseGateError, message):
            validate_release(self.run_dir)

    def test_valid_release_passes(self):
        result = validate_release(self.run_dir)
        self.assertEqual(result["status"], "PASS")

    def test_extra_output_file_fails(self):
        (self.outputs / "README.md").write_text("extra", encoding="utf-8")
        self.assert_gate_fails("exactly")

    def test_extra_output_directory_fails(self):
        (self.outputs / "internal").mkdir()
        self.assert_gate_fails("exactly")

    def test_missing_first_frame_fails(self):
        (self.outputs / "first_frame.png").unlink()
        self.assert_gate_fails("first_frame")

    def test_missing_prompt_fails(self):
        (self.outputs / "image_to_video_prompt.md").unlink()
        self.assert_gate_fails("image_to_video_prompt")

    def test_invalid_png_fails(self):
        (self.outputs / "first_frame.png").write_bytes(b"not png")
        self.assert_gate_fails("PNG")

    def test_orientation_mismatch_fails(self):
        write_png(self.outputs / "first_frame.png", 1280, 720)
        self.assert_gate_fails("orientation")

    def test_landscape_source_and_frame_pass(self):
        write_png(self.outputs / "first_frame.png", 1280, 720)
        self.image_sha256 = hashlib.sha256(
            (self.outputs / "first_frame.png").read_bytes()
        ).hexdigest()
        (self.internal / "video_metadata.json").write_text(
            json.dumps({"width": 1920, "height": 1080}), encoding="utf-8"
        )
        self._write_qa(complete_qa(self.image_sha256))
        provenance = json.loads((self.internal / "render_provenance.json").read_text())
        provenance["candidate_sha256"] = self.image_sha256
        self._write_provenance(provenance)
        self.assertEqual(validate_release(self.run_dir)["orientation"], "LANDSCAPE")

    def test_missing_video_metadata_fails(self):
        (self.internal / "video_metadata.json").unlink()
        self.assert_gate_fails("video metadata")

    def test_extra_recorded_qa_failure_is_not_ignored(self):
        qa = complete_qa(self.image_sha256)
        qa["checks"]["host_added_check"] = "FAIL"
        self._write_qa(qa)
        self.assert_gate_fails("recorded visual QA")

    def test_not_run_is_not_pass(self):
        qa = complete_qa(self.image_sha256)
        qa["checks"]["text_free"] = "NOT_RUN"
        self._write_qa(qa)
        self.assert_gate_fails("explicit PASS")

    def test_detected_readable_text_is_hard_failure(self):
        qa = complete_qa(self.image_sha256)
        qa["detected_readable_text"] = ["春联：迎春接福"]
        self._write_qa(qa)
        self.assert_gate_fails("readable text")

    def test_overall_fail_is_rejected(self):
        qa = complete_qa(self.image_sha256)
        qa["overall"] = "FAIL"
        self._write_qa(qa)
        self.assert_gate_fails("overall")

    def test_missing_qa_file_fails(self):
        (self.internal / "first_frame_visual_validation.json").unlink()
        self.assert_gate_fails("visual QA")

    def test_missing_video_analysis_fails(self):
        (self.internal / "video_analysis.json").unlink()
        self.assert_gate_fails("video analysis")

    def test_prompt_must_match_video_analysis(self):
        prompt_path = self.outputs / "image_to_video_prompt.md"
        prompt = prompt_path.read_text(encoding="utf-8").replace(
            "人物位置与尺度保持稳定", "人物默认向前走两步"
        )
        prompt_path.write_text(prompt, encoding="utf-8")
        self.assert_gate_fails("video analysis")

    def test_qa_hash_must_match_released_image(self):
        qa = complete_qa("0" * 64)
        self._write_qa(qa)
        self.assert_gate_fails("QA image hash")

    def test_provenance_hash_must_match_released_image(self):
        provenance = json.loads((self.internal / "render_provenance.json").read_text())
        provenance["candidate_sha256"] = "0" * 64
        self._write_provenance(provenance)
        self.assert_gate_fails("provenance image hash")

    def test_external_api_provenance_fails(self):
        provenance = json.loads((self.internal / "render_provenance.json").read_text())
        provenance["host_capability"] = "openai_image_api"
        self._write_provenance(provenance)
        self.assert_gate_fails("host ImageGen")

    def test_salvaged_image_fails(self):
        provenance = json.loads((self.internal / "render_provenance.json").read_text())
        provenance["salvaged_from_composite"] = True
        self._write_provenance(provenance)
        self.assert_gate_fails("salvaged")

    def test_non_direct_image_fails(self):
        provenance = json.loads((self.internal / "render_provenance.json").read_text())
        provenance["direct_image_result"] = False
        self._write_provenance(provenance)
        self.assert_gate_fails("direct")

    def test_source_frame_not_used_fails(self):
        provenance = json.loads((self.internal / "render_provenance.json").read_text())
        provenance["source_first_frame_used"] = False
        self._write_provenance(provenance)
        self.assert_gate_fails("source first frame")

    def test_missing_prompt_section_fails(self):
        prompt = complete_prompt().replace("## 负面约束", "## 其它约束")
        (self.outputs / "image_to_video_prompt.md").write_text(prompt, encoding="utf-8")
        self.assert_gate_fails("负面约束")

    def test_missing_shot_range_fails(self):
        prompt = complete_prompt().replace("0.000–3.000秒", "镜头一")
        (self.outputs / "image_to_video_prompt.md").write_text(prompt, encoding="utf-8")
        self.assert_gate_fails("0.000")

    def test_missing_spoken_text_fails(self):
        prompt = complete_prompt().replace("你好。", "另一句话。")
        (self.outputs / "image_to_video_prompt.md").write_text(prompt, encoding="utf-8")
        self.assert_gate_fails("spoken")

    def test_missing_reference_motion_rule_fails(self):
        prompt = complete_prompt().replace(
            "人物动作严格来自参考视频连续帧",
            "人物动作使用默认模板",
        )
        (self.outputs / "image_to_video_prompt.md").write_text(prompt, encoding="utf-8")
        self.assert_gate_fails("reference motion")

    def test_villa_release_requires_all_villa_checks(self):
        self._set_villa()
        self.assert_gate_fails("villa")

    def test_villa_release_passes_with_all_villa_checks(self):
        self._set_villa()
        self._write_qa(complete_qa(self.image_sha256, villa=True))
        self.assertEqual(validate_release(self.run_dir)["status"], "PASS")

    def test_more_than_one_video_input_fails(self):
        manifest = json.loads((self.internal / "run_manifest.json").read_text())
        manifest["video_input_count"] = 2
        (self.internal / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_gate_fails("one video")

    def _write_qa(self, value: dict):
        (self.internal / "first_frame_visual_validation.json").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    def _write_provenance(self, value: dict):
        (self.internal / "render_provenance.json").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    def _set_villa(self):
        manifest_path = self.internal / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["three_storey_rural_villa"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        analysis_path = self.internal / "video_analysis.json"
        analysis = json.loads(analysis_path.read_text())
        analysis["three_storey_rural_villa"] = True
        analysis_path.write_text(json.dumps(analysis), encoding="utf-8")
        (self.outputs / "image_to_video_prompt.md").write_text(
            compose_prompt(analysis), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
