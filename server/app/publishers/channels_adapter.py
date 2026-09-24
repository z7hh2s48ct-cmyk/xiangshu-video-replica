"""WeChat Channels delivery adapter over the vendored protocol publisher.

The adapter always hands the vendor a concrete cover file (user-selected or a
pre-extracted temporary frame): the upstream default writes its auto-extracted
cover into the module directory, which would race between concurrent
publishes. Adapters never raise — every outcome maps to a
:class:`PublishResult`.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .base import PublishResult

logger = logging.getLogger(__name__)

PLATFORM = "wechat_channels"


def _load_publisher(cookie: str) -> Any:
    from .vendor.weixinshipinhao_publisher import channels_publisher

    return channels_publisher.ChannelsPublisher(cookie=cookie)


def probe_channels(cookie: str) -> tuple[bool, str | None]:
    try:
        _load_publisher(cookie).auth_data()
    except Exception as exc:  # noqa: BLE001 - probe boundary
        logger.warning("channels login probe failed: %s", type(exc).__name__)
        head = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return False, f"视频号登录态已失效：{head[:200]}"
    return True, None


def _resolve_cover(video_path: Path, cover_path: Path | None) -> Path | None:
    if cover_path is not None:
        return cover_path
    from .vendor.weixinshipinhao_publisher import channels_publisher

    out = Path(tempfile.gettempdir()) / f"publish-cover-{uuid.uuid4().hex}.jpg"
    channels_publisher.extract_cover_jpeg(str(video_path), str(out))
    return out if out.exists() else None


def publish_to_channels(
    *,
    cookie: str,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    cover_path: Path | None,
) -> PublishResult:
    try:
        publisher = _load_publisher(cookie)
        ok, message = probe_channels(cookie)
        if not ok:
            return PublishResult(
                platform=PLATFORM,
                status="failed",
                message=message,
                account_invalid=True,
            )
        cover = _resolve_cover(video_path, cover_path)
        data = publisher.publish(
            str(video_path),
            description=description,
            short_title=(title or "").strip()[:16],
            topics=tags or None,
            cover_path=str(cover) if cover else None,
        )
    except Exception as exc:  # noqa: BLE001 - delivery boundary
        logger.warning("channels publish failed: %s", type(exc).__name__)
        head = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return PublishResult(
            platform=PLATFORM,
            status="failed",
            message=f"视频号发布失败：{head[:300]}",
        )
    export_id = data.get("exportId")
    return PublishResult(
        platform=PLATFORM,
        status="published",
        item_id=str(export_id) if export_id else None,
        short_url=str(data["shortUrl"]) if data.get("shortUrl") else None,
        message=None if data.get("shortUrl") else "发布完成，但未能解析作品短链",
    )
