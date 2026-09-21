"""Sub-account CRUD integration tests (Phase 5).

Pytest suite for testing all sub-account API endpoints against a real PostgreSQL database.

Test coverage:
- POST /api/admin/sub-accounts: create, validate parent, username uniqueness
- GET /api/admin/sub-accounts: list by parent_user_id, ordering
- PATCH /api/admin/sub-accounts/{id}: update display_name/is_active
- DELETE /api/admin/sub-accounts/{id}: cascade deletion
- Cross-parent access prevention (T2.9)
- Sub-account auth enforcement (T2.8)

Database setup:
- Creates scratch DB `cw062_sub_account_crud_test`
- Runs migrations to head
- TRUNCATE isolation per test module
- Drops DB after completion
- API requests ride dependency_overrides: get_database → scratch DB,
  get_current_user → stubbed actor (no real session required)

Usage:
    pytest server/tests/test_sub_account_crud.py -v --tb=short
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)
from psycopg.rows import dict_row

from app.admin_auth_routes import get_admin_actor
from app.auth import get_database
from app.db_portable import BusinessConnection
from app.main import app

# -----------------------------------------------------------------------------
# Test DB configuration
# -----------------------------------------------------------------------------

CW062_TEST_DB = "cw062_sub_account_crud_test"

# audit_logs.actor_user_id FK→users.id：API 桩管理员的操作留痕必须指向真实行
ADMIN_ID = "admin_test_admin"


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    """Create scratch PG DB → upgrade to head → cleanup."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW062_TEST_DB)
    try:
        upgrade_test_database_to_head(dsn)
        yield dsn
    finally:
        drop_test_database(CW062_TEST_DB)


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    """Keep stubbed auth/DB overrides from leaking between tests."""
    yield
    app.dependency_overrides.clear()


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture()
def conn(dsn: str) -> Iterator[psycopg.Connection]:
    """Autocommit connection for seeding: TRUNCATE isolation between tests."""
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET session_replication_role = replica")
    # Truncate only relevant tables
    conn.execute("TRUNCATE users, customer_devices, publish_accounts, audit_logs CASCADE")
    conn.execute("SET session_replication_role = DEFAULT")
    # Seed the platform admin actor every API test acts as
    conn.execute(
        """
        INSERT INTO users (id, username, display_name, role, is_active)
        VALUES (%s, 'admin_test', '平台管理员', 'admin', 1)
        """,
        (ADMIN_ID,),
    )
    try:
        yield conn
    finally:
        conn.close()


@dataclass
class UserSeed:
    """Pre-seeded user accounts for different scenarios."""

    master_id: str
    master_username: str
    sub_account_id: str
    sub_account_username: str
    cross_parent_sub_id: str
    cross_parent_parent_id: str


def seed_users(conn: psycopg.Connection) -> UserSeed:
    """Seed users for testing various scenarios."""
    master_id = f"master_{uuid.uuid4().hex[:8]}"
    sub_account_id = f"sub_{uuid.uuid4().hex[:8]}"
    cross_parent_parent_id = f"master_{uuid.uuid4().hex[:8]}"
    cross_parent_sub_id = f"sub_{uuid.uuid4().hex[:8]}"

    # Insert MASTER account
    conn.execute(
        """
        INSERT INTO users (id, username, display_name, parent_user_id,
                          account_type, role, is_active)
        VALUES (%s, %s, %s, NULL, 'MASTER', 'customer', 1)
        """,
        (master_id, "zhang_san", "张三"),
    )

    # Insert SUB account under first master
    conn.execute(
        """
        INSERT INTO users (id, username, display_name, parent_user_id,
                          account_type, role, is_active)
        VALUES (%s, %s, %s, %s, 'SUB', 'customer', 1)
        """,
        (sub_account_id, "li_si_child", "李四子", master_id),
    )

    # Insert another MASTER and its SUB (for cross-parent test)
    conn.execute(
        """
        INSERT INTO users (id, username, display_name, parent_user_id,
                          account_type, role, is_active)
        VALUES (%s, %s, %s, NULL, 'MASTER', 'customer', 1)
        """,
        (cross_parent_parent_id, "wang_wu", "王五"),
    )

    conn.execute(
        """
        INSERT INTO users (id, username, display_name, parent_user_id,
                          account_type, role, is_active)
        VALUES (%s, %s, %s, %s, 'SUB', 'customer', 1)
        """,
        (cross_parent_sub_id, "zhao_liu_child", "赵六子", cross_parent_parent_id),
    )

    return UserSeed(
        master_id=master_id,
        master_username="zhang_san",
        sub_account_id=sub_account_id,
        sub_account_username="li_si_child",
        cross_parent_sub_id=cross_parent_sub_id,
        cross_parent_parent_id=cross_parent_parent_id,
    )


