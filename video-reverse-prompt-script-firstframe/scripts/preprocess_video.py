#!/usr/bin/env python3
"""Prepare deterministic video evidence; semantic analysis remains a Codex task."""

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    from .contracts import SCHEMA_VERSION
    from .detect_scenes import parse_cut_times, parse_frame_times, scene_filter
except ImportError:
    from contracts import SCHEMA_VERSION
    from detect_scenes import parse_cut_times, parse_frame_times, scene_filter


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

DEFAULT_MAX_FRAMES = 60
DEFAULT_LONG_EDGE = 640
CONTACT_SHEET_LONG_EDGE = 320
MIN_SHOT_SECONDS = 0.15
MAX_FRAMES_PER_SHOT = 8
SECONDS_PER_EXTRA_FRAME = 2.5


class PreprocessError(RuntimeError):
    pass


def _run(command: List[str]) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise PreprocessError("cannot execute %s: %s" % (command[0], exc))
    if result.returncode != 0:
        raise PreprocessError("%s failed: %s" % (command[0], result.stderr.strip()))
    return result


def _probe(video: Path) -> Dict[str, Any]:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(video),
        ]
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreprocessError("ffprobe returned invalid JSON: %s" % exc)


def _video_facts(probe: Dict[str, Any]) -> Dict[str, Any]:
    streams = probe.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video_stream:
        raise PreprocessError("input has no video stream")
    duration = float(probe.get("format", {}).get("duration") or video_stream.get("duration") or 0)
    if duration <= 0:
        raise PreprocessError("video duration is missing or invalid")
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise PreprocessError("video dimensions are missing")
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "aspect_ratio": "%.6f" % (width / height),
        "fps": video_stream.get("r_frame_rate", "unknown"),
        "video_codec": video_stream.get("codec_name", "unknown"),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def _scale_filter(width: int, height: int, long_edge: int) -> str:
    """Cap the long edge so motion stays readable at a fraction of the vision cost."""
    if max(width, height) <= long_edge:
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    return "scale=%d:-2" % long_edge if width >= height else "scale=-2:%d" % long_edge


def _shot_segments(cuts: List[float], duration: float) -> List[Tuple[float, float]]:
    """Turn detected cut points into contiguous shot ranges."""
    boundaries = [0.0]
    for cut in cuts:
        if cut - boundaries[-1] >= MIN_SHOT_SECONDS and duration - cut >= MIN_SHOT_SECONDS:
            boundaries.append(cut)
    boundaries.append(duration)
    return [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]


def _allocate_budgets(segments: List[Tuple[float, float]], max_frames: int) -> List[int]:
    """Three frames per shot by default; long shots get more, short ones never do."""
    count = len(segments)
    if count * 3 > max_frames:
        budgets = [0] * count
        # Spread scarce frames across the full timeline instead of keeping only
        # the first shots. This preserves the global cap even for 100+ cuts.
        for slot in range(max_frames):
            index = min(count - 1, int((slot + 0.5) * count / max_frames))
            budgets[index] += 1
        return budgets
    budgets = [
        max(3, min(MAX_FRAMES_PER_SHOT, int((end - start) / SECONDS_PER_EXTRA_FRAME) + 1))
        for start, end in segments
    ]
    while sum(budgets) > max_frames:
        widest = budgets.index(max(budgets))
        if budgets[widest] <= 3:
            break
        budgets[widest] -= 1
    return budgets


def _sample_times(start: float, end: float, budget: int) -> List[Tuple[float, str]]:
    """Head/mid/tail timestamps that map straight onto motion evidence_timestamps."""
    if budget <= 0:
        return []
    span = end - start
    if budget == 1 or span <= 0:
        return [(round((start + end) / 2, 3), "mid")]
    edge = min(0.05, span / 10)
    low, high = start + edge, end - edge
    if budget == 2:
        return [(round(low, 3), "head"), (round(high, 3), "tail")]
    step = (high - low) / (budget - 1)
    samples = []
    for index in range(budget):
        role = "head" if index == 0 else ("tail" if index == budget - 1 else "mid")
        samples.append((round(low + step * index, 3), role))
    return samples


def _parse_fps(raw: Any) -> float:
    try:
        text = str(raw)
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            value = float(numerator) / float(denominator)
        else:
            value = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return value if value > 0 else 0.0


