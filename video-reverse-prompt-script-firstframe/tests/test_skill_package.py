import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillPackageTests(unittest.TestCase):
    def test_skill_frontmatter_is_present(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertRegex(text, r"(?m)^name: video-reverse-prompt-script-firstframe$")
        self.assertRegex(text, r"(?m)^description: 输入一个参考视频")

    def test_host_workflow_is_explicit(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for phrase in ["宿主内置 ImageGen", "宿主多模态", "最多 3 次", "outputs/"]:
            self.assertIn(phrase, text)

    def test_skill_requires_adaptive_reference_motion(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for phrase in ["连续动作证据", "不得固定为走路", "左右手", "动作起始", "动作结束"]:
            self.assertIn(phrase, text)

    def test_prompt_builder_has_no_sample_specific_action_defaults(self):
        text = (ROOT / "scripts" / "compose_prompt.py").read_text(encoding="utf-8")
        for forbidden in ["走两步", "抬起右手", "右手食指"]:
            self.assertNotIn(forbidden, text)

    def test_scripts_do_not_call_external_openai_api(self):
        scripts = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted((ROOT / "scripts").glob("*.py"))
        ).lower()
        for forbidden in ["api.openai.com", "openai.images", "from openai", "import openai", "requests.post"]:
            self.assertNotIn(forbidden, scripts)

    def test_no_dependency_manifest_is_needed(self):
        for name in ["requirements.txt", "pyproject.toml", "package.json"]:
            self.assertFalse((ROOT / name).exists())

    def test_portable_skill_excludes_development_evidence(self):
        self.assertFalse((ROOT / "verification").exists())
        self.assertFalse((ROOT / "docs").exists())
        self.assertFalse((ROOT / "README.md").exists())
        self.assertFalse((ROOT / "CHANGELOG.md").exists())

    def test_agent_metadata_uses_explicit_skill_invocation(self):
        text = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$video-reverse-prompt-script-firstframe", text)

    def test_skill_root_has_no_runtime_runs(self):
        self.assertFalse((ROOT / "video_reverse_runs").exists())


if __name__ == "__main__":
    unittest.main()