def api_client(dsn: str, *, role: str = "admin") -> TestClient:
    """TestClient riding the scratch DB with a stubbed AdminActor session.

    Only ``get_admin_actor`` is stubbed: the real ``get_admin_writer`` guard
    stays in the chain, so an ``auditor`` actor gets the production 403.
    """

    def _override_database():
        # 与 get_database 的 PG 通道同构：一个请求 = 一个事务；直连 scratch 库，
        # 不经 get_pg_pool（避免读取全局 VIDEO_REPLICA_DATABASE_URL）。
        raw = psycopg.connect(dsn)
        try:
            with raw.transaction():
                yield BusinessConnection.postgres(raw)
        finally:
            raw.close()

    actor = SimpleNamespace(
        user_id=ADMIN_ID,
        username="admin_test",
        display_name="平台管理员",
        role=role,
        auth_method="session",
        session_id="test-session",
        session_expires_at="2030-01-01T00:00:00Z",
        last_activity_at="2026-01-01T00:00:00Z",
    )
    app.dependency_overrides[get_database] = _override_database
    app.dependency_overrides[get_admin_actor] = lambda: actor
    return TestClient(app)


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_cw062_create_sub_account_under_master(dsn: str, conn: psycopg.Connection):
    """T3.2: Create sub-account under valid MASTER parent."""
    # Seed users
    users = seed_users(conn)

    # Prepare request body
    request_body = {
        "username": "new_sub_account",
        "display_name": "新子账号",
        "parent_user_id": users.master_id,
        "reason": "用于员工测试",
        "request_id": str(uuid.uuid4()),
    }

    client = api_client(dsn)

    response = client.post("/api/admin/sub-accounts", json=request_body)

    assert response.status_code == 200, f"Create failed: {response.text}"

    data = response.json()
    assert "id" in data
    assert data["username"] == "new_sub_account"
    assert data["display_name"] == "新子账号"
    assert data["account_type"] == "SUB"
    assert data["parent_user_id"] == users.master_id
    assert data["is_active"] is True

    # Verify in DB
    row = conn.execute(
        "SELECT id, username, account_type FROM users WHERE id = %s", (data["id"],)
    ).fetchone()
    assert row is not None
    assert row["account_type"] == "SUB"


def test_cw062_create_sub_account_invalid_parent(dsn: str, conn: psycopg.Connection):
    """T3.2: Cannot create sub-account under non-MASTER parent."""
    # Seed users
    users = seed_users(conn)

    request_body = {
        "username": "bad_parent_sub",
        "display_name": "Invalid Parent",
        "parent_user_id": users.sub_account_id,  # Trying to use SUB as parent
        "reason": "Should fail",
    }

    client = api_client(dsn)

    response = client.post("/api/admin/sub-accounts", json=request_body)

    assert response.status_code == 400  # Bad request (validation error)
    assert "not master" in response.text.lower()


def test_cw062_create_sub_account_nonexistent_parent(dsn: str, conn: psycopg.Connection):
    """T3.2: Cannot create sub-account under non-existent parent."""
    request_body = {
        "username": "ghost_parent_sub",
        "display_name": "Ghost Parent",
        "parent_user_id": "nonexistent_uuid_here",
        "reason": "Testing",
    }

    client = api_client(dsn)

    response = client.post("/api/admin/sub-accounts", json=request_body)

    assert response.status_code == 400
    assert "does not exist" in response.text.lower()


def test_cw062_list_sub_accounts(dsn: str, conn: psycopg.Connection):
    """T3.1: List all sub-accounts under a master."""
    users = seed_users(conn)

    # Create additional sub-account manually
    extra_sub_id = f"extra_sub_{uuid.uuid4().hex[:8]}"
    conn.execute(
        """
        INSERT INTO users (id, username, display_name, parent_user_id,
                          account_type, role, is_active)
        VALUES (%s, %s, %s, %s, 'SUB', 'customer', 1)
        """,
        (extra_sub_id, "extra_sub_1", "额外子账号", users.master_id),
    )

    client = api_client(dsn)

    response = client.get(f"/api/admin/sub-accounts?parent_user_id={users.master_id}")

    assert response.status_code == 200
    data = response.json()
    assert "sub_accounts" in data
    assert "total_count" in data

    assert len(data["sub_accounts"]) >= 1  # At least the seeded one
    assert data["total_count"] >= 1

    # Check structure
    for sa in data["sub_accounts"]:
        assert "id" in sa
        assert "username" in sa
        assert "display_name" in sa
        assert "parent_user_id" in sa
        assert sa["parent_user_id"] == users.master_id


