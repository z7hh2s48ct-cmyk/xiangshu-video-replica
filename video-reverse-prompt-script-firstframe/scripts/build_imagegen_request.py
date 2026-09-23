#!/usr/bin/env python3
"""Create the request payload that Codex must pass to its host ImageGen tool."""

import argparse
import json
import sys
from pathlib import Path

try:
    from .contracts import build_first_frame_request, orientation_of
except ImportError:
    from contracts import build_first_frame_request, orientation_of


def regeneration_instructions(qa: dict) -> list:
    value = qa.get("regeneration_instructions", [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError("regeneration_instructions must be a list of non-empty strings")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--metadata",
        type=Path,
        required=True,
        help="video_metadata.json written by preprocess_video.py; fixes the output orientation",
    )
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--previous-qa", type=Path)
    args = parser.parse_args()
    try:
        analysis = json.loads(args.analysis.read_text(encoding="utf-8"))
        metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
        orientation = orientation_of(int(metadata["width"]), int(metadata["height"]))
        failures = []
        if args.previous_qa:
            qa = json.loads(args.previous_qa.read_text(encoding="utf-8"))
            failures = regeneration_instructions(qa)
        request = build_first_frame_request(analysis, args.attempt, failures, orientation)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print("ERROR: cannot write ImageGen request: %s" % exc, file=sys.stderr)
        return 1
    print(str(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
