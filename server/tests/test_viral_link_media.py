"""Media preflight regressions: M4A, Windows decoder access and safe DNS errors."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app import media, media_tools, viral_import_routes
from app.media import VideoMetadata
from app.viral_import_routes import validate_resolved_media_content
from app.viral_link import (
    DouyidouHttpTransport,
    DouyidouLinkClient,
    ResolvedViralLink,
    ViralLinkError,
)
from app.viral_media import ViralMediaDNSUnavailable, ViralMediaResult


@pytest.fixture
def generated_aspect_media(tmp_path: Path) -> dict[str, Path]:
    """Small real MP4 fixtures exercise display pixels, rotation and copied audio."""
    ffmpeg = media_tools.resolve_media_binary("ffmpeg")
    paths = {name: tmp_path / f"{name}.mp4" for name in ("anamorphic", "square", "wide", "rotated")}
    for name, size, sar, audio in (
        ("anamorphic", "144x256", "64/63", True),
        ("square", "144x256", "1", False),
        ("wide", "256x144", "1", False),
    ):
        command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=24"]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000"]
        command += [
            "-t",
            "0.5",
            "-vf",
            f"setsar={sar}",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(paths[name]),
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=30)
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-display_rotation",
            "90",
            "-i",
            str(paths["wide"]),
            "-c",
            "copy",
            str(paths["rotated"]),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return paths


def _normalized_probe(path: Path, *, audio_packets: bool = False) -> dict[str, Any]:
    command = [media_tools.resolve_media_binary("ffprobe"), "-v", "error"]
    command += (
        [
            "-select_streams",
            "a:0",
            "-show_packets",
            "-show_data_hash",
            "sha256",
            "-show_entries",
            "packet=pts,dts,duration,size,data_hash",
        ]
        if audio_packets
        else ["-show_streams", "-show_format"]
    )
    return json.loads(
        subprocess.run(
            [*command, "-of", "json", str(path)], check=True, capture_output=True, timeout=30
        ).stdout
    )


def test_generated_video_normalizes_display_shape_and_copies_audio(
    generated_aspect_media: dict[str, Path], tmp_path: Path
) -> None:
    original = generated_aspect_media["anamorphic"].read_bytes()
    result = media_tools.normalize_generated_video(original, target_width=144, target_height=256)
    target = tmp_path / "normalized.mp4"
    target.write_bytes(result.content)
    assert result.transformed is True
    assert (result.width, result.height) == (144, 256)
    assert result.source_sample_aspect_ratio == "64:63"
    assert result.source_display_aspect_ratio == "4:7"
    assert result.sample_aspect_ratio == "1:1"
    assert result.display_aspect_ratio == "9:16"
    assert result.duration_seconds == pytest.approx(0.5)
    assert (
        _normalized_probe(target, audio_packets=True)["packets"]
        == _normalized_probe(generated_aspect_media["anamorphic"], audio_packets=True)["packets"]
    )
    # The 4:7 active picture becomes 144x252 with two black rows on each side.
    pixels = subprocess.run(
        [
            media_tools.resolve_media_binary("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(target),
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    for border in (pixels[: 144 * 2 * 3], pixels[-144 * 2 * 3 :]):
        # Lossy H.264 can ring at the colored picture/black-bar boundary.
        assert max(border) < 32 and sum(border) / len(border) < 3
    assert max(pixels[144 * 128 * 3 : 144 * 129 * 3]) > 100
    assert generated_aspect_media["anamorphic"].read_bytes() == original


def test_generated_video_preserves_valid_bytes_and_accepts_no_audio(
    generated_aspect_media: dict[str, Path],
) -> None:
    original = generated_aspect_media["square"].read_bytes()
    result = media_tools.normalize_generated_video(original, target_width=144, target_height=256)
    assert result.content == original
    assert result.transformed is False


def test_generated_video_applies_rotation_before_fitting(
    generated_aspect_media: dict[str, Path], tmp_path: Path
) -> None:
    result = media_tools.normalize_generated_video(
        generated_aspect_media["rotated"].read_bytes(), target_width=144, target_height=256
    )
    assert result.transformed is True
    assert abs(result.source_rotation_degrees) == 90
    assert result.source_display_aspect_ratio == "9:16"
    target = tmp_path / "rotated-normalized.mp4"
    target.write_bytes(result.content)
    video = _normalized_probe(target)["streams"][0]
    assert (video["width"], video["height"], video["sample_aspect_ratio"]) == (144, 256, "1:1")
    assert all(side.get("rotation", 0) == 0 for side in video.get("side_data_list", []))


@pytest.mark.parametrize(
    "width,height", [(0, 256), (143, 256), (144, 257), (8192, 8192), (True, 256)]
)
def test_generated_video_rejects_invalid_target_dimensions(width: int, height: int) -> None:
    with pytest.raises(media_tools.MediaValidationFailed):
        media_tools.normalize_generated_video(b"invalid", target_width=width, target_height=height)


@pytest.mark.parametrize("content", [b"", b"not video", b"\x00\x00\x00\x18ftypisom"])
def test_generated_video_rejects_invalid_input(content: bytes) -> None:
    with pytest.raises(media_tools.MediaValidationFailed):
        media_tools.normalize_generated_video(content, target_width=144, target_height=256)


def test_generated_video_rejects_truncation_even_when_metadata_survives(
    generated_aspect_media: dict[str, Path],
) -> None:
    original = generated_aspect_media["square"].read_bytes()
    with pytest.raises(media_tools.MediaValidationFailed):
        media_tools.normalize_generated_video(original[:-500], target_width=144, target_height=256)


def test_generated_video_timeout_is_redacted_and_temp_files_are_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media_tools, "resolve_media_binary", lambda tool: tool)
    monkeypatch.setattr(media_tools.tempfile, "tempdir", str(tmp_path))

    def timeout(command: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("private/path?private=example", 1)

    monkeypatch.setattr(media_tools.subprocess, "run", timeout)
    with pytest.raises(media_tools.MediaToolFailed) as exc:
        media_tools.normalize_generated_video(
            b"\x00\x00\x00\x18ftypisom", target_width=144, target_height=256
        )
    assert "private" not in str(exc.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration", "NaN"),
        ("duration", "61"),
        ("width", 16384),
        ("sample_aspect_ratio", "-1:1"),
        ("sample_aspect_ratio", "1:0"),
        ("rotation", 45),
    ],
)
def test_generated_video_rejects_unbounded_or_invalid_metadata_before_decode(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    payload: dict[str, Any] = {
        "format": {"duration": "1"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 144,
                "height": 256,
                "sample_aspect_ratio": "1:1",
            }
        ],
    }
    if field == "duration":
        payload["format"][field] = value
    elif field == "rotation":
        payload["streams"][0]["side_data_list"] = [{"rotation": value}]
    else:
        payload["streams"][0][field] = value
    calls: list[str] = []
    monkeypatch.setattr(media_tools, "resolve_media_binary", lambda tool: tool)

    def probe(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[0])
        return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode())

    monkeypatch.setattr(media_tools.subprocess, "run", probe)
    with pytest.raises(media_tools.MediaValidationFailed):
        media_tools.normalize_generated_video(
            b"\x00\x00\x00\x18ftypisom", target_width=144, target_height=256
        )
    assert calls == ["ffprobe"]


def test_generated_video_size_limit_precedes_tool_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_tools, "GENERATED_VIDEO_MAX_BYTES", 8)
    with pytest.raises(media_tools.MediaValidationFailed):
        media_tools.normalize_generated_video(
            b"\x00\x00\x00\x18ftypisom", target_width=144, target_height=256
        )


def test_generated_video_missing_tool_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(tool: str) -> str:
        raise media_tools.MediaToolUnavailable("未找到媒体工具")

    monkeypatch.setattr(media_tools, "resolve_media_binary", unavailable)
    with pytest.raises(media_tools.MediaToolUnavailable):
        media_tools.normalize_generated_video(
            b"\x00\x00\x00\x18ftypisom", target_width=144, target_height=256
        )


def test_link_preflight_accepts_mp4_container_audio() -> None:
    names: list[str] = []

    class Probe:
        def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
            names.append(filename)
            return VideoMetadata(duration_seconds=30)

    validate_resolved_media_content(
        b"\x00\x00\x00\x18ftypM4A ",
        kind="audio",
        content_type="audio/mp4",
        probe=Probe(),
        purpose="replica",
    )
    assert names == ["source.m4a"]


def test_ffprobe_input_is_readable_by_an_external_process_and_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda _: sys.executable)
    monkeypatch.setattr(media.tempfile, "tempdir", str(tmp_path))
    run = subprocess.run
    source_paths: list[Path] = []

    def decoder(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        source = Path(command[-1])
        source_paths.append(source)
        child = run(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; "
                "assert pathlib.Path(sys.argv[1]).read_bytes() == b'media-bytes'",
                str(source),
            ],
            **kwargs,
        )
        assert child.returncode == 0, "decoder could not read its input while probing"
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"format": {"duration": "12.5"}}), ""
        )

    monkeypatch.setattr(media.subprocess, "run", decoder)
    assert (
        media.FFprobeVideoProbe().probe(b"media-bytes", filename="source.mp4").duration_seconds
        == 12.5
    )
    assert source_paths and all(not path.exists() for path in source_paths)


def test_preflight_dns_failure_has_actionable_redacted_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipeline:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self, *args: Any, **kwargs: Any) -> None:
            raise ViralMediaDNSUnavailable("private-url?token=do-not-disclose")

    monkeypatch.setattr(viral_import_routes, "ViralMediaPipeline", Pipeline)
    resolved = ResolvedViralLink(
        platform="douyin",
        video_id="7672703482771972081",
        title="test",
        author="",
        cover_url=None,
        video_url="https://cdn.example/video.mp4",
        audio_url=None,
        duration_ms=0,
        source_description="",
    )
    with pytest.raises(ViralLinkError) as result:
        viral_import_routes.preflight_resolved_media(resolved, purpose="copy", storage=None)
    assert result.value.status_code == 503
    assert result.value.code == "VIRAL_LINK_MEDIA_DNS_UNAVAILABLE"
    assert "DNS" in result.value.message
    assert "token" not in result.value.message


def test_copy_preflight_uses_video_audio_track_instead_of_resolver_music(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferences: list[str | None] = []

    class Pipeline:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self, video: Any, *, prefer: str | None = None) -> ViralMediaResult:
            preferences.append(prefer)
            return ViralMediaResult(
                kind="video",
                storage_uri="cos://media/video.mp4",
                url="https://media.example/video.mp4",
                size=100,
                content_type="video/mp4",
                cache_hit=False,
                sha256="hash",
            )

    monkeypatch.setattr(viral_import_routes, "ViralMediaPipeline", Pipeline)
    resolved = ResolvedViralLink(
        platform="douyin",
        video_id="7672703482771972081",
        title="spoken video",
        author="",
        cover_url=None,
        video_url="https://media.example/video.mp4",
        audio_url="https://media.example/background-music.m4a",
        duration_ms=10_000,
        source_description="",
    )
    viral_import_routes.preflight_resolved_media(
        resolved,
        purpose="copy",
        storage=None,  # type: ignore[arg-type]
    )
    assert preferences == ["video"]


def test_copy_resolution_rejects_audio_only_media() -> None:
    class Transport(DouyidouHttpTransport):
        def request(self, url: str, *, headers: Any) -> bytes:
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "aweme_id": "7672703482771972081",
                        "audio": ["https://media.example/background-music.m4a"],
                    },
                }
            ).encode()

    resolver = DouyidouLinkClient(app_id="test", app_secret="test", transport=Transport())
    with pytest.raises(ViralLinkError) as result:
        resolver.resolve(
            "https://www.douyin.com/jingxuan?modal_id=7672703482771972081",
            purpose="copy",
        )
    assert result.value.status_code == 422
    assert result.value.code == "VIRAL_LINK_MEDIA_MISSING"


# ── 15 秒上限：ffprobe 实测二次校验与链接解析入口拦截 ────────────────────


def test_link_preflight_rejects_video_over_duration_limit() -> None:
    """复刻（replica）用途下，视频实测时长超过 15 秒上限时，ffprobe 校验阶段必须明确报错。"""

    class Probe:
        def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
            return VideoMetadata(duration_seconds=81.0)

    with pytest.raises(ViralLinkError) as result:
        validate_resolved_media_content(
            b"\x00\x00\x00\x18ftypisom",
            kind="video",
            content_type="video/mp4",
            probe=Probe(),
            purpose="replica",
        )
    assert result.value.status_code == 422
    assert result.value.code == "VIRAL_LINK_MEDIA_DURATION_EXCEEDED"
    assert "15" in result.value.message


def test_link_preflight_allows_long_audio_sample() -> None:
    """口播声音样本（audio）不受 15 秒视频上限约束，不得误拦。"""

    class Probe:
        def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
            return VideoMetadata(duration_seconds=81.0)

    validate_resolved_media_content(
        b"\x00\x00\x00\x18ftypM4A ",
        kind="audio",
        content_type="audio/mp4",
        probe=Probe(),
        purpose="replica",
    )


def test_preflight_rejects_resolved_link_over_duration_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复刻（replica）用途下，链接解析已返回超长时长时，应在下载媒体前拦截，不浪费带宽。"""
    fetched: list[bool] = []

    class Pipeline:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self, *args: Any, **kwargs: Any) -> ViralMediaResult:
            fetched.append(True)
            raise AssertionError("超长视频应在下载媒体前被拦截")

    monkeypatch.setattr(viral_import_routes, "ViralMediaPipeline", Pipeline)
    resolved = ResolvedViralLink(
        platform="douyin",
        video_id="7672703482771972081",
        title="long video",
        author="",
        cover_url=None,
        video_url="https://media.example/video.mp4",
        audio_url=None,
        duration_ms=81083,
        source_description="",
    )
    with pytest.raises(ViralLinkError) as result:
        viral_import_routes.preflight_resolved_media(
            resolved,
            purpose="replica",
            storage=None,  # type: ignore[arg-type]
        )
    assert result.value.status_code == 422
    assert result.value.code == "VIRAL_LINK_DURATION_EXCEEDED"
    assert "15" in result.value.message
    assert fetched == []