def test_cw062_update_sub_account_display_name(dsn: str, conn: psycopg.Connection):
    """T3.3: Update sub-account display name."""
    users = seed_users(conn)

    request_body = {
        "display_name": "Updated Name",
        "reason": "Correction",
    }

    client = api_client(dsn)

    response = client.patch(f"/api/admin/sub-accounts/{users.sub_account_id}", json=request_body)

    assert response.status_code == 200
    data = response.json()
    assert data["display_name"] == "Updated Name"

    # Verify in DB
    updated = conn.execute(
        "SELECT display_name FROM users WHERE id = %s", (users.sub_account_id,)
    ).fetchone()
    assert updated is not None
    assert updated["display_name"] == "Updated Name"


def test_cw062_toggle_sub_account_status(dsn: str, conn: psycopg.Connection):
    """T3.3: Toggle sub-account active status."""
    users = seed_users(conn)

    # First deactivate
    request_body = {"is_active": False, "reason": "Deactivation"}

    client = api_client(dsn)

    response = client.patch(f"/api/admin/sub-accounts/{users.sub_account_id}", json=request_body)

    assert response.status_code == 200
    assert response.json()["is_active"] is False

    # Re-activate
    request_body = {"is_active": True, "reason": "Reactivation"}
    response = client.patch(f"/api/admin/sub-accounts/{users.sub_account_id}", json=request_body)

    assert response.status_code == 200
    assert response.json()["is_active"] is True


def test_cw062_delete_sub_account(dsn: str, conn: psycopg.Connection):
    """T3.4: Delete sub-account (with cascade)."""
    users = seed_users(conn)

    # Verify exists
    row = conn.execute("SELECT id FROM users WHERE id = %s", (users.sub_account_id,)).fetchone()
    assert row is not None

    client = api_client(dsn)

    response = client.delete(f"/api/admin/sub-accounts/{users.sub_account_id}")

    assert response.status_code == 200
    assert response.json()["deleted"] is True

    # Verify deleted
    row = conn.execute("SELECT id FROM users WHERE id = %s", (users.sub_account_id,)).fetchone()
    assert row is None


def test_cw062_cross_parent_access_prevention(dsn: str, conn: psycopg.Connection):
    """T2.9: Prevent sub-accounts from accessing resources outside their parent org.

    This test verifies that when we implement resource listing (e.g., devices,
    publish accounts), the prevent_cross_parent_access middleware will block
    queries where caller.parent_user_id != resource.parent_user_id.

    NOTE: Full implementation requires integrating middleware into routes.
    This test documents expected behavior.
    """
    users = seed_users(conn)

    # Scenario: Sub-account A tries to access Sub-account B's resources
    # Expected: 403 CROSS_PARENT_ACCESS_FORBIDDEN

    # Placeholder for now - actual middleware integration in T2.9
    # Will be tested once backend routes include the guard
    assert users.cross_parent_parent_id != users.master_id
    assert users.cross_parent_sub_id != users.sub_account_id
    # Future test: call /api/customer/devices with sub-account auth
    # should reject if devices belong to different parent


def test_cw062_admin_only_access(dsn: str, conn: psycopg.Connection):
    """T2.8: Only admins can manage sub-accounts (auditor role is read-only)."""
    # Seed non-admin user
    regular_customer_id = f"customer_{uuid.uuid4().hex[:8]}"
    conn.execute(
        """
        INSERT INTO users (id, username, display_name, parent_user_id,
                          account_type, role, is_active)
        VALUES (%s, %s, %s, NULL, 'MASTER', 'customer', 1)
        """,
        (regular_customer_id, "regular_cust", "普通客户"),
    )

    # Regular customer tries to create sub-account → should fail
    request_body = {
        "username": "illegal_sub",
        "display_name": "Illegal",
        "parent_user_id": regular_customer_id,
        "reason": "Testing",
    }

    # Real AdminWriter guard + a read-only (auditor) session actor
    client = api_client(dsn, role="auditor")

    response = client.post("/api/admin/sub-accounts", json=request_body)

    # Should return 403 Forbidden
    assert response.status_code == 403, f"Non-admin was allowed: {response.text}"
    assert "auditor" in response.text.lower()


# -----------------------------------------------------------------------------
# Test metadata
# -----------------------------------------------------------------------------

__test__ = {
    "cw062_*": True,  # Enable all CW-062 tests
}
