"""One delivery attempt: protocol adapter first, browser automation as fallback.

The worker hands this module a decrypted ``storage_state`` and the storage
adapter; everything here runs with no database transaction open. Heavy
vendor / Playwright imports stay inside the adapters so the FastAPI process
never loads them.

Fallback policy: the browser path only runs when the protocol attempt failed
for a reason that a real browser session could still overcome (signing
environment missing, endpoint drift, risk-control block). A platform that
rejected the login state (``account_invalid``) is terminal for both paths.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.media import storage_key_from_uri
from app.publish_credentials import PublishCredentials, credentials_for_platform
from app.publishers.base import PublisherUnavailableError, PublishResult
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)

BROWSER_FALLBACK_ENV = "VIDEO_REPLICA_PUBLISH_BROWSER_FALLBACK"

DeliveryMode = Literal["api", "browser"]


@dataclass(frozen=True)
class DeliveryOutcome:
    result: PublishResult
    mode: DeliveryMode | None


def browser_fallback_enabled() -> bool:
    value = (os.environ.get(BROWSER_FALLBACK_ENV) or "1").strip().lower()
    return value not in {"0", "off", "false", "no"}


def _suffix_for(content_type: str | None, default: str) -> str:
    mapping = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower(), default)


def materialize_asset(
    storage: StorageAdapter,
    storage_uri: str,
    *,
    directory: Path,
    suffix: str,
) -> Path:
    """Stream one stored object into ``directory``; returns the local path."""
    key = storage_key_from_uri(storage_uri)
    target = directory / f"asset{suffix}"
    with target.open("wb") as handle:
        for chunk in storage.iter_object(key):
            handle.write(chunk)
    if target.stat().st_size <= 0:
        raise RuntimeError("stored object is empty")
    return target


@contextmanager
def materialized_media(
    storage: StorageAdapter,
    *,
    video_uri: str,
    video_content_type: str | None,
    cover_uri: str | None,
    cover_content_type: str | None,
) -> Iterator[tuple[Path, Path | None]]:
    with tempfile.TemporaryDirectory(prefix="publish-") as tmp:
        root = Path(tmp)
        video_dir = root / "video"
        video_dir.mkdir()
        video = materialize_asset(
            storage,
            video_uri,
            directory=video_dir,
            suffix=_suffix_for(video_content_type, ".mp4"),
        )
        cover: Path | None = None
        if cover_uri:
            cover_dir = root / "cover"
            cover_dir.mkdir()
            cover = materialize_asset(
                storage,
                cover_uri,
                directory=cover_dir,
                suffix=_suffix_for(cover_content_type, ".jpg"),
            )
        yield video, cover


def deliver_via_api(
    platform: str,
    credentials: PublishCredentials,
    *,
    video_path: Path,
    cover_path: Path | None,
    title: str,
    description: str,
    tags: list[str],
    options: dict[str, Any],
) -> PublishResult:
    if platform == "douyin":
        from app.publishers import douyin_adapter

        return douyin_adapter.publish_to_douyin(
            cookie=credentials.cookie,
            security_sdk=credentials.security_sdk,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            cover_path=cover_path,
            visibility=int(options.get("visibility", 0) or 0),
        )
    if platform == "wechat_channels":
        from app.publishers import channels_adapter

        return channels_adapter.publish_to_channels(
            cookie=credentials.cookie,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            cover_path=cover_path,
        )
    return PublishResult(platform=platform, status="failed", message="该平台暂不支持自动发布")


def deliver_via_browser(
    platform: str,
    storage_state: dict[str, Any],
    *,
    video_path: Path,
    cover_path: Path | None,
    title: str,
    description: str,
    tags: list[str],
    options: dict[str, Any],
) -> PublishResult:
    """Browser-automation delivery; implemented by the follow-up fallback task.

    Kept as an explicit seam so the fallback decision matrix is testable now.
    """
    raise PublisherUnavailableError(platform, "浏览器兜底发布尚未启用")


def deliver(
    platform: str,
    storage_state: dict[str, Any],
    *,
    video_path: Path,
    cover_path: Path | None,
    title: str,
    description: str,
    tags: list[str],
    options: dict[str, Any] | None = None,
) -> DeliveryOutcome:
    opts = options or {}
    try:
        credentials = credentials_for_platform(platform, storage_state)
    except ValueError as exc:
        return DeliveryOutcome(
            PublishResult(
                platform=platform, status="failed", message=str(exc), account_invalid=True
            ),
            None,
        )
    api_result = deliver_via_api(
        platform,
        credentials,
        video_path=video_path,
        cover_path=cover_path,
        title=title,
        description=description,
        tags=tags,
        options=opts,
    )
    if api_result.status == "published" or api_result.account_invalid:
        return DeliveryOutcome(api_result, "api")
    if not browser_fallback_enabled():
        return DeliveryOutcome(api_result, "api")
    logger.info("publish api attempt failed on %s; trying browser fallback", platform)
    try:
        browser_result = deliver_via_browser(
            platform,
            storage_state,
            video_path=video_path,
            cover_path=cover_path,
            title=title,
            description=description,
            tags=tags,
            options=opts,
        )
    except PublisherUnavailableError as exc:
        logger.info("browser fallback unavailable: %s", exc.reason)
        return DeliveryOutcome(api_result, "api")
    except Exception as exc:  # noqa: BLE001 - delivery boundary
        logger.warning("browser fallback failed: %s", type(exc).__name__)
        return DeliveryOutcome(
            PublishResult(
                platform=platform,
                status="failed",
                message=f"{api_result.message or '发布失败'}；浏览器兜底也未成功",
            ),
            "browser",
        )
    return DeliveryOutcome(browser_result, "browser")
