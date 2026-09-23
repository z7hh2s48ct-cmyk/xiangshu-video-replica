"""Publisher abstraction over the vendored platform delivery libraries.

The vendored douyin/channels publishers are reverse-engineered HTTP clients.
This module is the only surface the rest of the app depends on, so the heavy
vendor imports stay inside the adapters and only the publish worker ever
loads them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one platform delivery attempt."""

    platform: str
    status: Literal["published", "failed"]
    item_id: str | None = None
    short_url: str | None = None
    message: str | None = None
    account_invalid: bool = False


class PublisherUnavailableError(RuntimeError):
    """The platform adapter cannot run in this environment (e.g. missing Node)."""

    def __init__(self, platform: str, reason: str) -> None:
        super().__init__(f"{platform} publisher unavailable: {reason}")
        self.platform = platform
        self.reason = reason


class PublisherAuthError(RuntimeError):
    """The platform rejected the stored login state (cookie expired etc.)."""

    def __init__(self, platform: str, reason: str) -> None:
        super().__init__(f"{platform} login state rejected: {reason}")
        self.platform = platform
        self.reason = reason


class PublisherError(RuntimeError):
    """Any other platform-side delivery failure."""

    def __init__(self, platform: str, reason: str) -> None:
        super().__init__(f"{platform} publish failed: {reason}")
        self.platform = platform
        self.reason = reason


class PublisherProbeResult:
    """Login-state probe result for one stored account."""

    __slots__ = ("ok", "nickname", "message")

    def __init__(self, *, ok: bool, nickname: str | None = None, message: str = "") -> None:
        self.ok = ok
        self.nickname = nickname
        self.message = message
