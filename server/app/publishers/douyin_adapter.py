"""Douyin delivery adapter over the vendored protocol publisher.

Heavy vendor imports stay function-local: only the publish worker loads them
(the FastAPI process must never pull curl_cffi/Node machinery). Adapters
never raise — every outcome maps to a :class:`PublishResult` so the worker's
fenced finalize stays simple.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .base import PublishResult

logger = logging.getLogger(__name__)

PLATFORM = "douyin"

_UNAVAILABLE_MESSAGE = "服务端缺少 Node.js 18+ 环境（抖音发布签名依赖），请联系管理员安装后重试"


def _require_node() -> None:
    if shutil.which("node") is None:
        raise RuntimeError(_UNAVAILABLE_MESSAGE)


SecuritySdk = str | dict[str, Any] | None


def _load_publisher(cookie: str, security_sdk: SecuritySdk) -> Any:
    from .vendor.douyin_publisher import publish as douyin_publish

    _require_node()
    sdk: dict[str, Any] | None = None
    if isinstance(security_sdk, dict):
        sdk = security_sdk
    elif security_sdk:
        sdk = json.loads(security_sdk)
    return douyin_publish.DouyinPublisher(cookie=cookie, security_sdk=sdk)


def probe_douyin(cookie: str, security_sdk: SecuritySdk) -> tuple[bool, str | None]:
    """Cheap login-state probe; returns (ok, failure_message)."""
    try:
        publisher = _load_publisher(cookie, security_sdk)
        snapshot = publisher.check_login()
    except Exception as exc:  # noqa: BLE001 - probe boundary
        if "Node.js" in str(exc):
            raise
        logger.warning("douyin login probe failed: %s", type(exc).__name__)
        return False, "抖音登录态校验失败，请重新连接账号"
    if not bool(snapshot.get("ok")):
        return False, str(snapshot.get("message") or "抖音登录态已失效，请重新连接账号")
    return True, None


def publish_to_douyin(
    *,
    cookie: str,
    security_sdk: SecuritySdk,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    cover_path: Path | None,
    visibility: int = 0,
) -> PublishResult:
    """Deliver one video; account_invalid marks the stored login state dead.

    ``visibility`` follows the creator page: 0 public / 1 friends / 2 private.
    """
    try:
        from .vendor.douyin_publisher import publish_options

        publisher = _load_publisher(cookie, security_sdk)
        ok, message = probe_douyin(cookie, security_sdk)
        if not ok:
            return PublishResult(
                platform=PLATFORM,
                status="failed",
                message=message,
                account_invalid=True,
            )
        options = publish_options.PublishOptions.from_mapping(
            {
                "title": title,
                "caption": description,
                "hashtags": tags,
                "visibility_type": visibility if visibility in (0, 1, 2) else 0,
                "timing": 0,
            }
        )
        data = publisher.publish(video_path, options, cover_path=cover_path)
    except Exception as exc:  # noqa: BLE001 - delivery boundary
        logger.warning("douyin publish failed: %s", type(exc).__name__)
        return PublishResult(
            platform=PLATFORM,
            status="failed",
            message=_summarize(exc),
        )
    item_id = data.get("item_id") or data.get("data", {}).get("item_id")
    return PublishResult(
        platform=PLATFORM,
        status="published",
        item_id=str(item_id) if item_id else None,
        message=None if item_id else "发布完成，但平台未返回作品 ID",
    )


def _summarize(exc: Exception) -> str:
    text = str(exc).strip()
    if "Node.js" in text:
        return text
    head = text.splitlines()[0] if text else type(exc).__name__
    return f"抖音发布失败：{head[:300]}"
