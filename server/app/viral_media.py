"""爆款视频共享媒体管线。

每周采集或用户主动解析链接时，通过服务端临时文件分块下载，视频号仅解密
前 128 KiB，然后归档到主存储。PG 租约协调跨进程的不可变对象发布。
列表播放和项目导入使用 cached_only，只读取已归档对象并生成新签名。
项目持有独立副本；临时文件退出即清理，共享云对象不自动删除。
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import quote, urlencode, urljoin, urlsplit

from app.net_safety import FAKE_IP_NETWORK
from app.storage import DownloadIntent, StorageAdapter, StoredObject
from app.viral_decrypt import decrypt_chunks, is_encrypted_mp4
from app.viral_media_preparation import ViralMediaPreparation
from app.viral_tikhub import (
    PLATFORM_DOUYIN,
    PLATFORM_WECHAT,
    PLATFORM_XIAOHONGSHU,
    ViralSourceClient,
    ViralSourceError,
    ViralVideo,
    WechatVideoDetail,
)

VIRAL_STORAGE_PREFIX = "viral"
VIRAL_MEDIA_URL_TTL = timedelta(hours=6)
_DEFAULT_FETCH_TIMEOUT_SECONDS = 60.0
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
# 单文件下载上限：短视频/封面远超此值的必然是异常响应，防止把响应体整读进
# 内存时被恶意或异常源站打爆 API 进程。
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_PUBLIC_DNS_HOST = "cloudflare-dns.com"
_PUBLIC_DNS_IPS = ("1.1.1.1", "1.0.0.1")
_MAX_DNS_RESPONSE_BYTES = 16 * 1024

logger = logging.getLogger(__name__)
_MEDIA_LOCKS = tuple(threading.Lock() for _ in range(32))


@contextmanager
def _closing_chunks(chunks: Iterator[bytes]) -> Iterator[Iterator[bytes]]:
    try:
        yield chunks
    finally:
        close = getattr(chunks, "close", None)
        if callable(close):
            close()


class ViralMediaError(ViralSourceError):
    """爆款媒体获取失败（文案中性，不含供应商名称）。"""


class ViralMediaDNSUnavailable(ViralMediaError):
    """Network resolution failed before any media request was sent."""


@dataclass(frozen=True)
class ViralMediaResult:
    kind: str  # "audio" | "video"
    storage_uri: str
    url: str
    size: int
    content_type: str
    cache_hit: bool
    sha256: str = ""


def _storage_video_id(video_id: str) -> str:
    """保留历史合法 key；仅把含危险路径段的 opaque ID 映射为稳定名称。"""
    parts = video_id.split("/")
    if (
        "\\" not in video_id
        and "\x00" not in video_id
        and all(part not in {"", ".", ".."} for part in parts)
    ):
        return video_id
    digest = hashlib.sha256(video_id.encode("utf-8")).hexdigest()
    return f"unsafe-{digest}"


def viral_media_key(platform: str, video_id: str, kind: str) -> str:
    extension = "mp3" if kind == "audio" else "mp4"
    if platform == PLATFORM_DOUYIN and kind == "video":
        extension = "browser.mp4"
    return f"{VIRAL_STORAGE_PREFIX}/{platform}/{_storage_video_id(video_id)}.{extension}"


def viral_cover_key(platform: str, video_id: str) -> str:
    return f"{VIRAL_STORAGE_PREFIX}/cover/{platform}/{_storage_video_id(video_id)}"


class ViralStorage(Protocol):
    """媒体管线需要的最小存储面（``StorageAdapter`` 的结构子集）."""

    def head_object(self, key: str) -> StoredObject | None: ...

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject: ...

    def put_file(self, key: str, path: Path, *, content_type: str) -> StoredObject: ...

    def get_object(self, key: str) -> bytes: ...

    def iter_object(
        self, key: str, *, start: int = 0, end: int | None = None, chunk_size: int = 1024 * 1024
    ) -> Iterator[bytes]: ...

    def create_download_intent(
        self, key: str, *, expires_in: timedelta, can_read: bool
    ) -> DownloadIntent: ...


class UrlFetcher:
    """下载远端媒体字节（可注入以便测试）.

    上游返回的媒体/封面 URL 属于半可信输入：真实视频号 CDN 链接就是
    ``http://``，因此不能强制 https，但必须拒绝非 http(s) 协议与解析到
    私网/环回/链路本地的地址（``file://``、云元数据 169.254.169.254 等），
    否则被污染的数据源响应可以驱动服务端 SSRF。连接固定到本次校验得到的
    公网 IP；HTTPS 仍使用原主机名做 SNI 与证书校验，每次重定向重新校验。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT_SECONDS,
        max_bytes: int = _MAX_DOWNLOAD_BYTES,
        connection_factory: Callable[[str, str, int, str, float], http.client.HTTPConnection]
        | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.last_content_type: str | None = None
        self._connection_factory = connection_factory or _pinned_connection

    def fetch(self, url: str) -> bytes:
        return b"".join(self.iter_fetch(url))

    def iter_fetch(self, url: str) -> Iterator[bytes]:
        self.last_content_type = None
        current_url = url
        for _redirect in range(6):
            scheme, hostname, port, connect_ip = _resolve_public_http_url(current_url)
            parsed = urlsplit(current_url)
            target = parsed.path or "/"
            if parsed.query:
                target += f"?{parsed.query}"
            host_header = f"[{hostname}]" if ":" in hostname else hostname
            if port != (443 if scheme == "https" else 80):
                host_header = f"{host_header}:{port}"
            connection = self._connection_factory(
                scheme, hostname, port, connect_ip, self.timeout_seconds
            )
            try:
                connection.request(
                    "GET",
                    target,
                    headers={"Host": host_header, "User-Agent": _USER_AGENT},
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ViralMediaError("媒体地址重定向无效")
                    next_url = urljoin(current_url, location)
                    if scheme == "https" and urlsplit(next_url).scheme != "https":
                        raise ViralMediaError("媒体地址禁止降级到不安全连接")
                    current_url = next_url
                    continue
                if response.status < 200 or response.status >= 300:
                    raise ViralMediaError("媒体地址返回异常状态")
                yield from self._iter_response(response)
                return
            finally:
                connection.close()
        raise ViralMediaError("媒体地址重定向次数过多")

    def _read_response(self, response: http.client.HTTPResponse) -> bytes:
        return b"".join(self._iter_response(response))

    def _iter_response(self, response: http.client.HTTPResponse) -> Iterator[bytes]:
        self.last_content_type = response.headers.get("Content-Type")
        declared = response.headers.get("Content-Length")
        try:
            declared_size = int(declared) if declared is not None else None
            if declared_size is not None and (declared_size < 0 or declared_size > self.max_bytes):
                raise ViralMediaError("媒体文件超出可下载大小上限")
        except ValueError as exc:
            raise ViralMediaError("媒体地址返回无效文件长度") from exc
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_bytes:
                raise ViralMediaError("媒体文件超出可下载大小上限")
            yield chunk
        if declared_size is not None and total != declared_size:
            raise ViralMediaError("媒体地址返回不完整文件")
        if total == 0:
            raise ViralMediaError("媒体地址返回空文件")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, connect_ip: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._connect_ip, self.port), self.timeout)
        _verify_peer(self.sock, self._connect_ip)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, connect_ip: str, timeout: float) -> None:
        tls_context = ssl.create_default_context()
        super().__init__(host, port=port, timeout=timeout, context=tls_context)
        self._connect_ip = connect_ip
        self._tls_context = tls_context

    def connect(self) -> None:
        sock = socket.create_connection((self._connect_ip, self.port), self.timeout)
        _verify_peer(sock, self._connect_ip)
        try:
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _verify_peer(sock: socket.socket, expected_ip: str) -> None:
    try:
        actual_ip = str(sock.getpeername()[0])
        if ipaddress.ip_address(actual_ip) != ipaddress.ip_address(expected_ip):
            raise ViralMediaError("媒体连接地址与已验证地址不一致")
    except BaseException:
        sock.close()
        raise


