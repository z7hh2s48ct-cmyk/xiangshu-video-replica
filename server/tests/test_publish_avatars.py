"""Avatar re-hosting: platform CDN links never reach the studio."""

from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.publish_avatars import (
    MAX_AVATAR_BYTES,
    _validate_avatar_url,
    avatar_object_key,
    rehost_avatar,
)
from app.storage import StoredObject
from app.viral_media import ViralMediaError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

_REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def _fake_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """让测试用的 ``cdn`` 主机解析为公网 IP；IP 字面量仍走真实解析。

    SSRF 守卫会在建立连接前做 DNS 解析，因此把示例 CDN 主机映射到一个
    公网地址，既保留既有用例语义，又能让新增的内网拒绝用例走真实判定。
    """

    def fake_getaddrinfo(host: object, *args: object, **kwargs: object) -> list[object]:
        if host == "cdn":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        return _REAL_GETADDRINFO(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("app.publish_avatars.socket.getaddrinfo", fake_getaddrinfo)


@dataclass
class FakeStorage:
    """Only ``put_object`` is exercised; the rest of the protocol is unused here."""

    def __post_init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.failure: Exception | None = None

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        if self.failure is not None:
            raise self.failure
        self.objects[key] = (content, content_type)
        return StoredObject(
            provider="fake",
            bucket="bucket",
            key=key,
            uri=f"cos://bucket/{key}",
            size=len(content),
            content_type=content_type,
            sha256="0" * 64,
            updated_at=datetime.now(UTC),
        )


class _FakeResponse:
    """Stand-in for ``http.client.HTTPResponse``: only the fields ``_download`` touches."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            return self._body
        return self._body[:amt]

    def close(self) -> None:
        pass


@dataclass
class _FakeConnection:
    """Stand-in for the ``http.client.HTTPSConnection`` returned by ``pinned_connection``."""

    response: _FakeResponse
    connect_error: Exception | None = None
    closed: bool = field(default=False, init=False)

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def request(self, method: str, target: str, headers: dict[str, str] | None = None) -> None:
        pass

    def getresponse(self) -> _FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def install_fake_pinned_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    content_type: str = "image/png",
    content: bytes = PNG,
    connect_error: Exception | None = None,
) -> None:
    """Patch ``app.publish_avatars._pinned_connection`` with a canned fake.

    ``pinned_connection`` is the same cross-module alias ``first_frames`` uses
    (see ``viral_media.py``'s note on finding C16); patching the module-local
    alias here mirrors how ``first_frames``' own tests fake it, rather than
    reaching for the private ``viral_media._pinned_connection``.
    """
    headers = {"Content-Type": content_type, "Content-Length": str(len(content))}

    def fake_pinned_connection(
        scheme: str, hostname: str, port: int, connect_ip: str, timeout: float
    ) -> http.client.HTTPConnection:
        del scheme, hostname, port, connect_ip, timeout  # unused by the fake
        return _FakeConnection(_FakeResponse(status, headers, content), connect_error)  # type: ignore[return-value]

    monkeypatch.setattr("app.publish_avatars._pinned_connection", fake_pinned_connection)


def install_forbidden_pinned_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any call means the URL should have been rejected before reaching the network."""

    def fake_pinned_connection(*args: object, **kwargs: object) -> http.client.HTTPConnection:
        raise AssertionError("unsafe avatar URL must not be fetched")

    monkeypatch.setattr("app.publish_avatars._pinned_connection", fake_pinned_connection)


def rehost(storage: FakeStorage, url: str = "https://cdn/a.png") -> str | None:
    return rehost_avatar(
        url,
        storage=storage,
        owner="user-1",
        platform="douyin",
        platform_user_id="uid-1",
    )


def test_avatar_is_copied_into_our_storage_and_returns_the_stored_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pinned_connection(monkeypatch)
    storage = FakeStorage()
    uri = rehost(storage)
    assert uri is not None
    assert uri.startswith("cos://bucket/publish-avatars/douyin/")
    assert list(storage.objects.values()) == [(PNG, "image/png")]


def test_object_key_hides_the_platform_identifiers_and_is_stable() -> None:
    key = avatar_object_key("user-1", "douyin", "uid-1", "image/png")
    assert key == avatar_object_key("user-1", "douyin", "uid-1", "image/png")
    assert "uid-1" not in key and "user-1" not in key
    assert key.startswith("publish-avatars/douyin/") and key.endswith(".png")
    # A different account never shares an object.
    assert key != avatar_object_key("user-1", "douyin", "uid-2", "image/png")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": 403},
        {"status": 404},
        {"content_type": "text/html"},
        {"content_type": "image/svg+xml"},
        {"content": b""},
        {"content": b"0" * (MAX_AVATAR_BYTES + 1)},
    ],
)
def test_unusable_responses_are_skipped_without_storing(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object]
) -> None:
    install_fake_pinned_connection(monkeypatch, **kwargs)  # type: ignore[arg-type]
    storage = FakeStorage()
    assert rehost(storage) is None
    assert storage.objects == {}


