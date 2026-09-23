from __future__ import annotations

import hashlib
import struct

from app.character_contracts import RequiredCharacterViewType

CHARACTER_QUALITY_SCHEMA_VERSION = "character-quality.v1"


def inspect_fake_character_asset(
    content: bytes,
    *,
    view_type: RequiredCharacterViewType,
) -> dict[str, object]:
    """Return simulated quality signals for the deterministic Fake provider."""
    width, height = png_dimensions(content)
    digest = hashlib.sha256(content).digest()
    identity_score = round(0.9 + digest[0] / 2550, 3)
    costume_score = round(0.88 + digest[1] / 2550, 3)
    checks = {
        "body_proportions": "PASS",
        "dimensions": "PASS" if width >= 1024 and height >= 1024 else "WARN",
        "limb_integrity": "PASS",
        "person_count": "PASS",
        "sharpness": "PASS",
        "text_or_watermark": "PASS",
        "truncation": "PASS",
        "view_type_match": "PASS",
    }
    return {
        "blocking_issue_codes": [],
        "checks": checks,
        "dimensions": {"height": height, "width": width},
        "inspector": {"model": "fake-quality-v1", "provider": "fake_character_quality"},
        "schema_version": CHARACTER_QUALITY_SCHEMA_VERSION,
        "scores": {
            "costume_consistency": costume_score,
            "identity_consistency": identity_score,
        },
        "simulated": True,
        "view_type": view_type,
    }


def png_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
        raise ValueError("invalid PNG content")
    return struct.unpack(">II", content[16:24])
