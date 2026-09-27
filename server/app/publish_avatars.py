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
import http.client
import ipaddress
import logging
import socket
import time
from urllib.parse import urlsplit

from app.storage import StorageAdapter
from app.viral_media import ViralMediaError
from app.viral_media import pinned_connection as _pinned_connection

logger = logging.getLogger(__name__)

MAX_AVATAR_BYTES = 2_000_000
AVATAR_TIMEOUT_SECONDS = 10.0
AVATAR_USER_AGENT = "video-replica-avatar-fetch/1.0"
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
# 头像固定走标准 HTTPS 端口；显式或隐式的其它端口一律拒绝，避免被用作内网端口探测。
_ALLOWED_PORTS = frozenset({None, 443})


class _UnsafeAvatarUrl(Exception):
    """头像 URL 未通过 SSRF 守卫；best-effort 语义下按“跳过”处理，不外泄原因。"""


def _resolve_public_avatar_ips(hostname: str) -> tuple[str, tuple[str, ...]]:
    """解析主机名并要求每个地址都是公网（global）IP。

    返回 ``(IDNA 编码后的 hostname, 已校验公网 IP 元组)``：编码后的 hostname
    才是能安全塞进 ``http.client`` 请求行/Host 头的 ASCII 形式，调用方必须用
    这个返回值而不是原始 ``parsed.hostname``，否则国际化域名会在建连请求时
    抛 ``UnicodeEncodeError``（``ValueError`` 子类，不会被 :func:`_download`
    的 ``except (http.client.HTTPException, OSError)`` 捕获，会击穿“绝不 500”
    的 best-effort 契约）。``first_frames.require_safe_provider_download_url``
    用的是同一套“先就地覆盖 hostname 再返回”的写法（见其 line 1266），本 lane
    沿用同一约定。

    头像复制是尽力而为的装饰动作，绝不能变成探测内网的跳板：回环、私网、
    链路本地、组播以及 RFC2544 基准（含代理合成 fake-ip）等非全局地址一律拒绝。
    ``ipaddress.is_global`` 对 IPv4 组播地址返回 True（实测 224.0.0.1 等），
    因此这里显式再排一次 is_multicast，不把组播地址误当作可连接的公网单播地址。
    """
    try:
        encoded = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise _UnsafeAvatarUrl("avatar host is not a valid IDNA name") from exc
    try:
        infos = socket.getaddrinfo(encoded, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _UnsafeAvatarUrl("avatar host could not be resolved") from exc
    ips: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise _UnsafeAvatarUrl("avatar host must resolve to a public address")
        ips.append(str(ip))
    if not ips:
        raise _UnsafeAvatarUrl("avatar host could not be resolved")
    return encoded, tuple(dict.fromkeys(ips))


def _validate_avatar_url(url: str) -> tuple[str, str, tuple[str, ...]]:
    """校验头像 URL 并返回 ``(hostname, target, connect_ips)``。

    仅 HTTPS、无凭据、标准端口、且主机解析为公网地址。返回的 ``hostname`` 已经
    过 IDNA 编码（ASCII/punycode 形式），``target`` 已确认是纯 ASCII，两者都能
    直接塞进 ``http.client`` 请求行而不会抛 ``UnicodeEncodeError``。返回的
    ``connect_ips`` 会被 :func:`_download` 直接用于建连（DNS pinning），不在真正
    请求时再做第二次 DNS 解析——否则校验时是公网、连接时被 rebinding 换成内网的
    TOCTOU 窗口依然存在（``first_frames.require_safe_provider_download_url``
    也是这个理由才把已校验 IP 一路带到 ``_pinned_connection``，本 lane 沿用同一
    套约定）。
    """
    # urlsplit 对畸形 IPv6 字面量等会抛 ValueError；best-effort 语义下这类
    # 畸形输入必须被当成“跳过”而不是让异常冒泡成 500。
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise _UnsafeAvatarUrl("avatar URL is malformed") from exc
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
    # 用 IDNA 编码后的 hostname 而不是原始 parsed.hostname：非 ASCII 域名直接
    # 塞进 http.client 请求行/Host 头会抛 UnicodeEncodeError（ValueError 子类），
    # 不会被 _download 的 except (http.client.HTTPException, OSError) 捕获。
    hostname, connect_ips = _resolve_public_avatar_ips(hostname)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    # 平台方生成的头像链接正常都是已百分号编码的纯 ASCII；出现原始非 ASCII
    # 字符说明这条 URL 已经不符合常见 CDN 输出格式，与其冒然二次编码（可能把
    # 已有的 %XX 再编码一遍）不如按 best-effort 语义直接跳过。
    try:
        target.encode("ascii")
    except UnicodeError as exc:
        raise _UnsafeAvatarUrl("avatar URL path is not ASCII-safe") from exc
    return hostname, target, connect_ips


def avatar_object_key(
    owner: str,
    platform: str,
    platform_user_id: str,
    content_type: str,
) -> str:
    """One stable object per (owner, platform, account); identifiers never shape the path."""
    digest = hashlib.sha256(f"{owner}\0{platform}\0{platform_user_id}".encode()).hexdigest()
    return f"publish-avatars/{platform}/{digest}.{_EXTENSIONS[content_type]}"


def _download(hostname: str, target: str, connect_ips: tuple[str, ...]) -> tuple[bytes, str] | None:
    """按已校验的 IP 直连下载，保留 SNI/Host，不做第二次 DNS 解析。

    与 ``first_frames.UrllibApilioTransport.get`` 复用同一套 ``pinned_connection``
    机制（``viral_media`` 为跨模块调用专门开放的公共别名，而不是私有导入）：
    连接直接落在 ``_validate_avatar_url`` 阶段已经校验过的 IP 上，
    ``_verify_peer`` 还会再核对一次实际对端地址，彻底关掉“校验时公网、
    连接时被 DNS rebinding 换成内网”的 TOCTOU 窗口。

    与 first_frames 不同的是，这里所有失败（含 ``_verify_peer`` 不一致）都只
    记日志并返回 ``None``：头像是尽力而为的装饰动作，任何失败都不得让账号
    连接本身失败（见模块 docstring）。
    """
    connection: http.client.HTTPConnection | None = None
    last_error: OSError | None = None
    deadline = time.monotonic() + AVATAR_TIMEOUT_SECONDS
    for index, connect_ip in enumerate(connect_ips):
        remaining = AVATAR_TIMEOUT_SECONDS if index == 0 else deadline - time.monotonic()
        if remaining <= 0:
            break
        candidate = _pinned_connection("https", hostname, 443, connect_ip, remaining)
        try:
            candidate.connect()
        except ViralMediaError as exc:
            candidate.close()
            logger.warning("publish avatar connect address mismatch: %s", type(exc).__name__)
            return None
        except OSError as exc:
            candidate.close()
            last_error = exc
            continue
        connection = candidate
        break
    if connection is None:
        logger.warning(
            "publish avatar fetch failed to connect: %s",
            type(last_error).__name__ if last_error is not None else "NoAddress",
        )
        return None

    response: http.client.HTTPResponse | None = None
    try:
        # 连接到已校验 IP，但保留原始 hostname 作为 SNI/Host，证书校验不受影响。
        connection.request(
            "GET",
            target,
            headers={
                "Host": f"[{hostname}]" if ":" in hostname else hostname,
                "User-Agent": AVATAR_USER_AGENT,
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            logger.warning("publish avatar fetch rejected: http_status=%s", response.status)
            return None
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type not in _EXTENSIONS:
            logger.warning("publish avatar fetch returned unsupported type: %s", content_type)
            return None
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > MAX_AVATAR_BYTES:
                logger.warning("publish avatar exceeds %d bytes; skipped", MAX_AVATAR_BYTES)
                return None
        content = response.read(MAX_AVATAR_BYTES + 1)
        if len(content) > MAX_AVATAR_BYTES:
            logger.warning("publish avatar exceeds %d bytes; skipped", MAX_AVATAR_BYTES)
            return None
        if not content:
            logger.warning("publish avatar fetch returned an empty body")
            return None
        return content, content_type
    except (http.client.HTTPException, OSError) as exc:
        logger.warning("publish avatar fetch failed: %s", type(exc).__name__)
        return None
    finally:
        if response is not None:
            response.close()
        connection.close()


def rehost_avatar(
    url: str | None,
    *,
    storage: StorageAdapter,
    owner: str,
    platform: str,
    platform_user_id: str,
) -> str | None:
    """Return the stored object URI for ``url``, or None when it cannot be copied."""
    if not url:
        return None
    try:
        hostname, target, connect_ips = _validate_avatar_url(url)
    except _UnsafeAvatarUrl as exc:
        logger.warning("publish avatar URL rejected: %s", exc)
        return None
    downloaded = _download(hostname, target, connect_ips)
    if downloaded is None:
        return None
    content, content_type = downloaded
    key = avatar_object_key(owner, platform, platform_user_id, content_type)
    try:
        return storage.put_object(key, content, content_type=content_type).uri
    except Exception as exc:  # noqa: BLE001 - storage boundary; the account still connects
        logger.warning("publish avatar store failed: %s", type(exc).__name__)
        return None