def test_network_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pinned_connection(monkeypatch, connect_error=OSError("boom"))
    storage = FakeStorage()
    assert rehost(storage) is None
    assert storage.objects == {}


def test_verified_address_mismatch_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_verify_peer`` 发现实际对端与已校验 IP 不一致时，同样按 best-effort 跳过。"""
    install_fake_pinned_connection(
        monkeypatch, connect_error=ViralMediaError("媒体连接地址与已验证地址不一致")
    )
    storage = FakeStorage()
    assert rehost(storage) is None
    assert storage.objects == {}


def test_storage_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pinned_connection(monkeypatch)
    storage = FakeStorage()
    storage.failure = RuntimeError("bucket unavailable")
    assert rehost(storage) is None


@pytest.mark.parametrize("url", ["", None, "http://cdn/a.png", "data:image/png;base64,AA=="])
def test_only_https_links_are_fetched(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    install_forbidden_pinned_connection(monkeypatch)
    storage = FakeStorage()
    assert rehost(storage, url) is None  # type: ignore[arg-type]
    assert storage.objects == {}


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a.png",  # 回环
        "https://localhost/a.png",  # 回环主机名
        "https://169.254.169.254/a.png",  # 云元数据链路本地
        "https://10.0.0.5/a.png",  # 私网
        "https://198.18.0.1/a.png",  # RFC2544 基准/代理合成 fake-ip
        "https://224.0.0.1/a.png",  # 组播：is_global 对其为 True，须额外用 is_multicast 排除
        "https://user:pass@cdn/a.png",  # 携带凭据
        "https://cdn:8443/a.png",  # 非标准端口
        "https://[::1/a.png",  # 畸形 IPv6 字面量：urlsplit 会抛 ValueError，不得冒泡成 500
        "https://\ufffd.example/a.png",  # 非法 IDNA 主机名：encode('idna') 会抛 UnicodeError
    ],
)
def test_internal_or_malformed_hosts_are_never_fetched(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """SSRF 守卫：内网/链路本地/组播/凭据/非标端口/畸形主机的头像 URL 一律跳过，绝不发起请求。"""
    install_forbidden_pinned_connection(monkeypatch)
    storage = FakeStorage()
    assert rehost(storage, url) is None
    assert storage.objects == {}


def test_idn_hostname_is_idna_encoded_before_it_reaches_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """国际化域名必须先转成 punycode/ASCII，才不会在建连请求行里抛 UnicodeEncodeError。

    ``_download`` 的 ``except (http.client.HTTPException, OSError)`` 抓不住
    ``UnicodeEncodeError``（``ValueError`` 子类），所以这个保护必须提前发生在
    ``_validate_avatar_url`` 阶段，而不是指望下游兜底。
    """

    def fake_getaddrinfo(host: object, *args: object, **kwargs: object) -> list[object]:
        if host == "xn--r8jz45g.xn--zckzah":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        return _REAL_GETADDRINFO(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("app.publish_avatars.socket.getaddrinfo", fake_getaddrinfo)
    hostname, target, connect_ips = _validate_avatar_url("https://例え.テスト/a.png")
    assert hostname == "xn--r8jz45g.xn--zckzah"
    assert hostname.isascii()
    assert target == "/a.png"
    assert connect_ips == ("93.184.216.34",)


def test_non_ascii_path_is_skipped_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    """path/query 含原始非 ASCII 字符时按 best-effort 跳过，绝不冒泡成 500。"""
    install_forbidden_pinned_connection(monkeypatch)
    storage = FakeStorage()
    assert rehost(storage, "https://cdn/头像.png") is None
    assert storage.objects == {}
