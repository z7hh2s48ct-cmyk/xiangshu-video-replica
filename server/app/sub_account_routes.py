"""Sub-account management APIs (Phase 2 MVP).

Endpoints for creating, listing, updating and deleting sub-accounts under master accounts.
Design decisions:
- Sub-accounts are users.parent_user_id → ON DELETE CASCADE (set in Phase 1)
- Parent must be MASTER + role='customer' (enforced here)
- Account status reuses users.is_active (integer 0/1 CHECK), not a new column
- T2.10 actor tracking: mother consumes → actor=user_id; sub consumes → actor=sub_account_id
- Credentials (desktop follow-up): creation accepts an optional initial
  password and a dedicated endpoint rotates it; both write
  registration_source='admin_create', the origin the shared password-session
  rule (sub_account_auth.password_login_account_ok) requires. A sub-account
  without a password exists but cannot hold a session until one is set.

Security & audit:
- CW-026/027 管理面守卫：读写分离走 AdminReader / AdminWriter（管理端会话 + 写 CSRF）
- Cascade awareness: parent delete cascades to sub rows; a *sub* delete purges
  its session/device footprint first (those FKs carry no CASCADE) and answers
  409 SUB_ACCOUNT_HAS_HISTORY when ledger/task history pins the row
- Full audit logging on all mutations

Implemented September 2026 following audit Rev.2 decision that publish_accounts
belong to org (master-only); sub accounts share master's publish assets.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictStr

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import transaction_now_iso
from app.auth import Database
from app.customer_session_service import (
    REASON_SUB_ACCOUNT_PASSWORD_RESET,
    SUB_ACCOUNT_FOOTPRINT_TABLES,
    revoke_session,
)
from app.db_portable import BusinessConnection, IntegrityConstraintError
from app.password_hashing import PasswordPolicyError, hash_password

router = APIRouter(prefix="/api/admin", tags=["sub-accounts"])


class CreateSubAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: StrictStr
    display_name: StrictStr
    parent_user_id: str  # The master account ID
    # Optional initial credential: with a password the sub-account can log in
    # immediately; without one it exists but cannot hold a session until a
    # password is set via the reset endpoint.
    initial_password: StrictStr | None = None
    reason: str
    request_id: str | None = None


class ResetSubAccountPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: StrictStr
    reason: str
    request_id: str | None = None


class UpdateSubAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = None
    is_active: bool | None = None
    reason: str
    request_id: str | None = None


# ---------------------------------------------------------------------------
# Business logic helpers
# ---------------------------------------------------------------------------


def _validate_parent_exists(conn: BusinessConnection, parent_id: str) -> None:
    """Verify parent exists and has correct attributes."""
    row = conn.execute(
        "SELECT account_type, role FROM users WHERE id = %s",
        (parent_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Parent user {parent_id} does not exist")

    if row["account_type"] != "MASTER":
        raise ValueError(f"Parent {parent_id} is not MASTER type (must be MASTER)")
    if row["role"] != "customer":
        raise ValueError(f"Parent {parent_id} does not have role='customer'")


def _check_username_unique(conn: BusinessConnection, username: str) -> None:
    """Check that username is not already taken."""
    exists = conn.execute(
        "SELECT 1 FROM users WHERE username = %s",
        (username,),
    ).fetchone()
    if exists:
        raise ValueError(f"Username {username} already exists")


def _hash_sub_account_password(password: str) -> str:
    """Hash at the boundary; a policy violation is the stable 400 code.

    Plaintext is never persisted or echoed — the same rule and error code as
    the public registration lane (``/api/customer/register``).
    """
    try:
        return hash_password(password)
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "WEAK_PASSWORD", "message": str(exc)},
        ) from exc


def _fetch_sub_account(conn: BusinessConnection, sub_account_id: str) -> dict[str, object]:
    """Load one SUB account row as the API response shape (is_active → bool)."""
    row = conn.execute(
        "SELECT id, username, display_name, parent_user_id, is_active, "
        "(password_hash IS NOT NULL) AS has_password, "
        "created_at, updated_at FROM users WHERE id = %s AND account_type = 'SUB'",
        (sub_account_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Sub-account {sub_account_id} not found")
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "parent_user_id": row["parent_user_id"],
        "is_active": bool(row["is_active"]),
        "has_password": bool(row["has_password"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _write_sub_account_audit(
    conn: BusinessConnection,
    *,
    actor_user_id: str,
    action: str,
    entity_id: str,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (id, actor_user_id, action, entity_type,
                                entity_id, metadata_json)
        VALUES (%s, %s, %s, 'user', %s, %s)
        """,
        (
            str(uuid.uuid4()),
            actor_user_id,
            action,
            entity_id,
            json.dumps({"reason": reason}, ensure_ascii=False),
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/sub-accounts")
def create_sub_account(
    body: CreateSubAccountRequest,
    conn: Database,
    admin: AdminWriter,
) -> dict[str, object]:
    """Create a sub-account under a master account."""
    try:
        _validate_parent_exists(conn, parent_id=body.parent_user_id)
        _check_username_unique(conn, body.username)

        sub_id = f"sub-{uuid.uuid4().hex[:12]}"
        password_hash = None
        registration_source = None
        if body.initial_password is not None:
            # A credentialed sub-account is written whole: the hash and the
            # admin_create origin land in the same INSERT so no half-account
            # (origin without credential or vice versa) can exist.
            password_hash = _hash_sub_account_password(body.initial_password)
            registration_source = "admin_create"
        row = conn.execute(
            """
            INSERT INTO users (id, username, display_name, parent_user_id,
                              account_type, role, is_active,
                              password_hash, registration_source)
            VALUES (%s, %s, %s, %s, 'SUB', 'customer', 1, %s, %s)
            RETURNING created_at
            """,
            (
                sub_id,
                body.username,
                body.display_name,
                body.parent_user_id,
                password_hash,
                registration_source,
            ),
        ).fetchone()
        _write_sub_account_audit(
            conn,
            actor_user_id=admin.user_id,
            action="sub_account.create",
            entity_id=sub_id,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_SUB_ACCOUNT_REQUEST", "message": str(exc)},
        ) from exc

    return {
        "id": sub_id,
        "username": body.username,
        "display_name": body.display_name,
        "account_type": "SUB",
        "parent_user_id": body.parent_user_id,
        "is_active": True,
        "has_password": password_hash is not None,
        "created_at": row["created_at"] if row is not None else None,
    }


@router.get("/sub-accounts")
def list_sub_accounts(
    conn: Database,
    _: AdminReader,
    parent_user_id: str = Query(..., description="Master account ID"),
) -> dict[str, object]:
    """List all sub-accounts under a master account."""
    rows = conn.execute(
        """
        SELECT id, username, display_name, parent_user_id,
               CASE WHEN is_active = 1 THEN true ELSE false END AS is_active,
               (password_hash IS NOT NULL) AS has_password,
               created_at, updated_at
        FROM users
        WHERE parent_user_id = %s AND account_type = 'SUB'
        ORDER BY created_at DESC
        """,
        (parent_user_id,),
    ).fetchall()
    return {
        "sub_accounts": [
            {
                "id": r["id"],
                "username": r["username"],
                "display_name": r["display_name"],
                "parent_user_id": r["parent_user_id"],
                "is_active": bool(r["is_active"]),
                "has_password": bool(r["has_password"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ],
        "total_count": len(rows),
    }


@router.patch("/sub-accounts/{sub_account_id}")
def update_sub_account(
    sub_account_id: str,
    body: UpdateSubAccountRequest,
    conn: Database,
    admin: AdminWriter,
) -> dict[str, object]:
    """Update sub-account details (display_name, status)."""
    updates: list[str] = []
    params: list[object] = []

    if body.display_name is not None:
        updates.append("display_name = %s")
        params.append(body.display_name)

    if body.is_active is not None:
        # users.is_active is an integer 0/1 column (001_core CHECK constraint)
        updates.append("is_active = %s")
        params.append(1 if body.is_active else 0)

    if updates:
        params.append(sub_account_id)
        conn.execute(
            f"UPDATE users SET {', '.join(updates)}, updated_at = NOW() WHERE id = %s",
            params,
        )
        _write_sub_account_audit(
            conn,
            actor_user_id=admin.user_id,
            action="sub_account.update",
            entity_id=sub_account_id,
            reason=body.reason,
        )

    try:
        return _fetch_sub_account(conn, sub_account_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "SUB_ACCOUNT_NOT_FOUND", "message": str(exc)},
        ) from exc


@router.post("/sub-accounts/{sub_account_id}/password")
def reset_sub_account_password(
    sub_account_id: str,
    body: ResetSubAccountPasswordRequest,
    conn: Database,
    admin: AdminWriter,
) -> dict[str, object]:
    """Set or rotate a sub-account's login password.

    Writing ``registration_source='admin_create'`` alongside the hash is what
    admits the account on the shared password-session rule (login and session
    fence). Re-running rotates the password in place; an unknown sub row is
    answered 404. The plaintext never touches the database or the audit trail.
    """
    row = conn.execute(
        "SELECT parent_user_id FROM users WHERE id = %s AND account_type IN ('SUB', 'SUB_ADMIN')",
        (sub_account_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SUB_ACCOUNT_NOT_FOUND",
                "message": f"Sub-account {sub_account_id} not found",
            },
        )
    password_hash = _hash_sub_account_password(body.password)
    conn.execute(
        "UPDATE users SET password_hash = %s, registration_source = 'admin_create', "
        "updated_at = NOW() WHERE id = %s",
        (password_hash, sub_account_id),
    )
    # Credential rotation kills the live session in the same transaction: an
    # old session must not outlive the old password (customer-lane parity).
    revoke_session(
        conn.raw,
        user_id=sub_account_id,
        actor_user_id=admin.user_id,
        reason=REASON_SUB_ACCOUNT_PASSWORD_RESET,
        request_id=body.request_id or str(uuid.uuid4()),
        now_iso=transaction_now_iso(conn.raw),
    )
    _write_sub_account_audit(
        conn,
        actor_user_id=admin.user_id,
        action="sub_account.password.reset",
        entity_id=sub_account_id,
        reason=body.reason,
    )
    return {"id": sub_account_id, "has_password": True}


@router.delete("/sub-accounts/{sub_account_id}", status_code=200)
def delete_sub_account(
    sub_account_id: str,
    conn: Database,
    admin: AdminWriter,
) -> dict[str, object]:
    """Delete a sub-account, purging its session-state/device footprint first.

    Those rows go before the user row in the same transaction (their
    ``user_id`` FKs carry no CASCADE). Business history (ledger, tasks) and
    the append-only session-event log (029) are deliberately never deleted:
    when such rows pin the account the DELETE answers 409 and the operator
    deactivates instead.
    """
    row = conn.execute(
        "SELECT id FROM users WHERE id = %s AND account_type = 'SUB'",
        (sub_account_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SUB_ACCOUNT_NOT_FOUND",
                "message": f"Sub-account {sub_account_id} not found",
            },
        )

    try:
        for table in SUB_ACCOUNT_FOOTPRINT_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE user_id = %s", (sub_account_id,))
        conn.execute(
            "DELETE FROM users WHERE id = %s AND account_type = 'SUB'",
            (sub_account_id,),
        )
    except IntegrityConstraintError as exc:
        # A ledger/task FK without CASCADE refused to orphan the history; the
        # whole transaction rolls back and the operator deactivates instead.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SUB_ACCOUNT_HAS_HISTORY",
                "message": "该子账号存在资金/任务/会话历史数据，无法物理删除，请改为停用。",
            },
        ) from exc
    _write_sub_account_audit(
        conn,
        actor_user_id=admin.user_id,
        action="sub_account.delete",
        entity_id=sub_account_id,
        reason="sub-account deleted",
    )

    return {"deleted": True, "sub_account_id": sub_account_id}
