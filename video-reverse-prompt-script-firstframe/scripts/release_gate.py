#!/usr/bin/env python3
"""Fail-closed V0.6 release gate for exactly two user-facing artifacts."""

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    from .compose_prompt import PromptBuildError, compose_prompt
    from .contracts import (
        AnalysisValidationError,
        DELIVERY_FILES,
        REQUIRED_PROMPT_SECTIONS,
        SCHEMA_VERSION,
        orientation_of,
        required_visual_checks,
        validate_analysis,
    )
except ImportError:  # Direct script execution.
    from compose_prompt import PromptBuildError, compose_prompt
    from contracts import (
        AnalysisValidationError,
        DELIVERY_FILES,
        REQUIRED_PROMPT_SECTIONS,
        SCHEMA_VERSION,
        orientation_of,
        required_visual_checks,
        validate_analysis,
    )


class ReleaseGateError(RuntimeError):
    pass


def _read_json(path: Path, label: str) -> Dict[str, Any]:
    if not path.is_file():
        raise ReleaseGateError("missing %s: %s" % (label, path.name))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGateError("invalid %s: %s" % (label, exc))
    if not isinstance(value, dict):
        raise ReleaseGateError("%s must be a JSON object" % label)
    return value


def _png_dimensions(path: Path) -> Tuple[int, int]:
    try:
        header = path.read_bytes()[:24]
    except OSError as exc:
        raise ReleaseGateError("cannot read first_frame PNG: %s" % exc)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ReleaseGateError("first_frame.png must be a valid PNG")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ReleaseGateError("first_frame PNG dimensions are invalid")
    return width, height


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ReleaseGateError("cannot hash first_frame.png: %s" % exc)
    return digest.hexdigest()


def _source_dimensions(metadata: Dict[str, Any]) -> Tuple[int, int]:
    try:
        width = int(metadata["width"])
        height = int(metadata["height"])
    except (KeyError, TypeError, ValueError):
        raise ReleaseGateError("video metadata is missing usable source dimensions")
    if width <= 0 or height <= 0:
        raise ReleaseGateError("video metadata source dimensions are invalid")
    return width, height


def _validate_outputs(outputs: Path, metadata: Dict[str, Any]) -> Dict[str, Any]:
    if not outputs.is_dir():
        raise ReleaseGateError("missing outputs directory")
    actual = {item.name for item in outputs.iterdir()}
    if actual != DELIVERY_FILES or any(not item.is_file() for item in outputs.iterdir()):
        raise ReleaseGateError(
            "outputs must contain exactly first_frame.png and image_to_video_prompt.md"
        )
    width, height = _png_dimensions(outputs / "first_frame.png")
    source_width, source_height = _source_dimensions(metadata)
    frame_orientation = orientation_of(width, height)
    source_orientation = orientation_of(source_width, source_height)
    if frame_orientation != source_orientation:
        raise ReleaseGateError(
            "first_frame.png orientation %s does not match the source video orientation %s"
            % (frame_orientation, source_orientation)
        )
    return {
        "first_frame_width": width,
        "first_frame_height": height,
        "orientation": frame_orientation,
    }


def _validate_visual_qa(qa: Dict[str, Any], villa: bool, image_sha256: str) -> None:
    if qa.get("candidate_sha256") != image_sha256:
        raise ReleaseGateError("QA image hash does not match first_frame.png")
    if qa.get("overall") != "PASS":
        raise ReleaseGateError("visual QA overall must be explicit PASS")
    detected_text = qa.get("detected_readable_text")
    if not isinstance(detected_text, list):
        raise ReleaseGateError("visual QA detected_readable_text must be a list")
    if detected_text:
        raise ReleaseGateError("readable text detected in first frame")
    checks = qa.get("checks")
    if not isinstance(checks, dict):
        raise ReleaseGateError("visual QA checks are missing")
    required = required_visual_checks(villa)
    missing = sorted(required.difference(checks))
    if missing:
        label = "villa visual QA checks" if villa else "visual QA checks"
        raise ReleaseGateError("missing %s: %s" % (label, ", ".join(missing)))
    not_pass = sorted(name for name in required if checks.get(name) != "PASS")
    if not_pass:
        raise ReleaseGateError(
            "every visual QA check requires explicit PASS: %s" % ", ".join(not_pass)
        )
    extra_failures = sorted(name for name, value in checks.items() if value != "PASS")
    if extra_failures:
        raise ReleaseGateError(
            "every recorded visual QA check requires explicit PASS: %s"
            % ", ".join(extra_failures)
        )