def _pinned_connection(
    scheme: str, hostname: str, port: int, connect_ip: str, timeout: float
) -> http.client.HTTPConnection:
    if scheme == "https":
        return _PinnedHTTPSConnection(hostname, port, connect_ip, timeout)
    return _PinnedHTTPConnection(hostname, port, connect_ip, timeout)


# Public alias (finding C16): ``first_frames`` imports this name instead of
# reaching for the private ``_pinned_connection`` across modules. The private
# name is kept for the in-module call site and the existing tests.
#
# Binding-time snapshot, not a live indirection: ``first_frames`` copies the
# function object into its own ``_pinned_connection`` at import, and the
# in-module call site resolves the private name directly. Tests that need to
# fake the pinned connection must patch ``viral_media._pinned_connection``
# (in-module lane) or ``first_frames._pinned_connection`` (cross-module lane) —
# patching this public alias alone changes neither call path.
pinned_connection = _pinned_connection


def _resolve_fake_ip_domain(hostname: str) -> list[str]:
    """Resolve proxy synthetic DNS via authenticated DoH, without the media URL.

    Only called for non-literal hosts whose system answers are all 198.18/15.
    Resolver connections use fixed public IPs, original SNI and certificate checks.
    No redirects or system DNS are used for the resolver itself.
    """
    query = urlencode({"name": hostname, "type": "A"})
    for resolver_ip in _PUBLIC_DNS_IPS:
        connection = _PinnedHTTPSConnection(_PUBLIC_DNS_HOST, 443, resolver_ip, 5.0)
        try:
            connection.request(
                "GET",
                f"/dns-query?{query}",
                headers={"Host": _PUBLIC_DNS_HOST, "Accept": "application/dns-json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                continue
            body = response.read(_MAX_DNS_RESPONSE_BYTES + 1)
            if len(body) > _MAX_DNS_RESPONSE_BYTES:
                continue
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("Status") != 0 or payload.get("TC"):
                continue
            questions = payload.get("Question")
            if not isinstance(questions, list) or not any(
                isinstance(question, dict)
                and question.get("type") == 1
                and str(question.get("name", "")).lower().rstrip(".")
                == hostname.lower().rstrip(".")
                for question in questions
            ):
                continue
            answers = payload.get("Answer")
            if not isinstance(answers, list):
                continue
            ips = [
                str(ipaddress.IPv4Address(answer["data"]))
                for answer in answers
                if isinstance(answer, dict) and answer.get("type") == 1 and "data" in answer
            ]
            if ips:
                return ips
        except (OSError, http.client.HTTPException, ValueError, TypeError):
            continue
        finally:
            connection.close()
    raise ViralMediaDNSUnavailable("媒体域名无法解析到公网地址，请检查代理或 DNS 设置。")


def _resolve_public_http_url(url: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ViralMediaError("媒体地址协议不受支持")
    hostname = parsed.hostname
    if not hostname:
        raise ViralMediaError("媒体地址缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ViralMediaError("媒体地址不得包含凭据")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ViralMediaError("媒体地址端口无效") from exc
    expected_port = 443 if parsed.scheme == "https" else 80
    if port != expected_port:
        raise ViralMediaError("媒体地址端口不受支持")
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ViralMediaError("媒体地址无法解析") from exc
    if not addresses:
        raise ViralMediaError("媒体地址无法解析")
    resolved_ips = [ipaddress.ip_address(str(address[4][0])) for address in addresses]
    try:
        ipaddress.ip_address(hostname)
        literal_host = True
    except ValueError:
        literal_host = False
    if not literal_host and all(ip in FAKE_IP_NETWORK for ip in resolved_ips):
        resolved_ips = [ipaddress.ip_address(value) for value in _resolve_fake_ip_domain(hostname)]
    if not resolved_ips:
        raise ViralMediaDNSUnavailable("媒体域名无法解析到公网地址，请检查代理或 DNS 设置。")
    public_ips: list[str] = []
    for ip in resolved_ips:
        if not ip.is_global:
            raise ViralMediaError("媒体地址必须指向公网主机")
        ip_text = str(ip)
        if ip_text not in public_ips:
            public_ips.append(ip_text)
    return parsed.scheme, hostname, port, public_ips[0]


def guess_image_content_type(content: bytes, url: str = "") -> str:
    """按魔数判定图片类型，扩展名兜底（部分源站 URL 无扩展名）."""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"\x89PNG"):
        return "image/png"
    if content.startswith(b"GIF8"):
        return "image/gif"
    from mimetypes import guess_type

    guessed = guess_type(urlsplit(url).path)[0]
    return guessed if guessed and guessed.startswith("image/") else "image/jpeg"


def _fetch_or_raise(fetcher: UrlFetcher, url: str) -> bytes:
    try:
        return fetcher.fetch(url)
    except ViralSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - 远端 CDN 可能抛出任意异常
        logger.warning("Viral media download failed: %s", type(exc).__name__)
        raise ViralMediaError("该视频素材暂时无法获取，请稍后重试") from exc


class CoverEnricher:
    """把数据源封面落成自有存储的长期副本（源站签名链接会过期）.

    每个封面只下载一次（head 去重）；下载失败保留源站链接兜底。
    """

    def __init__(self, *, storage: ViralStorage, fetcher: UrlFetcher) -> None:
        self._storage = storage
        self._fetcher = fetcher

    def stable_url(self, platform: str, video_id: str) -> str:
        return f"/api/viral/covers/{platform}/{quote(video_id, safe='')}"

    def enrich(self, video: ViralVideo) -> ViralVideo:
        if not video.cover_url:
            return video
        key = viral_cover_key(video.platform, video.video_id)
        try:
            if self._storage.head_object(key) is None:
                content = _fetch_or_raise(self._fetcher, video.cover_url)
                self._storage.put_object(
                    key,
                    content,
                    content_type=guess_image_content_type(content, video.cover_url),
                )
        except Exception as exc:
            logger.warning(
                "Viral cover unavailable for %s/%s: %s",
                video.platform,
                video.video_id,
                type(exc).__name__,
            )
            return video
        return replace(
            video,
            cover_key=key,
            cover_url=self.stable_url(video.platform, video.video_id),
        )


class ViralMediaPipeline:
    """按平台把爆款视频媒体取回主存储并返回可播放/下载的签名地址."""

    def __init__(
        self,
        *,
        client: ViralSourceClient | None,
        storage: ViralStorage,
        fetcher: UrlFetcher | None = None,
        validator: Callable[[Path, str, str | None], None] | None = None,
        shared: bool = False,
        cached_only: bool = False,
        refresh_video: Callable[[ViralVideo], ViralVideo] | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._storage = storage
        self._fetcher = fetcher or UrlFetcher()
        self._validator = validator
        self._shared = shared
        self._cached_only = cached_only
        self._refresh_video = refresh_video
        self._cancellation_check = cancellation_check or (lambda: None)
        self.detail: WechatVideoDetail | None = None

    def fetch(self, video: ViralVideo, *, prefer: str | None = None) -> ViralMediaResult:
        kind, content_type = self._resolve_kind(video, prefer)
        key = viral_media_key(video.platform, video.video_id, kind)
        self.detail = None

        def prepare(destination: str, check: Callable[[], None]) -> StoredObject:
            def combined_check() -> None:
                check()
                self._cancellation_check()

            combined_check()
            current = self._refresh_video(video) if self._refresh_video is not None else video
            combined_check()
            return self._download_to_storage(
                current, kind, destination, content_type, combined_check
            )

        if self._shared:
            coordinator = ViralMediaPreparation(storage=cast(StorageAdapter, self._storage))
            if self._cached_only:
                stored = coordinator.cached(
                    platform=video.platform, video_id=video.video_id, kind=kind
                )
                if stored is None:
                    from app.viral_media_preparation import ViralMediaBusy

                    raise ViralMediaBusy("该视频的云端素材尚未准备完成，请稍后重试。")
                cache_hit = True
            else:
                stored, cache_hit = coordinator.fetch(
                    platform=video.platform, video_id=video.video_id, kind=kind, prepare=prepare
                )
            if cache_hit:
                self._validate_cached(stored.key, kind, stored.content_type)
            return self._result(stored.key, stored, kind, stored.content_type, cache_hit=cache_hit)
        # Isolated pipeline callers retain bounded local locking; every customer
        # entry point explicitly uses shared PostgreSQL preparation above.
        with _MEDIA_LOCKS[hash(key) % len(_MEDIA_LOCKS)]:
            existing = self._storage.head_object(key)
            if existing is not None:
                self._validate_cached(key, kind, existing.content_type)
                return self._result(key, existing, kind, content_type, cache_hit=True)
            stored = prepare(key, lambda: None)
            return self._result(key, stored, kind, content_type, cache_hit=False)

    def _validate_cached(self, key: str, kind: str, content_type: str) -> None:
        if self._validator is None:
            return
        with tempfile.TemporaryDirectory(prefix="viral-media-") as directory:
            path = Path(directory) / "source"
            with (
                _closing_chunks(self._storage.iter_object(key)) as chunks,
                path.open("wb") as output,
            ):
                for chunk in chunks:
                    output.write(chunk)
            self._validator(path, kind, content_type)

    def _download_to_storage(
        self, video: ViralVideo, kind: str, key: str, content_type: str, check: Callable[[], None]
    ) -> StoredObject:
        decode_key: str | None = None
        if video.platform == PLATFORM_WECHAT:
            detail = self._wechat_detail(video)
            self.detail = detail
            url, decode_key = detail.full_url, detail.decode_key
            if not url or not decode_key:
                raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")
        else:
            url = video.audio_url if kind == "audio" else video.play_url
        if not url:
            raise ViralMediaError("该视频暂无可用的媒体地址")
        with tempfile.TemporaryDirectory(prefix="viral-media-") as directory:
            path = Path(directory) / "source"
            head = bytearray()
            try:
                with (
                    _closing_chunks(self._fetcher.iter_fetch(url)) as source,
                    path.open("wb") as output,
                ):
                    chunks = decrypt_chunks(source, decode_key) if decode_key else source
                    for chunk in chunks:
                        check()
                        if len(head) < 12:
                            head.extend(chunk[: 12 - len(head)])
                        output.write(chunk)
            except ViralSourceError:
                raise
            except Exception as exc:
                logger.warning("Viral media download failed: %s", type(exc).__name__)
                raise ViralMediaError("该视频素材暂时无法获取，请稍后重试") from exc
            if not head or (kind == "video" and is_encrypted_mp4(bytes(head))):
                raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")
            if kind == "audio" and head[4:8] == b"ftyp":
                content_type = "audio/mp4"
            if self._validator is not None:
                self._validator(path, kind, self._fetcher.last_content_type)
            check()
            return self._storage.put_file(key, path, content_type=content_type)

    # -- 内部 -----------------------------------------------------------------

    def _resolve_kind(self, video: ViralVideo, prefer: str | None = None) -> tuple[str, str]:
        if video.platform in (PLATFORM_DOUYIN, PLATFORM_XIAOHONGSHU):
            if prefer == "video":
                if video.play_url or self._cached_only:
                    return "video", "video/mp4"
                raise ViralMediaError("该视频暂无可用的媒体地址")
            if video.audio_url:
                return "audio", "audio/mpeg"
            if video.play_url:
                return "video", "video/mp4"
            raise ViralMediaError("该视频暂无可用的媒体地址")
        if video.platform == PLATFORM_WECHAT:
            return "video", "video/mp4"
        raise ViralMediaError("暂不支持的视频平台")

    def _wechat_detail(self, video: ViralVideo) -> WechatVideoDetail:
        export_id = str(video.native.get("export_id") or "")
        if not export_id or self._client is None:
            raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")
        nonce = video.native.get("object_nonce_id") or None
        try:
            return self._client.wechat_video_detail(export_id=export_id, object_nonce_id=nonce)
        except ViralSourceError as exc:
            raise ViralMediaError("该视频素材暂时无法获取，请稍后重试") from exc

    def _result(
        self,
        key: str,
        stored: StoredObject,
        kind: str,
        content_type: str,
        *,
        cache_hit: bool,
    ) -> ViralMediaResult:
        intent = self._storage.create_download_intent(
            key, expires_in=VIRAL_MEDIA_URL_TTL, can_read=True
        )
        return ViralMediaResult(
            kind=kind,
            storage_uri=stored.uri,
            url=intent.url,
            size=stored.size,
            content_type=stored.content_type,
            cache_hit=cache_hit,
            sha256=str(getattr(stored, "sha256", "")),
        )
