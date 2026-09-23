import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.verify_package import PackageValidationError, validate_package


class PackageArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "video-reverse-prompt-script-firstframe"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "SKILL.md").write_text("version 0.6", encoding="utf-8")
        (self.root / "scripts" / "contracts.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.archive = Path(self.temp.name) / "skill.zip"

    def tearDown(self):
        self.temp.cleanup()

    def _write_archive(self, stale: bool = False):
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("video-reverse-prompt-script-firstframe/SKILL.md", "version 0.5" if stale else "version 0.6")
            bundle.writestr("video-reverse-prompt-script-firstframe/scripts/contracts.py", "VALUE = 1\n")

    def test_exact_archive_passes(self):
        self._write_archive()
        self.assertEqual(validate_package(self.root, self.archive)["files"], 2)

    def test_installed_directory_may_have_a_stable_codex_skill_name(self):
        self._write_archive()
        renamed = self.root.with_name("video-reverse-prompt-script-firstframe")
        self.root.rename(renamed)
        self.assertEqual(validate_package(renamed, self.archive)["files"], 2)

    def test_stale_archive_content_fails(self):
        self._write_archive(stale=True)
        with self.assertRaisesRegex(PackageValidationError, "stale"):
            validate_package(self.root, self.archive)

    def test_runtime_artifact_in_archive_fails(self):
        self._write_archive()
        with zipfile.ZipFile(self.archive, "a") as bundle:
            bundle.writestr("video-reverse-prompt-script-firstframe/video_reverse_runs/run/log.json", "{}")
        with self.assertRaisesRegex(PackageValidationError, "extra"):
            validate_package(self.root, self.archive)

    def test_unknown_skill_root_file_is_rejected(self):
        (self.root / "debug.log").write_text("debug", encoding="utf-8")
        self._write_archive()
        with self.assertRaisesRegex(PackageValidationError, "unsupported Skill file"):
            validate_package(self.root, self.archive)


if __name__ == "__main__":
    unittest.main()