def _validate_provenance(provenance: Dict[str, Any], image_sha256: str) -> None:
    if provenance.get("host_capability") != "codex_host_imagegen":
        raise ReleaseGateError("first frame must come from Codex host ImageGen")
    if provenance.get("direct_image_result") is not True:
        raise ReleaseGateError("first frame must be a direct image result")
    if provenance.get("salvaged_from_composite") is not False:
        raise ReleaseGateError("a salvaged or cropped composite image is forbidden")
    if provenance.get("source_first_frame_used") is not True:
        raise ReleaseGateError("source first frame must be used as visual evidence")
    if provenance.get("candidate_sha256") != image_sha256:
        raise ReleaseGateError("provenance image hash does not match first_frame.png")
    if not str(provenance.get("candidate_filename", "")).strip():
        raise ReleaseGateError("render provenance candidate filename is missing")
    attempt = provenance.get("attempt")
    if not isinstance(attempt, int) or attempt < 1:
        raise ReleaseGateError("render provenance attempt must be a positive integer")


def _validate_prompt(prompt: str, manifest: Dict[str, Any]) -> None:
    if len(prompt.strip()) < 120:
        raise ReleaseGateError("image_to_video_prompt.md is incomplete")
    for section in REQUIRED_PROMPT_SECTIONS:
        if "## %s" % section not in prompt:
            raise ReleaseGateError("prompt missing required section: %s" % section)
    if "人物动作严格来自参考视频连续帧" not in prompt:
        raise ReleaseGateError("prompt missing reference motion rule")

    shots = manifest.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ReleaseGateError("run manifest must contain at least one analyzed shot")
    for position, shot in enumerate(shots, start=1):
        try:
            start = "%.3f" % float(shot["start"])
            end = "%.3f" % float(shot["end"])
        except (KeyError, TypeError, ValueError):
            raise ReleaseGateError("invalid shot timing in run manifest")
        heading = "### %s–%s秒｜镜头 %02d" % (start, end, position)
        if heading not in prompt:
            raise ReleaseGateError("prompt missing shot range %s–%s" % (start, end))

    segments = manifest.get("spoken_segments", [])
    if not isinstance(segments, list):
        raise ReleaseGateError("spoken_segments must be a list")
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if text and text not in prompt:
            raise ReleaseGateError("prompt missing spoken text: %s" % text)

    lower = prompt.lower()
    for marker in ("人物动作", "摄影机", "场景", "口型", "画外音", "负面约束"):
        if marker.lower() not in lower:
            raise ReleaseGateError("prompt missing semantic requirement: %s" % marker)


def validate_release(run_dir: Path) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    outputs = run_dir / "outputs"
    internal = run_dir / "internal" / "debug"
    metadata = _read_json(internal / "video_metadata.json", "video metadata")
    output_facts = _validate_outputs(outputs, metadata)
    manifest = _read_json(internal / "run_manifest.json", "run manifest")
    if manifest.get("version") != SCHEMA_VERSION:
        raise ReleaseGateError("run manifest version must be %s" % SCHEMA_VERSION)
    if manifest.get("video_input_count") != 1:
        raise ReleaseGateError("V0.6 requires exactly one video input")
    villa = bool(manifest.get("three_storey_rural_villa", False))
    analysis = _read_json(internal / "video_analysis.json", "video analysis")
    try:
        validate_analysis(analysis)
    except AnalysisValidationError as exc:
        raise ReleaseGateError("invalid video analysis: %s" % exc)
    if analysis["three_storey_rural_villa"] is not villa:
        raise ReleaseGateError("video analysis and run manifest villa flags do not match")
    qa = _read_json(internal / "first_frame_visual_validation.json", "visual QA")
    provenance = _read_json(internal / "render_provenance.json", "render provenance")
    image_sha256 = _sha256(outputs / "first_frame.png")
    _validate_visual_qa(qa, villa, image_sha256)
    _validate_provenance(provenance, image_sha256)
    try:
        prompt = (outputs / "image_to_video_prompt.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseGateError("cannot read image_to_video_prompt.md: %s" % exc)
    _validate_prompt(prompt, manifest)
    try:
        expected_prompt = compose_prompt(analysis)
    except PromptBuildError as exc:
        raise ReleaseGateError("cannot compose prompt from video analysis: %s" % exc)
    if prompt != expected_prompt:
        raise ReleaseGateError("prompt does not match video analysis")
    return {
        "status": "PASS",
        "version": SCHEMA_VERSION,
        "delivery_files": sorted(DELIVERY_FILES),
        "three_storey_rural_villa": villa,
        **output_facts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    report_path = args.run_dir / "internal" / "debug" / "release_gate.json"
    try:
        report = validate_release(args.run_dir)
        exit_code = 0
    except ReleaseGateError as exc:
        report = {"status": "FAIL", "version": SCHEMA_VERSION, "reason": str(exc)}
        exit_code = 1
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print("ERROR: cannot write release report: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