def _extract_frames(
    video: Path, timestamps: List[float], frames: Path, scale: str, fps: float
) -> List[float]:
    """Pull every requested timestamp in ONE decode pass.

    Per-frame `-ss` seeking was measured 2.5x slower than a single pass on short
    clips because process startup dominates. `showinfo` reports the timestamp of
    each emitted frame, so the frame-to-shot mapping uses real times, never the
    requested ones, and a selection tolerance can never shift the mapping.
    """
    if not timestamps:
        return []
    tolerance = (1.0 / fps) if fps > 0 else 0.05
    terms = "+".join(
        "between(t\\,%.4f\\,%.4f)" % (max(0.0, value - tolerance), value + tolerance)
        for value in timestamps
    )
    result = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(video),
            "-vf",
            "select='%s',%s,showinfo" % (terms, scale),
            "-vsync",
            "0",
            str(frames / "frame_%03d.jpg"),
        ]
    )
    return parse_frame_times(result.stderr)


def _decode_once(video: Path, audio_path: Path, has_audio: bool) -> str:
    """One decode pass that yields both the cut points and the normalised audio."""
    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(video),
        "-filter:v",
        scene_filter(),
        "-an",
        "-f",
        "null",
        "-",
    ]
    if has_audio:
        command.extend(["-vn", "-ac", "1", "-ar", "16000", str(audio_path)])
    return _run(command).stderr


def _build_contact_sheet(frames: List[Path], target: Path, scale: str) -> bool:
    """Optional overview image; a failure here must never abort preprocessing.

    Every frame already shares one size, so the tiles need no padding — padding
    them into squares would spend ~44% of the sheet on black bars.
    """
    if not frames:
        return False
    columns = math.ceil(math.sqrt(len(frames)))
    rows = math.ceil(len(frames) / columns)
    listing = target.parent / "contact_sheet_inputs.txt"
    listing.write_text(
        "".join("file '%s'\n" % path.resolve() for path in frames), encoding="utf-8"
    )
    try:
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-vf",
                "%s,tile=%dx%d" % (scale, columns, rows),
                "-frames:v",
                "1",
                str(target),
            ]
        )
        return True
    except PreprocessError:
        return False
    finally:
        listing.unlink(missing_ok=True)


def _keep_closest_frames(
    frame_paths: List[Path], actual: List[float], planned: List[float]
) -> Tuple[List[Path], List[float]]:
    """Keep one frame per planned timestamp and delete the neighbours.

    The select tolerance must be wide enough to always hit a frame, which means it
    often hits two adjacent ones ~33ms apart. Those carry almost no extra motion
    information but double the vision cost, so they are dropped here.
    """
    if not frame_paths or len(frame_paths) != len(actual):
        return frame_paths, actual
    keep = sorted(
        {
            min(range(len(actual)), key=lambda index: abs(actual[index] - target))
            for target in planned
        }
    )
    kept = set(keep)
    for index, path in enumerate(frame_paths):
        if index not in kept:
            path.unlink(missing_ok=True)
    return [frame_paths[index] for index in keep], [actual[index] for index in keep]


def _shot_index_for(segments: List[Tuple[float, float]], timestamp: float) -> int:
    for index, (start, end) in enumerate(segments):
        if start <= timestamp < end:
            return index
    return len(segments) - 1


def _role_for(segment: Tuple[float, float], timestamp: float) -> str:
    start, end = segment
    span = (end - start) or 1.0
    ratio = (timestamp - start) / span
    if ratio <= 0.25:
        return "head"
    return "tail" if ratio >= 0.75 else "mid"


def _map_frames_to_shots(
    segments: List[Tuple[float, float]], frame_files: List[Path], times: List[float]
) -> List[Dict[str, Any]]:
    """Attach every extracted frame to the shot its real timestamp falls inside."""
    shots: List[Dict[str, Any]] = [
        {"index": index + 1, "start": round(start, 3), "end": round(end, 3), "frames": []}
        for index, (start, end) in enumerate(segments)
    ]
    for path, timestamp in zip(frame_files, times):
        index = _shot_index_for(segments, timestamp)
        shots[index]["frames"].append(
            {
                "frame": path.name,
                "timestamp": round(timestamp, 3),
                "role": _role_for(segments[index], timestamp),
            }
        )
    return shots


def _guard_existing_analysis(manifest_path: Path) -> None:
    """Refuse to overwrite a manifest the host has already analysed."""
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, dict):
        return
    if manifest.get("status") != "PREPARED" or manifest.get("shots"):
        raise PreprocessError(
            "run directory already holds an analysed run_manifest.json; "
            "start a new run directory instead of overwriting the analysis"
        )


