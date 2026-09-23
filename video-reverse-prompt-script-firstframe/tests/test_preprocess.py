import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.detect_scenes import parse_cut_times, parse_frame_times
from scripts.preprocess_video import (
    PreprocessError,
    _allocate_budgets,
    _keep_closest_frames,
    _sample_times,
    _scale_filter,
    _shot_segments,
    prepare_run,
)


class FramePlanningTests(unittest.TestCase):
    def test_scale_filter_caps_long_edge_without_upscaling(self):
        self.assertEqual(_scale_filter(720, 1280, 640), "scale=-2:640")
        self.assertEqual(_scale_filter(1920, 1080, 640), "scale=640:-2")
        self.assertNotIn("640", _scale_filter(360, 480, 640))

    def test_segments_cover_video_and_drop_tiny_cuts(self):
        self.assertEqual(
            _shot_segments([0.05, 3.0, 9.99], 10.0),
            [(0.0, 3.0), (3.0, 10.0)],
        )

    def test_frame_budget_never_exceeds_limit(self):
        segments = [(float(i), float(i + 1)) for i in range(30)]
        budgets = _allocate_budgets(segments, 60)
        self.assertLessEqual(sum(budgets), 60)
        self.assertTrue(all(value >= 1 for value in budgets))

    def test_more_shots_than_frames_still_honours_global_limit(self):
        segments = [(float(i), float(i + 1)) for i in range(100)]
        budgets = _allocate_budgets(segments, 60)
        self.assertEqual(sum(budgets), 60)
        self.assertEqual(len([value for value in budgets if value == 1]), 60)

    def test_sampling_marks_head_mid_tail(self):
        samples = _sample_times(0.0, 3.0, 3)
        self.assertEqual([role for _, role in samples], ["head", "mid", "tail"])

    def test_closest_frame_removes_adjacent_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / ("frame_%03d.jpg" % index) for index in range(4)]
            for path in paths:
                path.write_bytes(b"jpg")
            kept, times = _keep_closest_frames(
                paths, [0.04, 0.06, 1.49, 1.51], [0.05, 1.5]
            )
            self.assertEqual(len(kept), 2)
            self.assertEqual(len(times), 2)
            self.assertEqual(len(list(Path(tmp).glob("*.jpg"))), 2)

    def test_scene_and_frame_timestamp_parsers(self):
        raw = "pts_time:2.000 pts_time:1.000 pts_time:2.000"
        self.assertEqual(parse_frame_times(raw), [2.0, 1.0, 2.0])
        self.assertEqual(parse_cut_times(raw), [1.0, 2.0])


class PreprocessTests(unittest.TestCase):
    def test_missing_video_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(PreprocessError, "does not exist"):
                prepare_run(Path(tmp) / "missing.mp4", Path(tmp) / "run")

    def test_non_video_extension_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "notes.txt"
            source.write_text("no", encoding="utf-8")
            with self.assertRaisesRegex(PreprocessError, "video file"):
                prepare_run(source, Path(tmp) / "run")

    def test_non_empty_outputs_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.mp4"
            source.write_bytes(b"video")
            outputs = Path(tmp) / "run" / "outputs"
            outputs.mkdir(parents=True)
            (outputs / "old.txt").write_text("old", encoding="utf-8")
            with self.assertRaisesRegex(PreprocessError, "empty"):
                prepare_run(source, Path(tmp) / "run")

    @patch("scripts.preprocess_video.subprocess.run")
    def test_prepare_run_creates_only_internal_evidence(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.mp4"
            source.write_bytes(b"video")
            run.side_effect = self._fake_subprocess
            run_dir = Path(tmp) / "run"
            result = prepare_run(source, run_dir, max_frames=8)
            self.assertEqual(list((run_dir / "outputs").iterdir()), [])
            self.assertTrue((run_dir / "internal" / "debug" / "source_first_frame.png").exists())
            self.assertTrue((run_dir / "internal" / "debug" / "shot_segments.json").exists())
            manifest = json.loads((run_dir / "internal" / "debug" / "run_manifest.json").read_text())
            self.assertEqual(manifest["video_input_count"], 1)
            self.assertEqual(result["status"], "PREPARED")

    @staticmethod
    def _fake_subprocess(command, **kwargs):
        if "ffprobe" in command[0]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "format": {"duration": "15.0"},
                        "streams": [
                            {"codec_type": "video", "width": 720, "height": 1280, "r_frame_rate": "30/1"},
                            {"codec_type": "audio", "codec_name": "aac"},
                        ],
                    }
                ),
                stderr="",
            )
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        if "%03d" in output.name:
            for index in range(1, 5):
                (output.parent / f"frame_{index:03d}.jpg").write_bytes(b"jpg")
        else:
            output.write_bytes(b"artifact")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


if __name__ == "__main__":
    unittest.main()
