"""Copy a platform avatar into our own storage instead of hotlinking the platform.

Both QR-login paths already parse an identity response for the nickname; the same
response carries the account's avatar. Those links live on platform CDNs that
rate-limit, expire, and may refuse requests made outside the platform's own
pages, so storing the link is not dependable for display. The image is copied
once, at connect time, and the studio reads the stored object.

Re-hosting is best effort by design: an avatar is decoration, and no failure here
may cost the user a connected account.
"""

from __future__ import annotations

import hashlib
import logging

import httpx

from app.storage import StorageAdapter

logger = logging.getLogger(__name__)

MAX_AVATAR_BYTES = 2_000_000
AVATAR_TIMEOUT_SECONDS = 10.0
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def avatar_object_key(
    owner: str,
    platform: str,
    platform_user_id: str,
    content_type: str,
) -> str:
    """One stable object per (owner, platform, account); identifiers never shape the path."""
    digest = hashlib.sha256(f"{owner}\0{platform}\0{platform_user_id}".encode()).hexdigest()
    return f"publish-avatars/{platform}/{digest}.{_EXTENSIONS[content_type]}"


def _download(url: str, transport: httpx.BaseTransport | None) -> tuple[bytes, str] | None:
    with (
        httpx.Client(
            timeout=AVATAR_TIMEOUT_SECONDS,
            follow_redirects=True,
            transport=transport,
        ) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code != 200:
            logger.warning("publish avatar fetch rejected: http_status=%s", response.status_code)
            return None
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in _EXTENSIONS:
            logger.warning("publish avatar fetch returned unsupported type: %s", content_type)
            return None
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > MAX_AVATAR_BYTES:
                logger.warning("publish avatar exceeds %d bytes; skipped", MAX_AVATAR_BYTES)
                return None
    if not content:
        logger.warning("publish avatar fetch returned an empty body")
        return None
    return bytes(content), content_type


def rehost_avatar(
    url: str | None,
    *,
    storage: StorageAdapter,
    owner: str,
    platform: str,
    platform_user_id: str,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Return the stored object URI for ``url``, or None when it cannot be copied."""
    if not url or not url.startswith("https://"):
        return None
    try:
        downloaded = _download(url, transport)
    except httpx.HTTPError as exc:
        logger.warning("publish avatar fetch failed: %s", type(exc).__name__)
        return None
    if downloaded is None:
        return None
    content, content_type = downloaded
    key = avatar_object_key(owner, platform, platform_user_id, content_type)
    try:
        return storage.put_object(key, content, content_type=content_type).uri
    except Exception as exc:  # noqa: BLE001 - storage boundary; the account still connects
        logger.warning("publish avatar store failed: %s", type(exc).__name__)
        return None
