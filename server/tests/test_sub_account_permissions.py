"""CUSTOMER-CENTER-V2 Phase 3b — 子账号功能权限矩阵（audit-40 权限矩阵）。

``sub_account_permissions``（迁移 20260922T1800）给子账号限定「12 类业务权限
+ 2 项系统权限」，**无行 = 全允许**（与额度表同款 fail-open，存量子账号零回填）。
本文件按四层锁住这份契约：

- **表结构**（自建 ``t_sub_account_permission`` 专库）：PK = user_id、FK CASCADE、
  TEXT-JSON 列（JSON 全存 TEXT，表内 ``jsonb`` 列数为 0）、默认值，以及有权限
  行时的 fail-closed downgrade 守卫；
- **权限模块**：normalize（声明序/去重/未知键 ValueError）、is_all_permissions、
  read（母账号/无行折叠为 ``None``）、三个 enforce 的稳定 403 code；
- **API + guard 拆分**：PUT permissions（全开删行/子集 upsert/非法键 422）、
  create 原子带权限、PATCH account_type、SUB_ADMIN 只能摸普通 SUB 行、
  普通 SUB 仍 403 ``MASTER_ACCOUNT_REQUIRED``；
- **enforcement 三挂载点**：``accept_operation``（功能准入独立于 credits——
  免费科目同样被拦）、Token 创建（含幂等重放）、发布账号导入/扫码。

路由用例复用 ``test_customer_registration`` 的共享迁移库（``route_state``）；
专库名不入 ``pg_test_kit.RECORDED_TEST_DATABASES``（同 ``test_sub_account_quota``）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import require_pg_or_explicit_skip
from test_customer_registration import (
    client as registration_client,  # noqa: F401
)
from test_customer_registration import (
    registration_dsn,  # noqa: F401
    route_state,  # noqa: F401
)

from app.customer_sub_account_routes import router as customer_sub_account_router
from app.db_portable import BusinessConnection
from app.sub_account_permissions import (
    BUSINESS_FEATURES,
    enforce_sub_account_api_keys,
    enforce_sub_account_feature,
    enforce_sub_account_publish_accounts,
    is_all_permissions,
    normalize_businesses,
    read_sub_account_permissions,
)
from app.usage_billing import accept_operation

SUB_ACCOUNTS_PATH = "/api/customer/sub-accounts"
API_KEYS_PATH = "/api/customer/api-keys"
PUBLISH_BROWSER_PATH = "/api/studio/publish/browser"

MASTER_PASSWORD = "master-pass-9"
SUB_PASSWORD = "sub-pass-9"

ALL_BUSINESSES = [key for key, _ in BUSINESS_FEATURES]

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
PERMISSIONS_TABLE = "sub_account_permissions"
PERMISSIONS_DB = "t_sub_account_permission"

# 链尾：本分支的 20260923T1200_admin_refund_adjustment 按手册 §3 重挂于
# main 链尾（20260922T2200_material_preference_tags）之上；其后依次叠加
# 20260923T1800 部署垫片、20260924T0000 交易号唯一索引与
# 20260924T0100 口播提交时刻列、20260924T0200 口播隐藏偏好表、
# 20260926T0000 爆款首页策展排行与 20260925T1400 计费触发器追加修复，
# 故链尾为该值。
_HEAD_REVISION = "20260929T1200_admin_team_and_alert_settings"
_PRIOR_REVISION = "20260921T1200_sub_account_quotas"


@pytest.fixture()
def client(registration_client, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    from app.api_key_routes import router as api_key_router
    from app.publish_browser_routes import router as publish_browser_router

    # 钱包/凭据读取要解 Fernet 根钥；Token 铸钥要 HMAC 域（403 路径只需它能
    # 通过 ``_mutate_key`` 的前置配置检查，成功路径真正用到的就是它）。
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_API_KEY_HMAC_KEY", secrets.token_urlsafe(48))
    registration_client.app.include_router(customer_sub_account_router)
    registration_client.app.include_router(api_key_router)
    registration_client.app.include_router(publish_browser_router)
    return registration_client


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip(_pg_dsn())


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _drop_database(db_name: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def _create_database(db_name: str) -> str:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db_name}"')
    return _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"


def _alembic_config(dsn: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    return config


def _upgrade(dsn: str, target: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(dsn), target)


def _downgrade(dsn: str, target: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(dsn), target)


@pytest.fixture()
def permission_dsn() -> str:
    """Fresh database at head: one MASTER ('m1') and three SUBs ('s1'..'s3')."""
    dsn = _create_database(PERMISSIONS_DB)
    _upgrade(dsn, "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES ('m1', 'm1', 'Master 1')"
        )
        for sub_id in ("s1", "s2", "s3"):
            conn.execute(
                "INSERT INTO users (id, username, display_name, parent_user_id, account_type) "
                "VALUES (%s, %s, %s, 'm1', 'SUB')",
                (sub_id, sub_id, sub_id),
            )
    try:
        yield dsn
    finally:
        _drop_database(PERMISSIONS_DB)


def _column(conn: psycopg.Connection, table: str, name: str):  # type: ignore[no-untyped-def]
    """(data_type, is_nullable, column_default) for one column, or None if absent."""
    return conn.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, name),
    ).fetchone()


def _insert_permissions(
    conn: psycopg.Connection,
    user_id: str,
    *,
    businesses: list[str],
    allow_api_keys: bool = True,
    allow_publish_accounts: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO sub_account_permissions "
        "(user_id, allowed_businesses, allow_api_keys, allow_publish_accounts) "
        "VALUES (%s, %s, %s, %s)",
        (user_id, json.dumps(businesses), allow_api_keys, allow_publish_accounts),
    )


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


def _sub_session(client, username: str, password: str = SUB_PASSWORD):
    """Log a sub-account in; returns (headers, login body)."""
    login = _login(client, username, password)
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["session_token"]}, login.json()


def _create_sub(
    client,
    headers,
    *,
    username: str,
    permissions: dict[str, object] | None = None,
    monthly_quota_credits: int | None = None,
    password: str | None = SUB_PASSWORD,
):
    body: dict[str, object] = {"username": username, "display_name": "权限子账号"}
    if password is not None:
        body["password"] = password
    if monthly_quota_credits is not None:
        body["monthly_quota_credits"] = monthly_quota_credits
    if permissions is not None:
        body["permissions"] = permissions
    return client.post(SUB_ACCOUNTS_PATH, headers=headers, json=body)


def _detail_code(response) -> str:
    return response.json()["detail"]["code"]


def _set_permissions(
    client,
    headers,
    sub_account_id: str,
    *,
    businesses: list[str] | None = None,
    allow_api_keys: bool = True,
    allow_publish_accounts: bool = True,
):
    return client.put(
        f"{SUB_ACCOUNTS_PATH}/{sub_account_id}/permissions",
        headers=headers,
        json={
            "businesses": ALL_BUSINESSES if businesses is None else businesses,
            "allow_api_keys": allow_api_keys,
            "allow_publish_accounts": allow_publish_accounts,
        },
    )


# ---------------------------------------------------------------------------
# 20260922T1800: table shape, row constraints, cascade, downgrade guard
# ---------------------------------------------------------------------------


def test_permissions_table_columns_constraints_and_defaults(permission_dsn: str) -> None:
    with psycopg.connect(permission_dsn, autocommit=True) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _HEAD_REVISION

        user_id = _column(conn, PERMISSIONS_TABLE, "user_id")
        assert user_id is not None, "sub_account_permissions.user_id missing at head"
        assert user_id[0] == "text" and user_id[1] == "NO"

        # R-A / cw056 §617：JSON 全存 TEXT——本表不得引入 jsonb 列。
        businesses = _column(conn, PERMISSIONS_TABLE, "allowed_businesses")
        assert businesses is not None
        assert businesses[0] == "text" and businesses[1] == "NO"
        assert businesses[2] is not None and "[]" in businesses[2]
        jsonb_columns = conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND data_type = 'jsonb'",
            (PERMISSIONS_TABLE,),
        ).fetchone()[0]
        assert jsonb_columns == 0, "JSON 全存 TEXT（cw056 §617）"

        for name in ("allow_api_keys", "allow_publish_accounts"):
            column = _column(conn, PERMISSIONS_TABLE, name)
            assert column is not None, f"sub_account_permissions.{name} missing at head"
            assert column[0] == "boolean" and column[1] == "NO"
            assert column[2] is not None and "true" in column[2]

        for name in ("created_at", "updated_at"):
            column = _column(conn, PERMISSIONS_TABLE, name)
            assert column is not None, f"sub_account_permissions.{name} missing at head"
            assert column[0] == "text" and column[1] == "NO"
            assert column[2] is not None and "CURRENT_TIMESTAMP" in column[2]

        primary = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'sub_account_permissions'::regclass AND contype = 'p'"
        ).fetchone()
        assert primary is not None and "user_id" in primary[0]

        foreign = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'sub_account_permissions'::regclass AND contype = 'f'"
        ).fetchone()
        assert foreign is not None, "sub_account_permissions.user_id FK missing at head"
        assert "users(id)" in foreign[0] and "CASCADE" in foreign[0]


def test_permissions_row_rejects_duplicates_and_cascades_with_the_sub(
    permission_dsn: str,
) -> None:
    with psycopg.connect(permission_dsn, autocommit=True) as conn:
        _insert_permissions(conn, "s1", businesses=["video"])
        # PK：一行一账号（PUT 走 ON CONFLICT 更新，不依赖重复插入）。
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_permissions(conn, "s1", businesses=["oral"])
        # FK：权限行只能挂在真实用户上。
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_permissions(conn, "ghost", businesses=["video"])

        # ON DELETE CASCADE：子账号被真删时权限行随行清理。
        conn.execute("DELETE FROM users WHERE id = 's1'")
        assert conn.execute(f"SELECT count(*) FROM {PERMISSIONS_TABLE}").fetchone()[0] == 0

        # 母账号删除级联到子账号，再级联到权限行。
        _insert_permissions(conn, "s3", businesses=["video"])
        conn.execute("DELETE FROM users WHERE id = 'm1'")
        assert conn.execute(f"SELECT count(*) FROM {PERMISSIONS_TABLE}").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_downgrade_guard_refuses_while_permission_rows_exist(permission_dsn: str) -> None:
    """Permission rows present -> RuntimeError and the whole chain rolls back to head."""
    with psycopg.connect(permission_dsn, autocommit=True) as conn:
        _insert_permissions(conn, "s1", businesses=["video"])

    with pytest.raises(RuntimeError, match="sub-account permission rows exist"):
        _downgrade(permission_dsn, _PRIOR_REVISION)

    with psycopg.connect(permission_dsn, autocommit=True) as conn:
        # The downgrade runs in one transaction, so the guard rolls it all back.
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _HEAD_REVISION, "failed downgrade must leave the chain at head"
        assert _column(conn, PERMISSIONS_TABLE, "user_id") is not None

        conn.execute(f"DELETE FROM {PERMISSIONS_TABLE}")

    _downgrade(permission_dsn, _PRIOR_REVISION)

    with psycopg.connect(permission_dsn) as conn:
        versions = {row[0] for row in conn.execute("SELECT version_num FROM alembic_version")}
        assert versions == {_PRIOR_REVISION}
        assert _column(conn, PERMISSIONS_TABLE, "user_id") is None


# ---------------------------------------------------------------------------
# Permission module: normalisation, the full-grant spelling, read folding
# ---------------------------------------------------------------------------


def test_normalize_businesses_sorts_dedupes_and_rejects_unknown() -> None:
    assert normalize_businesses(["oral", "video", "oral"]) == ["video", "oral"]
    assert normalize_businesses([]) == []
    assert normalize_businesses(ALL_BUSINESSES) == ALL_BUSINESSES
    with pytest.raises(ValueError, match="未知业务权限键"):
        normalize_businesses(["video", "teleport"])


def test_is_all_permissions_requires_the_full_grant() -> None:
    assert is_all_permissions(
        businesses=ALL_BUSINESSES, allow_api_keys=True, allow_publish_accounts=True
    )
    assert not is_all_permissions(
        businesses=ALL_BUSINESSES[:-1], allow_api_keys=True, allow_publish_accounts=True
    )
    assert not is_all_permissions(
        businesses=ALL_BUSINESSES, allow_api_keys=False, allow_publish_accounts=True
    )
    assert not is_all_permissions(
        businesses=ALL_BUSINESSES, allow_api_keys=True, allow_publish_accounts=False
    )


def test_read_folds_master_and_missing_rows_to_unrestricted(permission_dsn: str) -> None:
    with psycopg.connect(permission_dsn, autocommit=True) as conn:
        # 母账号直通，即便有人绕过 API 给它插了一行。
        assert read_sub_account_permissions(conn, "m1") is None
        # 子账号无行 = 全允许。
        assert read_sub_account_permissions(conn, "s1") is None
        # 未知账号折叠为全允许（无父行可查）。
        assert read_sub_account_permissions(conn, "ghost") is None

        # 有行：TEXT-JSON 解析回业务集合与开关。
        _insert_permissions(
            conn,
            "s2",
            businesses=["video", "oral"],
            allow_api_keys=False,
            allow_publish_accounts=True,
        )
        permissions = read_sub_account_permissions(conn, "s2")
        assert permissions is not None
        assert permissions.businesses == frozenset({"video", "oral"})
        assert permissions.allow_api_keys is False
        assert permissions.allow_publish_accounts is True


def test_feature_gate_codes_and_passthroughs(permission_dsn: str) -> None:
    with psycopg.connect(permission_dsn, autocommit=True) as conn:
        # 无行 = 直通；未映射的内部科目（cos 等）不看权限行。
        enforce_sub_account_feature(conn, actor_id="s1", service="asr")
        enforce_sub_account_feature(conn, actor_id="s2", service="cos")

        _insert_permissions(conn, "s2", businesses=["oral"])
        # 允许的业务放行；禁用业务 403，code 稳定。
        enforce_sub_account_feature(conn, actor_id="s2", service="oral")
        with pytest.raises(HTTPException) as blocked:
            enforce_sub_account_feature(conn, actor_id="s2", service="asr")
        assert blocked.value.status_code == 403
        assert blocked.value.detail["code"] == "SUB_ACCOUNT_FEATURE_DISABLED"
        # 未映射科目即便有权限行也直通。
        enforce_sub_account_feature(conn, actor_id="s2", service="quality_inspection")

        # 母账号直通：即便有人绕过 API 给它插一行。
        _insert_permissions(conn, "m1", businesses=[])
        enforce_sub_account_feature(conn, actor_id="m1", service="asr")


def test_api_keys_and_publish_gates(permission_dsn: str) -> None:
    with psycopg.connect(permission_dsn, autocommit=True) as conn:
        # 无行 = 两个开关都直通。
        enforce_sub_account_api_keys(conn, actor_id="s1")
        enforce_sub_account_publish_accounts(conn, actor_id="s1")

        _insert_permissions(conn, "s2", businesses=ALL_BUSINESSES, allow_api_keys=False)
        with pytest.raises(HTTPException) as keys_blocked:
            enforce_sub_account_api_keys(conn, actor_id="s2")
        assert keys_blocked.value.status_code == 403
        assert keys_blocked.value.detail["code"] == "SUB_ACCOUNT_API_KEYS_DISABLED"
        # 发布开关未关 → 直通。
        enforce_sub_account_publish_accounts(conn, actor_id="s2")

        _insert_permissions(
            conn,
            "s3",
            businesses=ALL_BUSINESSES,
            allow_publish_accounts=False,
        )
        with pytest.raises(HTTPException) as publish_blocked:
            enforce_sub_account_publish_accounts(conn, actor_id="s3")
        assert publish_blocked.value.status_code == 403
        assert publish_blocked.value.detail["code"] == "SUB_ACCOUNT_PUBLISH_ACCOUNTS_DISABLED"
        enforce_sub_account_api_keys(conn, actor_id="s3")


# ---------------------------------------------------------------------------
# API: creation, PUT permissions, role changes, SUB_ADMIN scope
# ---------------------------------------------------------------------------


def test_create_with_initial_permissions_is_atomic(client, route_state: str) -> None:
    master_headers, _ = _master_session(client, "perm_master_create")
    created = _create_sub(
        client,
        master_headers,
        username="born_restricted",
        permissions={
            "businesses": ["video", "oral"],
            "allow_api_keys": False,
            "allow_publish_accounts": True,
        },
    )
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["permissions"] == {
        "businesses": ["video", "oral"],
        "allow_api_keys": False,
        "allow_publish_accounts": True,
    }

    with psycopg.connect(route_state) as conn:
        row = conn.execute(
            "SELECT allowed_businesses, allow_api_keys, allow_publish_accounts "
            "FROM sub_account_permissions WHERE user_id = %s",
            (sub["id"],),
        ).fetchone()
    assert row is not None
    assert json.loads(str(row[0])) == ["video", "oral"]  # TEXT-JSON
    assert row[1] is False and row[2] is True

    # 全开值创建 = 无行（唯一"全开"表达）。
    unrestricted = _create_sub(
        client,
        master_headers,
        username="born_open",
        permissions={
            "businesses": ALL_BUSINESSES,
            "allow_api_keys": True,
            "allow_publish_accounts": True,
        },
    )
    assert unrestricted.status_code == 201, unrestricted.text
    assert unrestricted.json()["permissions"] is None
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM sub_account_permissions WHERE user_id = %s",
                (unrestricted.json()["id"],),
            ).fetchone()[0]
            == 0
        )

    # 非法键在事务外即被拒：账号不落库（创建是原子的）。
    rejected = _create_sub(
        client,
        master_headers,
        username="born_broken",
        permissions={
            "businesses": ["teleport"],
            "allow_api_keys": True,
            "allow_publish_accounts": True,
        },
    )
    assert rejected.status_code == 422
    assert _detail_code(rejected) == "INVALID_BUSINESS_PERMISSIONS"
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute("SELECT count(*) FROM users WHERE username = 'born_broken'").fetchone()[0]
            == 0
        )


def test_put_permissions_upserts_then_full_grant_clears(client, route_state: str) -> None:
    master_headers, _ = _master_session(client, "perm_master_put")
    sub = _create_sub(client, master_headers, username="put_target").json()

    first = _set_permissions(
        client,
        master_headers,
        sub["id"],
        businesses=["video", "oral"],
        allow_api_keys=True,
        allow_publish_accounts=False,
    )
    assert first.status_code == 200, first.text
    assert first.json()["permissions"] == {
        "businesses": ["video", "oral"],
        "allow_api_keys": True,
        "allow_publish_accounts": False,
    }

    # 更新为更窄：仍是同一行（ON CONFLICT），空数组 = 全部业务禁用。
    second = _set_permissions(
        client,
        master_headers,
        sub["id"],
        businesses=[],
        allow_api_keys=False,
        allow_publish_accounts=False,
    )
    assert second.status_code == 200, second.text
    assert second.json()["permissions"]["businesses"] == []
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM sub_account_permissions WHERE user_id = %s",
                (sub["id"],),
            ).fetchone()[0]
            == 1
        )

    # 全开 → 删除行，payload 回到 None；列表同步。
    cleared = _set_permissions(client, master_headers, sub["id"], businesses=ALL_BUSINESSES)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["permissions"] is None
    listing = client.get(SUB_ACCOUNTS_PATH, headers=master_headers).json()["sub_accounts"]
    assert listing[0]["permissions"] is None
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM sub_account_permissions WHERE user_id = %s",
                (sub["id"],),
            ).fetchone()[0]
            == 0
        )


def test_put_permissions_rejects_unknown_keys_and_guards(client) -> None:
    master_a_headers, _ = _master_session(client, "perm_master_ga")
    master_b_headers, _ = _master_session(client, "perm_master_gb")
    own = _create_sub(client, master_a_headers, username="guard_target").json()
    foreign = _create_sub(client, master_b_headers, username="guard_foreign").json()

    unknown = _set_permissions(client, master_a_headers, own["id"], businesses=["teleport"])
    assert unknown.status_code == 422
    assert _detail_code(unknown) == "INVALID_BUSINESS_PERMISSIONS"

    foreign_attempt = _set_permissions(
        client, master_a_headers, foreign["id"], businesses=["video"]
    )
    assert foreign_attempt.status_code == 404
    assert _detail_code(foreign_attempt) == "SUB_ACCOUNT_NOT_FOUND"

    unknown_id = _set_permissions(
        client, master_a_headers, "sub-does-not-exist", businesses=["video"]
    )
    assert unknown_id.status_code == 404
    assert _detail_code(unknown_id) == "SUB_ACCOUNT_NOT_FOUND"

    assert (
        client.put(
            f"{SUB_ACCOUNTS_PATH}/{own['id']}/permissions",
            json={
                "businesses": ["video"],
                "allow_api_keys": True,
                "allow_publish_accounts": True,
            },
        ).status_code
        == 401
    )


def test_role_change_via_patch_updates_login_identity(client) -> None:
    master_headers, _ = _master_session(client, "perm_master_role")
    sub = _create_sub(client, master_headers, username="role_worker").json()
    assert sub["account_type"] == "SUB"

    promoted = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{sub['id']}",
        headers=master_headers,
        json={"account_type": "SUB_ADMIN"},
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["account_type"] == "SUB_ADMIN"

    _, login = _sub_session(client, "role_worker")
    assert login["account_type"] == "SUB_ADMIN"

    demoted = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{sub['id']}",
        headers=master_headers,
        json={"account_type": "SUB"},
    )
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["account_type"] == "SUB"

    # Literal 值域：非法角色是 422，不是静默落库。
    invalid = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{sub['id']}",
        headers=master_headers,
        json={"account_type": "MASTER"},
    )
    assert invalid.status_code == 422


def test_sub_admin_manages_plain_subs_only(client) -> None:
    master_headers, _ = _master_session(client, "perm_master_admin")
    plain = _create_sub(client, master_headers, username="admin_plain").json()
    boss = _create_sub(client, master_headers, username="admin_boss").json()
    promoted = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{boss['id']}",
        headers=master_headers,
        json={"account_type": "SUB_ADMIN"},
    )
    assert promoted.status_code == 200, promoted.text

    boss_headers, _ = _sub_session(client, "admin_boss")

    # 列表与对普通 SUB 的配置：管理员可以。
    listed = client.get(SUB_ACCOUNTS_PATH, headers=boss_headers)
    assert listed.status_code == 200, listed.text
    assert {item["id"] for item in listed.json()["sub_accounts"]} == {plain["id"], boss["id"]}

    quota = client.put(
        f"{SUB_ACCOUNTS_PATH}/{plain['id']}/quota",
        headers=boss_headers,
        json={"monthly_quota_credits": 120},
    )
    assert quota.status_code == 200, quota.text
    assert quota.json()["monthly_quota_credits"] == 120

    granted = _set_permissions(client, boss_headers, plain["id"], businesses=["video"])
    assert granted.status_code == 200, granted.text
    assert granted.json()["permissions"]["businesses"] == ["video"]

    # 管理员行（含自己）不在授权范围：403，且配置未变。
    self_scope = client.put(
        f"{SUB_ACCOUNTS_PATH}/{boss['id']}/quota",
        headers=boss_headers,
        json={"monthly_quota_credits": 999},
    )
    assert self_scope.status_code == 403
    assert _detail_code(self_scope) == "MASTER_ACCOUNT_REQUIRED"
    admin_scope = _set_permissions(client, boss_headers, boss["id"], businesses=[])
    assert admin_scope.status_code == 403
    assert _detail_code(admin_scope) == "MASTER_ACCOUNT_REQUIRED"

    # 结构变更仍归母账号：创建/改名/密码/删除全部 403。
    assert _create_sub(client, boss_headers, username="admin_child").status_code == 403
    assert (
        client.patch(
            f"{SUB_ACCOUNTS_PATH}/{plain['id']}",
            headers=boss_headers,
            json={"display_name": "改名"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{SUB_ACCOUNTS_PATH}/{plain['id']}/password",
            headers=boss_headers,
            json={"password": "brand-new-pass-9"},
        ).status_code
        == 403
    )
    assert (
        client.delete(f"{SUB_ACCOUNTS_PATH}/{plain['id']}", headers=boss_headers).status_code == 403
    )


def test_plain_sub_refused_by_quota_permission_and_list_lanes(client) -> None:
    master_headers, _ = _master_session(client, "perm_master_reg")
    target = _create_sub(client, master_headers, username="reg_target").json()
    assert _create_sub(client, master_headers, username="reg_plain").status_code == 201
    sub_headers, _ = _sub_session(client, "reg_plain")

    listed = client.get(SUB_ACCOUNTS_PATH, headers=sub_headers)
    assert listed.status_code == 403
    assert _detail_code(listed) == "MASTER_ACCOUNT_REQUIRED"

    quota = client.put(
        f"{SUB_ACCOUNTS_PATH}/{target['id']}/quota",
        headers=sub_headers,
        json={"monthly_quota_credits": 10},
    )
    assert quota.status_code == 403
    assert _detail_code(quota) == "MASTER_ACCOUNT_REQUIRED"

    permissions = _set_permissions(client, sub_headers, target["id"], businesses=["video"])
    assert permissions.status_code == 403
    assert _detail_code(permissions) == "MASTER_ACCOUNT_REQUIRED"


# ---------------------------------------------------------------------------
# Enforcement: accept_operation, the Token lane, publish-account binding
# ---------------------------------------------------------------------------


def test_accept_operation_blocks_disabled_features_before_credits(client, route_state: str) -> None:
    master_headers, master = _master_session(client, "perm_master_e2e")
    restricted = _create_sub(
        client,
        master_headers,
        username="perm_e2e_worker",
        permissions={
            "businesses": ["oral"],
            "allow_api_keys": True,
            "allow_publish_accounts": True,
        },
    ).json()
    sub_id = restricted["id"]

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        raw.execute("DELETE FROM billing_tariffs WHERE service IN ('asr', 'video_768p', 'oral')")
        raw.execute(
            "INSERT INTO billing_tariffs(service, enabled, unit_credits, unit_cost_fen) "
            "VALUES ('asr', true, 4, 1)"
        )
        raw.execute(
            "UPDATE wallets SET available_credits = 100, reserved_credits = 0 WHERE user_id = %s",
            (master["user_id"],),
        )

        # 有价科目被禁：403，且不碰钱包、不建计费单（准入在 credits 分支之前）。
        with pytest.raises(HTTPException) as priced:
            accept_operation(conn, user_id=sub_id, service="asr", source_id="perm-e2e-1", units=1)
        assert priced.value.status_code == 403
        assert priced.value.detail["code"] == "SUB_ACCOUNT_FEATURE_DISABLED"
        wallet = raw.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
            (master["user_id"],),
        ).fetchone()
        assert (int(wallet[0]), int(wallet[1])) == (100, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id = %s", (sub_id,)
            ).fetchone()[0]
            == 0
        )

        # 未配置计价（credits=0、免费）的科目同样被拦：功能准入独立于计价。
        with pytest.raises(HTTPException) as free:
            accept_operation(
                conn, user_id=sub_id, service="video_768p", source_id="perm-e2e-2", units=1
            )
        assert free.value.detail["code"] == "SUB_ACCOUNT_FEATURE_DISABLED"

        # 允许的业务直通：建出计费单（oral 未配价 → 免费单），钱包不动。
        operation_id = accept_operation(
            conn, user_id=sub_id, service="oral", source_id="perm-e2e-3", units=1
        )
        assert operation_id
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE id = %s", (operation_id,)
            ).fetchone()[0]
            == 1
        )

        # 母账号控制组：同为 asr，不因"组织里有受限子账号"而受影响。
        master_op = accept_operation(
            conn, user_id=master["user_id"], service="asr", source_id="perm-e2e-master", units=1
        )
        assert master_op
        wallet = raw.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
            (master["user_id"],),
        ).fetchone()
        assert (int(wallet[0]), int(wallet[1])) == (96, 4)


def test_api_key_creation_is_gated_and_replays_are_too(client) -> None:
    master_headers, _ = _master_session(client, "perm_master_keys")
    allowed = _create_sub(
        client,
        master_headers,
        username="key_allowed",
        permissions={
            "businesses": ["video"],
            "allow_api_keys": True,
            "allow_publish_accounts": True,
        },
    ).json()
    blocked = _create_sub(
        client,
        master_headers,
        username="key_blocked",
        permissions={
            "businesses": ["video"],
            "allow_api_keys": False,
            "allow_publish_accounts": True,
        },
    ).json()
    assert allowed["id"] and blocked["id"]

    blocked_headers, _ = _sub_session(client, "key_blocked")
    denied = client.post(
        API_KEYS_PATH,
        headers={**blocked_headers, "Idempotency-Key": str(uuid4())},
        json={"label": "受限"},
    )
    assert denied.status_code == 403, denied.text
    assert _detail_code(denied) == "SUB_ACCOUNT_API_KEYS_DISABLED"

    # 有行且开关打开：直通，真创建出 Token。
    allowed_headers, _ = _sub_session(client, "key_allowed")
    retry_key = str(uuid4())
    created = client.post(
        API_KEYS_PATH,
        headers={**allowed_headers, "Idempotency-Key": retry_key},
        json={"label": "允许"},
    )
    assert created.status_code == 201, created.text
    assert str(created.json()["plaintext"]).startswith("xsk_live_")

    # 幂等重放也走同一道门：关掉开关后重放不再返回明文。
    turned_off = _set_permissions(
        client, master_headers, allowed["id"], businesses=["video"], allow_api_keys=False
    )
    assert turned_off.status_code == 200, turned_off.text
    replayed = client.post(
        API_KEYS_PATH,
        headers={**allowed_headers, "Idempotency-Key": retry_key},
        json={"label": "允许"},
    )
    assert replayed.status_code == 403
    assert _detail_code(replayed) == "SUB_ACCOUNT_API_KEYS_DISABLED"


def test_publish_account_binding_is_gated_by_the_switch(client) -> None:
    master_headers, _ = _master_session(client, "perm_master_pub")
    blocked = _create_sub(
        client,
        master_headers,
        username="pub_blocked",
        permissions={
            "businesses": ["video"],
            "allow_api_keys": True,
            "allow_publish_accounts": False,
        },
    ).json()
    allowed = _create_sub(
        client,
        master_headers,
        username="pub_allowed",
        permissions={
            "businesses": ["video"],
            "allow_api_keys": True,
            "allow_publish_accounts": True,
        },
    ).json()
    assert blocked["id"] and allowed["id"]

    payload = {
        "platform": "douyin",
        "identity": {"platform_user_id": "uid-perm", "username": "权限号"},
        "storage_state": {
            "cookies": [{"name": "session", "value": "x", "domain": ".douyin.com"}],
            "origins": [],
        },
    }

    blocked_headers, _ = _sub_session(client, "pub_blocked")
    denied = client.post(
        PUBLISH_BROWSER_PATH + "/accounts/import", headers=blocked_headers, json=payload
    )
    assert denied.status_code == 403, denied.text
    assert _detail_code(denied) == "SUB_ACCOUNT_PUBLISH_ACCOUNTS_DISABLED"

    # 扫码绑定（SSE 入口）同样被挡，且不会启动浏览器。
    login_denied = client.post(
        PUBLISH_BROWSER_PATH + "/logins", headers=blocked_headers, json={"platform": "douyin"}
    )
    assert login_denied.status_code == 403
    assert _detail_code(login_denied) == "SUB_ACCOUNT_PUBLISH_ACCOUNTS_DISABLED"

    # 开关打开：导入直通（加密落库细节归发布模块的测试）。
    allowed_headers, _ = _sub_session(client, "pub_allowed")
    imported = client.post(
        PUBLISH_BROWSER_PATH + "/accounts/import", headers=allowed_headers, json=payload
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["platform_user_id"] == "uid-perm"
