"""方案 P2-3 — 客户标注：标签 / 备注 / 负责人（``customer_annotations``）。

契约要点（与 ``test_admin_customer_routes`` 的 T23 泳道同一套夹具技法）：

- 迁移 ``20260929T1000_customer_annotations``：按用户主键关联的标注行，
  ``tags_json`` 是 JSONB 数组（≤10 项），``note`` 上限 2000 字符，
  ``owner_user_id`` 指向管理员账号（可空）；三条 CHECK 是应用层校验之外
  的数据库护栏（defense in depth）；
- 读取（GET）降级为零值：没有标注行 = 空标签 / 空备注 / 无负责人，
  未知用户 404 ``USER_NOT_FOUND``；
- 写入（PUT）是整体替换：标签去空白、去空项、保序去重后再校验；
  三个字段全空即删除整行（空标注不落行）；
- 写入契约（dev doc §15）：真实管理员会话（审计员 403 AUDITOR_READ_ONLY）、
  Idempotency-Key、confirm=true、非空 reason；同一幂等键重放返回原快照
  （X-Idempotent-Replay），换参数撞同键 409 IDEMPOTENCY_CONFLICT；
- 每次实际写入落一条 ``audit_logs``（``customer_annotation.update``），
  旧值/新值都在 ``metadata_json`` 里；重放不产生第二条；
- 负责人候选只列启用中的管理员（审计员与停用账号不进列表）；
- 客户列表端点携带标注三件套（P2-3「列表补字段」）；
- 没有 PG 运行时 → 503 ``CUSTOMER_ANNOTATION_UNAVAILABLE``（fail-closed）。
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip
from psycopg.errors import CheckViolation

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

ANNOTATIONS_DB_NAME = "customer_annotations_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)  # admin-session HMAC key

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"

CUSTOMER_USER_ID = "customer_u"
QUIET_USER_ID = "quiet_u"

ANNOTATION_ACTION = "customer_annotation.update"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _db_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{ANNOTATIONS_DB_NAME}"


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def annotations_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{ANNOTATIONS_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{ANNOTATIONS_DB_NAME}"')
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
            conn.execute(f'DROP DATABASE IF EXISTS "{ANNOTATIONS_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(annotations_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE admin_write_idempotency, admin_sessions, audit_logs, "
            "customer_annotations, wallet_transactions, recharge_orders, wallets, "
            "users, security_rate_limit_counters, security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('ops_admin', 'ops_admin', 'Ops Admin', 'admin'), "
            "('retired_admin', 'retired_admin', 'Retired Admin', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor User', 'auditor'), "
            f"('{CUSTOMER_USER_ID}', 'customer_u', 'Customer Co', 'customer'), "
            f"('{QUIET_USER_ID}', 'quiet_u', 'Quiet Co', 'customer')"
        )
        conn.execute("UPDATE users SET is_active = 0 WHERE id = 'retired_admin'")
        # 客户列表条件（role='customer' + registration_source）需要这两个字段；
        # self_register 约束要求 password_hash 非空，给个假哈希即可。
        conn.execute(
            "UPDATE users SET registration_source = 'self_register', "
            "password_hash = 'scrypt$fixture' "
            f"WHERE id IN ('{CUSTOMER_USER_ID}', '{QUIET_USER_ID}')"
        )
    yield annotations_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_annotation_routes import router as annotation_router
    from app.admin_customer_routes import router as admin_customer_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_customer_router)
    app.include_router(annotation_router)
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


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _annotation_path(user_id: str = CUSTOMER_USER_ID) -> str:
    return f"/api/control/customers/{user_id}/annotation"


def _put_annotation(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    user_id: str = CUSTOMER_USER_ID,
    tags: list[str] | None = None,
    note: str = "",
    owner_user_id: str | None = None,
    reason: str = "客户交接：标注标签与负责人",
    confirm: bool = True,
    key: str | None = None,
) -> object:
    headers = dict(admin_headers)
    headers[IDEMPOTENCY_KEY_HEADER] = key or f"key-{uuid.uuid4()}"
    return client.put(
        _annotation_path(user_id),
        json={
            "confirm": confirm,
            "reason": reason,
            "tags": tags if tags is not None else [],
            "note": note,
            "owner_user_id": owner_user_id,
        },
        headers=headers,
    )


def _fetch_one(query: str, params: tuple[object, ...] = ()) -> tuple | None:
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        return conn.execute(query, params).fetchone()


def _fetch_all(query: str, params: tuple[object, ...] = ()) -> list[tuple]:
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        return conn.execute(query, params).fetchall()


def _annotation_rows() -> list[tuple]:
    return _fetch_all(
        "SELECT user_id, tags_json, note, owner_user_id, updated_by_user_id "
        "FROM customer_annotations ORDER BY user_id"
    )


def _annotation_audit_rows() -> list[tuple]:
    return _fetch_all(
        "SELECT entity_id, metadata_json FROM audit_logs WHERE action = %s ORDER BY created_at, id",
        (ANNOTATION_ACTION,),
    )


# ---------------------------------------------------------------------------
# Schema / migration (revision 20260929T1000)
# ---------------------------------------------------------------------------


def test_customer_annotations_table_shape(annotations_dsn: str) -> None:
    columns = {
        str(row[0])
        for row in _fetch_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'customer_annotations'"
        )
    }
    assert {
        "user_id",
        "tags_json",
        "note",
        "owner_user_id",
        "updated_by_user_id",
        "updated_at",
    } <= columns
    check_names = {
        str(row[0])
        for row in _fetch_all(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'customer_annotations'::regclass AND contype = 'c'"
        )
    }
    assert {
        "ck_customer_annotations_tags_array",
        "ck_customer_annotations_tags_count",
        "ck_customer_annotations_note_length",
    } <= check_names


def test_customer_annotations_check_constraints(annotations_dsn: str) -> None:
    """数据库护栏（defense in depth）：坏形状即使绕过路由也插不进去。"""
    with psycopg.connect(_db_dsn()) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('schema_u', 'schema_u', 'Schema', 'customer') "
            "ON CONFLICT DO NOTHING"
        )
        conn.commit()

        def _insert(tags_json: str, note: str) -> None:
            conn.execute(
                "INSERT INTO customer_annotations "
                "(user_id, tags_json, note, owner_user_id, updated_by_user_id) "
                "VALUES ('schema_u', %s::jsonb, %s, NULL, 'admin_u')",
                (tags_json, note),
            )

        for tags_json, note in (
            ('{"a": 1}', ""),  # 对象而不是数组
            (json.dumps([f"t{i}" for i in range(11)]), ""),  # 超过 10 个标签
            ("[]", "x" * 2001),  # 备注超过 2000 字符
        ):
            with pytest.raises(CheckViolation):
                try:
                    _insert(tags_json, note)
                finally:
                    conn.rollback()


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


def test_read_annotation_defaults_to_zero_value(client: TestClient) -> None:
    """无标注行 = 零值 payload：前端不需要区分「没标过」与「被清空」。"""
    admin = _admin_session(client)
    response = client.get(_annotation_path(), headers=admin)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["user_id"] == CUSTOMER_USER_ID
    assert payload["tags"] == []
    assert payload["note"] == ""
    assert payload["owner_user_id"] == ""
    assert payload["owner_username"] == ""
    assert payload["updated_by_user_id"] == ""
    assert payload["updated_at"] == ""


def test_read_annotation_unknown_user_is_404(client: TestClient) -> None:
    admin = _admin_session(client)
    response = client.get(_annotation_path("no-such-user"), headers=admin)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "USER_NOT_FOUND"


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def test_put_creates_annotation_with_audit_row(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _put_annotation(
        client,
        admin,
        tags=["VIP", " VIP ", "risk", ""],
        note="  优先交付  ",
        owner_user_id="ops_admin",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    # 归一化：去空白、去空项、保序去重；备注两端的空白也去掉。
    assert payload["tags"] == ["VIP", "risk"]
    assert payload["note"] == "优先交付"
    assert payload["owner_user_id"] == "ops_admin"
    assert payload["owner_username"] == "ops_admin"
    assert payload["updated_by_user_id"] == "admin_u"
    assert payload["updated_at"]

    rows = _annotation_rows()
    assert len(rows) == 1
    assert str(rows[0][0]) == CUSTOMER_USER_ID
    assert rows[0][1] == ["VIP", "risk"]
    assert str(rows[0][2]) == "优先交付"
    assert str(rows[0][3]) == "ops_admin"
    assert str(rows[0][4]) == "admin_u"

    audit = _annotation_audit_rows()
    assert len(audit) == 1
    assert str(audit[0][0]) == CUSTOMER_USER_ID
    metadata = json.loads(str(audit[0][1]))
    assert metadata["old_tags"] == []
    assert metadata["new_tags"] == ["VIP", "risk"]
    assert metadata["old_owner_user_id"] == ""
    assert metadata["new_owner_user_id"] == "ops_admin"
    assert metadata["reason"] == "客户交接：标注标签与负责人"


def test_put_replaces_the_whole_annotation(client: TestClient) -> None:
    """整体替换语义：后一次 PUT 覆盖全部三列，不留上一版残留。"""
    admin = _admin_session(client)
    first = _put_annotation(
        client, admin, tags=["A"], note="第一版", owner_user_id="ops_admin", key="ann-r-1"
    )
    assert first.status_code == 200, first.text
    second = _put_annotation(
        client, admin, tags=["B", "C"], note="第二版", owner_user_id=None, key="ann-r-2"
    )
    assert second.status_code == 200, second.text
    payload = second.json()
    assert payload["tags"] == ["B", "C"]
    assert payload["note"] == "第二版"
    assert payload["owner_user_id"] == ""
    rows = _annotation_rows()
    assert len(rows) == 1
    assert rows[0][1] == ["B", "C"]
    assert str(rows[0][2]) == "第二版"
    assert rows[0][3] is None


def test_put_validation_refusals_leave_no_row(client: TestClient) -> None:
    admin = _admin_session(client)
    cases = (
        ({"tags": [f"t{i}" for i in range(11)]}, "超过 10 个标签"),
        ({"tags": ["x" * 25]}, "单个标签超长"),
        ({"note": "y" * 2001}, "备注超长"),
        ({"owner_user_id": CUSTOMER_USER_ID}, "负责人不是管理员"),
        ({"owner_user_id": "auditor_u"}, "负责人是审计员"),
        ({"owner_user_id": "retired_admin"}, "负责人已停用"),
        ({"owner_user_id": "no-such-admin"}, "负责人不存在"),
    )
    for payload, label in cases:
        response = _put_annotation(
            client,
            admin,
            tags=payload.get("tags"),
            note=payload.get("note", ""),
            owner_user_id=payload.get("owner_user_id"),
        )
        assert response.status_code == 400, (label, response.text)
        assert response.json()["detail"]["code"] == "CUSTOMER_ANNOTATION_VALIDATION_FAILED"
    assert _annotation_rows() == []
    assert _annotation_audit_rows() == []


def test_put_clearing_all_fields_deletes_the_row(client: TestClient) -> None:
    admin = _admin_session(client)
    seeded = _put_annotation(
        client, admin, tags=["VIP"], note="有内容", owner_user_id="ops_admin", key="ann-c-1"
    )
    assert seeded.status_code == 200, seeded.text
    cleared = _put_annotation(client, admin, key="ann-c-2")
    assert cleared.status_code == 200, cleared.text
    payload = cleared.json()
    assert payload["tags"] == []
    assert payload["note"] == ""
    assert payload["owner_user_id"] == ""
    assert payload["updated_at"] == ""
    assert _annotation_rows() == []
    # 两次真实写入（一次建立、一次清空）各留一条审计；清空动作本身也要留痕。
    assert len(_annotation_audit_rows()) == 2


def test_put_replays_by_idempotency_key_and_conflicts_on_change(
    client: TestClient,
) -> None:
    admin = _admin_session(client)
    key = "ann-idem-1"
    first = _put_annotation(client, admin, tags=["VIP"], note="首版", key=key)
    assert first.status_code == 200, first.text
    replay = _put_annotation(client, admin, tags=["VIP"], note="首版", key=key)
    assert replay.status_code == 200, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json()["tags"] == ["VIP"]
    # 同键不同参数：409，且不产生第二行数据与第二条审计。
    conflict = _put_annotation(client, admin, tags=["其他"], note="首版", key=key)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(_annotation_audit_rows()) == 1
    assert [row[1] for row in _annotation_rows()] == [["VIP"]]


def test_auditor_reads_but_cannot_write(client: TestClient) -> None:
    admin = _admin_session(client)
    seeded = _put_annotation(client, admin, tags=["VIP"], key="ann-a-1")
    assert seeded.status_code == 200, seeded.text

    auditor = _admin_session(client, "auditor_u")
    read = client.get(_annotation_path(), headers=auditor)
    assert read.status_code == 200, read.text
    assert read.json()["tags"] == ["VIP"]
    write = _put_annotation(client, auditor, tags=["X"], key="ann-a-2")
    assert write.status_code == 403, write.text
    assert write.json()["detail"]["code"] == "AUDITOR_READ_ONLY"
    assert len(_annotation_audit_rows()) == 1


# ---------------------------------------------------------------------------
# Owner candidates & list integration
# ---------------------------------------------------------------------------


def test_owner_candidates_lists_active_admins_only(client: TestClient) -> None:
    admin = _admin_session(client)
    response = client.get("/api/control/customers/owner-candidates", headers=admin)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["user_id"] for item in items] == ["admin_u", "ops_admin"]
    assert items[0]["username"] == "admin_u"
    # 审计员只读也能看候选（负责人选择是只读信息）。
    auditor = _admin_session(client, "auditor_u")
    assert client.get("/api/control/customers/owner-candidates", headers=auditor).status_code == 200


def test_customer_list_carries_annotation_fields(client: TestClient) -> None:
    admin = _admin_session(client)
    seeded = _put_annotation(
        client,
        admin,
        tags=["重点"],
        note="备注从列表不返回全文之外的字段",
        owner_user_id="ops_admin",
    )
    assert seeded.status_code == 200, seeded.text

    listing = client.get("/api/control/customers", headers=admin)
    assert listing.status_code == 200, listing.text
    by_id = {item["user_id"]: item for item in listing.json()["items"]}
    annotated = by_id[CUSTOMER_USER_ID]
    assert annotated["tags"] == ["重点"]
    assert annotated["note"] == "备注从列表不返回全文之外的字段"
    assert annotated["owner_user_id"] == "ops_admin"
    assert annotated["owner_username"] == "ops_admin"
    # 没有标注的客户：零值，不需要前端兜底。
    quiet = by_id[QUIET_USER_ID]
    assert quiet["tags"] == []
    assert quiet["note"] == ""
    assert quiet["owner_user_id"] == ""
    assert quiet["owner_username"] == ""


# ---------------------------------------------------------------------------
# Fail-closed runtime
# ---------------------------------------------------------------------------


def test_missing_pg_runtime_fails_closed(monkeypatch: pytest.MonkeyPatch, route_state: str) -> None:
    from app import admin_auth_routes
    from app.admin_auth_routes import AdminActor
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_annotation_routes import router as annotation_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(annotation_router)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    stub_actor = AdminActor(
        user_id="admin_u",
        username="admin_u",
        display_name="Admin User",
        role="admin",
        is_super_admin=False,
        auth_method="password",
        session_id="sess-nopg",
        session_expires_at="2099-01-01T00:00:00+00:00",
        last_activity_at="2026-01-01T00:00:00+00:00",
    )

    def _stub_actor() -> AdminActor:
        return stub_actor

    # AdminReader 是 `Annotated[..., Depends(get_admin_actor)]`，导入时就把原
    # 函数对象捕进了依赖树，monkeypatch 模块属性换不掉它；按仓库先例
    # （test_admin_generation_media）用 dependency_overrides 顶掉真实会话
    # 依赖（其自身的 fail-closed 在 test_admin_auth 有专测），让请求落到
    # 路由体的 PG 守卫。
    app.dependency_overrides[admin_auth_routes.get_admin_actor] = _stub_actor
    close_pg_pool()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        read = test_client.get(_annotation_path(), headers={})
        write = _put_annotation(test_client, {}, tags=["X"], key="ann-nopg-1")
    assert read.status_code == 503, read.text
    assert read.json()["detail"]["code"] == "CUSTOMER_ANNOTATION_UNAVAILABLE"
    assert write.status_code == 503, write.text
    assert write.json()["detail"]["code"] == "CUSTOMER_ANNOTATION_UNAVAILABLE"
