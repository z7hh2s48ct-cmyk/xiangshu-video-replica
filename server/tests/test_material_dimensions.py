"""MATERIAL-UX-08 — 图片宽高头解析的单元矩阵（无 PG，纯函数）.

覆盖 ``_probe_image_dimensions`` 的三种容器（PNG/JPEG/WebP 三种 chunk）与
"任何失败返回 None、绝不阻塞上传"的降级语义。
"""

from __future__ import annotations

import struct

from app.materials import _probe_image_dimensions


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)


def _jpeg(width: int, height: int) -> bytes:
    # 最小 SOF0 段：段长 11 = 长度字段 2 + 精度 1 + 高 2 + 宽 2 + 通道块 4。
    return (
        b"\xff\xd8"
        + b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x22\x00\x00"
        + b"\xff\xd9"
    )


def test_png_dimensions() -> None:
    assert _probe_image_dimensions(_png(1920, 1080), ".png") == (1920, 1080)


def test_png_truncated_returns_none() -> None:
    assert _probe_image_dimensions(_png(1920, 1080)[:20], ".png") is None


def test_jpeg_dimensions() -> None:
    assert _probe_image_dimensions(_jpeg(64, 32), ".jpg") == (64, 32)
    assert _probe_image_dimensions(_jpeg(64, 32), ".jpeg") == (64, 32)


def test_jpeg_garbage_returns_none() -> None:
    assert _probe_image_dimensions(b"\xff\xd8garbage", ".jpg") is None


def test_webp_vp8x_dimensions() -> None:
    body = (
        b"RIFF"
        + struct.pack("<I", 30)
        + b"WEBP"
        + b"VP8X"
        + struct.pack("<I", 10)
        + b"\x00\x00\x00\x00"
        + (99).to_bytes(3, "little")
        + (49).to_bytes(3, "little")
    )
    assert len(body) == 30
    assert _probe_image_dimensions(body, ".webp") == (100, 50)


def test_webp_vp8_lossy_dimensions() -> None:
    body = (
        b"RIFF"
        + struct.pack("<I", 30)
        + b"WEBP"
        + b"VP8 "
        + struct.pack("<I", 10)
        + b"\x00\x00\x00"
        + b"\x9d\x01\x2a"
        + struct.pack("<HH", 640, 480)
    )
    assert _probe_image_dimensions(body, ".webp") == (640, 480)


def test_webp_vp8l_lossless_dimensions() -> None:
    bits = (800 - 1) | ((600 - 1) << 14)
    body = (
        b"RIFF"
        + struct.pack("<I", 30)
        + b"WEBP"
        + b"VP8L"
        + struct.pack("<I", 10)
        + b"\x2f"
        + struct.pack("<I", bits)
    )
    assert _probe_image_dimensions(body, ".webp") == (800, 600)


def test_unknown_container_returns_none() -> None:
    assert _probe_image_dimensions(b"not-an-image", ".png") is None
    assert _probe_image_dimensions(b"", ".jpg") is None
