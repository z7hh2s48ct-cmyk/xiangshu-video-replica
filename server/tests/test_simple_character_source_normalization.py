"""一键人物上传原图规范化专项测试.

手机直接上传的原图会带 EXIF 方向、动态照片/厂商尾部数据与超大分辨率，曾被结构校验
误拒或原样发给出图服务导致五视图失败；截图再上传却能成功。这里锁定：上传原图统一
被转成摆正、限尺寸、8 位 RGB/RGBA 的 PNG，且这份 PNG 就是存储与出图的唯一源图。

图片用真实 ffmpeg（lavfi）生成，与 test_material_thumbs.py 同手法；不依赖 PG。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import struct
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app import simple_character, simple_character_routes
from app.character_image_generation import png_chunk
from app.first_frames import GeneratedImage, ImageInput
from app.media_tools import MediaToolUnavailable, resolve_media_binary
from app.simple_character import (
    SIMPLE_SOURCE_MAX_EDGE,
    _decode_png_rgb,
    _parse_png,
    _source_exif_orientation,
    contact_sheet_placeholder_png,
    normalize_simple_character_source,
    prepare_simple_character_generation,
    validate_simple_character_source,
)
from app.storage import FakeStorageAdapter

DISPLAY_NAME = "规范化测试人物"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _encode(
    source: str, codec: str, *, fmt: str = "image2pipe", pix_fmt: str | None = None
) -> bytes:
    command = [resolve_media_binary("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", source]
    command += ["-frames:v", "1"]
    if pix_fmt is not None:
        command += ["-pix_fmt", pix_fmt]
    command += ["-q:v", "2", "-f", fmt, "-vcodec", codec, "-"]
    output = subprocess.run(command, capture_output=True, check=True).stdout
    assert output
    return output


def _marker_jpeg() -> bytes:
    """64x32 蓝底、左上角 16x16 红块：摆正后红块所在角可唯一判定方向变换。"""
    return _encode(
        "color=c=blue:s=64x32,drawbox=x=0:y=0:w=16:h=16:color=red:t=fill",
        "mjpeg",
    )


def _exif_tiff(orientation: int, *, little_endian: bool = False) -> bytes:
    order = "<" if little_endian else ">"
    header = b"II*\x00" if little_endian else b"MM\x00*"
    return (
        header
        + struct.pack(f"{order}I", 8)
        + struct.pack(f"{order}H", 1)
        + struct.pack(f"{order}HHIHH", 0x0112, 3, 1, orientation, 0)
        + struct.pack(f"{order}I", 0)
    )


def _with_exif(jpeg: bytes, orientation: int) -> bytes:
    payload = b"Exif\x00\x00" + _exif_tiff(orientation)
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:2] + app1 + jpeg[2:]


def _png_header(png: bytes) -> tuple[int, int, int, int]:
    parsed = _parse_png(png)
    assert parsed is not None
    return parsed.width, parsed.height, parsed.bit_depth, parsed.color_type


def _is_red(rows: list[bytes], x: int, y: int) -> bool:
    red, _green, blue = rows[y][x * 3 : x * 3 + 3]
    return red > 180 and blue < 80


def _red_corners(png: bytes) -> tuple[tuple[int, int], set[str]]:
    decoded = _decode_png_rgb(png)
    assert decoded is not None
    width, height, rows = decoded
    corners = {
        "TL": (4, 4),
        "TR": (width - 5, 4),
        "BL": (4, height - 5),
        "BR": (width - 5, height - 5),
    }
    return (width, height), {name for name, (x, y) in corners.items() if _is_red(rows, x, y)}


@pytest.mark.parametrize(
    ("orientation", "expected_size", "red_corner"),
    [
        (1, (64, 32), "TL"),
        (2, (64, 32), "TR"),
        (3, (64, 32), "BR"),
        (4, (64, 32), "BL"),
        (5, (32, 64), "TL"),
        (6, (32, 64), "TR"),
        (7, (32, 64), "BR"),
        (8, (32, 64), "BL"),
    ],
)
def test_exif_orientation_is_applied_exactly_once(
    orientation: int, expected_size: tuple[int, int], red_corner: str
) -> None:
    content, content_type = normalize_simple_character_source(
        _with_exif(_marker_jpeg(), orientation), "image/jpeg", DISPLAY_NAME
    )

    assert content_type == "image/png"
    assert _red_corners(content) == (expected_size, {red_corner})


@pytest.mark.parametrize(
    "trailer",
    [
        pytest.param(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048, id="motion-photo-mp4"),
        pytest.param(b"SEFH" + b"\x01" * 64 + b"SEFT", id="samsung-trailer"),
        pytest.param(b"\x00" * 64, id="zero-padding"),
    ],
)
def test_phone_jpeg_with_data_after_eoi_is_accepted(trailer: bytes) -> None:
    content, content_type = normalize_simple_character_source(
        _marker_jpeg() + trailer, "image/jpeg", DISPLAY_NAME
    )

    assert content_type == "image/png"
    assert _png_header(content) == (64, 32, 8, 2)
    # worker 会用同一校验复验存储的源图，规范化产物必须原样通过。
    validate_simple_character_source(content, content_type, DISPLAY_NAME)


def test_primary_jpeg_wins_over_appended_and_embedded_jpegs() -> None:
    """Ultra HDR 增益图/MPF 多图会在主图 EOI 后再拼一张完整 JPEG，EXIF 缩略图则
    内嵌在扫描前的 APP 段里；两者都不能取代主图或截错主图的结束位置。"""
    secondary = _encode("color=c=green:s=16x16", "mjpeg")
    thumbnail_app = b"\xff\xe2" + struct.pack(">H", len(secondary) + 2) + secondary
    primary = _marker_jpeg()
    original = primary[:2] + thumbnail_app + primary[2:] + secondary

    content, _ = normalize_simple_character_source(original, "image/jpeg", DISPLAY_NAME)

    decoded = _decode_png_rgb(content)
    assert decoded is not None
    width, height, rows = decoded
    assert (width, height) == (64, 32)
    assert _is_red(rows, 4, 4)


@pytest.mark.parametrize(
    ("content_type", "codec", "fmt"),
    [("image/png", "png", "image2pipe"), ("image/webp", "libwebp", "webp")],
)
def test_png_and_webp_with_trailing_bytes_are_accepted(
    content_type: str, codec: str, fmt: str
) -> None:
    original = _encode("color=c=red:s=120x80", codec, fmt=fmt)

    content, normalized_type = normalize_simple_character_source(
        original + b"\x00" * 32, content_type, DISPLAY_NAME
    )

    assert normalized_type == "image/png"
    assert _png_header(content)[:2] == (120, 80)


def test_oversized_photo_is_downscaled_to_max_edge() -> None:
    original = _encode("testsrc2=s=4000x3000", "mjpeg")

    content, _ = normalize_simple_character_source(original, "image/jpeg", DISPLAY_NAME)

    assert _png_header(content) == (SIMPLE_SOURCE_MAX_EDGE, 1152, 8, 2)


def test_small_image_keeps_its_size() -> None:
    original = _encode("color=c=red:s=300x200", "mjpeg")

    content, _ = normalize_simple_character_source(original, "image/jpeg", DISPLAY_NAME)

    assert _png_header(content) == (300, 200, 8, 2)


@pytest.mark.parametrize(
    ("pix_fmt", "expected_color_type"),
    [("rgba", 6), ("rgb48be", 2), ("gray", 2), ("pal8", 2)],
)
def test_png_variants_become_8bit_rgb_or_rgba(pix_fmt: str, expected_color_type: int) -> None:
    source = "color=c=red@0.5:s=40x30,format=rgba" if pix_fmt == "rgba" else "color=c=red:s=40x30"
    original = _encode(source, "png", pix_fmt=pix_fmt)

    content, _ = normalize_simple_character_source(original, "image/png", DISPLAY_NAME)

    assert _png_header(content) == (40, 30, 8, expected_color_type)
    assert _parse_png(content).interlace == 0  # type: ignore[union-attr]


def test_structurally_valid_but_undecodable_png_is_rejected() -> None:
    ihdr = struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)
    corrupt = PNG_SIGNATURE + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", b"not-zlib")
    corrupt += png_chunk(b"IEND", b"")

    with pytest.raises(HTTPException) as error:
        normalize_simple_character_source(corrupt, "image/png", DISPLAY_NAME)

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "SIMPLE_CHARACTER_IMAGE_INVALID"


def _encode_file(tmp_path: Path, name: str, source: str, *args: str) -> bytes:
    """TIFF/AVIF 的封装需要可回跳的输出，只能先写文件再读回。"""
    target = tmp_path / name
    command = [resolve_media_binary("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", source]
    completed = subprocess.run(
        [*command, "-frames:v", "1", *args, str(target)], capture_output=True, check=False
    )
    if completed.returncode != 0 and "libaom-av1" in args:
        pytest.skip("本机 ffmpeg 缺少 AV1 编码器，无法生成 AVIF 样本")
    assert completed.returncode == 0, completed.stderr
    return target.read_bytes()


@pytest.mark.parametrize(
    ("content_type", "name", "args", "expected_color_types"),
    [
        ("image/gif", "still.gif", (), {2, 6}),
        ("image/bmp", "plain.bmp", (), {2}),
        ("image/tiff", "plain.tiff", (), {2}),
        ("image/tiff", "lzw.tiff", ("-compression_algo", "lzw"), {2}),
        ("image/avif", "still.avif", ("-c:v", "libaom-av1", "-still-picture", "1"), {2}),
    ],
)
def test_additional_formats_are_normalized_to_png(
    tmp_path: Path,
    content_type: str,
    name: str,
    args: tuple[str, ...],
    expected_color_types: set[int],
) -> None:
    original = _encode_file(tmp_path, name, "testsrc=s=320x240", *args)

    content, normalized_type = normalize_simple_character_source(
        original, content_type, DISPLAY_NAME
    )

    assert normalized_type == "image/png"
    width, height, bit_depth, color_type = _png_header(content)
    assert (width, height, bit_depth) == (320, 240, 8)
    assert color_type in expected_color_types
    validate_simple_character_source(content, normalized_type, DISPLAY_NAME)


def test_animated_gif_uses_its_first_frame() -> None:
    original = _encode_frames("testsrc=s=160x120:d=1:r=5", "gif")

    content, _ = normalize_simple_character_source(original, "image/gif", DISPLAY_NAME)

    assert _png_header(content)[:3] == (160, 120, 8)


def _encode_frames(source: str, fmt: str) -> bytes:
    command = [resolve_media_binary("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", source]
    return subprocess.run([*command, "-f", fmt, "-"], capture_output=True, check=True).stdout


def _marker_tiff(orientation: int) -> bytes:
    """手工拼一张无压缩 RGB TIFF（IFD0 带方向标签），红块像素无损，方向判定精确。"""
    width, height = 64, 32
    red, blue = b"\xff\x00\x00", b"\x00\x00\xff"
    pixels = b"".join(
        red if x < 16 and y < 16 else blue for y in range(height) for x in range(width)
    )
    entry_count = 10
    bits_offset = 8 + 2 + entry_count * 12 + 4
    pixel_offset = bits_offset + 6
    entries = [
        (256, 3, 1, width),
        (257, 3, 1, height),
        (258, 3, 3, bits_offset),
        (259, 3, 1, 1),
        (262, 3, 1, 2),
        (273, 4, 1, pixel_offset),
        (274, 3, 1, orientation),
        (277, 3, 1, 3),
        (278, 3, 1, height),
        (279, 4, 1, len(pixels)),
    ]
    ifd = struct.pack("<H", entry_count)
    for tag, field_type, count, value in entries:
        ifd += struct.pack("<HHI", tag, field_type, count)
        inline_short = field_type == 3 and count == 1
        ifd += struct.pack("<HH", value, 0) if inline_short else struct.pack("<I", value)
    ifd += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", 8) + ifd + struct.pack("<HHH", 8, 8, 8) + pixels


@pytest.mark.parametrize(
    ("orientation", "expected_size", "red_corner"),
    [(1, (64, 32), "TL"), (3, (64, 32), "BR"), (6, (32, 64), "TR"), (8, (32, 64), "BL")],
)
def test_tiff_orientation_tag_is_applied(
    orientation: int, expected_size: tuple[int, int], red_corner: str
) -> None:
    content, _ = normalize_simple_character_source(
        _marker_tiff(orientation), "image/tiff", DISPLAY_NAME
    )

    assert _red_corners(content) == (expected_size, {red_corner})


def test_avif_orientation_is_left_to_its_container_transforms() -> None:
    assert (
        _source_exif_orientation(b"\x00\x00\x00\x14ftypavif\x00\x00\x00\x00avif", "image/avif")
        is None
    )
    assert _source_exif_orientation(_exif_tiff(6, little_endian=True), "image/tiff") == 6


@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        ("image/gif", b"GIF90a" + b"\x00" * 32),
        ("image/bmp", b"BX" + b"\x00" * 32),
        ("image/tiff", b"II+\x00" + b"\x00" * 32),
        ("image/avif", b"\x00\x00\x00\x14ftypheic\x00\x00\x00\x00mif1"),
    ],
)
def test_wrong_signature_for_declared_type_is_rejected(content_type: str, content: bytes) -> None:
    with pytest.raises(HTTPException) as error:
        normalize_simple_character_source(content, content_type, DISPLAY_NAME)

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "SIMPLE_CHARACTER_IMAGE_INVALID"


def test_decoder_pixel_limit_guards_signature_only_formats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GIF 只做签名校验，超出像素上限必须由 ffmpeg 的 -max_pixels 在解码前拒掉。
    monkeypatch.setattr(simple_character, "SIMPLE_IMAGE_MAX_PIXELS", 1000)
    original = _encode("color=c=red:s=320x240", "gif", fmt="gif")

    with pytest.raises(HTTPException) as error:
        normalize_simple_character_source(original, "image/gif", DISPLAY_NAME)

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "SIMPLE_CHARACTER_IMAGE_INVALID"


def test_unsupported_type_is_still_rejected_before_ffmpeg() -> None:
    with pytest.raises(HTTPException) as error:
        normalize_simple_character_source(b"ftypheic", "image/heic", DISPLAY_NAME)

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "SIMPLE_CHARACTER_IMAGE_TYPE_UNSUPPORTED"


def test_missing_ffmpeg_is_reported_as_temporarily_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(tool: str) -> str:
        raise MediaToolUnavailable(tool)

    monkeypatch.setattr(simple_character, "resolve_media_binary", unavailable)

    with pytest.raises(HTTPException) as error:
        normalize_simple_character_source(_marker_jpeg(), "image/jpeg", DISPLAY_NAME)

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "SIMPLE_CHARACTER_IMAGE_VALIDATION_UNAVAILABLE"


def _riff(chunks: list[tuple[bytes, bytes]]) -> bytes:
    body = b"WEBP"
    for chunk_type, payload in chunks:
        body += chunk_type + struct.pack("<I", len(payload)) + payload
        body += b"\x00" * (len(payload) % 2)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_exif_orientation_is_read_from_webp_and_png_containers() -> None:
    webp = _riff([(b"VP8X", b"\x08" + b"\x00" * 9), (b"EXIF", _exif_tiff(6, little_endian=True))])
    prefixed_webp = _riff([(b"EXIF", b"Exif\x00\x00" + _exif_tiff(3))])
    png = PNG_SIGNATURE + png_chunk(b"IHDR", b"\x00" * 13) + png_chunk(b"eXIf", _exif_tiff(8))

    assert _source_exif_orientation(webp, "image/webp") == 6
    assert _source_exif_orientation(prefixed_webp, "image/webp") == 3
    assert _source_exif_orientation(png, "image/png") == 8


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"\xff\xd8\xff\xe1\x00\x0aExif\x00\x00MM", id="truncated-tiff"),
        pytest.param(
            b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00XX\x00*\x00\x00\x00\x08", id="bad-order"
        ),
        pytest.param(_with_exif(b"\xff\xd8\xff\xd9", 9), id="out-of-range"),
        pytest.param(b"\xff\xd8\xff\xda", id="no-exif"),
    ],
)
def test_broken_exif_falls_back_to_upright(content: bytes) -> None:
    assert _source_exif_orientation(content, "image/jpeg") == 1


class _RecordingProvider:
    provider_name = "recording"

    def __init__(self) -> None:
        self.sources: list[ImageInput] = []

    def edit(self, *, source_image: ImageInput, **_: Any) -> list[GeneratedImage]:
        self.sources.append(source_image)
        return [
            GeneratedImage(content=contact_sheet_placeholder_png(b"seed"), content_type="image/png")
        ]


def test_worker_sends_the_normalized_png_to_the_image_provider() -> None:
    content, content_type = normalize_simple_character_source(
        _with_exif(_marker_jpeg(), 6) + b"\x00" * 64, "image/jpeg", DISPLAY_NAME
    )
    provider = _RecordingProvider()

    prepare_simple_character_generation(
        source_content=content,
        source_content_type=content_type,
        display_name=DISPLAY_NAME,
        image_provider=provider,  # type: ignore[arg-type]
    )

    [sent] = provider.sources
    assert sent.content == content
    assert sent.content_type == "image/png"
    assert sent.filename.endswith(".png")
    assert _png_header(sent.content)[:2] == (32, 64)


class _FakeDb:
    @contextmanager
    def write(self) -> Any:
        yield object(), object()


def test_enqueue_route_stores_and_queues_the_normalized_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: dict[str, Any] = {}

    def fake_enqueue(_conn: object, **kwargs: Any) -> dict[str, Any]:
        queued.update(kwargs)
        return {"source_storage_uri": kwargs["source_storage_uri"]}

    monkeypatch.setattr(simple_character_routes, "require_not_auditor", lambda *_a, **_k: None)
    monkeypatch.setattr(simple_character_routes, "enqueue_character_sheet_task", fake_enqueue)
    monkeypatch.setattr(simple_character_routes, "character_sheet_task_response", lambda row: row)
    storage = FakeStorageAdapter(provider="fake", bucket="normalize-bucket")
    original = _with_exif(_marker_jpeg(), 6) + b"\x00\x00\x00\x18ftypmp42"
    upload = UploadFile(
        file=io.BytesIO(original),
        size=len(original),
        filename="IMG_0001.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )

    asyncio.run(
        simple_character_routes._enqueue_simple_character_upload(
            storage=storage,
            db=_FakeDb(),  # type: ignore[arg-type]
            file=upload,
            display_name=DISPLAY_NAME,
            idempotency_key="normalize-route-test",
            persona_name="",
            project_id=None,
            image_consent_version="2026-09-14-v1",
            image_consent_accepted=True,
        )
    )

    [(key, (stored_bytes, stored))] = storage._objects.items()
    assert key.endswith(".png")
    assert stored.content_type == "image/png"
    assert stored_bytes.startswith(PNG_SIGNATURE)
    assert _png_header(stored_bytes)[:2] == (32, 64)
    assert queued["source_content_type"] == "image/png"
    assert queued["source_sha256"] == hashlib.sha256(stored_bytes).hexdigest()
    assert queued["source_size_bytes"] == len(stored_bytes)
