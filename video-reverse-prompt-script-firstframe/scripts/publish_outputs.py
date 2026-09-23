#!/usr/bin/env python3
"""Publish only the two delivery artifacts after QA evidence is ready."""

import argparse
import shutil
import sys
from pathlib import Path

try:
    from .release_gate import ReleaseGateError, validate_release
except ImportError:
    from release_gate import ReleaseGateError, validate_release


class PublishError(RuntimeError):
    pass


def publish(run_dir: Path, candidate: Path, prompt: Path) -> dict:
    run_dir = Path(run_dir)
    candidate = Path(candidate)
    prompt = Path(prompt)
    outputs = run_dir / "outputs"
    if not candidate.is_file():
        raise PublishError("candidate first frame is missing")
    if not prompt.is_file():
        raise PublishError("candidate prompt is missing")
    outputs.mkdir(parents=True, exist_ok=True)
    if any(outputs.iterdir()):
        raise PublishError("outputs must be empty before publication")

    first_frame = outputs / "first_frame.png"
    final_prompt = outputs / "image_to_video_prompt.md"
    try:
        shutil.copyfile(candidate, first_frame)
        shutil.copyfile(prompt, final_prompt)
        return validate_release(run_dir)
    except (OSError, ReleaseGateError):
        # These are the only two known files created by this function, so a
        # half-copied publication never blocks the next attempt.
        first_frame.unlink(missing_ok=True)
        final_prompt.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("prompt", type=Path)
    args = parser.parse_args()
    try:
        result = publish(args.run_dir, args.candidate, args.prompt)
    except (OSError, PublishError, ReleaseGateError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print("PASS: %s" % ", ".join(result["delivery_files"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
