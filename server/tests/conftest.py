from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def enable_dev_identity_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "development")
    monkeypatch.setenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "1")
    monkeypatch.delenv("VIDEO_REPLICA_DESKTOP_USER_ID", raising=False)
