from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.analysis import ApilioGemini, FakeGemini, analyze_video
from app.source_frames import (
    FFmpegSceneBoundaryDetector,
    SceneBoundaryDetectionFailed,
)


def _make_video(path: Path, *, hard_cut: bool) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    if hard_cut:
        source = (
            "color=c=red:s=160x120:d=1:r=12[a];"
            "color=c=blue:s=160x120:d=1:r=12[b];"
            "[a][b]concat=n=2:v=1:a=0,format=yuv420p"
        )
    else:
        source = "color=c=red:s=160x120:d=2:r=12,format=yuv420p"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", source, "-y", str(path)],
        check=True,
        capture_output=True,
        timeout=15,
    )


def test_scene_boundary_detector_finds_a_short_hard_cut(tmp_path: Path) -> None:
    video = tmp_path / "cut.mp4"
    _make_video(video, hard_cut=True)

    result = FFmpegSceneBoundaryDetector().detect(
        video.read_bytes(), filename=video.name, duration_seconds=2
    )

    assert len(result) == 1
    assert result[0] == pytest.approx(1, abs=0.15)


def test_scene_boundary_detector_does_not_invent_cuts_for_a_static_video(tmp_path: Path) -> None:
    video = tmp_path / "static.mp4"
    _make_video(video, hard_cut=False)

    assert (
        FFmpegSceneBoundaryDetector().detect(
            video.read_bytes(), filename=video.name, duration_seconds=2
        )
        == ()
    )


def test_scene_boundary_detector_fails_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, b"", b"bad input"),
    )

    with pytest.raises(SceneBoundaryDetectionFailed, match="could not detect"):
        FFmpegSceneBoundaryDetector().detect(b"video", filename="source.mp4", duration_seconds=2)


def test_scene_boundary_detector_bounds_fast_cuts_while_covering_the_full_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    stderr = "\n".join(f"showinfo pts_time:{index / 10:.1f}" for index in range(1, 31))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"", stderr.encode()),
    )

    result = FFmpegSceneBoundaryDetector().detect(
        b"video", filename="source.mp4", duration_seconds=4
    )

    assert len(result) == 24
    assert result[0] == 0.1
    assert result[-1] == 3.0


def test_analysis_prompt_treats_candidate_times_as_review_hints() -> None:
    facts = json.loads(FakeGemini().analyze(video_uri="fake", duration_seconds=2).text)

    class Transport:
        request: dict | None = None

        def post(self, url, *, headers, body):
            self.request = json.loads(body)
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

    transport = Transport()
    analyze_video(
        video_uri="https://example.test/source.mp4",
        video_duration_seconds=2,
        provider=ApilioGemini(api_key="test", transport=transport),
        analysis_guidance={
            "status": "AVAILABLE",
            "candidate_cut_times_seconds": [1.0],
            "message": "candidate only",
        },
    )

    assert transport.request is not None
    system_prompt = transport.request["messages"][0]["content"]
    assert '"candidate_cut_times_seconds": [1.0]' in system_prompt
    assert "候选时间不是真实切镜结论" in system_prompt
    assert transport.request["messages"][1]["content"][1]["image_url"]["url"].endswith("source.mp4")


def test_legacy_analysis_instruction_has_no_fixed_segment_count() -> None:
    from app.analysis import analysis_instruction

    instruction = analysis_instruction(15)
    assert "2-5" not in instruction
    assert "段数服从实际内容" in instruction


def test_generation_context_target_duration_does_not_enter_analysis_prompt() -> None:
    """拆解只描述源视频事实；目标生成时长由生成阶段 H3 API duration 参数承载，
    注入提示词前必须剥离，防止模型把目标时长当源时长或按目标时长凑段。"""
    captured: dict[str, object] = {}

    class Transport:
        def post(self, url, *, headers, body):
            captured["request"] = json.loads(body)
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode(), {}

    ApilioGemini(api_key="test", transport=Transport()).analyze_with_context(
        video_uri="https://example.test/source.mp4",
        duration_seconds=12.0,
        context={
            "mode": "I2VA",
            "duration_seconds": 15,
            "generation_assets": [],
            "issues": [],
            "media_info": {"fps": 30},
        },
        media=[],
    )

    prompt = captured["request"]["messages"][0]["content"]
    # 源视频时长（媒体信息）保留；目标生成时长（生成上下文）不得出现。
    assert '"duration_seconds": 12.0' in prompt
    assert '"duration_seconds": 15' not in prompt


@pytest.mark.parametrize("source_size_bytes", [None, 50 * 1024 * 1024 + 1])
def test_unbounded_source_is_not_downloaded_and_degrades_to_video_review(
    caplog: pytest.LogCaptureFixture,
    source_size_bytes: int | None,
) -> None:
    from app.analysis_routes import (
        AnalysisTaskLease,
        AnalysisTaskWork,
        detect_scene_boundary_guidance,
    )

    class Storage:
        provider = "cos"
        bucket = "source-bucket"
        reads = 0

        def iter_object(self, key: str):
            self.reads += 1
            yield b"video"

    storage = Storage()
    work = AnalysisTaskWork(
        lease=AnalysisTaskLease(
            id="task",
            project_id="project",
            asset_id="asset",
            created_by_user_id="user",
            duration_seconds=2,
            worker_id="worker",
        ),
        provider=ApilioGemini(api_key="test"),
        video_uri="https://example.test/source.mp4",
        asset_uri="cos://source-bucket/source.mp4",
        storage=storage,  # type: ignore[arg-type]
        source_size_bytes=source_size_bytes,
    )

    with caplog.at_level("WARNING"):
        guidance = detect_scene_boundary_guidance(work, duration_seconds=2)

    assert guidance == {
        "status": "UNAVAILABLE",
        "candidate_cut_times_seconds": [],
        "message": "本地切镜候选检测失败；请直接核对完整视频。",
    }
    assert storage.reads == 0
    assert "project=project asset=asset" in caplog.text
    assert "UploadedObjectSizeMismatch" in caplog.text
