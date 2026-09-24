#!/usr/bin/env python3
"""Create a NOT_RUN visual-QA template; the host must replace every value."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    from .contracts import SCHEMA_VERSION, required_visual_checks
except ImportError:
    from contracts import SCHEMA_VERSION, required_visual_checks


def create_template(villa: bool, attempt: int, candidate_sha256: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "attempt": attempt,
        "candidate_sha256": candidate_sha256,
        "checks": {name: "NOT_RUN" for name in sorted(required_visual_checks(villa))},
        "detected_readable_text": [],
        "overall": "NOT_RUN",
        "regeneration_instructions": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        candidate_sha256 = hashlib.sha256(args.candidate.read_bytes()).hexdigest()
        value = create_template(
            bool(manifest.get("three_storey_rural_villa")),
            args.attempt,
            candidate_sha256,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        print("ERROR: cannot create QA template: %s" % exc, file=sys.stderr)
        return 1
    print(str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
