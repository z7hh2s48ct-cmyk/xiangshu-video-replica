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
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import httpx

from app.storage import StorageAdapter

logger = logging.getLogger(__name__)

MAX_AVATAR_BYTES = 2_000_000
AVATAR_TIMEOUT_SECONDS = 10.0
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
# 头像固定走标准 HTTPS 端口；显式或隐式的其它端口一律拒绝，避免被用作内网端口探测。
_ALLOWED_PORTS = frozenset({None, 443})


class _UnsafeAvatarUrl(Exception):
    """头像 URL 未通过 SSRF 守卫；best-effort 语义下按“跳过”处理，不外泄原因。"""


def _resolve_public_avatar_ips(hostname: str) -> tuple[str, ...]:
    """解析主机名并要求每个地址都是公网（global）IP。

    头像复制是尽力而为的装饰动作，绝不能变成探测内网的跳板：回环、私网、
    链路本地以及 RFC2544 基准（含代理合成 fake-ip）等非全局地址一律拒绝。
    """
    encoded = hostname.encode("idna").decode("ascii")
    try:
        infos = socket.getaddrinfo(encoded, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _UnsafeAvatarUrl("avatar host could not be resolved") from exc
    ips: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise _UnsafeAvatarUrl("avatar host must resolve to a public address")
        ips.append(str(ip))
    if not ips:
        raise _UnsafeAvatarUrl("avatar host could not be resolved")
    return tuple(dict.fromkeys(ips))


def _validate_avatar_url(url: str) -> None:
    """校验头像 URL：仅 HTTPS、无凭据、标准端口、且主机解析为公网地址。"""
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise _UnsafeAvatarUrl("avatar URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise _UnsafeAvatarUrl("avatar URL credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise _UnsafeAvatarUrl("avatar URL port is invalid") from exc
    if port not in _ALLOWED_PORTS:
        raise _UnsafeAvatarUrl("avatar URL port is not allowed")
    hostname = parsed.hostname
    if not hostname:
        raise _UnsafeAvatarUrl("avatar URL host is invalid")
    _resolve_public_avatar_ips(hostname)


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
            # 禁用重定向：否则公网 CDN 上的开放重定向可把请求带进内网，
            # 绕过下单前对初始 URL 的公网校验。
            follow_redirects=False,
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
    if not url:
        return None
    try:
        _validate_avatar_url(url)
    except _UnsafeAvatarUrl as exc:
        logger.warning("publish avatar URL rejected: %s", exc)
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
