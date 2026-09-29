"""方案 P2-4 团队与权限：超管成员管理矩阵（admin_team_test，迁到 head，逐测 truncate）。

覆盖四组契约：

- **权限**：列表 / 新增 / 更新 / 重置密码全部超管专属——普通 admin 与
  auditor 一律 403（auditor 写侧先撞 AUDITOR_READ_ONLY，读侧撞
  SUPER_ADMIN_REQUIRED）；
- **账号三件套**：新增成员 = users + wallets + admin_password_credentials
  一次建齐，且新成员能真的用该密码登录（真实 scrypt 校验，不 mock）；
- **会话时效**：停用成员与其密码重置都立即吊销目标全部管理会话——已登录
  的人（以及被重置前的旧会话）下一次请求即 401；
- **自我保护**：不能停用自己 / 不能改自己的超管标记；「至少保留一个启用中
  的超管」是纵深防御兜底（经 API 正常路径不可达，由伪造会话的竞态用例
  直接验证）。

会话走 ``pg_test_kit.password_admin_session`` 的真实服务链路（不 override
授权依赖），因此这里的超管判定、会话吊销、密码验证都是真实行为。
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_HMAC_KEY_ENV,
    AdminActor,
    hash_admin_password,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

TEAM_DB_NAME = "admin_team_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)  # admin-session HMAC key

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"

MEMBERS_PATH = "/api/control/team/members"

STAFF_OLD_PASSWORD = "Staff Old Password 2026!"
STAFF_NEW_PASSWORD = "Staff New Passphrase 2026!"
NEW_MEMBER_PASSWORD = "Fresh Member Password 2026!"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _db_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{TEAM_DB_NAME}"


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def team_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEAM_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEAM_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _db_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _db_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{TEAM_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(team_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE admin_write_idempotency, admin_sessions, audit_logs, "
            "admin_password_credentials, wallet_transactions, recharge_orders, "
            "wallets, users, security_rate_limit_counters, security_auth_failures "
            "CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, is_active, is_super_admin) "
            "VALUES "
            "('super_u', 'super_u', 'Super Admin', 'admin', 1, 1), "
            "('admin_u', 'admin_u', 'Plain Admin', 'admin', 1, 0), "
            "('auditor_u', 'auditor_u', 'Auditor', 'auditor', 1, 0), "
            "('staff_u', 'staff_u', 'Staff Admin', 'admin', 1, 0)"
        )
        # staff_u 是重置密码的既有目标：给他一条真实凭据，让「旧密码可登录 →
        # 重置 → 旧密码失效 / 新密码可用」成为全链路断言而不是 mock 比对。
        conn.execute(
            "INSERT INTO admin_password_credentials "
            "(user_id, password_hash, credential_version, password_changed_at) "
            "VALUES ('staff_u', %s, 1, clock_timestamp())",
            (hash_admin_password(STAFF_OLD_PASSWORD),),
        )
    yield team_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_team_routes import router as team_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(team_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    yield app


@pytest.fixture()
def client(admin_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(admin_app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_session(client: TestClient, actor: str = "super_u") -> dict[str, str]:
    """Establish a real password-lane admin session; return CSRF write headers."""
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _save_cookie(client: TestClient) -> str:
    return client.cookies.get(ADMIN_SESSION_COOKIE) or ""


def _restore_cookie(client: TestClient, cookie: str) -> None:
    client.cookies.set(ADMIN_SESSION_COOKIE, cookie)


def _create_member(
    client: TestClient,
    session: dict[str, str],
    *,
    username: str = "new_ops",
    display_name: str = "New Ops",
    role: str = "admin",
    password: str = NEW_MEMBER_PASSWORD,
    reason: str = "新同事入职：开通管理账号",
    confirm: bool = True,
    key: str | None = None,
) -> object:
    headers = {**session, IDEMPOTENCY_KEY_HEADER: key or f"key-{uuid.uuid4()}"}
    body: dict[str, object] = {
        "confirm": confirm,
        "reason": reason,
        "username": username,
        "display_name": display_name,
        "role": role,
        "password": password,
    }
    return client.post(MEMBERS_PATH, headers=headers, json=body)


def _patch_member(
    client: TestClient,
    session: dict[str, str],
    user_id: str,
    *,
    display_name: str | None = None,
    is_active: bool | None = None,
    is_super_admin: bool | None = None,
    reason: str = "团队调整",
    confirm: bool = True,
    key: str | None = None,
) -> object:
    headers = {**session, IDEMPOTENCY_KEY_HEADER: key or f"key-{uuid.uuid4()}"}
    body: dict[str, object] = {"confirm": confirm, "reason": reason}
    if display_name is not None:
        body["display_name"] = display_name
    if is_active is not None:
        body["is_active"] = is_active
    if is_super_admin is not None:
        body["is_super_admin"] = is_super_admin
    return client.patch(f"{MEMBERS_PATH}/{user_id}", headers=headers, json=body)


def _reset_password(
    client: TestClient,
    session: dict[str, str],
    user_id: str,
    *,
    password: str = STAFF_NEW_PASSWORD,
    reason: str = "成员忘记密码：超管重置",
    confirm: bool = True,
    key: str | None = None,
) -> object:
    headers = {**session, IDEMPOTENCY_KEY_HEADER: key or f"key-{uuid.uuid4()}"}
    return client.post(
        f"{MEMBERS_PATH}/{user_id}/password",
        headers=headers,
        json={"confirm": confirm, "reason": reason, "password": password},
    )


def _password_login(client: TestClient, username: str, password: str) -> object:
    return client.post(
        "/api/control/admin/session/password",
        json={"username": username, "password": password},
    )


def _members_by_id(client: TestClient, session: dict[str, str]) -> dict[str, dict[str, object]]:
    response = client.get(MEMBERS_PATH, headers=session)
    assert response.status_code == 200, response.text
    return {str(item["user_id"]): item for item in response.json()["items"]}


# ---------------------------------------------------------------------------
# 权限：全部端点超管专属
# ---------------------------------------------------------------------------


def test_list_returns_team_with_flags_for_super_admin(client: TestClient, route_state: str) -> None:
    session = _admin_session(client, "super_u")
    members = _members_by_id(client, session)
    assert set(members) == {"super_u", "admin_u", "auditor_u", "staff_u"}
    assert members["super_u"]["is_super_admin"] is True
    assert members["super_u"]["role"] == "admin"
    assert members["auditor_u"]["role"] == "auditor"
    assert members["staff_u"]["has_password"] is True
    assert members["admin_u"]["has_password"] is False
    # 新会话刚建立：super_u 的最近登录时间已落到列表上。
    with psycopg.connect(route_state) as conn:
        expected = conn.execute(
            "SELECT MAX(created_at) FROM admin_sessions WHERE actor_user_id = 'super_u'"
        ).fetchone()
    assert expected is not None
    assert members["super_u"]["last_login_at"] == str(expected[0])


def test_list_requires_super_admin(client: TestClient) -> None:
    plain = _admin_session(client, "admin_u")
    denied = client.get(MEMBERS_PATH, headers=plain)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "SUPER_ADMIN_REQUIRED"

    auditor = _admin_session(client, "auditor_u")
    denied = client.get(MEMBERS_PATH, headers=auditor)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "SUPER_ADMIN_REQUIRED"


def test_writes_require_super_admin(client: TestClient) -> None:
    plain = _admin_session(client, "admin_u")
    denied = _create_member(client, plain)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "SUPER_ADMIN_REQUIRED"

    # auditor 写侧先撞既有 RBAC 语义：两层拒绝各回答自己的代码。
    auditor = _admin_session(client, "auditor_u")
    denied = _create_member(client, auditor)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUDITOR_READ_ONLY"


# ---------------------------------------------------------------------------
# 新增成员：三件套 / 写契约 / 幂等 / 冲突
# ---------------------------------------------------------------------------


def test_create_member_builds_whole_account_and_can_log_in(
    client: TestClient, route_state: str
) -> None:
    session = _admin_session(client, "super_u")
    created = _create_member(client, session)
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["username"] == "new_ops"
    assert payload["role"] == "admin"
    assert payload["is_active"] is True
    assert payload["is_super_admin"] is False
    assert payload["has_password"] is True
    new_user_id = str(payload["user_id"])

    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT role, is_active, is_super_admin FROM users WHERE id = %s",
            (new_user_id,),
        ).fetchone() == ("admin", 1, 0)
        assert conn.execute(
            "SELECT count(*) FROM wallets WHERE user_id = %s", (new_user_id,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT credential_version FROM admin_password_credentials WHERE user_id = %s",
            (new_user_id,),
        ).fetchone() == (1,)
        audit = conn.execute(
            "SELECT action, entity_type, metadata_json FROM audit_logs "
            "WHERE entity_id = %s AND action = 'team.member.create'",
            (new_user_id,),
        ).fetchone()
    assert audit is not None and audit[0] == "team.member.create"
    assert str(audit[1]) == "team_member"
    assert "新同事入职" in str(audit[2])

    # 全链路：新账号真的能用这个密码建立管理会话（真实 scrypt 校验）。
    login = _password_login(client, "new_ops", NEW_MEMBER_PASSWORD)
    assert login.status_code == 201, login.text


def test_create_member_enforces_write_contract(client: TestClient) -> None:
    session = _admin_session(client, "super_u")

    missing_key = client.post(
        MEMBERS_PATH,
        headers=session,
        json={
            "confirm": True,
            "reason": "新同事入职：开通管理账号",
            "username": "new_ops",
            "display_name": "New Ops",
            "role": "admin",
            "password": NEW_MEMBER_PASSWORD,
        },
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    no_confirm = _create_member(client, session, confirm=False)
    assert no_confirm.status_code == 400
    assert no_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    blank_reason = _create_member(client, session, reason="  ")
    assert blank_reason.status_code == 400
    assert blank_reason.json()["detail"]["code"] == "REASON_REQUIRED"


def test_create_member_idempotent_replay_books_once(client: TestClient, route_state: str) -> None:
    session = _admin_session(client, "super_u")
    key = f"team-replay-{uuid.uuid4()}"
    created = _create_member(client, session, key=key)
    assert created.status_code == 201, created.text

    replay = _create_member(client, session, key=key)
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json()["user_id"] == created.json()["user_id"]

    with psycopg.connect(route_state) as conn:
        assert conn.execute("SELECT count(*) FROM users WHERE username = 'new_ops'").fetchone() == (
            1,
        )
        assert conn.execute(
            "SELECT count(*) FROM wallets w JOIN users u ON u.id = w.user_id "
            "WHERE u.username = 'new_ops'"
        ).fetchone() == (1,)


def test_create_member_duplicate_username_conflict(client: TestClient) -> None:
    session = _admin_session(client, "super_u")
    taken = _create_member(client, session, username="staff_u")
    assert taken.status_code == 409
    assert taken.json()["detail"]["code"] == "TEAM_MEMBER_USERNAME_TAKEN"


def test_create_member_rejects_short_password(client: TestClient) -> None:
    session = _admin_session(client, "super_u")
    rejected = _create_member(client, session, password="too-short")
    assert rejected.status_code == 422  # pydantic 长度下限（12）


@pytest.mark.parametrize(
    ("username", "display_name"),
    [(" ", "New Ops"), ("new_ops", " "), (chr(9), chr(10))],
)
def test_create_member_rejects_names_that_are_blank_after_trimming(
    client: TestClient, route_state: str, username: str, display_name: str
) -> None:
    """``min_length`` 在去空白之前判定，纯空白会被裁成空串写入并永久占住唯一键。"""
    session = _admin_session(client, "super_u")
    rejected = _create_member(client, session, username=username, display_name=display_name)
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"]["code"] == "TEAM_MEMBER_VALIDATION_FAILED"
    with psycopg.connect(route_state) as conn:
        row = conn.execute("SELECT count(*) FROM users").fetchone()
    assert row is not None and row[0] == 4  # 夹具里的 4 个账号，没有新增


# ---------------------------------------------------------------------------
# 更新成员：显示名 / 停用吊销会话 / 自我保护 / 最后超管兜底
# ---------------------------------------------------------------------------


def test_update_display_name_records_audit(client: TestClient, route_state: str) -> None:
    session = _admin_session(client, "super_u")
    updated = _patch_member(client, session, "staff_u", display_name="Staff Renamed")
    assert updated.status_code == 200, updated.text
    assert updated.json()["display_name"] == "Staff Renamed"
    with psycopg.connect(route_state) as conn:
        assert conn.execute("SELECT display_name FROM users WHERE id = 'staff_u'").fetchone() == (
            "Staff Renamed",
        )
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE entity_id = 'staff_u' AND action = 'team.member.update'"
        ).fetchone()
    assert audit is not None
    meta = str(audit[0])
    assert '"old_display_name":"Staff Admin"' in meta
    assert '"new_display_name":"Staff Renamed"' in meta


def test_deactivate_member_revokes_their_sessions(client: TestClient, route_state: str) -> None:
    staff_cookie = ""
    staff_session = _admin_session(client, "staff_u")
    assert client.get("/api/control/admin/session", headers=staff_session).status_code == 200
    staff_cookie = _save_cookie(client)

    session = _admin_session(client, "super_u")
    deactivated = _patch_member(client, session, "staff_u", is_active=False)
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["is_active"] is False

    with psycopg.connect(route_state) as conn:
        assert conn.execute("SELECT is_active FROM users WHERE id = 'staff_u'").fetchone() == (0,)
        revoked = conn.execute(
            "SELECT count(*) FROM admin_sessions "
            "WHERE actor_user_id = 'staff_u' AND revoked_at IS NOT NULL"
        ).fetchone()
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE entity_id = 'staff_u' AND action = 'team.member.update'"
        ).fetchone()
    assert revoked is not None and int(revoked[0]) >= 1
    assert audit is not None and '"revoked_sessions":1' in str(audit[0])

    # 被停用者的旧会话立刻失效：下一次请求 401。
    _restore_cookie(client, staff_cookie)
    refused = client.get("/api/control/admin/session")
    assert refused.status_code == 401
    # 停用者也不能再登录（is_active=0 在密码校验前就被拒）。
    assert _password_login(client, "staff_u", STAFF_OLD_PASSWORD).status_code == 401


def test_cannot_deactivate_self(client: TestClient) -> None:
    session = _admin_session(client, "super_u")
    denied = _patch_member(client, session, "super_u", is_active=False)
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "CANNOT_DEACTIVATE_SELF"


def test_cannot_change_own_super_flag(client: TestClient) -> None:
    session = _admin_session(client, "super_u")
    # 同值设置是 no-op：不改变状态、也不触发保护（与 LAST_SUPER_ADMIN 的
    # 「真转换才判定」同一口径）。
    noop = _patch_member(client, session, "super_u", is_super_admin=True)
    assert noop.status_code == 200, noop.text
    assert noop.json()["is_super_admin"] is True
    # 真转换（撤销自己的超管）被拒。
    demoted = _patch_member(client, session, "super_u", is_super_admin=False)
    assert demoted.status_code == 400
    assert demoted.json()["detail"]["code"] == "CANNOT_CHANGE_OWN_SUPER_FLAG"


def test_last_super_admin_guard_rejects_orphaning_the_tenant(
    client: TestClient, admin_app: FastAPI
) -> None:
    """「至少保留一个启用中的超管」兜底。

    诚实会话路径不可达：操作者自己必然是活跃超管，「排除目标后无超管」
    不成成立。这里用 override 注入一个「会话声称超管、库行其实不是」的
    伪造身份（模拟身份与库行不一致的竞态），降级/停用唯一超管必须被拒。
    """
    from app import admin_auth_routes

    forged = AdminActor(
        user_id="admin_u",  # 库中 is_super_admin=0 的普通 admin
        username="admin_u",
        display_name="Plain Admin",
        role="admin",
        is_super_admin=True,  # 会话身份与库行不一致（竞态/脏数据）
        auth_method="password",
        session_id="forged-sess",
        session_expires_at="2099-01-01T00:00:00+00:00",
        last_activity_at="2026-01-01T00:00:00+00:00",
    )
    # override 整体替换 get_admin_actor（含其 CSRF 校验）：伪造会话下
    # 写请求直达业务层，正是这个兜底分支要防的场景。
    admin_app.dependency_overrides[admin_auth_routes.get_admin_actor] = lambda: forged
    try:
        demote = _patch_member(client, {}, "super_u", is_super_admin=False)
        deactivate = _patch_member(client, {}, "super_u", is_active=False)
    finally:
        admin_app.dependency_overrides.pop(admin_auth_routes.get_admin_actor, None)
    assert demote.status_code == 400
    assert demote.json()["detail"]["code"] == "LAST_SUPER_ADMIN_REQUIRED"
    assert deactivate.status_code == 400
    assert deactivate.json()["detail"]["code"] == "LAST_SUPER_ADMIN_REQUIRED"


def test_super_admin_can_demote_another_super_admin_when_one_remains(
    client: TestClient, route_state: str
) -> None:
    """存在第二个活跃超管时，降级另一超管是允许的（不是一刀切禁降级）。"""
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute("UPDATE users SET is_super_admin = 1 WHERE id = 'staff_u'")
    session = _admin_session(client, "super_u")
    demoted = _patch_member(client, session, "staff_u", is_super_admin=False)
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["is_super_admin"] is False


def test_update_rejects_display_name_that_is_blank_after_trimming(
    client: TestClient, route_state: str
) -> None:
    session = _admin_session(client, "super_u")
    rejected = _patch_member(client, session, "staff_u", display_name="   ")
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"]["code"] == "TEAM_MEMBER_VALIDATION_FAILED"
    with psycopg.connect(route_state) as conn:
        row = conn.execute("SELECT display_name FROM users WHERE id = 'staff_u'").fetchone()
    assert row is not None and row[0] == "Staff Admin"


def test_concurrent_mutual_demotion_cannot_orphan_the_tenant(
    client: TestClient, route_state: str
) -> None:
    """两个超管同时互相降级：后到的写请求必须等前一笔提交，再在最新数据上被拒。

    用第二个连接扮演「对方的并发事务」：先拿同一把锁、把 ``super_u`` 降级，暂不提交。
    没有串行化时，这条请求会立刻在「super_u 仍是超管」的旧快照里通过并提交，两笔
    合起来就没有启用超管了；有串行化时它必须先被挡住，放行后重新计数并被拒绝。
    """
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute("UPDATE users SET is_super_admin = 1 WHERE id = 'staff_u'")
    session = _admin_session(client, "super_u")
    other = psycopg.connect(route_state)
    try:
        other.execute("SELECT pg_advisory_xact_lock(hashtext('team:super_admin_guard'))")
        other.execute("UPDATE users SET is_super_admin = 0 WHERE id = 'super_u'")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_patch_member, client, session, "staff_u", is_super_admin=False)
            time.sleep(1.0)
            assert not future.done(), "写请求没有被串行化：它在对方提交前就完成了"
            other.commit()
            result = future.result(timeout=20)
    finally:
        other.close()
    assert result.status_code == 400, result.text
    assert result.json()["detail"]["code"] == "LAST_SUPER_ADMIN_REQUIRED"
    with psycopg.connect(route_state) as conn:
        row = conn.execute("SELECT is_super_admin FROM users WHERE id = 'staff_u'").fetchone()
    assert row is not None and row[0] == 1  # 仍有一个启用超管，团队接口没有被锁死


def test_update_unknown_member_is_404(client: TestClient) -> None:
    session = _admin_session(client, "super_u")
    missing = _patch_member(client, session, "no_such_user", is_active=False)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "TEAM_MEMBER_NOT_FOUND"


# ---------------------------------------------------------------------------
# 重置密码：旧会话吊销 / 新密码可登录 / 拒绝自己
# ---------------------------------------------------------------------------


def test_reset_password_end_to_end(client: TestClient, route_state: str) -> None:
    # 旧密码可用、旧会话在线。
    old_login = _password_login(client, "staff_u", STAFF_OLD_PASSWORD)
    assert old_login.status_code == 201, old_login.text
    staff_cookie = _save_cookie(client)

    session = _admin_session(client, "super_u")
    reset = _reset_password(client, session, "staff_u")
    assert reset.status_code == 200, reset.text
    assert int(reset.json()["revoked_sessions"]) >= 1

    with psycopg.connect(route_state) as conn:
        version = conn.execute(
            "SELECT credential_version FROM admin_password_credentials WHERE user_id = 'staff_u'"
        ).fetchone()
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE entity_id = 'staff_u' AND action = 'team.member.password_reset'"
        ).fetchone()
    assert version is not None and int(version[0]) == 2  # 1 → 2
    assert audit is not None and "超管重置" in str(audit[0])

    _restore_cookie(client, staff_cookie)
    assert client.get("/api/control/admin/session").status_code == 401
    # 旧密码失效、新密码可登录。
    assert _password_login(client, "staff_u", STAFF_OLD_PASSWORD).status_code == 401
    fresh = _password_login(client, "staff_u", STAFF_NEW_PASSWORD)
    assert fresh.status_code == 201, fresh.text


def test_reset_password_rejects_self(client: TestClient) -> None:
    session = _admin_session(client, "super_u")
    denied = _reset_password(client, session, "super_u")
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "CANNOT_RESET_OWN_PASSWORD"


def test_reset_password_requires_super_admin(client: TestClient) -> None:
    plain = _admin_session(client, "admin_u")
    denied = _reset_password(client, plain, "staff_u")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "SUPER_ADMIN_REQUIRED"