def prepare_run(
    video: Path,
    run_dir: Path,
    max_frames: int = DEFAULT_MAX_FRAMES,
    long_edge: int = DEFAULT_LONG_EDGE,
) -> Dict[str, Any]:
    video = Path(video).expanduser().resolve()
    run_dir = Path(run_dir).expanduser().resolve()
    if not video.is_file():
        raise PreprocessError("video does not exist: %s" % video)
    if video.suffix.lower() not in VIDEO_EXTENSIONS:
        raise PreprocessError("input must be a supported video file")
    if max_frames < 3 or max_frames > 200:
        raise PreprocessError("max_frames must be between 3 and 200")
    if long_edge < 240 or long_edge > 2160:
        raise PreprocessError("long_edge must be between 240 and 2160")

    outputs = run_dir / "outputs"
    internal = run_dir / "internal" / "debug"
    frames = internal / "sample_frames"
    if outputs.exists() and any(outputs.iterdir()):
        raise PreprocessError("outputs directory must be empty before a run")
    _guard_existing_analysis(internal / "run_manifest.json")
    outputs.mkdir(parents=True, exist_ok=True)
    # Drop stale frames so a shorter re-run cannot leak evidence from the last one.
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir(parents=True, exist_ok=True)

    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        raise PreprocessError("ffmpeg and ffprobe are required")

    probe = _probe(video)
    facts = _video_facts(probe)

    source_frame = internal / "source_first_frame.png"
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "0.05",
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(source_frame),
        ]
    )

    audio_path = internal / "audio_16k_mono.wav"
    stderr = _decode_once(video, audio_path, facts["has_audio"])
    cuts = parse_cut_times(stderr)
    (internal / "scene_cuts.json").write_text(
        json.dumps({"threshold": 0.20, "cut_times": cuts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    segments = _shot_segments(cuts, facts["duration"])
    budgets = _allocate_budgets(segments, max_frames)
    scale = _scale_filter(facts["width"], facts["height"], long_edge)

    planned = [
        timestamp
        for (start, end), budget in zip(segments, budgets)
        for timestamp, _ in _sample_times(start, end, budget)
    ]
    actual = _extract_frames(video, planned, frames, scale, _parse_fps(facts["fps"]))
    frame_paths = sorted(frames.glob("frame_*.jpg"))
    # showinfo should report exactly one timestamp per emitted file; if a future
    # ffmpeg ever disagrees, fall back to the planned times rather than mis-mapping.
    timing_verified = len(actual) == len(frame_paths)
    if timing_verified:
        frame_paths, actual = _keep_closest_frames(frame_paths, actual, planned)
    shot_records = _map_frames_to_shots(
        segments, frame_paths, actual if timing_verified else planned
    )

    contact_sheet = internal / "contact_sheet.jpg"
    has_sheet = _build_contact_sheet(
        frame_paths,
        contact_sheet,
        _scale_filter(facts["width"], facts["height"], CONTACT_SHEET_LONG_EDGE),
    )

    (internal / "shot_segments.json").write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "duration": facts["duration"],
                "cut_times": cuts,
                "frame_long_edge": long_edge,
                "frame_timing_verified": timing_verified,
                "contact_sheet": contact_sheet.name if has_sheet else None,
                "shots": shot_records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    metadata = {
        "source_video": str(video),
        **facts,
        "max_frames_requested": max_frames,
        "frames_extracted": len(frame_paths),
        "frame_long_edge": long_edge,
        "detected_shot_count": len(segments),
    }
    (internal / "video_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "version": SCHEMA_VERSION,
        "status": "PREPARED",
        "video_input_count": 1,
        "source_video": str(video),
        "three_storey_rural_villa": False,
        "shots": [],
        "spoken_segments": [],
        "character_references": [],
        "evidence": {
            "source_first_frame": str(source_frame),
            "sample_frames": str(frames),
            "shot_segments": str(internal / "shot_segments.json"),
            "contact_sheet": str(contact_sheet) if has_sheet else None,
            "audio": str(audio_path) if facts["has_audio"] else None,
        },
    }
    (internal / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"status": "PREPARED", "run_dir": str(run_dir), "metadata": metadata}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="The only required user input")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES)
    parser.add_argument("--long-edge", type=int, default=DEFAULT_LONG_EDGE)
    args = parser.parse_args()
    try:
        result = prepare_run(args.video, args.run_dir, args.max_frames, args.long_edge)
    except PreprocessError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
