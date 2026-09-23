#!/usr/bin/env python3
"""Detect likely hard cuts with FFmpeg and store evidence under internal/debug."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


PTS_PATTERN = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def scene_filter(threshold: float = 0.20) -> str:
    return "select='gt(scene,%.3f)',showinfo" % threshold


def parse_frame_times(stderr: str) -> list:
    return [round(float(value), 6) for value in PTS_PATTERN.findall(stderr)]


def parse_cut_times(stderr: str) -> list:
    return sorted(set(parse_frame_times(stderr)))


def detect(video: Path, threshold: float = 0.20) -> list:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(video),
        "-filter:v",
        scene_filter(threshold),
        "-an",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg scene detection failed: %s" % result.stderr.strip())
    return parse_cut_times(result.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--threshold", type=float, default=0.20)
    args = parser.parse_args()
    try:
        cuts = detect(args.video, args.threshold)
    except (OSError, RuntimeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps({"threshold": args.threshold, "cut_times": cuts}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print("ERROR: cannot write scene evidence: %s" % exc, file=sys.stderr)
        return 1
    print("detected %d cuts" % len(cuts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
