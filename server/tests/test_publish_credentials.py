"""storage_state → protocol-publisher credentials; pure functions on synthetic data."""

from __future__ import annotations

import json

import pytest

from app.publish_credentials import (
    PublishCredentials,
    cookie_header_from_storage,
    credentials_for_platform,
    douyin_security_sdk,
)

_SDK = {
    "ec_privateKey": "-----BEGIN EC PRIVATE KEY-----\\nAAAA\\n-----END EC PRIVATE KEY-----",
    "ec_publicKey": "-----BEGIN PUBLIC KEY-----\\nBBBB\\n-----END PUBLIC KEY-----",
    "ticket": "synthetic-ticket",
    "ts_sign": "ts.2.synthetic",
}


def _storage() -> dict[str, object]:
    return {
        "cookies": [
            {"name": "sessionid", "value": "s1", "domain": ".douyin.com", "path": "/"},
            {"name": "csrf", "value": "c1", "domain": "creator.douyin.com", "path": "/"},
            {"name": "other", "value": "x", "domain": "example.com", "path": "/"},
            {"name": "wxuin", "value": "w1", "domain": ".weixin.qq.com", "path": "/"},
            {"name": "qq_root", "value": "q1", "domain": ".qq.com", "path": "/"},
        ],
        "origins": [
            {
                "origin": "https://creator.douyin.com",
                "localStorage": [
                    {"name": "unrelated", "value": "1"},
                    {"name": "security-sdk", "value": json.dumps(_SDK)},
                ],
            },
            {"origin": "https://www.douyin.com", "localStorage": []},
        ],
    }


def test_cookie_header_filters_by_platform_hosts_and_preserves_order() -> None:
    header = cookie_header_from_storage(_storage(), ("douyin.com",))
    assert header == "sessionid=s1; csrf=c1"
    channels = cookie_header_from_storage(_storage(), ("weixin.qq.com", "qq.com"))
    assert channels == "wxuin=w1; qq_root=q1"
    assert "example.com" not in header and "other=" not in channels


def test_cookie_header_ignores_malformed_entries() -> None:
    storage = {
        "cookies": [{"name": "a"}, "junk", {"name": "b", "value": 3, "domain": ".douyin.com"}]
    }
    assert cookie_header_from_storage(storage, ("douyin.com",)) == ""


def test_douyin_security_sdk_reads_creator_local_storage() -> None:
    sdk = douyin_security_sdk(_storage())
    assert sdk == _SDK


def test_douyin_security_sdk_falls_back_to_scanning_for_ticket_material() -> None:
    storage = _storage()
    origins = storage["origins"]
    assert isinstance(origins, list)
    origins[0]["localStorage"] = [{"name": "Renamed-Key", "value": json.dumps(_SDK)}]
    assert douyin_security_sdk(storage) == _SDK


@pytest.mark.parametrize(
    "storage",
    [
        {},
        {"origins": []},
        {"origins": [{"origin": "https://www.douyin.com", "localStorage": []}]},
        {
            "origins": [
                {
                    "origin": "https://creator.douyin.com",
                    "localStorage": [{"name": "security-sdk", "value": "not json"}],
                }
            ]
        },
        {
            "origins": [
                {
                    "origin": "https://creator.douyin.com",
                    "localStorage": [
                        {"name": "security-sdk", "value": json.dumps({"ticket": "t"})}
                    ],
                }
            ]
        },
    ],
)
def test_douyin_security_sdk_returns_none_when_material_missing_or_incomplete(
    storage: dict[str, object],
) -> None:
    assert douyin_security_sdk(storage) is None


def test_credentials_for_platform_bundles_cookie_and_sdk() -> None:
    douyin = credentials_for_platform("douyin", _storage())
    assert douyin == PublishCredentials(cookie="sessionid=s1; csrf=c1", security_sdk=_SDK)
    channels = credentials_for_platform("wechat_channels", _storage())
    assert channels == PublishCredentials(cookie="wxuin=w1; qq_root=q1", security_sdk=None)


def test_credentials_for_platform_rejects_empty_cookie_and_unknown_platform() -> None:
    with pytest.raises(ValueError, match="登录态"):
        credentials_for_platform("douyin", {"cookies": [], "origins": []})
    with pytest.raises(ValueError, match="平台"):
        credentials_for_platform("xiaohongshu", _storage())


def test_credentials_repr_never_contains_secret_material() -> None:
    credentials = credentials_for_platform("douyin", _storage())
    text = repr(credentials) + str(credentials)
    assert "s1" not in text and "synthetic-ticket" not in text
