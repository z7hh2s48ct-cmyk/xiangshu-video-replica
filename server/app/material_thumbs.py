"""素材缩略图（MATERIAL-THUMBS-B-20260917 P0-3 / MATERIAL-UX-02-20260922）。

素材库网格的视频与图片瓦片此前只能让浏览器经服务端代理流式拉原对象。
本模块在素材入库的写入点（参考视频上传完成、素材上传完成、生成成片
归档）从**已在手的内容字节**抽首帧 JPEG（图片素材为单帧缩放），缩略图对象
与原对象同址派生存放，并把键记录进 ``assets.metadata_json``（零迁移）——
列表 CTE 全分支已带出该字段，批量授权据此签发 7 天有效的缩略图 URL。

不变量（由 tests/test_material_thumbs.py 钉住）：
- 键派生确定性：``<object_key>.thumb.jpg``；
- 任何抽帧/存储失败只损失缩略图本身，绝不影响原对象的可用性与上传结果；
- 缩略图必须显著小于原对象（网格批量下发才有意义）；
- 图片（image/*）按需派生必须整读源对象，不得走 ``_HEAD_PROBE_BYTES``
  头探测——JPEG 扫描流被截断后 ffmpeg 仍会解出残缺画面并当作成功。
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import timedelta
from typing import TYPE_CHECKING

from app.media_tools import resolve_media_binary

if TYPE_CHECKING:
    from app.storage import StorageAdapter

THUMBNAIL_SUFFIX = ".thumb.jpg"
# 缩略图是原对象的派生小图，签名可放宽到 7 天，让浏览器跨页/跨会话命中缓存
# （瓦片不再每次进素材库重新签名）。授权签发与代理响应的缓存窗口共用此值，
# 缓存因此永远不会比签名活得更久。
THUMBNAIL_URL_EXPIRES_IN = timedelta(days=7)
_THUMBNAIL_MAX_HEIGHT = 480
# 静态图片缩略图的宽度上界：五视图等超宽合成图在只限高策略下（5000×1000）
# 会派生出 2400px 宽的瓦片缩略图，网格一页 24 张时下载量被放大。视频帧宽高比
# 常规，保持原「只限高」策略（既有视频缩略图输出不变）。
THUMBNAIL_MAX_WIDTH = 960
# 按需派生时的读取预算：先试头部（覆盖 faststart 的绝大多数），再按上限整读。
_HEAD_PROBE_BYTES = 8 * 1024 * 1024
_FULL_READ_LIMIT_BYTES = 64 * 1024 * 1024
_IMAGE_KEY_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def thumbnail_key_for(object_key: str) -> str:
    """缩略图对象键：与原对象同址派生，确定性、无碰撞、无需建表."""
    return f"{object_key}{THUMBNAIL_SUFFIX}"


def extract_thumbnail_jpeg(
    content: bytes,
    *,
    max_height: int = _THUMBNAIL_MAX_HEIGHT,
    max_width: int | None = None,
) -> bytes | None:
    """从视频/图片字节抽首帧 JPEG（缩到 ≤max_height）；任何失败返回 None 不抛错.

    ``max_width`` 非 None 时同时约束宽度（等比缩进 max_width×max_height 的
    框内，覆盖五视图等超宽合成图）；默认只限高，输出与既有视频链路一致。
    """
    scale_filter = (
        f"scale=-2:min({max_height}\\,ih)"
        if max_width is None
        else (
            f"scale=w=min({max_width}\\,iw):h=min({max_height}\\,ih)"
            ":force_original_aspect_ratio=decrease"
        )
    )
    try:
        ffmpeg = resolve_media_binary("ffmpeg")
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-vf",
                # 滤镜表达式内的逗号必须转义（逗号是 ffmpeg 的滤镜分隔符）。
                scale_filter,
                "-q:v",
                "4",
                "-f",
                "mjpeg",
                "pipe:1",
            ],
            input=content,
            capture_output=True,
            timeout=60,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.startswith(b"\xff\xd8"):
        return None
    return result.stdout


def extract_image_thumbnail_jpeg(content: bytes) -> bytes | None:
    """静态图片缩略图：抽帧链路 + 宽度上界（五视图等超宽合成图）."""
    return extract_thumbnail_jpeg(content, max_width=THUMBNAIL_MAX_WIDTH)


def store_video_thumbnail(
    storage: StorageAdapter,
    object_key: str,
    content: bytes,
    *,
    is_image: bool = False,
) -> str | None:
    """抽帧并落存储；返回缩略图键，失败返回 None（调用方无需降级处理）.

    历史命名保留：视频与图片素材共用本函数（``is_image`` 时按静态图片
    约束宽度上界）。
    """
    thumb = extract_image_thumbnail_jpeg(content) if is_image else extract_thumbnail_jpeg(content)
    if thumb is None:
        return None
    key = thumbnail_key_for(object_key)
    try:
        storage.put_object(key, thumb, content_type="image/jpeg")
    except Exception:
        return None
    return key


def source_key_for_thumbnail(thumbnail_key: str) -> str | None:
    """反推缩略图对应的原对象键；不是缩略图键则 None."""
    if not thumbnail_key.endswith(THUMBNAIL_SUFFIX):
        return None
    return thumbnail_key[: -len(THUMBNAIL_SUFFIX)]


def ensure_thumbnail_object(storage: StorageAdapter, thumbnail_key: str) -> bool:
    """确保缩略图对象存在，缺失则现场从原对象派生；返回是否可用。

    抽帧原本只发生在上传写入点，因此更早入库的视频/图片永远没有缩略图，
    只能退回 ``<video preload=metadata>`` 或直拉原图。改成按需派生后历史素材
    在第一次被看到时自动补齐，既不需要回填脚本跑批，当初抽帧失败的那些也
    有了重试机会。

    派生键由原对象键确定性派生，重复请求天然幂等：已存在直接返回，不重读源。
    失败（源缺失/不可解码/存储异常）只是这条素材没有缩略图，一律返回 False 由
    调用方降级，绝不抛错拖垮当次预览请求。
    """
    source_key = source_key_for_thumbnail(thumbnail_key)
    if source_key is None:
        return False
    try:
        if storage.head_object(thumbnail_key) is not None:
            return True
    except Exception:
        return False
    try:
        source = storage.head_object(source_key)
    except Exception:
        source = None
    if source is None:
        return False
    is_image = _is_still_image(source_key, source.content_type)
    for content in _source_bytes_for_thumbnail(storage, source_key, head_probe=not is_image):
        thumb = (
            extract_image_thumbnail_jpeg(content) if is_image else extract_thumbnail_jpeg(content)
        )
        if thumb is None:
            continue
        try:
            storage.put_object(thumbnail_key, thumb, content_type="image/jpeg")
        except Exception:
            return False
        return True
    return False


def _is_still_image(source_key: str, content_type: str) -> bool:
    """静态图片判定：content_type 优先，对象键后缀兜底.

    图片必须整读、不能头探测：截断的 JPEG 会被 ffmpeg 宽容解出残缺画面，
    落盘后永久固化（评审实测 22MiB 图前 8MiB 派生出"半张图"）。
    """
    if content_type.startswith("image/"):
        return True
    return source_key.lower().endswith(_IMAGE_KEY_SUFFIXES)


def _source_bytes_for_thumbnail(
    storage: StorageAdapter, source_key: str, *, head_probe: bool = True
) -> Iterator[bytes]:
    """抽帧候选内容：先试文件头部，不够再整读（受上限保护）。

    首帧只需要文件头部的 moov 与第一个关键帧，非 faststart 的 MP4 把 moov 放在
    尾部才需要整个文件。素材视频可达数百 MB，一律整读会把预览请求变成内存炸弹，
    因此先用 ``_HEAD_PROBE_BYTES`` 试一次，失败且体积可控时才整读。

    ``head_probe=False``（静态图片）时跳过头探测直接整读：截断的 JPEG 会被
    ffmpeg "成功"解出残缺画面，头探测只允许用在以关键帧为完整性边界的视频上。
    """
    try:
        stored = storage.head_object(source_key)
    except Exception:
        return
    if stored is None:
        return
    if head_probe and stored.size > _HEAD_PROBE_BYTES:
        try:
            head = b"".join(storage.iter_object(source_key, start=0, end=_HEAD_PROBE_BYTES - 1))
        except Exception:
            head = b""
        if head:
            yield head
    if stored.size > _FULL_READ_LIMIT_BYTES:
        return
    try:
        yield storage.get_object(source_key)
    except Exception:
        return
