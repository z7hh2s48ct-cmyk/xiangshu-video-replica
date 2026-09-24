"""Avatar re-hosting: platform CDN links never reach the studio."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest

from app.publish_avatars import MAX_AVATAR_BYTES, avatar_object_key, rehost_avatar
from app.storage import StoredObject

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


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


def transport_for(
    *,
    status: int = 200,
    content_type: str = "image/png",
    content: bytes = PNG,
) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content, headers={"content-type": content_type})

    return httpx.MockTransport(handler)


def rehost(storage: FakeStorage, transport: httpx.MockTransport, url: str = "https://cdn/a.png"):
    return rehost_avatar(
        url,
        storage=storage,
        owner="user-1",
        platform="douyin",
        platform_user_id="uid-1",
        transport=transport,
    )


def test_avatar_is_copied_into_our_storage_and_returns_the_stored_uri() -> None:
    storage = FakeStorage()
    uri = rehost(storage, transport_for())
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
def test_unusable_responses_are_skipped_without_storing(kwargs: dict[str, object]) -> None:
    storage = FakeStorage()
    assert rehost(storage, transport_for(**kwargs)) is None  # type: ignore[arg-type]
    assert storage.objects == {}


def test_network_failure_is_not_fatal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    storage = FakeStorage()
    assert rehost(storage, httpx.MockTransport(handler)) is None
    assert storage.objects == {}


def test_storage_failure_is_not_fatal() -> None:
    storage = FakeStorage()
    storage.failure = RuntimeError("bucket unavailable")
    assert rehost(storage, transport_for()) is None


@pytest.mark.parametrize("url", ["", None, "http://cdn/a.png", "data:image/png;base64,AA=="])
def test_only_https_links_are_fetched(url: str | None) -> None:
    storage = FakeStorage()

    # A transport that would fail the test if it were ever called.
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("non-https avatar must not be fetched")

    assert rehost(storage, httpx.MockTransport(handler), url) is None  # type: ignore[arg-type]
    assert storage.objects == {}