def test_copy_preflight_allows_resolved_link_over_duration_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提取文案（copy）不受复刻的 15 秒上限约束：超长视频应照常下载原音轨。"""
    fetched: list[bool] = []

    class Pipeline:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self, video: Any, *, prefer: str | None = None) -> ViralMediaResult:
            fetched.append(True)
            return ViralMediaResult(
                kind="video",
                storage_uri="cos://media/video.mp4",
                url="https://media.example/video.mp4",
                size=100,
                content_type="video/mp4",
                cache_hit=False,
                sha256="hash",
            )

    monkeypatch.setattr(viral_import_routes, "ViralMediaPipeline", Pipeline)
    resolved = ResolvedViralLink(
        platform="douyin",
        video_id="7672703482771972081",
        title="long video for copy",
        author="",
        cover_url=None,
        video_url="https://media.example/video.mp4",
        audio_url=None,
        duration_ms=81083,
        source_description="",
    )
    viral_import_routes.preflight_resolved_media(
        resolved,
        purpose="copy",
        storage=None,  # type: ignore[arg-type]
    )
    assert fetched == [True]


def test_link_preflight_allows_long_video_for_copy() -> None:
    """提取文案（copy）用途下，超长视频不触发 15 秒上限误拦。"""

    class Probe:
        def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
            return VideoMetadata(duration_seconds=81.0)

    validate_resolved_media_content(
        b"\x00\x00\x00\x18ftypisom",
        kind="video",
        content_type="video/mp4",
        probe=Probe(),
        purpose="copy",
    )
