"""Real PG ownership/encryption/cancellation; platform browser responses are fixtures."""

import asyncio
import base64
import json
import os
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_publish_accounts import (
    _clear_dependency_overrides,
    _PgBusinessDb,
    actor,
    client,
    cw068_dsn,
    holder,
    lane_env,
    pg,
)

from app import publish_browser_engine as engine
from app import publish_browser_routes as routes
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.publish_browser import (
    BrowserProbeLeaseLostError,
    claim_browser_probe_work,
    finalize_browser_probe,
)
from app.publish_browser_engine import BrowserEvent, login_events, parse_identity
from app.settings import SETTINGS_KEY_ENV

__all__ = ["_clear_dependency_overrides", "client", "cw068_dsn", "holder", "lane_env", "pg"]
BASE = "/api/studio/publish/browser"


@pytest.mark.parametrize("platform", ["douyin", "wechat_channels", "xiaohongshu"])
def test_browser_scan_encrypts_state_and_never_streams_secrets(
    client: TestClient,
    pg: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    secret = "temporary-" + uuid4().hex
    storage = {"cookies": [{"name": "session", "value": secret}], "origins": []}

    async def events(*_: Any) -> AsyncGenerator[BrowserEvent, None]:
        yield BrowserEvent("qr_ready", image="data:image/png;base64,cXI=")
        yield BrowserEvent(
            "connected",
            identity={"platform_user_id": "uid-1", "username": "平台昵称"},
            storage=storage,
        )

    monkeypatch.setattr(routes, "login_events", events)
    response = client.post(BASE + "/logins", json={"platform": platform})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = [json.loads(line) for line in response.text.splitlines()]
    assert [event["phase"] for event in data] == ["loading", "qr_ready", "connected"]
    assert secret not in response.text and "storage" not in response.text
    row = pg.execute("SELECT storage_state_enc FROM publish_browser_accounts").fetchone()
    assert secret not in row[0]
    assert (
        json.loads(Fernet(os.environ[SETTINGS_KEY_ENV].encode()).decrypt(row[0].encode()))
        == storage
    )
    accounts = client.get(BASE + "/accounts")
    assert accounts.json()[0]["platform_user_id"] == "uid-1"
    assert secret not in accounts.text
    assert pg.execute("SELECT count(*) FROM publish_browser_logins").fetchone()[0] == 0
    # Same verified platform identity updates the same account instead of duplicating it.
    client.post(BASE + "/logins", json={"platform": platform})
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 1


def test_browser_cancel_prevents_late_account_persistence(
    client: TestClient,
    pg: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def events(*_: Any) -> AsyncGenerator[BrowserEvent, None]:
        with pg_transaction() as conn:
            conn.execute("DELETE FROM publish_browser_logins WHERE user_id='employee_1'")
        yield BrowserEvent(
            "connected", identity={"platform_user_id": "late", "username": "迟到账号"}, storage={}
        )

    monkeypatch.setattr(routes, "login_events", events)
    response = client.post(BASE + "/logins", json={"platform": "douyin"})
    assert json.loads(response.text.splitlines()[-1])["phase"] == "closed"
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 0


def test_browser_accounts_and_relogin_are_owner_scoped(
    client: TestClient,
    holder: _PgBusinessDb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def events(*_: Any) -> AsyncGenerator[BrowserEvent, None]:
        yield BrowserEvent(
            "connected", identity={"platform_user_id": "mine", "username": "我的账号"}, storage={}
        )

    monkeypatch.setattr(routes, "login_events", events)
    client.post(BASE + "/logins", json={"platform": "xiaohongshu"})
    account_id = client.get(BASE + "/accounts").json()[0]["id"]
    holder.current_actor = actor("employee_2")
    assert client.get(BASE + "/accounts").json() == []
    assert client.delete(BASE + "/accounts/" + account_id).status_code == 404
    assert (
        client.post(
            BASE + "/logins", json={"platform": "xiaohongshu", "account_id": account_id}
        ).status_code
        == 404
    )
    holder.current_actor = actor("employee_1")
    assert client.delete(BASE + "/accounts/" + account_id).status_code == 200
    assert client.get(BASE + "/accounts").json() == []


def test_browser_failure_is_redacted_and_releases_capacity(
    client: TestClient,
    pg: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-" + uuid4().hex

    async def events(*_: Any) -> AsyncGenerator[BrowserEvent, None]:
        raise RuntimeError(secret)
        yield BrowserEvent("closed")  # pragma: no cover

    monkeypatch.setattr(routes, "login_events", events)
    response = client.post(BASE + "/logins", json={"platform": "wechat_channels"})
    assert secret not in response.text
    assert json.loads(response.text.splitlines()[-1])["phase"] == "closed"
    assert pg.execute("SELECT count(*) FROM publish_browser_logins").fetchone()[0] == 0


def test_browser_qr_alone_never_marks_an_account_connected(
    client: TestClient,
    pg: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def events(*_: Any) -> AsyncGenerator[BrowserEvent, None]:
        yield BrowserEvent("qr_ready", image="data:image/png;base64,cXI=")
        yield BrowserEvent("expired")

    monkeypatch.setattr(routes, "login_events", events)
    response = client.post(BASE + "/logins", json={"platform": "douyin"})
    assert json.loads(response.text.splitlines()[-1])["phase"] == "expired"
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 0


def test_identity_rejects_failed_and_missing_platform_identity() -> None:
    assert (
        parse_identity("douyin", {"status_code": 8, "user": {"uid": "1", "nickname": "未登录"}})
        is None
    )
    assert parse_identity("xiaohongshu", {"success": True, "data": {"userName": "无ID"}}) is None
    assert parse_identity("wechat_channels", {"errCode": 0, "data": None}) is None
    assert parse_identity(
        "douyin", {"status_code": 0, "user": {"uid": 123, "nickname": "本人"}}
    ) == {"platform_user_id": "123", "username": "本人"}
    assert parse_identity(
        "douyin", {"status_code": "0", "user": {"uid": "", "user_id": "456", "nickname": "备用ID"}}
    ) == {"platform_user_id": "456", "username": "备用ID"}
    assert (
        parse_identity("douyin", {"status_code": False, "user": {"uid": "1", "nickname": "未登录"}})
        is None
    )


class _EngineResponse:
    def __init__(
        self,
        body: dict[str, Any],
        *,
        ok: bool = True,
        status: int = 200,
        url: str = "",
    ) -> None:
        self.body = body
        self.ok = ok
        self.status = status
        self.url = url
        self.disposed = False

    async def json(self) -> dict[str, Any]:
        return self.body

    async def dispose(self) -> None:
        self.disposed = True


class _EngineRequest:
    def __init__(self, responses: list[_EngineResponse | BaseException]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> _EngineResponse:
        self.calls.append({"url": url, **kwargs})
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _EnginePage:
    def __init__(self, after_goto_url: str) -> None:
        self.url = "about:blank"
        self.after_goto_url = after_goto_url
        self.goto_url: str | None = None
        self.frames: list[Any] = []
        self.response_handler: Any = None

    def on(self, event: str, handler: Any) -> None:
        assert event == "response"
        self.response_handler = handler

    async def goto(self, url: str = "", **__: Any) -> None:
        self.goto_url = url
        self.url = self.after_goto_url

    async def screenshot(self, **_: Any) -> bytes:
        return b"fullpage-png"


class _EngineContext:
    def __init__(self, page: _EnginePage, request: _EngineRequest) -> None:
        self.page = page
        self.request = request
        self.init_scripts: list[str] = []

    async def new_page(self) -> _EnginePage:
        return self.page

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def storage_state(self, **_: Any) -> dict[str, Any]:
        return {"cookies": [{"name": "session", "value": "secret"}], "origins": []}


class _EngineBrowser:
    def __init__(self, context: _EngineContext) -> None:
        self.context = context
        self.closed = False
        self.context_kwargs: dict[str, Any] = {}
        self.launch_kwargs: dict[str, Any] = {}

    async def new_context(self, **kwargs: Any) -> _EngineContext:
        self.context_kwargs = kwargs
        return self.context

    async def close(self) -> None:
        self.closed = True


class _EnginePlaywright:
    def __init__(self, browser: _EngineBrowser) -> None:
        self.chromium = self
        self.browser = browser

    async def launch(self, **kwargs: Any) -> _EngineBrowser:
        self.browser.launch_kwargs = kwargs
        return self.browser


class _EnginePlaywrightManager:
    def __init__(self, browser: _EngineBrowser) -> None:
        self.playwright = _EnginePlaywright(browser)

    async def __aenter__(self) -> _EnginePlaywright:
        return self.playwright

    async def __aexit__(self, *_: Any) -> None:
        return None


def _install_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_url: str,
    responses: list[_EngineResponse | BaseException],
) -> tuple[_EngineRequest, _EngineBrowser, list[float]]:
    page = _EnginePage(page_url)
    request = _EngineRequest(responses)
    browser = _EngineBrowser(_EngineContext(page, request))
    clock = [100.0]

    monkeypatch.setattr(engine, "async_playwright", lambda: _EnginePlaywrightManager(browser))
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])

    async def advance(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(engine.asyncio, "sleep", advance)

    async def unexpected_qr(*_: Any) -> None:
        raise AssertionError("creator backend must not be captured as a QR code")

    monkeypatch.setattr(engine, "qr_locator", unexpected_qr)
    return request, browser, clock


def test_douyin_creator_backend_probes_identity_and_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _EngineResponse(
        {"status_code": "0", "user": {"uid": "douyin-1", "nickname": "抖音账号"}}
    )
    request, browser, _ = _install_engine(
        monkeypatch,
        page_url="https://creator.douyin.com/creator-micro/content/manage",
        responses=[response],
    )

    async def run() -> BrowserEvent:
        events = login_events("douyin")
        try:
            return await anext(events)
        finally:
            await events.aclose()

    event = asyncio.run(run())

    assert event.phase == "connected"
    assert event.identity == {"platform_user_id": "douyin-1", "username": "抖音账号"}
    assert request.calls == [
        {
            "url": "https://creator.douyin.com/web/api/media/user/info/?aid=1128",
            "timeout": 5000,
            "max_redirects": 0,
        }
    ]
    assert response.disposed is True
    assert browser.closed is True


def test_douyin_probe_is_rate_limited_and_creator_backend_never_emits_qr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = _EngineResponse({"status_code": 8})
    request, browser, clock = _install_engine(
        monkeypatch,
        page_url="https://creator.douyin.com/creator-micro",
        responses=[RuntimeError("private response details"), second],
    )

    async def run() -> list[BrowserEvent]:
        events = login_events("douyin")
        try:
            one = await anext(events)
            two = await anext(events)
            clock[0] += 1.5
            three = await anext(events)
            return [one, two, three]
        finally:
            await events.aclose()

    events = asyncio.run(run())

    assert [event.phase for event in events] == ["confirming", "confirming", "confirming"]
    assert len(request.calls) == 2
    assert second.disposed is True
    assert browser.closed is True


def test_login_paths_route_only_channels_to_platform() -> None:
    # 抖音根路径已验证能直接出码，不能被改成 /platform；仅视频号走 /platform。
    assert engine.LOGIN_PATHS == {
        "douyin": "",
        "wechat_channels": "/platform",
        "xiaohongshu": "",
    }


def test_wechat_channels_fullpage_fallback_streams_page_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, browser, _ = _install_engine(
        monkeypatch,
        page_url="https://channels.weixin.qq.com/platform",
        responses=[],
    )

    async def no_qr(*_: Any) -> None:
        return None

    monkeypatch.setattr(engine, "qr_locator", no_qr)

    async def run() -> list[BrowserEvent]:
        seen: list[BrowserEvent] = []
        events = login_events("wechat_channels")
        try:
            async for event in events:
                seen.append(event)
                if event.phase == "qr_ready":
                    return seen
        finally:
            await events.aclose()
        return seen

    seen = asyncio.run(run())

    # grace 期内只报 loading；超过 grace 后用整页截图兜底成可扫图像。
    assert [event.phase for event in seen[:-1]] == ["loading"] * 4
    fallback = seen[-1]
    assert fallback.phase == "qr_ready"
    assert fallback.image == "data:image/png;base64," + base64.b64encode(b"fullpage-png").decode()
    # 视频号 goto /platform；并注入 stealth（launch args + init script + UA/locale）。
    page = browser.context.page
    assert page.goto_url == "https://channels.weixin.qq.com/platform"
    assert browser.launch_kwargs["args"] == ["--disable-blink-features=AutomationControlled"]
    assert browser.context_kwargs["locale"] == "zh-CN"
    assert "Chrome/" in browser.context_kwargs["user_agent"]
    assert any("webdriver" in script for script in browser.context.init_scripts)
    assert browser.closed is True


def test_douyin_late_failed_response_does_not_clear_verified_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, browser, _ = _install_engine(
        monkeypatch,
        page_url="https://creator.douyin.com/",
        responses=[],
    )
    page = browser.context.page

    async def harmless_qr(*_: Any) -> None:
        return None

    monkeypatch.setattr(engine, "qr_locator", harmless_qr)
    real_sleep = asyncio.tasks.sleep

    async def run() -> BrowserEvent:
        events = login_events("douyin")
        try:
            assert (await anext(events)).phase == "loading"
            page.response_handler(
                _EngineResponse(
                    {"status_code": 0, "user": {"uid": "douyin-1", "nickname": "已验证"}},
                    url="https://creator.douyin.com/web/api/media/user/info/?aid=1128",
                )
            )
            page.response_handler(
                _EngineResponse(
                    {"status_code": 8},
                    url="https://creator.douyin.com/web/api/media/user/info/?aid=1128",
                )
            )
            await real_sleep(0)
            return await anext(events)
        finally:
            await events.aclose()

    event = asyncio.run(run())

    assert event.phase == "connected"
    assert event.identity == {"platform_user_id": "douyin-1", "username": "已验证"}
    assert browser.closed is True


def test_douyin_failed_probe_does_not_clear_identity_captured_while_awaiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, browser, _ = _install_engine(
        monkeypatch,
        page_url="https://creator.douyin.com/creator-micro",
        responses=[],
    )
    page = browser.context.page
    failed_probe = _EngineResponse({"status_code": 8})

    class RacingRequest(_EngineRequest):
        async def get(self, url: str, **kwargs: Any) -> _EngineResponse:
            self.calls.append({"url": url, **kwargs})
            page.response_handler(
                _EngineResponse(
                    {"status_code": 0, "user": {"uid": "douyin-2", "nickname": "并发账号"}},
                    url="https://creator.douyin.com/web/api/media/user/info/?aid=1128",
                )
            )
            await asyncio.tasks.sleep(0)
            return failed_probe

    browser.context.request = RacingRequest([])

    async def run() -> BrowserEvent:
        events = login_events("douyin")
        try:
            return await anext(events)
        finally:
            await events.aclose()

    event = asyncio.run(run())

    assert event.phase == "connected"
    assert event.identity == {"platform_user_id": "douyin-2", "username": "并发账号"}
    assert failed_probe.disposed is True
    assert browser.closed is True


def test_douyin_navigation_away_during_probe_cannot_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, browser, _ = _install_engine(
        monkeypatch,
        page_url="https://creator.douyin.com/creator-micro",
        responses=[],
    )
    page = browser.context.page

    class NavigatingRequest(_EngineRequest):
        async def get(self, url: str, **kwargs: Any) -> _EngineResponse:
            self.calls.append({"url": url, **kwargs})
            page.url = "https://example.com/creator-micro"
            return _EngineResponse(
                {"status_code": 0, "user": {"uid": "douyin-3", "nickname": "跳转账号"}}
            )

    browser.context.request = NavigatingRequest([])

    async def harmless_qr(*_: Any) -> None:
        return None

    monkeypatch.setattr(engine, "qr_locator", harmless_qr)

    async def run() -> BrowserEvent:
        events = login_events("douyin")
        try:
            return await anext(events)
        finally:
            await events.aclose()

    event = asyncio.run(run())

    assert event.phase == "loading"
    assert browser.closed is True


def test_douyin_foreign_page_cannot_probe_or_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    request, browser, _ = _install_engine(
        monkeypatch,
        page_url="https://example.com/creator-micro",
        responses=[
            _EngineResponse({"status_code": 0, "user": {"uid": "attacker", "nickname": "伪造账号"}})
        ],
    )

    async def harmless_qr(*_: Any) -> None:
        return None

    monkeypatch.setattr(engine, "qr_locator", harmless_qr)

    async def run() -> BrowserEvent:
        events = login_events("douyin")
        try:
            return await anext(events)
        finally:
            await events.aclose()

    event = asyncio.run(run())

    assert event.phase == "loading"
    assert request.calls == []
    assert browser.closed is True


def test_douyin_cancel_during_probe_closes_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    class CancelDuringJson(_EngineResponse):
        async def json(self) -> dict[str, Any]:
            raise asyncio.CancelledError

        async def dispose(self) -> None:
            raise RuntimeError("private disposal details")

    request, browser, _ = _install_engine(
        monkeypatch,
        page_url="https://creator.douyin.com/creator-micro",
        responses=[CancelDuringJson({})],
    )

    async def run() -> None:
        events = login_events("douyin")
        with pytest.raises(asyncio.CancelledError):
            await anext(events)
        await events.aclose()

    asyncio.run(run())

    assert len(request.calls) == 1
    assert browser.closed is True


def test_relogin_wrong_account_preserves_original_encrypted_state(
    client: TestClient, pg: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = "first"
    received: list[Any] = []

    async def events(_: Any, storage: Any) -> AsyncGenerator[BrowserEvent, None]:
        received.append(storage)
        yield BrowserEvent(
            "connected",
            identity={"platform_user_id": uid, "username": uid},
            storage={"origins": []},
        )

    monkeypatch.setattr(routes, "login_events", events)
    client.post(BASE + "/logins", json={"platform": "douyin"})
    original = pg.execute("SELECT id,storage_state_enc FROM publish_browser_accounts").fetchone()
    uid = "different"
    response = client.post(BASE + "/logins", json={"platform": "douyin", "account_id": original[0]})
    assert "扫码账号与原账号不同" in response.text
    assert received == [None, {"origins": []}]
    assert pg.execute("SELECT id,storage_state_enc FROM publish_browser_accounts").fetchall() == [
        original
    ]


def test_changed_actor_during_scan_cannot_persist_and_closes_browser(
    client: TestClient,
    pg: psycopg.Connection,
    holder: _PgBusinessDb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    async def events(*_: Any) -> AsyncGenerator[BrowserEvent, None]:
        try:
            holder.current_actor = actor("employee_2")
            yield BrowserEvent(
                "connected", identity={"platform_user_id": "late", "username": "late"}, storage={}
            )
        finally:
            closed.append(True)

    monkeypatch.setattr(routes, "login_events", events)
    response = client.post(BASE + "/logins", json={"platform": "douyin"})
    assert "用户会话已变更" in response.text
    assert closed == [True]
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM publish_browser_logins").fetchone()[0] == 0


def test_one_active_scan_per_owner_and_expired_slot_can_be_replaced(
    client: TestClient, pg: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pg_transaction() as conn:
        conn.execute(
            "INSERT INTO publish_browser_logins(id,user_id,platform,expires_at) "
            "VALUES ('busy','employee_1','douyin',clock_timestamp()+interval '1 minute')"
        )
    assert client.post(BASE + "/logins", json={"platform": "douyin"}).status_code == 409
    with pg_transaction() as conn:
        conn.execute(
            "UPDATE publish_browser_logins SET expires_at=clock_timestamp()-interval '1 second'"
        )

    async def events(*_: Any) -> AsyncGenerator[BrowserEvent, None]:
        yield BrowserEvent("expired")

    monkeypatch.setattr(routes, "login_events", events)
    assert client.post(BASE + "/logins", json={"platform": "douyin"}).status_code == 200
    assert pg.execute("SELECT count(*) FROM publish_browser_logins").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# PUBLISH-DELIVERY-20260917: desktop login-state import
# --------------------------------------------------------------------------- #


def _desktop_state(secret: str) -> dict[str, Any]:
    return {
        "cookies": [{"name": "sessionid", "value": secret, "domain": ".douyin.com", "path": "/"}],
        "origins": [
            {
                "origin": "https://creator.douyin.com",
                "localStorage": [{"name": "security-sdk", "value": '{"ticket":"t"}'}],
            }
        ],
    }


def test_import_desktop_account_encrypts_state_and_marks_source(
    client: TestClient, pg: psycopg.Connection
) -> None:
    secret = "desktop-" + uuid4().hex
    response = client.post(
        BASE + "/accounts/import",
        json={
            "platform": "douyin",
            "identity": {"platform_user_id": "uid-desktop", "username": "桌面昵称"},
            "storage_state": _desktop_state(secret),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["platform_user_id"] == "uid-desktop"
    assert body["source"] == "desktop" and body["status"] == "connected"
    assert secret not in response.text
    row = pg.execute(
        "SELECT storage_state_enc, source, status FROM publish_browser_accounts"
    ).fetchone()
    assert secret not in row[0] and row[1] == "desktop" and row[2] == "connected"
    decrypted = json.loads(Fernet(os.environ[SETTINGS_KEY_ENV].encode()).decrypt(row[0].encode()))
    assert decrypted == _desktop_state(secret)
    listed = client.get(BASE + "/accounts").json()
    assert listed[0]["source"] == "desktop" and "storage" not in listed[0]


def test_import_refreshes_same_identity_and_clears_invalid_status(
    client: TestClient, pg: psycopg.Connection
) -> None:
    payload = {
        "platform": "douyin",
        "identity": {"platform_user_id": "uid-1", "username": "旧昵称"},
        "storage_state": _desktop_state("first"),
    }
    first = client.post(BASE + "/accounts/import", json=payload).json()
    pg.execute(
        "UPDATE publish_browser_accounts SET status='invalid', error_message='expired' WHERE id=%s",
        (first["id"],),
    )
    payload["identity"]["username"] = "新昵称"
    payload["storage_state"] = _desktop_state("second")
    second = client.post(BASE + "/accounts/import", json=payload).json()
    assert second["id"] == first["id"]
    assert second["username"] == "新昵称"
    assert second["status"] == "connected" and second["error_message"] is None
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 1


def test_import_rejects_malformed_or_empty_state(
    client: TestClient, pg: psycopg.Connection
) -> None:
    identity = {"platform_user_id": "uid-1", "username": "昵称"}
    for storage in (
        {"cookies": [], "origins": []},
        {"cookies": [{"name": "a", "value": "b"}], "origins": [], "extra": 1},
        {"cookies": "not-a-list", "origins": []},
        {"cookies": [{"name": "a", "value": "b"}], "origins": ["bad"]},
    ):
        response = client.post(
            BASE + "/accounts/import",
            json={"platform": "douyin", "identity": identity, "storage_state": storage},
        )
        assert response.status_code == 422, (storage, response.text)
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 0


def test_import_is_owner_scoped_and_auditor_blocked(
    client: TestClient, pg: psycopg.Connection, holder: _PgBusinessDb
) -> None:
    payload = {
        "platform": "wechat_channels",
        "identity": {"platform_user_id": "finder-1", "username": "号主"},
        "storage_state": {
            "cookies": [{"name": "wxuin", "value": "x", "domain": ".weixin.qq.com"}],
            "origins": [],
        },
    }
    assert client.post(BASE + "/accounts/import", json=payload).status_code == 200
    holder.current_actor = actor("employee_2")
    assert client.get(BASE + "/accounts").json() == []
    assert client.post(BASE + "/accounts/import", json=payload).status_code == 200
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 2
    pg.execute(
        "INSERT INTO users (id, username, display_name, role) "
        "VALUES ('auditor_x','auditor_x','Auditor','auditor')"
    )
    holder.current_actor = actor("auditor_x", role="auditor")
    assert client.post(BASE + "/accounts/import", json=payload).status_code == 403


_PROBE_DOMAINS = {
    "wechat_channels": ".weixin.qq.com",
    "douyin": ".douyin.com",
    "xiaohongshu": ".xiaohongshu.com",
}


def _seed_connected_browser_account(
    client: TestClient,
    *,
    platform: str = "wechat_channels",
    cookie_value: str = "wx-cookie",
) -> None:
    """Import one connected QR-login account carrying a real platform cookie."""
    payload = {
        "platform": platform,
        "identity": {"platform_user_id": "uid-probe", "username": "巡检号"},
        "storage_state": {
            "cookies": [
                {"name": "session", "value": cookie_value, "domain": _PROBE_DOMAINS[platform]}
            ],
            "origins": [],
        },
    }
    assert client.post(BASE + "/accounts/import", json=payload).status_code == 200


def test_browser_probe_claim_waits_for_schedule_then_reschedules_on_success(
    client: TestClient, pg: psycopg.Connection, lane_env: str
) -> None:
    _seed_connected_browser_account(client)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        # 刚扫码：next_probe_at = now+24h，尚未到期，claim 返回 None。
        assert claim_browser_probe_work(conn, worker_id="w1") is None
        due = datetime.now(UTC) + timedelta(hours=25)
        lease = claim_browser_probe_work(conn, worker_id="w1", now=due)
        assert lease is not None and lease.platform == "wechat_channels"
        # 租约未过期时并发 claim 被 FOR UPDATE SKIP LOCKED 跳过。
        assert claim_browser_probe_work(conn, worker_id="w2", now=due) is None
        finalize_browser_probe(conn, lease=lease, ok=True, now=due)
    row = pg.execute(
        "SELECT status, probe_lease_owner, next_probe_at FROM publish_browser_accounts"
    ).fetchone()
    assert row[0] == "connected"
    assert row[1] is None
    # 成功后 next_probe_at 被推到 finalize 时刻（due）+24h，即距今 >23h。
    assert row[2] > datetime.now(UTC) + timedelta(hours=23)


def test_browser_probe_failure_marks_invalid_and_fences_stale_finalize(
    client: TestClient, pg: psycopg.Connection, lane_env: str
) -> None:
    _seed_connected_browser_account(client)
    due = datetime.now(UTC) + timedelta(hours=25)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        lease = claim_browser_probe_work(conn, worker_id="w1", now=due)
        assert lease is not None
        finalize_browser_probe(conn, lease=lease, ok=False, message="登录态已失效", now=due)
    # 陈旧租约重放在独立事务里被栅栏拒绝（lease_owner 已清空）。
    with pytest.raises(BrowserProbeLeaseLostError), pg_transaction() as raw:
        finalize_browser_probe(BusinessConnection.postgres(raw), lease=lease, ok=True, now=due)
    row = pg.execute("SELECT status, error_message FROM publish_browser_accounts").fetchone()
    assert row[0] == "invalid"
    assert row[1] == "登录态已失效"
    # invalid 账号不再被巡检 claim（需重新扫码才恢复）。
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        far = datetime.now(UTC) + timedelta(days=5)
        assert claim_browser_probe_work(conn, worker_id="w1", now=far) is None


def test_run_publish_round_probes_due_browser_account_and_marks_invalid(
    client: TestClient, pg: psycopg.Connection, lane_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_connected_browser_account(client)
    # 把 next_probe_at 回拨成已到期，触发本轮巡检。
    pg.execute(
        "UPDATE publish_browser_accounts SET next_probe_at = clock_timestamp() - interval '1 hour'"
    )
    from app import publish_worker

    calls: list[tuple[str, str, object]] = []

    def fake_probe(platform: str, cookie: str, sdk: object) -> tuple[bool, str | None]:
        calls.append((platform, cookie, sdk))
        return False, "登录态已失效"

    monkeypatch.setattr(publish_worker, "_dispatch_probe", fake_probe)
    key = Fernet(os.environ[SETTINGS_KEY_ENV].encode())

    @contextmanager
    def open_txn() -> Iterator[BusinessConnection]:
        with pg_transaction() as raw:
            yield BusinessConnection.postgres(raw)

    processed = publish_worker.run_publish_round(open_txn, worker_id="w-probe", fernet=key)
    assert processed >= 1
    # 探测用解密后的 cookie 走对应平台适配器。
    assert calls and calls[0][0] == "wechat_channels" and "wx-cookie" in calls[0][1]
    row = pg.execute("SELECT status, error_message FROM publish_browser_accounts").fetchone()
    assert row[0] == "invalid"
    assert row[1] == "登录态已失效"
