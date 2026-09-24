"""Bridge a Playwright ``storage_state`` to the protocol publishers' credentials.

Both QR-login paths persist the same shape — ``{"cookies": [...], "origins":
[{"origin", "localStorage": [...]}]}`` — encrypted in
``publish_browser_accounts.storage_state_enc``. The vendored protocol
publishers instead take a ``Cookie`` header string and, for douyin, the
``security-sdk`` localStorage entry (ticket-guard material). This module is
the pure translation layer; it performs no I/O and its dataclass hides its
values from ``repr`` so a stray log line never leaks a session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_PLATFORM_COOKIE_HOSTS: dict[str, tuple[str, ...]] = {
    "douyin": ("douyin.com",),
    "wechat_channels": ("weixin.qq.com", "qq.com"),
}
_DOUYIN_CREATOR_ORIGIN = "https://creator.douyin.com"
_SECURITY_SDK_KEY = "security-sdk"
_SECURITY_SDK_REQUIRED = ("ticket", "ts_sign")
_SECURITY_SDK_PRIVATE_KEYS = ("ec_privateKey", "ec_private_key", "ec_private_key_pem")


@dataclass(frozen=True)
class PublishCredentials:
    cookie: str = field(repr=False)
    security_sdk: dict[str, Any] | None = field(default=None, repr=False)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "PublishCredentials(<redacted>)"


def _domain_matches(domain: str, hosts: tuple[str, ...]) -> bool:
    normalized = domain.lstrip(".").lower()
    return any(normalized == host or normalized.endswith("." + host) for host in hosts)


def cookie_header_from_storage(storage: Any, hosts: tuple[str, ...]) -> str:
    """Join ``name=value`` pairs for cookies scoped to any of ``hosts``; order kept."""
    cookies = storage.get("cookies") if isinstance(storage, dict) else None
    if not isinstance(cookies, list):
        return ""
    pairs: list[str] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
        if not (isinstance(name, str) and isinstance(value, str) and isinstance(domain, str)):
            continue
        if name and _domain_matches(domain, hosts):
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _complete_sdk(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    if not all(candidate.get(key) for key in _SECURITY_SDK_REQUIRED):
        return None
    if not any(candidate.get(key) for key in _SECURITY_SDK_PRIVATE_KEYS):
        return None
    return candidate


def douyin_security_sdk(storage: Any) -> dict[str, Any] | None:
    """Return the creator-center ``security-sdk`` localStorage payload, or None."""
    origins = storage.get("origins") if isinstance(storage, dict) else None
    if not isinstance(origins, list):
        return None
    entries: list[dict[str, Any]] = []
    for origin in origins:
        if not isinstance(origin, dict) or origin.get("origin") != _DOUYIN_CREATOR_ORIGIN:
            continue
        local_storage = origin.get("localStorage")
        if isinstance(local_storage, list):
            entries.extend(item for item in local_storage if isinstance(item, dict))
    # Exact key first; fall back to any entry that carries complete ticket-guard material.
    ordered = sorted(
        entries,
        key=lambda item: 0 if str(item.get("name", "")).lower() == _SECURITY_SDK_KEY else 1,
    )
    for item in ordered:
        raw = item.get("value")
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        sdk = _complete_sdk(parsed)
        if sdk is not None:
            return sdk
    return None


def credentials_for_platform(platform: str, storage: Any) -> PublishCredentials:
    hosts = _PLATFORM_COOKIE_HOSTS.get(platform)
    if hosts is None:
        raise ValueError("该平台暂不支持自动发布")
    cookie = cookie_header_from_storage(storage, hosts)
    if not cookie:
        raise ValueError("登录态中没有可用的平台 Cookie，请重新扫码")
    security_sdk = douyin_security_sdk(storage) if platform == "douyin" else None
    return PublishCredentials(cookie=cookie, security_sdk=security_sdk)
