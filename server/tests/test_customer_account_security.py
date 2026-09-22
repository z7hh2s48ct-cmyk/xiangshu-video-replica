"""CW-062 批次 4 / 个人中心审计方案 E —— 账号安全区块。

覆盖自助改密、退出所有设备（撤销活跃会话）、撤销全部 Token 与最近登录记录。

三份契约在这里被锁住：

- **自助改密** ``POST /api/customer/account/password/change``：会话围栏内校验
  当前密码（错则 401 ``INVALID_CREDENTIALS``，与登录同码、同文案），新密码走既有的
  ``password_hashing`` 策略（6–128，scrypt）；改密后旧密码登录必须失败、新密码登录
  必须成功，且**改密即撤销当前活跃会话**（旧会话不得比旧凭据活得更久）——撤销复用
  既有 ``revoke_session``，因此"退出所有设备"与"改密"是同一套语义（本仓库是单会话
  架构：``customer_session_state`` 主键即 ``user_id``）。
- **撤销全部 Token** ``DELETE /api/customer/api-keys``：软吊销调用方名下所有未吊销
  key，返回条数；DELETE 天然幂等（第二次为 0），且吊销后该 key 的独立认证泳道
  （``xsk_live_`` bearer）必须 401。
- **最近登录** ``GET /api/customer/sessions/history``：只读既有 append-only 表
  ``customer_session_events``（join ``customer_devices`` 取设备名/平台），倒序，
  **不返回任何凭据字段**，且严格限于调用方自己的事件。

审计：改密与批量吊销各写一行 ``audit_logs``（既有表，不建 security_event_log），
且元数据里**不得出现任何明文或摘要**。

路由用例复用 ``test_customer_registration`` 的共享迁移库（``route_state``）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
import os
import secrets
from uuid import uuid4

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip
from test_customer_registration import (
    client as registration_client,  # noqa: F401
)
from test_customer_registration import (
    registration_dsn,  # noqa: F401
    route_state,  # noqa: F401
)

from app.customer_security_routes import router as customer_security_router

ACCOUNT_PATH = "/api/customer/account/password/change"
API_KEYS_PATH = "/api/customer/api-keys"
HISTORY_PATH = "/api/customer/sessions/history"
REVOKE_ALL_PATH = "/api/customer/sessions/revoke-all"
API_KEY_READ_PATH = "/api/customer/recharge-orders"

MASTER_PASSWORD = "master-pass-9"
NEXT_PASSWORD = "master-pass-10"
WEAK_PASSWORD = "12345"

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

# 会话失效的稳定答案集：撤销把租约推到过去并递增 epoch，围栏按检出的具体
# 原因返回其中之一——三者都是 401，且都不泄漏凭据。
SESSION_GONE_CODES = {"SESSION_REPLACED", "SESSION_EXPIRED", "SESSION_TOKEN_REQUIRED"}

# 「没带会话」的答案：PG 泳道上围栏先于路由发话（``SESSION_TOKEN_REQUIRED``）；
# 无 PG 运行时才是路由自己的 ``SESSION_REQUIRED``（同 ``test_cw078_api_keys``
# 的 bare_client 用例）。两条都是 401，且都不回答「这个账号存不存在」。
NO_SESSION_CODE = "SESSION_TOKEN_REQUIRED"


@pytest.fixture()
def client(registration_client, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    from app.api_key_routes import router as api_key_router
    from app.recharge_routes import router as recharge_router

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_API_KEY_HMAC_KEY", secrets.token_urlsafe(48))
    registration_client.app.include_router(customer_security_router)
    registration_client.app.include_router(api_key_router)
    registration_client.app.include_router(recharge_router)
    return registration_client


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip(_pg_dsn())


def _fingerprint() -> str:
    return "fp-" + uuid4().hex + uuid4().hex


def _login(client, username: str, password: str):
    return client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "username": username,
            "password": password,
            "device_fingerprint": _fingerprint(),
        },
    )


def _master_session(client, username: str):
    """Register a master and log it in; returns (headers, login body)."""
    registered = client.post(
        "/api/customer/register", json={"username": username, "password": MASTER_PASSWORD}
    )
    assert registered.status_code == 201, registered.text
    login = _login(client, username, MASTER_PASSWORD)
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["session_token"]}, login.json()


def _change_password(client, headers, *, current: str, new: str):
    return client.post(
        ACCOUNT_PATH,
        headers=headers,
        json={"current_password": current, "new_password": new},
    )


def _create_key(client, headers, label: str):
    created = client.post(
        API_KEYS_PATH,
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"label": label},
    )
    assert created.status_code == 201, created.text
    plaintext = created.json()["plaintext"]
    assert plaintext, "a freshly minted key must return its one-time plaintext"
    return plaintext


def _detail_code(response) -> str:
    return response.json()["detail"]["code"]


def _username() -> str:
    return "sec" + uuid4().hex[:12]


# ---------------------------------------------------------------------------
# 自助改密
# ---------------------------------------------------------------------------


def test_password_change_requires_session_token(client) -> None:
    """无会话令牌 → 401，且不泄漏账号是否存在。"""
    response = client.post(
        ACCOUNT_PATH, json={"current_password": MASTER_PASSWORD, "new_password": NEXT_PASSWORD}
    )
    assert response.status_code == 401
    assert _detail_code(response) == NO_SESSION_CODE


def test_password_change_rejects_wrong_current_password(client) -> None:
    """当前密码错 → 401 INVALID_CREDENTIALS（与登录同码），且新密码不生效。"""
    username = _username()
    headers, _ = _master_session(client, username)

    response = _change_password(client, headers, current="not-the-password", new=NEXT_PASSWORD)
    assert response.status_code == 401
    assert _detail_code(response) == "INVALID_CREDENTIALS"

    still = _login(client, username, MASTER_PASSWORD)
    assert still.status_code == 200, "a rejected change must leave the credential intact"


def test_password_change_rejects_weak_new_password(client) -> None:
    """弱新密码 → 400 WEAK_PASSWORD，且当前密码不被消费。"""
    username = _username()
    headers, _ = _master_session(client, username)

    response = _change_password(client, headers, current=MASTER_PASSWORD, new=WEAK_PASSWORD)
    assert response.status_code == 400
    assert _detail_code(response) == "WEAK_PASSWORD"

    still = _login(client, username, MASTER_PASSWORD)
    assert still.status_code == 200


def test_password_change_switches_the_login_credential(client) -> None:
    """改密后旧密码登录失败、新密码登录成功。"""
    username = _username()
    headers, _ = _master_session(client, username)

    response = _change_password(client, headers, current=MASTER_PASSWORD, new=NEXT_PASSWORD)
    assert response.status_code == 200, response.text
    assert response.json()["changed"] is True

    stale = _login(client, username, MASTER_PASSWORD)
    assert stale.status_code == 401
    assert _detail_code(stale) == "INVALID_CREDENTIALS"

    fresh = _login(client, username, NEXT_PASSWORD)
    assert fresh.status_code == 200, fresh.text


def test_password_change_revokes_the_live_session(client) -> None:
    """改密即撤销活跃会话：旧会话令牌不得继续访问（退出所有设备的同一语义）。"""
    username = _username()
    headers, _ = _master_session(client, username)
    assert client.get(API_KEYS_PATH, headers=headers).status_code == 200

    response = _change_password(client, headers, current=MASTER_PASSWORD, new=NEXT_PASSWORD)
    assert response.status_code == 200, response.text
    assert response.json()["sessions_revoked"] >= 1

    revoked = client.get(API_KEYS_PATH, headers=headers)
    assert revoked.status_code == 401
    assert _detail_code(revoked) in SESSION_GONE_CODES


def test_password_change_audit_carries_no_credential_material(client, registration_dsn) -> None:
    """改密写一行 audit_logs，且元数据里没有明文/摘要。"""
    username = _username()
    headers, _ = _master_session(client, username)
    assert (
        _change_password(client, headers, current=MASTER_PASSWORD, new=NEXT_PASSWORD).status_code
        == 200
    )

    with psycopg.connect(registration_dsn) as conn:
        row = conn.execute(
            "SELECT action, metadata_json FROM audit_logs "
            "WHERE action = 'customer.password.changed' "
            "ORDER BY occurred_at DESC, id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "the password change must leave an audit row"
    metadata_json = row[1]
    assert MASTER_PASSWORD not in metadata_json
    assert NEXT_PASSWORD not in metadata_json
    assert "scrypt" not in metadata_json


# ---------------------------------------------------------------------------
# 撤销全部 Token
# ---------------------------------------------------------------------------


def test_bulk_revoke_requires_session_token(client) -> None:
    """无会话令牌 → 401。"""
    response = client.delete(API_KEYS_PATH)
    assert response.status_code == 401
    assert _detail_code(response) == NO_SESSION_CODE


def test_bulk_revoke_revokes_every_live_key_and_is_idempotent(client) -> None:
    """一次撤销名下所有未吊销 key，重复调用不报错且返回 0。"""
    username = _username()
    headers, _ = _master_session(client, username)
    _create_key(client, headers, "prod-service")
    _create_key(client, headers, "dev-service")

    first = client.delete(API_KEYS_PATH, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["revoked"] == 2

    second = client.delete(API_KEYS_PATH, headers=headers)
    assert second.status_code == 200
    assert second.json()["revoked"] == 0

    listed = client.get(API_KEYS_PATH, headers=headers)
    assert listed.status_code == 200
    assert all(item["revoked_at"] for item in listed.json()["items"])


def test_bulk_revoke_invalidates_api_key_authentication(client) -> None:
    """吊销后该 key 的独立泳道立即 401（行保留作审计，不硬删）。"""
    username = _username()
    headers, _ = _master_session(client, username)
    plaintext = _create_key(client, headers, "leak-response")
    bearer = {"Authorization": "Bearer " + plaintext}
    assert client.get(API_KEY_READ_PATH, headers=bearer).status_code == 200

    assert client.delete(API_KEYS_PATH, headers=headers).status_code == 200

    revoked = client.get(API_KEY_READ_PATH, headers=bearer)
    assert revoked.status_code == 401


# ---------------------------------------------------------------------------
# 最近登录
# ---------------------------------------------------------------------------


def test_session_history_requires_session_token(client) -> None:
    """无会话令牌 → 401。"""
    response = client.get(HISTORY_PATH)
    assert response.status_code == 401
    assert _detail_code(response) == NO_SESSION_CODE


def test_session_history_lists_login_events_newest_first(client) -> None:
    """登录后能读到自己的 LOGIN 事件，倒序，且只有非凭据字段。"""
    username = _username()
    headers, _ = _master_session(client, username)

    response = client.get(HISTORY_PATH, headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items, "a completed login must appear in the history"
    assert items[0]["event"] == "LOGIN"
    assert response.json()["total"] >= len(items)

    allowed = {"occurred_at", "event", "device_name", "platform", "reason"}
    for item in items:
        assert set(item) == allowed, f"only non-credential fields may be exposed: {sorted(item)}"
        assert item["occurred_at"]


def test_session_history_is_scoped_to_the_calling_account(client) -> None:
    """他人登录不会出现在我的记录里（跨账号不可枚举）。"""
    mine = _username()
    my_headers, _ = _master_session(client, mine)
    before = len(client.get(HISTORY_PATH, headers=my_headers).json()["items"])

    other = _username()
    _master_session(client, other)
    login = _login(client, other, MASTER_PASSWORD)
    assert login.status_code == 200, login.text

    after = client.get(HISTORY_PATH, headers=my_headers).json()["items"]
    assert len(after) == before, "another account's events must never leak into mine"


def test_session_history_limit_is_bounded(client) -> None:
    """limit 只接受 1..100 的整数，越界 → 422（不是静默截断）。"""
    username = _username()
    headers, _ = _master_session(client, username)

    assert client.get(HISTORY_PATH, headers=headers, params={"limit": 100}).status_code == 200
    assert client.get(HISTORY_PATH, headers=headers, params={"limit": 0}).status_code == 422
    assert client.get(HISTORY_PATH, headers=headers, params={"limit": 101}).status_code == 422


def test_session_history_timestamps_are_iso_8601(client) -> None:
    """时间戳必须是 ISO 8601：``str(timestamptz)`` 给的是空格分隔的非标形式。"""
    username = _username()
    headers, _ = _master_session(client, username)

    items = client.get(HISTORY_PATH, headers=headers).json()["items"]
    assert items, "登录事件应当已经在记录里"
    occurred = items[0]["occurred_at"]
    assert "T" in occurred, f"应为 ISO 8601（含 T），实际 {occurred!r}"
    assert occurred.endswith("Z") or "+" in occurred, f"应带时区，实际 {occurred!r}"


def test_wrong_current_password_is_recorded_as_an_auth_failure(client, registration_dsn) -> None:
    """当前密码错误要落一条失败记录（供暴力破解告警聚合）。

    这条锁住的是一个容易被「顺手挪进事务」破坏的细节：失败记录必须在业务事务
    **之外**写自己的连接，否则错误密码风暴会先耗尽连接池，记录反而全丢。
    """
    username = _username()
    headers, _ = _master_session(client, username)

    with psycopg.connect(registration_dsn) as conn:
        before = conn.execute("SELECT count(*) FROM security_auth_failures").fetchone()
    assert before is not None

    response = _change_password(client, headers, current="definitely-wrong-9", new=NEXT_PASSWORD)
    assert response.status_code == 401
    assert _detail_code(response) == "INVALID_CREDENTIALS"

    with psycopg.connect(registration_dsn) as conn:
        after = conn.execute("SELECT count(*) FROM security_auth_failures").fetchone()
    assert after is not None
    assert after[0] == before[0] + 1, "一次密码校验失败必须正好留一条失败记录"


def test_session_history_metadata_json_is_readable(client, registration_dsn) -> None:
    """留证：事件表里确有本账号的 LOGIN 行（接口之外的第二重核对）。"""
    username = _username()
    headers, login_body = _master_session(client, username)

    with psycopg.connect(registration_dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM customer_session_events WHERE event = 'LOGIN' AND user_id = %s",
            (login_body["user_id"],),
        ).fetchone()
    assert row is not None and row[0] >= 1
    assert client.get(HISTORY_PATH, headers=headers).status_code == 200


def test_session_history_hides_heartbeats(client, registration_dsn) -> None:
    """心跳不是「登录记录」：它每分钟一条，混进来会把真正的事件冲走。"""
    username = _username()
    headers, login_body = _master_session(client, username)
    beat = client.post(
        "/api/customer/sessions/heartbeat",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert beat.status_code == 200, beat.text

    with psycopg.connect(registration_dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM customer_session_events "
            "WHERE event = 'HEARTBEAT' AND user_id = %s",
            (login_body["user_id"],),
        ).fetchone()
    assert row is not None and row[0] >= 1, "心跳事件确实落库了，才谈得上下面的过滤"

    items = client.get(HISTORY_PATH, headers=headers).json()["items"]
    assert items, "登录本身仍然要留在记录里"
    assert all(item["event"] != "HEARTBEAT" for item in items)


# ---------------------------------------------------------------------------
# 退出所有设备（独立于改密的第二条自救路径）
# ---------------------------------------------------------------------------


def test_revoke_all_sessions_requires_session_token(client) -> None:
    """无会话令牌 → 401。"""
    response = client.post(REVOKE_ALL_PATH)
    assert response.status_code == 401
    assert _detail_code(response) == NO_SESSION_CODE


def test_revoke_all_sessions_kills_the_live_session(client) -> None:
    """一键下线：调用后连调用方自己的令牌也立即失效（单会话架构的应有之义）。"""
    username = _username()
    headers, _ = _master_session(client, username)
    assert client.get(API_KEYS_PATH, headers=headers).status_code == 200

    response = client.post(REVOKE_ALL_PATH, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["revoked_sessions"] == 1

    after = client.get(API_KEYS_PATH, headers=headers)
    assert after.status_code == 401
    assert _detail_code(after) in SESSION_GONE_CODES

    # 密码没被动过：改的是会话，不是凭据。
    assert _login(client, username, MASTER_PASSWORD).status_code == 200


def test_revoke_all_sessions_can_be_repeated_after_a_fresh_login(client) -> None:
    """计数反映的是「当前在线会话」，不是历史累计：重新登录再下线仍是 1。

    单会话架构下这段也顺带证明了「下线不动凭据」——同一个密码还能再登回来。
    """
    username = _username()
    headers, _ = _master_session(client, username)

    first = client.post(REVOKE_ALL_PATH, headers=headers)
    assert first.status_code == 200
    assert first.json()["revoked_sessions"] == 1

    reopened = _login(client, username, MASTER_PASSWORD)
    assert reopened.status_code == 200, reopened.text
    fresh = {"Authorization": "Bearer " + reopened.json()["session_token"]}

    second = client.post(REVOKE_ALL_PATH, headers=fresh)
    assert second.status_code == 200, second.text
    assert second.json()["revoked_sessions"] == 1


def test_revoke_all_sessions_writes_an_audit_row(client, registration_dsn) -> None:
    """下线动作留审计，且元数据里没有凭据材料。"""
    username = _username()
    headers, _ = _master_session(client, username)
    assert client.post(REVOKE_ALL_PATH, headers=headers).status_code == 200

    with psycopg.connect(registration_dsn) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE action = 'customer.sessions.revoked_all' "
            "ORDER BY occurred_at DESC, id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "revoking every session must leave an audit row"
    assert "master-pass" not in row[0]
